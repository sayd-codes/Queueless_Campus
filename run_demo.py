"""Command-line demo: sample token data -> average waits, peak hours, chart, suggestions.

Run:  python run_demo.py            (no database needed; writes outputs/)
      python run_demo.py --csv my_tokens.csv     (analyse your own CSV instead)
"""
from __future__ import annotations

import argparse
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from app import analytics, config, recommender, seed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--csv", help="CSV with columns counter, issued_at, served_at")
    ap.add_argument("--days", type=int, default=5, help="days of sample data (default 5)")
    ap.add_argument("--out", default="outputs", help="output folder (default outputs)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    raw = pd.read_csv(args.csv) if args.csv else seed.generate_sample(days=args.days)
    if not args.csv:
        raw.to_csv(os.path.join(args.out, "sample_tokens.csv"), index=False)
    df = analytics.prepare(raw)

    print("=== Waiting-time summary (minutes) ===")
    print(analytics.waiting_summary(df).to_string(index=False), "\n")

    hp = analytics.detect_peaks(analytics.hourly_stats(df))
    print("=== Peak windows ===")
    for w in analytics.peak_windows(hp).itertuples():
        print(f"{w.counter}: {w.start_hour:02d}:00-{w.end_hour:02d}:00")

    print("\n=== Suggestions ===")
    for r in recommender.build_recommendations(df, hp):
        print(f"\n[{r['severity']}] {r['counter']} {r['window']} "
              f"(avg wait {r['avg_wait_min']} min, ~{r['arrivals_per_hr']} tokens/hr)")
        for a in r["actions"]:
            print(f"  - {a}")

    # ---- chart: tokens per hour + average wait per hour
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    hours = sorted(hp["hour"].unique())
    counters = sorted(hp["counter"].unique())
    w = 0.8 / len(counters)
    for i, c in enumerate(counters):
        g = hp[hp["counter"] == c].set_index("hour").reindex(hours)
        axes[0].bar(np.array(hours) + (i - (len(counters) - 1) / 2) * w, g["avg_arrivals"], w, label=c)
        axes[1].plot(hours, g["avg_wait"], marker="o", label=c)
    for h in hp[hp["is_peak"]]["hour"].unique():
        axes[0].axvspan(h - 0.45, h + 0.45, color="red", alpha=0.07)
    axes[0].set(title="Peak-hour chart: average tokens per hour", xlabel="Hour of day", ylabel="Tokens")
    axes[1].set(title="Average waiting time by hour", xlabel="Hour of day", ylabel="Avg wait (min)")
    axes[1].axhline(config.TARGET_WAIT_MIN, ls="--", color="grey")
    for a in axes:
        a.set_xticks(hours)
        a.set_xticklabels([f"{h}:00" for h in hours], rotation=45)
        a.legend()
        a.grid(alpha=0.3)
    plt.tight_layout()
    path = os.path.join(args.out, "peak_hour_chart.png")
    plt.savefig(path, dpi=150)
    print(f"\nChart saved to {path}")


if __name__ == "__main__":
    main()
