"""Store numeric account quota observations. No credentials or conversations."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
import datetime as dt
from collections import defaultdict

GENERATED = '<!-- codex-quota-generated:v1 -->'


def date_label(at, fmt='%Y-%m-%d %H:%M:%S'):
    return dt.datetime.fromtimestamp(at).strftime(fmt) if at else '—'


def number(value, suffix=''):
    return f'{value:g}{suffix}' if value is not None else '—'


def write_generated(path, text):
    if path.exists():
        old = path.read_text(encoding='utf-8')
        if GENERATED not in old:
            raise RuntimeError('markdown-target-not-owned')
        if old == text:
            return False
    temp = path.with_suffix('.md.tmp')
    temp.write_text(text, encoding='utf-8')
    temp.replace(path)
    return True


def window(data, minutes):
    return next((w for w in data.get('windows', []) if w.get('windowDurationMins') == minutes), None)


class UsageHistory:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            existing={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'samples','errors'}.issubset(existing):
                db.executescript('''
                CREATE TABLE IF NOT EXISTS samples (
                    at REAL PRIMARY KEY,
                    five_remaining REAL, five_reset REAL,
                    week_remaining REAL, week_reset REAL,
                    credits INTEGER, source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS errors (at REAL PRIMARY KEY, category TEXT NOT NULL);
                PRAGMA user_version = 1;
                ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def record(self, data, source='auto'):
        five, week = window(data, 300), window(data, 10080)
        def remaining(w):
            return max(0, min(100, 100-float(w['usedPercent']))) if w else None
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (float(data['at']), remaining(five), five.get('resetsAt') if five else None,
                        remaining(week), week.get('resetsAt') if week else None,
                        data.get('credits'), source if source in ('auto', 'manual') else 'auto'))

    def record_error(self, category):
        # Deliberately fixed categories, never exception messages or HTTP URLs.
        category = category if category in ('network', 'login', 'empty', 'initialize') else 'unavailable'
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO errors VALUES (?, ?)', (time.time(), category))

    def recent(self, now=None):
        now = now or time.time()
        with self.connection() as db:
            rows = db.execute('SELECT * FROM samples WHERE at >= ? ORDER BY at DESC LIMIT 2000',
                              (now-86400,)).fetchall()
            return [dict(row) for row in reversed(rows)]

    def baseline(self, week_reset):
        if not week_reset:
            return None
        with self.connection() as db:
            row = db.execute('''SELECT * FROM samples WHERE week_reset BETWEEN ? AND ?
                AND five_reset > at AND five_remaining IS NOT NULL AND week_remaining IS NOT NULL
                ORDER BY at LIMIT 1''', (week_reset-90, week_reset+90)).fetchone()
        if not row:
            return None
        return {'at': row['at'], 'windows': [
            {'windowDurationMins': 300, 'usedPercent': 100-row['five_remaining'], 'resetsAt': row['five_reset']},
            {'windowDurationMins': 10080, 'usedPercent': 100-row['week_remaining'], 'resetsAt': row['week_reset']}
        ]}

    def export_markdown(self, vault_root, relative_dir):
        vault = Path(vault_root).resolve()
        if not vault.is_dir() or not (vault/'.obsidian').is_dir():
            raise RuntimeError('vault-not-found')
        target = (vault/relative_dir).resolve()
        if not target.is_relative_to(vault) or target == vault:
            raise RuntimeError('markdown-target-outside-vault')
        with self.connection() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM samples ORDER BY at')]
        if not rows:
            return {'samples': 0, 'files': 0}
        target.mkdir(parents=True, exist_ok=True)
        groups = defaultdict(list)
        for index,row in enumerate(rows):
            before=rows[index-1] if index else None
            delta=quota_delta(before,row,'week') if before else None
            span=row['at']-before['at'] if before else None
            state='首次采样' if before is None else '重置/修正/缺失字段' if delta is None else '长间隔' if span>900 else '正常'
            groups[date_label(row['at'],'%Y-%m')].append((row,span,delta,state))
        count=0
        for month,items in groups.items():
            latest=items[-1][0]['at']
            lines=[
                '---', f'title: Codex额度使用记录-{month}', 'type: log', 'generated: true',
                f'updated: {date_label(latest,"%Y-%m-%d")}', 'tags:', '  - 个人记录', '  - Codex', '  - 使用统计', '---', '',
                GENERATED, f'# Codex额度使用记录 · {month}', '',
                '> 自动生成的只读查看稿；每次成功采样后更新。修改此稿不会写回原始数据库。', '',
                '总览：[[Codex额度使用记录]]。时间为本机时区，区间消耗单位为完整周额度的百分点。', '',
                f'本月共 {len(items)} 个成功读数；最后读数：{date_label(latest)}。', '',
                '手动刷新可能短于 10 分钟；长间隔不补点；重置/读数修正不作为普通消耗。',
                '同账号其他设备的使用也包含在内。百分比未变化只表示接口读数未变。', '',
                '| 读数时间 | 5h剩余 | 周剩余 | 实际间隔（分钟） | 本区间周消耗（百分点） | 区间状态 | 5h重置时间 | 周重置时间 | 来源 |',
                '| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |']
            for row,span,delta,state in reversed(items):
                lines.append('| '+' | '.join([
                    date_label(row['at']),number(row['five_remaining'],'%'),number(row['week_remaining'],'%'),
                    f'{span/60:.2f}' if span is not None else '—',number(delta),state,
                    date_label(row['five_reset']),date_label(row['week_reset']),
                    '手动刷新' if row['source']=='manual' else '自动/启动采样'])+' |')
            lines.extend(['', '本页从库外 SQLite 全量再生成，可重新恢复；未采集的历史不会推测补齐。', ''])
            count+=write_generated(target/f'Codex额度使用记录-{month}.md','\n'.join(lines))
        daily=defaultdict(lambda:{'samples':0,'spent':0,'known':0,'unknown':0,'gaps':0})
        for items in groups.values():
            for row,span,delta,state in items:
                day=daily[date_label(row['at'],'%Y-%m-%d')]
                day['samples']+=1
                if delta is not None:
                    day['spent']+=delta
                    day['known']+=1
                if state=='重置/修正/缺失字段':day['unknown']+=1
                if state=='长间隔':day['gaps']+=1
        latest=rows[-1]
        lines=['---','title: Codex额度使用记录','type: log','generated: true',
               f'updated: {date_label(latest["at"],"%Y-%m-%d")}', 'tags:', '  - 个人记录','  - Codex','  - 使用统计','---','',
               GENERATED,'# Codex额度使用记录','',
               '> 自动生成的个人使用记录查看稿。原始数据在 本机 SQLite；只由本机额度工具自动更新。','',
               '## 最近读数','',f'- 时间：{date_label(latest["at"])}（本机时间）。',
               f'- 5h 剩余：{number(latest["five_remaining"],"%")}；周剩余：{number(latest["week_remaining"],"%")}。',
               f'- 5h 重置：{date_label(latest["five_reset"])}；周重置：{date_label(latest["week_reset"])}。',
               f'- 可用重置卡：{latest["credits"] if latest["credits"] is not None else "未知"}；累计成功采样：{len(rows)} 次。','',
               '## 每日汇总','',
               '按区间结束日归集；跨午夜或长间隔不拆分估计。这里是已观测到的额度下降，不是精确 token 计费。','',
               '| 日期 | 成功采样 | 已观测周消耗（百分点） | 重置/修正区间 | 长间隔 |',
               '| --- | ---: | ---: | ---: | ---: |']
        for date,day in sorted(daily.items(),reverse=True):
            spent=number(day['spent']) if day['known'] else '—'
            lines.append(f'| {date} | {day["samples"]} | {spent} | {day["unknown"]} | {day["gaps"]} |')
        lines.extend(['','## 按月完整明细',''])
        for month in sorted(groups,reverse=True):lines.append(f'- [[Codex额度使用记录-{month}|{month} 全部采样]]（{len(groups[month])} 条）。')
        lines.extend(['','## 来源与恢复','',f'- 原始数据库：`{self.path}`。',
                      '- 工具说明和统计口径请参阅仓库 README。',
                      '- 自动更新间隔约 10 分钟；手动刷新成功也更新。工具关闭、睡眠或读取失败时，不会产生新的成功读数。',
                      '- 只记录额度数字、重置时间、采样时间和来源；不保存对话、凭据或任务正文。',
                      '- 本目录是自动生成的 Markdown 查看稿，不作为第二份可编辑数据源；可从数据库重新生成。',''])
        count+=write_generated(target/'Codex额度使用记录.md','\n'.join(lines))
        return {'samples':len(rows),'files':int(count)}


def quota_delta(before, after, prefix):
    a, b = before.get(prefix+'_remaining'), after.get(prefix+'_remaining')
    ar, br = before.get(prefix+'_reset'), after.get(prefix+'_reset')
    if a is None or b is None or ar is None or br is None:
        return None
    if abs(ar-br) > 90 or b > a+0.001:
        return None  # reset, a reset credit, account change, or an upstream correction
    return max(0, a-b)


def intervals(rows):
    result = []
    for a, b in zip(rows, rows[1:]):
        seconds = b['at']-a['at']
        if seconds <= 0:
            continue
        result.append({'start': a['at'], 'end': b['at'], 'seconds': seconds,
                       'week': quota_delta(a, b, 'week'), 'five': quota_delta(a, b, 'five'),
                       'gap': seconds > 900, 'source': b['source']})
    return result
