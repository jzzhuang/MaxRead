#!/usr/bin/env python3
"""
Scan reader/data/ and produce a Markdown summary table of all arxiv papers,
with their IDs and translated titles extracted from summarize.md.
"""
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


def extract_title(summarize_path: Path) -> str | None:
    """Extract the Chinese title (first '# ' heading) from summarize.md."""
    try:
        first_line = summarize_path.read_text(encoding="utf-8").split("\n", 1)[0]
        if first_line.startswith("# "):
            return first_line[2:].strip()
    except Exception:
        pass
    return None


def main():
    rows: list[tuple[str, str]] = []
    for d in sorted(DATA_DIR.iterdir()):
        if not d.is_dir():
            continue
        arxiv_id = d.name
        title = extract_title(d / "summarize.md")
        if title is None:
            title = "（无 summarize.md）"
        rows.append((arxiv_id, title))

    # Print Markdown table
    print("| Arxiv ID | Title |")
    print("|----------|-------|")
    for arxiv_id, title in rows:
        print(f"| {arxiv_id} | {title} |")


if __name__ == "__main__":
    main()
