"""
Backward-compatible re-export layer.

All implementation has moved to:
  reader.download   — ArxivNotFoundError, PdfExtractionError, extract_arxiv_ids, download_source, extract_archive
  reader.dir_manager — directory trimming and image processing
  reader.agent      — Claude/Cursor CLI launching
  reader.pipeline   — prepare_paper_dir, summarize_prepared_dir, run_summarize, run_local_summarize
"""
from pathlib import Path

from .download import (
    ArxivNotFoundError,
    PdfExtractionError,
    extract_arxiv_ids,
    arxiv_id_from_url,
    download_source,
    extract_archive,
)
from .agent import (
    launch_claude_in_folder,
    launch_claude_for_illustration as _launch_claude_for_illustration_raw,
    launch_agent_in_folder,
)
from .pipeline import (
    SUMMARY_PROMPT,
    ILLUSTRATION_PROMPT,
    prepare_paper_dir,
    summarize_prepared_dir,
    run_local_summarize,
    run_summarize,
    main,
)


def launch_claude_for_illustration(paper_dir: Path) -> tuple[str, str]:
    """Backward-compatible wrapper: passes ILLUSTRATION_PROMPT automatically.
    Returns (prompt_text, caption_text).
    """
    return _launch_claude_for_illustration_raw(paper_dir, ILLUSTRATION_PROMPT)


__all__ = [
    "ArxivNotFoundError",
    "PdfExtractionError",
    "extract_arxiv_ids",
    "arxiv_id_from_url",
    "download_source",
    "extract_archive",
    "launch_claude_in_folder",
    "launch_claude_for_illustration",
    "launch_agent_in_folder",
    "prepare_paper_dir",
    "summarize_prepared_dir",
    "run_local_summarize",
    "run_summarize",
    "SUMMARY_PROMPT",
    "ILLUSTRATION_PROMPT",
    "main",
]


if __name__ == "__main__":
    main()
