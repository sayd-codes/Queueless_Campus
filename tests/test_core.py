import math
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import analytics, db, recommender, seed  # noqa: E402


def make_df():
    rows = []
    # 2 days, one counter; 11:00 is clearly the busiest hour with long waits
    for day in ("2026-10-05", "2026-10-06"):
        for hour, n, wait in [(9, 2, 1), (10, 3, 2), (11, 12, 25), (12, 3, 2), (13, 2, 1)]:
            for i in range(n):
                issued = pd.Timestamp(f"{day} {hour:02d}:{i * 4 % 60:02d}:00")
                rows.append({"counter": "Fee", "issued_at": issued,
                             "served_at": issued + pd.Timedelta(minutes=wait),
                             "finished_at": issued + pd.Timedelta(minutes=wait + 4)})
    return pd.DataFrame(rows)


def test_prepare_computes_wait_and_drops_bad_rows():
    raw = pd.DataFrame({
        "counter": ["A", "A", "A"],
        "issued_at": ["2026-10-05 10:00", "2026-10-05 10:10", "2026-10-05 10:20"],
        "served_at": ["2026-10-05 10:05", None, "2026-10-05 10:10"],  # unserved, negative
    })
    d = analytics.prepare(raw)
    assert len(d) == 1
    assert d.loc[0, "wait_min"] == pytest.approx(5.0)


def test_missing_columns_raise():
    with pytest.raises(ValueError):
        analytics.prepare(pd.DataFrame({"counter": ["A"]}))


def test_summary_average():
    d = analytics.prepare(make_df())
    s = analytics.waiting_summary(d)
    row = s[s["counter"] == "Fee"].iloc[0]
    assert row["tokens"] == len(d)
    assert row["avg_wait"] == pytest.approx(d["wait_min"].mean(), abs=0.05)
    assert "All counters" in set(s["counter"])


def test_peak_detected_at_busiest_hour():
    d = analytics.prepare(make_df())
    hp = analytics.detect_peaks(analytics.hourly_stats(d), 0.75)
    peaks = hp[hp["is_peak"]]["hour"].tolist()
    assert peaks == [11]
    win = analytics.peak_windows(hp)
    assert win.iloc[0]["start_hour"] == 11 and win.iloc[0]["end_hour"] == 12
    # avg arrivals per working day, not total
    assert hp[hp["hour"] == 11].iloc[0]["avg_arrivals"] == pytest.approx(12.0)


def test_consecutive_peaks_merge():
    hp = pd.DataFrame({"counter": ["A"] * 4, "hour": [10, 11, 12, 15],
                       "is_peak": [True, True, True, True]})
    win = analytics.peak_windows(hp)
    assert win[["start_hour", "end_hour"]].values.tolist() == [[10, 13], [15, 16]]


def test_erlang_c_properties():
    # more staff -> less waiting; overload -> infinite
    w2 = analytics.erlang_c_wait(30, 4, 3)
    w3 = analytics.erlang_c_wait(30, 4, 4)
    assert w3 < w2
    assert analytics.erlang_c_wait(60, 4, 2) == math.inf
    # M/M/1 check: lambda=6/hr, service 6 min (mu=10/hr) -> Wq = rho/(mu-lambda) = 0.6/4 hr = 9 min
    assert analytics.erlang_c_wait(6, 6, 1) == pytest.approx(9.0)
    n = analytics.staff_needed(40, 4, 5)
    assert analytics.erlang_c_wait(40, 4, n) <= 5
    assert n == 1 or analytics.erlang_c_wait(40, 4, n - 1) > 5


def test_rebalance_conserves_demand():
    plan = pd.DataFrame({
        "hour": [9, 10, 11, 12],
        "expected_demand": [5.0, 10.0, 40.0, 8.0],
        "capacity": [20.0] * 4,
    })
    plan["overflow"] = (plan["expected_demand"] - plan["capacity"]).clip(lower=0)
    plan["spare"] = (plan["capacity"] - plan["expected_demand"]).clip(lower=0)
    moves, out = recommender.rebalance(plan)
    assert out["suggested_demand"].sum() == pytest.approx(plan["expected_demand"].sum())
    assert (out["suggested_demand"] <= out["capacity"] + 1e-9).all()
    assert moves["tokens"].sum() == 20


def test_recommendations_on_sample_data():
    d = analytics.prepare(seed.generate_sample(days=3))
    hp = analytics.detect_peaks(analytics.hourly_stats(d))
    recs = recommender.build_recommendations(d, hp)
    assert recs, "sample data should contain peak windows"
    for r in recs:
        assert r["actions"] and r["severity"] in ("High", "Medium", "Low")
        assert r["recommended_staff"] >= 1


def test_database_roundtrip(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    a = db.issue_token("Fee", "x", "2026-10-05 10:00:00", path=path)
    b = db.issue_token("Fee", "x", "2026-10-05 10:05:00", path=path)
    c = db.issue_token("Fee", "x", "2026-10-06 10:00:00", path=path)
    assert (a["token_no"], b["token_no"], c["token_no"]) == (1, 2, 1)  # numbering restarts daily
    assert db.serve_token(a["id"], "2026-10-05 10:07:00", path=path)
    assert not db.serve_token(a["id"], path=path)       # cannot serve twice
    assert not db.finish_token(b["id"], path=path)      # cannot finish before serving
    assert db.finish_token(a["id"], "2026-10-05 10:12:00", path=path)
    d = analytics.prepare(db.load_tokens(path))
    assert len(d) == 1 and d.loc[0, "wait_min"] == pytest.approx(7.0)
    n = db.insert_dataframe(seed.generate_sample(days=1), path=path)
    assert n > 100 and len(db.load_tokens(path)) == n + 3
