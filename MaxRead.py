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
import hashlib
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path

# Project root
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.api.application.v6 import P2ApplicationBotMenuV6

from feishu.config import get_config
import feishu.messaging as messaging
import metadata
import job_queue
import illustration
from job_queue import RestartRequestedError

from reader.arxiv_summarize import (
    ArxivNotFoundError, PdfExtractionError, extract_arxiv_ids,
    prepare_paper_dir, summarize_prepared_dir, launch_claude_for_illustration,
)
from transform.feishu_doc import create_summary_doc, doc_url

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_feishu_client = None
_feishu_api_lock = threading.Lock()

# Deduplicate by message_id (Feishu may deliver the same message event more than once)
_MAX_PROCESSED_IDS = 5000
_processed_message_ids: set[str] = set()
_dedup_lock = threading.Lock()

_RESTART_DELAY_SECONDS = 3
_restart_lock = threading.Lock()
_restart_scheduled = False
_OWNER_OPEN_ID = "ou_fa85f3ecd5572996940d6f1185aa2a24"  # 天择 — only owner gets illustration


# ---------------------------------------------------------------------------
# Process restart
# ---------------------------------------------------------------------------
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
def _build_illustration_card(
    image_key: str,
    arxiv_id: str,
    *,
    caption: str = "",
    doc_link: str | None = None,
) -> dict:
    elements: list[dict] = [
        {"tag": "img", "img_key": image_key, "alt": {"tag": "plain_text", "content": "illustration"}},
    ]
    if caption:
        elements.append({"tag": "markdown", "content": caption})
    if doc_link:
        elements.append({"tag": "markdown", "content": f"哥，文档写好了 [点击查看]({doc_link})"})
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"arXiv {arxiv_id}"},
                "template": "green",
            },
            "body": {"elements": elements},
        }
    elements.append({"tag": "markdown", "content": "正在生成文档，预计还需几分钟 ⏳"})
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"arXiv {arxiv_id}"},
            "template": "indigo",
        },
        "body": {"elements": elements},
    }


def _process_one_arxiv_for_message(
    message_id: str,
    tenant_key: str,
    arxiv_id: str,
    *,
    staged_reactions: bool,
    sender_open_id: str | None = None,
    sender_name: str | None = None,
) -> None:
    """Download/summarize one paper and reply. staged_reactions drives Get/OnIt/Typing on this message.

    After download, two Claude processes run in parallel:
      1. Summary → Feishu cloud doc
      2. Illustration prompt → nano_banana → image
    The reply is sent once both are ready (illustration is best-effort).
    """
    logger.info("arXiv id %s for message %s", arxiv_id, message_id)
    reaction_id: str | None = None

    metadata.save_query_log(arxiv_id, sender_open_id, sender_name)

    if staged_reactions:
        reaction_id = messaging.add_reaction(message_id, "Get")

    try:
        existing_doc_link = metadata.load_saved_doc_link(arxiv_id)
        if existing_doc_link:
            logger.info("Reusing saved Feishu doc link for arXiv %s", arxiv_id)
            messaging.reply_to_message(message_id, f"哥，之前的文档在这里 {existing_doc_link}")
            metadata.save_reply_time(arxiv_id, "reused")
            if staged_reactions and reaction_id:
                messaging.remove_reaction(message_id, reaction_id)
                reaction_id = None
            return

        # --- Phase 1: download & prepare paper ---
        def on_progress(msg: str) -> None:
            nonlocal reaction_id
            msg = msg.strip()
            if not msg:
                return
            logger.info("[%s] %s", arxiv_id, msg)
            if not staged_reactions:
                return
            if msg.startswith("论文文件已准备完成"):
                messaging.remove_reaction(message_id, reaction_id)
                reaction_id = messaging.add_reaction(message_id, "OnIt")

        paper_dir = prepare_paper_dir(
            arxiv_id,
            data_dir=ROOT / "reader" / "data",
            progress_callback=on_progress,
        )

        # --- Phase 2: run summary + illustration in parallel ---
        illustration_result: dict = {"image_key": None, "caption": "", "card_message_id": None}
        illust_thread = None

        if sender_open_id == _OWNER_OPEN_ID:
            def _illustration_worker() -> None:
                image_key, caption = illustration.run_illustration_pipeline(paper_dir, arxiv_id)
                illustration_result["image_key"] = image_key
                illustration_result["caption"] = caption
                if image_key:
                    card = _build_illustration_card(image_key, arxiv_id, caption=caption)
                    card_msg_id = messaging.reply_with_card(message_id, card)
                    illustration_result["card_message_id"] = card_msg_id

            illust_thread = threading.Thread(
                target=_illustration_worker,
                name=f"illust_{arxiv_id}",
                daemon=True,
            )
            illust_thread.start()

        # Summary runs in the current thread
        summary = summarize_prepared_dir(paper_dir, mode="claude")

        if staged_reactions:
            messaging.remove_reaction(message_id, reaction_id)
            reaction_id = messaging.add_reaction(message_id, "Typing")

        title = f"arXiv {arxiv_id} 摘要"
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

        # --- Phase 3: wait for illustration, then send or update reply ---
        if illust_thread is not None:
            illust_thread.join(timeout=300)
        image_key = illustration_result["image_key"]
        card_message_id = illustration_result["card_message_id"]

        if doc_id:
            document_link = doc_url(doc_id, tenant_key)
            metadata.save_doc_link(arxiv_id, doc_id, document_link, tenant_key)
            caption = illustration_result["caption"]
            if card_message_id:
                card = _build_illustration_card(image_key, arxiv_id, caption=caption, doc_link=document_link)
                messaging.update_card(card_message_id, card)
            elif image_key:
                messaging.reply_with_doc_and_image(message_id, document_link, image_key)
            else:
                messaging.reply_to_message(message_id, f"哥，文档写好了 {document_link}")
            metadata.save_reply_time(arxiv_id, "success")
        else:
            messaging.reply_to_message(message_id, f"哥，文档写好了，但文档链接生成失败（arXiv {arxiv_id}）")
            metadata.save_reply_time(arxiv_id, "success_no_link")
    except ArxivNotFoundError:
        logger.warning("arXiv 404: paper %s not found", arxiv_id)
        messaging.reply_to_message(message_id, f"哥，我文章下载不了（arXiv {arxiv_id}）")
        metadata.save_reply_time(arxiv_id, "not_found")
        return
    except PdfExtractionError as e:
        logger.warning("PDF extraction failed for %s: %s", arxiv_id, e)
        messaging.reply_to_message(message_id, f"哥，这篇文章只有PDF没有源码，文字提取也失败了（arXiv {arxiv_id}）")
        metadata.save_reply_time(arxiv_id, "pdf_extraction_failed")
        return
    except Exception as e:
        logger.exception("Summarize/reply failed: %s", e)
        if _should_restart_for_exception(e):
            _schedule_process_restart(f"Claude CLI crash while handling arXiv {arxiv_id}")
            raise RestartRequestedError(f"Restart requested for arXiv {arxiv_id}") from e
        raise
    finally:
        if staged_reactions and reaction_id:
            messaging.remove_reaction(message_id, reaction_id)


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
        reaction_id = messaging.add_reaction(message_id, "Get")

    try:
        logger.info("Downloading PDF %s from Feishu...", file_name)
        file_bytes = messaging.download_feishu_file(message_id, file_key)

        file_hash = hashlib.sha256(file_bytes).hexdigest()[:16]
        paper_id = file_hash

        metadata.save_query_log(paper_id, sender_open_id, sender_name)

        existing_doc_link = metadata.load_saved_doc_link(paper_id)
        if existing_doc_link:
            logger.info("Reusing saved Feishu doc link for PDF %s", paper_id)
            messaging.reply_to_message(message_id, f"哥，之前的文档在这里 {existing_doc_link}")
            metadata.save_reply_time(paper_id, "reused")
            if staged_reactions and reaction_id:
                messaging.remove_reaction(message_id, reaction_id)
                reaction_id = None
            return

        paper_dir = ROOT / "reader" / "data" / paper_id
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
                messaging.remove_reaction(message_id, reaction_id)
                reaction_id = messaging.add_reaction(message_id, "OnIt")

        from reader.arxiv_summarize import run_local_summarize
        summary = run_local_summarize(
            paper_id,
            data_dir=ROOT / "reader" / "data",
            mode="claude",
            progress_callback=on_progress,
        )

        if staged_reactions:
            messaging.remove_reaction(message_id, reaction_id)
            reaction_id = messaging.add_reaction(message_id, "Typing")

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
            metadata.save_doc_link(paper_id, doc_id, document_link, tenant_key)
            messaging.reply_to_message(message_id, f"哥，文档写好了 {document_link}")
            metadata.save_reply_time(paper_id, "success")
        else:
            messaging.reply_to_message(message_id, f"哥，文档写好了，但文档链接生成失败（{file_name}）")
            metadata.save_reply_time(paper_id, "success_no_link")
    except PdfExtractionError as e:
        logger.warning("PDF extraction failed for %s: %s", file_name, e)
        messaging.reply_to_message(message_id, f"哥，这个PDF文字提取失败了（{file_name}）")
        if paper_id:
            metadata.save_reply_time(paper_id, "pdf_extraction_failed")
        return
    except Exception as e:
        logger.exception("PDF processing failed for %s: %s", file_name, e)
        if _should_restart_for_exception(e):
            _schedule_process_restart(f"Claude CLI crash while handling PDF {file_name}")
            raise RestartRequestedError(f"Restart requested for PDF {file_name}") from e
        raise
    finally:
        if staged_reactions and reaction_id:
            messaging.remove_reaction(message_id, reaction_id)


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

            sender_name = messaging.fetch_user_name(sender_open_id) if sender_open_id else None
            job_queue.enqueue_pdf_job(
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

        sender_name = messaging.fetch_user_name(sender_open_id) if sender_open_id else None

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
                               sender_open_id=sender_open_id, sender_name=sender_name)
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
                sender_open_id=sender_open_id,
                sender_name=sender_name,
            )
    except Exception as e:
        logger.exception("Handler error: %s", e)


def _on_bot_menu(data: P2ApplicationBotMenuV6) -> None:
    """Handle bot custom menu events."""
    try:
        event = data.event
        event_key = getattr(event, "event_key", None) or ""
        operator = getattr(event, "operator", None)
        operator_id = getattr(operator, "operator_id", None) if operator else None
        open_id = getattr(operator_id, "open_id", None) if operator_id else None

        user_name = messaging.fetch_user_name(open_id) if open_id else None
        logger.info("Bot menu event: event_key=%s open_id=%s name=%s", event_key, open_id, user_name)

        if event_key == "Action: help" and open_id:
            _help_file = ROOT / "prompts" / "help.md"
            try:
                _help_text = _help_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                _help_text = "帮助文件不存在，请联系管理员。"
            card = messaging.build_help_card(_help_text)
            messaging.send_card_message(open_id, card)
    except Exception as e:
        logger.exception("Bot menu handler error: %s", e)


def _noop(_) -> None:
    """Ignore unsupported event types (e.g. bot_p2p_chat_entered_v1, message_read_v1)."""
    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    global _feishu_client
    cfg = get_config()
    app_id = cfg.get("app_id") or ""
    app_secret = cfg.get("app_secret") or ""
    if not app_id or not app_secret:
        logger.error("Missing FEISHU_APP_ID or FEISHU_APP_SECRET in feishu/.env")
        sys.exit(1)

    _feishu_client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    # Initialize modules
    messaging.init(_feishu_client, _feishu_api_lock)
    metadata.init(ROOT)
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
