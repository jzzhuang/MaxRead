"""
Feishu 长连接：使用长连接接收事件，收到消息后只在终端打印。

  python -m feishu.bot_ws

前置：飞书开放平台 → 事件与回调 → 事件配置 → 使用长连接接收事件，添加事件 im.message.receive_v1；
应用需在「版本管理与发布」中创建版本并发布。.env 中配置 FEISHU_APP_ID、FEISHU_APP_SECRET。
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from feishu.config import get_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot_ws")


def _on_message(data: P2ImMessageReceiveV1) -> None:
    """Receive message and print to terminal only."""
    try:
        event = data.event
        message = event.message
        sender = event.sender
        msg_type = message.message_type if message else None
        message_id = message.message_id if message else None
        chat_id = message.chat_id if message else None
        content = message.content if message else None
        sender_id = sender.sender_id if sender else None
        open_id = getattr(sender_id, "open_id", None) if sender_id else None

        logger.info(
            "RECEIVED msg_id=%s chat_id=%s type=%s open_id=%s",
            message_id,
            chat_id,
            msg_type,
            open_id,
        )
        if content:
            try:
                text = (json.loads(content).get("text") or "")[:200]
                if text:
                    logger.info("  text: %s", text)
            except Exception:
                logger.info("  content: %s", str(content)[:200])
    except Exception as e:
        logger.exception("Handler error: %s", e)


def main():
    cfg = get_config()
    encrypt_key = cfg.get("encrypt_key") or ""
    token = cfg.get("verification_token") or ""
    handler = (
        lark.EventDispatcherHandler.builder(encrypt_key, token, lark.LogLevel.INFO)
        .register_p2_im_message_receive_v1(_on_message)
        .build()
    )
    ws = lark.ws.Client(
        cfg["app_id"],
        cfg["app_secret"],
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
        auto_reconnect=True,
    )
    logger.info("长连接启动中；连接成功后请在开放平台保存事件配置，发消息给机器人即可在终端看到 RECEIVED。")
    ws.start()


if __name__ == "__main__":
    main()
