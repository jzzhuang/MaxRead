#!/usr/bin/env python3
"""
For an arXiv link: download TeX source, unzip into data/, launch an AI agent
(Claude CLI or Cursor agent) in that folder to read the files and write a
concise summary to summarize.md (Markdown with sections, paragraphs, and LaTeX
equations), then print the result.

Modes:
  claude (default): requires Claude Code CLI to be installed.
  cursor: requires the Cursor `agent` command to be installed and authenticated.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

# ArXiv requires a descriptive User-Agent
USER_AGENT = "MaxRead-arxiv-summarize/1.0 (mailto:research@example.com)"
DATA_DIR = Path(__file__).resolve().parent / "data"
PROMPT_FILE = Path(__file__).resolve().parent / "prompt.txt"
CURSOR_KEY_FILE = Path(__file__).resolve().parent.parent / "cursor_api_key.txt"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


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


def _load_prompt() -> str:
    """Load the summarization prompt from reader/prompt.txt."""
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(f"Prompt file not found: {PROMPT_FILE}")
    return PROMPT_FILE.read_text(encoding="utf-8").strip()


SUMMARY_PROMPT = _load_prompt()


def _emit_progress(progress_callback: Callable[[str], None] | None, message: str) -> None:
    """Send a progress update if a callback was provided."""
    if progress_callback and message:
        progress_callback(message)


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


def _resolve_claude_cli_path() -> str:
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


# Bare arXiv id: YYMM.NNNNN or YYMM.NNNNNvN
_ARXIV_BARE_RE = re.compile(r"(?:^|[^\w.])(\d{4}\.\d{4,5}(?:v\d+)?)(?=[^\d]|$)")
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf|e-print)/([^/?#\s]+)", re.I)


def arxiv_id_from_url(url: str) -> str | None:
    """Extract arXiv id from abs/pdf/e-print URL. E.g. 2301.12345 or 2301.12345v2."""
    m = _ARXIV_URL_RE.search(url)
    return m.group(1).strip() if m else None


def extract_arxiv_ids(text: str) -> list[str]:
    """Extract all arXiv ids from text: full URLs and bare ids like 2301.12345."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _ARXIV_URL_RE.finditer(text):
        aid = m.group(1).strip()
        if aid not in seen:
            seen.add(aid)
            out.append(aid)
    for m in _ARXIV_BARE_RE.finditer(text):
        aid = m.group(1).strip()
        if aid not in seen:
            seen.add(aid)
            out.append(aid)
    return out


def download_source(arxiv_id: str) -> Path:
    """Download e-print source from arXiv; return path to the downloaded file."""
    import urllib.request

    url = f"https://arxiv.org/e-print/{arxiv_id}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with tempfile.NamedTemporaryFile(delete=False, suffix=".arxiv") as f:
        path = Path(f.name)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        path.write_bytes(data)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def extract_archive(archive_path: Path, out_dir: Path) -> None:
    """Extract .tar.gz or .zip to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data = archive_path.read_bytes()
    if data[:2] == b"\x1f\x8b":  # gzip
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(out_dir)
    elif data[:2] == b"PK":
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(out_dir)
    else:
        raise ValueError("Unknown archive format (expected .tar.gz or .zip)")


def launch_claude_in_folder(paper_dir: Path, prompt: str) -> str:
    """
    Launch the Claude CLI in paper_dir with the given prompt. Returns the
    content of summarize.md after Claude exits.
    """
    paper_dir = Path(paper_dir).resolve()
    out_file = paper_dir / "summarize.md"
    out_file.unlink(missing_ok=True)
    claude_cmd = _resolve_claude_cli_path()
    proc = subprocess.run(
        [
            claude_cmd,
            "-p",
            prompt,
            "--model",
            "claude-opus-4-6-thinking-medium",
            "--permission-mode",
            "bypassPermissions",
        ],
        cwd=str(paper_dir),
        capture_output=True,
        text=True,
        timeout=600,
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


def run_summarize(
    arxiv_id: str,
    data_dir: Path | None = None,
    mode: str = "claude",
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    """
    Download arXiv source, extract to data_dir/<id>/, launch the chosen agent
    (claude or cursor) in that folder to summarize and write summarize.md.
    Returns the Markdown summary.
    """
    data_dir = data_dir or DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    paper_dir = data_dir / arxiv_id.replace("/", "_")
    _emit_progress(progress_callback, f"开始下载 arXiv {arxiv_id} 源码...")
    archive_path = download_source(arxiv_id)
    try:
        _emit_progress(progress_callback, "源码下载完成，正在解压文件...")
        extract_archive(archive_path, paper_dir)
    finally:
        archive_path.unlink(missing_ok=True)
    _emit_progress(progress_callback, f"论文文件已准备完成：{paper_dir.name}")
    if mode == "cursor":
        return launch_agent_in_folder(paper_dir, SUMMARY_PROMPT, progress_callback=progress_callback)
    return launch_claude_in_folder(paper_dir, SUMMARY_PROMPT)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download arXiv TeX source, launch Claude or Cursor agent in that folder to summarize."
    )
    parser.add_argument("url", help="arXiv URL (e.g. https://arxiv.org/abs/2301.12345)")
    parser.add_argument(
        "--mode",
        choices=("claude", "cursor"),
        default="claude",
        help="Agent to use: claude (default) or cursor",
    )
    args = parser.parse_args()

    arxiv_id = arxiv_id_from_url(args.url)
    if not arxiv_id:
        print("ERROR: Could not parse arXiv id from URL.", file=sys.stderr)
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    paper_dir = DATA_DIR / arxiv_id.replace("/", "_")

    print(f"arXiv id: {arxiv_id}")
    print("Downloading source...")
    archive_path = download_source(arxiv_id)
    try:
        print(f"Extracting to {paper_dir}...")
        extract_archive(archive_path, paper_dir)
    finally:
        archive_path.unlink(missing_ok=True)

    agent_name = "Cursor agent" if args.mode == "cursor" else "Claude"
    print(f"Launching {agent_name} in folder (reads files and writes summarize.md)...")
    if args.mode == "cursor":
        summary = launch_agent_in_folder(paper_dir, SUMMARY_PROMPT)
    else:
        summary = launch_claude_in_folder(paper_dir, SUMMARY_PROMPT)
    print("Summary (saved to summarize.md):")
    print(summary)


if __name__ == "__main__":
    main()
