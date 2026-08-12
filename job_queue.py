"""
File-based job queue for paper processing.

Jobs are JSON files on disk, moved between pending/ → running/ → deleted.
Supports group tracking for multi-paper messages and automatic retries.

Call ``init()`` to set up callbacks before starting workers.
"""
import hashlib
import json
import logging
import os
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
_MAX_WAITERS_PER_JOB = 100
_DEFAULT_MAX_QUEUED_JOBS = 500

# Queue directories (set by init)
_QUEUE_DIR: Path | None = None
_QUEUE_PENDING_DIR: Path | None = None
_QUEUE_RUNNING_DIR: Path | None = None
_QUEUE_GROUP_DIR: Path | None = None

# Internal state
_queue_lock = threading.Lock()
_queue_event = threading.Event()
_shutdown_event = threading.Event()
_active_jobs = 0
_active_jobs_zero = threading.Condition(threading.Lock())
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def _job_key(message_id: str, item_id: str) -> str:
    raw = f"{message_id}\0{item_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _group_key(message_id: str) -> str:
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()


def _pending_job_path(job_key: str) -> Path:
    return _QUEUE_PENDING_DIR / f"{job_key}.json"


def _running_job_path(job_key: str) -> Path:
    return _QUEUE_RUNNING_DIR / f"{job_key}.json"


def _group_state_path(message_id: str) -> Path:
    return _QUEUE_GROUP_DIR / f"{_group_key(message_id)}.json"


def _dedupe_key_for_payload(payload: dict) -> str:
    return str(payload["dedupe_key"])


def _context_from_payload(payload: dict) -> dict:
    context = {
        "job_key": payload["job_key"],
        "message_id": payload["message_id"],
        "tenant_key": payload["tenant_key"],
        "staged_reactions": bool(payload["staged_reactions"]),
        "group_message_id": payload.get("group_message_id"),
        "force_regen": bool(payload.get("force_regen")),
    }
    if payload.get("job_type") == "pdf":
        context["job_type"] = "pdf"
        context["file_key"] = payload.get("file_key")
        context["file_name"] = payload.get("file_name")
    else:
        context["arxiv_id"] = payload.get("arxiv_id")
    return context


def _waiter_key(context: dict) -> str:
    return str(context["job_key"])


def _payload_waiters(payload: dict) -> list[dict]:
    return payload["waiters"]


def _payload_context_keys(payload: dict) -> set[str]:
    keys = {_waiter_key(_context_from_payload(payload))}
    for waiter in _payload_waiters(payload):
        keys.add(_waiter_key(waiter))
    return keys


def _append_waiter(payload: dict, waiter: dict) -> bool:
    key = _waiter_key(waiter)
    if key in _payload_context_keys(payload):
        return False
    waiters = _payload_waiters(payload)
    if len(waiters) >= _MAX_WAITERS_PER_JOB:
        raise OverflowError("Too many duplicate waiters for one job")
    waiters.append(waiter)
    payload["waiters"] = waiters
    return True


def _max_queued_jobs() -> int:
    raw = (os.environ.get("MAXREAD_MAX_QUEUED_JOBS") or "").strip()
    try:
        return max(1, int(raw)) if raw else _DEFAULT_MAX_QUEUED_JOBS
    except ValueError:
        logger.warning("Invalid MAXREAD_MAX_QUEUED_JOBS; using default")
        return _DEFAULT_MAX_QUEUED_JOBS


def _ensure_queue_capacity() -> None:
    total = sum(1 for _ in _QUEUE_PENDING_DIR.glob("*.json"))
    total += sum(1 for _ in _QUEUE_RUNNING_DIR.glob("*.json"))
    if total >= _max_queued_jobs():
        raise OverflowError("MaxRead queue is full")


def _add_sender_load(sender_load: dict[str, int], payload: dict) -> None:
    for context in [_context_from_payload(payload), *_payload_waiters(payload)]:
        requester = str(context.get("message_id") or "")
        sender_load[requester] = sender_load.get(requester, 0) + 1


def _find_pending_duplicate(dedupe_key: str, *, exclude_path: Path | None = None) -> tuple[Path, dict] | None:
    for path in sorted(_QUEUE_PENDING_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime):
        if exclude_path is not None and path == exclude_path:
            continue
        try:
            payload = _read_json_file(path)
        except Exception:
            continue
        if _dedupe_key_for_payload(payload) == dedupe_key:
            return path, payload
    return None


def _merge_matching_pending_jobs(
    selected_path: Path,
    selected_payload: dict,
    pending_jobs: list[tuple[Path, dict]],
) -> dict:
    dedupe_key = _dedupe_key_for_payload(selected_payload)
    selected_payload["dedupe_key"] = dedupe_key
    changed = False
    for path, payload in pending_jobs:
        if path == selected_path:
            continue
        if _dedupe_key_for_payload(payload) != dedupe_key:
            continue
        changed = _append_waiter(selected_payload, _context_from_payload(payload)) or changed
        for waiter in _payload_waiters(payload):
            changed = _append_waiter(selected_payload, waiter) or changed
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Failed to remove merged duplicate queue job %s: %s", path.name, e)
    if changed:
        _write_json_file(selected_path, selected_payload)
        logger.info(
            "Merged %d duplicate waiter(s) into queue job %s",
            len(_payload_waiters(selected_payload)),
            selected_path.name,
        )
    return selected_payload


def _iter_job_contexts(payload: dict) -> list[dict]:
    contexts = [_context_from_payload(payload)]
    contexts.extend(_payload_waiters(payload))
    return contexts


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
        if _restart_scheduled or _shutdown_event.is_set():
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
            _add_sender_load(sender_load, payload)

        running_dedupe_keys: set[str] = set()
        for path in _QUEUE_RUNNING_DIR.glob("*.json"):
            try:
                payload = _read_json_file(path)
            except Exception:
                continue
            running_dedupe_keys.add(_dedupe_key_for_payload(payload))
            _add_sender_load(sender_load, payload)

        claimable_jobs = [
            (path, payload)
            for path, payload in pending_jobs
            if _dedupe_key_for_payload(payload) not in running_dedupe_keys
        ]
        if not claimable_jobs:
            return None

        # Prioritize senders with fewest total jobs; stable sort preserves FIFO within same load
        claimable_jobs.sort(
            key=lambda item: sender_load.get(str(item[1].get("message_id") or ""), 0),
        )

        for pending_path, payload in claimable_jobs:
            payload = _merge_matching_pending_jobs(pending_path, payload, pending_jobs)
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
    updated_payload["queued_at"] = _now_iso()
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
    force_regen: bool = False,
) -> None:
    job_key = _job_key(message_id, arxiv_id)
    pending_path = _pending_job_path(job_key)
    running_path = _running_job_path(job_key)
    dedupe_key = f"arxiv:{arxiv_id.strip().lower()}:regen:{'1' if force_regen else '0'}"
    payload = {
        "job_key": job_key,
        "dedupe_key": dedupe_key,
        "message_id": message_id,
        "tenant_key": tenant_key,
        "arxiv_id": arxiv_id,
        "staged_reactions": staged_reactions,
        "group_message_id": group_message_id,
        "force_regen": force_regen,
        "queued_at": _now_iso(),
        "waiters": [],
    }
    with _queue_lock:
        if pending_path.exists() or running_path.exists():
            logger.info("Queue job already exists for message %s arXiv %s", message_id, arxiv_id)
            return
        duplicate = _find_pending_duplicate(dedupe_key)
        if duplicate:
            duplicate_path, duplicate_payload = duplicate
            if _append_waiter(duplicate_payload, _context_from_payload(payload)):
                _write_json_file(duplicate_path, duplicate_payload)
                logger.info(
                    "Attached duplicate arXiv %s request from message %s to pending job %s",
                    arxiv_id,
                    message_id,
                    duplicate_path.name,
                )
            else:
                logger.info("Queue waiter already exists for message %s arXiv %s", message_id, arxiv_id)
            _queue_event.set()
            return
        _ensure_queue_capacity()
        _write_json_file(pending_path, payload)
    _queue_event.set()


def enqueue_pdf_job(
    message_id: str,
    tenant_key: str,
    file_key: str,
    file_name: str,
) -> None:
    job_key = _job_key(message_id, file_key)
    pending_path = _pending_job_path(job_key)
    running_path = _running_job_path(job_key)
    dedupe_key = f"pdf_file:{file_key}"
    payload = {
        "job_key": job_key,
        "dedupe_key": dedupe_key,
        "job_type": "pdf",
        "message_id": message_id,
        "tenant_key": tenant_key,
        "file_key": file_key,
        "file_name": file_name,
        "staged_reactions": True,
        "queued_at": _now_iso(),
        "waiters": [],
    }
    with _queue_lock:
        if pending_path.exists() or running_path.exists():
            logger.info("Queue job already exists for message %s file %s", message_id, file_key)
            return
        duplicate = _find_pending_duplicate(dedupe_key)
        if duplicate:
            duplicate_path, duplicate_payload = duplicate
            if _append_waiter(duplicate_payload, _context_from_payload(payload)):
                _write_json_file(duplicate_path, duplicate_payload)
                logger.info(
                    "Attached duplicate PDF %s request from message %s to pending job %s",
                    file_name,
                    message_id,
                    duplicate_path.name,
                )
            else:
                logger.info("Queue waiter already exists for message %s file %s", message_id, file_key)
            _queue_event.set()
            return
        _ensure_queue_capacity()
        _write_json_file(pending_path, payload)
    _queue_event.set()


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------
def _job_label(job: dict) -> str:
    if job.get("job_type") == "pdf":
        return str(job.get("file_name") or "PDF")
    return f"arXiv {job.get('arxiv_id', 'unknown')}"


def _reply_to_job_contexts(job: dict, text: str) -> None:
    if not _reply_fn:
        return
    sent_to: set[str] = set()
    for context in _iter_job_contexts(job):
        message_id = str(context.get("message_id") or "")
        if not message_id or message_id in sent_to:
            continue
        _reply_fn(message_id, text)
        sent_to.add(message_id)


def _mark_job_contexts_finished(job: dict) -> None:
    for context in _iter_job_contexts(job):
        group_message_id = str(context.get("group_message_id") or "").strip() or None
        if group_message_id:
            _mark_group_job_finished(group_message_id)


class RestartRequestedError(RuntimeError):
    """Raised when the current job should be retried after a process restart."""
    pass


def _queue_worker() -> None:
    global _active_jobs
    while not _shutdown_event.is_set():
        claimed = _claim_next_job()
        if claimed is None:
            _queue_event.clear()
            _queue_event.wait(timeout=1.0)
            continue

        with _active_jobs_zero:
            _active_jobs += 1

        running_path, job = claimed
        preserve_running_job = False
        label = _job_label(job)
        extra_waiters = _payload_waiters(job)
        try:
            if job.get("job_type") == "pdf":
                _process_pdf_fn(
                    str(job["message_id"]),
                    str(job["tenant_key"]),
                    str(job["file_key"]),
                    str(job.get("file_name", "")),
                    staged_reactions=bool(job.get("staged_reactions")),
                    extra_waiters=extra_waiters,
                )
            else:
                _process_arxiv_fn(
                    str(job["message_id"]),
                    str(job["tenant_key"]),
                    str(job["arxiv_id"]),
                    staged_reactions=bool(job.get("staged_reactions")),
                    force_regen=bool(job.get("force_regen")),
                    extra_waiters=extra_waiters,
                )
        except RestartRequestedError:
            logger.warning(
                "Requeueing job at end before restart: message=%s %s",
                job.get("message_id"), label,
            )
            _requeue_job_at_end(running_path, job, note="process restart")
            preserve_running_job = True
        except Exception as e:
            logger.exception("Queue worker failed for %s: %s", running_path.name, e)
            retry_count = int(job.get("retry_count") or 0)
            message_id = str(job.get("message_id", ""))
            if retry_count >= _MAX_JOB_RETRIES:
                logger.error("Job exceeded max retries (%d): %s", _MAX_JOB_RETRIES, label)
                if message_id:
                    _reply_to_job_contexts(job, f"哥，试了 {retry_count} 次还是不行，先放弃了（{label}）")
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
            if not preserve_running_job:
                _mark_job_contexts_finished(job)
            with _active_jobs_zero:
                _active_jobs -= 1
                if _active_jobs == 0:
                    _active_jobs_zero.notify_all()


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


def shutdown(timeout: float = 600) -> bool:
    """Signal workers to stop and wait for in-flight jobs to finish.

    Returns True if all jobs drained within *timeout* seconds.
    """
    _shutdown_event.set()
    _queue_event.set()

    with _active_jobs_zero:
        if _active_jobs == 0:
            return True
        logger.info("Waiting for %d in-flight job(s) to finish (timeout=%ds)...", _active_jobs, timeout)
        _active_jobs_zero.wait_for(lambda: _active_jobs == 0, timeout=timeout)
        drained = _active_jobs == 0
    if not drained:
        logger.warning("Shutdown timed out with %d job(s) still running", _active_jobs)
    return drained
