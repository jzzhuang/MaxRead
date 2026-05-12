"""
Feishu messaging: reply, reactions, cards, file download, image upload, user lookup.

Call ``init(client, api_lock)`` before using any other function in this module.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
    PatchMessageRequest, PatchMessageRequestBody,
)
from lark_oapi.api.im.v1.model.create_message_reaction_request import CreateMessageReactionRequest
from lark_oapi.api.im.v1.model.create_message_reaction_request_body import CreateMessageReactionRequestBody
from lark_oapi.api.im.v1.model.delete_message_reaction_request import DeleteMessageReactionRequest
from lark_oapi.api.im.v1.model.emoji import Emoji
from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
from lark_oapi.api.im.v1.model.reply_message_request_body import ReplyMessageRequestBody
from lark_oapi.api.contact.v3 import GetUserRequest

from feishu.resilient import call_api

logger = logging.getLogger("MaxRead")

# Module-level state set by init()
_client = None
_api_lock: threading.Lock | None = None


def init(client, api_lock: threading.Lock) -> None:
    """Initialize the messaging module with a Feishu client and API lock."""
    global _client, _api_lock
    _client = client
    _api_lock = api_lock


# ---------------------------------------------------------------------------
# Reply & reactions
# ---------------------------------------------------------------------------
def reply_to_message(message_id: str, text: str, *, reply_in_thread: bool = True) -> None:
    """Send a text reply to the given message, defaulting to thread replies."""
    if not _client:
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
        resp = call_api(_api_lock, _client.im.v1.message.reply, req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning("Reply API code: %s msg: %s", getattr(resp, "code", None), getattr(resp, "msg", ""))
        else:
            logger.info("Replied to %s", message_id)
    except Exception as e:
        logger.exception("Failed to reply: %s", e)


def add_reaction(message_id: str, emoji_type: str) -> str | None:
    """Add a reaction of the given emoji type; returns the reaction_id."""
    if not _client:
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
        resp = call_api(_api_lock, _client.im.v1.message_reaction.create, req)
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


def remove_reaction(message_id: str, reaction_id: str | None) -> None:
    """Remove a reaction by its reaction_id."""
    if not _client or not reaction_id:
        return
    try:
        req = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        resp = call_api(_api_lock, _client.im.v1.message_reaction.delete, req)
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


# ---------------------------------------------------------------------------
# Direct messages & cards
# ---------------------------------------------------------------------------
def send_message(open_id: str, text: str) -> None:
    """Send a direct message to a user by open_id."""
    if not _client:
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
        resp = call_api(_api_lock, _client.im.v1.message.create, req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning("Send message API code: %s msg: %s", getattr(resp, "code", None), getattr(resp, "msg", ""))
        else:
            logger.info("Sent message to %s", open_id)
    except Exception as e:
        logger.exception("Failed to send message: %s", e)


def reply_with_card(
    message_id: str,
    card: dict,
    *,
    reply_in_thread: bool = True,
) -> str | None:
    """Reply to a message with an interactive card. Returns the sent message_id."""
    if not _client:
        logger.error("Feishu HTTP client not initialized")
        return None
    body = (
        ReplyMessageRequestBody.builder()
        .content(json.dumps(card))
        .msg_type("interactive")
        .reply_in_thread(reply_in_thread)
        .build()
    )
    req = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
    try:
        resp = call_api(_api_lock, _client.im.v1.message.reply, req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning("Reply card API code: %s msg: %s",
                           getattr(resp, "code", None), getattr(resp, "msg", ""))
            return None
        sent_id = getattr(getattr(resp, "data", None), "message_id", None)
        logger.info("Replied card to %s, sent_id=%s", message_id, sent_id)
        return sent_id
    except Exception as e:
        logger.exception("Failed to reply with card: %s", e)
        return None


def update_card(message_id: str, card: dict) -> None:
    """Update an existing card message by replacing its content."""
    if not _client:
        logger.error("Feishu HTTP client not initialized")
        return
    body = (
        PatchMessageRequestBody.builder()
        .content(json.dumps(card))
        .build()
    )
    req = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
    try:
        resp = call_api(_api_lock, _client.im.v1.message.patch, req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning("Update card API code: %s msg: %s",
                           getattr(resp, "code", None), getattr(resp, "msg", ""))
        else:
            logger.info("Updated card %s", message_id)
    except Exception as e:
        logger.exception("Failed to update card: %s", e)


def send_card_message(open_id: str, card: dict) -> None:
    """Send an interactive card message to a user by open_id."""
    if not _client:
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
        resp = call_api(_api_lock, _client.im.v1.message.create, req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning("Send card API code: %s msg: %s", getattr(resp, "code", None), getattr(resp, "msg", ""))
        else:
            logger.info("Sent card to %s", open_id)
    except Exception as e:
        logger.exception("Failed to send card: %s", e)


# ---------------------------------------------------------------------------
# Help card builder
# ---------------------------------------------------------------------------
def build_help_card(md_text: str) -> dict:
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


# ---------------------------------------------------------------------------
# File download & image upload
# ---------------------------------------------------------------------------
def download_feishu_file(message_id: str, file_key: str) -> bytes:
    """Download a file attachment from a Feishu message."""
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    req = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(file_key)
        .type("file")
        .build()
    )
    resp = call_api(_api_lock, _client.im.v1.message_resource.get, req)
    if not resp or getattr(resp, "code", -1) != 0:
        raise IOError(
            f"Failed to download file {file_key}: "
            f"code={getattr(resp, 'code', None)}, msg={getattr(resp, 'msg', None)}"
        )
    f = getattr(resp, "file", None)
    if f is None:
        raise IOError(f"No file content in response for {file_key}")
    return f.read()


def upload_im_image(image_path: Path) -> str | None:
    """Upload an image via Feishu IM API and return the image_key."""
    if not _client:
        logger.error("Feishu HTTP client not initialized for image upload")
        return None
    try:
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody
        body = CreateImageRequestBody.builder().image_type("message").image(image_path.open("rb")).build()
        req = CreateImageRequest.builder().request_body(body).build()
        resp = call_api(_api_lock, _client.im.v1.image.create, req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning("Upload IM image failed: code=%s msg=%s",
                           getattr(resp, "code", None), getattr(resp, "msg", ""))
            return None
        image_key = getattr(getattr(resp, "data", None), "image_key", None)
        if image_key:
            logger.info("Uploaded IM image: %s → %s", image_path.name, image_key)
        return image_key
    except Exception as e:
        logger.exception("Failed to upload IM image: %s", e)
        return None


def reply_with_doc_and_image(
    message_id: str,
    doc_link: str,
    image_key: str | None,
    *,
    reply_in_thread: bool = True,
) -> None:
    """Reply with a rich-text (post) message containing the doc link and optionally an illustration."""
    if not _client:
        logger.error("Feishu HTTP client not initialized")
        return

    content_lines: list[list[dict]] = [
        [{"tag": "text", "text": "哥，文档写好了 "}, {"tag": "a", "text": "点击查看", "href": doc_link}],
    ]
    if image_key:
        content_lines.append([{"tag": "img", "image_key": image_key}])

    post_content = json.dumps({"zh_cn": {"content": content_lines}})
    body = (
        ReplyMessageRequestBody.builder()
        .content(post_content)
        .msg_type("post")
        .reply_in_thread(reply_in_thread)
        .build()
    )
    req = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
    try:
        resp = call_api(_api_lock, _client.im.v1.message.reply, req)
        if resp and getattr(resp, "code", 0) != 0:
            logger.warning("Reply post API code: %s msg: %s",
                           getattr(resp, "code", None), getattr(resp, "msg", ""))
        else:
            logger.info("Replied to %s with doc+image", message_id)
    except Exception as e:
        logger.exception("Failed to reply with doc+image: %s", e)


# ---------------------------------------------------------------------------
# User lookup
# ---------------------------------------------------------------------------
def fetch_user_name(open_id: str) -> str | None:
    """Look up a Feishu user's display name by open_id. Returns None on failure."""
    if not _client or not open_id:
        return None
    try:
        req = (
            GetUserRequest.builder()
            .user_id(open_id)
            .user_id_type("open_id")
            .build()
        )
        resp = call_api(_api_lock, _client.contact.v3.user.get, req)
        if resp and getattr(resp, "code", 0) == 0:
            user = getattr(getattr(resp, "data", None), "user", None)
            return getattr(user, "name", None) if user else None
        logger.warning("Fetch user name API code: %s, msg: %s", getattr(resp, "code", None), getattr(resp, "msg", None))
    except Exception as e:
        logger.warning("Failed to fetch user name for %s: %s", open_id, e)
    return None
