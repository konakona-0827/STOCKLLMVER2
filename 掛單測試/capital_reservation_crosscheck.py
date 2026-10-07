"""
Capital API read-only cross-check for current reserved orders.

What it does:
1) Load *.env from this folder.
2) Login / certificate / stock account.
3) Connect SKReplyLib and wait for OnComplete (replay/backfill complete).
4) Collect TC (intraday odd-lot) OnNewData rows from replay.
5) Call GetOrderReport(..., 9) for reserved-order query.
6) Compare an optional expected 13-digit sequence locally.

IMPORTANT:
- READ ONLY: this script does NOT submit, modify, or cancel any order.
- Official Capital API documentation says GetOrderReport excludes intraday odd-lot (TC),
  so a TC reservation may be absent from GetOrderReport(9) while still being present
  in SKReplyLib replay.
"""

from __future__ import annotations
import argparse
import os
import re
import time
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

    hits = list((ROOT.parent / "群益API").glob("**/CapitalAPI_2.13.59/**/x64/SKCOM.dll"))
    if not hits:
        hits = list((ROOT.parent / "群益API").glob("**/x64/SKCOM.dll"))
    if not hits:
        raise SystemExit("Cannot find SKCOM.dll under sibling folder 群益API.")
    return hits[0]


def redact_row(raw: str) -> str:
    """Redact obvious account/order identifiers while keeping market/type/status evidence."""
    parts = raw.split(",")
    if len(parts) > 0 and parts[0]:
        parts[0] = "***SEQ13***"
    if len(parts) > 4 and parts[4]:
        parts[4] = "***BROKER***"
    if len(parts) > 5 and parts[5]:
        parts[5] = "***ACCOUNT***"
    # There can be another 13-digit sequence near the end.
    for i, v in enumerate(parts):
        if re.fullmatch(r"\d{13}", v or ""):
            parts[i] = "***SEQ13***"
    return ",".join(parts)


def contains_seq(text: str, seq: str | None) -> bool | None:
    if not seq:
        return None
    return seq in (text or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seq", help="expected 13-digit order sequence; checked locally only")
    ap.add_argument("--raw", action="store_true", help="print unredacted raw broker rows")
    ap.add_argument("--replay-wait", type=float, default=3.0,
                    help="extra seconds to collect replay events after OnComplete")
    args = ap.parse_args()

    load_env()

    user = os.getenv("CAPITAL_USER_ID", "").strip()
    pwd = os.getenv("CAPITAL_PASSWORD", "").strip()
    account_override = os.getenv("CAPITAL_ACCOUNT", "").strip()
    expected_seq = (args.seq or os.getenv("CAPITAL_EXPECT_SEQ", "").strip() or None)

    if not user or not pwd:
        raise SystemExit("Missing CAPITAL_USER_ID or CAPITAL_PASSWORD in env.")
    if expected_seq and not re.fullmatch(r"\d{13}", expected_seq):
        raise SystemExit("--seq / CAPITAL_EXPECT_SEQ must be exactly 13 digits.")

    dll = find_skcom()
    print(f"[DLL] {dll}")
    print("[MODE] READ ONLY - no order will be sent/cancelled/modified")

    import comtypes.client
    comtypes.client.GetModule(str(dll))
    import comtypes.gen.SKCOMLib as sk

    skC = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
    skO = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)
    skR = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)

    state = {
        "reply_complete": False,
        "all_rows": [],
        "tc_rows": [],
        "accounts": [],
    }

    class ReplyEvents:
        def OnReplyMessage(self, bstrUserID, bstrMessage):
            return -1

        def OnComplete(self, bstrUserID):
            state["reply_complete"] = True
            print("[REPLAY COMPLETE]")

        def OnNewData(self, bstrUserID, bstrData):
            raw = str(bstrData)
            state["all_rows"].append(raw)
            parts = raw.split(",")
            if len(parts) >= 4 and parts[1] == "TC":
                state["tc_rows"].append(raw)

        def OnReplyClear(self, bstrMarket):
            pass

    class OrderEvents:
        def OnAccount(self, bstrLogInID, bstrAccountData):
            raw = str(bstrAccountData)
            p = raw.split(",")
            if len(p) >= 4 and p[0].strip() == "TS":
                acct = p[1].strip() + p[3].strip()
                if acct and acct not in state["accounts"]:
                    state["accounts"].append(acct)

    reply_conn = comtypes.client.GetEvents(skR, ReplyEvents())
    order_conn = comtypes.client.GetEvents(skO, OrderEvents())
    _keepalive = (reply_conn, order_conn)

    rc = skC.SKCenterLib_Login(user, pwd)
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

    rc = skO.ReadCertByID(user)
    print("[CERT]", rc)
    if rc != 0:
        raise SystemExit(f"ReadCertByID failed: {rc}")

    rc = skO.GetUserAccount()
    print("[GET ACCOUNT]", rc)
    if rc != 0:
        raise SystemExit(f"GetUserAccount failed: {rc}")

    end = time.time() + 8
    while time.time() < end and not state["accounts"] and not account_override:
        comtypes.client.PumpEvents(0.25)

    if account_override:
        account = account_override
    elif len(state["accounts"]) == 1:
        account = state["accounts"][0]
    elif len(state["accounts"]) == 0:
        raise SystemExit("No TS account received. Add CAPITAL_ACCOUNT to env.")
    else:
        raise SystemExit("Multiple TS accounts found. Add CAPITAL_ACCOUNT to env.")

    print("[ACCOUNT] selected (redacted)")

    rc = skR.SKReplyLib_ConnectByID(user)
    print("[REPLY CONNECT]", rc)
    if rc != 0:
        raise SystemExit(f"Reply connect failed: {rc}")

    end = time.time() + 20
    while time.time() < end and not state["reply_complete"]:
        comtypes.client.PumpEvents(0.25)

    if not state["reply_complete"]:
        raise SystemExit("No OnComplete received; replay/backfill is not trustworthy.")

    # Small grace period for any queued callbacks.
    end = time.time() + max(0.0, args.replay_wait)
    while time.time() < end:
        comtypes.client.PumpEvents(0.25)

    # Official reservation query. Documentation says this query excludes TC intraday odd-lot.
    try:
        reservation_report = skO.GetOrderReport(user, account, 9)
    except Exception as e:
        reservation_report = f"<GetOrderReport exception: {type(e).__name__}: {e}>"

    reservation_text = str(reservation_report or "")

    tc_normal_new = []
    tc_cancel = []
    tc_fill = []
    for raw in state["tc_rows"]:
        p = raw.split(",")
        if len(p) < 4:
            continue
        typ = p[2]
        err = p[3]
        if typ == "N" and err == "N":
            tc_normal_new.append(raw)
        elif typ == "C":
            tc_cancel.append(raw)
        elif typ == "D":
            tc_fill.append(raw)

    print()
    print("========== CROSS-CHECK SUMMARY ==========")
    print("REPLAY_COMPLETE=True")
    print(f"TC_EVENT_COUNT={len(state['tc_rows'])}")
    print(f"TC_NORMAL_ORDER_COUNT={len(tc_normal_new)}")
    print(f"TC_CANCEL_EVENT_COUNT={len(tc_cancel)}")
    print(f"TC_FILL_EVENT_COUNT={len(tc_fill)}")

    # GetOrderReport(9) visibility
    short_report = reservation_text.replace("\r", "\\r").replace("\n", "\\n")
    if len(short_report) > 500 and not args.raw:
        short_report = short_report[:500] + "...<truncated>"
    print(f"GET_ORDER_REPORT_FORMAT9={short_report}")

    if expected_seq:
        found_replay = any(expected_seq in row for row in state["tc_rows"])
        found_tc_normal = any(expected_seq in row for row in tc_normal_new)
        found_report9 = expected_seq in reservation_text
        print(f"EXPECTED_SEQ_FOUND_IN_TC_REPLAY={found_replay}")
        print(f"EXPECTED_SEQ_FOUND_AS_TC_NORMAL_ORDER={found_tc_normal}")
        print(f"EXPECTED_SEQ_FOUND_IN_GETORDERREPORT9={found_report9}")
    else:
        print("EXPECTED_SEQ_CHECK=SKIPPED (use --seq <13-digit-seq> or CAPITAL_EXPECT_SEQ)")

    print()
    print("TC_REPLAY_ROWS:")
    if not state["tc_rows"]:
        print("  <none>")
    else:
        for raw in state["tc_rows"]:
            print("  " + (raw if args.raw else redact_row(raw)))

    print()
    print("INTERPRETATION:")
    print("- TC = intraday odd-lot.")
    print("- Type N + OrderErr N = normal order report.")
    print("- GetOrderReport(format=9) is the broker reservation-query API,")
    print("  but official documentation states GetOrderReport excludes intraday odd-lot.")
    print("- Therefore for TC, SKReplyLib replay/backfill through OnComplete is the")
    print("  documented path to reconstruct the order event stream.")
    print("=========================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
