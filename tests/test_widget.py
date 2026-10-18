import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


class WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.name != 'nt':
            raise unittest.SkipTest('Windows-only widget')
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {'CODEX_QUOTA_DATA_DIR': cls.temp.name,
                                          'CODEX_QUOTA_WEEKLY_PER_WINDOW': '16'})
        cls.env.start()
        import usage_widget
        cls.m = usage_widget

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def test_window_capacity(self):
        windows = [{'windowDurationMins': 300, 'usedPercent': 27, 'resetsAt': 10000},
                   {'windowDurationMins': 10080, 'usedPercent': 41, 'resetsAt': 1000000}]
        f = self.m.forecast(windows, 0, 80000)
        self.assertEqual([round(slot['after'], 2) for slot in f['slots']], [47.32, 31.32, 15.32, 0])
        self.assertEqual(f['earliest'], 46000)
        self.assertEqual(f['finish_by'], 64000)
        self.assertIsNone(self.m.forecast(windows, 10000))

    def test_idle_and_usage_countdown(self):
        five = {'windowDurationMins': 300, 'usedPercent': 0, 'resetsAt': 13600}
        week = {'windowDurationMins': 10080, 'usedPercent': 40, 'resetsAt': 1000000}
        self.assertIn('尚未开始使用', self.m.five_summary(five, 10000))
        self.assertTrue(self.m.forecast([five, week], 10000)['idle'])
        self.assertEqual(self.m.due_alerts(14000, {'pending_windows': [five]}), [])
        five['usedPercent'] = .1
        self.assertIn('01:00:00', self.m.five_summary(five, 10000))
        self.assertFalse(self.m.forecast([five, week], 10000).get('idle', False))
        self.assertEqual(len(self.m.due_alerts(14000, {'pending_windows': [five]})), 1)

    def test_capacity_parameter_and_deadline_boundary(self):
        windows = [{'windowDurationMins': 300, 'usedPercent': 50, 'resetsAt': 10000},
                   {'windowDurationMins': 10080, 'usedPercent': 50, 'resetsAt': 1000000}]
        with patch.object(self.m, 'WEEKLY_PER_WINDOW', 20):
            f = self.m.forecast(windows, 0, 10000)
            self.assertEqual(f['current_capacity'], 10)
            self.assertEqual(f['at_deadline'], 40)
            self.assertEqual(self.m.forecast(windows, 0, 10001)['at_deadline'], 20)

    def test_timeline_alignment_and_rolling_history(self):
        windows = [{'windowDurationMins': 300, 'usedPercent': 20, 'resetsAt': 20000},
                   {'windowDurationMins': 10080, 'usedPercent': 40, 'resetsAt': 1000000}]
        f = self.m.forecast(windows, 10000)
        layout = self.m.timeline_layout(f, 10000)
        moved = self.m.timeline_layout(f, 10600)
        self.assertEqual(layout['start'], 10000-18000)
        self.assertEqual(layout['slots'][0]['start'], 20000-18000)
        self.assertAlmostEqual(layout['y'](20000)-layout['y'](2000), 150)
        self.assertAlmostEqual(layout['y'](20000)-moved['y'](20000), 5)

    def test_scheduled_notice_watch_fallback_and_revision(self):
        scheduled = {'tweet_id': 'example-schedule', 'scheduled_for': '2030-01-02T12:00:00Z',
                     'announced_at': '2030-01-01T12:00:00Z'}
        watch = {'tweet_id': 'example-watch', 'expires_at': '2030-01-03T12:00:00Z'}
        now = self.m.iso_time('2030-01-02T00:00:00Z')
        notice = self.m.tibo_notice({'scheduled': scheduled, 'watch': watch}, now)
        self.assertEqual(notice['kind'], 'scheduled')
        self.assertEqual(self.m.tibo_notice({'watch': watch}, now)['kind'], 'watch')
        invalid = dict(scheduled, scheduled_for='invalid')
        self.assertEqual(self.m.tibo_notice({'scheduled': invalid, 'watch': watch}, now)['kind'], 'watch')
        self.assertFalse(self.m.tibo_notice({'scheduled': scheduled}, notice['deadline']+1)['active'])
        revision = dict(notice, deadline=notice['deadline']+3600)
        self.assertNotEqual(self.m.notice_signature(notice), self.m.notice_signature(revision))
        complete = {'announced_at': '2030-01-03T12:00:00Z'}
        self.assertIsNone(self.m.tibo_notice({'scheduled': scheduled, 'latest': complete}, now))

    def test_idle_render_and_latest_projection(self):
        app = self.m.Widget()
        try:
            now = time.time()
            app.quota = {'at': now, 'windows': [
                {'windowDurationMins': 300, 'usedPercent': 0, 'resetsAt': now+3600},
                {'windowDurationMins': 10080, 'usedPercent': 40, 'resetsAt': now+604800}]}
            app.display()
            self.assertIn('尚未开始使用', app.main.cget('text'))
            self.assertFalse(app.timeline.find_withtag('window-card-0'))
            deadline = now+7200
            app.draw_forecast(self.m.forecast(app.quota['windows'], now, deadline), now, deadline, False)
            self.assertTrue(app.timeline.find_withtag('reset-deadline'))
            app.history_base = {'at': now-18000, 'windows': [
                {'windowDurationMins': 300, 'usedPercent': 90, 'resetsAt': now-1},
                {'windowDurationMins': 10080, 'usedPercent': 80, 'resetsAt': now+604800}]}
            app.quota['windows'][0]['usedPercent'] = 20
            app.display()
            curve = app.timeline.coords(app.timeline.find_withtag('theory')[0])
            self.assertAlmostEqual(curve[0], 95+(214-95)*.4)
            self.assertTrue(app.timeline.find_withtag('window-card-0'))
            self.assertFalse(any('WorkBuddy' in name or 'Flower' in name for name in app.section_controls))
        finally:
            app.close()

    def test_offline_widget_and_controls(self):
        app = self.m.Widget()
        try:
            app.last_start = {'quota': time.monotonic(), 'tibo': time.monotonic()}
            app.toggle()
            app.root.update()
            self.assertFalse(any('Flower' in title for title in app.section_controls))
            self.assertEqual(app.refresh_button.master, app.main.master)
            self.assertFalse(hasattr(app, 'sound_choice'))
            app.main.event_generate('<Button-1>', x=5, y=5, rootx=110, rooty=110)
            app.main.event_generate('<ButtonRelease-1>', x=5, y=5, rootx=110, rooty=110)
            app.root.update()
            self.assertFalse(app.expanded)
            for heading, body in app.section_controls.values():
                before = bool(body.winfo_manager())
                heading.invoke()
                self.assertNotEqual(bool(body.winfo_manager()), before)
        finally:
            app.close()


if __name__ == '__main__':
    unittest.main()
