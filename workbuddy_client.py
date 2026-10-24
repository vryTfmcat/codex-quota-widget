"""Read-only billing adapter using a separately authorized WorkBuddy client session.

Reference protocol: CaoDonald/buddyToken and Chikage0o0/stravia-vendor-workbuddy.
No client-file decryption, account switching, rewards, chat or task endpoints.
"""
import base64
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from workbuddy_api import CreditClient, ApiError, NoRedirect

ORIGIN = 'https://www.workbuddy.cn'
ZONE = dt.timezone(dt.timedelta(hours=8))
SUMMARY = '/billing/meter/get-user-resource-summary'
PAID = '/billing/meter/get-user-resource-paid-packages'
FREE = '/billing/meter/get-user-resource-free-packages'
AUTH = '/v2/plugin/auth/'
ALLOWED = {SUMMARY, PAID, FREE, AUTH+'state', AUTH+'token', AUTH+'token/refresh'}


def decimal(value):
    if value is None or isinstance(value,bool) or value == '':
        raise ApiError('missing-credit-value')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ApiError('invalid-credit-value') from None
    if not number.is_finite() or number < 0:
        raise ApiError('invalid-credit-value')
    return number


def pick(row, keys):
    for key in keys:
        if row.get(key) is not None and row[key] != '':
            return decimal(row[key])
    raise ApiError('missing-credit-value')


def epoch(value):
    if value is None or value == '':
        return None
    try:
        if isinstance(value,(int,float)):
            result = float(value)/1000 if value > 1e11 else float(value)
            dt.datetime.fromtimestamp(result,ZONE)
            return result if result > 0 else None
        date = dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return date.replace(tzinfo=ZONE).timestamp() if date.tzinfo is None else date.timestamp()
    except (ValueError,TypeError,OverflowError,OSError):
        return None


def normalize_batch(row):
    total = pick(row, ('CycleCapacitySizePrecise','CycleCapacitySize','CapacitySizePrecise','CapacitySize'))
    left = pick(row, ('CycleCapacityRemainPrecise','CycleCapacityRemain','CapacityRemainPrecise','CapacityRemain'))
    used = pick(row, ('CycleCapacityUsedPrecise','CycleCapacityUsed','CapacityUsedPrecise','CapacityUsed'))
    if abs(total-left-used) > Decimal('.0001'):
        raise ApiError('inconsistent-batch-values')
    # ExpiredTime can be a consumption/completion timestamp, not package expiry.
    candidates = [epoch(row.get(key)) for key in ('CycleEndTime','DeductionEndTime')]
    end = min((v for v in candidates if v is not None),default=None)
    if end is None:
        raise ApiError('missing-batch-expiry')
    identity = str(row.get('AccountId') or '')+'|'+str(row.get('ResourceCycleId') or '')
    if identity == '|':
        raise ApiError('missing-batch-identity')
    return {'source_batch_id':identity, 'type':str(row.get('PackageName') or '积分套餐'),
            'total':float(total), 'used':float(used), 'remaining':float(left),
            'expires_at':dt.datetime.fromtimestamp(end,ZONE).isoformat(timespec='seconds')}


def parse_summary(data, now, account_id):
    rows = data.get('Packages')
    if not isinstance(rows,list) or not rows:
        raise ApiError('missing-credit-packages')
    total = sum((decimal(r.get('CycleTotalCapacity')) for r in rows),Decimal(0))
    used = sum((decimal(r.get('CycleUsedCapacity')) for r in rows),Decimal(0))
    remaining = sum((decimal(r.get('CycleRemainCapacity')) for r in rows),Decimal(0))
    if abs(remaining-decimal(data.get('TotalCredits'))) > Decimal('.0001'):
        raise ApiError('inconsistent-credit-total')
    categories = []
    groups = data.get('CreditGroups')
    if isinstance(groups,list) and groups:
        for row in groups:
            categories.append({'name':str(row.get('Name') or row.get('Kind') or '其他积分'),
                               'remaining':float(decimal(row.get('CycleRemainCapacity')))})
        if abs(sum((decimal(r['remaining']) for r in categories),Decimal(0))-remaining) > Decimal('.0001'):
            raise ApiError('inconsistent-credit-groups')
    return {'at':now,'total':float(total),'used':float(used),'remaining':float(remaining),
            'source':'client-billing','account_id':account_id,'categories':categories}


class ClientBilling(CreditClient):
    def __init__(self,path):
        super().__init__(path)
        self.pending = CreditClient(self.path.with_name('workbuddy-client-pending.dpapi'))

    def request(self,path,credentials=None,body=None,query=None,extra=None):
        if path not in ALLOWED:
            raise ApiError('invalid-endpoint')
        headers = {'Content-Type':'application/json','Accept':'application/json',
                   'X-Requested-With':'XMLHttpRequest','User-Agent':'WorkBuddy/5.8.3',
                   'Origin':ORIGIN,'Referer':ORIGIN+'/profile/plans-usage'}
        if credentials:
            for key,value in {'Authorization':'Bearer '+credentials['access_token'],
                    'X-User-Id':credentials['uid'],'X-Domain':credentials['domain'],
                    'X-Client-Platform':'web'}.items():
                if '\r' in value or '\n' in value:
                    raise ApiError('invalid-credential-header')
                headers[key] = value
        if extra:
            headers.update(extra)
        url = ORIGIN+path+('?' + urllib.parse.urlencode(query) if query else '')
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url,data=payload,headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect).open(request,timeout=25) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            raise ApiError({401:'login',403:'permission',429:'rate-limit'}.get(error.code,'client-http')) from None
        except (OSError,ValueError):
            raise ApiError('client-network-or-response') from None
        if not isinstance(data,dict):
            raise ApiError('client-response')
        code = data.get('code')
        if code == 11217:
            raise ApiError('login-pending')
        if code in (401,403,10085):
            raise ApiError('login')
        if code not in (0,200):
            raise ApiError('client-business-error')
        result = data.get('data')
        if not isinstance(result,dict):
            raise ApiError('client-response')
        return result

    def start_login(self):
        data = self.request(AUTH+'state',body={},query={'platform':'workbuddy'})
        state,url = data.get('state'),data.get('authUrl')
        parsed = urllib.parse.urlsplit(url or '')
        if not state or parsed.scheme != 'https' or parsed.hostname != 'www.workbuddy.cn' or parsed.username or parsed.password:
            raise ApiError('invalid-client-login-url')
        self.pending._save({'state':state,'at':time.time()})
        return url

    def store_token(self,data,old=None):
        token = data.get('accessToken')
        if not isinstance(token,str) or not token:
            raise ApiError('client-token-response')
        try:
            claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1]+'==='))
            uid = claims['sub']
        except (ValueError,KeyError,IndexError):
            raise ApiError('client-token-response') from None
        if old and old['uid'] != uid:
            raise ApiError('account-changed')
        expires = claims.get('exp')
        if not isinstance(expires,(int,float)):
            raise ApiError('client-token-expiry')
        saved = {'access_token':token,'refresh_token':data.get('refreshToken') or (old or {}).get('refresh_token'),
                 'uid':uid,'domain':data.get('domain') or (old or {}).get('domain') or 'www.workbuddy.cn',
                 'expires_at':expires}
        self._save(saved)
        return saved

    def poll_login(self):
        pending = self.pending._read()
        if time.time()-pending['at'] > 600:
            raise ApiError('client-login-expired')
        data = self.request(AUTH+'token',query={'state':pending['state']})
        with self.lock:
            self.store_token(data)

    def authorize(self,open_browser,cancel):
        open_browser(self.start_login())
        end = time.monotonic()+600
        while not cancel.is_set() and time.monotonic()<end:
            try:
                self.poll_login()
                return self.fetch()
            except ApiError as error:
                if str(error) != 'login-pending':
                    raise
            cancel.wait(3)
        raise ApiError('client-login-expired')

    def refresh(self,old):
        if not old.get('refresh_token'):
            raise ApiError('login')
        data = self.request(AUTH+'token/refresh',old,body={},extra={
            'X-Refresh-Token':old['refresh_token'],'X-Auth-Refresh-Source':'plugin'})
        # Some deployed versions nest the refresh payload once more.
        if isinstance(data.get('data'),dict):
            data = data['data']
        return self.store_token(data,old)

    def detail(self,path,codes,credentials):
        result = []
        for page in range(1,101):
            body = {'PageNumber':page,'PageSize':200,'Status':[0,3],'PackageCodes':codes}
            if path == PAID:
                body['NeedRenewInfo'] = True
            else:
                day = dt.datetime.now(ZONE).strftime('%Y-%m-%d')
                body.update(SlicePeriodStartTime=day+' 00:00:00',SlicePeriodEndTime=day+' 23:59:59')
            data = self.request(path,credentials,body)
            rows,count = data.get('Accounts'),data.get('TotalCount')
            if not isinstance(rows,list) or not isinstance(count,int):
                raise ApiError('client-details-response')
            result.extend(rows)
            if len(result) >= count:
                return result
            if not rows:
                raise ApiError('client-details-incomplete')
        raise ApiError('client-details-incomplete')

    def fetch(self):
        with self.lock:
            credentials = self._read()
            if credentials.get('expires_at',0) <= time.time()+60:
                credentials = self.refresh(credentials)
            try:
                data = self.request(SUMMARY,credentials,{})
            except ApiError as error:
                if str(error) != 'login':
                    raise
                credentials = self.refresh(credentials)
                data = self.request(SUMMARY,credentials,{})
            now = time.time()
            result = parse_summary(data,now,hashlib.sha256(credentials['uid'].encode()).hexdigest()[:16])
            try:
                codes = [r['PackageCode'] for r in data['Packages']]
                records = self.detail(PAID,codes,credentials)+self.detail(FREE,codes,credentials)
                batches = [normalize_batch(row) for row in records]
                identities = [row['source_batch_id'] for row in batches]
                if len(identities) != len(set(identities)):
                    raise ApiError('duplicate-batch-identity')
                if abs(sum((decimal(r['remaining']) for r in batches),Decimal(0))-decimal(result['remaining'])) > Decimal('.0001'):
                    raise ApiError('batch-summary-mismatch')
                result['snapshot'] = {'schema_version':1,'live':False,
                    'captured_date':dt.datetime.fromtimestamp(now,ZONE).date().isoformat(),
                    'captured_at':dt.datetime.fromtimestamp(now,ZONE).isoformat(timespec='seconds'),
                    'source':'client-billing','total_granted':result['total'],'total_used':result['used'],
                    'total_remaining':result['remaining'],'batches':batches,
                    'account_balance':{'remaining':result['remaining'],'categories':result['categories']},
                    'model_display_rates':[]}
            except (ApiError,KeyError) as error:
                result['detail_error'] = str(error) if isinstance(error,ApiError) else 'client-details-response'
            return result
