# Synthetic LLM BUY/SELL signal tests

These two files mimic the final analysis/LLM handoff.

They generate an `advice_interface`-compatible JSON and can immediately pass it
to the same `execution.pipeline_hook` used by the 30-minute production pipeline.

## Files

- `test_llm_buy_signal.py`
- `test_llm_sell_signal.py`

Copy both to the project root.

## BUY — emit only

```cmd
python test_llm_buy_signal.py --symbol 0050 --qty 1 --price 100
```

This writes a synthetic advice JSON but does not execute it.

## BUY — full automatic pipeline

```cmd
python test_llm_buy_signal.py --symbol 0050 --qty 1 --price 100 --execute
```

If `config\execution_live.json` has `"enabled": true`, this can submit a REAL order.
There is no interactive SEND/EXECUTE confirmation.

## SELL — emit only

```cmd
python test_llm_sell_signal.py --symbol 0050 --qty 1 --price 100
```

## SELL — full automatic pipeline

```cmd
python test_llm_sell_signal.py --symbol 0050 --qty 1 --price 100 --execute
```

SELL still passes:
- Risk Guard
- AI-managed quantity check
- broker inventory/sellable quantity check
- cross-check

Recommended end-to-end test:
1. BUY 1 share through `test_llm_buy_signal.py --execute`
2. wait for confirmed fill
3. SELL that same 1 share through `test_llm_sell_signal.py --execute`

That way the AI-managed ledger is created by a real confirmed BUY fill.

If you intentionally want to bootstrap an existing share into AI management for a SELL test:

```cmd
python test_llm_sell_signal.py --symbol 2891 --qty 1 --price <price> --bootstrap-ai-managed-qty 1 --execute
```

Only use that option when you explicitly intend to grant that existing quantity to the AI execution ledger.
