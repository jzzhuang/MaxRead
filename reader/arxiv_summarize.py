"""
Backward-compatible re-export layer.

All implementation has moved to:
  reader.download   — ArxivNotFoundError, PdfExtractionError, extract_arxiv_ids, download_source, extract_archive
  reader.dir_manager — directory trimming and image processing
  reader.agent      — tool-free OpenAI Responses API calls
  reader.pipeline   — prepare_paper_dir, summarize_prepared_dir, run_summarize, run_local_summarize
"""
from .agent import (
    DEFAULT_MODEL,
    launch_codex_in_folder,
)
from .download import (
    ArxivNotFoundError,
    PdfExtractionError,
    arxiv_id_from_url,
    download_source,
    extract_archive,
    extract_arxiv_ids,
)
from .pipeline import (
    SUMMARY_PROMPT,
    main,
    prepare_paper_dir,
    run_local_summarize,
    run_summarize,
    summarize_prepared_dir,
)

__all__ = [
    "ArxivNotFoundError",
    "PdfExtractionError",
    "extract_arxiv_ids",
    "arxiv_id_from_url",
    "download_source",
    "extract_archive",
    "DEFAULT_MODEL",
    "launch_codex_in_folder",
    "prepare_paper_dir",
    "summarize_prepared_dir",
    "run_local_summarize",
    "run_summarize",
    "SUMMARY_PROMPT",
    "main",
]


if __name__ == "__main__":
    main()
