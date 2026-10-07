# STOCKLLMVER2 — Execution Step 7: SELL + advice bridge

Copy only:

```text
sell_one_test.py
execute_advice_sell.py
execution/advice_sell_runner.py
tests/test_advice_sell_runner.py
```

into the real project root.

## 1) Manual sell test

For the user's current test case, interpret `2891 7 shares` as:
- symbol: 2891
- currently held: 7 shares
- test SELL: 1 share

You choose the limit price yourself:

```powershell
python sell_one_test.py --symbol 2891 --qty 1 --price <LIMIT_PRICE> --send-real
```

Second confirmation must be typed exactly:

```text
SELL 2891 1
```

This reuses the already-tested `CapitalOddLotExecutor`.

## 2) LLM advice -> SELL bridge

The stable handoff is still:

```text
advice_interface.py
→ data/analysis/advice_latest.json
→ advice_adapter.py
→ advice_sell_runner.py
→ risk_guard.py
→ capital_executor.py
→ cross-check
```

Dry run first:

```powershell
python execute_advice_sell.py --advice data\analysis\advice_latest.json --symbol 2891
```

No order is sent without `--send-real`.

Real execution:

```powershell
python execute_advice_sell.py --advice data\analysis\advice_latest.json --symbol 2891 --send-real
```

The first version permits exactly ONE real SELL plan at a time and requires a second typed confirmation.

## Position ownership rule remains unchanged

The LLM execution bridge may sell only:

```text
ai_managed_qty
```

It does NOT treat all broker-held shares as AI-managed.

So if the 7 shares of 2891 are your existing personal shares, use `sell_one_test.py` for the manual test.

To let future LLM SELL recommendations manage 2891, the analysis/position configuration must explicitly mark the permitted quantity as `ai_managed_qty`.

Example:

```text
user_qty = 6
ai_managed_qty = 1
```

Then a SELL 1 recommendation can pass the risk guard.

## Tests

```powershell
python -m unittest discover -s tests -p "test_advice_sell_runner.py" -v
```

Expected: 4 tests.

Then:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

This step adds no automatic position update and no automatic retry.
