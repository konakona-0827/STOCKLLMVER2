# STOCKLLMVER2 — Execution Step 4: capital_crosscheck

Copy only:

```text
execution/capital_crosscheck.py
tests/test_capital_crosscheck.py
```

into the real project root.

Run:

```powershell
python -m unittest discover -s tests -p "test_capital_crosscheck.py" -v
```

Expected: 11 tests pass.

Then run all execution tests:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected with Steps 2–4: 23 tests pass.

## What this step does

This is deterministic cross-check logic only. It sends NO order.

Known/verified SKReply interpretation used here:

```text
parts[1] == TC
parts[2] == N and parts[3] == N -> normal accepted TC order
parts[2] == D                     -> fill event
parts[2] == C                     -> cancel event
```

Primary verification is exact 13-digit SEQ13 in live/replay SKReply rows.

`GetOrderReport(..., 9)` is supplementary only for TC and cannot, by itself,
turn an unproven TC order into ACKNOWLEDGED.

If exact SEQ13 cannot be proven after complete replay:

```text
UNCONFIRMED
position update = NO
automatic retry = NO   (enforced later by executor/store)
```

Fallback when SendStockOddLotOrder returns no SEQ13 is deliberately conservative:
it requires a complete replay, exactly one new matching normal-TC row, and one
unambiguous 13-digit sequence in that row. Unknown SKReply columns are NOT guessed.

Next step:
- integrate a read-only SKReply collector/session
- then build `capital_executor.py`
