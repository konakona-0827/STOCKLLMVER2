"""Bounded, read-only resource sampling; recovery uses the original graceful handoff."""
import ctypes
import os
import time
from functools import lru_cache

MIB=1024**2
PRIVATE_LIMIT=2*1024**3


@lru_cache(maxsize=1)
def _windows_api():
    from ctypes import wintypes as w
    class ProcessMemory(ctypes.Structure):
        _fields_=[('cb',w.DWORD),('faults',w.DWORD)]+[(key,ctypes.c_size_t) for key in
            ('peak_working','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile','private')]
    class SystemMemory(ctypes.Structure):
        _fields_=[('length',w.DWORD),('load',w.DWORD)]+[(key,ctypes.c_ulonglong) for key in
            ('total_physical','available_physical','total_commit','available_commit','total_virtual','available_virtual','extended')]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);api=ctypes.WinDLL('psapi',use_last_error=True)
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
    kernel.GlobalMemoryStatusEx.argtypes=[ctypes.POINTER(SystemMemory)];kernel.GlobalMemoryStatusEx.restype=w.BOOL
    api.GetProcessMemoryInfo.argtypes=[w.HANDLE,ctypes.POINTER(ProcessMemory),w.DWORD];api.GetProcessMemoryInfo.restype=w.BOOL
    return kernel,api,ProcessMemory,SystemMemory


def sample_memory(pid):
    if os.name!='nt':return None
    kernel,api,ProcessMemory,SystemMemory=_windows_api()
    handle=kernel.OpenProcess(0x1000|0x0010,False,int(pid))
    if not handle:return None
    try:
        process=ProcessMemory();process.cb=ctypes.sizeof(process)
        system=SystemMemory();system.length=ctypes.sizeof(system)
        if not api.GetProcessMemoryInfo(handle,ctypes.byref(process),process.cb):return None
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(system)):return None
        return dict(private_commit_bytes=int(process.private),working_set_bytes=int(process.working),
            available_commit_bytes=int(system.available_commit),available_physical_bytes=int(system.available_physical),
            total_physical_bytes=int(system.total_physical),system_memory_load_pct=int(system.load))
    finally:kernel.CloseHandle(handle)


def pressure_reason(row):
    if not isinstance(row,dict):return None
    private=row.get('private_commit_bytes');available=row.get('available_commit_bytes')
    if type(private) is int and private>=PRIVATE_LIMIT:return 'PROCESS_MEMORY_BUDGET_EXCEEDED'
    if type(available) is int and 0<=available<256*MIB:return 'SYSTEM_COMMIT_CRITICAL'
    return None


class MemoryGuard:
    def __init__(self,clock=time.monotonic,sampler=None):
        self.clock,self.sampler=clock,sampler or sample_memory;self.checked=-float('inf');self.row=None
    def check(self,pid):
        moment=self.clock()
        if moment-self.checked>=15:
            self.checked=moment
            try:self.row=self.sampler(pid)
            except (OSError,ValueError,TypeError):self.row=None
        return pressure_reason(self.row)
