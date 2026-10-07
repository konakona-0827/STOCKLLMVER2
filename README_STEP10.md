# Step 10 — clearer synthetic tests + per-order cross-check/audit

Copy/replace:

```text
execution/capital_executor.py
execution/auto_advice_executor.py
execution/execution_store.py

test_llm_buy_signal.py
test_llm_sell_signal.py

tests/test_execution_store_step10.py
```

## Important interpretation

`PIPELINE_EXECUTED=YES` does NOT mean a broker order was sent.

Look at each result:

```text
BROKER_ORDER_SENT=False
CROSSCHECK=<not-run>
```

means the pipeline ran but the order was blocked before the broker call.

Example:

```text
status=SKIPPED_CHECK_FAILED
reason=no AI-managed shares available to sell
```

means Capital never received a SELL order.

## Synthetic SELL precheck

When `--execute` is used, the SELL test first prints:

```text
BROKER_REALTIME_QTY=
BROKER_SELLABLE_QTY=
AI_MANAGED_QTY=
PRECHECK_WOULD_ALLOW_SELL=
```

Then the production pipeline independently checks the same gates again.

## Synthetic BUY precheck

It prints:

```text
AVAILABLE_TO_BUY_TWD=
CURRENT_HOLDING_QTY=
```

The production pipeline still performs its own deterministic checks.

## Every submitted order is cross-checked

For each BUY/SELL that reaches the broker:

```text
STARTED
→ SUBMITTED  (written immediately to SQLite)
→ exact SEQ13 cross-check
→ ACKNOWLEDGED / PARTIALLY_FILLED / FILLED / CANCELLED / UNCONFIRMED / FAILED
```

The final state is then written to SQLite.

Database:

```text
data/execution/execution.sqlite3
```

Tables:

```text
execution_attempts
execution_events
ai_positions
```

## Multi-recommendation JSON

The automatic executor already loops over every trade intent in ONE final advice JSON:

```python
for intent in intents:
    ...
```

One failed intent does not stop the next one.

Only the final portfolio-manager/advice-interface JSON should drive execution.
Intermediate agent JSON files must not independently trigger trading.

## Performance

`observe_seconds` is now a timeout, not a mandatory sleep when SEQ13 is known.
The executor stops waiting as soon as SKReply proves an ACK/CANCEL/DEAL event.

For batches up to roughly 10 orders, sequential send -> immediate cross-check -> persist
is intentionally preferred over parallel order placement because it keeps:

- cash reservation deterministic
- sellable inventory deterministic
- event correlation simple
- error isolation clear

The batch takes one account/inventory snapshot and then reserves cash/stock locally
for later orders in the same batch.

If you want a tighter timeout, set in `config/execution_live.json`:

```json
"observe_seconds": 5
```

The 120-second quote-age gate still applies independently.
