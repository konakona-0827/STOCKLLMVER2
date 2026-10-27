from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from execution.capital_reply_session import (
    CapitalReplySessionError,
    ReplySnapshot,
    find_default_env,
    find_skcom_dll,
    load_env_file,
)


class CapitalReplySessionOfflineTests(unittest.TestCase):
    def test_snapshot_copy_is_independent(self):
        original = ReplySnapshot(
            replay_complete=True,
            all_rows=["A"],
            tc_rows=["T"],
            accounts=["ACC"],
            announcements=["M"],
        )
        cloned = original.copy()
        cloned.tc_rows.append("T2")
        self.assertEqual(["T"], original.tc_rows)
        self.assertEqual(["T", "T2"], cloned.tc_rows)

    def test_load_env_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env"
            p.write_text("ABC_TEST_KEY=hello\n# comment\n", encoding="utf-8")
            os.environ.pop("ABC_TEST_KEY", None)
            load_env_file(p)
            self.assertEqual("hello", os.environ["ABC_TEST_KEY"])
            os.environ.pop("ABC_TEST_KEY", None)

    def test_find_default_env_prefers_project_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "掛單測試").mkdir()
            (root / ".env").write_text("X=1", encoding="utf-8")
            (root / "掛單測試" / ".env").write_text("X=2", encoding="utf-8")
            self.assertEqual(root / ".env", find_default_env(root))

    def test_find_default_env_falls_back_to_order_test_folder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / "掛單測試"
            folder.mkdir()
            env = folder / ".env"
            env.write_text("X=2", encoding="utf-8")
            self.assertEqual(env, find_default_env(root))

    def test_find_skcom_dll_current_layout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dll = (
                root
                / "CapitalAPI_2.13.59"
                / "CapitalAPI_2.13.59"
                / "CapitalAPI_2.13.59"
                / "元件"
                / "x64"
                / "SKCOM.dll"
            )
            dll.parent.mkdir(parents=True)
            dll.write_bytes(b"fake")

            old = os.environ.pop("CAPITAL_COM_DLL", None)
            try:
                self.assertEqual(dll, find_skcom_dll(root))
            finally:
                if old is not None:
                    os.environ["CAPITAL_COM_DLL"] = old

    def test_explicit_missing_dll_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("CAPITAL_COM_DLL")
            os.environ["CAPITAL_COM_DLL"] = str(Path(td) / "missing.dll")
            try:
                with self.assertRaises(CapitalReplySessionError):
                    find_skcom_dll(td)
            finally:
                if old is None:
                    os.environ.pop("CAPITAL_COM_DLL", None)
                else:
                    os.environ["CAPITAL_COM_DLL"] = old


if __name__ == "__main__":
    unittest.main()
