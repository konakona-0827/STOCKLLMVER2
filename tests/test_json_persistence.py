"""Offline regression tests for connection.json publication on Windows."""
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from main import Audit, save_json


class JsonPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'connection.json'
        self.path.write_text('{"previous": true}', encoding='utf-8')

    def assert_no_temps(self):
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_audit_recovers_from_transient_windows_locks(self):
        original = Path.replace
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                calls = []

                def replace(source, target):
                    calls.append(source)
                    if len(calls) <= 2:
                        error = OSError(errno.EACCES, 'File temporarily locked')
                        error.winerror = code
                        raise error
                    return original(source, target)

                audit = Audit(self.path.parent)
                with patch.object(Path, 'replace', replace), patch('main.time.sleep'):
                    audit.event('CAPITAL_BATCH_SUBSCRIBE', {'return_code': 0})
                self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), audit.events)
                self.assert_no_temps()

    def test_temporary_file_write_lock_is_retried(self):
        original = Path.open
        blocked = False

        def open_file(path, *args, **kwargs):
            nonlocal blocked
            if path.suffix == '.tmp' and not blocked:
                blocked = True
                raise PermissionError('Temporary file locked')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'open', open_file), patch('main.time.sleep'):
            save_json(self.path, {'recovered': True})
        self.assertEqual(json.loads(self.path.read_text()), {'recovered': True})
        self.assert_no_temps()

    def test_permanent_lock_raises_and_preserves_previous_json(self):
        with patch.object(Path, 'replace', side_effect=PermissionError('Still locked')) as replace:
            with patch('main.time.sleep'), self.assertRaises(PermissionError):
                save_json(self.path, {'new': True})
        self.assertEqual(replace.call_count, 6)
        self.assertEqual(json.loads(self.path.read_text()), {'previous': True})
        self.assert_no_temps()

    def test_unrelated_io_error_is_not_retried(self):
        with patch.object(Path, 'replace', side_effect=OSError(errno.ENOSPC, 'Disk full')):
            with patch('main.time.sleep') as sleep, self.assertRaises(OSError):
                save_json(self.path, {'new': True})
        sleep.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), {'previous': True})
        self.assert_no_temps()

    def test_concurrent_writers_publish_complete_json(self):
        barrier = threading.Barrier(2)
        original = Path.replace
        payloads = [{'writer': i, 'value': str(i) * 10000} for i in range(2)]
        sources = set()
        lock = threading.Lock()

        def replace(source, target):
            with lock:
                first_attempt = source not in sources
                sources.add(source)
                first_pair = len(sources) <= 2
            if first_attempt and first_pair:
                barrier.wait(timeout=5)
            return original(source, target)

        with patch.object(Path, 'replace', replace), ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda payload: save_json(self.path, payload), payloads))
        self.assertIn(json.loads(self.path.read_text()), payloads)
        self.assertGreaterEqual(len(sources), 2)
        self.assert_no_temps()

    def test_redaction_and_unicode_are_preserved(self):
        with patch.dict(os.environ, {'CAPITAL_PASSWORD': 'unit-test-secret'}):
            save_json(self.path, {'message': '連線 unit-test-secret'})
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),
                         {'message': '連線 [REDACTED]'})

    def test_invalid_json_does_not_replace_previous_file(self):
        with self.assertRaises(ValueError):
            save_json(self.path, {'invalid': float('nan')})
        self.assertEqual(json.loads(self.path.read_text()), {'previous': True})
        self.assert_no_temps()

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows sharing locks')
    def test_real_windows_sharing_lock_recovers(self):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                               wintypes.HANDLE)
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        # Allow reads/writes, but deny replacement until the handle closes.
        handle = create_file(str(self.path), 0x80000000, 3, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        with self.assertRaises(PermissionError):
            self.path.rename(self.path.with_name('locked.json'))
        timer = threading.Timer(0.2, lambda: close_handle(handle))
        timer.start()
        try:
            save_json(self.path, {'recovered': True})
        finally:
            timer.join()
        self.assertEqual(json.loads(self.path.read_text()), {'recovered': True})
        self.assert_no_temps()


if __name__ == '__main__':
    unittest.main()
