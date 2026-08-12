#!/usr/bin/env python3
"""
Listen to Feishu; for any message containing an arXiv link (URL or bare id),
download the TeX source, summarize through the OpenAI Responses API
(output: concise Markdown in summarize.md),
create a cloud doc (云文档) with sections, paragraphs, and equations, then reply with a short completion message.
Each incoming message is handled in a background thread so new messages are accepted while work runs;
a message with multiple arXiv ids processes them with at most two jobs at a time (others queue) and sends one reply per paper.

  python MaxRead.py

Requires: FEISHU_APP_ID, FEISHU_APP_SECRET, and OPENAI_API_KEY.
"""
import hashlib
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from pathlib import Path

# Project root
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import lark_oapi as lark
from lark_oapi.api.application.v6 import P2ApplicationBotMenuV6
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.custom import CustomizedEvent

import comment_handler
import feishu.doc_comments as doc_comments
import feishu.messaging as messaging
import job_queue
import metadata
from feishu.config import get_config
from job_queue import RestartRequestedError
from reader.arxiv_summarize import (
    DEFAULT_MODEL,
    ArxivNotFoundError,
    PdfExtractionError,
    extract_arxiv_ids,
    prepare_paper_dir,
    summarize_prepared_dir,
)
from transform.feishu_doc import create_summary_doc, doc_url

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_log_fmt = logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
))

_log_handlers: list[logging.Handler] = [_console]
_log_file_name = (os.environ.get("MAXREAD_LOG_FILE") or "").strip()
if _log_file_name:
    _file_handler = logging.handlers.RotatingFileHandler(
        Path(_log_file_name).expanduser(),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _file_handler.setFormatter(_log_fmt)
    _log_handlers.append(_file_handler)

logging.basicConfig(level=logging.INFO, handlers=_log_handlers)
logger = logging.getLogger("MaxRead")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_feishu_realtime_client = None
_feishu_doc_client = None
_feishu_realtime_api_lock = threading.Lock()
_feishu_doc_api_lock = threading.Lock()
_DEFAULT_FEISHU_API_TIMEOUT_SECONDS = 30.0

# Deduplicate by message_id (Feishu may deliver the same message event more than once)
_MAX_PROCESSED_IDS = 5000
_processed_message_ids: set[str] = set()
_dedup_lock = threading.Lock()

def _data_dir() -> Path:
    custom = os.environ.get("MAXREAD_DATA_DIR")
    if custom:
        return Path(custom)
    return ROOT / "reader" / "data"


def _feishu_api_timeout_seconds() -> float:
    raw = os.environ.get("FEISHU_API_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_FEISHU_API_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid FEISHU_API_TIMEOUT_SECONDS=%r, using %.0fs",
            raw,
            _DEFAULT_FEISHU_API_TIMEOUT_SECONDS,
        )
        return _DEFAULT_FEISHU_API_TIMEOUT_SECONDS
    return max(1.0, value)


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using %.0fs", name, raw, default)
        return default
    return max(0.0, value)


def _build_feishu_client(app_id: str, app_secret: str, timeout_seconds: float):
    return (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .timeout(timeout_seconds)
        .build()
    )

_RESTART_DELAY_SECONDS = 3
_restart_lock = threading.Lock()
_restart_scheduled = False
_MAX_FEISHU_PDF_BYTES = 50 * 1024 * 1024
_HELP_MENU_COOLDOWN_SECONDS = _env_seconds("MAXREAD_HELP_MENU_COOLDOWN_SECONDS", 7 * 24 * 60 * 60)
_HELP_MENU_SEEN_TTL_SECONDS = _env_seconds("MAXREAD_HELP_MENU_SEEN_TTL_SECONDS", 7 * 24 * 60 * 60)
_HELP_MENU_STATE_FILE = ROOT / "mybot-workspace" / "help_menu_state.json"
_help_menu_lock = threading.Lock()
_help_menu_state_loaded = False
_help_menu_last_sent: dict[str, float] = {}
_help_menu_seen_events: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Help menu deduplication
# ---------------------------------------------------------------------------
def _coerce_float_map(raw: object) -> dict[str, float]:
    values: dict[str, float] = {}
    if not isinstance(raw, dict):
        return values
    for key, value in raw.items():
        try:
            values[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return values


def _help_menu_last_sent_key(open_id: str, event_key: str) -> str:
    return hashlib.sha256(f"{open_id}\0{event_key}".encode()).hexdigest()


def _help_menu_event_signatures(
    open_id: str,
    event_key: str,
    event_id: str | None,
    event_ts: int | str | None,
) -> list[str]:
    signatures: list[str] = []
    if event_id:
        signatures.append("event_id:" + hashlib.sha256(event_id.encode()).hexdigest())
    if event_ts is not None:
        raw = f"{open_id}\0{event_key}\0{event_ts}".encode()
        signatures.append("event_ts:" + hashlib.sha256(raw).hexdigest())
    return signatures


def _seed_help_menu_last_sent_from_logs_locked() -> None:
    # Public releases never parse historical logs for user identifiers.
    return


def _prune_help_menu_state_locked(now: float) -> None:
    seen_cutoff = now - _HELP_MENU_SEEN_TTL_SECONDS
    for key, value in list(_help_menu_seen_events.items()):
        if value < seen_cutoff:
            _help_menu_seen_events.pop(key, None)

    sent_cutoff = now - max(_HELP_MENU_COOLDOWN_SECONDS, _HELP_MENU_SEEN_TTL_SECONDS)
    for key, value in list(_help_menu_last_sent.items()):
        if value < sent_cutoff:
            _help_menu_last_sent.pop(key, None)


def _save_help_menu_state_locked(now: float) -> None:
    _prune_help_menu_state_locked(now)
    try:
        _HELP_MENU_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _HELP_MENU_STATE_FILE.with_name(f"{_HELP_MENU_STATE_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "last_sent": _help_menu_last_sent,
                    "seen_events": _help_menu_seen_events,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        tmp.replace(_HELP_MENU_STATE_FILE)
    except OSError as exc:
        logger.warning("Failed to save help menu state: %s", exc)


def _load_help_menu_state_locked(now: float) -> None:
    global _help_menu_state_loaded
    if _help_menu_state_loaded:
        return
    if _HELP_MENU_STATE_FILE.exists():
        try:
            raw = json.loads(_HELP_MENU_STATE_FILE.read_text(encoding="utf-8"))
            _help_menu_last_sent.update(_coerce_float_map(raw.get("last_sent")))
            _help_menu_seen_events.update(_coerce_float_map(raw.get("seen_events")))
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            logger.warning("Failed to load help menu state, rebuilding from logs: %s", exc)
            _seed_help_menu_last_sent_from_logs_locked()
    else:
        _seed_help_menu_last_sent_from_logs_locked()
    _help_menu_state_loaded = True
    _save_help_menu_state_locked(now)


def _reserve_help_menu_send(
    open_id: str,
    event_key: str,
    event_id: str | None,
    event_ts: int | str | None,
) -> tuple[bool, str]:
    now = time.time()
    signatures = _help_menu_event_signatures(open_id, event_key, event_id, event_ts)
    with _help_menu_lock:
        _load_help_menu_state_locked(now)
        for signature in signatures:
            if signature in _help_menu_seen_events:
                return False, "duplicate event"

        last_sent_key = _help_menu_last_sent_key(open_id, event_key)
        last_sent = _help_menu_last_sent.get(last_sent_key, 0.0)
        elapsed = now - last_sent if last_sent else None
        if (
            _HELP_MENU_COOLDOWN_SECONDS > 0
            and elapsed is not None
            and elapsed < _HELP_MENU_COOLDOWN_SECONDS
        ):
            for signature in signatures:
                _help_menu_seen_events[signature] = now
            _save_help_menu_state_locked(now)
            remaining = int(_HELP_MENU_COOLDOWN_SECONDS - elapsed)
            return False, f"cooldown {remaining}s remaining"

        for signature in signatures:
            _help_menu_seen_events[signature] = now
        _help_menu_last_sent[last_sent_key] = now
        _save_help_menu_state_locked(now)
        return True, "reserved"


def _send_help_card_async(
    open_id: str,
    event_key: str,
    event_id: str | None,
    event_ts: int | str | None,
) -> None:
    def _send() -> None:
        logger.info("Bot menu event: event_key=%s", event_key)
        _help_file = ROOT / "prompts" / "help.md"
        try:
            _help_text = _help_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            _help_text = "帮助文件不存在，请联系管理员。"
        card = messaging.build_help_card(_help_text)
        logger.info("Sending help card: event_key=%s", event_key)
        messaging.send_card_message(open_id, card)

    threading.Thread(
        target=_send,
        name=f"help_menu_{hashlib.sha256(open_id.encode('utf-8')).hexdigest()[:8]}",
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Process restart
# ---------------------------------------------------------------------------
def _should_restart_for_exception(exc: Exception) -> bool:
    """API errors are surfaced to the queue retry policy, not process-restarted."""
    return False


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
    job_queue.set_restart_scheduled(True)
    threading.Thread(
        target=_restart_process_after_delay,
        args=(reason,),
        name="maxread_restart",
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Paper processing
# ---------------------------------------------------------------------------
def _message_targets(
    message_id: str,
    tenant_key: str,
    staged_reactions: bool,
    *,
    force_regen: bool = False,
    extra_waiters: list[dict] | None = None,
) -> list[dict]:
    targets = [{
        "message_id": message_id,
        "tenant_key": tenant_key,
        "staged_reactions": staged_reactions,
        "force_regen": force_regen,
    }]
    seen = {message_id}
    for waiter in extra_waiters or []:
        if not isinstance(waiter, dict):
            continue
        waiter_message_id = str(waiter.get("message_id") or "").strip()
        if not waiter_message_id or waiter_message_id in seen:
            continue
        seen.add(waiter_message_id)
        targets.append({
            "message_id": waiter_message_id,
            "tenant_key": str(waiter.get("tenant_key") or tenant_key),
            "staged_reactions": bool(waiter.get("staged_reactions")),
            "force_regen": bool(waiter.get("force_regen")),
        })
    return targets


class _ReactionFanout:
    def __init__(self, targets: list[dict]):
        self._targets = targets
        self._reaction_ids: dict[str, str | None] = {}

    def add(self, emoji_type: str) -> None:
        for target in self._targets:
            if not target.get("staged_reactions"):
                continue
            message_id = str(target.get("message_id") or "")
            if not message_id:
                continue
            self._reaction_ids[message_id] = messaging.add_reaction(message_id, emoji_type)

    def remove_all(self) -> None:
        for target in self._targets:
            message_id = str(target.get("message_id") or "")
            if not message_id:
                continue
            reaction_id = self._reaction_ids.get(message_id)
            if reaction_id:
                messaging.remove_reaction(message_id, reaction_id)
            self._reaction_ids[message_id] = None

    def switch(self, emoji_type: str) -> None:
        self.remove_all()
        self.add(emoji_type)


def _reply_text_to_targets(targets: list[dict], text) -> None:
    for target in targets:
        message_id = str(target.get("message_id") or "")
        if not message_id:
            continue
        messaging.reply_to_message(message_id, text(target) if callable(text) else text)


def _doc_url_for_target(doc_id: str, target: dict, fallback_tenant_key: str) -> str:
    return doc_url(doc_id, str(target.get("tenant_key") or fallback_tenant_key))


def _process_one_arxiv_for_message(
    message_id: str,
    tenant_key: str,
    arxiv_id: str,
    *,
    staged_reactions: bool,
    force_regen: bool = False,
    extra_waiters: list[dict] | None = None,
) -> None:
    """Download/summarize one paper and reply. staged_reactions drives Get/OnIt/Typing on this message.

    Paper material is summarized through the tool-free OpenAI API and written
    to a private Feishu cloud document.
    """
    targets = _message_targets(
        message_id,
        tenant_key,
        staged_reactions,
        force_regen=force_regen,
        extra_waiters=extra_waiters,
    )
    logger.info(
        "arXiv id %s for message %s (force_regen=%s, waiters=%d)",
        arxiv_id,
        message_id,
        force_regen,
        len(targets) - 1,
    )
    reactions = _ReactionFanout(targets)

    for target in targets:
        metadata.save_query_log(arxiv_id)

    reactions.add("Get")

    try:
        existing_doc_link = metadata.load_saved_doc_link(arxiv_id)
        old_doc_link: str | None = None  # for /regen reply
        if existing_doc_link:
            if any(bool(target.get("force_regen")) for target in targets):
                old_doc_link = existing_doc_link
                logger.info("Force regen for arXiv %s", arxiv_id)
            else:
                logger.info("Reusing saved Feishu doc link for arXiv %s", arxiv_id)
                _reply_text_to_targets(targets, f"哥，之前的文档在这里 {existing_doc_link}")
                metadata.save_reply_times(arxiv_id, "reused", count=len(targets))
                reactions.remove_all()
                return

        # --- Phase 1: download & prepare paper ---
        def on_progress(msg: str) -> None:
            msg = msg.strip()
            if not msg:
                return
            logger.info("[%s] %s", arxiv_id, msg)
            if msg.startswith("论文文件已准备完成"):
                reactions.switch("OnIt")

        paper_dir = prepare_paper_dir(
            arxiv_id,
            data_dir=_data_dir(),
            progress_callback=on_progress,
        )

        model = DEFAULT_MODEL
        summary = summarize_prepared_dir(paper_dir, mode="api", model=model)

        reactions.switch("Typing")

        title = f"arXiv {arxiv_id} 摘要"
        is_pdf_source = (paper_dir / "paper.pdf").is_file()
        is_txt_converted = (paper_dir / "paper.txt").is_file()
        if is_pdf_source and is_txt_converted:
            lines = summary.split("\n", 1)
            summary = lines[0] + "\n\n（该文章通过PDF转换为文字，表格和图文对应可能不准，推荐试试发送arxiv链接）" + ("\n" + lines[1] if len(lines) > 1 else "")
        doc_id = None
        if _feishu_doc_client:
            doc_id = create_summary_doc(
                _feishu_doc_client, title, summary, base_dir=paper_dir, arxiv_id=arxiv_id,
                api_lock=_feishu_doc_api_lock,
            )

        if doc_id:
            document_link = doc_url(doc_id, tenant_key)
            metadata.save_doc_link(arxiv_id, doc_id, document_link, tenant_key, model=model, file_token=doc_id)
            regen_suffix = f"\n旧文档：{old_doc_link}" if old_doc_link else ""
            _reply_text_to_targets(
                targets,
                lambda target: f"哥，文档写好了 {_doc_url_for_target(doc_id, target, tenant_key)}{regen_suffix}",
            )
            metadata.save_reply_times(arxiv_id, "success_regen" if old_doc_link else "success", count=len(targets))
        else:
            _reply_text_to_targets(targets, f"哥，文档写好了，但文档链接生成失败（arXiv {arxiv_id}）")
            metadata.save_reply_times(arxiv_id, "success_no_link", count=len(targets))
    except ArxivNotFoundError:
        logger.warning("arXiv 404: paper %s not found", arxiv_id)
        _reply_text_to_targets(targets, f"哥，我文章下载不了（arXiv {arxiv_id}）")
        metadata.save_reply_times(arxiv_id, "not_found", count=len(targets))
        return
    except PdfExtractionError as e:
        logger.warning("PDF extraction failed for %s: %s", arxiv_id, e)
        _reply_text_to_targets(targets, f"哥，这篇文章只有PDF没有源码，文字提取也失败了（arXiv {arxiv_id}）")
        metadata.save_reply_times(arxiv_id, "pdf_extraction_failed", count=len(targets))
        return
    except Exception as e:
        logger.exception("Summarize/reply failed: %s", e)
        if _should_restart_for_exception(e):
            _schedule_process_restart(f"worker failure while handling arXiv {arxiv_id}")
            raise RestartRequestedError(f"Restart requested for arXiv {arxiv_id}") from e
        raise
    finally:
        reactions.remove_all()


def _process_one_pdf_for_message(
    message_id: str,
    tenant_key: str,
    file_key: str,
    file_name: str,
    *,
    staged_reactions: bool,
    extra_waiters: list[dict] | None = None,
) -> None:
    """Download a PDF from Feishu, summarize it, and reply with a doc link."""
    targets = _message_targets(
        message_id,
        tenant_key,
        staged_reactions,
        extra_waiters=extra_waiters,
    )
    logger.info("PDF %s for message %s (waiters=%d)", file_name, message_id, len(targets) - 1)
    reactions = _ReactionFanout(targets)
    paper_id: str | None = None

    reactions.add("Get")

    try:
        logger.info("Downloading PDF %s from Feishu...", file_name)
        file_bytes = messaging.download_feishu_file(
            message_id, file_key, max_bytes=_MAX_FEISHU_PDF_BYTES
        )
        if len(file_bytes) > _MAX_FEISHU_PDF_BYTES:
            raise ValueError(f"PDF exceeds {_MAX_FEISHU_PDF_BYTES} bytes")

        file_hash = hashlib.sha256(file_bytes).hexdigest()[:16]
        paper_id = file_hash

        for target in targets:
            metadata.save_query_log(paper_id)

        existing_doc_link = metadata.load_saved_doc_link(paper_id)
        if existing_doc_link:
            logger.info("Reusing saved Feishu doc link for PDF %s", paper_id)
            _reply_text_to_targets(targets, f"哥，之前的文档在这里 {existing_doc_link}")
            metadata.save_reply_times(paper_id, "reused", count=len(targets))
            reactions.remove_all()
            return

        paper_dir = _data_dir() / paper_id
        paper_dir.mkdir(parents=True, exist_ok=True)
        (paper_dir / "paper.pdf").write_bytes(file_bytes)

        def on_progress(msg: str) -> None:
            msg = msg.strip()
            if not msg:
                return
            logger.info("[%s] %s", paper_id, msg)
            if msg.startswith("论文文件已准备完成"):
                reactions.switch("OnIt")

        from reader.arxiv_summarize import run_local_summarize
        model = DEFAULT_MODEL
        summary = run_local_summarize(
            paper_id,
            data_dir=_data_dir(),
            mode="api",
            progress_callback=on_progress,
            model=model,
        )

        reactions.switch("Typing")

        title_base = Path(file_name).stem if file_name else paper_id
        title = f"{title_base} 摘要"
        lines = summary.split("\n", 1)
        summary = lines[0] + "\n\n（该文章通过PDF转换为文字，表格和图文对应可能不准，推荐试试发送arxiv链接）" + ("\n" + lines[1] if len(lines) > 1 else "")

        doc_id = None
        if _feishu_doc_client:
            doc_id = create_summary_doc(
                _feishu_doc_client, title, summary, base_dir=paper_dir,
                api_lock=_feishu_doc_api_lock,
            )
        if doc_id:
            document_link = doc_url(doc_id, tenant_key)
            metadata.save_doc_link(paper_id, doc_id, document_link, tenant_key, model=model, file_token=doc_id)
            _reply_text_to_targets(
                targets,
                lambda target: f"哥，文档写好了 {_doc_url_for_target(doc_id, target, tenant_key)}",
            )
            metadata.save_reply_times(paper_id, "success", count=len(targets))
        else:
            _reply_text_to_targets(targets, f"哥，文档写好了，但文档链接生成失败（{file_name}）")
            metadata.save_reply_times(paper_id, "success_no_link", count=len(targets))
    except PdfExtractionError as e:
        logger.warning("PDF extraction failed for %s: %s", file_name, e)
        _reply_text_to_targets(targets, f"哥，这个PDF文字提取失败了（{file_name}）")
        if paper_id:
            metadata.save_reply_times(paper_id, "pdf_extraction_failed", count=len(targets))
        return
    except Exception as e:
        logger.exception("PDF processing failed for %s: %s", file_name, e)
        if _should_restart_for_exception(e):
            _schedule_process_restart(f"worker failure while handling PDF {file_name}")
            raise RestartRequestedError(f"Restart requested for PDF {file_name}") from e
        raise
    finally:
        reactions.remove_all()


# ---------------------------------------------------------------------------
# Feishu event handlers
# ---------------------------------------------------------------------------
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

            job_queue.enqueue_pdf_job(
                message_id, tenant_key, file_key, file_name,
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

        force_regen = False
        if "/regen" in text:
            force_regen = True
            text = text.replace("/regen", "").strip()

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
            job_queue.enqueue_arxiv_job(message_id, tenant_key, ids[0], staged_reactions=True,
                               force_regen=force_regen)
            return

        reaction_id = messaging.add_reaction(message_id, "Get")
        job_queue.ensure_group_state(message_id, len(ids), reaction_id)
        for arxiv_id in ids:
            job_queue.enqueue_arxiv_job(
                message_id,
                tenant_key,
                arxiv_id,
                staged_reactions=False,
                group_message_id=message_id,
                force_regen=force_regen,
            )
    except Exception as e:
        logger.exception("Handler error: %s", e)


def _on_bot_menu(data: P2ApplicationBotMenuV6) -> None:
    """Handle bot custom menu events."""
    try:
        event = data.event
        event_key = getattr(event, "event_key", None) or ""
        event_ts = getattr(event, "timestamp", None) if event else None
        header = getattr(data, "header", None)
        event_id = getattr(header, "event_id", None) if header else None
        operator = getattr(event, "operator", None)
        operator_id = getattr(operator, "operator_id", None) if operator else None
        open_id = getattr(operator_id, "open_id", None) if operator_id else None

        logger.info("Bot menu event received: event_key=%s", event_key)

        if event_key == "Action: help" and open_id:
            should_send, reason = _reserve_help_menu_send(open_id, event_key, event_id, event_ts)
            if not should_send:
                logger.info(
                    "Skipped help card: event_key=%s reason=%s",
                    event_key,
                    reason,
                )
                return
            _send_help_card_async(open_id, event_key, event_id, event_ts)
    except Exception as e:
        logger.exception("Bot menu handler error: %s", e)


# Deduplicate comment events
_MAX_PROCESSED_COMMENT_IDS = 2000
_processed_comment_ids: set[str] = set()
_comment_dedup_lock = threading.Lock()


def _on_doc_comment(data: CustomizedEvent) -> None:
    """Handle document comment events — generate and post AI replies."""
    try:
        event = data.event or {}
        notice_meta = event.get("notice_meta", {})
        file_token = notice_meta.get("file_token", "") or event.get("file_token", "")
        comment_id = str(event.get("comment_id", ""))

        if not file_token or not comment_id:
            logger.warning("Doc comment event missing file_token or comment_id: file_token=%r comment_id=%r", file_token, comment_id)
            return

        # Deduplicate
        dedup_key = f"{file_token}:{comment_id}"
        with _comment_dedup_lock:
            if dedup_key in _processed_comment_ids:
                logger.debug("Doc comment dedup skip: %s", dedup_key)
                return
            if len(_processed_comment_ids) >= _MAX_PROCESSED_COMMENT_IDS:
                _processed_comment_ids.clear()
            _processed_comment_ids.add(dedup_key)

        # Fetch full comment details to get text and quote
        comment_detail = doc_comments.get_comment(file_token, comment_id)
        if not comment_detail:
            logger.warning("Could not fetch comment %s on doc %s", comment_id, file_token)
            return

        if comment_detail.get("is_solved"):
            logger.info("Doc comment %s already solved, skipping", comment_id)
            return

        replies = comment_detail.get("reply_list", [])
        if not replies:
            logger.warning("Doc comment %s has no replies, skipping", comment_id)
            return

        latest_reply = replies[-1]
        comment_text = latest_reply.get("text", "")
        if not comment_text:
            logger.warning("Doc comment %s latest reply has empty text, skipping", comment_id)
            return

        reply_id = latest_reply.get("reply_id", "")
        quote = comment_detail.get("quote", "")

        if latest_reply.get("has_mention", False):
            logger.info("Doc comment %s contains @mention, skipping reply", comment_id)
            return

        threading.Thread(
            target=_handle_comment_safe,
            args=(file_token, comment_id, reply_id, comment_text, quote),
            daemon=True,
        ).start()
    except Exception as e:
        logger.exception("Doc comment handler error: %s", e)


def _handle_comment_safe(
    file_token: str,
    comment_id: str,
    reply_id: str,
    comment_text: str,
    quote: str,
) -> None:
    try:
        # OnIt — AI is generating reply
        if reply_id:
            doc_comments.add_reaction(file_token, reply_id, "OnIt")

        comment_handler.handle_comment(
            file_token,
            comment_id,
            comment_text,
            quote,
            reply_id=reply_id,
        )

        # Done — remove OnIt after successful reply
        if reply_id:
            doc_comments.remove_reaction(file_token, reply_id, "OnIt")
    except Exception as e:
        logger.exception("Comment reply failed for %s/%s: %s", file_token, comment_id, e)
        # Clean up reaction on failure
        if reply_id:
            doc_comments.remove_reaction(file_token, reply_id, "OnIt")


def _noop(_) -> None:
    """Ignore unsupported event types (e.g. bot_p2p_chat_entered_v1, message_read_v1)."""
    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    global _feishu_realtime_client, _feishu_doc_client
    cfg = get_config()
    app_id = cfg.get("app_id") or ""
    app_secret = cfg.get("app_secret") or ""
    if not app_id or not app_secret:
        logger.error("Missing FEISHU_APP_ID or FEISHU_APP_SECRET in environment/.env")
        sys.exit(1)

    feishu_timeout = _feishu_api_timeout_seconds()
    _feishu_realtime_client = _build_feishu_client(app_id, app_secret, feishu_timeout)
    _feishu_doc_client = _build_feishu_client(app_id, app_secret, feishu_timeout)
    logger.info("Feishu API timeout set to %.0fs", feishu_timeout)

    # Initialize modules
    messaging.init(_feishu_realtime_client, _feishu_realtime_api_lock)
    doc_comments.init(_feishu_realtime_client, _feishu_realtime_api_lock)
    metadata.init(ROOT)
    with _help_menu_lock:
        _load_help_menu_state_locked(time.time())
    job_queue.init(
        ROOT,
        process_arxiv=_process_one_arxiv_for_message,
        process_pdf=_process_one_pdf_for_message,
        reply_to_message=messaging.reply_to_message,
        remove_reaction=messaging.remove_reaction,
    )
    job_queue.initialize()

    encrypt_key = cfg.get("encrypt_key") or ""
    token = cfg.get("verification_token") or ""
    handler = (
        lark.EventDispatcherHandler.builder(encrypt_key, token, lark.LogLevel.INFO)
        .register_p2_im_message_receive_v1(_on_message)
        .register_p2_application_bot_menu_v6(_on_bot_menu)
        .register_p2_customized_event("drive.notice.comment_add_v1", _on_doc_comment)
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

    shutdown_triggered = threading.Event()

    def _graceful_shutdown(signum, _frame):
        sig_name = signal.Signals(signum).name
        if shutdown_triggered.is_set():
            logger.warning("收到第二次 %s，强制退出", sig_name)
            sys.exit(1)
        shutdown_triggered.set()
        logger.info("收到 %s，正在优雅退出…", sig_name)
        job_queue.shutdown(timeout=300)
        logger.info("MaxRead 已退出")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    from mcp_server import mcp as mcp_app
    mcp_thread = threading.Thread(target=mcp_app.run, kwargs={"transport": "streamable-http"},
                                  name="mcp_server", daemon=True)
    mcp_thread.start()
    logger.info(
        "MCP server 已启动：http://127.0.0.1:%s/mcp",
        os.environ.get("MCP_PORT", "8765"),
    )

    logger.info("MaxRead 已启动：收到含 arXiv 链接的消息将自动下载并总结后回复。")
    ws.start()


if __name__ == "__main__":
    main()
