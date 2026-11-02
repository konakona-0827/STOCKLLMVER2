from copy import deepcopy
from io import BytesIO
import json
import re
import unittest
from unittest.mock import patch

from support_scout.dashboard import DESK_UI_VERSION,serve
from support_scout.japan_demo import demo_bundle
from support_scout.pipeline import run
from support_scout.reporting import render_html
from support_scout.scan_schedule import SCHEDULE_VERSION
from support_scout.settings import ROOT


class DeskLayoutTests(unittest.TestCase):
    def test_comparisons_stay_together_and_each_stock_has_four_hidden_detail_panels(self):
        bundle=demo_bundle();report=run(bundle);before=deepcopy(report)
        html=render_html(report,bundle)
        self.assertEqual(report,before)
        self.assertEqual(html.count('data-desk-tab='),4)
        self.assertEqual(html.count('data-stock='),len(report['securities']))
        self.assertEqual(html.count('data-stock-panel='),len(report['securities'])*4)
        self.assertIn('id="observation-table"',html)
        for label in ['總覽比較','個股研究','持倉與通知','掃描與稽核','JSON中文說明']:
            self.assertIn(label,html)
        ids=re.findall(r'\bid="([^"]+)"',html)
        self.assertEqual(len(ids),len(set(ids)))
        self.assertNotIn('fetch(',html)  # Pure report tab switching has zero network/model calls.

    def test_upgrade_pauses_old_app_before_reusing_updated_server_without_killing_processes(self):
        def response(value):return BytesIO(json.dumps(value).encode())
        old={'project_root':str(ROOT.resolve()),'scheduler':{'engine':'PROGRAM_INTERNAL','enabled':True,'running':False}}
        current={**old,'ui_version':DESK_UI_VERSION,'scheduler':{**old['scheduler'],'schedule_version':SCHEDULE_VERSION}}
        replies=[response(old),BytesIO(b'<script>const deskToken="fixture-local-token";</script>'),response({'enabled':False}),response(current)]
        with patch('urllib.request.urlopen',side_effect=replies) as http,patch('support_scout.dashboard.open_dashboard') as browser:
            serve(8765,open_browser=True)
        request=http.call_args_list[2].args[0]
        self.assertEqual(request.full_url,'http://127.0.0.1:8765/api/scheduler')
        self.assertEqual(json.loads(request.data),{'enabled':False})
        browser.assert_called_once_with('http://127.0.0.1:8766/#scan-control')


if __name__=='__main__':unittest.main()
