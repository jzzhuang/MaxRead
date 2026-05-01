"""List articles queried between 01:00 and 07:00 (CST)."""

import json
import glob
import os
from datetime import datetime, timedelta, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "reader", "data")
TZ = timezone(timedelta(hours=8))

NIGHT_START = 1
NIGHT_END = 7


def load_night_queries():
    records = []
    for path in glob.glob(os.path.join(DATA_DIR, "*/queries.json")):
        article_id = os.path.basename(os.path.dirname(path))
        with open(path) as f:
            for q in json.load(f):
                dt = datetime.fromisoformat(q["query_time"])
                if NIGHT_START <= dt.hour < NIGHT_END:
                    records.append({
                        "article_id": article_id,
                        "sender_name": q.get("sender_name"),
                        "sender_open_id": q.get("sender_open_id"),
                        "query_time": dt,
                    })
    records.sort(key=lambda r: r["query_time"])
    return records


def main():
    records = load_night_queries()
    print(f"Night queries (01:00-07:00 CST): {len(records)}")
    if not records:
        return

    by_date = {}
    for r in records:
        date_str = r["query_time"].strftime("%Y-%m-%d")
        by_date.setdefault(date_str, []).append(r)

    for date_str, items in by_date.items():
        print(f"\n{date_str} ({len(items)} queries):")
        for r in items:
            name = r["sender_name"] or r["sender_open_id"] or "unknown"
            time_str = r["query_time"].strftime("%H:%M")
            print(f"  {time_str}  {name}  {r['article_id']}")


if __name__ == "__main__":
    main()
