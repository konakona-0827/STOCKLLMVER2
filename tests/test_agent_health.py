import tempfile
from pathlib import Path
import unittest

from support_scout.agent_health import model_health
from support_scout.observation import observe
from support_scout.presentation import report_status
from support_scout.providers import ProviderError
from tests.test_observation import observation_fixture
from tests.test_desk import FakeProvider


class AgentHealthTests(unittest.TestCase):
    def test_partial_failure_stays_degraded_and_keeps_valid_peer(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            r,_,code=observe(bundle=observation_fixture(),agents=True,researcher=FakeProvider(),
                reviewer=FakeProvider(failure=ProviderError('API_TIMEOUT')),max_packets=1,max_calls=2,
                output_root=root/'out',db_path=root/'data.sqlite3',settings_path=root/'absent.json')
            self.assertEqual(code,3)
            self.assertEqual(r['observation']['agent_status'],'PARTIAL_FAILURE')
            self.assertIn('MODEL_RESEARCH_INCOMPLETE',r['observation']['degraded_reasons'])
            h=model_health(r)
            self.assertEqual(h['accepted_reviews'],1)
            self.assertEqual(h['label'],'部分研究未完成')
            self.assertIn('API等待逾時',h['text'])
            self.assertEqual(r['reviewed_shortlist'],[])

    def test_input_budget_and_dependent_skip_are_distinct_from_api_failure(self):
        r={'desk':{'events':[{'role':'researcher','symbol':'A','status':'ERROR','error':'COMPACT_INPUT_BUDGET_EXCEEDED'},
            {'role':'reviewer','symbol':'A','status':'SKIPPED','error':'SHARED_PUBLIC_SOURCES_UNAVAILABLE_NO_SECOND_SEARCH'}]}}
        h=model_health(r)
        self.assertEqual((h['error_roles'],h['skipped_roles']),(1,1))
        self.assertIn('尚未送到API',h['text']);self.assertIn('連帶略過',h['text'])
        self.assertIn('不是第二次API故障',h['text'])

    def test_data_degraded_does_not_relabel_successful_models(self):
        r={'securities':[],'collection':{'status':'DEGRADED'},'observation':{'agent_status':'COMPLETED','status':'DEGRADED'},
            'desk':{'events':[{'status':'ACCEPTED'}]*8,'error_count':0}}
        h=model_health(r)
        self.assertEqual(h['label'],'模型研究已完成')
        self.assertIn('8/8',h['text'])
        self.assertIn('資料降級與模型研究狀態分開',report_status(r)['text'])

    def test_offline_reviews_are_not_described_as_actual_model_success(self):
        self.assertIn('未呼叫模型',model_health({'desk':{'mode':'offline_simulation','events':[{'status':'ACCEPTED'}]}})['label'])


if __name__ == '__main__':unittest.main()
