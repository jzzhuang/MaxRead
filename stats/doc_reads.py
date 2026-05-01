"""Feishu doc read statistics: UV top N and PV-UV top N."""

import json
import os
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.drive.v1.model.get_file_statistics_request import (
    GetFileStatisticsRequest,
)

from feishu.config import get_config
from feishu.resilient import call_api

DATA_DIR = Path(os.path.dirname(__file__), "..", "reader", "data")


def stats(client, data_dir: Path = DATA_DIR, top_n: int = 20) -> str:
    records: list[dict] = []
    for link_path in sorted(data_dir.glob("*/doc_link.json")):
        try:
            info = json.loads(link_path.read_text())
        except Exception:
            continue
        doc_id = info.get("document_id")
        arxiv_id = info.get("arxiv_id", link_path.parent.name)
        if not doc_id:
            continue

        req = GetFileStatisticsRequest.builder() \
            .file_token(doc_id) \
            .file_type("docx") \
            .build()
        resp = call_api(None, client.drive.v1.file_statistics.get, req)
        if resp is None or resp.code != 0:
            continue

        s = resp.data.statistics
        records.append({
            "arxiv_id": arxiv_id,
            "doc_url": info.get("doc_url", ""),
            "pv": s.pv or 0,
            "uv": s.uv or 0,
        })

    lines: list[str] = []
    lines.append(f"Total docs scanned: {len(records)}")

    by_uv = sorted(records, key=lambda r: r["uv"], reverse=True)[:top_n]
    lines.append(f"\n=== UV Top {top_n} ===")
    for i, r in enumerate(by_uv, 1):
        lines.append(f"{i:>3}. {r['arxiv_id']}  uv={r['uv']}  pv={r['pv']}  {r['doc_url']}")

    by_diff = sorted(records, key=lambda r: r["pv"] - r["uv"], reverse=True)[:top_n]
    lines.append(f"\n=== PV-UV Top {top_n} ===")
    for i, r in enumerate(by_diff, 1):
        diff = r["pv"] - r["uv"]
        lines.append(f"{i:>3}. {r['arxiv_id']}  pv-uv={diff}  pv={r['pv']}  uv={r['uv']}  {r['doc_url']}")

    return "\n".join(lines)


def main():
    config = get_config()
    client = lark.Client.builder().app_id(config["app_id"]).app_secret(config["app_secret"]).build()
    print(stats(client))


if __name__ == "__main__":
    main()
