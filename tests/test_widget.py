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
