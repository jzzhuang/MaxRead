"""Feishu Drive comment API: get comments, post replies, subscribe to events."""
from __future__ import annotations

import logging
import threading
from typing import Optional

from lark_oapi.api.drive.v1 import (
    GetFileCommentRequest,
    ListFileCommentRequest,
    SubscribeFileRequest,
)

from .resilient import call_api

logger = logging.getLogger(__name__)

_client = None
_api_lock: Optional[threading.Lock] = None


def init(client, api_lock: threading.Lock) -> None:
    global _client, _api_lock
    _client = client
    _api_lock = api_lock


def get_comment(file_token: str, comment_id: str) -> Optional[dict]:
    """Fetch a single comment's details (quote, reply_list, etc.).

    Tries the single-comment GET first; if it returns 1069307 (not exist),
    falls back to listing all comments and filtering — the list API is more
    reliable for freshly-created comments.
    """
    req = (
        GetFileCommentRequest.builder()
        .file_token(file_token)
        .comment_id(int(comment_id))
        .file_type("docx")
        .user_id_type("open_id")
        .build()
    )
    resp = call_api(_api_lock, _client.drive.v1.file_comment.get, req)
    if getattr(resp, "code", 0) == 0:
        data = getattr(resp, "data", None)
        comment = getattr(data, "comment", None) if data else None
        if comment:
            return {
                "comment_id": getattr(comment, "comment_id", ""),
                "quote": getattr(comment, "quote", ""),
                "is_solved": getattr(comment, "is_solved", False),
                "reply_list": _parse_reply_list(comment),
            }

    # Fallback: list all comments and find the one we want
    logger.info("get_comment falling back to list for comment %s on doc %s", comment_id, file_token)
    return _get_comment_via_list(file_token, comment_id)


def _get_comment_via_list(file_token: str, comment_id: str) -> Optional[dict]:
    """Fetch a comment by listing all comments and filtering by ID."""
    req = (
        ListFileCommentRequest.builder()
        .file_token(file_token)
        .file_type("docx")
        .user_id_type("open_id")
        .build()
    )
    resp = call_api(_api_lock, _client.drive.v1.file_comment.list, req)
    if getattr(resp, "code", 0) != 0:
        logger.warning("list_comments failed: %s %s", getattr(resp, "code", ""), getattr(resp, "msg", ""))
        return None
    data = getattr(resp, "data", None)
    items = getattr(data, "items", []) if data else []
    if not items:
        return None
    for comment in items:
        if str(getattr(comment, "comment_id", "")) == str(comment_id):
            return {
                "comment_id": getattr(comment, "comment_id", ""),
                "quote": getattr(comment, "quote", ""),
                "is_solved": getattr(comment, "is_solved", False),
                "reply_list": _parse_reply_list(comment),
            }
    logger.warning("Comment %s not found in list for doc %s", comment_id, file_token)
    return None


def _parse_reply_list(comment) -> list[dict]:
    reply_list_obj = getattr(comment, "reply_list", None)
    if not reply_list_obj:
        return []
    replies = getattr(reply_list_obj, "replies", None)
    if not replies:
        return []
    result = []
    for r in replies:
        text = _extract_reply_text(r)
        result.append({
            "reply_id": getattr(r, "reply_id", ""),
            "text": text,
            "has_mention": _has_mention(r),
        })
    return result


def _has_mention(reply) -> bool:
    content = getattr(reply, "content", None)
    if not content:
        return False
    elements = getattr(content, "elements", None)
    if not elements:
        return False
    return any(getattr(el, "type", "") == "mention_user" for el in elements)


def _extract_reply_text(reply) -> str:
    content = getattr(reply, "content", None)
    if not content:
        return ""
    elements = getattr(content, "elements", None)
    if not elements:
        return ""
    parts = []
    for el in elements:
        if getattr(el, "type", "") == "text_run":
            tr = getattr(el, "text_run", None)
            if tr:
                parts.append(getattr(tr, "text", ""))
    return "".join(parts)


def post_reply(file_token: str, comment_id: str, text: str) -> bool:
    """Post a reply to a file comment via raw HTTP (SDK RawRequest not available)."""
    return bool(post_reply_result(file_token, comment_id, text).get("ok"))


def post_reply_result(file_token: str, comment_id: str, text: str) -> dict:
    """Post a reply and return a structured result for audit logging."""
    import requests as _requests

    token = _get_tenant_token()
    if not token:
        logger.warning("post_reply: could not get tenant access token")
        return {"ok": False, "error": "could_not_get_tenant_access_token"}

    url = (
        f"https://open.feishu.cn/open-apis/drive/v1/files/{file_token}"
        f"/comments/{comment_id}/replies?file_type=docx&user_id_type=open_id"
    )
    body = {
        "content": {
            "elements": [
                {"type": "text_run", "text_run": {"text": text}}
            ]
        }
    }
    try:
        resp = _requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=15)
        try:
            data = resp.json()
        except Exception as e:
            logger.warning("post_reply invalid JSON response: %s", e)
            return {
                "ok": False,
                "http_status": getattr(resp, "status_code", None),
                "error": "invalid_json_response",
                "response_text": getattr(resp, "text", "")[:1000],
            }
        if data.get("code", -1) != 0:
            logger.warning("post_reply failed: %s %s", data.get("code"), data.get("msg"))
            return {
                "ok": False,
                "http_status": getattr(resp, "status_code", None),
                "code": data.get("code"),
                "msg": data.get("msg"),
                "response": data,
            }
        return {
            "ok": True,
            "http_status": getattr(resp, "status_code", None),
            "response": data,
            "data": data.get("data"),
        }
    except Exception as e:
        logger.warning("post_reply exception: %s", e)
        return {"ok": False, "error": str(e)}


def _get_tenant_token() -> Optional[str]:
    """Get a tenant access token for raw HTTP calls."""
    import requests as _requests

    from .config import get_config
    cfg = get_config()
    try:
        resp = _requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            return data.get("tenant_access_token", "")
    except Exception as e:
        logger.warning("_get_tenant_token failed: %s", e)
    return None


def add_reaction(file_token: str, reply_id: str, reaction_type: str) -> bool:
    """Add an emoji reaction to a comment reply (drive v2 API)."""
    import requests as _requests

    token = _get_tenant_token()
    if not token:
        logger.warning("add_reaction: could not get tenant access token")
        return False

    url = (
        f"https://open.feishu.cn/open-apis/drive/v2/files/{file_token}"
        f"/comments/reaction?file_type=docx"
    )
    body = {"action": "add", "reply_id": reply_id, "reaction_type": reaction_type}
    try:
        resp = _requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=15)
        data = resp.json()
        if data.get("code", -1) != 0:
            logger.warning("add_reaction failed: %s %s", data.get("code"), data.get("msg"))
            return False
        return True
    except Exception as e:
        logger.warning("add_reaction exception: %s", e)
        return False


def remove_reaction(file_token: str, reply_id: str, reaction_type: str) -> bool:
    """Remove an emoji reaction from a comment reply (drive v2 API)."""
    import requests as _requests

    token = _get_tenant_token()
    if not token:
        logger.warning("remove_reaction: could not get tenant access token")
        return False

    url = (
        f"https://open.feishu.cn/open-apis/drive/v2/files/{file_token}"
        f"/comments/reaction?file_type=docx"
    )
    body = {"action": "delete", "reply_id": reply_id, "reaction_type": reaction_type}
    try:
        resp = _requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=15)
        data = resp.json()
        if data.get("code", -1) != 0:
            logger.warning("remove_reaction failed: %s %s", data.get("code"), data.get("msg"))
            return False
        return True
    except Exception as e:
        logger.warning("remove_reaction exception: %s", e)
        return False


def subscribe_doc_comments(file_token: str) -> bool:
    """Subscribe to comment events for a specific document."""
    req = (
        SubscribeFileRequest.builder()
        .file_token(file_token)
        .file_type("docx")
        .build()
    )
    resp = call_api(_api_lock, _client.drive.v1.file.subscribe, req)
    if getattr(resp, "code", 0) != 0:
        logger.warning(
            "subscribe_doc_comments failed for %s: %s %s",
            file_token, getattr(resp, "code", ""), getattr(resp, "msg", ""),
        )
        return False
    logger.info("Subscribed to comments for doc %s", file_token)
    return True
