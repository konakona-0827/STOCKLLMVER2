# Step 14 — fix rc=1019 duplicate account query

## Root cause

The synthetic test used one Capital session correctly, but it still queried inventory twice:

```text
synthetic precheck
→ GetRealBalanceReport

immediately afterward production execution
→ GetRealBalanceReport again
→ rc=1019
```

`1019 = SK_ERROR_QUERY_IN_PROCESSING`: the broker query is still considered in process /
was called too quickly.

## Fix

One account snapshot per final-advice batch:

```text
Login once
→ session health check
→ account snapshot ONCE
   ├─ buying power (when BUY exists)
   └─ inventory (when SELL exists; synthetic BUY test may also request it for display)
→ pass SAME session + SAME account snapshot into production executor
→ no immediate duplicate GetRealBalanceReport
→ BUY/SELL loop
→ per-order Cross-check
```

Inventory query also has a small READ-ONLY fallback:
- only rc=1019 is retried
- max 2 attempts
- 0.8 s settle delay
- this is NOT an order retry

## Replace / add

Replace:

```text
execution/account_guard.py
execution/auto_advice_executor.py
execution/pipeline_hook.py
execution/synthetic_llm_test_runner.py
```

Add:

```text
execution/account_snapshot.py
```

The two test wrappers from Step 13 can remain unchanged.

## Commands

BUY:

```cmd
python test_llm_buy_signal.py --symbol 0050 --qty 1 --price 150 --execute
```

SELL:

```cmd
python test_llm_sell_signal.py --symbol 2891 --qty 1 --price 150 --bootstrap-ai-managed-qty 1 --execute
```
