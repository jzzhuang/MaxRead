"""Generate a bar chart of query counts per 2-hour bucket."""

import json
import glob
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "reader", "data")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "queries_per_2h.png")

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 4, 17, 0, 1, tzinfo=TZ)
BUCKET_HOURS = 2


def load_queries():
    queries = []
    for path in glob.glob(os.path.join(DATA_DIR, "*/queries.json")):
        paper_id = os.path.basename(os.path.dirname(path))
        with open(path) as f:
            for q in json.load(f):
                q["paper_id"] = paper_id
                queries.append(q)
    queries.sort(key=lambda q: q["query_time"])
    return queries


def bucket_key(dt: datetime) -> datetime:
    elapsed = (dt - START).total_seconds()
    if elapsed < 0:
        return None
    bucket_index = int(elapsed // (BUCKET_HOURS * 3600))
    return START + timedelta(hours=BUCKET_HOURS * bucket_index)


def build_histogram(queries):
    new_counts = defaultdict(int)
    reused_counts = defaultdict(int)
    seen_papers = set()
    for q in queries:
        dt = datetime.fromisoformat(q["query_time"])
        paper_id = q["paper_id"]
        key = bucket_key(dt)
        is_new = paper_id not in seen_papers
        seen_papers.add(paper_id)
        if key is not None:
            if is_new:
                new_counts[key] += 1
            else:
                reused_counts[key] += 1
    all_keys = sorted(set(new_counts) | set(reused_counts))
    return {k: (new_counts[k], reused_counts[k]) for k in all_keys}


def plot(counts: dict, output: str = OUTPUT_PATH):
    if not counts:
        print("No queries found after the start time.")
        return

    buckets = list(counts.keys())
    last_bucket = max(buckets)
    all_buckets = []
    t = min(buckets)
    while t <= last_bucket:
        all_buckets.append(t)
        t += timedelta(hours=BUCKET_HOURS)

    new_vals = [counts.get(b, (0, 0))[0] for b in all_buckets]
    reused_vals = [counts.get(b, (0, 0))[1] for b in all_buckets]
    totals = [n + r for n, r in zip(new_vals, reused_vals)]

    fig, ax = plt.subplots(figsize=(12, 5))
    bar_width = timedelta(hours=BUCKET_HOURS) * 0.8

    bars_reused = ax.bar(all_buckets, reused_vals, width=bar_width,
                         color="#E8A838", edgecolor="white", linewidth=0.5,
                         label="Reused paper")
    bars_new = ax.bar(all_buckets, new_vals, width=bar_width,
                      bottom=reused_vals, color="#4C72B0", edgecolor="white",
                      linewidth=0.5, label="New paper")

    for bucket, n, r, total in zip(all_buckets, new_vals, reused_vals, totals):
        if total > 0:
            cx = mdates.date2num(bucket)
            ax.text(cx, total + 0.3, str(total),
                    ha="center", va="bottom", fontsize=9)

    ax.set_xlabel("Time (2-hour buckets)")
    ax.set_ylabel("Number of Queries")
    ax.set_title("Queries per 2 Hours (starting 04/17 00:01 CST)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M", tz=TZ))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=BUCKET_HOURS, tz=TZ))
    fig.autofmt_xdate(rotation=45)
    ax.set_ylim(0, max(totals) + 3)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper left")
    plt.tight_layout()
    plt.savefig(output, dpi=150)
    plt.close()
    print(f"Saved to {output}")


def main():
    queries = load_queries()
    counts = build_histogram(queries)
    plot(counts)


if __name__ == "__main__":
    main()
