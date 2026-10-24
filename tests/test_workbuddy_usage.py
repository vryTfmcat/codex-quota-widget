import _test_environment
import copy
import importlib.util
import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch
from workbuddy_usage import SnapshotStore, balance_view, changes, expiry_alerts, timestamp, validate


def fixture():
    return {'schema_version': 1, 'live': False, 'captured_date': '2026-10-18', 'captured_at': None,
            'total_granted': 100, 'total_used': 25, 'total_remaining': 75,
            'batches': [{'snapshot_batch_id': 'row1', 'source_batch_id': None,
                         'type': '奖励', 'expires_at': '2026-10-29T14:51:26+08:00',
                         'total': 100, 'used': 25, 'remaining': 75}],
            'model_display_rates': [{'name': 'example', 'display_multiplier': .1, 'label': '参考'}]}


class WorkBuddyTests(unittest.TestCase):
    def test_account_balance_is_separate_from_reward_batches(self):
        old=fixture();new=copy.deepcopy(old)
        new['account_balance']={'remaining':575,'categories':[{'name':'套餐基础积分','remaining':500},{'name':'平台奖励积分','remaining':75},{'name':'购买积分','remaining':0}]}
        validate(new)
        self.assertEqual(new['total_remaining'],75)
        self.assertIn('不作为新增赠送','\n'.join(changes(old,new)))
        new['account_balance']['remaining']=576
        with self.assertRaises(ValueError): validate(new)

    def test_invalid_is_unknown_not_zero(self):
        for field, value in [('batches', []), ('total_remaining', float('nan')), ('total_used', 26)]:
            data = fixture(); data[field] = value
            with self.assertRaises(ValueError): validate(data)

    def test_store_dedup_and_failure_preserves_history(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder); source = folder/'input.json'; store = SnapshotStore(folder/'history.json')
            source.write_text(json.dumps(fixture()), encoding='utf-8')
            self.assertTrue(store.import_file(source, 1))
            self.assertFalse(store.import_file(source, 2))
            source.write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError): store.import_file(source, 3)
            self.assertEqual(len(SnapshotStore(store.path).entries), 1)
            self.assertEqual(store.latest['total_remaining'], 75)
            old=fixture();old['captured_date']='2026-10-17';source.write_text(json.dumps(old),encoding='utf-8')
            with self.assertRaises(ValueError): store.import_file(source,4)

    def test_observed_consumption_and_unknown(self):
        old=fixture(); new=copy.deepcopy(old)
        new['batches'][0].update(used=30,remaining=70)
        self.assertIn('观察到的消费', changes(old,new)[0])
        new['batches'][0]['snapshot_batch_id']='different-row-number'
        self.assertIn('增加 5',changes(old,new)[0])
        new['batches'][0]['expires_at']='2026-10-30T14:51:26+08:00'
        result='\n'.join(changes(old,new))
        self.assertIn('是否新赠送未知',result);self.assertIn('记录消失',result)
        duplicate=copy.deepcopy(old);duplicate['batches']*=2
        self.assertIn('匹配不唯一',changes(duplicate,old)[0])

    def test_expiry_is_distinct_from_consumption(self):
        data=fixture(); deadline=timestamp(data['batches'][0]['expires_at'])
        before=balance_view(data,deadline-1); after=balance_view(data,deadline)
        self.assertEqual(before['unexpired_snapshot'],75)
        self.assertEqual(after['unexpired_snapshot'],0)
        self.assertEqual(after['past_due_snapshot'],75)
        self.assertEqual(data['total_used'],25)

    def test_reminders_dedup_restart_and_cross_threshold(self):
        data=fixture(); deadline=timestamp(data['batches'][0]['expires_at']);seen={}
        self.assertEqual(expiry_alerts(data,deadline-8*86400,seen),[])
        self.assertEqual(len(expiry_alerts(data,deadline-5*86400,seen)),1)
        seen=json.loads(json.dumps(seen))
        self.assertEqual(expiry_alerts(data,deadline-4*86400,seen),[])
        self.assertEqual(len(expiry_alerts(data,deadline-1800,seen)),1)
        self.assertEqual(expiry_alerts(data,deadline-1799,seen),[])
        self.assertIn('已过',expiry_alerts(data,deadline,seen)[0])
        data['batches'][0]['remaining']=0
        self.assertEqual(expiry_alerts(data,deadline,{}),[])

    def test_markdown_write_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);source=folder/'input.json';target=folder/'view.md'
            source.write_text(json.dumps(fixture()),encoding='utf-8')
            store=SnapshotStore(folder/'history.json');store.import_file(source,1)
            store.export_markdown(target);first=target.stat().st_mtime_ns
            store.export_markdown(target);self.assertEqual(first,target.stat().st_mtime_ns)
            self.assertIn('非实时余额',target.read_text(encoding='utf-8'))
            target.write_text('user note',encoding='utf-8')
            with self.assertRaises(ValueError):store.export_markdown(target)

    def test_offline_widget_integration(self):
        spec=importlib.util.spec_from_file_location('widget_test',Path(__file__).resolve().parents[1]/'usage_widget.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);source=folder/'snapshot.json';source.write_text(json.dumps(fixture()),encoding='utf-8')
            config={'snapshot_path':str(source)}
            (folder/'workbuddy.local.json').write_text(json.dumps(config),encoding='utf-8')
            with patch.object(module,'ROOT',folder),patch.object(module,'STATE',folder/'state.json'),patch.object(module,'STATUS',folder/'status.json'):
                widget=module.Widget();widget.root.withdraw()
                try:
                    widget.wb_reload();widget.wb_display(timestamp('2026-10-18T01:00:00+08:00'))
                    self.assertEqual(len(widget.wb_table.get_children()),1)
                    self.assertIn('75.00',widget.wb_summary.cget('text'))
                    self.assertIn('非实时',widget.wb_summary.cget('text'))
                    from tkinter import ttk
                    style=ttk.Style(widget.root)
                    self.assertEqual(style.lookup('WorkBuddy.Treeview','background'),module.PANEL)
                    self.assertGreater(int(style.lookup('WorkBuddy.Treeview','rowheight')),widget.wb_table_font.metrics('linespace'))
                    heading,body=widget.section_controls['WorkBuddy 积分']
                    self.assertFalse(widget.state['sections']['WorkBuddy 积分'])
                    heading.invoke();self.assertTrue(widget.state['sections']['WorkBuddy 积分'])
                    heading.invoke();self.assertFalse(widget.state['sections']['WorkBuddy 积分'])
                    widget.wb_reload(str(folder/'missing.json'))
                    widget.wb_display(timestamp('2026-10-18T01:00:00+08:00'))
                    self.assertIn('保留上次快照',widget.wb_summary.cget('text'))
                    self.assertEqual(widget.wb_store.latest['total_remaining'],75)
                finally:
                    widget.close()


if __name__=='__main__':
    unittest.main()
