"""Turns peak-hour analysis into concrete staffing, token and appointment suggestions."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import analytics, config

SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}


def capacity_per_hour(staff: int, service_min: float,
                      utilization: float = config.TARGET_UTILIZATION) -> float:
    """Tokens one counter can serve per hour while staying at the target utilisation."""
    return staff * 60.0 / service_min * utilization


def build_recommendations(df: pd.DataFrame,
                          hp: pd.DataFrame,
                          staff: dict[str, int] | None = None,
                          target_wait: float = config.TARGET_WAIT_MIN,
                          appointment_share: float = config.APPOINTMENT_SHARE,
                          utilization: float = config.TARGET_UTILIZATION) -> list[dict]:
    """One recommendation per peak window of each counter, most severe first.

    df: prepared token data; hp: output of analytics.detect_peaks().
    """
    staff = staff or {}
    out: list[dict] = []
    for _, w in analytics.peak_windows(hp).iterrows():
        c = w["counter"]
        sub = hp[(hp["counter"] == c) & (hp["hour"] >= w["start_hour"]) & (hp["hour"] < w["end_hour"])]
        arrivals = float(sub["avg_arrivals"].mean())
        tokens = sub["tokens"].sum()
        avg_wait = float(np.average(sub["avg_wait"], weights=sub["tokens"])) if tokens > 0 else 0.0
        service_min = analytics.estimate_service_min(df, c)
        current = int(staff.get(c, config.COUNTERS.get(c, {}).get("staff", config.DEFAULT_STAFF)))
        needed = analytics.staff_needed(arrivals, service_min, target_wait)
        cap = capacity_per_hour(current, service_min, utilization)
        appt_slots = math.floor(cap * appointment_share)
        walkin_cap = math.floor(cap) - appt_slots

        # quiet hours with the most spare capacity (non-peak, lowest demand)
        others = hp[(hp["counter"] == c) & (~hp["is_peak"])].sort_values("avg_arrivals").head(2)
        quiet = [int(h) for h in others["hour"]]

        if avg_wait > 2 * target_wait:
            severity = "High"
        elif avg_wait > target_wait or needed > current:
            severity = "Medium"
        else:
            severity = "Low"

        window = f"{int(w['start_hour']):02d}:00-{int(w['end_hour']):02d}:00"
        actions = []
        if arrivals > cap:
            actions.append(f"Demand (~{arrivals:.0f} tokens/hr) is above safe capacity "
                           f"(~{cap:.0f}/hr with {current} staff).")
        if needed > current:
            actions.append(f"Add {needed - current} temporary staff during {window} "
                           f"(needed {needed}, have {current}) to keep waits under {target_wait:.0f} min.")
        actions.append(f"Cap walk-in tokens at {max(walkin_cap, 0)}/hr and release {appt_slots} "
                       f"appointment slots/hr during {window}.")
        if quiet:
            qs = ", ".join(f"{h:02d}:00" for h in quiet)
            actions.append(f"Move flexible requests to quieter hours ({qs}) using appointment booking.")
        svc = df.loc[df["counter"] == c, "service_min"].dropna() if "service_min" in df else pd.Series(dtype=float)
        if len(svc) >= 10 and svc.mean() > 0 and svc.std() / svc.mean() > 0.6:
            actions.append("Service times vary a lot: open a fast lane for short requests "
                           "(receipts, certificate collection).")

        out.append({
            "counter": c, "window": window, "severity": severity,
            "avg_wait_min": round(avg_wait, 1), "arrivals_per_hr": round(arrivals, 1),
            "service_min": round(service_min, 1), "current_staff": current,
            "recommended_staff": needed, "capacity_per_hr": round(cap, 1),
            "appointment_slots_per_hr": appt_slots, "walkin_cap_per_hr": max(walkin_cap, 0),
            "shift_to_hours": quiet, "actions": actions,
        })
    out.sort(key=lambda r: (SEVERITY_ORDER[r["severity"]], -r["avg_wait_min"]))
    return out


def recommendations_frame(recs: list[dict]) -> pd.DataFrame:
    """Flat table of recommendations (actions joined) for display or export."""
    if not recs:
        return pd.DataFrame()
    d = pd.DataFrame(recs)
    d["shift_to_hours"] = d["shift_to_hours"].apply(lambda hs: ", ".join(f"{h:02d}:00" for h in hs))
    d["actions"] = d["actions"].apply(lambda a: " | ".join(a))
    return d


def slot_plan(hp: pd.DataFrame, counter: str, staff: int, service_min: float,
              appointment_share: float = config.APPOINTMENT_SHARE,
              utilization: float = config.TARGET_UTILIZATION) -> pd.DataFrame:
    """Hour-by-hour plan: expected demand vs capacity, appointment slots and walk-in tokens."""
    h = hp[hp["counter"] == counter][["hour", "avg_arrivals"]].copy()
    cap = capacity_per_hour(staff, service_min, utilization)
    h = h.rename(columns={"avg_arrivals": "expected_demand"})
    h["expected_demand"] = h["expected_demand"].round(1)
    h["capacity"] = round(cap, 1)
    h["appointment_slots"] = math.floor(cap * appointment_share)
    h["walkin_tokens"] = math.floor(cap) - h["appointment_slots"]
    h["overflow"] = (h["expected_demand"] - h["capacity"]).clip(lower=0).round(1)
    h["spare"] = (h["capacity"] - h["expected_demand"]).clip(lower=0).round(1)
    return h.reset_index(drop=True)


def rebalance(plan: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Move overflow demand from overloaded hours to the nearest hours with spare capacity.

    Returns (moves, plan) where moves has from_hour, to_hour, tokens and the plan gains
    a 'suggested_demand' column. Total demand is conserved except for tokens that
    cannot be absorbed (reported in moves with to_hour = None).
    """
    p = plan.copy()
    spare = {int(r.hour): int(math.floor(r.spare)) for r in p.itertuples()}
    suggested = {int(r.hour): float(r.expected_demand) for r in p.itertuples()}
    moves = []
    overloaded = sorted(p[p["overflow"] > 0].itertuples(), key=lambda r: -r.overflow)
    for r in overloaded:
        left = int(math.ceil(r.overflow))
        src = int(r.hour)
        for dst in sorted(spare, key=lambda x: abs(x - src)):
            if left <= 0:
                break
            if spare[dst] <= 0 or dst == src:
                continue
            n = min(left, spare[dst])
            spare[dst] -= n
            left -= n
            suggested[src] -= n
            suggested[dst] += n
            moves.append({"from_hour": src, "to_hour": dst, "tokens": n})
        if left > 0:
            moves.append({"from_hour": src, "to_hour": None, "tokens": left})
    p["suggested_demand"] = p["hour"].map(lambda x: round(suggested[int(x)], 1))
    return pd.DataFrame(moves, columns=["from_hour", "to_hour", "tokens"]), p
