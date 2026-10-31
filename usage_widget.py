"""Local Windows quota widget. Only reads Codex account data; no inference."""
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
from tkinter import font as tkfont
import urllib.request
import webbrowser
import winsound
import winreg
import math
import io
import wave
from array import array
from usage_history import UsageHistory, intervals, quota_delta
from workbuddy_usage import SnapshotStore, balance_view, expiry_notice, timestamp as wb_timestamp
from workbuddy_api import CreditHistory, ApiError
from workbuddy_client import ClientBilling

APP_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('CODEX_QUOTA_DATA_DIR', str(Path(os.environ['LOCALAPPDATA']) / 'CodexQuotaWidgetPortable'))).expanduser().resolve()
ROOT.mkdir(parents=True, exist_ok=True)
WEEKLY_PER_WINDOW = float(os.environ.get('CODEX_QUOTA_WEEKLY_PER_WINDOW', '16'))
if not 0 < WEEKLY_PER_WINDOW <= 100:
    raise ValueError('CODEX_QUOTA_WEEKLY_PER_WINDOW must be in (0, 100]')
STATE = ROOT / 'widget-state.json'
STATUS = ROOT / 'widget-status.json'
API = 'https://codex-resets.com/api/resets'
BG, PANEL, FG, MUTED, GREEN = '#17191e', '#23262e', '#f1f2f6', '#a4a9b8', '#9cddb6'
ACTIVE = set()
ACTIVE_LOCK = threading.Lock()
ALARM_PATH = Path(os.environ['WINDIR']) / 'Media/Alarm02.wav'
QUOTA_REFRESH_INTERVALS = {'等待重置10s': 10, '每30秒': 30, '每1分钟': 60, '每2分钟': 120,
                           '每5分钟': 300, '每10分钟': 600, '每30分钟': 1800}


def alarm_wave(volume):
    """Use the user's fixed Alarm02 sound, once, with volume scaling."""
    with wave.open(str(ALARM_PATH), 'rb') as original:
        params = original.getparams()
        samples = array('h', original.readframes(original.getnframes()))
    gain = max(0, min(100, float(volume))) / 100
    samples = array('h', (int(s*gain) for s in samples))
    out = io.BytesIO()
    with wave.open(out, 'wb') as sound:
        sound.setparams(params)
        sound.writeframes(samples.tobytes())
    return out.getvalue()


def iso_time(value):
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def tibo_notice(data, now):
    """Prefer an announced schedule over a speculative observation."""
    candidates = []
    latest_at = iso_time((data.get('latest') or {}).get('announced_at'))
    for kind, field in (('scheduled', 'scheduled_for'), ('watch', 'expires_at')):
        item = data.get(kind)
        if not isinstance(item, dict):
            continue
        deadline = iso_time(item.get(field))
        if deadline is None or not math.isfinite(deadline):
            continue
        announced = iso_time(item.get('announced_at'))
        if latest_at and announced and latest_at > announced:
            continue  # A subsequent completed reset supersedes this prediction.
        candidates.append(dict(item, kind=kind, deadline=deadline, active=deadline > now))
    return next((item for item in candidates if item['active']), candidates[0] if candidates else None)


def notice_signature(notice):
    return '|'.join(str(notice.get(key) or '') for key in ('kind', 'tweet_id', 'deadline')) if notice else ''


def five_idle(window):
    # This is a display policy requested by the user, not proof about backend timing.
    return window.get('windowDurationMins') == 300 and window.get('usedPercent') == 0


def five_summary(five, now):
    left = max(0, 100-five['usedPercent'])
    if five_idle(five):
        return '5h 剩 100%   尚未开始使用'
    sec = max(0, int((five.get('resetsAt') or now)-now))
    countdown = f'{sec//3600:02}:{sec%3600//60:02}:{sec%60:02}'
    return f'5h 剩 {left:g}%   {countdown}' if sec else f'5h 上次剩 {left:g}%   待核对重置'


def compact_summary(five, now, stale=False):
    if not five or five.get('usedPercent') is None:
        return '5h —\n待读取'
    left = max(0, min(100, 100-five['usedPercent']))
    if five_idle(five):
        countdown = '待开始'
    else:
        seconds = max(0, int((five.get('resetsAt') or now)-now))
        countdown = f'{seconds//3600:02}:{seconds%3600//60:02}:{seconds%60:02}' if seconds else '待核对'
    return f'5h {left:g}%'+(' · 旧' if stale else '')+'\n'+countdown


def dock_position(bounds, width, height, side, y):
    left, top, right, bottom = bounds
    return (left if side == 'left' else max(left, right-width),
            min(max(top, y), max(top, bottom-height)))


def promote_topmost(tk_handle):
    """Promote Tk's actual Windows wrapper without activating or moving it."""
    from ctypes import wintypes
    user = ctypes.windll.user32
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.SetWindowPos.restype = wintypes.BOOL
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongW.restype = ctypes.c_long
    hwnd = user.GetAncestor(tk_handle, 2) or tk_handle  # GA_ROOT, not Tk's child HWND.
    flags = 0x0001 | 0x0002 | 0x0010 | 0x0200  # NOSIZE, NOMOVE, NOACTIVATE, NOOWNERZORDER.
    if not user.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, flags):
        return False
    return bool(user.GetWindowLongW(hwnd, -20) & 0x0008)  # WS_EX_TOPMOST


def forecast(windows, now, deadline=None):
    five = next((w for w in windows if w.get('windowDurationMins') == 300), None)
    week = next((w for w in windows if w.get('windowDurationMins') == 10080), None)
    if not five or not week:
        return None
    remaining = max(0, 100-week['usedPercent'])
    if five_idle(five):
        return {'idle': True, 'remaining': remaining, 'current_capacity': WEEKLY_PER_WINDOW,
                'earliest': now, 'finish_by': now, 'slots': [], 'next_reset': None,
                'week_reset': week.get('resetsAt'), 'points': [(now, remaining)], 'at_deadline': None}
    if not five.get('resetsAt') or five['resetsAt'] <= now:
        return None
    current = WEEKLY_PER_WINDOW * max(0, 100-five['usedPercent']) / 100
    rest = max(0, remaining-current)
    windows_needed = math.ceil(max(0, rest-1e-9)/WEEKLY_PER_WINDOW)
    earliest = now if not windows_needed else five['resetsAt']+(windows_needed-1)*18000
    points = [(now, remaining), (five['resetsAt'], remaining),
              (five['resetsAt'], max(0, remaining-current))]
    slots = [{'start': now, 'end': five['resetsAt'], 'after': max(0, remaining-current)}]
    left = max(0, remaining-current)
    for n in range(windows_needed):
        start = five['resetsAt']+n*18000
        when = start+18000
        points.extend([(when, left), (when, max(0, left-WEEKLY_PER_WINDOW))])
        left = max(0, left-WEEKLY_PER_WINDOW)
        slots.append({'start': start, 'end': when, 'after': left})
    by_deadline = None
    if deadline and deadline >= now:
        # A window opening exactly at the deadline has no time before it.
        count = max(0, math.ceil((deadline-five['resetsAt'])/18000))
        by_deadline = max(0, remaining-current-WEEKLY_PER_WINDOW*count)
    return {'remaining': remaining, 'current_capacity': current, 'earliest': earliest,
            'finish_by': slots[-1]['end'], 'slots': slots,
            'next_reset': five['resetsAt'], 'week_reset': week.get('resetsAt'),
            'points': points, 'at_deadline': by_deadline}


def timeline_layout(f, now, deadline=None):
    """One linear scale for both the graph and every window card."""
    start = now-18000
    current_start = now if f.get('idle') else f['next_reset']-18000
    slots = [dict(slot) for slot in f['slots']]
    if slots:
        slots[0]['start'] = current_start
    end = max(f['finish_by'], deadline or 0, now+60)
    top, scale = 54, 150/18000
    y = lambda when: top+(when-start)*scale
    return {'start': start, 'end': end, 'current_start': current_start,
            'slots': slots, 'top': top, 'bottom': y(end), 'now_y': y(now),
            'height': math.ceil(y(end)+76), 'y': y}


def visible_theory_points(theory, start, end):
    """Clip the reference curve, never drawing before its recorded baseline."""
    begin=max(start,theory['points'][0][0])
    if begin>end:
        return []
    remaining=theory['remaining']
    for when,value in theory['points']:
        if when<=begin:
            remaining=value
    result=[(begin,remaining)]
    for when,value in theory['points']:
        if begin<when<=end:
            result.append((when,value))
            remaining=value
    if result[-1][0]<end:
        result.append((end,remaining))
    return result


def stamp(t, fmt='%m-%d %H:%M:%S'):
    return dt.datetime.fromtimestamp(t).strftime(fmt) if t else '未知'


def load(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def codex_path():
    found = shutil.which('codex')
    if found:
        return found
    base = Path(os.environ['LOCALAPPDATA']) / 'OpenAI/Codex/bin'
    candidates = list(base.glob('*/codex.exe'))
    if not candidates:
        raise RuntimeError('codex-missing')
    return str(max(candidates, key=lambda p: p.stat().st_mtime))


class QuotaReadError(RuntimeError):
    pass


def read_quota():
    for attempt in range(3):
        try:
            result = read_quota_once()
            result['attempts'] = attempt+1
            return result
        except (QuotaReadError, OSError, TimeoutError, queue.Empty):
            if attempt == 2:
                raise
            time.sleep(.7*(attempt+1))


def read_quota_once():
    p = subprocess.Popen([codex_path(), 'app-server'], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         encoding='utf-8', text=True, creationflags=0x08000000)
    with ACTIVE_LOCK:
        ACTIVE.add(p)
    lines = queue.Queue()
    def consume():
        for line in p.stdout:
            lines.put(line)
        lines.put(None)
    threading.Thread(target=consume, daemon=True).start()
    try:
        def send(msg):
            p.stdin.write(json.dumps(msg)+'\n')
            p.stdin.flush()
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'local_quota_widget', 'version': '1.0'}}})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = lines.get(timeout=max(.1, deadline - time.monotonic()))
            if line is None:
                break
            data = json.loads(line)
            if data.get('id') == 1:
                if 'error' in data:
                    raise QuotaReadError('initialize')
                send({'method': 'initialized'})
                send({'id': 2, 'method': 'account/rateLimits/read'})
            if data.get('id') == 2:
                if 'error' in data:
                    message = str(data['error'].get('message', '')).lower()
                    reason = 'login' if ('401' in message or 'authentication required' in message) else 'network'
                    raise QuotaReadError(reason)
                r = data['result']
                limits = (r.get('rateLimitsByLimitId') or {}).get('codex') or r.get('rateLimits') or {}
                windows = [limits.get(k) for k in ('primary', 'secondary') if limits.get(k)]
                if not windows:
                    raise QuotaReadError('empty')
                return {'windows': windows,
                        'credits': (r.get('rateLimitResetCredits') or {}).get('availableCount'),
                        'at': time.time()}
        raise TimeoutError('codex-timeout')
    finally:
        if p.poll() is None:
            p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        p.stdin.close()
        p.stdout.close()
        with ACTIVE_LOCK:
            ACTIVE.discard(p)


def read_tibo():
    req = urllib.request.Request(API, headers={'User-Agent': 'Mozilla/5.0 (LocalQuotaWidget)', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as response:
        data = json.load(response)
    events = data.get('events')
    if not isinstance(events, list) or not events:
        raise ValueError('tibo-empty')
    return {'latest': events[0], 'events': events, 'scheduled': data.get('scheduled'),
            'watch': data.get('watch'), 'at': time.time()}




def window_key(w):
    # Backend timestamps sometimes vary by a second; group the same reset minute.
    return f"{w['windowDurationMins']}:{int(w.get('resetsAt') or 0)//60}"


def tibo_changed(latest, state):
    previous = state.get('tibo_seen_at')
    current = latest.get('announced_at', '')
    state['tibo_seen_at'] = max(previous or '', current)
    return bool(previous and current > previous)


def quota_alerts(windows, state):
    alerts = []
    sent = state.setdefault('thresholds', [])
    for w in windows:
        label = '5小时' if w['windowDurationMins'] == 300 else '周' if w['windowDurationMins'] == 10080 else f"{w['windowDurationMins']}分钟"
        used = w.get('usedPercent')
        if used is None:
            continue
        crossed = [n for n in (80, 90, 100) if used >= n and f'{window_key(w)}:{n}' not in sent]
        if crossed:
            alerts.append(f'{label}额度剩余 {max(0, 100-used):g}%')
            sent.extend(f'{window_key(w)}:{n}' for n in crossed)
    state['thresholds'] = sent[-40:]
    return alerts


def due_alerts(now, state):
    alerts = []
    fired = state.setdefault('reset_fired', [])
    for w in state.get('pending_windows', []):
        if five_idle(w):
            continue
        reset = w.get('resetsAt')
        key = window_key(w)
        if reset and now >= reset and key not in fired:
            label = '5小时' if w['windowDurationMins'] == 300 else '周'
            alerts.append(f'{label}额度预定重置时间已到（{stamp(reset)}）。正在核对最新额度。')
            fired.append(key)
    state['reset_fired'] = fired[-20:]
    return alerts


def reset_changes(before, after):
    """Ignore consumption, missing fields and small timestamp corrections."""
    previous = {w.get('windowDurationMins'): w for w in before.get('windows', [])}
    messages = []
    for current in after.get('windows', []):
        duration = current.get('windowDurationMins')
        old = previous.get(duration)
        if duration not in (300, 10080) or not old:
            continue
        a, b = old.get('usedPercent'), current.get('usedPercent')
        if a is None or b is None:
            continue
        label = '5h' if duration == 300 else '每周'
        if b < a-0.001:
            messages.append(f'{label}剩余额度：{max(0,100-a):g}% → {max(0,100-b):g}%')
        # An idle 5h window receiving its first task is not a reset event.
        old_time, new_time = old.get('resetsAt'), current.get('resetsAt')
        if old_time and new_time and abs(new_time-old_time)>90 and not five_idle(old):
            messages.append(f'{label}重置时间：{stamp(old_time)} → {stamp(new_time)}')
    return messages


class Widget:
    def __init__(self, smoke=False):
        self.root = tk.Tk()
        self.root.title('Codex 用量与重置提醒')
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', 1.0)
        self.state = load(STATE)
        self.quota = self.state.get('quota') or {}
        self.tibo = self.state.get('tibo') or {}
        self.wb_store = None
        self.wb_error = None
        self.wb_export_error = None
        self.wb_checked = 0
        self.wb_config = load(ROOT / 'workbuddy.local.json')
        self.wb_api = ClientBilling(ROOT / 'data/workbuddy-client.dpapi')
        self.wb_live = None
        self.wb_live_history = None
        self.wb_live_export_error = None
        try:
            self.wb_live_history = CreditHistory(ROOT / 'data/workbuddy-credit-history.json')
            self.wb_live = self.wb_live_history.samples[-1] if self.wb_live_history.samples else None
        except Exception:
            self.wb_live_export_error = '线上读数历史读取失败；原文件未覆盖。'
        try:
            self.wb_store = SnapshotStore(ROOT / 'data/workbuddy-history.json')
        except Exception as error:
            self.wb_error = '本地历史读取失败：' + type(error).__name__
        self.errors = {}
        self.history_error = None
        self.markdown_error = None
        self.markdown_at = None
        self.history_rows = []
        self.history_base = None
        try:
            self.history = UsageHistory(ROOT / 'data/usage-history.sqlite3')
            self.history_rows = self.history.recent()
            weekly = next((w for w in self.quota.get('windows',[]) if w.get('windowDurationMins')==10080),None)
            self.history_base = self.history.baseline(weekly.get('resetsAt') if weekly else None)
        except Exception as error:
            self.history = None
            self.history_error = type(error).__name__
        self.export_markdown()
        self.q = queue.Queue()
        self.busy = set()
        self.expanded = False
        self.closed = threading.Event()
        self.last_start = {'quota': 0, 'tibo': 0, 'workbuddy-live': 0}
        saved_interval = self.state.get('quota_refresh_seconds', 600)
        self.normal_refresh_seconds = saved_interval if type(saved_interval) is int and saved_interval in QUOTA_REFRESH_INTERVALS.values() and saved_interval != 10 else 600
        self.quota_refresh_seconds = self.normal_refresh_seconds
        self.reset_watch = self.quota_refresh_seconds < 600
        self.reset_watch_baseline = None
        self.dock_enabled = self.state.get('dock_enabled', True)
        self.dock_side = self.state.get('dock_side', 'right')
        self.dock_compact = False
        self.dock_dragging = False
        self.dock_hover_job = None
        self.dock_hide_job = None
        self.last_topmost = 0
        self.topmost_ok = None
        self.sound_stop = threading.Event()
        self.root.geometry(self.state.get('position', '+80+65'))
        self.build()
        self.root.bind('<Enter>', self.dock_enter, add='+')
        self.root.bind('<Leave>', self.dock_leave, add='+')
        self.root.bind('<Map>', lambda event: self.root.after_idle(self.ensure_topmost), add='+')
        if self.dock_enabled:
            self.dock_hide()
        self.root.after_idle(self.ensure_topmost)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(200, self.tick)
        if smoke:
            self.root.after(1600, self.smoke)

    def label(self, parent, text='', size=10, fg=FG, **kw):
        label = tk.Label(parent, text=text, bg=BG, fg=fg, font=('Microsoft YaHei UI', size), **kw)
        self.bind_drag(label)
        return label

    def bind_drag(self, widget):
        widget.bind('<Button-1>', self.drag_start)
        widget.bind('<B1-Motion>', self.drag)
        widget.bind('<ButtonRelease-1>', self.drag_end)
        widget.configure(cursor='fleur')

    def section(self, text):
        card = tk.Frame(self.details, bg=BG, pady=8)
        card.pack(fill='x', pady=(0, 5))
        self.bind_drag(card)
        body = tk.Frame(card, bg=BG)
        opened = self.state.setdefault('sections', {}).get(text, True)
        def toggle_section():
            visible = self.state['sections'].get(text, True)
            self.state['sections'][text] = not visible
            if visible:
                body.pack_forget()
            else:
                body.pack(fill='x')
            heading.config(text=('▸ ' if visible else '▾ ') + text)
            self.persist()
        header = tk.Frame(card, bg=BG)
        header.pack(fill='x', pady=(0, 7))
        heading = tk.Button(header, text=('▾ ' if opened else '▸ ')+text,
                            command=toggle_section, bg=BG, fg=GREEN, activebackground=PANEL,
                            activeforeground=GREEN, anchor='w', relief='flat', bd=0,
                            cursor='hand2', font=('Microsoft YaHei UI', 14, 'bold'))
        heading.pack(side='left', fill='x', expand=True)
        if text == 'WorkBuddy 积分':
            self.wb_expiry_badge = self.label(header, '', 9, '#ffc66d',
                                             wraplength=340, justify='right', anchor='e')
            self.wb_expiry_badge.pack(side='right', padx=(8, 0))
            self.wb_expiry_badge.bind('<Button-1>', lambda event: heading.invoke())
        line = tk.Frame(card, bg='#3a404d', height=1)
        line.pack(fill='x', pady=(0, 7))
        if opened:
            body.pack(fill='x')
        self.section_controls[text] = (heading, body)
        return body

    def button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, bg=PANEL, fg=FG,
                         activebackground='#383d49', activeforeground=FG,
                         relief='flat', bd=0, padx=9, pady=4, cursor='hand2',
                         font=('Microsoft YaHei UI', 9))

    def autostart(self):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
            try:
                winreg.QueryValueEx(key, 'CodexQuotaWidgetPortable')
                winreg.DeleteValue(key, 'CodexQuotaWidgetPortable')
                message = '已关闭登录自动启动。当前窗口继续运行。'
            except FileNotFoundError:
                pythonw = str(Path(sys.executable).with_name('pythonw.exe'))
                cmd = subprocess.list2cmdline([pythonw, str(Path(__file__).resolve())])
                winreg.SetValueEx(key, 'CodexQuotaWidgetPortable', 0, winreg.REG_SZ, cmd)
                message = '已开启登录自动启动。可以再次点击同一按钮关闭。'
        self.alert(message)

    def build(self):
        self.section_controls = {}
        self.mini = tk.Frame(self.root, bg=BG, padx=5, pady=3)
        self.mini_label = self.label(self.mini, '5h —\n待读取', 9, GREEN, justify='center')
        self.mini_label.pack()
        for element in (self.mini, self.mini_label):
            element.bind('<Button-1>', self.header_press)
            element.bind('<B1-Motion>', self.header_motion)
            element.bind('<ButtonRelease-1>', self.header_release)
            element.configure(cursor='hand2')
        bar = tk.Frame(self.root, bg=BG, padx=12, pady=8)
        self.bar = bar
        bar.pack(fill='x')
        drag = self.label(bar, '⠿ CODEX', 10, GREEN)
        drag.pack(side='left', padx=(0, 12))
        self.main = self.label(bar, '读取额度…', 11)
        self.main.pack(side='left')
        self.refresh_button = self.button(bar, '刷新', self.refresh)
        self.refresh_button.pack(side='left', padx=(12, 0))
        self.refresh_interval_var = tk.StringVar(value=self.refresh_interval_label())
        self.refresh_interval_menu = tk.OptionMenu(bar, self.refresh_interval_var,
                                                 *QUOTA_REFRESH_INTERVALS, command=self.set_refresh_interval)
        self.refresh_interval_menu.config(bg=PANEL, fg=FG, activebackground='#375368', activeforeground=FG,
                                          highlightthickness=0, bd=0, font=('Microsoft YaHei UI', 10),
                                          cursor='hand2', padx=9, pady=6)
        self.refresh_interval_menu['menu'].config(bg=PANEL, fg=FG, activebackground='#375368',
                                                 activeforeground=FG, font=('Microsoft YaHei UI', 10))
        self.refresh_interval_menu.pack(side='left', padx=(6, 0))
        self.chevron = self.label(bar, '▾', 11, MUTED)
        self.chevron.pack(side='left', padx=(10, 0))
        self.dock_button = self.button(bar, '贴边：开' if self.dock_enabled else '贴边：关', self.toggle_dock)
        self.dock_button.pack(side='left', padx=(6, 0))
        for element in (bar, drag, self.main, self.chevron):
            element.bind('<Button-1>', self.header_press)
            element.bind('<B1-Motion>', self.header_motion)
            element.bind('<ButtonRelease-1>', self.header_release)
            element.configure(cursor='hand2')
        self.button(bar, '×', self.close).pack(side='left', padx=(6, 0))
        self.root.update_idletasks()
        self.details_host = tk.Frame(self.root, bg=BG)
        self.detail_canvas = tk.Canvas(self.details_host, bg=BG, highlightthickness=0,
                                       width=max(750, bar.winfo_reqwidth()), height=min(1050, int(self.root.winfo_screenheight()*.65)))
        scrollbar = tk.Scrollbar(self.details_host, orient='vertical', command=self.detail_canvas.yview)
        scrollbar.pack(side='right', fill='y')
        self.detail_canvas.pack(side='left', fill='both', expand=True)
        self.detail_canvas.configure(yscrollcommand=scrollbar.set)
        self.details = tk.Frame(self.detail_canvas, bg=BG, padx=14, pady=8)
        detail_window = self.detail_canvas.create_window(0, 0, window=self.details, anchor='nw')
        self.details.bind('<Configure>', self.resize_details)
        self.detail_canvas.bind('<Configure>', lambda e: self.detail_canvas.itemconfigure(detail_window, width=e.width))
        self.root.bind_all('<MouseWheel>', lambda e: self.detail_canvas.yview_scroll(-int(e.delta/120), 'units'))
        sections=self.state.setdefault('sections',{})
        if 'codex用量节奏与重置时间线' not in sections and '用量节奏与重置时间线' in sections:
            sections['codex用量节奏与重置时间线']=sections.pop('用量节奏与重置时间线')
        forecast_card = self.section('codex用量节奏与重置时间线')
        self.quota_detail = self.label(forecast_card, anchor='w', justify='left')
        self.quota_detail.pack(fill='x')
        self.forecast_detail = self.label(forecast_card, anchor='w', justify='left', wraplength=700)
        self.timeline = tk.Canvas(forecast_card, height=160, bg=BG, highlightthickness=0)
        self.timeline.pack(fill='x', pady=5)
        self.forecast_detail.pack(fill='x', pady=(5, 0))
        stats_card = self.section('实际使用统计')
        self.stats_detail = self.label(stats_card, anchor='w', justify='left', wraplength=700)
        self.stats_detail.pack(fill='x')
        self.stats_canvas = tk.Canvas(stats_card, height=160, bg=BG, highlightthickness=0)
        self.stats_canvas.pack(fill='x', pady=6)
        self.stats_canvas.bind('<Motion>', self.stats_hover)
        self.stats_canvas.bind('<Leave>', lambda e: self.stats_hover_text.config(text='悬停柱子可看实际采样时间和消耗。'))
        self.stats_hover_text = self.label(stats_card, '悬停柱子可看实际采样时间和消耗。', 9, MUTED,
                                           anchor='w', justify='left', wraplength=700)
        self.stats_hover_text.pack(fill='x')
        self.button(stats_card, '打开统计数据文件夹', lambda: os.startfile(str(ROOT / 'data'))).pack(anchor='w', pady=5)
        tibo_card = self.section('Tibo 重置公告')
        self.tibo_detail = self.label(tibo_card, anchor='w', justify='left', wraplength=700)
        self.tibo_detail.pack(fill='x')
        self.button(tibo_card, '打开预告原帖', self.open_tibo).pack(anchor='w', pady=(6, 0))
        self.state.setdefault('sections', {}).setdefault('WorkBuddy 积分', False)
        wb_card = self.section('WorkBuddy 积分')
        self.wb_live_detail = self.label(wb_card, anchor='w', justify='left', wraplength=700)
        self.wb_live_detail.pack(fill='x', pady=(0, 7))
        live_actions = tk.Frame(wb_card, bg=BG)
        live_actions.pack(fill='x', pady=(0, 8))
        self.button(live_actions, '登录 / 重新登录', self.wb_authorize_api).pack(side='left', padx=(0, 6))
        self.button(live_actions, '查询线上余额', self.wb_fetch_api).pack(side='left')
        self.wb_summary = self.label(wb_card, anchor='w', justify='left', wraplength=700)
        self.wb_summary.pack(fill='x')
        actions = tk.Frame(wb_card, bg=BG)
        actions.pack(fill='x', pady=6)
        self.button(actions, '导入积分快照 JSON', self.wb_import).pack(side='left', padx=(0, 6))
        self.button(actions, '重新读取快照', self.wb_reload).pack(side='left', padx=(0, 6))
        self.wb_reminder_button = self.button(actions, '标题提示：开', self.wb_toggle_reminders)
        self.wb_reminder_button.pack(side='left')
        self.button(wb_card, '打开库内积分记录', self.wb_open_record).pack(anchor='w', pady=(0, 6))
        style = ttk.Style(self.root)
        style.theme_use('clam')
        table_font = tkfont.Font(root=self.root, family='Microsoft YaHei UI', size=9)
        style.configure('WorkBuddy.Treeview', background=PANEL, fieldbackground=PANEL, foreground=FG,
                        font=table_font, rowheight=table_font.metrics('linespace')+14,
                        borderwidth=0, lightcolor=PANEL, darkcolor=PANEL)
        style.map('WorkBuddy.Treeview', background=[('selected', '#375368')], foreground=[('selected', FG)])
        style.configure('WorkBuddy.Treeview.Heading', background='#303641', foreground=GREEN,
                        font=('Microsoft YaHei UI', 9, 'bold'), padding=(10, 9), relief='flat')
        style.map('WorkBuddy.Treeview.Heading', background=[('active', '#3a4350')])
        self.wb_table_font = table_font
        self.wb_table = ttk.Treeview(wb_card, style='WorkBuddy.Treeview', columns=('expiry', 'remaining', 'state'), show='headings', height=7)
        for column, label, width in [('expiry', '到期时间（本机时区）', table_font.measure('2026-11-18 00:27:58')+32), ('remaining', '快照剩余积分', table_font.measure('快照剩余积分')+36), ('state', '状态', table_font.measure('到期待核对')+42)]:
            self.wb_table.heading(column, text=label)
            self.wb_table.column(column, width=width, minwidth=width, anchor='w')
        self.wb_table.pack(fill='x')
        self.wb_table.bind('<MouseWheel>', self.wb_scroll)
        self.wb_history_detail = self.label(wb_card, size=9, fg=MUTED, anchor='w', justify='left', wraplength=700)
        self.wb_history_detail.pack(fill='x', pady=6)
        self.button(wb_card, '展开 / 收起模型倍率参考', self.wb_models_toggle).pack(anchor='w')
        self.wb_models_detail = self.label(wb_card, size=9, fg=MUTED, anchor='w', justify='left', wraplength=700)
        sound_card = self.section('提醒与声音')
        self.label(sound_card, '固定提示音：Windows Alarm02 · 每次只播放一次。', 9, MUTED,
                   anchor='w').pack(fill='x', pady=(0, 6))
        sound_row = tk.Frame(sound_card, bg=BG)
        sound_row.pack(fill='x')
        self.label(sound_row, '响铃音量', 10).pack(side='left', padx=(0, 10))
        self.volume = tk.Scale(sound_row, from_=10, to=100, orient='horizontal',
                               bg=BG, fg=FG, troughcolor=PANEL, highlightthickness=0,
                               activebackground=GREEN, length=190, command=self.set_volume)
        self.volume.set(self.state.get('volume', 85))
        self.volume.pack(side='left')
        self.label(sound_card, '音量会自动保存。', 9, MUTED,
                   anchor='w').pack(fill='x', pady=(4, 0))
        buttons = tk.Frame(self.details, bg=BG)
        buttons.pack(fill='x', pady=(12, 3))
        for text, action in [('试听响铃', self.play_alarm),
                             ('Tibo 原站', lambda: webbrowser.open('https://codex-resets.com/zh-CN')),
                             ('静音/开声', self.mute), ('登录自启开关', self.autostart)]:
            button = self.button(buttons, text, action)
            button.pack(side='left', padx=(0, 6))
        self.footer = self.label(self.details, fg=MUTED, size=9, anchor='w', justify='left')
        self.footer.pack(fill='x', pady=(5, 0))
        source = self.label(self.details, '公告数据：codex-resets.com（第三方） ↗', 9, MUTED, cursor='hand2')
        source.pack(anchor='w')
        source.bind('<Button-1>', lambda e: webbrowser.open('https://codex-resets.com'))

    def resize_details(self, event=None):
        self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox('all'),
                                     height=min(1050, int(self.root.winfo_screenheight()*.65),
                                                max(100, self.details.winfo_reqheight())))

    def persist(self):
        save(STATE, self.state)

    def export_markdown(self):
        if not self.history:
            return
        cfg=load(ROOT/'history-export.local.json')
        if not cfg:
            return
        try:
            self.history.export_markdown(cfg['vault_root'],cfg['relative_dir'])
            self.markdown_error=None
            self.markdown_at=time.time()
        except Exception as failure:
            self.markdown_error=type(failure).__name__

    def drag_start(self, e):
        self.dock_dragging = True
        self.origin = (e.x_root-self.root.winfo_x(), e.y_root-self.root.winfo_y())

    def header_press(self, e):
        self.drag_start(e)
        self.header_origin = (e.x_root, e.y_root)
        self.header_moved = False

    def header_motion(self, e):
        if max(abs(e.x_root-self.header_origin[0]), abs(e.y_root-self.header_origin[1])) > 5:
            self.header_moved = True
        if self.header_moved:
            self.drag(e)

    def header_release(self, e):
        self.dock_dragging = False
        distance = max(abs(e.x_root-self.header_origin[0]), abs(e.y_root-self.header_origin[1]))
        if self.header_moved or distance > 5:
            self.drag_end(e)
        else:
            self.toggle()

    def drag(self, e):
        x, y = e.x_root-self.origin[0], e.y_root-self.origin[1]
        self.root.geometry(f'{x:+d}{y:+d}')

    def drag_end(self, e):
        self.dock_dragging = False
        if self.dock_enabled:
            left, top, right, bottom = self.work_area()
            middle = self.root.winfo_x()+self.root.winfo_width()/2
            self.dock_side = 'left' if middle < (left+right)/2 else 'right'
            self.state['dock_side'] = self.dock_side
            self.state['dock_y'] = self.root.winfo_y()
            self.dock_layout()
        self.state['position'] = f'+{max(0,self.root.winfo_x())}+{max(0,self.root.winfo_y())}'
        self.persist()

    def toggle(self):
        self.dock_show()
        self.expanded = not self.expanded
        self.chevron.config(text='▴' if self.expanded else '▾')
        if self.expanded:
            self.details_host.pack(fill='both', expand=True)
        else:
            self.details_host.pack_forget()
        self.dock_layout()

    def work_area(self):
        try:
            from ctypes import wintypes
            class MonitorInfo(ctypes.Structure):
                _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                            ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]
            user = ctypes.windll.user32
            user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user.MonitorFromWindow.restype = wintypes.HANDLE
            user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            monitor = user.MonitorFromWindow(self.root.winfo_id(), 2)
            info = MonitorInfo(); info.cbSize = ctypes.sizeof(info)
            if user.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.rcWork
                return r.left, r.top, r.right, r.bottom
        except (AttributeError, OSError):
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def dock_layout(self):
        if not self.dock_enabled or self.dock_dragging:
            return
        self.root.update_idletasks()
        bounds = self.work_area()
        width = min(self.root.winfo_reqwidth(), bounds[2]-bounds[0])
        height = min(self.root.winfo_reqheight(), bounds[3]-bounds[1])
        y = self.state.get('dock_y', bounds[1]+(bounds[3]-bounds[1])*2//5)
        x, y = dock_position(bounds, width, height, self.dock_side, y)
        self.root.geometry(f'{width}x{height}{x:+d}{y:+d}')
        self.ensure_topmost()


    def ensure_topmost(self):
        if self.closed.is_set() or not self.root.winfo_viewable():
            return
        # Do not cover this app's alerts or interfere with an open dropdown grab.
        if self.root.grab_current() or any(isinstance(w, tk.Toplevel) and w.winfo_viewable()
                                           for w in self.root.winfo_children()):
            return
        self.last_topmost = time.monotonic()
        try:
            self.topmost_ok = promote_topmost(self.root.winfo_id())
        except (AttributeError, OSError):
            self.topmost_ok = False

    def dock_show(self):
        if not self.dock_compact:
            return
        self.dock_compact = False
        self.mini.pack_forget()
        self.bar.pack(fill='x', before=self.details_host) if self.details_host.winfo_manager() else self.bar.pack(fill='x')
        self.dock_layout()

    def dock_hide(self):
        self.dock_hide_job = None
        if not self.dock_enabled or self.expanded or self.dock_dragging:
            return
        self.dock_compact = True
        self.bar.pack_forget()
        self.mini.pack()
        self.dock_layout()

    def dock_enter(self, event=None):
        if self.dock_hide_job:
            self.root.after_cancel(self.dock_hide_job); self.dock_hide_job = None
        if self.dock_enabled and self.dock_compact and not self.dock_hover_job:
            self.dock_hover_job = self.root.after(180, self.dock_hover)

    def dock_hover(self):
        self.dock_hover_job = None
        if not self.dock_dragging and self.pointer_inside():
            self.dock_show()

    def pointer_inside(self):
        x, y = self.root.winfo_pointerxy()
        return (self.root.winfo_x() <= x < self.root.winfo_x()+self.root.winfo_width()
                and self.root.winfo_y() <= y < self.root.winfo_y()+self.root.winfo_height())

    def dock_leave(self, event=None):
        if self.dock_enabled and not self.dock_hide_job:
            self.dock_hide_job = self.root.after(900, self.dock_maybe_hide)

    def dock_maybe_hide(self):
        self.dock_hide_job = None
        if self.closed.is_set():
            return
        if not self.pointer_inside() and not self.root.grab_current():
            self.dock_hide()
        elif not self.expanded:
            self.dock_hide_job = self.root.after(900, self.dock_maybe_hide)

    def toggle_dock(self):
        self.dock_show()
        self.dock_enabled = not self.dock_enabled
        self.state['dock_enabled'] = self.dock_enabled
        self.dock_button.config(text='贴边：开' if self.dock_enabled else '贴边：关')
        self.persist()
        if self.dock_enabled:
            self.dock_layout()
            self.dock_leave()
        else:
            self.root.geometry('')

    def mute(self):
        self.state['muted'] = not self.state.get('muted', False)
        self.persist()

    def set_volume(self, value):
        self.state['volume'] = int(float(value))
        self.persist()

    def stop_sound(self):
        self.sound_stop.set()

    def play_alarm(self):
        self.stop_sound()
        stop = threading.Event()
        self.sound_stop = stop
        sound = alarm_wave(self.state.get('volume', 85))
        def play():
            if not stop.is_set():
                winsound.PlaySound(sound, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        threading.Thread(target=play, daemon=True).start()

    def open_tibo(self):
        notice = tibo_notice(self.tibo, time.time()) or {}
        url = notice.get('tweet_url') or (self.tibo.get('latest') or {}).get('tweet_url')
        if isinstance(url, str) and url.startswith('https://x.com/'):
            webbrowser.open(url)

    def alert(self, message, test=False):
        if test or not self.state.get('muted'):
            self.play_alarm()
        win = tk.Toplevel(self.root)
        win.title('Codex 提醒')
        win.configure(bg=BG)
        win.attributes('-topmost', True)
        self.label(win, message, 12, wraplength=440, justify='left').pack(padx=24, pady=22)
        def dismiss():
            self.stop_sound()
            win.destroy()
        self.button(win, '停止响铃 / 知道了', dismiss).pack(pady=(0, 18))
        win.protocol('WM_DELETE_WINDOW', dismiss)
        win.geometry('+150+160')

    def start(self, name, fn, reason='auto'):
        if name in self.busy:
            return
        self.busy.add(name)
        if name == 'quota':
            self.read_reason = reason
        self.last_start[name] = time.monotonic()
        def work():
            try:
                self.q.put((name, fn(), None))
            except Exception as e:
                # Never write exception text: HTTP exceptions can contain secret URLs.
                reason = str(e) if isinstance(e, (QuotaReadError, ApiError)) else type(e).__name__
                self.q.put((name, None, reason))
        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        self.start('quota', read_quota, reason='manual')
        self.start('tibo', read_tibo)
        self.wb_reload()
        self.wb_fetch_api()

    def wb_fetch_api(self):
        if self.wb_api.configured and 'workbuddy-auth' not in self.busy:
            self.start('workbuddy-live', self.wb_api.fetch)

    def wb_authorize_api(self):
        if not {'workbuddy-auth', 'workbuddy-live'} & self.busy:
            self.start('workbuddy-auth', lambda: self.wb_api.authorize(webbrowser.open, self.closed))


    def wb_show_live(self, now):
        messages = []
        if self.wb_live:
            row = self.wb_live
            old = now-row['at']>1200
            messages.extend([f'线上总剩余 {row["remaining"]:,.2f} 积分' + (' · 旧读数' if old else ''),
                             f'腾讯客户端接口 · 更新 {stamp(row["at"])}',
                             ' · '.join(f'{r["name"]} {r["remaining"]:,.2f}' for r in row.get('categories',[]))])
            if row.get('detail_error'):
                messages.append('总余额已更新，批次读取未完成；下方保留旧批次并注明采集时间。')
        else:
            messages.append('线上余额尚未读取；下方为手动快照。')
        if 'workbuddy-auth' in self.busy:
            messages.append('等待腾讯客户端登录，10 分钟内完成；不依赖应用审核。')
        elif 'workbuddy-live' in self.busy:
            messages.append('正在查询腾讯余额与批次…')
        elif not self.wb_api.configured:
            messages.append('点击“登录 / 重新登录”，不需要开放平台应用审核。')
        else:
            messages.append('线上余额每 10 分钟读取；顶部刷新也会查询。')
        error = self.errors.get('workbuddy-auth') or self.errors.get('workbuddy-live')
        if error:
            labels = {'login':'登录态失效，请重新登录', 'permission':'服务端拒绝读取，请重新登录后重试',
                      'client-login-expired':'登录等待已过期，请重新登录',
                      'permission-not-confirmed':'授权响应未确认积分读取权限',
                      'endpoint-unavailable':'官方余额端点不可用，需核对当前接口',
                      'callback-port-in-use':'本机 8766 端口被占用', 'rate-limit':'服务端限频，稍后重试'}
            messages.append('线上查询/授权未完成：' + labels.get(error, error) + '。旧读数与快照保留。')
        if self.wb_live_export_error:
            messages.append(self.wb_live_export_error)
        self.wb_live_detail.config(text='\n'.join(messages))

    def refresh_interval_label(self):
        return next(label for label, seconds in QUOTA_REFRESH_INTERVALS.items() if seconds == self.quota_refresh_seconds)

    def set_refresh_interval(self, label):
        if label not in QUOTA_REFRESH_INTERVALS:
            return
        self.quota_refresh_seconds = QUOTA_REFRESH_INTERVALS[label]
        self.refresh_interval_var.set(label)
        if self.quota_refresh_seconds != 10:
            self.normal_refresh_seconds = self.quota_refresh_seconds
        # The 10-second watch is temporary; restart with the last normal interval.
        self.state['quota_refresh_seconds'] = self.normal_refresh_seconds
        self.reset_watch = self.quota_refresh_seconds < 600
        # Old cached data must not be mistaken for a newly detected reset.
        self.reset_watch_baseline = self.quota if self.reset_watch and time.time()-self.quota.get('at',0)<=1200 else None
        self.persist()
        self.start('quota', read_quota, reason='reset-watch' if self.reset_watch else 'manual')
        self.display()

    def check_reset_watch(self, data):
        if not self.reset_watch:
            return
        changes = reset_changes(self.reset_watch_baseline, data) if self.reset_watch_baseline else []
        # Only successful, comparable observations form the next baseline.
        if any(w.get('windowDurationMins') in (300,10080) and w.get('usedPercent') is not None
               for w in data.get('windows', [])):
            merged = {w.get('windowDurationMins'): w for w in (self.reset_watch_baseline or {}).get('windows', [])}
            merged.update({w.get('windowDurationMins'): w for w in data.get('windows', []) if w.get('usedPercent') is not None})
            self.reset_watch_baseline = dict(data, windows=list(merged.values()))
        if changes:
            if self.quota_refresh_seconds == 10:
                self.quota_refresh_seconds = self.normal_refresh_seconds
                self.refresh_interval_var.set(self.refresh_interval_label())
                self.reset_watch = self.quota_refresh_seconds < 600
                self.reset_watch_baseline = dict(data, windows=list(merged.values())) if self.reset_watch else None
                self.persist()
                message = f'\n已关闭等待重置，恢复{self.refresh_interval_label()}更新。'
            else:
                message = f'\n继续按{self.refresh_interval_label()}更新。'
            self.alert('检测到额度/重置时间变化：\n'+'\n'.join(changes)+message)

    def wb_scroll(self, event):
        self.wb_table.yview_scroll(-int(event.delta/120), 'units')
        return 'break'

    def wb_toggle_reminders(self):
        self.state['workbuddy_reminders'] = not self.state.get('workbuddy_reminders', True)
        self.persist()

    def wb_import(self):
        path = filedialog.askopenfilename(parent=self.root, title='导入 WorkBuddy 本地积分快照',
                                          filetypes=[('JSON 快照', '*.json')])
        if path:
            self.wb_reload(path)

    def wb_models_toggle(self):
        if self.wb_models_detail.winfo_manager():
            self.wb_models_detail.pack_forget()
        else:
            self.wb_models_detail.pack(fill='x', pady=5)

    def wb_open_record(self):
        for path in (self.wb_config.get('live_markdown_path'), self.wb_config.get('markdown_path')):
            if path and Path(path).is_file():
                os.startfile(path)
                break

    def wb_reload(self, path=None):
        self.wb_checked = time.monotonic()
        if not self.wb_store:
            return
        source = path or self.wb_config.get('snapshot_path')
        if not source:
            self.wb_error = '尚未选择快照文件；点击导入。'
            return
        try:
            self.wb_store.import_file(source, time.time())
            if path:
                self.wb_config['snapshot_path'] = str(Path(path).resolve())
                save(ROOT / 'workbuddy.local.json', self.wb_config)
            self.wb_error = None
        except Exception as error:
            # Preserve the previous snapshot; never turn a read failure into zero.
            self.wb_error = '快照读取失败：' + type(error).__name__ + '；保留上次快照。'
        target = self.wb_config.get('markdown_path')
        if target:
            try:
                self.wb_store.export_markdown(target)
                self.wb_export_error = None
            except Exception as error:
                self.wb_export_error = 'Markdown 导出失败：' + type(error).__name__

    def wb_display(self, now):
        self.wb_show_live(now)
        live_snapshot = (self.wb_live or {}).get('snapshot')
        data = live_snapshot or (self.wb_store.latest if self.wb_store else None)
        self.wb_reminder_button.config(text='标题提示：' + ('开' if self.state.get('workbuddy_reminders', True) else '关'))
        batches_at = (self.wb_live or {}).get('batches_at', (self.wb_live or {}).get('at', 0))
        stale = (not live_snapshot or now-batches_at > 1200 or bool((self.wb_live or {}).get('detail_error'))
                 or bool(self.errors.get('workbuddy-live') or self.errors.get('workbuddy-auth')))
        badge = expiry_notice(data, now, stale) if data and self.state.get('workbuddy_reminders', True) else ''
        self.wb_expiry_badge.config(text=badge)
        if not data:
            self.wb_summary.config(text=self.wb_error or '尚无快照；余额未知。')
            return
        view = balance_view(data, now)
        captured = data.get('captured_at') or (data['captured_date'] + '（准确时刻未知）')
        balance = data.get('account_balance')
        text = ([f'总剩余 {balance["remaining"]:,.2f} 积分 · 手动更新，非实时',
                 ' · '.join(f'{r["name"]} {r["remaining"]:,.2f}' for r in balance['categories']),
                 '套餐基础积分到期时间未知；以下仅显示奖励批次。'] if balance else [])
        text.extend([f'奖励批次快照 · 非实时 · 采集 {captured}',
                     f'奖励快照剩余 {data["total_remaining"]:,.2f} · 尚未到期部分 {view["unexpired_snapshot"]:,.2f}'])
        if live_snapshot:
            text = [f'线上批次明细 · 读取 {stamp(self.wb_live.get("batches_at",self.wb_live["at"]))}' +
                    (' · 上次成功批次' if self.wb_live.get('detail_error') else '')]
        row = view['next']
        if row:
            seconds = max(0, int(wb_timestamp(row['expires_at'])-now))
            text.extend([f'最近到期：{stamp(wb_timestamp(row["expires_at"]))} · 当时剩 {row["remaining"]:g}',
                         f'距到期 {seconds//86400} 天 {seconds%86400//3600:02}:{seconds%3600//60:02}:{seconds%60:02}'])
        else:
            text.append('所列正余额批次均已过预计到期时间；当前余额未知。')
        if view['past_due_snapshot']:
            text.append(f'已过期时刻的快照余额 {view["past_due_snapshot"]:g}；不记为已核实消费或失效。')
        text.append('每 10 分钟更新；顶部刷新或“查询线上余额”可立即查询。' if live_snapshot else
                    '此处为旧快照；余额与批次未扣后续消费。')
        text.append(f'逐批明细共 {len(data["batches"])} 笔，可在下表内滚轮查看。')
        if self.wb_error:
            text.append(self.wb_error)
        if self.wb_export_error:
            text.append(self.wb_export_error)
        self.wb_summary.config(text='\n'.join(text))
        # Rebuild only on an import or expiration-state transition, preserving scroll.
        signature = (str(self.wb_live['at']) if live_snapshot else self.wb_store.entries[-1]['hash'], tuple(wb_timestamp(r['expires_at']) <= now for r in data['batches']))
        if signature != getattr(self, 'wb_table_signature', None):
            self.wb_table_signature = signature
            self.wb_table.delete(*self.wb_table.get_children())
            for r in sorted(data['batches'], key=lambda r: wb_timestamp(r['expires_at'])):
                status = '已用完' if not r['remaining'] else '到期待核对' if wb_timestamp(r['expires_at']) <= now else '待核对'
                if live_snapshot and r['remaining'] and wb_timestamp(r['expires_at'])>now:
                    status = '有效'
                self.wb_table.insert('', 'end', values=(stamp(wb_timestamp(r['expires_at']), '%Y-%m-%d %H:%M:%S'), f'{r["remaining"]:g}', status))
            lines = ['导入与变化记录（导入顺序；无精确采集时刻时不推算时段用量）：']
            for entry in (self.wb_store.entries[-4:] if self.wb_store else []):
                lines.append(stamp(entry['imported_at']) + ' 导入 · 采集 ' + entry['snapshot']['captured_date'])
                lines.extend(entry['changes'][:6])
                if len(entry['changes']) > 6:
                    lines.append('更多逐批变化保留在本地历史文件。')
            lines.append('倍率截图仅供参考，不用于推算费用；未假设先到期先扣。')
            if live_snapshot:
                lines = ['最近线上读数（余额变化不统一视为消费）：']
                samples = self.wb_live_history.samples if self.wb_live_history else []
                for sample in samples[-4:]:
                    lines.append(f'{stamp(sample["at"])} · 剩余 {sample["remaining"]:,.2f}')
                lines.append('完整批次与变化记录已写入库内实时积分记录；原手动快照保留。')
            self.wb_history_detail.config(text='\n'.join(lines))
            reference = (self.wb_store.latest if self.wb_store else None) or data
            rates = reference.get('model_display_rates', [])
            self.wb_models_detail.config(text='采集 ' + reference['captured_date'] + ' 的菜单截图，非当前计费公式。\n' +
                '\n'.join(f'{r["name"]}：{r["display_multiplier"]:g}×  {r.get("label", "")}' for r in rates))
    def apply(self, name, data, error):
        self.busy.discard(name)
        if error:
            self.errors[name] = error
            if name == 'quota' and self.history:
                try:
                    self.history.record_error(error)
                except Exception as failure:
                    self.history_error = type(failure).__name__
        else:
            self.errors.pop(name, None)
            if name in ('workbuddy-live', 'workbuddy-auth'):
                if not data.get('snapshot') and self.wb_live and self.wb_live.get('snapshot') and data.get('account_id') == self.wb_live.get('account_id'):
                    data = dict(data, snapshot=self.wb_live['snapshot'],
                                batches_at=self.wb_live.get('batches_at',self.wb_live['at']))
                self.wb_live = data
                self.errors.pop('workbuddy-auth', None)
                self.errors.pop('workbuddy-live', None)
                self.last_start['workbuddy-live'] = time.monotonic()
                try:
                    if self.wb_live_history is None:
                        raise ApiError('history-unavailable')
                    self.wb_live_history.record(data)
                    target = self.wb_config.get('live_markdown_path')
                    if target:
                        self.wb_live_history.export_markdown(target)
                    self.wb_live_export_error = None
                except Exception:
                    self.wb_live_export_error = '线上余额已读取，但本地历史/Markdown 写入失败。'
            if name == 'quota':
                self.check_reset_watch(data)
                self.quota = data
                self.state['quota'] = data
                if self.history:
                    try:
                        self.history.record(data, getattr(self, 'read_reason', 'auto'))
                        self.history_rows = self.history.recent()
                        weekly = next((w for w in data['windows'] if w['windowDurationMins'] == 10080), None)
                        self.history_base = self.history.baseline(weekly.get('resetsAt') if weekly else None)
                        self.history_error = None
                        self.export_markdown()
                    except Exception as failure:
                        self.history_error = ('数据库不可写，请关闭占用数据库文件的应用'
                                              if getattr(failure,'sqlite_errorcode',None)==8 else type(failure).__name__)
                for message in quota_alerts(data['windows'], self.state):
                    self.alert(message)
                self.state['pending_windows'] = [w for w in data['windows'] if w.get('resetsAt') and w['windowDurationMins'] in (300, 10080) and not five_idle(w)]
            if name == 'tibo':
                latest = data['latest']
                # First fetch establishes a baseline; old announcements do not ring.
                if tibo_changed(latest, self.state):
                    self.alert('Tibo 新重置公告：\n'+latest.get('display_text', latest.get('text', ''))[:500])
                notice = tibo_notice(data, time.time())
                signature = notice_signature(notice)
                previous = self.state.get('tibo_notice_seen')
                if previous is None:
                    previous = notice_signature(tibo_notice(self.tibo, time.time()))
                if (self.tibo.get('at') or 'tibo_notice_seen' in self.state) and signature != previous and notice and notice['active']:
                    label = '已安排重置' if notice['kind'] == 'scheduled' else '预告观察'
                    self.alert(f"Tibo {label}：{stamp(notice['deadline'])} 前（上限，可能更早）\n"+
                               notice.get('display_text', notice.get('text', ''))[:400])
                self.state['tibo_notice_seen'] = signature
                self.tibo = {'latest': latest, 'at': data['at'], 'watch': data.get('watch'), 'scheduled': data.get('scheduled')}
                self.state['tibo'] = self.tibo
        self.persist()
        save(STATUS, {'checked_at': time.time(), 'quota_at': self.quota.get('at'),
                      'reset_watch_active': self.reset_watch,
                      'tibo_at': self.tibo.get('at'), 'errors': self.errors,
                      'markdown_error': self.markdown_error, 'markdown_at': self.markdown_at,
                      'history_error': self.history_error,
                      'dock_compact': self.dock_compact,
                      'widget_size': [self.root.winfo_width(), self.root.winfo_height()],
                      'native_topmost': self.topmost_ok,
                      'pid': os.getpid(), 'running': True})

    def display(self):
        windows = self.quota.get('windows', [])
        five = next((w for w in windows if w.get('windowDurationMins') == 300), None)
        week = next((w for w in windows if w.get('windowDurationMins') == 10080), None)
        now = time.time()
        stale = now-self.quota.get('at', 0) > max(1200, self.quota_refresh_seconds*2)
        mini_text = compact_summary(five, now, stale)
        if mini_text != self.mini_label.cget('text'):
            self.mini_label.config(text=mini_text, fg=MUTED if stale else GREEN)
            if self.dock_compact:
                self.dock_layout()
        failed = 'quota' in self.errors
        self.refresh_button.config(text='刷新中…' if 'quota' in self.busy else '刷新',
                                   state='disabled' if 'quota' in self.busy else 'normal')
        self.refresh_interval_menu.config(fg=GREEN if self.reset_watch else FG)
        if five:
            short = five_summary(five, now)
            if week:
                short += f"   周剩 {max(0,100-week['usedPercent']):g}%"
            self.main.config(text=short + ('  · 旧数据' if stale else '  · 刷新失败' if failed else ''), fg=MUTED if stale else FG)
        else:
            self.main.config(text='额度暂不可用 · 展开查看' if self.errors.get('quota') else '正在读取额度…')
        detail = []
        for w in windows:
            label = '5 小时' if w['windowDurationMins'] == 300 else '每周' if w['windowDurationMins'] == 10080 else f"{w['windowDurationMins']} 分钟"
            detail.append(f'{label}：剩余 100% · 尚未开始使用' if five_idle(w) else
                          f"{label}：剩余 {max(0,100-w['usedPercent']):g}% · 重置 {stamp(w.get('resetsAt'))}")
        detail.append(f"可用重置卡：{self.quota.get('credits', '未知')}（仅显示）")
        self.quota_detail.config(text='\n'.join(detail))
        latest = self.tibo.get('latest') or {}
        t = latest.get('announced_at')
        date = stamp(iso_time(t), '%m-%d %H:%M') if t else '尚未获取'
        notice = tibo_notice(self.tibo, now)
        deadline = notice['deadline'] if notice and notice['active'] else None
        report = [f"最近已发布：{date} · {'普通重置' if latest.get('reset_type') == 'regular' else '重置卡/其他'}",
                  latest.get('display_text', latest.get('text', '尚无正文'))]
        if notice:
            label = '已安排重置' if notice['kind'] == 'scheduled' else '预告观察'
            report.extend(['', label + (' · 等待实际重置确认' if notice['active'] else ' · 预计时间已过，等待新消息'),
                           f"站点解析上限：{stamp(notice['deadline'])} 前，可能更早",
                           'Tibo 原文：'+notice.get('display_text', notice.get('text', '无原文')),
                           '时间来自第三方站点解析；实际额度以账号读数为准。'])
        else:
            report.extend(['', '暂未获取到有效的预告观察。'])
        if self.errors.get('tibo'):
            report.append('公告检查失败，以上为上次缓存。')
        self.tibo_detail.config(text='\n'.join(report))
        self.draw_forecast(forecast(windows, now, deadline), now, deadline, stale, notice)
        self.draw_stats(now)
        self.wb_display(now)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
                winreg.QueryValueEx(key, 'CodexQuotaWidgetPortable')
            auto = '开'
        except FileNotFoundError:
            auto = '关'
        read_state = '正在刷新额度…' if 'quota' in self.busy else ('额度读取失败；可点击顶部刷新，或等下次自动更新。' if failed else f'额度{self.refresh_interval_label()}更新；公告与 WorkBuddy 每 10 分钟；顶部可手动刷新。')
        if self.reset_watch:
            read_state += (f' · 等待重置：每10秒检测，变化后响铃并恢复{next(k for k,v in QUOTA_REFRESH_INTERVALS.items() if v==self.normal_refresh_seconds)}。'
                           if self.quota_refresh_seconds == 10 else ' · 检测到重置变化时响铃，继续按所选间隔更新。')
        if self.errors.get('quota') == 'login':
            read_state = '登录状态失效，请检查 Codex 当前账号登录后再刷新。'
        if self.markdown_error:
            read_state += ' · 库内 Markdown 导出失败：'+self.markdown_error
        self.footer.config(text=f"额度更新 {stamp(self.quota.get('at'))} · 公告检查 {stamp(self.tibo.get('at'))}\n{read_state}\n剩余 20% / 10% / 0% 时提醒 · 声音{'关闭' if self.state.get('muted') else '开启'} · 登录自启{auto}\n拖动正文或顶部额度栏移动 · 点击分区标题收起详情")
        if self.dock_enabled and not self.dock_compact:
            self.root.after_idle(self.dock_layout)

    def draw_forecast(self, f, now, deadline, stale, notice=None):
        c = self.timeline
        c.delete('all')
        if not f:
            self.forecast_detail.config(text='等待有效的 5 小时与周额度数据后计算。')
            return
        messages = ['绿虚线：按当前窗口剩余重算；蓝线：实际读数。',
                    '假设各窗口用满，末张卡片为最早可用完区间。',
                    '具体用完时间取决于使用速度。']
        if f.get('idle'):
            messages = ['5h 尚未开始使用；每 10 分钟检测一次，也可手动刷新。',
                        '检测到消耗后，按接口时间显示倒计时和预测台阶。']
        if f['week_reset'] and f['earliest'] >= f['week_reset']:
            messages.append('注意：按此节奏，在系统周重置前无法用完当前周额度。')
        if stale:
            messages.append('额度数据已过时，以下仅为缓存推算。')
        self.forecast_detail.config(text='\n'.join(messages))
        width = max(600, c.winfo_width())
        graph_left, graph_right, card_left = 95, 214, 246
        layout=timeline_layout(f,now,deadline)
        start,end=layout['start'],layout['end']
        plot_top,plot_bottom=layout['top'],layout['bottom']
        to_y=layout['y']
        height=layout['height']
        # Both the stairs and the cards start from the latest actual reading.
        # A newly observed window therefore replaces the previous projection.
        theory = f
        actual = [r for r in self.history_rows if r.get('week_remaining') is not None and
                  start <= r['at'] <= now and r.get('week_reset') and f.get('week_reset') and
                  abs(r['week_reset']-f['week_reset']) <= 90]
        to_x = lambda remaining: graph_left+(graph_right-graph_left)*(100-max(0,min(100,remaining)))/100
        c.create_text(8, 12, text='时间 ↓  周剩 →', anchor='w', fill=MUTED, font=('Microsoft YaHei UI', 9))
        c.create_text(graph_left, 35, text='100%', anchor='w', fill=MUTED, font=('Microsoft YaHei UI', 8))
        c.create_text(graph_right, 35, text='0%', anchor='e', fill=MUTED, font=('Microsoft YaHei UI', 8))
        c.create_line(graph_right, plot_top, graph_right, plot_bottom, fill='#515a6e')
        times=sorted(set([start,layout['current_start']]+[slot['end'] for slot in f['slots']]))
        label_positions=[to_y(now)]
        for when in times:
            if when<start or when>end:
                continue
            yy = to_y(when)
            c.create_line(graph_left, yy, width-6, yy, fill='#343b49', dash=(2,4),tags='time-boundary')
            label=stamp(when,'%m-%d\n%H:%M')
            if all(abs(yy-used_y)>=44 for used_y in label_positions):
                c.create_text(graph_left-10, yy, text=label, anchor='e', fill=MUTED,
                              font=('Microsoft YaHei UI', 8))
                label_positions.append(yy)
        # Historical actual samples stay visible; the projection begins now.
        curve=[]
        for when,remaining in visible_theory_points(theory,start,end):
            curve.extend([to_x(remaining),to_y(when)])
        if len(curve)>=4 and not f.get('idle'):
            c.create_line(*curve,fill=GREEN,width=2,dash=(5,3),tags='theory')
        for a,b in zip(actual,actual[1:]):
            if b['at']-a['at']<=900 and quota_delta(a,b,'week') is not None:
                c.create_line(to_x(a['week_remaining']),to_y(a['at']),to_x(b['week_remaining']),to_y(b['at']),
                              fill='#80c6ed',width=3,tags='actual')
        for row in actual:
            x,y=to_x(row['week_remaining']),to_y(row['at'])
            c.create_oval(x-3,y-3,x+3,y+3,fill='#80c6ed',outline='',tags='actual')
        now_y=to_y(now)
        c.create_line(graph_left,now_y,card_left-8,now_y,fill='#80c6ed',dash=(3,2),tags='now-line')
        c.create_text(graph_left-10,now_y,text='现在\n'+stamp(now,'%H:%M'),anchor='e',fill='#80c6ed',font=('Microsoft YaHei UI',8,'bold'))
        history_bottom=min(to_y(layout['current_start']),now_y)
        if history_bottom-plot_top>=18:
            c.create_rectangle(card_left,plot_top+3,width-6,history_bottom-3,fill='#1d2027',outline='#343b49',tags='history-card')
            c.create_text(card_left+14,plot_top+19,text='历史 · 最近 5 小时内',anchor='w',fill=MUTED,font=('Microsoft YaHei UI',9,'bold'))
            if history_bottom-plot_top>=74:
                c.create_text(card_left+14,plot_top+53,text=f"{stamp(start,'%H:%M')} → {stamp(layout['current_start'],'%H:%M')}",
                              anchor='w',fill=MUTED,font=('Microsoft YaHei UI',9))
        for index, slot in enumerate(layout['slots']):
            top,bottom=to_y(slot['start']),to_y(slot['end'])
            last = index == len(f['slots'])-1
            inside = deadline and slot['start'] <= deadline < slot['end']
            color = '#f5be6c' if inside else GREEN if last else '#80c6ed'
            c.create_rectangle(card_left,top+3,width-6,bottom-3,fill=PANEL,outline=color if last or inside else '#343b49',tags=f'window-card-{index}')
            title = '当前窗口' if index == 0 else f'后续第 {index} 个窗口'
            if last:
                title += ' · 可用完'
            c.create_text(card_left+14, top+19, text=title, anchor='w', fill=color, font=('Microsoft YaHei UI', 9, 'bold'))
            begin = stamp(slot['start'], '%H:%M')
            cross_day = stamp(slot['start'], '%m-%d') != stamp(slot['end'], '%m-%d')
            end_label = ('次日 ' if cross_day else '')+stamp(slot['end'], '%H:%M')
            c.create_text(card_left+14, top+54, text=f"{begin} → {end_label}",
                          anchor='w', fill=FG, font=('Microsoft YaHei UI', 9))
            c.create_text(width-20, top+19, text='用满后周剩', anchor='e', fill=MUTED, font=('Microsoft YaHei UI', 8))
            c.create_text(width-20, top+54, text=f"{slot['after']:.1f}%", anchor='e', fill=color,
                          font=('Microsoft YaHei UI', 15, 'bold'))
        if deadline:
            yy=to_y(deadline)
            c.create_line(graph_left,yy,width-6,yy,fill='#f5be6c',dash=(4,3),width=2,tags='reset-deadline')
            c.create_oval(graph_right-4,yy-4,graph_right+4,yy+4,fill='#f5be6c',outline='')
            label = '公告上限' if notice and notice['kind'] == 'scheduled' else '预告上限'
            capacity_text = '5h 尚未开始，暂不计算截止前的用量窗口。' if f.get('idle') else (
                f"截止前可用当前窗口 + {max(0, math.ceil((deadline-f['next_reset'])/18000))} 个新窗口；理论最低周剩 {f['at_deadline']:.1f}%。")
            c.create_text(card_left,plot_bottom+14,
                          text=f"{label} {stamp(deadline, '%m-%d %H:%M')} · 可能更早\n{capacity_text}",
                          anchor='nw', width=width-card_left-12, fill='#f5be6c', font=('Microsoft YaHei UI', 9))
        c.configure(height=height)

    def stats_hover(self, event):
        for left,right,part in getattr(self,'stats_hitboxes',[]):
            if left<=event.x<=right:
                value = '重置或读数修正，不能可靠计算' if part['week'] is None else f"周额度消耗 {part['week']:g} 个百分点"
                self.stats_hover_text.config(text=f"{stamp(part['start'])} → {stamp(part['end'])}\n实际间隔 {part['seconds']/60:.1f} 分钟 · {value}"+
                                             (' · 长间隔，未补点' if part['gap'] else ''))
                return

    def draw_stats(self, now):
        c=self.stats_canvas
        c.delete('all')
        self.stats_hitboxes=[]
        rows=[r for r in self.history_rows if r['at']>=now-86400]
        parts=intervals(rows)
        if self.history_error:
            self.stats_detail.config(text='统计文件读写失败：'+self.history_error+'。额度显示仍可使用。')
            return
        if not parts:
            self.stats_detail.config(text=f"已记录 {len(rows)} 个真实读数。至少两次成功读取后开始显示消耗。\n每 10 分钟自动采样；手动刷新也记录。不补造过去数据。")
            c.create_text(12,35,anchor='nw',text='等待下一次成功采样…',fill=MUTED,font=('Microsoft YaHei UI',10))
            return
        total=sum(p['week'] for p in parts if p['week'] is not None)
        skipped=sum(p['week'] is None for p in parts)
        self.stats_detail.config(text=f"最近 24 小时已观测周额度消耗：{total:g} 个百分点\n共 {len(rows)} 个读数；{skipped} 个重置/修正区间未计入。\n下图：最近 24 个采样区间；通常 10 分钟，手动刷新间隔更短。\n蓝柱为正常间隔，橙柱为超过 15 分钟；↻ 为重置/修正。")
        selected=parts[-24:]
        width=max(550,c.winfo_width())
        left,right,top,bottom=62,width-12,24,116
        peak=max(1,max((p['week'] or 0 for p in selected),default=1))
        c.create_text(left-8,top,text=f'{peak:g}%',anchor='e',fill=MUTED,font=('Microsoft YaHei UI',8))
        c.create_text(left-8,bottom,text='0%',anchor='e',fill=MUTED,font=('Microsoft YaHei UI',8))
        c.create_line(left,top,left,bottom,right,bottom,fill='#515a6e')
        cell=(right-left)/len(selected)
        for index,part in enumerate(selected):
            x=left+index*cell
            self.stats_hitboxes.append((x,x+cell,part))
            if part['week'] is None:
                c.create_text(x+cell/2,bottom-10,text='↻',fill='#f5be6c',font=('Microsoft YaHei UI',9),tags='usage-bars')
            else:
                h=max(2,(bottom-top)*part['week']/peak)
                c.create_rectangle(x+2,bottom-h,x+cell-2,bottom,fill='#f5be6c' if part['gap'] else '#80c6ed',
                                   outline='',tags='usage-bars')
            if index%max(1,len(selected)//5)==0 or index==len(selected)-1:
                c.create_text(x+cell/2,bottom+19+(index%2)*14,text=stamp(part['end'],'%H:%M'),fill=MUTED,font=('Microsoft YaHei UI',8))
        c.configure(height=160)

    def tick(self):
        if self.closed.is_set():
            return
        if time.monotonic()-self.last_topmost >= 2:
            self.ensure_topmost()
        for msg in due_alerts(time.time(), self.state):
            self.alert(msg)
            self.persist()
            self.last_start['quota'] = 0
        while not self.q.empty():
            self.apply(*self.q.get())
        for name, interval, fn in [('quota', self.quota_refresh_seconds, read_quota), ('tibo', 600, read_tibo)]:
            if time.monotonic()-self.last_start[name] >= interval:
                self.start(name, fn, reason='reset-watch' if name=='quota' and self.reset_watch else 'auto')
        if time.monotonic()-self.wb_checked >= 600:
            self.wb_reload()
        if self.wb_api.configured and time.monotonic()-self.last_start.get('workbuddy-live',0) >= 600:
            self.wb_fetch_api()
        self.display()
        self.root.after(1000, self.tick)

    def smoke(self):
        self.toggle()
        self.root.update_idletasks()
        print(json.dumps({'width': self.root.winfo_width(), 'height': self.root.winfo_height(),
                          'quota_label': self.main.cget('text'), 'details_visible': self.details.winfo_ismapped()}, ensure_ascii=False))
        self.close()

    def close(self):
        self.closed.set()
        self.stop_sound()
        with ACTIVE_LOCK:
            for p in ACTIVE:
                if p.poll() is None:
                    p.terminate()
        self.state['position'] = f'+{max(0,self.root.winfo_x())}+{max(0,self.root.winfo_y())}'
        self.persist()
        save(STATUS, {'running': False, 'pid': os.getpid(), 'checked_at': time.time()})
        self.root.destroy()


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    # Do not allow duplicate windows or duplicate sounds.
    ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\CodexQuotaWidgetPortable')
    if ctypes.windll.kernel32.GetLastError() == 183:
        return
    try:
        Widget('--smoke' in sys.argv).root.mainloop()
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


if __name__ == '__main__':
    main()
