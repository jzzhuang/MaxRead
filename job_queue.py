"""
File-based job queue for paper processing.

Jobs are JSON files on disk, moved between pending/ → running/ → deleted.
Supports group tracking for multi-paper messages and automatic retries.

Call ``init()`` to set up callbacks before starting workers.
"""
import hashlib
import json
import logging
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger("MaxRead")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MAX_PARALLEL = 8
_MAX_JOB_RETRIES = 5

# Queue directories (set by init)
_QUEUE_DIR: Path | None = None
_QUEUE_PENDING_DIR: Path | None = None
_QUEUE_RUNNING_DIR: Path | None = None
_QUEUE_GROUP_DIR: Path | None = None

# Internal state
_queue_lock = threading.Lock()
_queue_event = threading.Event()
_queue_workers_started = False
_restart_scheduled = False

# Callbacks (set by init)
_process_arxiv_fn: Callable | None = None
_process_pdf_fn: Callable | None = None
_reply_fn: Callable | None = None
_remove_reaction_fn: Callable | None = None

# Project root for error dump (set by init)
_root: Path | None = None


def init(
    root: Path,
    *,
    process_arxiv: Callable,
    process_pdf: Callable,
    reply_to_message: Callable,
    remove_reaction: Callable,
) -> None:
    """Initialize queue with root path and processing callbacks."""
    global _QUEUE_DIR, _QUEUE_PENDING_DIR, _QUEUE_RUNNING_DIR, _QUEUE_GROUP_DIR
    global _process_arxiv_fn, _process_pdf_fn, _reply_fn, _remove_reaction_fn, _root

    _root = root
    _QUEUE_DIR = root / ".maxread_queue"
    _QUEUE_PENDING_DIR = _QUEUE_DIR / "pending"
    _QUEUE_RUNNING_DIR = _QUEUE_DIR / "running"
    _QUEUE_GROUP_DIR = _QUEUE_DIR / "groups"

    _process_arxiv_fn = process_arxiv
    _process_pdf_fn = process_pdf
    _reply_fn = reply_to_message
    _remove_reaction_fn = remove_reaction


def set_restart_scheduled(value: bool) -> None:
    """Mark that a process restart has been scheduled (stops claiming new jobs)."""
    global _restart_scheduled
    _restart_scheduled = value


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------
def _write_json_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _read_json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def _job_key(message_id: str, item_id: str) -> str:
    raw = f"{message_id}\0{item_id}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _group_key(message_id: str) -> str:
    return hashlib.sha1(message_id.encode("utf-8")).hexdigest()


def _pending_job_path(job_key: str) -> Path:
    return _QUEUE_PENDING_DIR / f"{job_key}.json"


def _running_job_path(job_key: str) -> Path:
    return _QUEUE_RUNNING_DIR / f"{job_key}.json"


def _group_state_path(message_id: str) -> Path:
    return _QUEUE_GROUP_DIR / f"{_group_key(message_id)}.json"


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------
def _recover_running_jobs() -> int:
    recovered = 0
    for running_path in sorted(_QUEUE_RUNNING_DIR.glob("*.json")):
        payload = _read_json_file(running_path)
        _requeue_job_at_end(running_path, payload, note="Recovered after restart")
        recovered += 1
    return recovered


def _claim_next_job() -> tuple[Path, dict] | None:
    with _queue_lock:
        if _restart_scheduled:
            return None

        pending_paths = sorted(
            _QUEUE_PENDING_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        if not pending_paths:
            return None

        # Count jobs per sender across pending + running
        sender_load: dict[str, int] = {}
        pending_jobs: list[tuple[Path, dict]] = []
        for path in pending_paths:
            try:
                payload = _read_json_file(path)
            except Exception:
                continue
            pending_jobs.append((path, payload))
            sender = payload.get("sender_open_id") or ""
            sender_load[sender] = sender_load.get(sender, 0) + 1
        for path in _QUEUE_RUNNING_DIR.glob("*.json"):
            try:
                payload = _read_json_file(path)
            except Exception:
                continue
            sender = payload.get("sender_open_id") or ""
            sender_load[sender] = sender_load.get(sender, 0) + 1

        # Prioritize senders with fewest total jobs; stable sort preserves FIFO within same load
        pending_jobs.sort(
            key=lambda item: sender_load.get(item[1].get("sender_open_id") or "", 0),
        )

        for pending_path, payload in pending_jobs:
            running_path = _QUEUE_RUNNING_DIR / pending_path.name
            try:
                pending_path.replace(running_path)
            except FileNotFoundError:
                continue
            return running_path, payload
    return None


def _complete_job(running_path: Path) -> None:
    try:
        running_path.unlink(missing_ok=True)
    except Exception as e:
        logger.warning("Failed to remove completed queue job %s: %s", running_path.name, e)


def _requeue_job_at_end(running_path: Path, payload: dict, *, note: str) -> None:
    pending_path = _QUEUE_PENDING_DIR / running_path.name
    updated_payload = dict(payload)
    updated_payload["queued_at"] = datetime.now(timezone.utc).isoformat()
    updated_payload["retry_count"] = int(updated_payload.get("retry_count") or 0) + 1
    updated_payload["last_requeue_note"] = note
    _write_json_file(pending_path, updated_payload)
    running_path.unlink(missing_ok=True)
    _queue_event.set()


# ---------------------------------------------------------------------------
# Group state (multi-paper messages)
# ---------------------------------------------------------------------------
def ensure_group_state(message_id: str, total_jobs: int, reaction_id: str | None) -> None:
    path = _group_state_path(message_id)
    with _queue_lock:
        if path.exists():
            return
        _write_json_file(
            path,
            {
                "message_id": message_id,
                "remaining": total_jobs,
                "reaction_id": reaction_id,
            },
        )


def _mark_group_job_finished(message_id: str) -> None:
    path = _group_state_path(message_id)
    reaction_id: str | None = None
    with _queue_lock:
        if not path.exists():
            return
        payload = _read_json_file(path)
        remaining = max(0, int(payload.get("remaining") or 0) - 1)
        if remaining > 0:
            payload["remaining"] = remaining
            _write_json_file(path, payload)
            return
        reaction_id = str(payload.get("reaction_id") or "").strip() or None
        path.unlink(missing_ok=True)
    if reaction_id and _remove_reaction_fn:
        _remove_reaction_fn(message_id, reaction_id)


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------
def enqueue_arxiv_job(
    message_id: str,
    tenant_key: str,
    arxiv_id: str,
    *,
    staged_reactions: bool,
    group_message_id: str | None = None,
    sender_open_id: str | None = None,
    sender_name: str | None = None,
) -> None:
    job_key = _job_key(message_id, arxiv_id)
    pending_path = _pending_job_path(job_key)
    running_path = _running_job_path(job_key)
    payload = {
        "message_id": message_id,
        "tenant_key": tenant_key,
        "arxiv_id": arxiv_id,
        "staged_reactions": staged_reactions,
        "group_message_id": group_message_id,
        "sender_open_id": sender_open_id,
        "sender_name": sender_name,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    with _queue_lock:
        if pending_path.exists() or running_path.exists():
            logger.info("Queue job already exists for message %s arXiv %s", message_id, arxiv_id)
            return
        _write_json_file(pending_path, payload)
    _queue_event.set()


def enqueue_pdf_job(
    message_id: str,
    tenant_key: str,
    file_key: str,
    file_name: str,
    *,
    sender_open_id: str | None = None,
    sender_name: str | None = None,
) -> None:
    job_key = _job_key(message_id, file_key)
    pending_path = _pending_job_path(job_key)
    running_path = _running_job_path(job_key)
    payload = {
        "job_type": "pdf",
        "message_id": message_id,
        "tenant_key": tenant_key,
        "file_key": file_key,
        "file_name": file_name,
        "staged_reactions": True,
        "sender_open_id": sender_open_id,
        "sender_name": sender_name,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    with _queue_lock:
        if pending_path.exists() or running_path.exists():
            logger.info("Queue job already exists for message %s file %s", message_id, file_key)
            return
        _write_json_file(pending_path, payload)
    _queue_event.set()


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------
def _job_label(job: dict) -> str:
    if job.get("job_type") == "pdf":
        return str(job.get("file_name") or "PDF")
    return f"arXiv {job.get('arxiv_id', 'unknown')}"


class RestartRequestedError(RuntimeError):
    """Raised when the current job should be retried after a process restart."""
    pass


def _queue_worker() -> None:
    while True:
        claimed = _claim_next_job()
        if claimed is None:
            _queue_event.clear()
            _queue_event.wait(timeout=1.0)
            continue

        running_path, job = claimed
        group_message_id = str(job.get("group_message_id") or "").strip() or None
        preserve_running_job = False
        label = _job_label(job)
        try:
            if job.get("job_type") == "pdf":
                _process_pdf_fn(
                    str(job["message_id"]),
                    str(job["tenant_key"]),
                    str(job["file_key"]),
                    str(job.get("file_name", "")),
                    staged_reactions=bool(job.get("staged_reactions")),
                    sender_open_id=job.get("sender_open_id"),
                    sender_name=job.get("sender_name"),
                )
            else:
                _process_arxiv_fn(
                    str(job["message_id"]),
                    str(job["tenant_key"]),
                    str(job["arxiv_id"]),
                    staged_reactions=bool(job.get("staged_reactions")),
                    sender_open_id=job.get("sender_open_id"),
                    sender_name=job.get("sender_name"),
                )
        except RestartRequestedError:
            logger.warning(
                "Requeueing job at end before restart: message=%s %s",
                job.get("message_id"), label,
            )
            _requeue_job_at_end(running_path, job, note="Claude CLI crash restart")
            preserve_running_job = True
        except subprocess.TimeoutExpired as e:
            message_id = str(job.get("message_id", ""))
            logger.error("Claude CLI timed out for %s, giving up", label)
            errors_dir = _root / "errors"
            errors_dir.mkdir(parents=True, exist_ok=True)
            safe_label = label.replace("/", "_").replace(" ", "_")
            prompt_file = errors_dir / f"timed_out_prompt_{safe_label}.txt"
            prompt_content = " ".join(str(a) for a in (e.cmd or [])) if e.cmd else str(e)
            prompt_file.write_text(prompt_content, encoding="utf-8")
            logger.info("Saved timed-out prompt to %s", prompt_file)
            if message_id and _reply_fn:
                _reply_fn(message_id, f"哥，网又寄了，要不待会再试（{label} 超时了）")
        except Exception as e:
            logger.exception("Queue worker failed for %s: %s", running_path.name, e)
            retry_count = int(job.get("retry_count") or 0)
            message_id = str(job.get("message_id", ""))
            if retry_count >= _MAX_JOB_RETRIES:
                logger.error("Job exceeded max retries (%d): %s", _MAX_JOB_RETRIES, label)
                if message_id and _reply_fn:
                    _reply_fn(message_id, f"哥，试了 {retry_count} 次还是不行，先放弃了（{label}）")
            else:
                logger.warning(
                    "Requeueing failed job at end (retry %d/%d): message=%s %s",
                    retry_count + 1, _MAX_JOB_RETRIES, job.get("message_id"), label,
                )
                _requeue_job_at_end(running_path, job, note=f"Job failed: {type(e).__name__}")
                preserve_running_job = True
        finally:
            if not preserve_running_job and running_path.exists():
                _complete_job(running_path)
            if group_message_id and not preserve_running_job:
                _mark_group_job_finished(group_message_id)


def start_workers() -> None:
    global _queue_workers_started
    with _queue_lock:
        if _queue_workers_started:
            return
        _queue_workers_started = True
    for index in range(_MAX_PARALLEL):
        threading.Thread(
            target=_queue_worker,
            name=f"arxiv_queue_{index + 1}",
            daemon=True,
        ).start()


def initialize() -> None:
    """Create queue directories, recover interrupted jobs, and start workers."""
    for path in (_QUEUE_PENDING_DIR, _QUEUE_RUNNING_DIR, _QUEUE_GROUP_DIR):
        path.mkdir(parents=True, exist_ok=True)
    recovered = _recover_running_jobs()
    pending = len(list(_QUEUE_PENDING_DIR.glob("*.json")))
    if recovered:
        logger.warning("Recovered %s running arXiv job(s) back into the queue", recovered)
    if pending:
        logger.info("Queue has %s pending arXiv job(s) on startup", pending)
        _queue_event.set()
    start_workers()
