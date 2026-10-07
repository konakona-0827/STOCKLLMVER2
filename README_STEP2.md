# STOCKLLMVER2 — Execution Step 2

Copy this package to the project root:

```text
STOCKLLMVER2/
├─ advice_interface.py
├─ execution/
│  ├─ __init__.py
│  ├─ models.py
│  └─ advice_adapter.py
├─ tests/
│  └─ test_advice_adapter.py
├─ config/
├─ data/
└─ ...
```

Recommended stable handoff files:

```text
data/analysis/advice_latest.json
data/analysis/runs/<run_id>/advice_interface.json
```

The analysis side writes these files.
The execution side only reads them.

Test:

```powershell
python -m unittest -v tests.test_advice_adapter
```

Validate a real advice interface without placing any order:

```powershell
python -m execution.advice_adapter data\analysis\advice_latest.json
```

Expected final line:

```text
BROKER_ORDER_SENT=NO
```

Next step is `execution/risk_guard.py`.

Do not add SKCOM order submission until `risk_guard.py` tests pass.
