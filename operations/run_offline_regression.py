"""Run regression without external network, real models, broker or shared state."""
import argparse
import contextlib
from pathlib import Path
import socket
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
for name in ('connect','connect_ex'):
    original=getattr(socket.socket,name)
    def guarded(sock,address,original=original):
        if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1','localhost'):
            return original(sock,address)
        raise RuntimeError('OFFLINE_REGRESSION_EXTERNAL_NETWORK_DISABLED')
    setattr(socket.socket,name,guarded)

def audit(event,args):
    if event=='subprocess.Popen' and 'codex' in str(args[0]).lower():
        raise RuntimeError('OFFLINE_REGRESSION_REAL_MODEL_DISABLED')

sys.addaudithook(audit)
parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--pattern',default='test*.py')
args=parser.parse_args()
args.output.parent.mkdir(parents=True,exist_ok=True)
with args.output.open('w',encoding='utf-8') as stream,contextlib.redirect_stdout(stream),contextlib.redirect_stderr(stream):
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=args.pattern)
    result=unittest.TextTestRunner(stream=stream,verbosity=1).run(suite)
print('tests',result.testsRun,'failures',len(result.failures),'errors',len(result.errors),'skipped',len(result.skipped))
for test,trace in result.failures+result.errors:print(str(test),trace[-1800:])
sys.exit(not result.wasSuccessful())
