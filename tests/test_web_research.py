import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from web_research import enable_search, search_audit
from position_analyzer import analyze
from dashboard import decision_text


class WebResearchTests(unittest.TestCase):
    def test_switch_off_preserves_original_request(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'search.json'
            config.write_text('{"enabled": false}', encoding='utf-8')
            original = {'instructions': '只使用輸入', 'input': '{}'}
            with patch('web_research.CONFIG_PATH', config):
                self.assertEqual(enable_search(original), original)

    def test_bad_cost_limit_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'search.json'
            for limit in (0, 11, True, '3'):
                config.write_text(json.dumps({'enabled': True, 'max_tool_calls': limit}), encoding='utf-8')
                with patch('web_research.CONFIG_PATH', config), self.assertRaises(ValueError):
                    enable_search({'instructions': '測試'})

    def test_billing_uses_reported_search_requests_not_page_opens(self):
        response = {'output': [
            {'type': 'web_search_call', 'status': 'completed',
             'action': {'type': 'search', 'queries': ['公司公告'],
                        'sources': [{'url': 'https://www.twse.com.tw/a'}]}},
            {'type': 'web_search_call', 'action': {'type': 'open_page'}},
            {'type': 'message', 'content': [{'annotations': [
                {'type': 'url_citation', 'url': 'https://www.twse.com.tw/a', 'title': '公告'},
                {'type': 'url_citation', 'url': 'javascript:alert(1)'}]}]}],
            'tool_usage': {'web_search': {'num_requests': 1}}}
        audit = search_audit(response)
        self.assertEqual(audit['tool_calls'], 2)
        self.assertEqual(audit['estimated_search_tool_cost_usd'], .01)
        self.assertEqual(len(audit['sources']), 1)
        self.assertEqual(audit['sources'][0]['url'], 'https://www.twse.com.tw/a')

    def test_missing_billing_is_unknown_not_zero(self):
        self.assertIsNone(search_audit({})['estimated_search_tool_cost_usd'])

    def test_api_failure_still_returns_wait_and_error(self):
        candidate = {'symbol': '2330', 'last_price': 100, 'quote_status': 'LIVE'}
        def failed_provider(request):
            raise RuntimeError('search unavailable')
        decision, response = analyze([candidate], {'input': '{}'}, provider=failed_provider)
        self.assertEqual(response['validation'], 'ERROR')
        self.assertEqual(decision['decisions'][0]['decision'], 'WAIT')

    def test_ui_does_not_claim_search_when_no_calls(self):
        text = decision_text({'market_view': 'test'}, web_search=search_audit({}))
        self.assertIn('本輪未執行搜尋', text)
        self.assertIn('尚無計費次數資料', text)


if __name__ == '__main__':
    unittest.main()
