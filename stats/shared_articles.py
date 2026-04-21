"""Find articles queried by more than one user, sorted by distinct user count."""

import json
import glob
import os
import re

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "reader", "data")


def get_title(paper_dir: str) -> str:
    path = os.path.join(paper_dir, "summarize.md")
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        for line in f:
            m = re.match(r"^#\s+(.+)", line)
            if m:
                return m.group(1).strip()
    return ""


def collect_article_users():
    articles = {}
    for path in glob.glob(os.path.join(DATA_DIR, "*/queries.json")):
        paper_dir = os.path.dirname(path)
        article_id = os.path.basename(paper_dir)
        with open(path) as f:
            queries = json.load(f)
        users = {}
        for q in queries:
            uid = q["sender_open_id"]
            if uid not in users:
                users[uid] = q.get("sender_name", uid)
        if len(users) > 1:
            title = get_title(paper_dir)
            articles[article_id] = {"users": users, "title": title}
    return articles


def main():
    articles = collect_article_users()
    ranked = sorted(articles.items(), key=lambda x: len(x[1]["users"]), reverse=True)

    if not ranked:
        print("No articles queried by more than one user.")
        return

    print(f"Articles queried by multiple users: {len(ranked)}\n")
    for article_id, info in ranked:
        user_count = len(info["users"])
        title = info["title"]
        names = ", ".join(info["users"].values())
        header = f"  {article_id}"
        if title:
            header += f"  {title}"
        print(f"{header}")
        print(f"    {user_count} users: {names}")
        print()


if __name__ == "__main__":
    main()
