#!/usr/bin/env python3
"""
Listen to Feishu; for any message containing an arXiv link (URL or bare id),
download the TeX source, summarize with Cursor Agent by default
(output: concise Markdown in summarize.md),
create a cloud doc (云文档) with sections, paragraphs, and equations, then reply with a short completion message.

  python MaxRead.py

Requires: FEISHU_APP_ID, FEISHU_APP_SECRET in feishu/.env;
  CURSOR_API_KEY in env or cursor_api_key.txt at repo root.
"""
import json
import logging
import sys
from pathlib import Path

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
_PROCESSING_EMOJI = "OK"


def _reply_to_message(message_id: str, text: str) -> None:
    """Send a text reply to the given message."""
    if not _feishu_client:
        logger.error("Feishu HTTP client not initialized")
        return
    body = (
        ReplyMessageRequestBody.builder()
        .content(json.dumps({"text": text}))
        .msg_type("text")
        .build()
    )
    req = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
    try:
        resp = _feishu_client.im.v1.message.reply(req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning("Reply API code: %s msg: %s", getattr(resp, "code", None), getattr(resp, "msg", ""))
        else:
            logger.info("Replied to %s", message_id)
    except Exception as e:
        logger.exception("Failed to reply: %s", e)


def _add_processing_reaction(message_id: str) -> str | None:
    """Add a temporary reaction to indicate processing started."""
    if not _feishu_client:
        return None
    try:
        body = (
            CreateMessageReactionRequestBody.builder()
            .reaction_type(Emoji.builder().emoji_type(_PROCESSING_EMOJI).build())
            .build()
        )
        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        resp = _feishu_client.im.v1.message_reaction.create(req)
        if resp and getattr(resp, "code", 0) == 0:
            reaction_id = getattr(getattr(resp, "data", None), "reaction_id", None)
            logger.info("Added processing reaction to %s", message_id)
            return reaction_id
        logger.warning(
            "Add reaction API code: %s msg: %s",
            getattr(resp, "code", None),
            getattr(resp, "msg", ""),
        )
    except Exception as e:
        logger.warning("Failed to add reaction for %s: %s", message_id, e)
    return None


def _remove_processing_reaction(message_id: str, reaction_id: str | None) -> None:
    """Remove the temporary processing reaction."""
    if not _feishu_client or not reaction_id:
        return
    try:
        req = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        resp = _feishu_client.im.v1.message_reaction.delete(req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning(
                "Delete reaction API code: %s msg: %s",
                getattr(resp, "code", None),
                getattr(resp, "msg", ""),
            )
        else:
            logger.info("Removed processing reaction from %s", message_id)
    except Exception as e:
        logger.warning("Failed to remove reaction for %s: %s", message_id, e)


def _noop(_) -> None:
    """Ignore unsupported event types (e.g. bot_p2p_chat_entered_v1, message_read_v1)."""
    pass


def _on_message(data: P2ImMessageReceiveV1) -> None:
    """On Feishu message: if it contains an arXiv link, summarize and reply."""
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

        ids = extract_arxiv_ids(text)
        if not ids:
            return

        if message_id in _processed_message_ids:
            return
        if len(_processed_message_ids) >= _MAX_PROCESSED_IDS:
            _processed_message_ids.clear()
        _processed_message_ids.add(message_id)

        # Use first arXiv id only (one summary per message)
        arxiv_id = ids[0]
        logger.info("arXiv id %s from message %s", arxiv_id, message_id)
        reaction_id = _add_processing_reaction(message_id)
        try:
            summary = run_summarize(arxiv_id, data_dir=ROOT / "reader" / "data")
            title = f"arXiv {arxiv_id} 摘要"
            paper_dir = ROOT / "reader" / "data" / arxiv_id.replace("/", "_")
            if _feishu_client:
                _ = create_summary_doc(_feishu_client, title, summary, base_dir=paper_dir)
            _reply_to_message(message_id, f"哥我文档写好了 {doc_url(doc_id, tenant_key)}")
        except Exception as e:
            logger.exception("Summarize/reply failed: %s", e)
            _reply_to_message(message_id, f"[arXiv {arxiv_id}] 处理失败: {e!s}")
        finally:
            _remove_processing_reaction(message_id, reaction_id)
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
