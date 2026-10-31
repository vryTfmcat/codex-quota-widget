import _test_environment
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import usage_widget as m


class DockTests(unittest.TestCase):
    def test_native_topmost_restores_flag_without_changing_focus(self):
        import ctypes
        from ctypes import wintypes
        import tkinter as tk
        root=tk.Tk();root.overrideredirect(True)
        root.geometry('80x45+30+30');root.update()
        try:
            user=ctypes.windll.user32
            user.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
            user.GetAncestor.restype=wintypes.HWND
            user.GetForegroundWindow.restype=wintypes.HWND
            self.assertTrue(m.promote_topmost(root.winfo_id()))
            hwnd=user.GetAncestor(root.winfo_id(),2)
            user.SetWindowPos(hwnd,wintypes.HWND(-2),0,0,0,0,0x0013)
            self.assertFalse(user.GetWindowLongW(hwnd,-20)&8)
            before=user.GetForegroundWindow()
            self.assertTrue(m.promote_topmost(root.winfo_id()))
            self.assertEqual(user.GetForegroundWindow(),before)
        finally:root.destroy()

    def test_compact_countdown_idle_stale_and_missing(self):
        five={'windowDurationMins':300,'usedPercent':63,'resetsAt':10000}
        self.assertEqual(m.compact_summary(five,6400),'5h 37%\n01:00:00')
        self.assertIn('旧',m.compact_summary(five,6400,True))
        self.assertIn('待核对',m.compact_summary(five,10001))
        five['usedPercent']=0
        self.assertEqual(m.compact_summary(five,6400),'5h 100%\n待开始')
        self.assertIn('待读取',m.compact_summary(None,6400))

    def test_edge_and_vertical_clamping(self):
        self.assertEqual(m.dock_position((0,0,1920,1040),90,50,'right',100), (1830,100))
        self.assertEqual(m.dock_position((-1920,0,0,1040),90,50,'left',5000),(-1920,990))
        self.assertEqual(m.dock_position((0,40,1920,1080),900,700,'left',-10),(0,40))

    def test_mini_peek_details_auto_hide_drag_and_disable(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)
            with patch.object(m,'ROOT',p),patch.object(m,'STATE',p/'state.json'),patch.object(m,'STATUS',p/'status.json'):
                app=m.Widget()
                try:
                    app.quota={'at':time.time(),'windows':[{'windowDurationMins':300,'usedPercent':63,'resetsAt':time.time()+3600}]}
                    app.display();app.root.update_idletasks()
                    self.assertTrue(app.dock_compact)
                    self.assertLess(app.root.winfo_width(),180)
                    self.assertLess(app.root.winfo_height(),110)
                    self.assertIn('5h 37%',app.mini_label.cget('text'))
                    mini_height=app.root.winfo_height()
                    with patch.object(app,'pointer_inside',return_value=True):app.dock_hover()
                    self.assertFalse(app.dock_compact)
                    self.assertTrue(app.bar.winfo_manager())
                    app.toggle();app.root.update_idletasks()
                    self.assertTrue(app.expanded)
                    self.assertGreater(app.root.winfo_height(),mini_height)
                    app.dock_hide();self.assertFalse(app.dock_compact)
                    app.toggle()
                    with patch.object(app,'pointer_inside',return_value=False):app.dock_maybe_hide()
                    self.assertTrue(app.dock_compact)
                    app.dock_dragging=True;app.dock_show();app.dock_hide()
                    self.assertFalse(app.dock_compact)
                    app.dock_dragging=False
                    app.toggle_dock();self.assertFalse(app.dock_enabled)
                    app.dock_hide();self.assertFalse(app.dock_compact)
                    self.assertFalse(m.load(p/'state.json')['dock_enabled'])
                    app.root.update_idletasks()
                finally:app.close()


if __name__=='__main__':unittest.main()
