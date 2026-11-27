"""Windows venv launchers must not hide the actual heartbeat/resource PID."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv
from unittest.mock import patch

from operations.flow_supervisor import owned_python_process


class PythonOwnerTests(unittest.TestCase):
    def test_unrelated_program_and_normal_python_keep_command_and_environment(self):
        env={'TEST_MARKER':'unchanged'};command=['arbitrary-tool','--read-only']
        result=owned_python_process(command,env)
        self.assertIs(result[0],command);self.assertIs(result[1],env)
        with patch('operations.flow_supervisor.sys.prefix','same'),patch('operations.flow_supervisor.sys.base_prefix','same'):
            result=owned_python_process([sys.executable,'-c','pass'],env)
            self.assertIs(result[1],env)

    @unittest.skipUnless(os.name=='nt','Windows venv redirector')
    def test_real_venv_process_pid_matches_popen_and_retains_isolated_packages(self):
        with tempfile.TemporaryDirectory(prefix='flow venv owner ') as tmp:
            folder=Path(tmp)/'venv';venv.EnvBuilder(with_pip=False).create(folder)
            (folder/'Lib/site-packages/flow_isolated_dependency.py').write_text('VALUE=731\n',encoding='utf-8')
            script='''import json,subprocess,sys,os
from operations.flow_supervisor import owned_python_process
code="import os,sys,json,flow_isolated_dependency as d; print(json.dumps(dict(pid=os.getpid(),prefix=sys.prefix,dependency=d.VALUE)))"
command,env=owned_python_process([sys.executable,'-c',code],dict(os.environ))
p=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
out,err=p.communicate(timeout=10)
if p.returncode:raise RuntimeError(err.decode())
r=json.loads(out);r.update(expected_pid=p.pid,expected_prefix=sys.prefix)
print(json.dumps(r))
'''
            result=subprocess.run([str(folder/'Scripts/python.exe'),'-c',script],cwd=Path(__file__).resolve().parents[1],
                capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            value=json.loads(result.stdout)
            self.assertEqual(value['pid'],value['expected_pid'])
            self.assertEqual(value['prefix'],value['expected_prefix'])
            self.assertEqual(value['dependency'],731)


if __name__=='__main__':unittest.main()
