"""STA pumping and ownership of Automation records returned by SKCOM.

No console control callbacks, financial calls, or thread changes happen here.
"""
import ctypes
from functools import lru_cache
from types import ModuleType, SimpleNamespace
from threading import Lock

_lock=Lock()
_counters=dict(stock_records=0,bstr_freed=0,bstr_bytes_freed=0,sta_waits=0)


def counters():
    with _lock:return dict(_counters)


@lru_cache(maxsize=1)
def _api():
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    ole=ctypes.WinDLL('ole32');automation=ctypes.WinDLL('oleaut32')
    user=ctypes.WinDLL('user32')
    kernel.CreateEventW.argtypes=[ctypes.c_void_p,w.BOOL,w.BOOL,w.LPCWSTR]
    kernel.CreateEventW.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
    ole.CoWaitForMultipleHandles.argtypes=[w.DWORD,w.DWORD,w.ULONG,ctypes.POINTER(w.HANDLE),ctypes.POINTER(w.DWORD)]
    ole.CoWaitForMultipleHandles.restype=ctypes.c_long
    user.PeekMessageW.argtypes=[ctypes.POINTER(w.MSG),w.HWND,w.UINT,w.UINT,w.UINT]
    user.PeekMessageW.restype=w.BOOL
    user.TranslateMessage.argtypes=[ctypes.POINTER(w.MSG)];user.TranslateMessage.restype=w.BOOL
    user.DispatchMessageW.argtypes=[ctypes.POINTER(w.MSG)];user.DispatchMessageW.restype=ctypes.c_ssize_t
    automation.SysFreeString.argtypes=[ctypes.c_void_p];automation.SysFreeString.restype=None
    automation.SysStringByteLen.argtypes=[ctypes.c_void_p];automation.SysStringByteLen.restype=w.UINT
    return kernel,ole,user,automation


def pump_events(client,seconds):
    # Alternate clients are injectable in the offline broker harness.
    if not isinstance(client,ModuleType) or not client.__name__.startswith('comtypes'):
        return client.PumpEvents(seconds)
    from ctypes import wintypes as w
    kernel,ole,user,_=_api()
    with _lock:_counters['sta_waits']+=1
    msg=w.MSG()
    for _ in range(1000):
        if not user.PeekMessageW(ctypes.byref(msg),None,0,0,1):break
        if msg.message==0x12:raise KeyboardInterrupt  # WM_QUIT
        user.TranslateMessage(ctypes.byref(msg));user.DispatchMessageW(ctypes.byref(msg))
    event=kernel.CreateEventW(None,True,False,None)
    if not event:raise ctypes.WinError(ctypes.get_last_error())
    try:
        handles=(w.HANDLE*1)(event);index=w.DWORD()
        rc=ole.CoWaitForMultipleHandles(0,max(0,int(seconds*1000)),1,handles,ctypes.byref(index))
        if rc & 0xffffffff not in (0,0x80010115):  # RPC_S_CALLPENDING is timeout.
            raise OSError(f'COM_STA_WAIT_FAILED:0x{rc & 0xffffffff:08X}')
    finally:kernel.CloseHandle(event)


def stock_record(factory,method,*args):
    """Copy [in,out] SKSTOCKLONG to Python, then release its owned BSTRs.

    ctypes destroys a Structure without freeing BSTR fields written by COM.
    The zeroed input has no borrowed strings. COM's [in,out] Automation contract
    transfers output ownership to us; never free a Python string or COM pointer.
    Both possible comtypes return shapes are handled, including error paths.
    """
    original=factory();records=[original]
    try:
        result=method(*args,original)
        from capital_check import unpack
        if isinstance(result,(tuple,list)):
            records.extend(v for v in result if hasattr(v,'nClose'))
        rc=unpack(result)
        if rc:raise ValueError('QUOTE_RETURN_CODE_'+str(rc))
        stock=records[-1]
        if not isinstance(stock,ctypes.Structure):return stock
        return SimpleNamespace(**{field[0]:getattr(stock,field[0]) for field in stock._fields_})
    finally:
        pointers=set()
        for record in records:
            if not isinstance(record,ctypes.Structure):continue
            for name,kind,*_ in record._fields_:
                if getattr(kind,'_type_',None)!='X':continue  # BSTR only.
                slot=ctypes.c_void_p.from_address(ctypes.addressof(record)+getattr(type(record),name).offset)
                if slot.value:pointers.add(slot.value);slot.value=None
        if pointers:
            automation=_api()[3];size=0
            for pointer in pointers:
                size+=automation.SysStringByteLen(pointer)
                automation.SysFreeString(pointer)
            with _lock:
                _counters['bstr_freed']+=len(pointers)
                _counters['bstr_bytes_freed']+=size
        with _lock:_counters['stock_records']+=1
