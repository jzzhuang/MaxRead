"""
Paper metadata management: doc links, query logs, and reply times.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("MaxRead")

_DOC_LINK_METADATA_FILE = "doc_link.json"
_DOC_LINK_SCHEMA_VERSION = 2
_FILE_TOKEN_INDEX_FILE = "file_token_index.json"
_COMMENT_LOG_FILE = "comments.json"
_UNMATCHED_COMMENT_LOG_FILE = "comments_unmatched.json"

# Set by init()
_root: Path | None = None

# Reverse index: file_token → arxiv_id (lazy-built)
_file_token_index: dict[str, str] = {}
_file_token_index_built = False
_file_token_miss_scanned: set[str] = set()
_file_token_lock = threading.Lock()
_comment_log_lock = threading.Lock()


def init(root: Path) -> None:
    """Initialize the metadata module with the project root path."""
    global _root
    _root = root


def _data_dir() -> Path:
    custom = os.environ.get("MAXREAD_DATA_DIR")
    if custom:
        return Path(custom)
    assert _root is not None, "metadata.init() must be called first"
    return _root / "reader" / "data"


def _paper_dir(arxiv_id: str) -> Path:
    """Return the local working directory for a paper."""
    return _data_dir() / arxiv_id.replace("/", "_")


def _doc_link_metadata_path(arxiv_id: str) -> Path:
    """Return the metadata path used to persist a generated doc link."""
    return _paper_dir(arxiv_id) / _DOC_LINK_METADATA_FILE


def _comment_log_path(arxiv_id: str | None) -> Path:
    if arxiv_id:
        return _paper_dir(arxiv_id) / _COMMENT_LOG_FILE
    return _data_dir() / _UNMATCHED_COMMENT_LOG_FILE


def _file_token_index_path() -> Path:
    return _data_dir() / _FILE_TOKEN_INDEX_FILE


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _doc_link_record_from_payload(payload) -> dict | None:
    if not isinstance(payload, dict):
        return None

    record = {
        "document_id": _clean_optional_str(payload.get("document_id")),
        "doc_url": _clean_optional_str(payload.get("doc_url")),
        "tenant_key": _clean_optional_str(payload.get("tenant_key")),
        "model": _clean_optional_str(payload.get("model")),
        "file_token": _clean_optional_str(payload.get("file_token")),
        "saved_at": _clean_optional_str(payload.get("saved_at")),
    }
    if not record["file_token"] and record["document_id"]:
        record["file_token"] = record["document_id"]
    if not any(record.get(k) for k in ("document_id", "doc_url", "file_token")):
        return None
    return record


def _doc_link_record_key(record: dict) -> str:
    return str(record.get("file_token") or record.get("document_id") or record.get("doc_url") or "")


def _append_doc_link_record(records: list[dict], record: dict) -> None:
    key = _doc_link_record_key(record)
    if key:
        for i, existing in enumerate(records):
            if _doc_link_record_key(existing) == key:
                merged = dict(existing)
                merged.update({k: v for k, v in record.items() if v is not None})
                del records[i]
                records.append(merged)
                return
    records.append(record)


def _doc_link_records_from_payload(payload) -> list[dict]:
    records: list[dict] = []
    if not isinstance(payload, dict):
        return records

    history = payload.get("history")
    if isinstance(history, list):
        for item in history:
            record = _doc_link_record_from_payload(item)
            if record:
                _append_doc_link_record(records, record)

    current = _doc_link_record_from_payload(payload)
    if current:
        _append_doc_link_record(records, current)
    return records


def _current_doc_link_record(payload) -> dict | None:
    current = _doc_link_record_from_payload(payload)
    if current:
        return current
    records = _doc_link_records_from_payload(payload)
    return records[-1] if records else None


def _doc_token_from_url(doc_url: str | None) -> str | None:
    if not doc_url:
        return None
    try:
        parsed = urlparse(doc_url)
    except Exception:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("docx", "docs", "doc"):
        if marker not in parts:
            continue
        idx = parts.index(marker)
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def _record_file_tokens(record: dict) -> list[str]:
    tokens: list[str] = []
    for key in ("file_token", "document_id"):
        value = _clean_optional_str(record.get(key))
        if value and value not in tokens:
            tokens.append(value)
    url_token = _doc_token_from_url(_clean_optional_str(record.get("doc_url")))
    if url_token and url_token not in tokens:
        tokens.append(url_token)
    return tokens


def _index_doc_link_payload(payload, fallback_arxiv_id: str | None = None) -> None:
    if not isinstance(payload, dict):
        return
    arxiv_id = _clean_optional_str(payload.get("arxiv_id")) or fallback_arxiv_id
    if not arxiv_id:
        return
    for record in _doc_link_records_from_payload(payload):
        for token in _record_file_tokens(record):
            _file_token_index[token] = arxiv_id


def _write_file_token_index_unlocked() -> None:
    try:
        path = _file_token_index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "generated_at": _now_utc_iso(),
            "file_tokens": dict(sorted(_file_token_index.items())),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to write file token index: %s", e)


def _load_file_token_index_unlocked() -> bool:
    path = _file_token_index_path()
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        file_tokens = payload.get("file_tokens", payload)
        if not isinstance(file_tokens, dict):
            return False
        for token, arxiv_id in file_tokens.items():
            token = _clean_optional_str(token)
            arxiv_id = _clean_optional_str(arxiv_id)
            if token and arxiv_id:
                _file_token_index[token] = arxiv_id
        return True
    except Exception as e:
        logger.warning("Failed to read file token index: %s", e)
        return False


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

    current = _current_doc_link_record(payload)
    link = str((current or {}).get("doc_url") or "").strip()
    return link or None


def load_saved_doc_model(arxiv_id: str) -> str | None:
    """Load the model used to generate the saved doc for the given arXiv id."""
    metadata_path = _doc_link_metadata_path(arxiv_id)
    if not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    current = _current_doc_link_record(payload)
    return (current or {}).get("model") or None


def save_doc_link(arxiv_id: str, document_id: str, document_url: str, tenant_key: str | None, model: str | None = None, file_token: str | None = None) -> None:
    """Persist Feishu doc metadata while retaining previous generated docs."""
    global _file_token_index_built
    paper_dir = _paper_dir(arxiv_id)
    paper_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = _doc_link_metadata_path(arxiv_id)

    existing_payload = {}
    if metadata_path.is_file():
        try:
            existing_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to read existing doc link for %s: %s", arxiv_id, e)

    records = _doc_link_records_from_payload(existing_payload)
    new_record = {
        "document_id": document_id,
        "doc_url": document_url,
        "tenant_key": tenant_key,
        "model": model,
        "file_token": file_token or document_id,
        "saved_at": _now_utc_iso(),
    }
    _append_doc_link_record(records, new_record)
    current = records[-1]

    payload = {
        "schema_version": _DOC_LINK_SCHEMA_VERSION,
        "arxiv_id": arxiv_id,
        "document_id": current.get("document_id"),
        "doc_url": current.get("doc_url"),
        "tenant_key": current.get("tenant_key"),
        "model": current.get("model"),
        "file_token": current.get("file_token"),
        "saved_at": current.get("saved_at"),
        "history": records,
    }
    metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with _file_token_lock:
        if not _file_token_index_built and _load_file_token_index_unlocked():
            _file_token_index_built = True
        _index_doc_link_payload(payload, fallback_arxiv_id=arxiv_id)
        _write_file_token_index_unlocked()


def save_query_log(arxiv_id: str) -> None:
    """Append a timestamp-only query record.

    Deployments do not persist Feishu user IDs or display names.
    """
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
        "query_time": datetime.now(timezone(timedelta(hours=8))).isoformat(),
    })

    queries_path.write_text(
        json.dumps(queries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def save_reply_time(arxiv_id: str, reply_type: str) -> None:
    """Update the last query record in queries.json with reply_time."""
    save_reply_times(arxiv_id, reply_type, count=1)


def save_reply_times(arxiv_id: str, reply_type: str, *, count: int) -> None:
    """Update the last *count* query records in queries.json with reply_time."""
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
    try:
        update_count = max(1, int(count))
    except Exception:
        update_count = 1
    reply_time = datetime.now(timezone(timedelta(hours=8))).isoformat()
    for query in queries[-update_count:]:
        query["reply_time"] = reply_time
        query["reply_type"] = reply_type
    queries_path.write_text(
        json.dumps(queries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def save_comment_reply_log(
    arxiv_id: str | None,
    *,
    status: str,
    reply_model: str | None = None,
    error_recorded: bool = False,
) -> None:
    """Append a minimal comment audit record only when explicitly enabled.

    Comment bodies, quotes, user IDs, document tokens, API responses and model
    output are never persisted by this function.
    """
    if (os.environ.get("MAXREAD_ENABLE_COMMENT_AUDIT_LOG") or "").lower() not in {
        "1", "true", "yes",
    }:
        return
    arxiv_id = _clean_optional_str(arxiv_id)

    record = {
        "schema_version": 2,
        "handled_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "status": str(status)[:80],
        "arxiv_id": arxiv_id,
        "reply_model": _clean_optional_str(reply_model),
    }
    if error_recorded:
        record["error_recorded"] = True

    path = _comment_log_path(arxiv_id)
    with _comment_log_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        comments: list[dict] = []
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, list):
                    comments = existing
            except Exception as e:
                logger.warning("Failed to read comment log %s: %s", path, e)
        comments.append(record)
        path.write_text(
            json.dumps(comments, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _build_file_token_index() -> None:
    """Scan all doc_link.json files and build the file_token → arxiv_id index."""
    global _file_token_index_built
    _file_token_index.clear()
    data_dir = _data_dir()
    if not data_dir.is_dir():
        _file_token_index_built = True
        return
    for meta_file in data_dir.glob(f"*/{_DOC_LINK_METADATA_FILE}"):
        try:
            payload = json.loads(meta_file.read_text(encoding="utf-8"))
            _index_doc_link_payload(payload, fallback_arxiv_id=meta_file.parent.name)
        except Exception:
            continue
    _file_token_index_built = True
    _write_file_token_index_unlocked()


def _ensure_file_token_index() -> None:
    global _file_token_index_built
    if _file_token_index_built:
        return
    if _load_file_token_index_unlocked():
        _file_token_index_built = True
        return
    _build_file_token_index()


def lookup_paper_by_file_token(file_token: str) -> str | None:
    """Return the arxiv_id for a given Feishu file_token, or None."""
    file_token = str(file_token or "").strip()
    if not file_token:
        return None
    with _file_token_lock:
        _ensure_file_token_index()
        arxiv_id = _file_token_index.get(file_token)
        if arxiv_id or file_token in _file_token_miss_scanned:
            return arxiv_id

        # Another process may have generated a doc after this process loaded
        # the reverse index. Rescan once for this token before giving up.
        _file_token_miss_scanned.add(file_token)
        _build_file_token_index()
        return _file_token_index.get(file_token)


def lookup_paper_dir_by_file_token(file_token: str) -> Path | None:
    """Return the paper directory for a given file_token, or None."""
    arxiv_id = lookup_paper_by_file_token(file_token)
    if not arxiv_id:
        return None
    d = _paper_dir(arxiv_id)
    return d if d.is_dir() else None
