"""WorkBuddy official OAuth client; credentials are encrypted for this Windows user."""
import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.error
import urllib.parse
import urllib.request

BASE = 'https://www.workbuddy.cn/openapi/v2/'
SCOPE = 'user.credit.readable'
REDIRECT = 'http://localhost:8766/workbuddy/callback'


class ApiError(RuntimeError):
    """Only fixed, non-secret error categories may leave the client."""


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def protect(value, decrypt=False):
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ApiError('credential-storage')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError('unexpected-redirect')


def request_json(endpoint, form=None, access_token=None):
    if endpoint not in ('token', 'credit'):
        raise ApiError('invalid-endpoint')
    headers = {'Accept': 'application/json', 'User-Agent': 'PersonalCreditMonitor/1.0'}
    if access_token:
        headers['Authorization'] = 'Bearer ' + access_token
    payload = None
    if form is not None:
        payload = urllib.parse.urlencode(form).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(BASE+endpoint, data=payload, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=25) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        category = {401:'login', 403:'permission', 404:'endpoint-unavailable', 429:'rate-limit'}.get(error.code, 'http')
        raise ApiError(category) from None
    except (OSError, ValueError):
        raise ApiError('network-or-response') from None
    if not isinstance(data, dict):
        raise ApiError('invalid-response')
    return data


def parse_credit(data, now):
    values = data.get('data') if isinstance(data.get('data'), dict) else data
    total, used = values.get('total_capacity_size'), values.get('total_capacity_used')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in (total, used)):
        raise ApiError('invalid-credit-response')
    if used > total:
        raise ApiError('inconsistent-credit-response')
    return {'at': now, 'total': total, 'used': used, 'remaining': round(total-used, 6), 'source':'official-api'}


def valid_callback(path, expected_state):
    parsed = urllib.parse.urlsplit(path)
    params = urllib.parse.parse_qs(parsed.query)
    if parsed.path != '/workbuddy/callback' or not secrets.compare_digest(params.get('state', [''])[0], expected_state):
        raise ApiError('invalid-callback')
    if params.get('error'):
        raise ApiError('authorization-denied')
    code = params.get('code', [''])[0]
    if not code:
        raise ApiError('missing-code')
    return code


class CreditClient:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    @property
    def configured(self):
        return self.path.is_file()

    def _read(self):
        try:
            return json.loads(protect(self.path.read_bytes(), decrypt=True))
        except (OSError, ValueError):
            raise ApiError('not-configured') from None

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = protect(json.dumps(data).encode())
        temp = self.path.with_suffix('.tmp')
        temp.write_bytes(blob)
        os.replace(temp, self.path)

    def configure(self, client_id, client_secret):
        if not client_id.strip() or not client_secret.strip():
            raise ApiError('missing-client-credentials')
        with self.lock:
            self._save({'client_id':client_id.strip(), 'client_secret':client_secret.strip()})

    def authorization_url(self, state):
        with self.lock:
            data = self._read()
        return BASE+'authorize?'+urllib.parse.urlencode({'response_type':'code', 'client_id':data['client_id'],
                'redirect_uri':REDIRECT, 'scope':SCOPE, 'state':state})

    def _store_tokens(self, credentials, result, require_scope=False):
        token = result.get('access_token')
        if not isinstance(token, str) or not token:
            raise ApiError('token-response')
        scope = result.get('scope', '')
        if scope and SCOPE not in scope.split():
            raise ApiError('permission')
        if require_scope and not scope:
            raise ApiError('permission-not-confirmed')
        lifetime = result.get('expires_in')
        if isinstance(lifetime, bool) or not isinstance(lifetime, (float,int)) or not math.isfinite(lifetime) or lifetime <= 0:
            raise ApiError('token-expiry')
        credentials.update(access_token=token, expires_at=time.time()+lifetime)
        if result.get('refresh_token'):
            credentials['refresh_token'] = result['refresh_token']
        self._save(credentials)
        return credentials

    def exchange(self, code):
        with self.lock:
            data = self._read()
            result = request_json('token', form={'grant_type':'authorization_code', 'code':code,
                'client_id':data['client_id'], 'client_secret':data['client_secret'], 'redirect_uri':REDIRECT})
            self._store_tokens(data, result, require_scope=True)

    def _refresh(self, data):
        if not data.get('refresh_token'):
            raise ApiError('login')
        result = request_json('token', form={'grant_type':'refresh_token', 'refresh_token':data['refresh_token'],
            'client_id':data['client_id'], 'client_secret':data['client_secret']})
        return self._store_tokens(data, result)

    def fetch(self):
        with self.lock:
            data = self._read()
            if not data.get('access_token'):
                raise ApiError('login')
            if data.get('expires_at', 0) <= time.time()+60:
                data = self._refresh(data)
            try:
                result = request_json('credit', access_token=data['access_token'])
            except ApiError as error:
                if str(error) != 'login':
                    raise
                data = self._refresh(data)
                result = request_json('credit', access_token=data['access_token'])
            return parse_credit(result, time.time())

    def authorize(self, open_browser, cancel):
        """A transient loopback listener, active only during explicit authorization."""
        state = secrets.token_urlsafe(32)
        client = self
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log OAuth query strings.

            def do_GET(self):
                if self.headers.get('Host') != 'localhost:8766':
                    self.send_error(400)
                    return
                try:
                    code = valid_callback(self.path, state)
                except ApiError as error:
                    self.send_error(400)
                    if str(error) == 'authorization-denied':
                        received['error'] = error
                    return
                try:
                    client.exchange(code)
                    received['ok'] = True
                    body = '授权成功，可关闭此页面，返回额度悬浮窗。'.encode('utf-8')
                    self.send_response(200)
                except ApiError as error:
                    received['error'] = error
                    body = '授权未完成，请回到悬浮窗查看状态。'.encode('utf-8')
                    self.send_response(400)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

        try:
            server = HTTPServer(('127.0.0.1',8766), Handler)
        except OSError:
            raise ApiError('callback-port-in-use') from None
        with server:
            server.timeout = .5
            open_browser(self.authorization_url(state))
            end = time.monotonic()+300
            while not received and not cancel.is_set() and time.monotonic()<end:
                server.handle_request()
        if received.get('error'):
            raise received['error']
        if not received.get('ok'):
            raise ApiError('authorization-cancelled-or-timeout')
        return self.fetch()


class CreditHistory:
    """Account totals are observations, never a per-batch spending ledger."""
    def __init__(self, path):
        self.path = Path(path)
        self.samples = json.loads(self.path.read_text(encoding='utf-8'))['samples'] if self.path.exists() else []

    def record(self, sample):
        entries = self.samples+[sample]
        atomic_json(self.path, {'schema_version':1, 'samples':entries})
        self.samples = entries

    def export_markdown(self, path):
        marker = '<!-- workbuddy-live-generated:v1 -->'
        path = Path(path)
        old = path.read_text(encoding='utf-8') if path.exists() else ''
        if old and marker not in old:
            raise ApiError('markdown-not-owned')
        lines = ['---', 'title: WorkBuddy实时积分记录', 'generated: true', '---', marker,
                 '# WorkBuddy 实时积分记录', '',
                 '腾讯接口的账户读数，与手动快照分开保存；客户端接口可提供分类和批次，缺失时不作零处理。',
                 '总余额变化可能来自使用、过期或赠送，不统一标记为消费。时间 UTC+8。', '',
                 '| 读取时间 | 总额度 | 已用 | 剩余 |', '| --- | ---: | ---: | ---: |']
        import datetime as dt
        zone = dt.timezone(dt.timedelta(hours=8))
        for row in reversed(self.samples):
            stamp = dt.datetime.fromtimestamp(row['at'],zone).strftime('%Y-%m-%d %H:%M:%S')
            lines.append(f'| {stamp} | {row["total"]:g} | {row["used"]:g} | {row["remaining"]:g} |')
        latest = self.samples[-1] if self.samples else {}
        snapshot = latest.get('snapshot')
        if snapshot:
            lines.extend(['', '## 最近成功读取的分类与批次', '',
                '；'.join(f'{r["name"]} {r["remaining"]:g}' for r in latest.get('categories',[])), '',
                '| 到期时间 | 套餐 | 总额 | 已用 | 剩余 |', '| --- | --- | ---: | ---: | ---: |'])
            for row in sorted(snapshot['batches'],key=lambda r:r['expires_at']):
                name = row['type'].replace('|','／').replace('\n',' ')
                lines.append(f'| {row["expires_at"]} | {name} | {row["total"]:g} | {row["used"]:g} | {row["remaining"]:g} |')
            lines.extend(['', '## 逐批变化', '', '以平台批次身份与已用字段比较，不假设先到期先扣。', ''])
            from workbuddy_usage import changes
            previous = None
            account = None
            for sample in self.samples:
                current = sample.get('snapshot')
                if not current:
                    continue
                if account != sample.get('account_id'):
                    previous = None
                notes = changes(previous,current)
                if previous is None or notes != ['逐批额度无变化；未把总余额下降自动计为消费。']:
                    when = dt.datetime.fromtimestamp(sample['at'],zone).strftime('%Y-%m-%d %H:%M:%S')
                    lines.extend(['### '+when, ''])
                    lines.extend('- '+note for note in notes)
                    lines.append('')
                previous,account = current,sample.get('account_id')
        text = '\n'.join(lines)+'\n'
        if old != text:
            path.parent.mkdir(parents=True,exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(text,encoding='utf-8')
            os.replace(temp,path)
