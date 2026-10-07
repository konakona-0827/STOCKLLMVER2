from __future__ import annotations
import os, sys, time, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def load_env():
    envs = list(ROOT.glob("*.env"))
    if not envs:
        raise SystemExit("No *.env file found in this folder.")
    p = envs[0]
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    print(f"[ENV] {p.name}")

def find_skcom():
    explicit = os.getenv("CAPITAL_COM_DLL", "").strip()
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise SystemExit(f"CAPITAL_COM_DLL not found: {p}")

    # Expected layout:
    # ...\nisa\掛單測試\this_script.py
    # ...\nisa\群益API\...\元件\x64\SKCOM.dll
    parent = ROOT.parent
    hits = list((parent / "群益API").glob("**/x64/SKCOM.dll"))
    if not hits:
        raise SystemExit("Cannot find SKCOM.dll under sibling folder 群益API.")
    return hits[0]

load_env()

USER = os.getenv("CAPITAL_USER_ID", "").strip()
PWD = os.getenv("CAPITAL_PASSWORD", "").strip()
ACCOUNT = os.getenv("CAPITAL_ACCOUNT", "").strip()
SYMBOL = os.getenv("CAPITAL_SYMBOL", "0050").strip()
PRICE = os.getenv("CAPITAL_TEST_PRICE", "115").strip()
QTY = int(os.getenv("CAPITAL_QTY", "1"))
SIDE_TEXT = os.getenv("CAPITAL_SIDE", "BUY").strip().upper()
SIDE = 0 if SIDE_TEXT == "BUY" else 1

if not USER or not PWD:
    raise SystemExit("Missing CAPITAL_USER_ID or CAPITAL_PASSWORD in env.")
if SIDE_TEXT not in ("BUY", "SELL"):
    raise SystemExit("CAPITAL_SIDE must be BUY or SELL.")
if not (1 <= QTY <= 999):
    raise SystemExit("CAPITAL_QTY must be 1..999.")

DLL = find_skcom()
print(f"[DLL] {DLL}")
print(f"[ORDER] {SIDE_TEXT} {SYMBOL} x {QTY} @ {PRICE}")

confirm = input("Type SEND to submit this REAL order: ").strip()
if confirm != "SEND":
    print("Cancelled. No order sent.")
    raise SystemExit(0)

import comtypes.client
comtypes.client.GetModule(str(DLL))
import comtypes.gen.SKCOMLib as sk

skC = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
skO = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)
skR = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)

accounts = []
reply_complete = False

class ReplyEvents:
    def OnReplyMessage(self, bstrUserID, bstrMessage):
        # comtypes removes the [out] SHORT* argument from the Python callback
        # signature. Returning -1 supplies the required confirmation value.
        print("[ANN]", bstrMessage)
        return -1

    def OnComplete(self, bstrUserID):
        global reply_complete
        reply_complete = True
        print("[REPLAY COMPLETE]")

    def OnNewData(self, bstrUserID, bstrData):
        print("[OnNewData]", bstrData)

    def OnReplyClear(self, bstrMarket):
        print("[ReplyClear]", bstrMarket)

class OrderEvents:
    def OnAccount(self, bstrLogInID, bstrAccountData):
        raw = str(bstrAccountData)
        print("[ACCOUNT RAW]", raw)
        p = raw.split(",")
        if len(p) >= 4 and p[0].strip() == "TS":
            acct = p[1].strip() + p[3].strip()
            if acct and acct not in accounts:
                accounts.append(acct)

reply_conn = comtypes.client.GetEvents(skR, ReplyEvents())
order_conn = comtypes.client.GetEvents(skO, OrderEvents())

rc = skC.SKCenterLib_Login(USER, PWD)
print("[LOGIN]", rc)
if rc != 0:
    try:
        print("[LOGIN MESSAGE]", skC.SKCenterLib_GetReturnCodeMessage(rc))
        print("[LAST LOG]", skC.SKCenterLib_GetLastLogInfo())
    except Exception:
        pass
    raise SystemExit(f"Login failed: {rc}")

rc = skO.SKOrderLib_Initialize()
print("[INIT]", rc)
if rc != 0:
    raise SystemExit(f"Initialize failed: {rc}")

rc = skO.ReadCertByID(USER)
print("[CERT]", rc)
if rc != 0:
    raise SystemExit(f"ReadCertByID failed: {rc}")

rc = skO.GetUserAccount()
print("[GET ACCOUNT]", rc)
if rc != 0:
    raise SystemExit(f"GetUserAccount failed: {rc}")

end = time.time() + 8
while time.time() < end and not accounts:
    comtypes.client.PumpEvents(0.25)

if ACCOUNT:
    account = ACCOUNT
elif len(accounts) == 1:
    account = accounts[0]
elif len(accounts) == 0:
    raise SystemExit("No TS stock account received. Add CAPITAL_ACCOUNT to env.")
else:
    raise SystemExit("Multiple TS accounts found. Add CAPITAL_ACCOUNT to env: " + ",".join(accounts))

print("[ACCOUNT]", account)

rc = skR.SKReplyLib_ConnectByID(USER)
print("[REPLY CONNECT]", rc)
if rc != 0:
    raise SystemExit(f"Reply connect failed: {rc}")

end = time.time() + 15
while time.time() < end and not reply_complete:
    comtypes.client.PumpEvents(0.25)

if not reply_complete:
    raise SystemExit("No OnComplete received; no order sent.")

o = sk.STOCKORDER()
o.bstrFullAccount = account
o.bstrStockNo = SYMBOL
o.sPeriod = 4
o.sFlag = 0
o.sBuySell = SIDE
o.bstrPrice = PRICE
o.nQty = QTY

if hasattr(o, "sPrime"):
    o.sPrime = 0
if hasattr(o, "nTradeType"):
    o.nTradeType = 0
if hasattr(o, "nSpecialTradeType"):
    o.nSpecialTradeType = 2

res = skO.SendStockOddLotOrder(USER, False, o)
print("[SEND RAW]", res)

msg = ""
rc = None
if isinstance(res, tuple):
    if len(res) >= 2:
        msg, rc = res[0], res[-1]
else:
    rc = res

print("[SEND]", "rc=", rc, "message=", msg)
m = re.search(r"\d{13}", str(msg))
if m:
    print("[SEQ13]", m.group(0))

print("[WAIT] listening for broker replies for 20 seconds...")
end = time.time() + 20
while time.time() < end:
    comtypes.client.PumpEvents(0.25)

print("[DONE] This script does NOT cancel the order.")
