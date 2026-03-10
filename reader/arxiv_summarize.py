#!/usr/bin/env python3
"""
For an arXiv link: download TeX source, unzip into data/, launch the Claude CLI
in that folder so it can read the files and write a one-sentence summary to
summarize.txt, then print the result.
Requires the `claude` command (Claude Code) to be installed and authenticated.
"""
import argparse
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

# ArXiv requires a descriptive User-Agent
USER_AGENT = "MaxRead-arxiv-summarize/1.0 (mailto:research@example.com)"
DATA_DIR = Path(__file__).resolve().parent / "data"
SUMMARY_PROMPT = "Summarize the paper in this folder into one sentence and write it to summarize.txt"


# Bare arXiv id: YYMM.NNNNN or YYMM.NNNNNvN
_ARXIV_BARE_RE = re.compile(r"(?:^|[^\w.])(\d{4}\.\d{4,5}(?:v\d+)?)(?=[^\d]|$)")
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf|e-print)/([^/?#\s]+)", re.I)


def arxiv_id_from_url(url: str) -> str | None:
    """Extract arXiv id from abs/pdf/e-print URL. E.g. 2301.12345 or 2301.12345v2."""
    m = _ARXIV_URL_RE.search(url)
    return m.group(1).strip() if m else None


def extract_arxiv_ids(text: str) -> list[str]:
    """Extract all arXiv ids from text: full URLs and bare ids like 2301.12345."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _ARXIV_URL_RE.finditer(text):
        aid = m.group(1).strip()
        if aid not in seen:
            seen.add(aid)
            out.append(aid)
    for m in _ARXIV_BARE_RE.finditer(text):
        aid = m.group(1).strip()
        if aid not in seen:
            seen.add(aid)
            out.append(aid)
    return out


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


def launch_claude_in_folder(paper_dir: Path, prompt: str) -> str:
    """
    Launch the Claude CLI in paper_dir with the given prompt. Claude can read
    files in that folder and write summarize.txt. Returns the content of
    summarize.txt after Claude exits.
    """
    paper_dir = Path(paper_dir).resolve()
    out_file = paper_dir / "summarize.txt"
    out_file.unlink(missing_ok=True)
    proc = subprocess.run(
        ["claude", "-p", prompt, "--permission-mode", "bypassPermissions"],
        cwd=str(paper_dir),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"claude command failed: {err}")
    if not out_file.exists():
        cmd = f'cd {paper_dir!r} && claude -p {prompt!r} --permission-mode bypassPermissions'
        raise FileNotFoundError(
            f"Claude did not create summarize.txt in {paper_dir}. stdout: {(proc.stdout or '')[:500]}\n\n"
            f"Run this in your terminal to run Claude directly:\n  {cmd}"
        )
    return out_file.read_text(encoding="utf-8").strip()


def run_summarize(arxiv_id: str, data_dir: Path | None = None) -> str:
    """
    Download arXiv source, extract to data_dir/<id>/, launch Claude CLI in that
    folder to summarize and write summarize.txt. Returns the one-sentence summary.
    """
    data_dir = data_dir or DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    paper_dir = data_dir / arxiv_id.replace("/", "_")
    archive_path = download_source(arxiv_id)
    try:
        extract_archive(archive_path, paper_dir)
    finally:
        archive_path.unlink(missing_ok=True)
    return launch_claude_in_folder(paper_dir, SUMMARY_PROMPT)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download arXiv TeX source, launch Claude in that folder to summarize."
    )
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

    print("Launching Claude in folder (reads files and writes summarize.txt)...")
    summary = launch_claude_in_folder(paper_dir, SUMMARY_PROMPT)
    print("Summary (saved to summarize.txt):")
    print(summary)


if __name__ == "__main__":
    main()
