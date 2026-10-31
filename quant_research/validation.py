"""Chronological out-of-sample folds; no random split or threshold fitting."""
from .runner import replay
from .core import Costs


def walk_forward(data, policy, costs=Costs(), train_sessions=80, test_sessions=20):
    dates = sorted({b['date'] for rows in data.values() for b in rows})
    folds = []
    # Each fold starts a new account; positions never leak across folds.
    for start in range(train_sessions, len(dates), test_sessions):
        end = min(start+test_sessions, len(dates))-1
        result = replay(data, policy, costs, start=dates[start], end=dates[end])
        folds.append(dict(train_end=dates[start-1], test_start=dates[start], test_end=dates[end],
                          metrics=result['metrics'], open_positions=len(result['open_positions']),
                          purge='ALL_LABEL_ENDS_STRICTLY_BEFORE_EACH_DECISION'))
    return dict(folds=folds, minimum_train_sessions=train_sessions,
                test_sessions=test_sessions, threshold_tuning=False)
