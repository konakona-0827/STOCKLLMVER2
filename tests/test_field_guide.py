"""Presentation changes preserve decisions and explain units without API calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen

from support_scout.dashboard import DeskServer
from support_scout.field_guide import labeled_tree, render_field_guide


class FieldGuideTests(unittest.TestCase):
    def test_ratios_null_zero_and_booleans_have_distinct_meanings(self):
        html=labeled_tree({'target_return_fraction':.02,'suggested_amount_jpy':0,
                           'estimated_loss_jpy':None,'execution_enabled':False})
        for value in ['2.00%','原值 0.02','建議投入金額（日圓）','不適用（null）','否（false）']:
            self.assertIn(value,html)
        self.assertNotIn('2.00%',labeled_tree({'selection_reward_risk':2.0}))

    def test_nested_values_are_escaped_and_original_keys_preserved(self):
        item={'symbol':'6857.T','name':'<script>bad()</script>',
              'opportunity_decision':{'action':'WAIT','new_future_field':'<img onerror=bad()>'}}
        before=deepcopy(item);html=render_field_guide({'securities':[item]})
        self.assertEqual(item,before)
        self.assertIn('等待（WAIT）',html);self.assertIn('new_future_field',html)
        self.assertIn('新增／未登錄欄位',html);self.assertIn('opportunity_decision',html)
        self.assertNotIn('<script>bad()',html);self.assertNotIn('<img onerror',html)

    def test_old_report_receives_labels_without_changing_archive_or_running_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);reports=root/'reports';run=reports/'old';run.mkdir(parents=True)
            archived='<!doctype html><main><h1>舊報告</h1></main>'
            (run/'report.html').write_text(archived,encoding='utf-8')
            (run/'committed.json').write_text('{}')
            report={'as_of':'2026-11-02T13:00:00+09:00','securities':[{'symbol':'6857.T','execution_plan':{'sizing_status':'NO_NEW_BUY_SIGNAL'}}]}
            (run/'report.json').write_text(json.dumps(report))
            (reports/'latest.json').write_text(json.dumps({'report':str(run/'report.html')}))
            calls=[];server=DeskServer(0,data_root=root,output_root=reports,session_runner=lambda **kw:calls.append(kw))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base=f'http://127.0.0.1:{server.server_port}'
                with urlopen(base) as response:html=response.read().decode()
                for text in ['程式連線正常','程式連線中斷','立即手動分析','JSON中文欄位說明','沒有新的買入訊號']:
                    self.assertIn(text,html)
                with urlopen(base+'/api/state') as response:state=json.load(response)
                self.assertIn('server_time',state);self.assertEqual(calls,[])
                self.assertEqual((run/'report.html').read_text(encoding='utf-8'),archived)
            finally:
                server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
