"""Sample data generator: simulates token queues for each counter over several working days."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import config


def _last_weekdays(n: int, end: date | None = None) -> list[date]:
    d = (end or date.today()) - timedelta(days=1)
    days: list[date] = []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def generate_sample(days: int = 5, seed: int = 42, counters: dict | None = None,
                    end: date | None = None) -> pd.DataFrame:
    """Simulate `days` working days of tokens.

    Arrivals per hour are Poisson around each counter's profile (scaled by weekday).
    Service times are normally distributed; the next free staff member serves each token.
    """
    counters = counters or config.COUNTERS
    rng = np.random.default_rng(seed)
    rows = []
    for day in _last_weekdays(days, end):
        factor = config.WEEKDAY_FACTOR.get(day.weekday(), 1.0)
        midnight = pd.Timestamp(day)
        for name, cfg in counters.items():
            arrivals = []
            for i, base in enumerate(cfg["profile"]):
                n = rng.poisson(base * factor)
                start = midnight + pd.Timedelta(hours=config.OPEN_HOUR + i)
                offs = rng.uniform(0, 60, n)
                arrivals += [start + pd.Timedelta(minutes=float(o)) for o in offs]
            arrivals.sort()
            free_at = [midnight + pd.Timedelta(hours=config.OPEN_HOUR)] * cfg["staff"]
            for token_no, issued in enumerate(arrivals, 1):
                k = int(np.argmin(free_at))
                served = max(issued, free_at[k])
                dur = max(1.0, rng.normal(cfg["service_min"], 1.2))
                finished = served + pd.Timedelta(minutes=float(dur))
                free_at[k] = finished
                rows.append({
                    "counter": name,
                    "token_no": token_no,
                    "service_type": str(rng.choice(cfg["service_types"])),
                    "issued_at": issued,
                    "served_at": served,
                    "finished_at": finished,
                })
    return pd.DataFrame(rows)
