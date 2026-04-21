"""Count unique users served on a given day."""

import json
import glob
import os
from datetime import datetime, timedelta, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "reader", "data")
TZ = timezone(timedelta(hours=8))


def count_users(date: datetime = None):
    if date is None:
        date = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = date + timedelta(days=1)

    users = {}
    for path in glob.glob(os.path.join(DATA_DIR, "*/queries.json")):
        article_id = os.path.basename(os.path.dirname(path))
        with open(path) as f:
            for q in json.load(f):
                dt = datetime.fromisoformat(q["query_time"])
                if date <= dt < next_day:
                    uid = q["sender_open_id"]
                    if uid not in users:
                        users[uid] = (q["sender_name"], set())
                    users[uid][1].add(article_id)
    return users


def all_dates():
    timestamps = []
    for path in glob.glob(os.path.join(DATA_DIR, "*/queries.json")):
        with open(path) as f:
            for q in json.load(f):
                timestamps.append(datetime.fromisoformat(q["query_time"]))
    if not timestamps:
        return []
    first = min(timestamps).astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    last = max(timestamps).astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    dates = []
    d = first
    while d <= last:
        dates.append(d)
        d += timedelta(days=1)
    return dates


def main():
    for date in all_dates():
        users = count_users(date)
        date_str = date.strftime("%Y-%m-%d")
        print(f"{date_str}: {len(users)} users")
        for name, articles in users.values():
            print(f"  - {name} ({len(articles)} articles)")


if __name__ == "__main__":
    main()
