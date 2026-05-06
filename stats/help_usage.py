"""Count help button usage per user from maxread.log."""

import re
import sys
import glob
import os
from collections import Counter

import lark_oapi as lark
from lark_oapi.api.contact.v3 import GetUserRequest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from feishu.config import get_config

LOG_DIR = os.path.join(os.path.dirname(__file__), "..")
PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) .* event_key=Action: help open_id=(\S+?)(?:\s+name=(.+))?$",
)


def _build_client():
    cfg = get_config()
    app_id = cfg.get("app_id") or ""
    app_secret = cfg.get("app_secret") or ""
    if not app_id or not app_secret:
        return None
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


def _fetch_user_name(client, open_id: str) -> str | None:
    if not client:
        return None
    try:
        req = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
        resp = client.contact.v3.user.get(req)
        if resp and getattr(resp, "code", 0) == 0:
            user = getattr(getattr(resp, "data", None), "user", None)
            return getattr(user, "name", None) if user else None
    except Exception:
        pass
    return None


def parse_help_actions():
    counter: Counter[str] = Counter()
    names: dict[str, str] = {}
    first_date = None
    last_date = None

    for path in sorted(glob.glob(os.path.join(LOG_DIR, "maxread.log*"))):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = PATTERN.match(line)
                    if not m:
                        continue
                    date_str, oid, name = m.group(1), m.group(2), m.group(3)
                    counter[oid] += 1
                    if name and name != "None":
                        names[oid] = name
                    if first_date is None or date_str < first_date:
                        first_date = date_str
                    if last_date is None or date_str > last_date:
                        last_date = date_str
        except Exception:
            continue

    return counter, names, first_date, last_date


def main():
    counter, names, first_date, last_date = parse_help_actions()
    if not counter:
        print("暂无 help 使用记录。")
        return

    client = _build_client()
    for oid in counter:
        if oid not in names:
            name = _fetch_user_name(client, oid)
            if name:
                names[oid] = name

    print(f"Help 使用统计 ({first_date} ~ {last_date})\n")
    for oid, count in counter.most_common():
        display = names.get(oid, oid)
        print(f"  {display}: {count} 次")
    print(f"\n合计: {sum(counter.values())} 次, {len(counter)} 位用户")


if __name__ == "__main__":
    main()
