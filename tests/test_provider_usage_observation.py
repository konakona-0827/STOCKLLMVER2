import asyncio
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from research_runtime import ResponseRecorder,usage_actor


class ProviderUsageObservationTests(unittest.TestCase):
    def test_provider_model_and_wall_clock_are_recorded_even_during_historical_replay(self):
        async def response(**kwargs):
            return SimpleNamespace(id='resp_saved',status='completed',model='gpt-6-sol',service_tier='default',
                usage=SimpleNamespace(input_tokens=100,output_tokens=10,
                    input_tokens_details=SimpleNamespace(cached_tokens=50,cache_write_tokens=40),
                    output_tokens_details=SimpleNamespace(reasoning_tokens=5)))
        client=SimpleNamespace(responses=SimpleNamespace(create=response))
        with tempfile.TemporaryDirectory() as tmp:
            recorder=ResponseRecorder(Path(tmp)/'usage.json','test').install(client)
            with patch('research_runtime.now',return_value=datetime(2026,10,19,tzinfo=timezone.utc)):
                asyncio.run(client.responses.create(model='gpt-6-sol',input='frozen data'))
            row=recorder.rows[0]
            self.assertEqual(row['model'],'gpt-6-sol')
            self.assertEqual(row['service_tier'],'default')
            self.assertLess(abs((datetime.now(timezone.utc)-datetime.fromisoformat(row['observed_at_utc'])).total_seconds()),5)
            self.assertEqual(row['input_tokens'],100)

    def test_parallel_specialists_keep_separate_usage_actor_labels(self):
        sequence=0
        async def response(**kwargs):
            nonlocal sequence
            sequence+=1;identity=sequence
            await asyncio.sleep(0)
            return SimpleNamespace(id='resp_'+str(identity),status='completed',model='gpt-6-sol',
                usage=SimpleNamespace(input_tokens=10,output_tokens=2,input_tokens_details=None,output_tokens_details=None))
        client=SimpleNamespace(responses=SimpleNamespace(create=response))
        with tempfile.TemporaryDirectory() as tmp:
            recorder=ResponseRecorder(Path(tmp)/'usage.json','trading_and_support').install(client)
            async def one(role,symbol):
                with usage_actor(role,symbol):await client.responses.create(model='gpt-6-sol')
            async def both():
                await asyncio.gather(one('support_researcher','X'),one('support_reviewer','Y'))
            asyncio.run(both())
            self.assertCountEqual([(r['actor'],r['symbol']) for r in recorder.rows],
                [('support_researcher','X'),('support_reviewer','Y')])
