"""
Illustration pipeline: Claude generates image prompt → nano_banana API →
download image → upload to Feishu IM.
"""
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from feishu.messaging import upload_im_image
from reader.arxiv_summarize import launch_claude_for_illustration

logger = logging.getLogger("MaxRead")

_NANO_BANANA_URL = "https://ys-api.xaminim.com/api/nano_banana_image"
_NANO_BANANA_TIMEOUT = 600

_PROXY_ENV = {
    "https_proxy": "http://pac-internal.xaminim.com:3129",
    "http_proxy": "http://pac-internal.xaminim.com:3129",
    "no_proxy": "localhost,127.0.0.1,10.0.0.0/8",
}


def _curl_env() -> dict[str, str]:
    """Return a copy of os.environ with proxy vars guaranteed to be set."""
    env = os.environ.copy()
    for k, v in _PROXY_ENV.items():
        env.setdefault(k, v)
    return env


def _call_nano_banana(prompt_text: str) -> str | None:
    """Call nano_banana image API via curl and return image URL on success, None on failure."""
    body = json.dumps({"prompt": prompt_text})
    for attempt in range(3):
        try:
            proc = subprocess.run(
                [
                    "curl", "-s", "-L",
                    "--max-time", str(_NANO_BANANA_TIMEOUT),
                    "--connect-timeout", "30",
                    _NANO_BANANA_URL,
                    "-H", "Content-Type: application/json",
                    "-d", body,
                ],
                capture_output=True,
                text=True,
                timeout=_NANO_BANANA_TIMEOUT + 10,
                env=_curl_env(),
            )
            if proc.returncode != 0:
                logger.warning("nano_banana curl failed (attempt %d): exit %d, stderr: %s",
                               attempt + 1, proc.returncode, (proc.stderr or "")[:200])
                if attempt < 2:
                    time.sleep(3)
                    continue
                return None
            data = json.loads(proc.stdout)
            if data.get("success") and data.get("data"):
                return str(data["data"])
            logger.warning("nano_banana API returned failure (attempt %d): %s", attempt + 1, data)
            return None
        except Exception as e:
            logger.warning("nano_banana API call failed (attempt %d): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(3)
                continue
            return None
    return None


def _download_image_from_url(url: str, dest: Path) -> bool:
    """Download an image from a URL to a local path. Returns True on success."""
    try:
        proc = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "60", "--connect-timeout", "15", "-o", str(dest), url],
            capture_output=True,
            timeout=70,
            env=_curl_env(),
        )
        return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        logger.exception("Failed to download image from %s: %s", url, e)
        return False


def run_illustration_pipeline(paper_dir: Path, arxiv_id: str) -> tuple[str | None, str]:
    """
    Full illustration pipeline: Claude generates prompt + caption → nano_banana
    generates image → upload to Feishu IM.
    Returns (image_key, caption) — image_key is None on failure, caption may be
    empty if Claude did not produce one.
    Designed to run in a background thread.
    """
    caption = ""
    try:
        logger.info("[%s] Starting illustration prompt generation...", arxiv_id)
        prompt_text, caption = launch_claude_for_illustration(paper_dir)
        if not prompt_text:
            logger.warning("[%s] Illustration prompt is empty", arxiv_id)
            return None, caption
        logger.info("[%s] Illustration prompt generated (%d chars), caption (%d chars), calling nano_banana...",
                    arxiv_id, len(prompt_text), len(caption))

        image_url = _call_nano_banana(prompt_text)
        if not image_url:
            logger.warning("[%s] nano_banana returned no image URL", arxiv_id)
            return None, caption
        logger.info("[%s] nano_banana image URL: %s", arxiv_id, image_url)

        image_path = paper_dir / "illustration.png"
        if not _download_image_from_url(image_url, image_path):
            logger.warning("[%s] Failed to download illustration image", arxiv_id)
            return None, caption

        image_key = upload_im_image(image_path)
        if not image_key:
            logger.warning("[%s] Failed to upload illustration to Feishu", arxiv_id)
            return None, caption

        logger.info("[%s] Illustration pipeline complete: image_key=%s", arxiv_id, image_key)
        return image_key, caption
    except Exception as e:
        logger.exception("[%s] Illustration pipeline failed: %s", arxiv_id, e)
        return None, caption
