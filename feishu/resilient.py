"""Centralized Feishu API call with thread-safe locking and automatic retry."""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


def _slow_api_log_seconds() -> float:
    raw = os.environ.get("FEISHU_SLOW_API_LOG_SECONDS", "10")
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("Invalid FEISHU_SLOW_API_LOG_SECONDS=%r, using 10s", raw)
        return 10.0


_SLOW_API_LOG_SECONDS = _slow_api_log_seconds()


def _api_name(fn) -> str:
    return getattr(fn, "__qualname__", None) or repr(fn)


def call_api(
    lock: threading.Lock | None,
    fn,
    *args,
    retries: int = 3,
    backoff: float = 2.0,
    **kwargs,
):
    for attempt in range(retries):
        name = _api_name(fn)
        start = time.monotonic()
        try:
            if lock is not None:
                with lock:
                    resp = fn(*args, **kwargs)
            else:
                resp = fn(*args, **kwargs)
        except Exception:
            elapsed = time.monotonic() - start
            if attempt < retries - 1:
                logger.warning(
                    "Feishu API exception on %s after %.1fs, retry %d/%d",
                    name, elapsed, attempt + 1, retries,
                )
                time.sleep(backoff * (attempt + 1))
                continue
            raise

        elapsed = time.monotonic() - start
        code = getattr(resp, "code", 0)
        if elapsed >= _SLOW_API_LOG_SECONDS:
            logger.warning("Slow Feishu API %s took %.1fs (code=%s)", name, elapsed, code)
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
