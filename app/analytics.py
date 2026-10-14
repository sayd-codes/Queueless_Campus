"""Waiting-time analytics: cleaning, summaries, peak-hour detection, queueing maths."""
from __future__ import annotations

from math import factorial, inf

import numpy as np
import pandas as pd

from . import config

REQUIRED_COLUMNS = ["counter", "issued_at", "served_at"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# --------------------------------------------------------------------------- cleaning
def require_columns(df: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Expected at least: {', '.join(REQUIRED_COLUMNS)}"
        )


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Parse timestamps and add wait_min, service_min, hour, date, weekday.

    Rows that were never served, or have a negative wait, are dropped.
    wait_min = served_at - issued_at (service start minus token issue time).
    """
    columns = ["counter", "token_no", "service_type", "issued_at", "served_at",
               "finished_at", "source", "wait_min", "service_min", "hour", "date", "weekday"]
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=columns)
    require_columns(df)
    d = df.copy()
    for col in ("issued_at", "served_at", "finished_at"):
        d[col] = pd.to_datetime(d[col], errors="coerce") if col in d else pd.NaT
    d = d.dropna(subset=["counter", "issued_at", "served_at"])
    d["wait_min"] = (d["served_at"] - d["issued_at"]).dt.total_seconds() / 60.0
    d = d[d["wait_min"] >= 0].copy()
    d["service_min"] = (d["finished_at"] - d["served_at"]).dt.total_seconds() / 60.0
    d["hour"] = d["issued_at"].dt.hour
    d["date"] = d["issued_at"].dt.date
    d["weekday"] = d["issued_at"].dt.day_name()
    for col in ("token_no", "service_type", "source"):
        if col not in d:
            d[col] = None
    return d.reset_index(drop=True)


# --------------------------------------------------------------------------- summaries
def _p90(s: pd.Series) -> float:
    return float(s.quantile(0.9))


def waiting_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Per-counter waiting-time statistics (minutes), plus an 'All counters' row."""
    cols = ["counter", "tokens", "avg_wait", "median_wait", "p90_wait", "max_wait"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    def agg(g: pd.DataFrame) -> pd.Series:
        w = g["wait_min"]
        return pd.Series({
            "tokens": int(len(w)),
            "avg_wait": w.mean(),
            "median_wait": w.median(),
            "p90_wait": _p90(w),
            "max_wait": w.max(),
        })

    per = df.groupby("counter").apply(agg, include_groups=False).reset_index()
    total = agg(df)
    total["counter"] = "All counters"
    out = pd.concat([per, total.to_frame().T], ignore_index=True)
    out["tokens"] = out["tokens"].astype(int)
    for c in ("avg_wait", "median_wait", "p90_wait", "max_wait"):
        out[c] = out[c].astype(float).round(1)
    return out[cols]


def hourly_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Tokens and waits per counter and hour.

    avg_arrivals is the average number of tokens issued in that hour on a working day,
    so multi-day data is comparable with a single day.
    """
    cols = ["counter", "hour", "tokens", "avg_wait", "p90_wait", "days", "avg_arrivals"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    lo = min(config.OPEN_HOUR, int(df["hour"].min()))
    hi = max(config.CLOSE_HOUR - 1, int(df["hour"].max()))
    hours = list(range(lo, hi + 1))

    g = (df.groupby(["counter", "hour"])
           .agg(tokens=("wait_min", "size"),
                avg_wait=("wait_min", "mean"),
                p90_wait=("wait_min", _p90))
           .reset_index())
    idx = pd.MultiIndex.from_product([sorted(df["counter"].unique()), hours],
                                     names=["counter", "hour"])
    g = g.set_index(["counter", "hour"]).reindex(idx).reset_index()
    g["tokens"] = g["tokens"].fillna(0).astype(int)
    g[["avg_wait", "p90_wait"]] = g[["avg_wait", "p90_wait"]].fillna(0.0)
    days = df.groupby("counter")["date"].nunique()
    g["days"] = g["counter"].map(days).astype(int)
    g["avg_arrivals"] = g["tokens"] / g["days"]
    return g[cols]


def detect_peaks(hourly: pd.DataFrame, k: float | None = None) -> pd.DataFrame:
    """Flag peak hours: avg_arrivals > mean + k * std of that counter's hours."""
    k = config.PEAK_SENSITIVITY if k is None else k
    h = hourly.copy()
    if h.empty:
        h["threshold"] = []
        h["is_peak"] = []
        return h
    stats = h.groupby("counter")["avg_arrivals"].agg(["mean", "std"]).fillna(0.0)
    h["threshold"] = h["counter"].map(stats["mean"] + k * stats["std"])
    h["is_peak"] = h["avg_arrivals"] > h["threshold"]
    return h


def peak_windows(hp: pd.DataFrame) -> pd.DataFrame:
    """Merge consecutive peak hours into windows, e.g. 11:00-13:00."""
    cols = ["counter", "start_hour", "end_hour"]
    if hp.empty or "is_peak" not in hp:
        return pd.DataFrame(columns=cols)
    rows = []
    for counter, g in hp[hp["is_peak"]].groupby("counter"):
        hrs = sorted(int(x) for x in g["hour"])
        start = prev = hrs[0]
        for x in hrs[1:] + [None]:
            if x is not None and x == prev + 1:
                prev = x
                continue
            rows.append({"counter": counter, "start_hour": start, "end_hour": prev + 1})
            if x is not None:
                start = prev = x
    return pd.DataFrame(rows, columns=cols)


def weekday_hour_matrix(df: pd.DataFrame, counter: str | None = None) -> pd.DataFrame:
    """Average tokens per hour for each weekday (rows) and hour (columns)."""
    d = df if counter in (None, "All counters") else df[df["counter"] == counter]
    if d.empty:
        return pd.DataFrame()
    per_day = d.groupby(["date", "weekday", "hour"]).size().rename("n").reset_index()
    m = per_day.groupby(["weekday", "hour"])["n"].mean().unstack("hour").fillna(0.0)
    order = [w for w in WEEKDAYS if w in m.index]
    return m.loc[order].round(1)


def estimate_service_min(df: pd.DataFrame, counter: str, default: float | None = None) -> float:
    """Median service time of a counter if recorded, else the configured default."""
    fallback = default if default is not None else config.COUNTERS.get(
        counter, {}).get("service_min", config.DEFAULT_SERVICE_MIN)
    if "service_min" in df and not df.empty:
        s = df.loc[df["counter"] == counter, "service_min"].dropna()
        s = s[s > 0]
        if len(s) >= 10:
            return float(s.median())
    return float(fallback)


# --------------------------------------------------------------------------- queueing maths
def erlang_c_wait(arrivals_per_hr: float, service_min: float, servers: int) -> float:
    """Expected queue wait (minutes) for an M/M/c queue (Erlang C). inf if overloaded."""
    if servers <= 0 or service_min <= 0:
        return inf
    if arrivals_per_hr <= 0:
        return 0.0
    mu = 60.0 / service_min                 # services per hour per server
    a = arrivals_per_hr / mu                # offered load in erlangs
    if a >= servers:
        return inf
    s = sum(a ** k / factorial(k) for k in range(servers))
    top = a ** servers / factorial(servers) * servers / (servers - a)
    p_wait = top / (s + top)
    return p_wait / (servers * mu - arrivals_per_hr) * 60.0


def staff_needed(arrivals_per_hr: float, service_min: float,
                 target_wait_min: float, max_servers: int = 20) -> int:
    """Smallest number of staff that keeps the expected wait under the target."""
    for c in range(1, max_servers + 1):
        if erlang_c_wait(arrivals_per_hr, service_min, c) <= target_wait_min:
            return c
    return max_servers
