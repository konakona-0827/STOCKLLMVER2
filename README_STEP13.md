# Step 13 — refactored BUY/SELL synthetic LLM tests

The two test files are now thin wrappers around one shared runner:

```text
test_llm_buy_signal.py
test_llm_sell_signal.py
        ↓
execution/synthetic_llm_test_runner.py
        ↓
ONE Capital login/session
        ↓
session health check
        ↓
account/inventory precheck
        ↓
same session passed to production pipeline
        ↓
health check again before each BUY/SELL
        ↓
SendStockOddLotOrder
        ↓
Cross-check
        ↓
DB event log
```

There is no second login inside the same test process.

## Replace/add

Add:

```text
execution/synthetic_llm_test_runner.py
```

Replace:

```text
test_llm_buy_signal.py
test_llm_sell_signal.py
```

## BUY test

```cmd
python test_llm_buy_signal.py --symbol 0050 --qty 1 --price 150 --execute
```

## SELL test

```cmd
python test_llm_sell_signal.py --symbol 2891 --qty 1 --price 150 --bootstrap-ai-managed-qty 1 --execute
```

Important output:

```text
session_health_before.ready
session_health_before.reply_state
session_health_before.action

BROKER_ORDER_SENT=True/False
CROSSCHECK=...
```
