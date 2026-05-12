"""
High-level paper processing pipeline: prepare directories, run summarization,
and provide CLI entry point.
"""
import argparse
import shutil
import sys
from pathlib import Path
from typing import Callable

from .download import (
    ArxivNotFoundError,
    PdfExtractionError,
    arxiv_id_from_url,
    extract_arxiv_ids,
    download_source,
    extract_archive,
    _pdf_to_text,
)
from .dir_manager import (
    _MAX_DIR_BYTES,
    dir_size,
    convert_figure_pdfs,
    trim_paper_dir,
)
from .agent import (
    launch_claude_in_folder,
    launch_claude_for_illustration,
    launch_agent_in_folder,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
PROMPT_FILE = Path(__file__).resolve().parent / "prompt.txt"
ILLUSTRATION_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "paper_illustration.md"


def _load_prompt() -> str:
    """Load the summarization prompt from reader/prompt.txt."""
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(f"Prompt file not found: {PROMPT_FILE}")
    return PROMPT_FILE.read_text(encoding="utf-8").strip()


SUMMARY_PROMPT = _load_prompt()


def _load_illustration_prompt() -> str:
    """Load the illustration prompt from prompts/paper_illustration.md."""
    if not ILLUSTRATION_PROMPT_FILE.exists():
        raise FileNotFoundError(f"Illustration prompt file not found: {ILLUSTRATION_PROMPT_FILE}")
    base = ILLUSTRATION_PROMPT_FILE.read_text(encoding="utf-8").strip()
    return (
        base
        + "\n\n---\n\n"
        "现在请阅读当前目录下的论文文件（.tex、.md、.txt、图片等），按照上述工作流程生成英文 image prompt 和中文配文。\n"
        "注意：不要输出到终端，将内容直接写入两个文件：\n"
        "1. illustration_prompt.txt — 英文 image prompt（纯文本内容，不要包含 ``` 代码块标记）\n"
        "2. illustration_caption.txt — 中文配文（纯文本，1-3 句话，不要包含 ``` 代码块标记）"
    )


ILLUSTRATION_PROMPT = _load_illustration_prompt()


def _emit_progress(progress_callback: Callable[[str], None] | None, message: str) -> None:
    """Send a progress update if a callback was provided."""
    if progress_callback and message:
        progress_callback(message)


def prepare_paper_dir(
    arxiv_id: str,
    data_dir: Path | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> Path:
    """
    Download arXiv source and extract into data_dir/<id>/. Returns the paper
    directory path, ready for agent processing.
    """
    data_dir = data_dir or DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    paper_dir = data_dir / arxiv_id.replace("/", "_")
    _emit_progress(progress_callback, f"开始下载 arXiv {arxiv_id} 源码...")
    archive_path = download_source(arxiv_id)
    try:
        # Preserve queries.json across re-extractions
        queries_backup = None
        queries_path = paper_dir / "queries.json"
        if queries_path.is_file():
            queries_backup = queries_path.read_bytes()
        if paper_dir.exists():
            shutil.rmtree(paper_dir)
        _emit_progress(progress_callback, "源码下载完成，正在解压文件...")
        extract_archive(archive_path, paper_dir)
        if queries_backup is not None:
            (paper_dir / "queries.json").write_bytes(queries_backup)
        convert_figure_pdfs(paper_dir)
        if dir_size(paper_dir) > _MAX_DIR_BYTES:
            trim_paper_dir(paper_dir)
    finally:
        archive_path.unlink(missing_ok=True)
    _emit_progress(progress_callback, f"论文文件已准备完成：{paper_dir.name}")
    return paper_dir


def summarize_prepared_dir(
    paper_dir: Path,
    mode: str = "claude",
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    """
    Run the summarization agent on an already-prepared paper directory.
    Returns the Markdown summary text.
    """
    if mode == "cursor":
        return launch_agent_in_folder(paper_dir, SUMMARY_PROMPT, progress_callback=progress_callback)
    return launch_claude_in_folder(paper_dir, SUMMARY_PROMPT)


def run_local_summarize(
    name: str,
    data_dir: Path | None = None,
    mode: str = "claude",
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    """
    Summarize a paper already present in data/<name>/ with a .pdf file.
    Converts the PDF to markdown, then launches the chosen agent.
    """
    data_dir = data_dir or DATA_DIR
    paper_dir = data_dir / name
    if not paper_dir.is_dir():
        raise FileNotFoundError(f"Directory not found: {paper_dir}")

    pdfs = [f for f in paper_dir.iterdir() if f.suffix.lower() == ".pdf" and f.is_file()]
    if not pdfs:
        raise FileNotFoundError(f"No PDF file found in {paper_dir}")
    pdf_path = pdfs[0]

    _emit_progress(progress_callback, f"正在转换 PDF：{pdf_path.name}")
    md_path = paper_dir / "paper.md"
    text = _pdf_to_text(pdf_path, paper_dir)
    md_path.write_text(text, encoding="utf-8")

    convert_figure_pdfs(paper_dir)
    if dir_size(paper_dir) > _MAX_DIR_BYTES:
        trim_paper_dir(paper_dir)

    _emit_progress(progress_callback, f"论文文件已准备完成：{paper_dir.name}")
    if mode == "cursor":
        return launch_agent_in_folder(paper_dir, SUMMARY_PROMPT, progress_callback=progress_callback)
    return launch_claude_in_folder(paper_dir, SUMMARY_PROMPT)


def run_summarize(
    arxiv_id: str,
    data_dir: Path | None = None,
    mode: str = "claude",
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    """
    Download arXiv source, extract to data_dir/<id>/, launch the chosen agent
    (claude or cursor) in that folder to summarize and write summarize.md.
    Returns the Markdown summary.
    """
    paper_dir = prepare_paper_dir(arxiv_id, data_dir, progress_callback)
    return summarize_prepared_dir(paper_dir, mode, progress_callback)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download arXiv TeX source, launch Claude or Cursor agent in that folder to summarize."
    )
    parser.add_argument("input", help="arXiv URL, arXiv id, or local folder name in data/ (e.g. deepseekV4)")
    parser.add_argument(
        "--mode",
        choices=("claude", "cursor"),
        default="claude",
        help="Agent to use: claude (default) or cursor",
    )
    args = parser.parse_args()

    local_dir = DATA_DIR / args.input
    if local_dir.is_dir():
        print(f"Using local paper directory: {local_dir}")
        summary = run_local_summarize(args.input, mode=args.mode)
        print("Summary (saved to summarize.md):")
        print(summary)
        return

    arxiv_id = arxiv_id_from_url(args.input) or args.input.strip()
    if not arxiv_id:
        print("ERROR: Could not parse arXiv id from input.", file=sys.stderr)
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

    agent_name = "Cursor agent" if args.mode == "cursor" else "Claude"
    print(f"Launching {agent_name} in folder (reads files and writes summarize.md)...")
    if args.mode == "cursor":
        summary = launch_agent_in_folder(paper_dir, SUMMARY_PROMPT)
    else:
        summary = launch_claude_in_folder(paper_dir, SUMMARY_PROMPT)
    print("Summary (saved to summarize.md):")
    print(summary)


if __name__ == "__main__":
    main()
