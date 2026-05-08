#!/usr/bin/env python3
"""
Listen to Feishu; for any message containing an arXiv link (URL or bare id),
download the TeX source, summarize with Claude by default
(output: concise Markdown in summarize.md),
create a cloud doc (云文档) with sections, paragraphs, and equations, then reply with a short completion message.
Each incoming message is handled in a background thread so new messages are accepted while work runs;
a message with multiple arXiv ids processes them with at most two jobs at a time (others queue) and sends one reply per paper.

  python MaxRead.py

Requires: FEISHU_APP_ID, FEISHU_APP_SECRET in feishu/.env;
  CURSOR_API_KEY in env or cursor_api_key.txt at repo root.
"""
import json
import hashlib
import logging
import logging.handlers
import re
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Project root
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, CreateMessageRequest, CreateMessageRequestBody
from lark_oapi.api.application.v6 import P2ApplicationBotMenuV6
from lark_oapi.api.im.v1.model.create_message_reaction_request import CreateMessageReactionRequest
from lark_oapi.api.im.v1.model.create_message_reaction_request_body import CreateMessageReactionRequestBody
from lark_oapi.api.im.v1.model.delete_message_reaction_request import DeleteMessageReactionRequest
from lark_oapi.api.im.v1.model.emoji import Emoji
from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
from lark_oapi.api.im.v1.model.reply_message_request_body import ReplyMessageRequestBody

from lark_oapi.api.contact.v3 import GetUserRequest

from feishu.config import get_config
from feishu.resilient import call_api
from reader.arxiv_summarize import ArxivNotFoundError, PdfExtractionError, extract_arxiv_ids, run_summarize, run_local_summarize
from transform.feishu_doc import create_summary_doc, doc_url

_LOG_FILE = ROOT / "maxread.log"
_log_fmt = logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
))

_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_file_handler.setFormatter(_log_fmt)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_console, _file_handler],
)
logger = logging.getLogger("MaxRead")

# HTTP client (built in main)
_feishu_client = None

# Deduplicate by message_id (Feishu may deliver the same message event more than once)
_MAX_PROCESSED_IDS = 5000
_processed_message_ids: set[str] = set()
_dedup_lock = threading.Lock()
_DOC_LINK_METADATA_FILE = "doc_link.json"

# Feishu HTTP client may not be thread-safe; serialize API calls.
_feishu_api_lock = threading.Lock()

# Background work so the WS handler returns immediately and new messages can be accepted.
# At most 2 arXiv jobs run at once; additional work waits in a disk-backed queue so restart can resume them.
_MAX_PARALLEL = 8
_MAX_JOB_RETRIES = 5
_RESTART_DELAY_SECONDS = 3
_restart_lock = threading.Lock()
_restart_scheduled = False
_queue_lock = threading.Lock()
_queue_event = threading.Event()
_queue_workers_started = False
_QUEUE_DIR = ROOT / ".maxread_queue"
_QUEUE_PENDING_DIR = _QUEUE_DIR / "pending"
_QUEUE_RUNNING_DIR = _QUEUE_DIR / "running"
_QUEUE_GROUP_DIR = _QUEUE_DIR / "groups"


class RestartRequestedError(RuntimeError):
    """Raised when the current job should be retried after a process restart."""
    pass


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


def _job_key(message_id: str, arxiv_id: str) -> str:
    raw = f"{message_id}\0{arxiv_id}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _group_key(message_id: str) -> str:
    return hashlib.sha1(message_id.encode("utf-8")).hexdigest()


def _pending_job_path(job_key: str) -> Path:
    return _QUEUE_PENDING_DIR / f"{job_key}.json"


def _running_job_path(job_key: str) -> Path:
    return _QUEUE_RUNNING_DIR / f"{job_key}.json"


def _group_state_path(message_id: str) -> Path:
    return _QUEUE_GROUP_DIR / f"{_group_key(message_id)}.json"


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
        pending_paths = sorted(_QUEUE_PENDING_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime)
        for pending_path in pending_paths:
            running_path = _QUEUE_RUNNING_DIR / pending_path.name
            try:
                pending_path.replace(running_path)
            except FileNotFoundError:
                continue
            return running_path, _read_json_file(running_path)
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


def _ensure_group_state(message_id: str, total_jobs: int, reaction_id: str | None) -> None:
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
    if reaction_id:
        _remove_reaction(message_id, reaction_id)


def _enqueue_arxiv_job(
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


def _enqueue_pdf_job(
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


def _job_label(job: dict) -> str:
    if job.get("job_type") == "pdf":
        return str(job.get("file_name") or "PDF")
    return f"arXiv {job.get('arxiv_id', 'unknown')}"


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
                _process_one_pdf_for_message(
                    str(job["message_id"]),
                    str(job["tenant_key"]),
                    str(job["file_key"]),
                    str(job.get("file_name", "")),
                    staged_reactions=bool(job.get("staged_reactions")),
                    sender_open_id=job.get("sender_open_id"),
                    sender_name=job.get("sender_name"),
                )
            else:
                _process_one_arxiv_for_message(
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
            errors_dir = ROOT / "errors"
            errors_dir.mkdir(parents=True, exist_ok=True)
            safe_label = label.replace("/", "_").replace(" ", "_")
            prompt_file = errors_dir / f"timed_out_prompt_{safe_label}.txt"
            prompt_content = " ".join(str(a) for a in (e.cmd or [])) if e.cmd else str(e)
            prompt_file.write_text(prompt_content, encoding="utf-8")
            logger.info("Saved timed-out prompt to %s", prompt_file)
            if message_id:
                _reply_to_message(message_id, f"哥，网又寄了，要不待会再试（{label} 超时了）")
        except Exception as e:
            logger.exception("Queue worker failed for %s: %s", running_path.name, e)
            retry_count = int(job.get("retry_count") or 0)
            message_id = str(job.get("message_id", ""))
            if retry_count >= _MAX_JOB_RETRIES:
                logger.error("Job exceeded max retries (%d): %s", _MAX_JOB_RETRIES, label)
                if message_id:
                    _reply_to_message(message_id, f"哥，试了 {retry_count} 次还是不行，先放弃了（{label}）")
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


def _start_queue_workers() -> None:
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


def _initialize_queue() -> None:
    for path in (_QUEUE_PENDING_DIR, _QUEUE_RUNNING_DIR, _QUEUE_GROUP_DIR):
        path.mkdir(parents=True, exist_ok=True)
    recovered = _recover_running_jobs()
    pending = len(list(_QUEUE_PENDING_DIR.glob("*.json")))
    if recovered:
        logger.warning("Recovered %s running arXiv job(s) back into the queue", recovered)
    if pending:
        logger.info("Queue has %s pending arXiv job(s) on startup", pending)
        _queue_event.set()
    _start_queue_workers()


def _should_restart_for_exception(exc: Exception) -> bool:
    """Restart only for the known Claude/Bun crash that a fresh process often clears."""
    text = str(exc).lower()
    if "claude command failed" not in text:
        return False
    return "illegal instruction" in text or "bun has crashed" in text


def _restart_process_after_delay(reason: str) -> None:
    logger.warning("MaxRead will restart in %ss: %s", _RESTART_DELAY_SECONDS, reason)
    time.sleep(_RESTART_DELAY_SECONDS)
    logger.warning("Restarting MaxRead now")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.argv])


def _schedule_process_restart(reason: str) -> None:
    global _restart_scheduled
    with _restart_lock:
        if _restart_scheduled:
            logger.info("Restart already scheduled; skipping duplicate request")
            return
        _restart_scheduled = True
    threading.Thread(
        target=_restart_process_after_delay,
        args=(reason,),
        name="maxread_restart",
        daemon=True,
    ).start()


def _reply_to_message(message_id: str, text: str, *, reply_in_thread: bool = True) -> None:
    """Send a text reply to the given message, defaulting to thread replies."""
    if not _feishu_client:
        logger.error("Feishu HTTP client not initialized")
        return
    body = (
        ReplyMessageRequestBody.builder()
        .content(json.dumps({"text": text}))
        .msg_type("text")
        .reply_in_thread(reply_in_thread)
        .build()
    )
    req = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
    try:
        resp = call_api(_feishu_api_lock, _feishu_client.im.v1.message.reply, req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning("Reply API code: %s msg: %s", getattr(resp, "code", None), getattr(resp, "msg", ""))
        else:
            logger.info("Replied to %s", message_id)
    except Exception as e:
        logger.exception("Failed to reply: %s", e)


def _add_reaction(message_id: str, emoji_type: str) -> str | None:
    """Add a reaction of the given emoji type; returns the reaction_id."""
    if not _feishu_client:
        return None
    try:
        body = (
            CreateMessageReactionRequestBody.builder()
            .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
            .build()
        )
        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        resp = call_api(_feishu_api_lock, _feishu_client.im.v1.message_reaction.create, req)
        if resp and getattr(resp, "code", 0) == 0:
            reaction_id = getattr(getattr(resp, "data", None), "reaction_id", None)
            logger.info("Added reaction %s to %s", emoji_type, message_id)
            return reaction_id
        logger.warning(
            "Add reaction API code: %s msg: %s",
            getattr(resp, "code", None),
            getattr(resp, "msg", ""),
        )
    except Exception as e:
        logger.warning("Failed to add reaction for %s: %s", message_id, e)
    return None


def _download_feishu_file(message_id: str, file_key: str) -> bytes:
    """Download a file attachment from a Feishu message."""
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    req = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(file_key)
        .type("file")
        .build()
    )
    resp = call_api(_feishu_api_lock, _feishu_client.im.v1.message_resource.get, req)
    if not resp or getattr(resp, "code", -1) != 0:
        raise IOError(
            f"Failed to download file {file_key}: "
            f"code={getattr(resp, 'code', None)}, msg={getattr(resp, 'msg', None)}"
        )
    f = getattr(resp, "file", None)
    if f is None:
        raise IOError(f"No file content in response for {file_key}")
    return f.read()


def _remove_reaction(message_id: str, reaction_id: str | None) -> None:
    """Remove a reaction by its reaction_id."""
    if not _feishu_client or not reaction_id:
        return
    try:
        req = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        resp = call_api(_feishu_api_lock, _feishu_client.im.v1.message_reaction.delete, req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning(
                "Delete reaction API code: %s msg: %s",
                getattr(resp, "code", None),
                getattr(resp, "msg", ""),
            )
        else:
            logger.info("Removed reaction from %s", message_id)
    except Exception as e:
        logger.warning("Failed to remove reaction for %s: %s", message_id, e)


def _send_message(open_id: str, text: str) -> None:
    """Send a direct message to a user by open_id."""
    if not _feishu_client:
        logger.error("Feishu HTTP client not initialized")
        return
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(open_id)
        .content(json.dumps({"text": text}))
        .msg_type("text")
        .build()
    )
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(body)
        .build()
    )
    try:
        resp = call_api(_feishu_api_lock, _feishu_client.im.v1.message.create, req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning("Send message API code: %s msg: %s", getattr(resp, "code", None), getattr(resp, "msg", ""))
        else:
            logger.info("Sent message to %s", open_id)
    except Exception as e:
        logger.exception("Failed to send message: %s", e)


def _send_card_message(open_id: str, card: dict) -> None:
    """Send an interactive card message to a user by open_id."""
    if not _feishu_client:
        logger.error("Feishu HTTP client not initialized")
        return
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(open_id)
        .content(json.dumps(card))
        .msg_type("interactive")
        .build()
    )
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(body)
        .build()
    )
    try:
        resp = call_api(_feishu_api_lock, _feishu_client.im.v1.message.create, req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning("Send card API code: %s msg: %s", getattr(resp, "code", None), getattr(resp, "msg", ""))
        else:
            logger.info("Sent card to %s", open_id)
    except Exception as e:
        logger.exception("Failed to send card: %s", e)


def _build_help_card(md_text: str) -> dict:
    """Parse help.md content into a Feishu interactive card."""
    _EMOJI_MAP = {"[了解]": ":Get:", "[在做了]": ":OnIt:", "[敲键盘]": ":Typing:"}
    for bracket, colon in _EMOJI_MAP.items():
        md_text = md_text.replace(bracket, colon)

    # Split by ━━━ section dividers
    parts = re.split(r"━━━\s*(.*?)\s*━━━", md_text.strip())

    elements = []

    # parts[0] = intro (before first divider)
    intro = parts[0].strip()
    if intro:
        elements.append({"tag": "markdown", "content": intro})

    # Remaining parts come in (title, content) pairs
    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        elements.append({"tag": "hr"})
        section_md = f"**{title}**\n\n{body}" if body else f"**{title}**"
        elements.append({"tag": "markdown", "content": section_md})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📖 读不动了 — 使用帮助"},
            "template": "blue",
        },
        "elements": elements,
    }


def _on_bot_menu(data: P2ApplicationBotMenuV6) -> None:
    """Handle bot custom menu events."""
    try:
        event = data.event
        event_key = getattr(event, "event_key", None) or ""
        operator = getattr(event, "operator", None)
        operator_id = getattr(operator, "operator_id", None) if operator else None
        open_id = getattr(operator_id, "open_id", None) if operator_id else None

        user_name = _fetch_user_name(open_id) if open_id else None
        logger.info("Bot menu event: event_key=%s open_id=%s name=%s", event_key, open_id, user_name)

        if event_key == "Action: help" and open_id:
            _help_file = ROOT / "help.md"
            try:
                _help_text = _help_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                _help_text = "帮助文件不存在，请联系管理员。"
            card = _build_help_card(_help_text)
            _send_card_message(open_id, card)
    except Exception as e:
        logger.exception("Bot menu handler error: %s", e)


def _noop(_) -> None:
    """Ignore unsupported event types (e.g. bot_p2p_chat_entered_v1, message_read_v1)."""
    pass


def _fetch_user_name(open_id: str) -> str | None:
    """Look up a Feishu user's display name by open_id. Returns None on failure."""
    if not _feishu_client or not open_id:
        return None
    try:
        req = (
            GetUserRequest.builder()
            .user_id(open_id)
            .user_id_type("open_id")
            .build()
        )
        resp = call_api(_feishu_api_lock, _feishu_client.contact.v3.user.get, req)
        if resp and getattr(resp, "code", 0) == 0:
            user = getattr(getattr(resp, "data", None), "user", None)
            return getattr(user, "name", None) if user else None
        logger.warning("Fetch user name API code: %s, msg: %s", getattr(resp, "code", None), getattr(resp, "msg", None))
    except Exception as e:
        logger.warning("Failed to fetch user name for %s: %s", open_id, e)
    return None


def _paper_dir_for_arxiv(arxiv_id: str) -> Path:
    """Return the local working directory for a paper."""
    return ROOT / "reader" / "data" / arxiv_id.replace("/", "_")


def _doc_link_metadata_path(arxiv_id: str) -> Path:
    """Return the metadata path used to persist a generated doc link."""
    return _paper_dir_for_arxiv(arxiv_id) / _DOC_LINK_METADATA_FILE


def _load_saved_doc_link(arxiv_id: str) -> str | None:
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


def _save_doc_link(arxiv_id: str, document_id: str, document_url: str, tenant_key: str | None) -> None:
    """Persist the Feishu doc metadata so duplicate requests can reuse it."""
    paper_dir = _paper_dir_for_arxiv(arxiv_id)
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


def _save_query_log(
    arxiv_id: str,
    sender_open_id: str | None,
    sender_name: str | None,
) -> None:
    """Append a query record to queries.json in the paper's data directory."""
    paper_dir = _paper_dir_for_arxiv(arxiv_id)
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


def _save_reply_time(arxiv_id: str, reply_type: str) -> None:
    """Update the last query record in queries.json with reply_time."""
    paper_dir = _paper_dir_for_arxiv(arxiv_id)
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


def _process_one_arxiv_for_message(
    message_id: str,
    tenant_key: str,
    arxiv_id: str,
    *,
    staged_reactions: bool,
    sender_open_id: str | None = None,
    sender_name: str | None = None,
) -> None:
    """Download/summarize one paper and reply. staged_reactions drives Get/OnIt/Typing on this message."""
    logger.info("arXiv id %s for message %s", arxiv_id, message_id)
    reaction_id: str | None = None

    _save_query_log(arxiv_id, sender_open_id, sender_name)

    if staged_reactions:
        reaction_id = _add_reaction(message_id, "Get")

    try:
        existing_doc_link = _load_saved_doc_link(arxiv_id)
        if existing_doc_link:
            logger.info("Reusing saved Feishu doc link for arXiv %s", arxiv_id)
            _reply_to_message(message_id, f"哥，之前的文档在这里 {existing_doc_link}")
            _save_reply_time(arxiv_id, "reused")
            if staged_reactions and reaction_id:
                _remove_reaction(message_id, reaction_id)
                reaction_id = None
            return

        def on_progress(msg: str) -> None:
            nonlocal reaction_id
            msg = msg.strip()
            if not msg:
                return
            logger.info("[%s] %s", arxiv_id, msg)
            if not staged_reactions:
                return
            if msg.startswith("论文文件已准备完成"):
                _remove_reaction(message_id, reaction_id)
                reaction_id = _add_reaction(message_id, "OnIt")

        summary = run_summarize(
            arxiv_id,
            data_dir=ROOT / "reader" / "data",
            mode="claude",
            progress_callback=on_progress,
        )

        if staged_reactions:
            _remove_reaction(message_id, reaction_id)
            reaction_id = _add_reaction(message_id, "Typing")

        title = f"arXiv {arxiv_id} 摘要"
        paper_dir = _paper_dir_for_arxiv(arxiv_id)
        is_pdf_source = (paper_dir / "paper.pdf").is_file()
        is_txt_converted = (paper_dir / "paper.txt").is_file()
        if is_pdf_source and is_txt_converted:
            lines = summary.split("\n", 1)
            summary = lines[0] + "\n\n（该文章通过PDF转换为文字，表格和图文对应可能不准，推荐试试发送arxiv链接）" + ("\n" + lines[1] if len(lines) > 1 else "")
        doc_id = None
        if _feishu_client:
            doc_id = create_summary_doc(
                _feishu_client, title, summary, base_dir=paper_dir, arxiv_id=arxiv_id,
                api_lock=_feishu_api_lock,
            )
        if doc_id:
            document_link = doc_url(doc_id, tenant_key)
            _save_doc_link(arxiv_id, doc_id, document_link, tenant_key)
            _reply_to_message(message_id, f"哥，文档写好了 {document_link}")
            _save_reply_time(arxiv_id, "success")
        else:
            _reply_to_message(message_id, f"哥，文档写好了，但文档链接生成失败（arXiv {arxiv_id}）")
            _save_reply_time(arxiv_id, "success_no_link")
    except ArxivNotFoundError:
        logger.warning("arXiv 404: paper %s not found", arxiv_id)
        _reply_to_message(message_id, f"哥，我文章下载不了（arXiv {arxiv_id}）")
        _save_reply_time(arxiv_id, "not_found")
        return
    except PdfExtractionError as e:
        logger.warning("PDF extraction failed for %s: %s", arxiv_id, e)
        _reply_to_message(message_id, f"哥，这篇文章只有PDF没有源码，文字提取也失败了（arXiv {arxiv_id}）")
        _save_reply_time(arxiv_id, "pdf_extraction_failed")
        return
    except Exception as e:
        logger.exception("Summarize/reply failed: %s", e)
        if _should_restart_for_exception(e):
            _schedule_process_restart(f"Claude CLI crash while handling arXiv {arxiv_id}")
            raise RestartRequestedError(f"Restart requested for arXiv {arxiv_id}") from e
        raise
    finally:
        if staged_reactions and reaction_id:
            _remove_reaction(message_id, reaction_id)


def _process_one_pdf_for_message(
    message_id: str,
    tenant_key: str,
    file_key: str,
    file_name: str,
    *,
    staged_reactions: bool,
    sender_open_id: str | None = None,
    sender_name: str | None = None,
) -> None:
    """Download a PDF from Feishu, summarize it, and reply with a doc link."""
    logger.info("PDF %s for message %s", file_name, message_id)
    reaction_id: str | None = None
    paper_id: str | None = None

    if staged_reactions:
        reaction_id = _add_reaction(message_id, "Get")

    try:
        logger.info("Downloading PDF %s from Feishu...", file_name)
        file_bytes = _download_feishu_file(message_id, file_key)

        file_hash = hashlib.sha256(file_bytes).hexdigest()[:16]
        paper_id = file_hash

        _save_query_log(paper_id, sender_open_id, sender_name)

        existing_doc_link = _load_saved_doc_link(paper_id)
        if existing_doc_link:
            logger.info("Reusing saved Feishu doc link for PDF %s", paper_id)
            _reply_to_message(message_id, f"哥，之前的文档在这里 {existing_doc_link}")
            _save_reply_time(paper_id, "reused")
            if staged_reactions and reaction_id:
                _remove_reaction(message_id, reaction_id)
                reaction_id = None
            return

        paper_dir = _paper_dir_for_arxiv(paper_id)
        paper_dir.mkdir(parents=True, exist_ok=True)
        (paper_dir / "paper.pdf").write_bytes(file_bytes)

        def on_progress(msg: str) -> None:
            nonlocal reaction_id
            msg = msg.strip()
            if not msg:
                return
            logger.info("[%s] %s", paper_id, msg)
            if not staged_reactions:
                return
            if msg.startswith("论文文件已准备完成"):
                _remove_reaction(message_id, reaction_id)
                reaction_id = _add_reaction(message_id, "OnIt")

        summary = run_local_summarize(
            paper_id,
            data_dir=ROOT / "reader" / "data",
            mode="claude",
            progress_callback=on_progress,
        )

        if staged_reactions:
            _remove_reaction(message_id, reaction_id)
            reaction_id = _add_reaction(message_id, "Typing")

        title_base = Path(file_name).stem if file_name else paper_id
        title = f"{title_base} 摘要"
        lines = summary.split("\n", 1)
        summary = lines[0] + "\n\n（该文章通过PDF转换为文字，表格和图文对应可能不准，推荐试试发送arxiv链接）" + ("\n" + lines[1] if len(lines) > 1 else "")

        doc_id = None
        if _feishu_client:
            doc_id = create_summary_doc(
                _feishu_client, title, summary, base_dir=paper_dir,
                api_lock=_feishu_api_lock,
            )
        if doc_id:
            document_link = doc_url(doc_id, tenant_key)
            _save_doc_link(paper_id, doc_id, document_link, tenant_key)
            _reply_to_message(message_id, f"哥，文档写好了 {document_link}")
            _save_reply_time(paper_id, "success")
        else:
            _reply_to_message(message_id, f"哥，文档写好了，但文档链接生成失败（{file_name}）")
            _save_reply_time(paper_id, "success_no_link")
    except PdfExtractionError as e:
        logger.warning("PDF extraction failed for %s: %s", file_name, e)
        _reply_to_message(message_id, f"哥，这个PDF文字提取失败了（{file_name}）")
        if paper_id:
            _save_reply_time(paper_id, "pdf_extraction_failed")
        return
    except Exception as e:
        logger.exception("PDF processing failed for %s: %s", file_name, e)
        if _should_restart_for_exception(e):
            _schedule_process_restart(f"Claude CLI crash while handling PDF {file_name}")
            raise RestartRequestedError(f"Restart requested for PDF {file_name}") from e
        raise
    finally:
        if staged_reactions and reaction_id:
            _remove_reaction(message_id, reaction_id)


def _on_message(data: P2ImMessageReceiveV1) -> None:
    """On Feishu message: if it contains an arXiv link or a PDF file, summarize and reply (work runs in background)."""
    try:
        event = data.event
        message = event.message
        if not message:
            return
        message_id = message.message_id
        content = message.content
        if not content:
            return

        tenant_key = getattr(getattr(data, "header", None), "tenant_key", None) or None
        assert tenant_key, "tenant_key should not be empty"

        sender = getattr(event, "sender", None)
        sender_id_obj = getattr(sender, "sender_id", None) if sender else None
        sender_open_id = getattr(sender_id_obj, "open_id", None) if sender_id_obj else None

        msg_type = getattr(message, "message_type", None) or ""

        # ---- PDF file messages ----
        if msg_type == "file":
            try:
                payload = json.loads(content)
                file_key = payload.get("file_key", "")
                file_name = payload.get("file_name", "")
            except Exception:
                return
            if not file_key or not file_name.lower().endswith(".pdf"):
                return

            with _dedup_lock:
                if message_id in _processed_message_ids:
                    return
                if len(_processed_message_ids) >= _MAX_PROCESSED_IDS:
                    _processed_message_ids.clear()
                _processed_message_ids.add(message_id)

            sender_name = _fetch_user_name(sender_open_id) if sender_open_id else None
            _enqueue_pdf_job(
                message_id, tenant_key, file_key, file_name,
                sender_open_id=sender_open_id, sender_name=sender_name,
            )
            return

        # ---- Text / rich-text messages (arXiv links) ----
        try:
            payload = json.loads(content)
            text = (payload.get("text") or "").strip()
            # Topic groups send messages as rich text (post) with nested content
            if not text and "content" in payload:
                parts = []
                for block in payload["content"]:
                    for elem in block:
                        tag = elem.get("tag")
                        if tag == "text":
                            parts.append(elem.get("text", ""))
                        elif tag == "a":
                            parts.append(elem.get("href", ""))
                text = " ".join(parts).strip()
        except Exception:
            return
        if not text:
            return

        sender_name = _fetch_user_name(sender_open_id) if sender_open_id else None

        ids = extract_arxiv_ids(text)
        if not ids:
            return

        with _dedup_lock:
            if message_id in _processed_message_ids:
                return
            if len(_processed_message_ids) >= _MAX_PROCESSED_IDS:
                _processed_message_ids.clear()
            _processed_message_ids.add(message_id)

        if len(ids) == 1:
            _enqueue_arxiv_job(message_id, tenant_key, ids[0], staged_reactions=True,
                               sender_open_id=sender_open_id, sender_name=sender_name)
            return

        reaction_id = _add_reaction(message_id, "Get")
        _ensure_group_state(message_id, len(ids), reaction_id)
        for arxiv_id in ids:
            _enqueue_arxiv_job(
                message_id,
                tenant_key,
                arxiv_id,
                staged_reactions=False,
                group_message_id=message_id,
                sender_open_id=sender_open_id,
                sender_name=sender_name,
            )
    except Exception as e:
        logger.exception("Handler error: %s", e)


def main() -> None:
    global _feishu_client
    cfg = get_config()
    app_id = cfg.get("app_id") or ""
    app_secret = cfg.get("app_secret") or ""
    if not app_id or not app_secret:
        logger.error("Missing FEISHU_APP_ID or FEISHU_APP_SECRET in feishu/.env")
        sys.exit(1)

    _feishu_client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    _initialize_queue()
    encrypt_key = cfg.get("encrypt_key") or ""
    token = cfg.get("verification_token") or ""
    handler = (
        lark.EventDispatcherHandler.builder(encrypt_key, token, lark.LogLevel.INFO)
        .register_p2_im_message_receive_v1(_on_message)
        .register_p2_application_bot_menu_v6(_on_bot_menu)
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(_noop)
        .register_p2_im_message_message_read_v1(_noop)
        .build()
    )
    ws = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )
    # Lark SDK 默认 120s ping，NAT 网关常在 60-90s 后断连。
    # 缩短 ping 保活、加快断线检测、加快重连。
    ws._ping_interval = 30
    ws._reconnect_interval = 10
    ws._reconnect_nonce = 3
    logger.info("MaxRead 已启动：收到含 arXiv 链接的消息将自动下载并总结后回复。")
    ws.start()


if __name__ == "__main__":
    main()
