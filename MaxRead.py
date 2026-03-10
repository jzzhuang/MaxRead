#!/usr/bin/env python3
"""
Listen to Feishu; for any message containing an arXiv link (URL or bare id),
download the TeX source, summarize with Claude (output: concise Markdown in summarize.md),
create a cloud doc (云文档) with sections, paragraphs, and equations, and reply with the summary and doc link.

  python MaxRead.py

Requires: FEISHU_APP_ID, FEISHU_APP_SECRET in feishu/.env;
  ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY in ~/.claude/settings.json.
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
        try:
            summary = run_summarize(arxiv_id, data_dir=ROOT / "reader" / "data")
            title = f"arXiv {arxiv_id} 摘要"
            doc_id = create_summary_doc(_feishu_client, title, summary) if _feishu_client else None
            if doc_id:
                tenant_key = getattr(getattr(data, "header", None), "tenant_key", None) or None
                reply_text = f"已创建云文档「{title}」，内容：\n\n{summary}\n\n👉 打开文档：{doc_url(doc_id, tenant_key)}"
            else:
                reply_text = f"[arXiv {arxiv_id}]\n{summary}"
            _reply_to_message(message_id, reply_text)
        except Exception as e:
            logger.exception("Summarize/reply failed: %s", e)
            _reply_to_message(message_id, f"[arXiv {arxiv_id}] 处理失败: {e!s}")
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
