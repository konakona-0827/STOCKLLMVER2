"""Closing references are valuation evidence, never live execution quotes."""
from datetime import datetime
import unittest
from unittest.mock import patch
from config import TAIPEI
from dashboard import current_position_quotes,display_position_quotes


class ClosingPriceLabelTests(unittest.TestCase):
    def test_close_has_a_visible_date_and_cannot_pass_the_live_price_filter(self):
        q=dict(symbol='0050',name='reference',quote_status='DAILY_CLOSE_REFERENCE',
            quote_basis='OFFICIAL_DAILY_CLOSE_NOT_EXECUTABLE',display_only=True,execution_usable=False,
            price_as_of='2026-11-12',last_price=80,quote_timestamp=None,bid=None,ask=None)
        result=dict(batch=dict(quotes=[q]))
        with patch('dashboard.now',return_value=datetime(2026,11,13,17,tzinfo=TAIPEI)):
            self.assertEqual(current_position_quotes({},result),{})
            rows=display_position_quotes({},result)
            self.assertEqual(rows['0050'][1],80);self.assertEqual(rows['0050'][3],'2026-11-12 收盤參考＊')
            q['price_as_of']='2026-11-14';self.assertEqual(display_position_quotes({},result),{})


if __name__=='__main__':unittest.main()
