"""Run isolated regression tests with external sockets and live credentials disabled."""
import argparse
import os
from pathlib import Path
import socket
import sys
import unittest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code-root',type=Path,required=True)
    parser.add_argument('--pattern',default='test*.py');args=parser.parse_args()
    root=args.code_root.resolve()
    if (root/'.env').exists() or (root/'.trading-state.json').exists():
        raise ValueError('OFFLINE_TEST_ROOT_CONTAINS_LIVE_BINDING')
    for key in ('OPENAI_API_KEY','OPENAI_BASE_URL','OPENAI_PROJECT_ID','OPENAI_ORG_ID','TRADING_STATE_ROOT',
                'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
        os.environ.pop(key,None)
    original_connect=socket.socket.connect;original_connect_ex=socket.socket.connect_ex
    def connect(sock,address):
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1','localhost'):
            raise OSError('OFFLINE_TEST_EXTERNAL_CONNECTION_BLOCKED')
        return original_connect(sock,address)
    def connect_ex(sock,address):
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1','localhost'):
            raise OSError('OFFLINE_TEST_EXTERNAL_CONNECTION_BLOCKED')
        return original_connect_ex(sock,address)
    socket.socket.connect=connect;socket.socket.connect_ex=connect_ex
    os.chdir(root);sys.path[:0]=[str(root),str(root/'tests')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(
        str(root/'tests'),pattern=args.pattern))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
