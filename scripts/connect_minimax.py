#!/usr/bin/env python3
"""
Connect to MiniMax (Anthropic-compatible API). Single attempt.
Uses API key from ~/.claude/settings.json (env.ANTHROPIC_AUTH_TOKEN).
"""
import json
import os
import sys

SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
BASE_URLS = [
    "https://api.minimax.io/anthropic",   # International
    "https://api.minimaxi.com/anthropic", # China
]
MODEL = "MiniMax-M2.5"


def load_api_key():
    with open(SETTINGS_PATH, "r") as f:
        data = json.load(f)
    env = data.get("env") or {}
    key = (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "").strip()
    base = (env.get("ANTHROPIC_BASE_URL") or BASE_URLS[0]).rstrip("/")
    return key, base, BASE_URLS


def main():
    api_key, base_url, all_bases = load_api_key()
    if not api_key:
        print("ERROR: No ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY in env (settings.json)", file=sys.stderr)
        sys.exit(1)
    try:
        import anthropic
    except ImportError:
        print("Installing anthropic...", file=sys.stderr)
        os.system(f"{sys.executable} -m pip install -q anthropic")
        import anthropic
    # Try configured base_url first, then the other region (key may be for China or Intl)
    urls_to_try = [base_url] + [b.rstrip("/") for b in all_bases if b.rstrip("/") != base_url]
    last_error = None
    for base in urls_to_try:
        print(f"Trying base URL: {base}")
        try:
            client = anthropic.Anthropic(
                base_url=base,
                auth_token=api_key,
            )
            message = client.messages.create(
                model=MODEL,
                max_tokens=64,
                messages=[{"role": "user", "content": [{"type": "text", "text": "Say OK"}]}],
            )
            print("Success.")
            for block in message.content:
                if getattr(block, "type", None) == "text":
                    print("Reply:", (getattr(block, "text", "") or "")[:200])
            return
        except Exception as e:
            err_str = str(e).lower()
            if "401" in err_str and "invalid api key" in err_str:
                last_error = e
                print(f"  -> 401 invalid api key (trying other region)")
            elif "500" in err_str and "insufficient balance" in err_str:
                print("Success (API key valid). Account has insufficient balance — add credits at https://platform.minimaxi.com")
                return
            else:
                last_error = e
                print(f"  -> {e}")
    print(f"Error: {last_error}")
    sys.exit(1)


if __name__ == "__main__":
    main()
