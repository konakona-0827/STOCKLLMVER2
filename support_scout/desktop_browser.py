"""Open the local desk in Google Chrome, never an embedded/default browser."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .contracts import DataError


def find_chrome():
    candidates=[]
    if sys.platform=='win32':
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive,r'Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe') as key:
                    candidates.append(os.path.expandvars(winreg.QueryValue(key,None).strip('"')))
            except OSError:pass
        for name in ('PROGRAMFILES','PROGRAMFILES(X86)','LOCALAPPDATA'):
            if os.environ.get(name):candidates.append(str(Path(os.environ[name])/'Google/Chrome/Application/chrome.exe'))
    elif sys.platform=='darwin':
        candidates.append('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    candidates.extend(path for name in ('google-chrome','google-chrome-stable','chrome') if (path:=shutil.which(name)))
    return next((str(Path(path)) for path in candidates if Path(path).is_file()),None)


def open_dashboard(url):
    chrome=find_chrome()
    if not chrome:
        raise DataError('找不到Google Chrome。請先安裝Chrome，或自行在Chrome開啟：'+url)
    subprocess.Popen([chrome,url],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
