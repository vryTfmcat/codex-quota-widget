import _test_environment
import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import usage_widget as m


def sample(used=40, week=50):
    now=time.time()
    return {'at':now,'windows':[
        {'windowDurationMins':300,'usedPercent':used,'resetsAt':now+1000},
        {'windowDurationMins':10080,'usedPercent':week,'resetsAt':now+604800}]}


class ResetWatchTests(unittest.TestCase):
    def test_consumption_and_small_clock_drift_are_not_resets(self):
        before=sample();after=copy.deepcopy(before)
        after['windows'][0]['usedPercent']+=5
        after['windows'][0]['resetsAt']+=2
        self.assertEqual(m.reset_changes(before,after),[])
        self.assertEqual(m.reset_changes(before,{'windows':[]}),[])

    def test_refill_week_or_reschedule_is_reported(self):
        before=sample();after=copy.deepcopy(before)
        after['windows'][0]['usedPercent']=0
        self.assertTrue(any('5h剩余' in s for s in m.reset_changes(before,after)))
        after=copy.deepcopy(before);after['windows'][1]['usedPercent']=0
        self.assertTrue(any('每周剩余' in s for s in m.reset_changes(before,after)))
        after=copy.deepcopy(before);after['windows'][0]['resetsAt']+=100
        self.assertTrue(any('重置时间' in s for s in m.reset_changes(before,after)))

    def test_idle_start_is_not_reset(self):
        before=sample(used=0);after=copy.deepcopy(before)
        after['windows'][0].update(usedPercent=1,resetsAt=after['windows'][0]['resetsAt']+3600)
        self.assertEqual(m.reset_changes(before,after),[])

    def test_toggle_cadence_alert_once_and_return_to_normal(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            with patch.object(m,'ROOT',folder),patch.object(m,'STATE',folder/'state.json'),patch.object(m,'STATUS',folder/'status.json'):
                app=m.Widget();app.root.withdraw()
                try:
                    self.assertFalse(app.reset_watch)
                    app.quota=sample()
                    with patch.object(app,'start') as start,patch.object(app,'alert') as alert:
                        app.toggle_reset_watch()
                        self.assertTrue(app.reset_watch)
                        start.assert_called_once_with('quota',m.read_quota,reason='reset-watch')
                        start.reset_mock()
                        app.last_start={'quota':90,'tibo':90};app.wb_checked=100
                        with patch.object(m.time,'monotonic',return_value=100):app.tick()
                        start.assert_called_once_with('quota',m.read_quota,reason='reset-watch')
                        # A failed read does not establish a new baseline or stop monitoring.
                        baseline=app.reset_watch_baseline
                        app.apply('quota',None,'network')
                        self.assertIs(app.reset_watch_baseline,baseline)
                        self.assertTrue(app.reset_watch)
                        reset=sample(used=0)
                        app.check_reset_watch(reset)
                        self.assertFalse(app.reset_watch);alert.assert_called_once()
                        app.check_reset_watch(reset);alert.assert_called_once()
                        start.reset_mock();app.last_start={'quota':90,'tibo':90}
                        with patch.object(m.time,'monotonic',return_value=100):app.tick()
                        start.assert_not_called()
                        app.quota=sample();app.quota['at']-=3600
                        app.toggle_reset_watch();self.assertIsNone(app.reset_watch_baseline)
                        app.check_reset_watch(sample(used=0))
                        self.assertTrue(app.reset_watch)  # First fresh reading only establishes a baseline.
                        app.toggle_reset_watch();self.assertFalse(app.reset_watch)
                finally:app.close()


if __name__=='__main__':unittest.main()
