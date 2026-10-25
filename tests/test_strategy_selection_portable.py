"""The saved live strategy must work after moving the code checkout."""
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import strategy_catalog


class LiveStrategySelectionTests(unittest.TestCase):
    def test_saved_relative_strategy_resolves_from_code_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            code = Path(directory) / 'code'
            selected = code / 'strategies' / 'selected.md'
            selected.parent.mkdir(parents=True)
            selected.write_text('可攜策略', encoding='utf-8')
            selection_file = Path(directory) / 'state' / 'live_strategy.json'
            selection_file.parent.mkdir()
            selection_file.write_text(json.dumps({'path': 'strategies/selected.md'}), encoding='utf-8')
            with patch.object(strategy_catalog, 'CODE_ROOT', code), \
                    patch.object(strategy_catalog, 'LIVE_SELECTION', selection_file), \
                    patch.dict(os.environ, {}, clear=False):
                os.environ.pop('TRADING_STRATEGY_PATH', None)
                strategy = strategy_catalog.live_selection()
            self.assertEqual(Path(strategy['path']), selected)
            self.assertEqual(strategy['prompt'], '可攜策略')

    def test_explicit_override_uses_requested_file_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / 'selected.md'
            selected.write_text('可核對的測試策略', encoding='utf-8')
            with patch.dict(os.environ, {'TRADING_STRATEGY_PATH': str(selected)}):
                self.assertEqual(strategy_catalog.live_selection()['prompt'], '可核對的測試策略')
            with patch.dict(os.environ, {'TRADING_STRATEGY_PATH': str(selected.with_name('missing.md'))}):
                with self.assertRaises(ValueError):
                    strategy_catalog.live_selection()

    def test_saved_selection_supersedes_startup_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root/'first.md', root/'second.md'
            first.write_text('Startup strategy', encoding='utf-8')
            second.write_text('User selected strategy', encoding='utf-8')
            with patch.object(strategy_catalog, 'LIVE_SELECTION', root/'selection.json'), \
                    patch.dict(os.environ, {'TRADING_STRATEGY_PATH': str(first)}):
                strategy_catalog.save_live_selection(second)
                self.assertEqual(strategy_catalog.live_selection()['path'], str(second))


if __name__ == '__main__':
    unittest.main()
