"""Lossless economic fields, compact repeated provenance for LLM transport only.

Persisted audit data and Python risk validation must use the original objects.
"""
from copy import deepcopy


def compact(value):
    if isinstance(value, list):
        return [compact(row) for row in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == 'daily_bars' and isinstance(item, list):
            # Tabular encoding preserves every field/value and removes repeated
            # JSON keys. Mixed schemas stay untouched to preserve missing/null.
            if item and all(isinstance(b, dict) and set(b) == set(item[0]) for b in item):
                columns = list(item[0])
                result[key] = dict(columns=columns, rows=[[b[k] for k in columns] for b in item])
            else:
                result[key] = deepcopy(item)
        else:
            result[key] = compact(item)
    return deepcopy(result)
