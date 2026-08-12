"""Load runtime settings from the repository root or legacy feishu/.env."""
import os
from pathlib import Path

from dotenv import load_dotenv

_root_env_path = Path(__file__).resolve().parent.parent / ".env"
_legacy_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_root_env_path)
load_dotenv(_legacy_env_path)


def get_config():
    """Return dict with app_id, app_secret, and optional encrypt_key, verification_token."""
    return {
        "app_id": os.environ.get("FEISHU_APP_ID", ""),
        "app_secret": os.environ.get("FEISHU_APP_SECRET", ""),
        "encrypt_key": os.environ.get("FEISHU_ENCRYPT_KEY", ""),
        "verification_token": os.environ.get("FEISHU_VERIFICATION_TOKEN", ""),
    }
