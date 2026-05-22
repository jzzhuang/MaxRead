"""Read output.json and send as a Feishu card."""

import json
import sys
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feishu.config import get_config

OWNER_OPEN_ID = "ou_fa85f3ecd5572996940d6f1185aa2a24"
OUTPUT_PATH = Path(__file__).resolve().parent / "output.json"


def build_card(docs: list[dict]) -> dict:
    is_delta = "duv" in docs[0]

    if is_delta:
        title = "文档阅读趋势"
        note = "ΔUV: 新增独立访客 | Δ(PV-UV): 新增重复阅读次数\n按 ΔUV 和 Δ(PV-UV) 综合排名"
        uv_col, pv_uv_col = "duv", "dpv_uv"
        uv_key, pv_uv_key = "duv", "dpv_uv"
    else:
        title = "🏆 读不动了文档排行榜（截至5月15日）"
        note = "UV: 独立访客数 | PV-UV: 重复阅读次数（总阅读量减去独立访客）\n按 UV 和 PV-UV 综合排名，取两者 Top N 的并集"
        uv_col, pv_uv_col = "UV", "PV-UV"
        uv_key, pv_uv_key = "uv", "pv_uv"

    rows = []
    for doc in docs:
        t = doc["title"]
        rows.append({
            "doc": f"[{t}]({doc['doc_url']})",
            "stats": f"{doc[uv_key]}, {doc[pv_uv_key]}",
        })

    columns = [
        {"name": "doc", "display_name": "文章", "data_type": "lark_md", "width": "auto"},
        {"name": "stats", "display_name": f"{uv_col}, {pv_uv_col}", "width": "80px"},
    ]
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": note,
                    "text_size": "notation",
                },
                {
                    "tag": "table",
                    "page_size": len(rows),
                    "row_height": "low",
                    "header_style": {
                        "text_align": "center",
                        "text_size": "normal",
                        "background_style": "grey",
                        "bold": True,
                        "lines": 1,
                    },
                    "columns": columns,
                    "rows": rows,
                },
            ],
        },
    }


def send_card(client, open_id, card):
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(open_id)
        .content(json.dumps(card))
        .msg_type("interactive")
        .build()
    )
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(body)
        .build()
    )
    resp = client.im.v1.message.create(req)
    if not resp.success():
        print(f"send failed: {resp.code} {resp.msg}", file=sys.stderr)
        sys.exit(1)
    print("OK card sent")


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else OUTPUT_PATH
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        sys.exit(1)

    docs = json.loads(path.read_text())
    if not docs:
        print("no data in output.json", file=sys.stderr)
        sys.exit(1)
    print(f"{len(docs)} docs loaded")

    config = get_config()
    client = (
        lark.Client.builder()
        .app_id(config["app_id"])
        .app_secret(config["app_secret"])
        .build()
    )
    card = build_card(docs)
    send_card(client, OWNER_OPEN_ID, card)


if __name__ == "__main__":
    main()
