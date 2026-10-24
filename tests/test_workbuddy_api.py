import _test_environment
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
import workbuddy_api as api


class OfficialApiTests(unittest.TestCase):
    def test_decimal_balance_and_missing_values(self):
        self.assertEqual(api.parse_credit({'total_capacity_size':1000,'total_capacity_used':123.45},1)['remaining'],876.55)
        self.assertEqual(api.parse_credit({'data':{'total_capacity_size':0,'total_capacity_used':0}},1)['remaining'],0)
        for data in ({},{'total_capacity_size':2,'total_capacity_used':3},
                     {'total_capacity_size':float('nan'),'total_capacity_used':0}):
            with self.assertRaises(api.ApiError):api.parse_credit(data,1)

    def test_dpapi_roundtrip_and_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'oauth.dpapi';client=api.CreditClient(p)
            client.configure('test-client','synthetic-secret')
            self.assertNotIn(b'synthetic-secret',p.read_bytes())
            self.assertEqual(client._read()['client_id'],'test-client')
            url=client.authorization_url('random-state')
            self.assertEqual(urlsplit(url).hostname,'www.workbuddy.cn')
            params=parse_qs(urlsplit(url).query)
            self.assertEqual(params['scope'],[api.SCOPE])
            self.assertEqual(params['redirect_uri'],[api.REDIRECT])
            self.assertNotIn('synthetic-secret',url)

    def test_callback_state_path_and_denial(self):
        self.assertEqual(api.valid_callback('/workbuddy/callback?code=abc&state=right','right'),'abc')
        for path in ('/wrong?code=abc&state=right','/workbuddy/callback?code=abc&state=wrong',
                     '/workbuddy/callback?state=right&error=access_denied'):
            with self.assertRaises(api.ApiError):api.valid_callback(path,'right')

    def test_token_refresh_and_no_secret_in_result(self):
        with tempfile.TemporaryDirectory() as folder:
            client=api.CreditClient(Path(folder)/'oauth.dpapi')
            client.configure('test-client','synthetic-secret')
            token={'access_token':'fake-access','refresh_token':'fake-refresh','expires_in':3600,'scope':api.SCOPE}
            with patch.object(api,'request_json',return_value=token):client.exchange('single-use-code')
            saved=client._read();saved['expires_at']=0;client._save(saved)
            with patch.object(api,'request_json',side_effect=[dict(token,access_token='rotated-access'),
                {'total_capacity_size':100,'total_capacity_used':25}]) as call:
                row=client.fetch()
                self.assertEqual(call.call_args_list[0].kwargs['form']['grant_type'],'refresh_token')
                self.assertEqual(row['remaining'],75)
                self.assertNotIn('fake',json.dumps(row))
            # Reject a grant for unrelated permissions, keeping previous working token.
            with patch.object(api,'request_json',return_value=dict(token,scope='user.task.readable')):
                with self.assertRaises(api.ApiError):client.exchange('other-code')
            self.assertEqual(client._read()['access_token'],'rotated-access')

    def test_history_and_markdown_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);history=api.CreditHistory(folder/'history.json')
            row=api.parse_credit({'total_capacity_size':100,'total_capacity_used':25},time.time())
            history.record(row);self.assertEqual(api.CreditHistory(history.path).samples[-1]['remaining'],75)
            md=folder/'view.md';history.export_markdown(md)
            self.assertIn('不统一标记为消费',md.read_text(encoding='utf-8'))
            md.write_text('user note',encoding='utf-8')
            with self.assertRaises(api.ApiError):history.export_markdown(md)

    def test_widget_live_result_and_failure_keep_snapshot_separate(self):
        import usage_widget as widget
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            with patch.object(widget,'ROOT',folder),patch.object(widget,'STATE',folder/'state.json'),patch.object(widget,'STATUS',folder/'status.json'):
                app=widget.Widget();app.root.withdraw()
                try:
                    row=api.parse_credit({'total_capacity_size':500,'total_capacity_used':100},time.time())
                    app.apply('workbuddy-live',row,None)
                    app.wb_show_live(time.time())
                    self.assertIn('400.00',app.wb_live_detail.cget('text'))
                    app.apply('workbuddy-live',None,'network-or-response')
                    app.wb_show_live(time.time())
                    self.assertEqual(app.wb_live['remaining'],400)
                    self.assertIn('旧读数与快照保留',app.wb_live_detail.cget('text'))
                    self.assertIsNone(app.wb_store.latest)
                finally:app.close()


if __name__=='__main__':unittest.main()
