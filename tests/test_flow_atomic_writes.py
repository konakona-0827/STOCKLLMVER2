"""Windows readers can deny rename; retry the file replacement, never the trade."""
import ctypes
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opportunity_flow.common import atomic_write_text,read,save


def sharing_error(code=5):
    error=PermissionError(errno.EACCES,'isolated Windows contention')
    error.winerror=code
    return error


class AtomicWriteTests(unittest.TestCase):
    def test_transient_windows_errors_keep_old_json_until_complete_replacement(self):
        for code in (5,32,33):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'state.json';save(path,dict(version='old'))
                original=Path.replace;attempts=[]
                def replace(source,target):
                    attempts.append(source)
                    self.assertEqual(read(path),dict(version='old'))
                    if len(attempts)<3:raise sharing_error(code)
                    return original(source,target)
                with patch.object(Path,'replace',replace),patch('opportunity_flow.common.time.sleep') as backoff:
                    save(path,dict(version='new',complete=True))
                self.assertEqual(read(path),dict(version='new',complete=True))
                self.assertEqual(backoff.call_count,2)
                self.assertEqual(len(set(attempts)),1)
                self.assertFalse(list(Path(tmp).glob('*.tmp')))

    def test_persistent_denial_is_bounded_preserves_old_json_and_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';save(path,dict(version='old'));error=sharing_error()
            with patch.object(Path,'replace',side_effect=error) as replace,\
                 patch('opportunity_flow.common.time.sleep') as backoff:
                with self.assertRaises(PermissionError) as caught:save(path,dict(version='new'))
            self.assertIs(caught.exception,error)
            self.assertEqual(replace.call_count,4);self.assertEqual(backoff.call_count,3)
            self.assertEqual(read(path),dict(version='old'))
            self.assertFalse(list(Path(tmp).glob('*.tmp')))

    def test_unrelated_disk_failure_is_not_retried_or_hidden_by_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.txt';atomic_write_text(path,'old')
            error=OSError(errno.ENOSPC,'isolated disk full')
            with patch.object(Path,'replace',side_effect=error),\
                 patch.object(Path,'unlink',side_effect=PermissionError('cleanup denied')),\
                 patch('opportunity_flow.common.time.sleep') as backoff:
                with self.assertRaises(OSError) as caught:atomic_write_text(path,'new')
            self.assertIs(caught.exception,error);backoff.assert_not_called()
            self.assertEqual(path.read_text(),'old')

    def test_invalid_financial_numbers_still_fail_without_replacing_good_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';save(path,dict(budget=9534))
            with self.assertRaises(ValueError):save(path,dict(budget=float('nan')))
            self.assertEqual(read(path),dict(budget=9534))
            self.assertFalse(list(Path(tmp).glob('*.tmp')))


@unittest.skipUnless(os.name=='nt','Native Windows file sharing')
class NativeSharingTests(unittest.TestCase):
    def hold_without_delete_sharing(self,path):
        from ctypes import wintypes
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,
                                 wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        api.CreateFileW.restype=wintypes.HANDLE
        api.CloseHandle.argtypes=[wintypes.HANDLE];api.CloseHandle.restype=wintypes.BOOL
        handle=api.CreateFileW(str(path),0x80000000,3,None,3,0,None)
        if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
        return api,handle

    def test_real_windows_reader_lock_recovers_after_reader_releases(self):
        with tempfile.TemporaryDirectory(prefix='flow native atomic ') as tmp:
            path=Path(tmp)/'heartbeat.json';save(path,dict(version='old'))
            api,handle=self.hold_without_delete_sharing(path);closed=[];errors=[];replace=Path.replace
            def observed(source,target):
                try:return replace(source,target)
                except OSError as exc:errors.append(getattr(exc,'winerror',None));raise
            def release(seconds):
                if not closed:
                    self.assertTrue(api.CloseHandle(handle));closed.append(True)
            try:
                with patch.object(Path,'replace',observed),patch('opportunity_flow.common.time.sleep',side_effect=release):
                    save(path,dict(version='new',complete=True))
            finally:
                if not closed:api.CloseHandle(handle)
            self.assertTrue(errors);self.assertTrue(all(code in (5,32,33) for code in errors))
            self.assertEqual(read(path),dict(version='new',complete=True))
            self.assertFalse(list(Path(tmp).glob('*.tmp')))

    def test_real_persistent_reader_lock_never_truncates_human_report(self):
        with tempfile.TemporaryDirectory(prefix='flow native atomic ') as tmp:
            path=Path(tmp)/'latest.txt';atomic_write_text(path,'complete old report')
            api,handle=self.hold_without_delete_sharing(path)
            try:
                with self.assertRaises(PermissionError):atomic_write_text(path,'new report')
                self.assertEqual(path.read_text(),'complete old report')
            finally:api.CloseHandle(handle)
            self.assertFalse(list(Path(tmp).glob('*.tmp')))


if __name__=='__main__':unittest.main()
