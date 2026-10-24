import _test_environment
import base64
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import workbuddy_client as c
import usage_widget as w


def summary():
    return {'Packages':[{'PackageCode':'sample','CycleTotalCapacity':'100','CycleRemainCapacity':'75.25','CycleUsedCapacity':'24.75'}],
            'TotalCredits':75.25,'CreditGroups':[{'Kind':'benefit','Name':'奖励','CycleRemainCapacity':'75.25'}]}


def batch():
    return {'AccountId':'sample-id','ResourceCycleId':'sample-cycle','PackageName':'奖励',
            'CycleCapacitySizePrecise':'100','CycleCapacityRemainPrecise':'75.25','CycleCapacityUsedPrecise':'24.75',
            'CycleEndTime':'2030-01-10 12:30:00','DeductionEndTime':None,'ExpiredTime':'2029-12-01 00:00:00'}


def token(uid='example'):
    payload=base64.urlsafe_b64encode(json.dumps({'sub':uid,'exp':time.time()+3600}).encode()).decode().rstrip('=')
    return {'accessToken':'example.'+payload+'.signature','refreshToken':'fake-refresh','domain':'www.workbuddy.cn'}


class ClientBillingTests(unittest.TestCase):
    def test_decimal_values_and_bad_summary(self):
        s=c.parse_summary(summary(),1,'hash')
        self.assertEqual(s['remaining'],75.25)
        for value in (None,'',False,'NaN','-1'):
            with self.assertRaises(c.ApiError):c.decimal(value)
        data=summary();data['TotalCredits']=76
        with self.assertRaises(c.ApiError):c.parse_summary(data,1,'hash')
        data=summary();data['Packages']=[]
        with self.assertRaises(c.ApiError):c.parse_summary(data,1,'hash')

    def test_precise_batch_and_actual_expiry_not_completion_time(self):
        row=c.normalize_batch(batch())
        self.assertEqual(row['remaining'],75.25)
        self.assertEqual(row['expires_at'],'2030-01-10T12:30:00+08:00')
        self.assertEqual(row['source_batch_id'],'sample-id|sample-cycle')
        bad=batch();bad['CycleCapacityUsedPrecise']='0'
        with self.assertRaises(c.ApiError):c.normalize_batch(bad)

    def test_endpoint_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            client=c.ClientBilling(Path(folder)/'client.dpapi')
            for path in ('/v2/chat/completions','/billing/meter/daily-checkin','https://example.com/'):
                with self.assertRaises(c.ApiError):client.request(path)

    def test_login_save_and_account_stability(self):
        with tempfile.TemporaryDirectory() as folder:
            client=c.ClientBilling(Path(folder)/'client.dpapi')
            saved=client.store_token(token())
            self.assertNotIn(b'fake-refresh',client.path.read_bytes())
            with self.assertRaises(c.ApiError):client.store_token(token('different'),saved)
            self.assertEqual(client._read()['uid'],'example')
            with patch.object(client,'request',return_value={'state':'fake','authUrl':'https://attacker.example/login'}):
                with self.assertRaises(c.ApiError):client.start_login()

    def test_full_fetch_and_partial_details(self):
        with tempfile.TemporaryDirectory() as folder:
            client=c.ClientBilling(Path(folder)/'client.dpapi');client.store_token(token())
            responses=[summary(),{'Accounts':[],'TotalCount':0},{'Accounts':[batch()],'TotalCount':1}]
            with patch.object(client,'request',side_effect=responses):row=client.fetch()
            self.assertEqual(row['snapshot']['batches'][0]['remaining'],75.25)
            self.assertEqual(row['source'],'client-billing')
            self.assertNotIn('fake-refresh',json.dumps(row))
            with patch.object(client,'request',side_effect=[summary(),c.ApiError('client-network-or-response')]):partial=client.fetch()
            self.assertEqual(partial['remaining'],75.25)
            self.assertNotIn('snapshot',partial)
            self.assertEqual(partial['detail_error'],'client-network-or-response')

    def test_pagination_and_incomplete_page(self):
        with tempfile.TemporaryDirectory() as folder:
            client=c.ClientBilling(Path(folder)/'client.dpapi')
            with patch.object(client,'request',side_effect=[{'Accounts':[batch()],'TotalCount':2},{'Accounts':[],'TotalCount':2}]):
                with self.assertRaises(c.ApiError):client.detail(c.FREE,['sample'],{})

    def test_widget_uses_live_batches_and_keeps_last_good_details(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            with patch.object(w,'ROOT',folder),patch.object(w,'STATE',folder/'state.json'),patch.object(w,'STATUS',folder/'status.json'):
                app=w.Widget();app.root.withdraw()
                try:
                    row=c.parse_summary(summary(),time.time(),'account-hash')
                    row['snapshot']={'captured_at':'2030-01-01T00:00:00+08:00','captured_date':'2030-01-01',
                        'total_remaining':75.25,'total_granted':100,'total_used':24.75,'batches':[c.normalize_batch(batch())]}
                    app.state['workbuddy_reminders']=False
                    app.apply('workbuddy-live',row,None);app.wb_display(time.time())
                    self.assertEqual(len(app.wb_table.get_children()),1)
                    self.assertIn('线上批次明细',app.wb_summary.cget('text'))
                    partial=c.parse_summary(summary(),time.time()+1,'account-hash');partial['detail_error']='test-error'
                    app.apply('workbuddy-live',partial,None);app.wb_display(time.time())
                    self.assertEqual(len(app.wb_table.get_children()),1)
                    self.assertIn('上次成功批次',app.wb_summary.cget('text'))
                    self.assertEqual(app.wb_live['batches_at'],row['at'])
                finally:app.close()


if __name__=='__main__':unittest.main()
