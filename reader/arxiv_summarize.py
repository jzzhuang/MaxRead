#!/usr/bin/env python3
"""
For an arXiv link: download TeX source, unzip into data/, ask Claude to summarize
the paper in one sentence (saved to summarize.txt), and print the result.
Uses API key from ~/.claude/settings.json (ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY).
"""
import argparse
import json
import os
import re
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

# ArXiv requires a descriptive User-Agent
USER_AGENT = "MaxRead-arxiv-summarize/1.0 (mailto:research@example.com)"
SETTINGS_PATH = Path(os.path.expanduser("~/.claude/settings.json"))
DATA_DIR = Path(__file__).resolve().parent / "data"
MAX_TEX_CHARS = 150_000  # Limit context size for Claude


def arxiv_id_from_url(url: str) -> str | None:
    """Extract arXiv id from abs/pdf/e-print URL. E.g. 2301.12345 or 2301.12345v2."""
    m = re.search(r"arxiv\.org/(?:abs|pdf|e-print)/([^/?#]+)", url, re.I)
    return m.group(1).strip() if m else None


def download_source(arxiv_id: str) -> Path:
    """Download e-print source from arXiv; return path to the downloaded file."""
    import urllib.request

    url = f"https://arxiv.org/e-print/{arxiv_id}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with tempfile.NamedTemporaryFile(delete=False, suffix=".arxiv") as f:
        path = Path(f.name)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        path.write_bytes(data)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def extract_archive(archive_path: Path, out_dir: Path) -> None:
    """Extract .tar.gz or .zip to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data = archive_path.read_bytes()
    if data[:2] == b"\x1f\x8b":  # gzip
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(out_dir)
    elif data[:2] == b"PK":
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(out_dir)
    else:
        raise ValueError("Unknown archive format (expected .tar.gz or .zip)")


def collect_tex_content(tex_dir: Path) -> str:
    """Gather .tex file contents, preferring main.tex. Truncate to MAX_TEX_CHARS."""
    tex_dir = Path(tex_dir)
    tex_files = sorted(tex_dir.rglob("*.tex"))
    main_tex = tex_dir / "main.tex"
    if main_tex.exists():
        order = [main_tex] + [p for p in tex_files if p != main_tex]
    else:
        order = tex_files
    parts = []
    total = 0
    for p in order:
        if total >= MAX_TEX_CHARS:
            break
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        take = min(len(text), MAX_TEX_CHARS - total)
        parts.append(f"--- {p.relative_to(tex_dir)} ---\n{text[:take]}")
        total += take
    return "\n\n".join(parts) if parts else ""


def load_claude_settings() -> dict:
    """Load env from ~/.claude/settings.json."""
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with open(SETTINGS_PATH) as f:
            return (json.load(f).get("env") or {})
    except Exception:
        return {}


def summarize_with_claude(tex_content: str, paper_dir: Path) -> str:
    """Call Claude to summarize the paper in one sentence; return that sentence."""
    env = load_claude_settings()
    api_key = (
        env.get("ANTHROPIC_AUTH_TOKEN")
        or env.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or ""
    ).strip()
    if not api_key:
        raise SystemExit(
            "No ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY in ~/.claude/settings.json or env."
        )
    base_url = (env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    model = env.get("ANTHROPIC_MODEL") or os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-20250514"

    import anthropic
    client_kwargs = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = anthropic.Anthropic(**client_kwargs)

    system = (
        "You are given the TeX source of an academic paper. "
        "Reply with exactly one sentence that summarizes the paper. "
        "Do not include quotes, prefixes, or extra text—only the one-sentence summary."
    )
    user = (
        "Summarize the following paper in one sentence. "
        "Your reply will be saved to summarize.txt.\n\n"
        + (tex_content or "(No .tex content found)")
    )
    message = client.messages.create(
        model=model,
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    summary = ""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            summary += (getattr(block, "text", "") or "")
    summary = summary.strip()
    (paper_dir / "summarize.txt").write_text(summary, encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Download arXiv TeX source and summarize with Claude.")
    parser.add_argument("url", help="arXiv URL (e.g. https://arxiv.org/abs/2301.12345)")
    args = parser.parse_args()

    arxiv_id = arxiv_id_from_url(args.url)
    if not arxiv_id:
        print("ERROR: Could not parse arXiv id from URL.", file=sys.stderr)
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    paper_dir = DATA_DIR / arxiv_id.replace("/", "_")

    print(f"arXiv id: {arxiv_id}")
    print("Downloading source...")
    archive_path = download_source(arxiv_id)
    try:
        print(f"Extracting to {paper_dir}...")
        extract_archive(archive_path, paper_dir)
    finally:
        archive_path.unlink(missing_ok=True)

    tex_content = collect_tex_content(paper_dir)
    print("Asking Claude for a one-sentence summary...")
    summary = summarize_with_claude(tex_content, paper_dir)
    print("Summary (saved to summarize.txt):")
    print(summary)


if __name__ == "__main__":
    main()
