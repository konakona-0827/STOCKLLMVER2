# STOCKLLMVER2 — Execution Step 3: risk_guard

Copy:

```text
execution/risk_guard.py
tests/test_risk_guard.py
```

into the real project root.

Run:

```powershell
python -m unittest discover -s tests -p "test_risk_guard.py" -v
```

Expected: 8 tests pass.

Then run both Step 2 and Step 3 tests:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

## Important behavior

Real-order preparation currently requires:

- `quote_status == LIVE`
- quote age <= 120 seconds
- BUY uses current `ask`
- SELL uses current `bid`
- BUY quantity comes from `suggested_qty`, or `amount_twd / ask`
- SELL can use `suggested_qty`, or `floor(ai_managed_qty * action_ratio)`
- SELL may never exceed `ai_managed_qty`
- `user_qty` is never sellable by this execution layer

This file still sends NO broker order.

Next step after tests pass:
`capital_crosscheck.py`, then `capital_executor.py`.
