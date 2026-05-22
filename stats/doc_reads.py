"""Feishu doc read statistics: UV top N and PV-UV top N."""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.drive.v1.model.get_file_statistics_request import (
    GetFileStatisticsRequest,
)
from tqdm import tqdm

from feishu.config import get_config
from feishu.resilient import call_api

DATA_DIR = Path(os.path.dirname(__file__), "..", "reader", "data")
BLACKLIST_PATH = Path(os.path.dirname(__file__), "blacklist.txt")
SNAPSHOT_DIR = Path(os.path.dirname(__file__), "snapshots")
OUTPUT_PATH = Path(os.path.dirname(__file__), "output.json")


def _load_blacklist(path: Path = BLACKLIST_PATH) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")}


def _read_title(paper_dir: Path) -> str:
    summary = paper_dir / "summarize.md"
    if summary.exists():
        first_line = summary.read_text().split("\n", 1)[0]
        if first_line.startswith("# "):
            return first_line[2:].strip()
    return ""


def _fetch_one(client, link_path: Path) -> dict | None:
    try:
        info = json.loads(link_path.read_text())
    except Exception:
        return None
    doc_id = info.get("document_id")
    arxiv_id = info.get("arxiv_id", link_path.parent.name)
    if not doc_id:
        return None

    req = GetFileStatisticsRequest.builder() \
        .file_token(doc_id) \
        .file_type("docx") \
        .build()
    resp = call_api(None, client.drive.v1.file_statistics.get, req)
    if resp is None or resp.code != 0:
        return None

    s = resp.data.statistics
    return {
        "arxiv_id": arxiv_id,
        "title": _read_title(link_path.parent),
        "doc_url": info.get("doc_url", ""),
        "pv": s.pv or 0,
        "uv": s.uv or 0,
    }


def _fetch_all(client, data_dir: Path = DATA_DIR, workers: int = 8) -> list[dict]:
    blacklist = _load_blacklist()
    paths = [p for p in sorted(data_dir.glob("*/doc_link.json")) if p.parent.name not in blacklist]
    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, client, p): p for p in paths}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Fetching stats"):
            result = fut.result()
            if result is not None:
                records.append(result)
    return records


def snapshot(records: list[dict], snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    path = snapshot_dir / f"{today}.json"
    data = {
        "date": today,
        "docs": {
            r["arxiv_id"]: {"uv": r["uv"], "pv": r["pv"], "doc_url": r["doc_url"], "title": r.get("title", "")}
            for r in records
        },
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


def stats(records: list[dict], top_n: int = 20, baseline: dict | None = None) -> str:
    if baseline is not None:
        return _stats_delta(records, top_n, baseline)

    lines: list[str] = []
    lines.append(f"Total docs scanned: {len(records)}")

    by_uv = sorted(records, key=lambda r: r["uv"], reverse=True)
    by_diff = sorted(records, key=lambda r: r["pv"] - r["uv"], reverse=True)

    uv_rank = {id(r): i for i, r in enumerate(by_uv, 1)}
    diff_rank = {id(r): i for i, r in enumerate(by_diff, 1)}

    top_uv_ids = {id(r) for r in by_uv[:top_n]}
    top_diff_ids = {id(r) for r in by_diff[:top_n]}
    merged_ids = top_uv_ids | top_diff_ids

    merged = [r for r in by_uv if id(r) in merged_ids]

    lines.append(f"\n=== Top Docs (UV + PV-UV, {len(merged)} entries) ===")
    lines.append(f"{'':>3}  {'UV#':>4} {'PV-UV#':>6}  {'arxiv_id':<20s} {'uv':>4} {'pv-uv':>5}  url")
    lines.append("  " + "-" * 90)
    for i, r in enumerate(merged, 1):
        diff = r["pv"] - r["uv"]
        ur = uv_rank[id(r)]
        dr = diff_rank[id(r)]
        ur_s = f"{ur}" if ur <= top_n else ""
        dr_s = f"{dr}" if dr <= top_n else ""
        lines.append(
            f"{i:>3}. {ur_s:>4} {dr_s:>6}  {r['arxiv_id']:<20s} {r['uv']:>4} {diff:>5}  {r['doc_url']}"
        )

    return "\n".join(lines)


def _stats_delta(records: list[dict], top_n: int, baseline: dict) -> str:
    current = {r["arxiv_id"]: r for r in records}
    all_ids = set(current) | set(baseline)

    merged_records: list[dict] = []
    for aid in all_ids:
        cur = current.get(aid)
        base = baseline.get(aid)
        title = (cur or base).get("title", "")
        doc_url = (cur or base).get("doc_url", "")
        if cur and base:
            merged_records.append({
                "arxiv_id": aid, "title": title, "doc_url": doc_url,
                "duv": cur["uv"] - base["uv"],
                "dpv": cur["pv"] - base["pv"],
            })
        elif cur:
            merged_records.append({
                "arxiv_id": aid, "title": title, "doc_url": doc_url,
                "duv": cur["uv"], "dpv": cur["pv"],
            })
        else:
            merged_records.append({
                "arxiv_id": aid, "title": title, "doc_url": doc_url,
                "duv": 0, "dpv": 0,
            })

    lines: list[str] = []
    lines.append(f"Total docs: {len(merged_records)} (current: {len(current)}, baseline: {len(baseline)})")

    by_duv = sorted(merged_records, key=lambda r: r["duv"], reverse=True)
    by_ddiff = sorted(merged_records, key=lambda r: r["dpv"] - r["duv"], reverse=True)

    duv_rank = {id(r): i for i, r in enumerate(by_duv, 1)}
    ddiff_rank = {id(r): i for i, r in enumerate(by_ddiff, 1)}

    top_duv_ids = {id(r) for r in by_duv[:top_n]}
    top_ddiff_ids = {id(r) for r in by_ddiff[:top_n]}
    merged_ids = top_duv_ids | top_ddiff_ids

    merged = [r for r in by_duv if id(r) in merged_ids]

    lines.append(f"\n=== Top Docs Delta (ΔUV + Δ(PV-UV), {len(merged)} entries) ===")
    lines.append(f"{'':>3}  {'ΔUV#':>4} {'ΔPV-UV#':>7}  {'arxiv_id':<20s} {'Δuv':>4} {'Δpv-uv':>6}  url")
    lines.append("  " + "-" * 90)
    for i, r in enumerate(merged, 1):
        ddiff = r["dpv"] - r["duv"]
        ur = duv_rank[id(r)]
        dr = ddiff_rank[id(r)]
        ur_s = f"{ur}" if ur <= top_n else ""
        dr_s = f"{dr}" if dr <= top_n else ""
        lines.append(
            f"{i:>3}. {ur_s:>4} {dr_s:>7}  {r['arxiv_id']:<20s} {r['duv']:>4} {ddiff:>6}  {r['doc_url']}"
        )

    return "\n".join(lines)


def write_output(records: list[dict], top_n: int = 20, baseline: dict | None = None,
                  output_path: Path = OUTPUT_PATH) -> Path:
    if baseline is not None:
        return _write_output_delta(records, top_n, baseline, output_path)

    by_uv = sorted(records, key=lambda r: r["uv"], reverse=True)
    by_diff = sorted(records, key=lambda r: r["pv"] - r["uv"], reverse=True)

    uv_rank = {r["arxiv_id"]: i for i, r in enumerate(by_uv, 1)}
    diff_rank = {r["arxiv_id"]: i for i, r in enumerate(by_diff, 1)}

    top_ids = {r["arxiv_id"] for r in by_uv[:top_n]} | {r["arxiv_id"] for r in by_diff[:top_n]}
    merged = [r for r in by_uv if r["arxiv_id"] in top_ids]

    output = [
        {
            "arxiv_id": r["arxiv_id"],
            "title": r.get("title", ""),
            "doc_url": r["doc_url"],
            "uv": r["uv"],
            "pv_uv": r["pv"] - r["uv"],
            "uv_rank": uv_rank[r["arxiv_id"]],
            "pv_uv_rank": diff_rank[r["arxiv_id"]],
        }
        for r in merged
    ]
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    return output_path


def _write_output_delta(records: list[dict], top_n: int, baseline: dict,
                        output_path: Path) -> Path:
    current = {r["arxiv_id"]: r for r in records}
    all_ids = set(current) | set(baseline)

    merged_records: list[dict] = []
    for aid in all_ids:
        cur = current.get(aid)
        base = baseline.get(aid)
        title = (cur or base).get("title", "")
        doc_url = (cur or base).get("doc_url", "")
        merged_records.append({
            "arxiv_id": aid, "title": title, "doc_url": doc_url,
            "duv": (cur["uv"] if cur else 0) - (base["uv"] if base else 0),
            "dpv": (cur["pv"] if cur else 0) - (base["pv"] if base else 0),
        })

    by_duv = sorted(merged_records, key=lambda r: r["duv"], reverse=True)
    by_ddiff = sorted(merged_records, key=lambda r: r["dpv"] - r["duv"], reverse=True)

    duv_rank = {r["arxiv_id"]: i for i, r in enumerate(by_duv, 1)}
    ddiff_rank = {r["arxiv_id"]: i for i, r in enumerate(by_ddiff, 1)}

    top_ids = {r["arxiv_id"] for r in by_duv[:top_n]} | {r["arxiv_id"] for r in by_ddiff[:top_n]}
    merged = [r for r in by_duv if r["arxiv_id"] in top_ids]

    output = [
        {
            "arxiv_id": r["arxiv_id"],
            "title": r.get("title", ""),
            "doc_url": r["doc_url"],
            "duv": r["duv"],
            "dpv_uv": r["dpv"] - r["duv"],
            "duv_rank": duv_rank[r["arxiv_id"]],
            "dpv_uv_rank": ddiff_rank[r["arxiv_id"]],
        }
        for r in merged
    ]
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    return output_path


def _load_snapshot(snap_path: Path) -> list[dict]:
    docs = json.loads(snap_path.read_text())["docs"]
    return [
        {"arxiv_id": aid, "title": v.get("title", ""), "doc_url": v["doc_url"], "pv": v["pv"], "uv": v["uv"]}
        for aid, v in docs.items()
    ]


def _get_today_records(client) -> list[dict]:
    today_path = SNAPSHOT_DIR / f"{date.today().isoformat()}.json"
    if today_path.exists():
        print(f"Using cached snapshot: {today_path}")
        return _load_snapshot(today_path)
    records = _fetch_all(client)
    path = snapshot(records)
    print(f"Snapshot saved: {path} ({len(records)} docs)")
    return records


def main():
    parser = argparse.ArgumentParser(description="Feishu doc read statistics")
    parser.add_argument("--since", metavar="DATE", help="Show delta since snapshot date (YYYY-MM-DD)")
    parser.add_argument("--refresh", action="store_true", help="Ignore today's cache and re-fetch")
    args = parser.parse_args()

    config = get_config()
    client = lark.Client.builder().app_id(config["app_id"]).app_secret(config["app_secret"]).build()

    if args.refresh:
        today_path = SNAPSHOT_DIR / f"{date.today().isoformat()}.json"
        if today_path.exists():
            today_path.unlink()

    records = _get_today_records(client)

    if args.since:
        snap_path = SNAPSHOT_DIR / f"{args.since}.json"
        if not snap_path.exists():
            print(f"Snapshot not found: {snap_path}")
            return
        baseline = json.loads(snap_path.read_text())["docs"]
        print(stats(records, baseline=baseline))
        out = write_output(records, baseline=baseline)
    else:
        print(stats(records))
        out = write_output(records)

    print(f"\nOutput written to: {out}")


if __name__ == "__main__":
    main()
