"""Personal, offline WorkBuddy snapshot adapter. No credentials or network IO."""
import datetime as dt
import hashlib
import json
import math
import os
from collections import Counter
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return parsed.timestamp()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('积分必须为非负有限数字')
    return float(value)


def validate(data):
    if data.get('schema_version') != 1 or data.get('live') is not False:
        raise ValueError('需要 schema_version=1、live=false 的本地快照')
    dt.date.fromisoformat(data['captured_date'])
    if data.get('captured_at'):
        timestamp(data['captured_at'])
    batches = data.get('batches')
    if not isinstance(batches, list) or not batches:
        raise ValueError('快照缺少逐批明细，不能解释为零余额')
    for row in batches:
        timestamp(row['expires_at'])
        total, used, remaining = (number(row[key]) for key in ('total', 'used', 'remaining'))
        if abs(total-used-remaining) > .011:
            raise ValueError('批次总额、已用与剩余不一致')
    for key, field in (('total_granted', 'total'), ('total_used', 'used'), ('total_remaining', 'remaining')):
        if abs(number(data[key])-sum(number(row[field]) for row in batches)) > .011:
            raise ValueError('汇总与逐批明细不一致')
    rates = data.get('model_display_rates', [])
    if not isinstance(rates, list):
        raise ValueError('模型倍率参考必须是列表')
    for rate in rates:
        if not isinstance(rate.get('name'), str):
            raise ValueError('模型参考缺少名称')
        number(rate['display_multiplier'])
    balance = data.get('account_balance')
    if balance is not None:
        remaining = number(balance['remaining'])
        categories = balance['categories']
        if not categories or abs(sum(number(row['remaining']) for row in categories)-remaining) > .011:
            raise ValueError('账户总剩余与积分分类不一致')
    return data


def batch_key(row):
    if row.get('source_batch_id'):
        return 'id:' + str(row['source_batch_id'])
    # Snapshot row numbers are not platform batch identities.
    return json.dumps([row.get('type'), timestamp(row['expires_at']), number(row['total'])], ensure_ascii=False)


def changes(old, new):
    """Only explicitly increased used fields count as observed consumption."""
    if not old:
        return ['首次导入：建立基线，无可比较区间。']
    if new['captured_date'] < old['captured_date']:
        return ['采集日期早于上一份：历史补录，不推断消费。']
    counts = Counter(batch_key(row) for row in old['batches'] + new['batches'])
    before = {batch_key(row): row for row in old['batches']}
    after = {batch_key(row): row for row in new['batches']}
    result = []
    for key, row in after.items():
        label = row['expires_at'][:19].replace('T', ' ')
        previous = before.get(key)
        if previous is None:
            result.append(f'{label}：新增记录 {row["remaining"]:g}，是否新赠送未知。')
        elif counts[key] != 2:
            result.append(f'{label}：匹配不唯一，变化归因未知。')
        else:
            used = row['used']-previous['used']
            drop = previous['remaining']-row['remaining']
            if used > .001 and abs(drop-used) <= .011 and row['total'] == previous['total']:
                result.append(f'{label}：已用字段增加 {used:g}（观察到的消费）。')
            elif abs(drop) > .001 or abs(used) > .001:
                result.append(f'{label}：余额变化 {-drop:+g}，归因未知。')
    for key, row in before.items():
        if key not in after:
            result.append(f'{row["expires_at"][:19]}：批次记录消失，消费/到期/范围变化未知。')
    balance, previous_balance = new.get('account_balance'), old.get('account_balance')
    if balance != previous_balance and balance:
        if previous_balance:
            delta = balance['remaining']-previous_balance['remaining']
            result.append(f'账户总剩余变化 {delta:+g}，原因未单独核实，不归入消费或赠送。')
        else:
            result.append(f'补充账户总剩余 {balance["remaining"]:g}，此前仅记录奖励批次；不作为新增赠送。')
    return result or ['逐批额度无变化；未把总余额下降自动计为消费。']


class SnapshotStore:
    def __init__(self, path):
        self.path = Path(path)
        self.entries = []
        if self.path.exists():
            self.entries = json.loads(self.path.read_text(encoding='utf-8'))['entries']
            for entry in self.entries:
                validate(entry['snapshot'])

    @property
    def latest(self):
        return self.entries[-1]['snapshot'] if self.entries else None

    def import_file(self, source, now):
        data = validate(json.loads(Path(source).read_text(encoding='utf-8-sig')))
        if self.latest and data['captured_date'] < self.latest['captured_date']:
            raise ValueError('旧日期快照不能替换当前快照')
        if self.latest and data.get('captured_at') and self.latest.get('captured_at'):
            if timestamp(data['captured_at']) < timestamp(self.latest['captured_at']):
                raise ValueError('旧时刻快照不能替换当前快照')
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.entries and digest == self.entries[-1]['hash']:
            return False
        entry = {'imported_at': now, 'hash': digest, 'snapshot': data, 'changes': changes(self.latest, data)}
        entries = self.entries + [entry]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps({'schema_version': 1, 'entries': entries}, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, self.path)
        self.entries = entries
        return True

    def export_markdown(self, path):
        marker = '<!-- workbuddy-snapshot-generated:v1 -->'
        path = Path(path)
        if not self.latest:
            return
        old = path.read_text(encoding='utf-8') if path.exists() else ''
        if old and marker not in old:
            raise ValueError('不覆盖非本工具生成的笔记')
        data = self.latest
        lines = ['---', 'title: WorkBuddy积分使用记录', 'generated: true', '---',
                 marker, '# WorkBuddy 积分使用记录', '',
                 '本地快照导出的个人查看稿，非实时余额。原始历史在工具目录 data/workbuddy-history.json。',
                 '时间 UTC+8；导入时间不等于采集时间。不假设先到期先扣，不按倍率推算费用。', '',
                 f'采集：{data.get("captured_at") or data["captured_date"] + "（准确时刻未知）"}',
                 f'所列批次当时总额 {data["total_granted"]:g}，已用 {data["total_used"]:g}，剩余 {data["total_remaining"]:g}。', '',
                 '## 当前快照逐批明细', '', '| 到期时间 | 总额 | 已用 | 剩余 |', '| --- | ---: | ---: | ---: |']
        for row in sorted(data['batches'], key=lambda r: timestamp(r['expires_at'])):
            lines.append(f'| {row["expires_at"]} | {row["total"]:g} | {row["used"]:g} | {row["remaining"]:g} |')
        balance = data.get('account_balance')
        if balance:
            details = ['', '## 手动更新的账户总余额', '', f'总剩余 **{balance["remaining"]:,.2f}**；非实时。',
                       '分类：' + '；'.join(f'{row["name"]} {row["remaining"]:g}' for row in balance['categories']) + '。',
                       '套餐基础积分的到期时刻尚未提供，不纳入奖励批次到期倒计时。', '']
            lines[lines.index('## 当前快照逐批明细'):lines.index('## 当前快照逐批明细')] = details
        lines.extend(['', '## 导入与变化历史', '', '同日无精确采集时刻时，仅按导入顺序对比，不能计算每小时用量。', ''])
        for entry in self.entries:
            imported = dt.datetime.fromtimestamp(entry['imported_at'], TZ).isoformat(timespec='seconds')
            lines.extend([f'### 导入 {imported}', '', f'采集日期 {entry["snapshot"]["captured_date"]}；当时剩余 {entry["snapshot"]["total_remaining"]:g}。', ''])
            lines.extend('- ' + change for change in entry['changes'])
            lines.append('')
        content = '\n'.join(lines) + '\n'
        if old != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(content, encoding='utf-8')
            os.replace(temp, path)


def balance_view(data, now):
    positive = sorted((r for r in data['batches'] if r['remaining'] > 0), key=lambda r: timestamp(r['expires_at']))
    eligible = [r for r in positive if timestamp(r['expires_at']) > now]
    expired = [r for r in positive if timestamp(r['expires_at']) <= now]
    return {'next': eligible[0] if eligible else None,
            'unexpired_snapshot': sum(r['remaining'] for r in eligible),
            'past_due_snapshot': sum(r['remaining'] for r in expired)}


def expiry_notice(data, now, stale=False):
    """Quiet title badge for positive balances expiring in the next 72 hours."""
    import math
    from decimal import Decimal
    due = [(timestamp(r['expires_at'])-now, Decimal(str(r['remaining'])))
           for r in data['batches'] if r['remaining'] > 0
           and 0 < timestamp(r['expires_at'])-now <= 3*86400]
    if not due:
        return ''
    seconds = min(t for t, _ in due)
    remaining = sum((amount for _, amount in due), Decimal(0))
    amount = f'{remaining:,.2f}' if remaining >= Decimal('0.01') else '<0.01'
    duration = f'{math.ceil(seconds/3600)}小时' if seconds >= 3600 else f'{math.ceil(seconds/60)}分钟'
    return (f'3日内到期 {amount} 积分 · 最近剩{duration}' +
            (' · 快照待刷新' if stale else ''))
