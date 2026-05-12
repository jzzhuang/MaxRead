"""
Launch Claude CLI or Cursor agent in a paper directory to summarize or
generate illustration prompts. Handles CLI path resolution, streaming
output parsing, and process management.
"""
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

CURSOR_KEY_FILE = Path(__file__).resolve().parent.parent / "cursor_api_key.txt"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------
def _load_claude_settings_env() -> dict:
    """Load env-like values from ~/.claude/settings.json when available."""
    if not CLAUDE_SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
        env_payload = data.get("env") or {}
        if isinstance(env_payload, dict):
            return env_payload
    except Exception:
        pass
    return {}


def resolve_claude_cli_path() -> str:
    """
    Resolve Claude CLI executable path in a persistent-friendly order:
    1) CLAUDE_CLI_PATH in environment (including feishu/.env via dotenv)
    2) CLAUDE_CLI_PATH in ~/.claude/settings.json env block
    3) PATH lookup
    4) common user/global install locations
    """
    env_path = (os.environ.get("CLAUDE_CLI_PATH") or "").strip()
    if env_path:
        expanded = str(Path(env_path).expanduser())
        if Path(expanded).is_file():
            return expanded
        raise FileNotFoundError(f"CLAUDE_CLI_PATH is set but not found: {expanded}")

    settings_env = _load_claude_settings_env()
    settings_path = str(settings_env.get("CLAUDE_CLI_PATH") or "").strip()
    if settings_path:
        expanded = str(Path(settings_path).expanduser())
        if Path(expanded).is_file():
            return expanded
        raise FileNotFoundError(f"~/.claude/settings.json CLAUDE_CLI_PATH not found: {expanded}")

    from_path = shutil.which("claude")
    if from_path:
        return from_path

    candidates = [
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/usr/bin/claude"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    raise FileNotFoundError(
        "Claude CLI executable not found. Set CLAUDE_CLI_PATH to the full path of your "
        "Claude binary (for example, /home/<you>/.local/bin/claude)."
    )


def _resolve_cursor_api_key() -> str | None:
    """
    Resolve CURSOR_API_KEY from environment first, then from repo-level
    cursor_api_key.txt (first non-empty line).
    """
    env_key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if env_key:
        return env_key
    if CURSOR_KEY_FILE.exists():
        for line in CURSOR_KEY_FILE.read_text(encoding="utf-8").splitlines():
            key = line.strip()
            if key:
                return key
    return None


# ---------------------------------------------------------------------------
# Stream output parsing
# ---------------------------------------------------------------------------
def _extract_stream_text(payload: dict) -> str:
    """Extract user-visible text from agent stream-json output."""
    event_type = str(payload.get("type") or "")
    if event_type == "assistant":
        message = payload.get("message") or {}
        contents = message.get("content") or []
        parts: list[str] = []
        for item in contents:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts)

    if event_type == "tool_call":
        subtype = str(payload.get("subtype") or "")
        tool_call = payload.get("tool_call") or {}
        if "readToolCall" in tool_call:
            args = (tool_call.get("readToolCall") or {}).get("args") or {}
            path = Path(args.get("path") or "").name
            if subtype == "started" and path:
                return f"正在读取 {path}"
            if subtype == "completed" and path:
                return f"已读取 {path}"
        if "shellToolCall" in tool_call and subtype == "started":
            return "正在运行命令"
        if "editToolCall" in tool_call and subtype == "started":
            return "正在修改文件"
        return ""

    if event_type == "result" and payload.get("subtype") == "success":
        result = payload.get("result")
        if isinstance(result, str):
            return result.strip()

    return ""


def _should_forward_agent_output(text: str) -> bool:
    """Hide noisy or reasoning-related text from user-visible progress updates."""
    lowered = " ".join(text.lower().split())
    if not lowered:
        return False
    hidden_markers = (
        "thinking",
        "reasoning",
        "thought process",
        "chain of thought",
        "tokens used",
        "context window",
    )
    return not any(marker in lowered for marker in hidden_markers)


def _emit_progress(progress_callback: Callable[[str], None] | None, message: str) -> None:
    """Send a progress update if a callback was provided."""
    if progress_callback and message:
        progress_callback(message)


# ---------------------------------------------------------------------------
# Claude CLI launching
# ---------------------------------------------------------------------------
def launch_claude_in_folder(paper_dir: Path, prompt: str) -> str:
    """
    Launch the Claude CLI in paper_dir with the given prompt. Returns the
    content of summarize.md after Claude exits.
    """
    paper_dir = Path(paper_dir).resolve()
    out_file = paper_dir / "summarize.md"
    out_file.unlink(missing_ok=True)
    claude_cmd = resolve_claude_cli_path()
    proc = subprocess.run(
        [
            claude_cmd,
            "-p",
            prompt,
            "--model",
            "claude-opus-4-6-thinking-max",
            "--permission-mode",
            "bypassPermissions",
        ],
        cwd=str(paper_dir),
        capture_output=True,
        text=True,
        timeout=None,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"claude command failed ({claude_cmd}): {err}")
    if not out_file.exists():
        cmd = f"cd {paper_dir!r} && {claude_cmd!r} -p {prompt!r} --permission-mode bypassPermissions"
        raise FileNotFoundError(
            f"Claude did not create summarize.md in {paper_dir}. stdout: {(proc.stdout or '')[:500]}\n\n"
            f"Run this in your terminal to run Claude directly:\n  {cmd}"
        )
    return out_file.read_text(encoding="utf-8").strip()


def launch_claude_for_illustration(paper_dir: Path, prompt: str) -> tuple[str, str]:
    """
    Launch Claude CLI in paper_dir to read the paper and generate an image
    prompt and a Chinese caption. Returns (prompt_text, caption_text).
    caption_text may be empty if Claude did not write illustration_caption.txt.
    """
    paper_dir = Path(paper_dir).resolve()
    prompt_file = paper_dir / "illustration_prompt.txt"
    caption_file = paper_dir / "illustration_caption.txt"
    prompt_file.unlink(missing_ok=True)
    caption_file.unlink(missing_ok=True)
    claude_cmd = resolve_claude_cli_path()
    proc = subprocess.run(
        [
            claude_cmd,
            "-p",
            prompt,
            "--model",
            "claude-opus-4-6-thinking-max",
            "--permission-mode",
            "bypassPermissions",
        ],
        cwd=str(paper_dir),
        capture_output=True,
        text=True,
        timeout=600,  # 10 min timeout for illustration
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"Claude illustration command failed ({claude_cmd}): {err}")
    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Claude did not create illustration_prompt.txt in {paper_dir}. "
            f"stdout: {(proc.stdout or '')[:500]}"
        )
    prompt_text = prompt_file.read_text(encoding="utf-8").strip()
    caption_text = ""
    if caption_file.exists():
        caption_text = caption_file.read_text(encoding="utf-8").strip()
    else:
        logger.warning("Claude did not create illustration_caption.txt in %s", paper_dir)
    return prompt_text, caption_text


# ---------------------------------------------------------------------------
# Cursor agent launching
# ---------------------------------------------------------------------------
def launch_agent_in_folder(
    paper_dir: Path,
    prompt: str,
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    """
    Launch the Cursor agent in paper_dir with the given prompt. The agent can
    read files in that folder and write summarize.md. Returns the content of
    summarize.md after the agent exits.
    """
    paper_dir = Path(paper_dir).resolve()
    out_file = paper_dir / "summarize.md"
    out_file.unlink(missing_ok=True)
    api_key = _resolve_cursor_api_key()
    if not api_key:
        raise RuntimeError(
            "Missing CURSOR_API_KEY. Set CURSOR_API_KEY env var or put the key in "
            f"{CURSOR_KEY_FILE} (first non-empty line)."
        )
    env = os.environ.copy()
    env["CURSOR_API_KEY"] = api_key
    _emit_progress(progress_callback, "正在启动 Cursor 总结代理...")
    command = [
        "agent",
        "--print",
        "--output-format",
        "stream-json",
        prompt,
        "--workspace",
        str(paper_dir),
        "--model",
        "claude-4.6-opus-high-thinking",
        "--yolo",
        "--trust",
    ]
    output_chunks: list[str] = []
    with subprocess.Popen(
        command,
        cwd=str(paper_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        bufsize=1,
    ) as proc:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                output_chunks.append(line)
                raw_line = line.strip()
                if not raw_line:
                    continue
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    if _should_forward_agent_output(raw_line):
                        _emit_progress(progress_callback, f"Cursor: {raw_line}")
                    continue

                visible_text = _extract_stream_text(payload)
                if _should_forward_agent_output(visible_text):
                    _emit_progress(progress_callback, f"Cursor: {visible_text}")
            proc.wait(timeout=1800)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise subprocess.TimeoutExpired(command, 1800, output="".join(output_chunks))

    stdout = "".join(output_chunks)
    if proc.returncode != 0:
        err = stdout.strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"agent command failed: {err}")
    if not out_file.exists():
        cmd = (
            f"agent --print --output-format stream-json {prompt!r} --workspace {paper_dir!r} "
            "--model claude-4.6-opus-high-thinking --yolo --trust"
        )
        raise FileNotFoundError(
            f"Agent did not create summarize.md in {paper_dir}. stdout: {stdout[:500]}\n\n"
            f"Run this in your terminal to run the agent directly:\n  {cmd}"
        )
    return out_file.read_text(encoding="utf-8").strip()
