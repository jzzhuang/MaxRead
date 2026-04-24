"""Centralized Feishu API call with thread-safe locking and automatic retry."""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


def call_api(
    lock: threading.Lock | None,
    fn,
    *args,
    retries: int = 3,
    backoff: float = 2.0,
    **kwargs,
):
    for attempt in range(retries):
        try:
            if lock is not None:
                with lock:
                    resp = fn(*args, **kwargs)
            else:
                resp = fn(*args, **kwargs)
        except Exception:
            if attempt < retries - 1:
                logger.warning(
                    "Feishu API exception on %s, retry %d/%d",
                    fn, attempt + 1, retries,
                )
                time.sleep(backoff * (attempt + 1))
                continue
            raise

        code = getattr(resp, "code", 0)
        if code == 0:
            return resp

        if attempt < retries - 1:
            logger.warning(
                "Feishu API error %s: %s, retry %d/%d",
                code, getattr(resp, "msg", ""), attempt + 1, retries,
            )
            time.sleep(backoff * (attempt + 1))
        else:
            logger.warning(
                "Feishu API error %s: %s after %d attempts",
                code, getattr(resp, "msg", ""), retries,
            )
            return resp

    return None
