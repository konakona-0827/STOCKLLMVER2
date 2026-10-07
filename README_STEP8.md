# STOCKLLMVER2 — Step 8: automatic BUY/SELL execution pipeline

## What changes

Interactive typing confirmation is removed from the automatic path.

Pipeline:

```text
30-minute analysis
→ portfolio_manager final
→ advice_interface.json
→ advice_adapter
→ risk_guard
→ live account check
→ BUY / SELL
→ SendStockOddLotOrder
→ SEQ13 / fallback cross-check
→ confirmed deal quantity
→ AI-managed position ledger update
```

Cancel/order-delete is NOT part of this step.

Each recommendation is isolated:
- one failed recommendation is logged and skipped
- remaining recommendations continue
- UNCONFIRMED is never automatically retried
- duplicate decision_id is never executed twice

## Live account checks

BUY:
- call SKOrderLib.GetBalance(login_id)
- require verified 一戶通
- use field 4: "當日可買進金額"
- require buying power >= planned order value + configured buffer
- cannot verify funds => skip BUY

SELL:
- call GetRealBalanceReport(login_id, account)
- wait for OnRealBalanceReport completion marker `##`
- use T (集保) inventory
- field 12 = currently sellable quantity
- symbol absent / sellable qty 0 => skip SELL
- broker sellable qty smaller than requested => skip SELL
- AI-managed qty limit still applies before broker inventory check

## Files to copy/replace

Replace:

```text
execution/capital_crosscheck.py
execution/capital_reply_session.py
execution/capital_executor.py
```

Add:

```text
execution/account_guard.py
execution/execution_store.py
execution/auto_advice_executor.py
execution/pipeline_hook.py
auto_execute_advice.py
config/execution_live.json

tests/test_account_guard.py
tests/test_execution_store.py
tests/test_capital_crosscheck_step8.py
```

## Tests

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

## Enable automatic REAL execution

The default config is deliberately disabled:

```json
"enabled": false
```

After tests and account queries are confirmed, change:

```json
"enabled": true
```

No typed SEND / EXECUTE confirmation is used after that.

## Manual run of the same automatic path

```powershell
python auto_execute_advice.py --advice data\analysis\advice_latest.json
```

With `enabled=false`, it reports DISABLED and sends nothing.

## Connect directly after LLM/advice output

Immediately after `write_advice_interface(...)` successfully writes the final advice file:

```python
from execution.pipeline_hook import execute_after_advice_written

execute_after_advice_written(
    advice_path,
    project_root=ROOT,
)
```

Call this only once for the final `portfolio_manager` advice output.

Do not call it for trader/researcher intermediate outputs.

## Fallback policy

```text
invalid advice                 -> skip
duplicate decision_id          -> skip
stale/non-LIVE quote           -> skip
BUY funds cannot be verified   -> skip
BUY funds insufficient         -> skip
SELL symbol absent in broker   -> skip
SELL sellable qty insufficient -> skip
risk check failed              -> skip
broker send failed             -> FAILED
cross-check cannot confirm     -> UNCONFIRMED, no retry
other exception                -> FAILED, continue next recommendation
```

No cancel API is invoked in this version.


## Read-only account check before enabling

```powershell
python check_account_state.py
```

This sends no order. It verifies whether the installed API/session can return:
- GetBalance buying power
- GetRealBalanceReport holdings/sellable quantity

Expected final line:

```text
REAL_ORDER_SENT=NO
```
