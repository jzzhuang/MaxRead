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
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone

# Project root
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.api.im.v1.model.create_message_reaction_request import CreateMessageReactionRequest
from lark_oapi.api.im.v1.model.create_message_reaction_request_body import CreateMessageReactionRequestBody
from lark_oapi.api.im.v1.model.delete_message_reaction_request import DeleteMessageReactionRequest
from lark_oapi.api.im.v1.model.emoji import Emoji
from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
from lark_oapi.api.im.v1.model.reply_message_request_body import ReplyMessageRequestBody

from feishu.config import get_config
from reader.arxiv_summarize import extract_arxiv_ids, run_summarize
from transform.feishu_doc import create_summary_doc, doc_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
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
# At most 2 arXiv jobs run at once; additional work waits in the executor queue (same for message dispatch).
_MAX_PARALLEL = 2
_message_executor = ThreadPoolExecutor(max_workers=_MAX_PARALLEL, thread_name_prefix="feishu_msg")
_arxiv_executor = ThreadPoolExecutor(max_workers=_MAX_PARALLEL, thread_name_prefix="arxiv")


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
        with _feishu_api_lock:
            resp = _feishu_client.im.v1.message.reply(req)
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
        with _feishu_api_lock:
            resp = _feishu_client.im.v1.message_reaction.create(req)
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
        with _feishu_api_lock:
            resp = _feishu_client.im.v1.message_reaction.delete(req)
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


def _noop(_) -> None:
    """Ignore unsupported event types (e.g. bot_p2p_chat_entered_v1, message_read_v1)."""
    pass


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


def _process_one_arxiv_for_message(
    message_id: str,
    tenant_key: str,
    arxiv_id: str,
    *,
    staged_reactions: bool,
) -> None:
    """Download/summarize one paper and reply. staged_reactions drives Get/OnIt/Typing on this message."""
    logger.info("arXiv id %s for message %s", arxiv_id, message_id)
    reaction_id: str | None = None

    if staged_reactions:
        reaction_id = _add_reaction(message_id, "Get")

    try:
        existing_doc_link = _load_saved_doc_link(arxiv_id)
        if existing_doc_link:
            logger.info("Reusing saved Feishu doc link for arXiv %s", arxiv_id)
            _reply_to_message(message_id, f"哥，之前的文档在这里 {existing_doc_link}")
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
        doc_id = None
        if _feishu_client:
            with _feishu_api_lock:
                doc_id = create_summary_doc(
                    _feishu_client, title, summary, base_dir=paper_dir, arxiv_id=arxiv_id
                )
        if doc_id:
            document_link = doc_url(doc_id, tenant_key)
            _save_doc_link(arxiv_id, doc_id, document_link, tenant_key)
            _reply_to_message(message_id, f"哥，文档写好了 {document_link}")
        else:
            _reply_to_message(message_id, f"哥，文档写好了，但文档链接生成失败（arXiv {arxiv_id}）")
    except Exception as e:
        logger.exception("Summarize/reply failed: %s", e)
        _reply_to_message(message_id, f"[arXiv {arxiv_id}] 处理失败: {e!s}")
    finally:
        if staged_reactions and reaction_id:
            _remove_reaction(message_id, reaction_id)


def _handle_message_work(message_id: str, tenant_key: str, ids: list[str]) -> None:
    """Run one or more arXiv jobs: single message uses staged reactions; multiple share a global cap of 2."""
    try:
        if len(ids) == 1:
            fut = _arxiv_executor.submit(
                _process_one_arxiv_for_message,
                message_id,
                tenant_key,
                ids[0],
                staged_reactions=True,
            )
            fut.result()
            return

        reaction_id = _add_reaction(message_id, "Get")
        try:
            futures = [
                _arxiv_executor.submit(
                    _process_one_arxiv_for_message,
                    message_id,
                    tenant_key,
                    aid,
                    staged_reactions=False,
                )
                for aid in ids
            ]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    logger.exception("Parallel arXiv task failed: %s", e)
        finally:
            _remove_reaction(message_id, reaction_id)
    except Exception as e:
        logger.exception("Handler error: %s", e)


def _on_message(data: P2ImMessageReceiveV1) -> None:
    """On Feishu message: if it contains an arXiv link, summarize and reply (work runs in background)."""
    try:
        event = data.event
        message = event.message
        if not message:
            return
        message_id = message.message_id
        content = message.content
        if not content:
            return
        try:
            payload = json.loads(content)
            text = (payload.get("text") or "").strip()
        except Exception:
            return
        if not text:
            return

        tenant_key = getattr(getattr(data, "header", None), "tenant_key", None) or None
        assert tenant_key, "tenant_key should not be empty"

        ids = extract_arxiv_ids(text)
        if not ids:
            return

        with _dedup_lock:
            if message_id in _processed_message_ids:
                return
            if len(_processed_message_ids) >= _MAX_PROCESSED_IDS:
                _processed_message_ids.clear()
            _processed_message_ids.add(message_id)

        _message_executor.submit(_handle_message_work, message_id, tenant_key, ids)
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
    encrypt_key = cfg.get("encrypt_key") or ""
    token = cfg.get("verification_token") or ""
    handler = (
        lark.EventDispatcherHandler.builder(encrypt_key, token, lark.LogLevel.INFO)
        .register_p2_im_message_receive_v1(_on_message)
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
    logger.info("MaxRead 已启动：收到含 arXiv 链接的消息将自动下载并总结后回复。")
    ws.start()


if __name__ == "__main__":
    main()
