"""Isolated Windows Automation allocator probe. Never logs in or creates SKCOM.

The legacy case deliberately reproduces unfreed [in,out] record strings only
inside this disposable process; use a different process for the fixed case.
"""
import argparse
import ctypes
import gc
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def run(mode,iterations):
    import comtypes
    import comtypes.client
    from execution.com_lifetime import stock_record,pump_events,_api
    from operations.flow_resources import sample_memory
    class Stock(ctypes.Structure):
        _fields_=[('bstrStockNo',comtypes.BSTR),('bstrStockName',comtypes.BSTR),('nClose',ctypes.c_long)]
    automation=_api()[3]
    automation.SysAllocString.argtypes=[ctypes.c_wchar_p];automation.SysAllocString.restype=ctypes.c_void_p
    payload='allocator-probe-'+'x'*2033
    def output(record):
        for name in ('bstrStockNo','bstrStockName'):
            ptr=automation.SysAllocString(payload)
            if not ptr:raise MemoryError('SysAllocString failed')
            ctypes.c_void_p.from_address(ctypes.addressof(record)+getattr(Stock,name).offset).value=ptr
        record.nClose=123
        return record,0
    samples=[];start=time.monotonic()
    for i in range(iterations):
        if mode=='legacy':
            original=Stock();value=output(original)[0];symbol=value.bstrStockNo
        elif mode=='fixed':
            value=stock_record(Stock,output);assert value.bstrStockNo==payload and value.nClose==123
        elif mode=='pump':pump_events(comtypes.client,0)
        if i in (0,iterations//2,iterations-1):
            gc.collect();samples.append(dict(iteration=i+1,**sample_memory(os.getpid())))
    return dict(mode=mode,iterations=iterations,payload_chars=len(payload),samples=samples,
        elapsed_seconds=round(time.monotonic()-start,3),python=sys.version.split()[0],
        scope='WINDOWS_AUTOMATION_ALLOCATOR_ONLY_NO_BROKER_NO_MODEL_NO_ORDER',
        limitation='Synthetic COM-contract output; does not measure live SKCOM allocation sizes or certify the live growth rate.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('legacy','fixed','pump'),required=True)
    parser.add_argument('--iterations',type=int,default=10000)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not 1<=args.iterations<=100000:parser.error('iterations must be 1..100000')
    result=run(args.mode,args.iterations)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result))
