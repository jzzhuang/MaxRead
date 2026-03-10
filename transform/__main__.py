#!/usr/bin/env python3
"""
Module test entrypoint: run transformation on a fixed .md file and print the output link.

  # Local test: parse .md to blocks, write JSON to transform_out/, print file path
  python -m transform path/to/summarize.md
  python -m transform --md path/to/summarize.md --out-dir ./out

  # With Feishu: create cloud doc and print doc URL (requires feishu/.env)
  python -m transform --md path/to/summarize.md --feishu --title "My Summary"
"""
import argparse
import json
import sys
from pathlib import Path

# Project root for feishu config
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from transform.parser import parse_md_to_blocks
from transform.constants import (
    DOCX_BLOCK_TYPE_TEXT,
    DOCX_BLOCK_TYPE_HEADING1,
    DOCX_BLOCK_TYPE_HEADING2,
    DOCX_BLOCK_TYPE_HEADING3,
    DOCX_BLOCK_TYPE_EQUATION,
    DOCX_BLOCK_TYPE_BULLET,
    DOCX_BLOCK_TYPE_ORDERED,
    DOCX_BLOCK_TYPE_TABLE,
    DOCX_BLOCK_TYPE_CODE,
)

BLOCK_NAMES = {
    DOCX_BLOCK_TYPE_TEXT: "text",
    DOCX_BLOCK_TYPE_HEADING1: "heading1",
    DOCX_BLOCK_TYPE_HEADING2: "heading2",
    DOCX_BLOCK_TYPE_HEADING3: "heading3",
    DOCX_BLOCK_TYPE_EQUATION: "equation",
    DOCX_BLOCK_TYPE_BULLET: "bullet",
    DOCX_BLOCK_TYPE_ORDERED: "ordered",
    DOCX_BLOCK_TYPE_TABLE: "table",
    DOCX_BLOCK_TYPE_CODE: "code",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transform a Markdown file: output blocks (local) or create Feishu doc (--feishu) and print link.",
    )
    parser.add_argument(
        "md_file",
        nargs="?",
        help="Path to the .md file",
    )
    parser.add_argument(
        "--md",
        dest="md_path",
        help="Path to the .md file (alternative to positional)",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Document title (default: stem of md file)",
    )
    parser.add_argument(
        "--feishu",
        action="store_true",
        help="Create Feishu cloud doc and print doc URL (requires feishu/.env)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "transform_out",
        help="When not using --feishu, write blocks JSON here (default: transform_out)",
    )
    parser.add_argument(
        "--tenant",
        default=None,
        help="Tenant key for Feishu doc URL (e.g. your-tenant). Optional.",
    )
    args = parser.parse_args()

    path = args.md_path or args.md_file
    if not path:
        parser.error("Provide an .md file: path/to/file.md or --md path/to/file.md")
    md_path = Path(path)
    if not md_path.is_file():
        print(f"ERROR: File not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    content = md_path.read_text(encoding="utf-8")
    blocks = parse_md_to_blocks(content)
    title = args.title or md_path.stem

    if args.feishu:
        from feishu.config import get_config
        import lark_oapi as lark
        from transform.feishu_doc import create_summary_doc, doc_url

        cfg = get_config()
        app_id = cfg.get("app_id") or ""
        app_secret = cfg.get("app_secret") or ""
        if not app_id or not app_secret:
            print("ERROR: Missing FEISHU_APP_ID or FEISHU_APP_SECRET in feishu/.env", file=sys.stderr)
            sys.exit(1)
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        doc_id = create_summary_doc(client, title, content)
        if not doc_id:
            print("ERROR: Failed to create Feishu document", file=sys.stderr)
            sys.exit(1)
        url = doc_url(doc_id, args.tenant)
        print(url)
        return

    # Local: write blocks to JSON and print path
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"{md_path.stem}_blocks.json"
    out_path = args.out_dir / out_name
    payload = []
    for btype, c in blocks:
        name = BLOCK_NAMES.get(btype, btype)
        payload.append({"block_type": name, "content": c})
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out_path.resolve())


if __name__ == "__main__":
    main()
