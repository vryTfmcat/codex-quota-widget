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
import urllib.request
import webbrowser
import winsound
import winreg
import math
import io
import wave
from array import array
from usage_history import UsageHistory, intervals, quota_delta

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


def forecast(windows, now, deadline=None):
    five = next((w for w in windows if w.get('windowDurationMins') == 300), None)
    week = next((w for w in windows if w.get('windowDurationMins') == 10080), None)
    if not five or not week or not five.get('resetsAt') or five['resetsAt'] <= now:
        return None
    remaining = max(0, 100-week['usedPercent'])
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
        count = max(0, math.floor((deadline-five['resetsAt'])/18000)+1)
        by_deadline = max(0, remaining-current-WEEKLY_PER_WINDOW*count)
    return {'remaining': remaining, 'current_capacity': current, 'earliest': earliest,
            'finish_by': slots[-1]['end'], 'slots': slots,
            'next_reset': five['resetsAt'], 'week_reset': week.get('resetsAt'),
            'points': points, 'at_deadline': by_deadline}


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
        reset = w.get('resetsAt')
        key = window_key(w)
        if reset and now >= reset and key not in fired:
            label = '5小时' if w['windowDurationMins'] == 300 else '周'
            alerts.append(f'{label}额度预定重置时间已到（{stamp(reset)}）。正在核对最新额度。')
            fired.append(key)
    state['reset_fired'] = fired[-20:]
    return alerts


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
        self.last_start = {'quota': 0, 'tibo': 0}
        self.sound_stop = threading.Event()
        self.root.geometry(self.state.get('position', '+80+65'))
        self.build()
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
        heading = tk.Button(card, text=('▾ ' if opened else '▸ ')+text,
                            command=toggle_section, bg=BG, fg=GREEN, activebackground=PANEL,
                            activeforeground=GREEN, anchor='w', relief='flat', bd=0,
                            cursor='hand2', font=('Microsoft YaHei UI', 14, 'bold'))
        heading.pack(fill='x', pady=(0, 7))
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
        bar = tk.Frame(self.root, bg=BG, padx=12, pady=8)
        bar.pack(fill='x')
        drag = self.label(bar, '⠿ CODEX', 10, GREEN)
        drag.pack(side='left', padx=(0, 12))
        self.main = self.label(bar, '读取额度…', 11)
        self.main.pack(side='left')
        self.refresh_button = self.button(bar, '刷新', self.refresh)
        self.refresh_button.pack(side='left', padx=(12, 0))
        self.chevron = self.label(bar, '▾', 11, MUTED)
        self.chevron.pack(side='left', padx=(10, 0))
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
        distance = max(abs(e.x_root-self.header_origin[0]), abs(e.y_root-self.header_origin[1]))
        if self.header_moved or distance > 5:
            self.drag_end(e)
        else:
            self.toggle()

    def drag(self, e):
        self.root.geometry(f'+{max(0,e.x_root-self.origin[0])}+{max(0,e.y_root-self.origin[1])}')

    def drag_end(self, e):
        self.state['position'] = f'+{max(0,self.root.winfo_x())}+{max(0,self.root.winfo_y())}'
        self.persist()

    def toggle(self):
        self.expanded = not self.expanded
        self.chevron.config(text='▴' if self.expanded else '▾')
        if self.expanded:
            self.details_host.pack(fill='both', expand=True)
        else:
            self.details_host.pack_forget()

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
        watch = self.tibo.get('watch') or {}
        url = watch.get('tweet_url') or (self.tibo.get('latest') or {}).get('tweet_url')
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
                reason = str(e) if isinstance(e, QuotaReadError) else type(e).__name__
                self.q.put((name, None, reason))
        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        self.start('quota', read_quota, reason='manual')
        self.start('tibo', read_tibo)

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
            if name == 'quota':
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
                self.state['pending_windows'] = [w for w in data['windows'] if w.get('resetsAt') and w['windowDurationMins'] in (300, 10080)]
            if name == 'tibo':
                latest = data['latest']
                # First fetch establishes a baseline; old announcements do not ring.
                if tibo_changed(latest, self.state):
                    self.alert('Tibo 新重置公告：\n'+latest.get('display_text', latest.get('text', ''))[:500])
                watch = data.get('watch') or {}
                signature = '|'.join(str(watch.get(k) or '') for k in ('tweet_id', 'expires_at', 'level'))
                previous_watch = self.state.get('watch_seen')
                if previous_watch and signature != previous_watch and watch.get('tweet_id'):
                    self.alert('Tibo 有新的重置预告线索（尚未确认）：\n'+watch.get('display_text', watch.get('text', ''))[:400])
                self.state['watch_seen'] = signature
                self.tibo = {'latest': latest, 'at': data['at'], 'watch': data.get('watch'), 'scheduled': data.get('scheduled')}
                self.state['tibo'] = self.tibo
        self.persist()
        save(STATUS, {'checked_at': time.time(), 'quota_at': self.quota.get('at'),
                      'tibo_at': self.tibo.get('at'), 'errors': self.errors,
                      'markdown_error': self.markdown_error, 'markdown_at': self.markdown_at,
                      'history_error': self.history_error,
                      'pid': os.getpid(), 'running': True})

    def display(self):
        windows = self.quota.get('windows', [])
        five = next((w for w in windows if w.get('windowDurationMins') == 300), None)
        week = next((w for w in windows if w.get('windowDurationMins') == 10080), None)
        now = time.time()
        stale = now-self.quota.get('at', 0) > 1200
        failed = 'quota' in self.errors
        self.refresh_button.config(text='刷新中…' if 'quota' in self.busy else '刷新',
                                   state='disabled' if 'quota' in self.busy else 'normal')
        if five:
            left = max(0, 100-five['usedPercent'])
            sec = max(0, int((five.get('resetsAt') or now)-now))
            countdown = f'{sec//3600:02}:{sec%3600//60:02}:{sec%60:02}'
            short = f'5h 剩 {left:g}%   {countdown}' if sec else f'5h 上次剩 {left:g}%   待核对重置'
            if week:
                short += f"   周剩 {max(0,100-week['usedPercent']):g}%"
            self.main.config(text=short + ('  · 旧数据' if stale else '  · 刷新失败' if failed else ''), fg=MUTED if stale else FG)
        else:
            self.main.config(text='额度暂不可用 · 展开查看' if self.errors.get('quota') else '正在读取额度…')
        detail = []
        for w in windows:
            label = '5 小时' if w['windowDurationMins'] == 300 else '每周' if w['windowDurationMins'] == 10080 else f"{w['windowDurationMins']} 分钟"
            detail.append(f"{label}：剩余 {max(0,100-w['usedPercent']):g}% · 重置 {stamp(w.get('resetsAt'))}")
        detail.append(f"可用重置卡：{self.quota.get('credits', '未知')}（仅显示）")
        self.quota_detail.config(text='\n'.join(detail))
        latest = self.tibo.get('latest') or {}
        t = latest.get('announced_at')
        date = stamp(dt.datetime.fromisoformat(t.replace('Z', '+00:00')).timestamp(), '%m-%d %H:%M') if t else '尚未获取'
        watch = self.tibo.get('watch') or {}
        deadline = iso_time(watch.get('expires_at'))
        watch_active = bool(deadline and deadline > now)
        report = [f"最近已发布：{date} · {'普通重置' if latest.get('reset_type') == 'regular' else '重置卡/其他'}",
                  latest.get('display_text', latest.get('text', '尚无正文'))]
        if watch:
            report.extend(['', '预告观察 · ' + ('有迹象，尚未确认' if watch_active else '观察窗口已过期，等待新消息'),
                           f"站点预测：{stamp(deadline)} 前可能重置",
                           'Tibo 原文：'+watch.get('display_text', watch.get('text', '无原文'))])
            report.append('预测来自第三方对推文的解读，不是确定的重置安排。')
        else:
            report.extend(['', '暂未获取到有效的预告观察。'])
        if self.errors.get('tibo'):
            report.append('公告检查失败，以上为上次缓存。')
        self.tibo_detail.config(text='\n'.join(report))
        self.draw_forecast(forecast(windows, now, deadline if watch_active else None), now,
                           deadline if watch_active else None, stale)
        self.draw_stats(now)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
                winreg.QueryValueEx(key, 'CodexQuotaWidgetPortable')
            auto = '开'
        except FileNotFoundError:
            auto = '关'
        read_state = '正在刷新额度…' if 'quota' in self.busy else ('额度读取失败；可点击顶部刷新，或等下次自动更新。' if failed else '额度与公告每 10 分钟自动更新；顶部可手动刷新。')
        if self.errors.get('quota') == 'login':
            read_state = '登录状态失效，请检查 Codex 当前账号登录后再刷新。'
        if self.markdown_error:
            read_state += ' · 库内 Markdown 导出失败：'+self.markdown_error
        self.footer.config(text=f"额度更新 {stamp(self.quota.get('at'))} · 公告检查 {stamp(self.tibo.get('at'))}\n{read_state}\n剩余 20% / 10% / 0% 时提醒 · 声音{'关闭' if self.state.get('muted') else '开启'} · 登录自启{auto}\n拖动正文或顶部额度栏移动 · 点击分区标题收起详情")

    def draw_forecast(self, f, now, deadline, stale):
        c = self.timeline
        c.delete('all')
        if not f:
            self.forecast_detail.config(text='等待有效的 5 小时与周额度数据后计算。')
            return
        messages = [f'绿虚线：{WEEKLY_PER_WINDOW:g}%/5h 推算；蓝线：实际读数。',
                    '假设各窗口用满，末张卡片为最早可用完区间。',
                    '具体用完时间取决于使用速度。']
        if f['week_reset'] and f['earliest'] >= f['week_reset']:
            messages.append('注意：按此节奏，在系统周重置前无法用完当前周额度。')
        if stale:
            messages.append('额度数据已过时，以下仅为缓存推算。')
        self.forecast_detail.config(text='\n'.join(messages))
        width = max(600, c.winfo_width())
        row_height = 104
        graph_left, graph_right, card_left = 95, 214, 246
        height = len(f['slots'])*row_height+12
        if deadline:
            height += 78
        plot_top, plot_bottom = 53, height-18
        base = self.history_base
        theory = forecast(base['windows'], base['at']) if base else None
        theory = theory or f
        actual = [r for r in self.history_rows if r.get('week_remaining') is not None and
                  r['at'] >= now-21600 and r.get('week_reset') and f.get('week_reset') and
                  abs(r['week_reset']-f['week_reset']) <= 90]
        start = min([now]+[r['at'] for r in actual])
        end = max(f['finish_by'], theory['finish_by'], deadline or 0, now+60)
        to_y = lambda when: plot_top+(plot_bottom-plot_top)*(when-start)/(end-start)
        to_x = lambda remaining: graph_left+(graph_right-graph_left)*(100-max(0,min(100,remaining)))/100
        c.create_text(8, 12, text='时间 ↓  周剩 →', anchor='w', fill=MUTED, font=('Microsoft YaHei UI', 9))
        c.create_text(graph_left, 35, text='100%', anchor='w', fill=MUTED, font=('Microsoft YaHei UI', 8))
        c.create_text(graph_right, 35, text='0%', anchor='e', fill=MUTED, font=('Microsoft YaHei UI', 8))
        c.create_line(graph_right, plot_top, graph_right, plot_bottom, fill='#515a6e')
        times = sorted(set([now]+[r['at'] for r in actual[::max(1,len(actual)//3)]]+[slot['end'] for slot in f['slots']]))
        last_label_y = -100
        for index, when in enumerate(times):
            yy = to_y(when)
            c.create_line(graph_left, yy, graph_right, yy, fill='#343b49', dash=(2, 4))
            label = '现在' if when == now else stamp(when, '%m-%d\n%H:%M')
            if yy-last_label_y >= 38:
                c.create_text(graph_left-10, yy, text=label, anchor='e', fill=MUTED,
                              font=('Microsoft YaHei UI', 8))
                last_label_y = yy
        # Clip the fixed baseline to the visible range without rebasing it on each refresh.
        previous = theory['remaining']
        for when, remaining in theory['points']:
            if when <= start:
                previous = remaining
        curve=[to_x(previous),to_y(start)]
        for when, remaining in theory['points']:
            if when < start:
                continue
            curve.extend([to_x(remaining),to_y(when)])
        if len(curve)>=4:
            c.create_line(*curve,fill=GREEN,width=2,dash=(5,3),tags='theory')
        if end > theory['finish_by']:
            c.create_line(graph_right,max(plot_top,to_y(theory['finish_by'])),graph_right,plot_bottom,
                          fill=GREEN,width=2,dash=(5,3),tags='theory')
        for a,b in zip(actual,actual[1:]):
            if b['at']-a['at']<=900 and quota_delta(a,b,'week') is not None:
                c.create_line(to_x(a['week_remaining']),to_y(a['at']),to_x(b['week_remaining']),to_y(b['at']),
                              fill='#80c6ed',width=3,tags='actual')
        for row in actual:
            x,y=to_x(row['week_remaining']),to_y(row['at'])
            c.create_oval(x-3,y-3,x+3,y+3,fill='#80c6ed',outline='',tags='actual')
        c.create_line(graph_left,to_y(now),graph_right,to_y(now),fill='#a4a9b8',dash=(2,2))
        for index, slot in enumerate(f['slots']):
            top = index*row_height+6
            last = index == len(f['slots'])-1
            inside = deadline and slot['start'] <= deadline < slot['end']
            color = '#f5be6c' if inside else GREEN if last else '#80c6ed'
            c.create_rectangle(card_left, top, width-6, top+92, fill=PANEL, outline=color if last or inside else '#343b49')
            title = '当前窗口' if index == 0 else f'后续第 {index} 个窗口'
            if last:
                title += ' · 可用完'
            c.create_text(card_left+14, top+19, text=title, anchor='w', fill=color, font=('Microsoft YaHei UI', 9, 'bold'))
            begin = '现在' if index == 0 else stamp(slot['start'], '%H:%M')
            cross_day = stamp(slot['start'], '%m-%d') != stamp(slot['end'], '%m-%d')
            end_label = ('次日 ' if cross_day else '')+stamp(slot['end'], '%H:%M')
            c.create_text(card_left+14, top+54, text=f"{begin} → {end_label}",
                          anchor='w', fill=FG, font=('Microsoft YaHei UI', 9))
            c.create_text(width-20, top+19, text='用满后周剩', anchor='e', fill=MUTED, font=('Microsoft YaHei UI', 8))
            c.create_text(width-20, top+54, text=f"{slot['after']:.1f}%", anchor='e', fill=color,
                          font=('Microsoft YaHei UI', 15, 'bold'))
        if deadline:
            relation = '在以上窗口之后' if deadline >= f['finish_by'] else '落在上面橙色标记的窗口内'
            yy=to_y(deadline)
            c.create_line(graph_left,yy,graph_right,yy,fill='#f5be6c',dash=(4,3),width=2)
            c.create_oval(graph_right-4,yy-4,graph_right+4,yy+4,fill='#f5be6c',outline='')
            c.create_text(card_left, len(f['slots'])*row_height+20,
                          text=f"预告 {stamp(deadline, '%m-%d %H:%M')} 前 · 未确认\n{relation}；可能更早重置。",
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
        for msg in due_alerts(time.time(), self.state):
            self.alert(msg)
            self.persist()
            self.last_start['quota'] = 0
        while not self.q.empty():
            self.apply(*self.q.get())
        for name, interval, fn in [('quota', 600, read_quota), ('tibo', 600, read_tibo)]:
            if time.monotonic()-self.last_start[name] >= interval:
                self.start(name, fn)
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
