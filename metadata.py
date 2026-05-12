"""
Paper metadata management: doc links, query logs, and reply times.
"""
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("MaxRead")

_DOC_LINK_METADATA_FILE = "doc_link.json"

# Set by init()
_root: Path | None = None


def init(root: Path) -> None:
    """Initialize the metadata module with the project root path."""
    global _root
    _root = root


def _paper_dir(arxiv_id: str) -> Path:
    """Return the local working directory for a paper."""
    assert _root is not None, "metadata.init() must be called first"
    return _root / "reader" / "data" / arxiv_id.replace("/", "_")


def _doc_link_metadata_path(arxiv_id: str) -> Path:
    """Return the metadata path used to persist a generated doc link."""
    return _paper_dir(arxiv_id) / _DOC_LINK_METADATA_FILE


def load_saved_doc_link(arxiv_id: str) -> str | None:
    """Load a previously saved Feishu doc link for the given arXiv id."""
    metadata_path = _doc_link_metadata_path(arxiv_id)
    if not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to read saved doc link for %s: %s", arxiv_id, e)
        return None

    link = str(payload.get("doc_url") or "").strip()
    return link or None


def save_doc_link(arxiv_id: str, document_id: str, document_url: str, tenant_key: str | None) -> None:
    """Persist the Feishu doc metadata so duplicate requests can reuse it."""
    paper_dir = _paper_dir(arxiv_id)
    paper_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = _doc_link_metadata_path(arxiv_id)
    payload = {
        "arxiv_id": arxiv_id,
        "document_id": document_id,
        "doc_url": document_url,
        "tenant_key": tenant_key,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def save_query_log(
    arxiv_id: str,
    sender_open_id: str | None,
    sender_name: str | None,
) -> None:
    """Append a query record to queries.json in the paper's data directory."""
    paper_dir = _paper_dir(arxiv_id)
    paper_dir.mkdir(parents=True, exist_ok=True)
    queries_path = paper_dir / "queries.json"

    queries: list[dict] = []
    if queries_path.is_file():
        try:
            queries = json.loads(queries_path.read_text(encoding="utf-8"))
        except Exception:
            queries = []

    queries.append({
        "sender_open_id": sender_open_id,
        "sender_name": sender_name,
        "query_time": datetime.now(timezone(timedelta(hours=8))).isoformat(),
    })

    queries_path.write_text(
        json.dumps(queries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def save_reply_time(arxiv_id: str, reply_type: str) -> None:
    """Update the last query record in queries.json with reply_time."""
    paper_dir = _paper_dir(arxiv_id)
    queries_path = paper_dir / "queries.json"
    if not queries_path.is_file():
        return
    try:
        queries = json.loads(queries_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not queries:
        return
    queries[-1]["reply_time"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
    queries[-1]["reply_type"] = reply_type
    queries_path.write_text(
        json.dumps(queries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
