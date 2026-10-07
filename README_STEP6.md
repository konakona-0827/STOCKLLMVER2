# STOCKLLMVER2 — Execution Step 6: first REAL order test

Copy only:

```text
execution/capital_crosscheck.py   # replaces Step 4 version (safer D-event semantics)
execution/capital_executor.py
tests/test_capital_crosscheck.py  # replaces Step 4 test
tests/test_capital_executor.py
real_order_test.py
inspect_order_api.py
```

## Why capital_crosscheck.py changed

A broker `D` event proves that a deal/fill event occurred, but this project has not yet
verified which fields contain filled quantity. Therefore Step 6 no longer labels any
single D event as a guaranteed full fill.

Current interpretation:

```text
N/N only      -> ACKNOWLEDGED
D observed    -> PARTIALLY_FILLED  (some deal occurred; full quantity not assumed)
C observed    -> CANCELLED
D + C         -> PARTIALLY_FILLED  (deal + cancellation observed)
nothing found -> UNCONFIRMED
```

Portfolio accounting is still NOT updated by this step.

## Offline tests

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected with Steps 2–6: 35 tests.

## Manual REAL BUY test

Choose the symbol/qty/limit price yourself.

Example syntax only:

```powershell
python real_order_test.py --symbol 0050 --side BUY --qty 1 --price 100 --send-real
```

The script prints the order details and then requires a second exact typed confirmation:

```text
SEND 0050 BUY 1
```

Without both `--send-real` and exact typed confirmation, nothing is submitted.

After submit:

```text
SendStockOddLotOrder
→ parse SEQ13
→ wait SKReply events
→ exact SEQ13 cross-check
→ if SEQ13 missing: conservative unique-new-TC fallback
```

There is no automatic retry and no position update.

SELL uses the same executor:

```powershell
python real_order_test.py --symbol 0050 --side SELL --qty 1 --price 100 --send-real
```

Only run a SELL for shares you actually intend and are able to sell.

## Order state

Current event-level meaning:

```text
ACKNOWLEDGED
= broker normal-order event was observed (TC N/N).
  No deal or cancellation for that SEQ13 has been observed by this session.

PARTIALLY_FILLED
= one or more TC D/deal events were observed.
  Filled quantity is not parsed yet, so this does NOT claim full fill.

CANCELLED
= a TC C event was observed.

UNCONFIRMED
= evidence could not prove the order.
```

Do not translate ACKNOWLEDGED into a guaranteed "currently working/open" state yet.
It means "accepted and no later terminal/deal event observed in our evidence."

## Cancel API discovery

Before implementing cancel, inspect the installed SKCOM 2.13.59 type library:

```powershell
python inspect_order_api.py
```

This sends/cancels nothing. Paste the output back into ChatGPT and implement the exact
cancel method/signature from the installed SDK instead of guessing it.
