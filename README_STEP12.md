# Step 12 — Capital session health check before every BUY/SELL

## Goal

Avoid duplicate login and detect reply-channel disconnects before sending orders.

## New behavior

Before every trade intent:

```text
session.ensure_ready()
```

Health logic:

```text
Existing session + SKReplyLib_IsConnectedByID == 1
→ REUSE
→ NO SKCenterLib_Login

Existing in-process login but Reply state != 1
→ reconnect SKReplyLib_ConnectByID only
→ wait OnComplete
→ NO SKCenterLib_Login

No active in-process session
→ do NOT blindly log in inside the order call
→ fail/skip that action so the owning runtime can rebuild the session safely
```

This keeps the trading path from becoming:

```text
BUY -> Login
SELL -> Login
BUY -> Login
...
```

The intended production model is:

```text
BOT starts
→ one Capital login/session
→ 30-minute analysis cycles reuse same session
→ health check before each BUY/SELL
```

## Replace/add

Replace:

```text
execution/capital_reply_session.py
execution/capital_executor.py
execution/auto_advice_executor.py
```

Add:

```text
check_capital_session.py
tests/test_session_health_step12.py
```

## Read-only test

```cmd
python check_capital_session.py
```

Expected healthy reply connection:

```text
READY=True
REPLY_STATE=1
ACTION=REUSE
REAL_ORDER_SENT=NO
```
