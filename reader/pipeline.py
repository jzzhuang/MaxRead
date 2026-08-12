"""
High-level paper processing pipeline: prepare directories, run summarization,
and provide CLI entry point.
"""
import argparse
import json
import logging
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from .agent import (
    DEFAULT_MODEL,
    launch_codex_in_folder,
    launch_codex_latex_repair,
)
from .dir_manager import (
    _MAX_DIR_BYTES,
    convert_figure_pdfs,
    dir_size,
    shrink_image,
    trim_paper_dir,
)
from .download import (
    _pdf_to_text,
    arxiv_id_from_url,
    download_source,
    extract_archive,
)

DATA_DIR = Path(os.environ.get("MAXREAD_DATA_DIR", Path(__file__).resolve().parent / "data"))
PROMPT_FILE = Path(__file__).resolve().parent / "prompt.txt"
logger = logging.getLogger(__name__)


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


def _format_latex_errors(errors: list[dict], *, limit: int = 20) -> str:
    parts: list[str] = []
    for i, err in enumerate(errors[:limit], 1):
        kind = "display math" if err.get("display") else "inline math"
        parts.append(
            "\n".join(
                [
                    f"{i}. {kind}",
                    f"line: {err.get('line') or 'unknown'}",
                    f"error: {err.get('error') or 'unknown'}",
                    "latex:",
                    str(err.get("latex") or ""),
                    "nearby markdown:",
                    str(err.get("context") or ""),
                ]
            )
        )
    if len(errors) > limit:
        parts.append(f"... plus {len(errors) - limit} more LaTeX errors.")
    return "\n\n".join(parts)


def _build_latex_repair_prompt(errors: list[dict]) -> str:
    return f"""当前目录里已有 summarize.md。

检测到 summarize.md 里有以下 LaTeX 公式问题，包括 KaTeX 无法解析，或飞书文档不支持的 LaTeX 命令：

{_format_latex_errors(errors)}

请只修复 summarize.md 中这些 LaTeX 公式的问题。

严格要求：
- 返回完整修复后的 summarize.md 内容。
- 不要重写文章，不要调整文章结构，不要润色非公式段落。
- 不要删除重要公式；优先根据上下文补全缺失的大括号、参数、下标、上标、转义符或分隔符。
- 对飞书不支持的命令，改写成飞书支持的等价表达；例如把 \\ell 改成普通 l，把 \\mathbbm{{1}}/\\mathds{{1}} 改成 \\mathbf{{1}} 或 \\mathbb{{1}}，把 \\ppl_{{m_i}} 改成 \\mathrm{{ppl}}_{{m_i}}，删除 \\qquad 这类纯排版间距命令。
- 如果某个公式无法可靠修复，请把该公式改写为普通中文解释，避免保留损坏的 LaTeX。
- 保留 Markdown 图片、表格、标题和正文顺序。
- 不要创建新文件，不要解释修改过程。
"""


def _validate_summary_latex(summary: str) -> list[dict]:
    """Validate LaTeX for both KaTeX syntax and Feishu compatibility."""
    from transform.feishu_latex_compat import validate_feishu_latex
    from transform.katex_validate import validate_markdown

    return validate_markdown(summary) + validate_feishu_latex(summary)


_MD_IMAGE_RE = re.compile(r"!\[(?:[^\]]|\](?!\())*\]\(([^)]+)\)")
_MD_IMAGE_WITH_ALT_RE = re.compile(r"!\[((?:[^\]]|\](?!\())*)\]\(([^)]+)\)")

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".tiff", ".bmp", ".svg", ".pdf"})
_SUMMARY_PRIMARY_IMAGE_COUNT = 3
_SUMMARY_PRIMARY_IMAGE_TARGET_BYTES = 2 * 1024 * 1024
_SUMMARY_SECONDARY_IMAGE_TARGET_BYTES = 500 * 1024
_IMAGE_COMPRESSED_MARKER = "[过大已压缩]"

_PROCESS_SECTION_TITLE_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:本地材料与覆盖清单|覆盖清单|自检清单)\s*$"
)


def _strip_process_sections(summary: str) -> str:
    """Remove process-only checklist sections that should not appear in final notes."""
    lines = summary.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _PROCESS_SECTION_TITLE_RE.match(line):
            i += 1
            while i < len(lines) and not re.match(r"^\s{0,3}#{1,6}\s+\S", lines[i]):
                i += 1
            while out and not out[-1].strip():
                out.pop()
            if out and i < len(lines):
                out.append("")
            continue
        out.append(line)
        i += 1
    return "\n".join(out).strip()


def _extract_markdown_image_path(raw_path: str) -> str:
    """Extract the path part from a Markdown image target."""
    raw_path = raw_path.strip()
    if not raw_path:
        return ""
    if raw_path.startswith("<"):
        end = raw_path.find(">")
        if end > 0:
            return raw_path[1:end].strip()

    try:
        parts = shlex.split(raw_path)
    except ValueError:
        parts = raw_path.split()
    return parts[0] if parts else raw_path


def _resolve_local_summary_image(
    raw_path: str,
    paper_dir: Path,
    *,
    require_inside: bool = True,
) -> Path | None:
    """Resolve a Markdown image target to a local path under *paper_dir*."""
    image_path = _extract_markdown_image_path(raw_path)
    if not image_path:
        return None

    parsed = urlparse(image_path)
    if parsed.scheme or parsed.netloc:
        return None

    base_dir = paper_dir.resolve()
    candidate = (base_dir / unquote(parsed.path)).resolve()
    if require_inside:
        try:
            if not candidate.is_relative_to(base_dir):
                return None
        except ValueError:
            return None
    return candidate


def _add_image_caption_to_match(markdown_image: str, caption: str) -> str:
    """Return a Markdown image with *caption* stored in its alt text."""
    match = _MD_IMAGE_WITH_ALT_RE.fullmatch(markdown_image.strip())
    if not match:
        return markdown_image

    alt = match.group(1).strip()
    raw_path = match.group(2).strip()
    if caption in alt:
        new_alt = alt
    elif alt:
        new_alt = f"{alt} {caption}"
    else:
        new_alt = caption
    return f"![{new_alt}]({raw_path})"


def _add_image_caption_to_spans(
    line: str,
    spans: list[tuple[int, int]],
    caption: str,
) -> str:
    """Add *caption* to exact Markdown image spans in *line*."""
    updated = line
    for start, end in sorted(spans, reverse=True):
        updated = updated[:start] + _add_image_caption_to_match(updated[start:end], caption) + updated[end:]
    return updated


def _find_broken_images(summary: str, paper_dir: Path) -> list[dict]:
    """Return a list of broken image references in the summary markdown.

    Each entry contains ``line`` (1-based), ``path`` (the raw reference),
    and ``context`` (surrounding markdown lines).
    """
    broken: list[dict] = []
    lines = summary.splitlines()
    for i, line in enumerate(lines):
        for m in _MD_IMAGE_RE.finditer(line):
            raw_path = m.group(1).strip()
            candidate = _resolve_local_summary_image(raw_path, paper_dir, require_inside=False)
            if candidate is not None and not candidate.is_file():
                ctx_start = max(0, i - 1)
                ctx_end = min(len(lines), i + 2)
                broken.append({
                    "line": i + 1,
                    "path": raw_path,
                    "context": "\n".join(lines[ctx_start:ctx_end]),
                })
    return broken


def _list_available_images(paper_dir: Path, *, limit: int = 60) -> str:
    """List image files available under paper_dir, for inclusion in the repair prompt."""
    images: list[str] = []
    for f in sorted(paper_dir.rglob("*")):
        if f.suffix.lower() in _IMAGE_SUFFIXES and f.is_file():
            images.append(str(f.relative_to(paper_dir)))
    if not images:
        return "（目录中没有图片文件）"
    shown = images[:limit]
    result = "\n".join(f"  ./{p}" for p in shown)
    if len(images) > limit:
        result += f"\n  ... 以及其他 {len(images) - limit} 个图片文件"
    return result


def _format_broken_images(broken: list[dict], *, limit: int = 30) -> str:
    parts: list[str] = []
    for i, entry in enumerate(broken[:limit], 1):
        parts.append(
            "\n".join(
                [
                    f"{i}. 第 {entry['line']} 行",
                    f"引用路径: {entry['path']}",
                    "上下文:",
                    entry["context"],
                ]
            )
        )
    if len(broken) > limit:
        parts.append(f"... 以及其他 {len(broken) - limit} 个失效引用。")
    return "\n\n".join(parts)


def _build_image_repair_prompt(broken: list[dict], paper_dir: Path) -> str:
    return f"""当前目录里已有 summarize.md。

检测到 summarize.md 中以下图片引用指向了不存在的文件：

{_format_broken_images(broken)}

当前目录下实际可用的图片文件有：
{_list_available_images(paper_dir)}

请修复 summarize.md 中这些失效的图片路径。

严格要求：
- 返回完整修复后的 summarize.md 内容。
- 根据文件名的相似性，将失效路径替换为上述可用文件中最匹配的路径（使用相对路径，如 ./figures/xxx.png）。
- 常见问题包括：路径有多余的 ../、扩展名不对（如 .pdf 实际已转为 .png）、路径大小写或目录层级不符。
- 如果某个图片确实找不到匹配的文件，就把该图片引用删除，不要保留指向不存在文件的 ![...](...) 标记。
- 不要重写文章，不要调整文章结构，不要润色非图片段落。
- 保留所有文字、公式、表格、标题和正文顺序。
- 不要创建新文件，不要解释修改过程。
"""


def _repair_summary_images(
    paper_dir: Path,
    summary: str,
    *,
    model: str,
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    """
    Detect broken image references and ask the tool-free model to fix them.
    """
    paper_dir = Path(paper_dir)

    broken = _find_broken_images(summary, paper_dir)
    if not broken:
        return summary

    logger.warning(
        "Found %d broken image path(s) in %s",
        len(broken),
        paper_dir / "summarize.md",
    )
    _emit_progress(
        progress_callback,
        f"检测到 {len(broken)} 个图片路径失效，正在请求模型修复...",
    )

    try:
        summary = launch_codex_latex_repair(
            paper_dir,
            _build_image_repair_prompt(broken, paper_dir),
            model=model,
        )
    except Exception as e:
        logger.warning("Model image path repair failed for %s: %s", paper_dir, e)
        return summary

    (paper_dir / "summarize.md").write_text(summary + "\n", encoding="utf-8")

    # Verify
    still_broken = _find_broken_images(summary, paper_dir)
    if still_broken:
        logger.warning(
            "Still %d broken image path(s) after repair in %s",
            len(still_broken),
            paper_dir / "summarize.md",
        )
        _emit_progress(progress_callback, f"仍有 {len(still_broken)} 个图片路径未修复，将继续生成文档")
    else:
        _emit_progress(progress_callback, "图片路径检查已通过")

    return summary


def _compress_summary_images(
    paper_dir: Path,
    summary: str,
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    """Compress images referenced by summarize.md in Markdown order."""
    paper_dir = Path(paper_dir)
    lines = summary.splitlines()
    compressed_image_spans: dict[int, list[tuple[int, int]]] = {}
    compressed_count = 0
    image_index = 0

    for line_index, line in enumerate(lines):
        for m in _MD_IMAGE_RE.finditer(line):
            image_index += 1
            target_bytes = (
                _SUMMARY_PRIMARY_IMAGE_TARGET_BYTES
                if image_index <= _SUMMARY_PRIMARY_IMAGE_COUNT
                else _SUMMARY_SECONDARY_IMAGE_TARGET_BYTES
            )
            img_path = _resolve_local_summary_image(m.group(1), paper_dir)
            if img_path is None or not img_path.is_file():
                continue
            try:
                if img_path.stat().st_size <= target_bytes:
                    continue
            except OSError:
                continue

            if shrink_image(img_path, target_bytes=target_bytes):
                compressed_count += 1
                compressed_image_spans.setdefault(line_index, []).append(m.span())
                logger.info(
                    "Compressed summary image %s to <= %d bytes target",
                    img_path.relative_to(paper_dir),
                    target_bytes,
                )

    changed = bool(compressed_image_spans)
    if not changed and _IMAGE_COMPRESSED_MARKER not in summary:
        return summary

    updated_lines: list[str] = []
    for i, line in enumerate(lines):
        if line.strip() == _IMAGE_COMPRESSED_MARKER and updated_lines:
            previous = updated_lines[-1]
            if _MD_IMAGE_WITH_ALT_RE.fullmatch(previous.strip()):
                updated_lines[-1] = _add_image_caption_to_match(previous, _IMAGE_COMPRESSED_MARKER)
                changed = True
                continue
        if i in compressed_image_spans:
            line = _add_image_caption_to_spans(
                line,
                compressed_image_spans[i],
                _IMAGE_COMPRESSED_MARKER,
            )
            changed = True
        updated_lines.append(line)

    if not changed:
        return summary
    updated_summary = "\n".join(updated_lines).strip()
    (paper_dir / "summarize.md").write_text(updated_summary + "\n", encoding="utf-8")
    if compressed_count:
        _emit_progress(progress_callback, f"已压缩 {compressed_count} 张过大图片")
    return updated_summary


def _repair_summary_latex(
    paper_dir: Path,
    summary: str,
    *,
    model: str,
    progress_callback: Callable[[str], None] | None = None,
    max_attempts: int = 2,
) -> str:
    """
    Validate generated Markdown and ask the tool-free model to repair broken
    LaTeX formulas. Returns the latest summary text.
    """
    paper_dir = Path(paper_dir)
    warnings_path = paper_dir / "katex_warnings.json"

    for attempt in range(1, max_attempts + 1):
        errors = _validate_summary_latex(summary)
        if not errors:
            warnings_path.unlink(missing_ok=True)
            if attempt > 1:
                _emit_progress(progress_callback, "公式渲染检查已通过")
            return summary

        warnings_path.write_text(
            json.dumps(errors, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.warning(
            "LaTeX validation found %d error(s) in %s; repair attempt %d/%d",
            len(errors),
            paper_dir / "summarize.md",
            attempt,
            max_attempts,
        )
        _emit_progress(
            progress_callback,
            f"检测到 {len(errors)} 个公式可能无法渲染，正在请求模型修复（第 {attempt}/{max_attempts} 次）...",
        )

        try:
            summary = launch_codex_latex_repair(
                paper_dir,
                _build_latex_repair_prompt(errors),
                model=model,
            )
        except Exception as e:
            logger.warning("Model LaTeX repair failed for %s: %s", paper_dir, e)
            return summary
        (paper_dir / "summarize.md").write_text(summary + "\n", encoding="utf-8")

    errors = _validate_summary_latex(summary)
    if errors:
        warnings_path.write_text(
            json.dumps(errors, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.warning(
            "LaTeX validation still has %d error(s) after repair attempts in %s",
            len(errors),
            paper_dir / "summarize.md",
        )
        _emit_progress(progress_callback, f"仍有 {len(errors)} 个公式未通过检查，将继续生成文档")
    else:
        warnings_path.unlink(missing_ok=True)
        _emit_progress(progress_callback, "公式渲染检查已通过")
    return summary


def prepare_paper_dir(
    arxiv_id: str,
    data_dir: Path | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> Path:
    """
    Download arXiv source and extract into data_dir/<id>/. Returns the paper
    directory path, ready for agent processing.
    """
    data_dir = data_dir or DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    paper_dir = data_dir / arxiv_id.replace("/", "_")
    _emit_progress(progress_callback, f"开始下载 arXiv {arxiv_id} 源码...")
    archive_path = download_source(arxiv_id)
    try:
        # Preserve metadata across re-extractions. For /regen, keep the old
        # doc link visible until the new Feishu doc is successfully saved.
        queries_backup = None
        doc_link_backup = None
        queries_path = paper_dir / "queries.json"
        doc_link_path = paper_dir / "doc_link.json"
        if queries_path.is_file():
            queries_backup = queries_path.read_bytes()
        if doc_link_path.is_file():
            doc_link_backup = doc_link_path.read_bytes()
        if paper_dir.exists():
            shutil.rmtree(paper_dir)
        _emit_progress(progress_callback, "源码下载完成，正在解压文件...")
        extract_archive(archive_path, paper_dir)
        if queries_backup is not None:
            (paper_dir / "queries.json").write_bytes(queries_backup)
        if doc_link_backup is not None:
            (paper_dir / "doc_link.json").write_bytes(doc_link_backup)
        convert_figure_pdfs(paper_dir)
        if dir_size(paper_dir) > _MAX_DIR_BYTES:
            trim_paper_dir(paper_dir)
    finally:
        archive_path.unlink(missing_ok=True)
    _emit_progress(progress_callback, f"论文文件已准备完成：{paper_dir.name}")
    return paper_dir


def summarize_prepared_dir(
    paper_dir: Path,
    mode: str = "api",
    progress_callback: Callable[[str], None] | None = None,
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Run tool-free API summarization on an already-prepared paper directory.
    Returns the Markdown summary text.
    """
    if mode not in {"api", "codex"}:
        raise ValueError("Only tool-free API mode is supported")
    summary = launch_codex_in_folder(paper_dir, SUMMARY_PROMPT, model=model)
    summary = _strip_process_sections(summary)
    (Path(paper_dir) / "summarize.md").write_text(summary + "\n", encoding="utf-8")
    summary = _repair_summary_images(
        paper_dir, summary, model=model, progress_callback=progress_callback,
    )
    summary = _compress_summary_images(
        paper_dir, summary, progress_callback=progress_callback,
    )
    summary = _repair_summary_latex(
        paper_dir,
        summary,
        model=model,
        progress_callback=progress_callback,
    )
    (Path(paper_dir) / "summarize.md").write_text(summary + "\n", encoding="utf-8")
    return summary


def run_local_summarize(
    name: str,
    data_dir: Path | None = None,
    mode: str = "api",
    progress_callback: Callable[[str], None] | None = None,
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Summarize a paper already present in data/<name>/ with a .pdf file.
    Converts the PDF to Markdown, then calls the tool-free API path.
    """
    data_dir = data_dir or DATA_DIR
    paper_dir = data_dir / name
    if not paper_dir.is_dir():
        raise FileNotFoundError(f"Directory not found: {paper_dir}")

    pdfs = [f for f in paper_dir.iterdir() if f.suffix.lower() == ".pdf" and f.is_file()]
    if not pdfs:
        raise FileNotFoundError(f"No PDF file found in {paper_dir}")
    pdf_path = pdfs[0]

    _emit_progress(progress_callback, f"正在转换 PDF：{pdf_path.name}")
    md_path = paper_dir / "paper.md"
    text = _pdf_to_text(pdf_path, paper_dir)
    md_path.write_text(text, encoding="utf-8")

    convert_figure_pdfs(paper_dir)
    if dir_size(paper_dir) > _MAX_DIR_BYTES:
        trim_paper_dir(paper_dir)

    _emit_progress(progress_callback, f"论文文件已准备完成：{paper_dir.name}")
    if mode not in {"api", "codex"}:
        raise ValueError("Only tool-free API mode is supported")
    summary = launch_codex_in_folder(paper_dir, SUMMARY_PROMPT, model=model)
    summary = _strip_process_sections(summary)
    (paper_dir / "summarize.md").write_text(summary + "\n", encoding="utf-8")
    summary = _repair_summary_images(
        paper_dir, summary, model=model, progress_callback=progress_callback,
    )
    summary = _compress_summary_images(
        paper_dir, summary, progress_callback=progress_callback,
    )
    summary = _repair_summary_latex(
        paper_dir,
        summary,
        model=model,
        progress_callback=progress_callback,
    )
    (paper_dir / "summarize.md").write_text(summary + "\n", encoding="utf-8")
    return summary


def run_summarize(
    arxiv_id: str,
    data_dir: Path | None = None,
    mode: str = "api",
    progress_callback: Callable[[str], None] | None = None,
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Download arXiv source, extract it, and summarize through the tool-free API.
    Returns the Markdown summary.
    """
    paper_dir = prepare_paper_dir(arxiv_id, data_dir, progress_callback)
    return summarize_prepared_dir(paper_dir, mode, progress_callback, model=model)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download arXiv TeX source and summarize it through the OpenAI Responses API."
    )
    parser.add_argument("input", help="arXiv URL, arXiv id, or local folder name in data/ (e.g. deepseekV4)")
    parser.add_argument(
        "--mode",
        choices=("api",),
        default="api",
        help="Tool-free OpenAI Responses API",
    )
    args = parser.parse_args()

    local_dir = DATA_DIR / args.input
    if local_dir.is_dir():
        print(f"Using local paper directory: {local_dir}")
        summary = run_local_summarize(args.input, mode=args.mode)
        print("Summary (saved to summarize.md):")
        print(summary)
        return

    arxiv_id = arxiv_id_from_url(args.input) or args.input.strip()
    if not arxiv_id:
        print("ERROR: Could not parse arXiv id from input.", file=sys.stderr)
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

    print("Calling the tool-free OpenAI Responses API...")
    summary = launch_codex_in_folder(paper_dir, SUMMARY_PROMPT)
    (paper_dir / "summarize.md").write_text(summary + "\n", encoding="utf-8")
    print("Summary (saved to summarize.md):")
    print(summary)


if __name__ == "__main__":
    main()
