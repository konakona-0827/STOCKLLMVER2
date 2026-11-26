"""Optional real open-source library experiments, never imported by the core CLI."""
from __future__ import annotations

import importlib.metadata
import math
import os
import json
from pathlib import Path
import time

from .data import digest, load_dataset, new_directory, write_csv, write_json
from .experiment import build_observations, evaluate, DEFAULT_PROTOCOL


def qlib_check(dataset, output, protocol):
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    import numpy as np
    import pandas as pd
    import qlib
    from qlib.contrib.data.loader import Alpha158DL
    from qlib.data import D
    from lightgbm import LGBMRegressor

    started = time.monotonic()
    out = new_directory(output)
    grouped, sessions, manifest = load_dataset(dataset)
    provider = out / "qlib_binary"
    for directory in ("calendars", "instruments", "features"):
        (provider / directory).mkdir(parents=True)
    (provider / "calendars/day.txt").write_text("\n".join(sessions) + "\n", encoding="utf-8")
    instruments = []
    for symbol, bars in grouped.items():
        instrument = "tw" + symbol
        instruments.append(instrument + "\t" + bars[0]["date"] + "\t" + bars[-1]["date"])
        folder = provider / "features" / instrument
        folder.mkdir()
        start, end = bars[0]["session_index"], bars[-1]["session_index"]
        by_index = {b["session_index"]: b for b in bars}
        for field in ("open", "high", "low", "close", "volume"):
            # Qlib's .bin format: starting calendar index followed by float32 values.
            # Preserve missing sessions as NaN. No invented restoration factor.
            values = [float(start)] + [float(by_index[i][field]) if i in by_index else np.nan for i in range(start, end + 1)]
            np.asarray(values, dtype="<f4").tofile(folder / (field + ".day.bin"))
    (provider / "instruments/all.txt").write_text("\n".join(instruments) + "\n", encoding="utf-8")
    qlib.init(provider_uri=str(provider), region="us", kernels=1,
              expression_cache=None, dataset_cache=None, logging_level="ERROR")
    expressions, names = Alpha158DL.get_feature_config()
    # Alpha158 uses historical offsets only; labels are constructed separately.
    frame = D.features(["tw" + s for s in grouped], expressions,
                       start_time=sessions[0], end_time=sessions[-1], freq="day")
    frame.columns = names
    observations, _ = build_observations(grouped, sessions, protocol)
    train_keys, train_targets, test_keys, score_keys = [], [], [], []
    for item in observations:
        feature = item["features"]
        key = ("tw" + feature["symbol"], pd.Timestamp(feature["signal_date"]))
        # The full outcome window must end before training cutoff (purge boundary).
        if item.get("label") and item["label"]["status"] == "COMPLETE" and item["label_ready_at"] <= protocol["train_through"]:
            train_keys.append(key)
            train_targets.append(float(item["label"]["net_return"]))
        if feature["signal_date"] >= protocol["test_from"]:
            test_keys.append(key)
            score_keys.append((feature["symbol"], feature["signal_date"]))
    if not train_keys or not test_keys:
        raise ValueError("QLIB_REQUIRES_SEPARATE_TRAIN_AND_TEST_PERIODS")
    train = frame.reindex(pd.MultiIndex.from_tuples(train_keys, names=frame.index.names))
    test = frame.reindex(pd.MultiIndex.from_tuples(test_keys, names=frame.index.names))
    train = train.replace([np.inf, -np.inf], np.nan)
    test = test.replace([np.inf, -np.inf], np.nan)
    nonempty = train.notna().any(axis=1)
    if nonempty.mean() < .99 or test.notna().any(axis=1).mean() < .99:
        raise ValueError("QLIB_FEATURE_ALIGNMENT_FAILED")
    # Frozen small tree model; no test-set parameter search or probability claim.
    parameters = dict(n_estimators=100, max_depth=3, num_leaves=7,
                      min_child_samples=40, learning_rate=.05,
                      random_state=11, n_jobs=1, verbosity=-1)
    model = LGBMRegressor(**parameters)
    model.fit(train.loc[nonempty], np.asarray(train_targets)[nonempty])
    predictions = model.predict(test)
    scores = dict(zip(score_keys, map(float, predictions)))
    write_csv(out / "scores.csv", ["symbol", "date", "score"],
              [dict(symbol=k[0], date=k[1], score=v) for k, v in scores.items()])
    model.booster_.save_model(str(out / "model.txt"))
    write_json(out / "scores_manifest.json", dict(
        dataset_manifest_sha256=digest((Path(dataset) / "manifest.json").read_bytes()),
        protocol_sha256=digest(json.dumps(protocol, sort_keys=True).encode()),
        scores_sha256=digest((out / "scores.csv").read_bytes()),
        model_sha256=digest((out / "model.txt").read_bytes()),
        train_labels_mature_through=protocol["train_through"],
        scope="RAW_PRICE_DIAGNOSTIC_NOT_CERTIFIED_FOR_LIVE_TRADING"))
    result = evaluate(dataset, out / "comparison", protocol, external_scores=scores)
    result.update(qlib_version=importlib.metadata.version("pyqlib"),
                  lightgbm_version=importlib.metadata.version("lightgbm"),
                  feature_count=len(names), train_rows=int(nonempty.sum()), test_rows=len(test_keys),
                  train_labels_mature_through=protocol["train_through"], model_parameters=parameters,
                  elapsed_seconds=round(time.monotonic() - started, 3),
                  use="RAW_PRICE_DIAGNOSTIC_NOT_CERTIFIED_ADJUSTED_ALPHA158_MODEL",
                  qlib_region_note="US region selects computational defaults only; no market execution simulated")
    write_json(out / "result.json", result)
    return result


def backtesting_check(dataset, output, protocol):
    import pandas as pd
    from backtesting import Backtest, Strategy

    grouped, _, _ = load_dataset(dataset)
    out = new_directory(output)
    rows = []
    for symbol, bars in grouped.items():
        # A deliberately simple independent library check, NOT the TP/SL engine:
        # signal on day t close -> next open -> close at a later next open.
        # Its different exit price and end-of-data semantics are disclosed.
        class Probe(Strategy):
            def init(self):
                self.entered_bar = None

            def next(self):
                if not self.position and self.entered_bar is None and len(self.data) == 62:
                    self.buy(size=1)
                    self.entered_bar = len(self.data)
                elif self.position and len(self.data) >= self.entered_bar + 6:
                    self.position.close()

        frame = pd.DataFrame([{f.capitalize(): b[f] for f in ("open", "high", "low", "close", "volume")} for b in bars],
                             index=pd.to_datetime([b["date"] for b in bars]))
        if len(frame) < 70:
            continue
        # Skip instruments with gaps in this short mechanical test window.
        from .experiment import contiguous
        if not contiguous(bars[:70]):
            continue
        stats = Backtest(frame.iloc[:70], Probe, cash=100000,
                         commission=(float(protocol["costs"]["minimum_commission_twd"]),
                                     float(protocol["costs"]["commission_rate"])),
                         spread=0, trade_on_close=False, finalize_trades=False).run()
        trades = stats["_trades"]
        rows.append(dict(symbol=symbol, trades=len(trades),
                         entry_date=str(trades.iloc[0]["EntryTime"].date()) if len(trades) else None,
                         exit_date=str(trades.iloc[0]["ExitTime"].date()) if len(trades) else None,
                         entry_price=float(trades.iloc[0]["EntryPrice"]) if len(trades) else None,
                         exit_price=float(trades.iloc[0]["ExitPrice"]) if len(trades) else None))
    result = dict(library="backtesting.py", version=importlib.metadata.version("backtesting"),
                  input_symbols=len(grouped), completed_probes=len(rows), results=rows,
                  scope="DATA_ADAPTER_AND_NEXT_OPEN_EXECUTION_SMOKE_ONLY",
                  not_equivalent_to_research_engine=True,
                  missing=["asymmetric_sell_tax", "max_of_minimum_and_percentage_fee",
                           "oddlot_quote_fill_evidence", "research_TP_SL_ordering", "T_PLUS_2_cash"])
    write_json(out / "result.json", result)
    return result
