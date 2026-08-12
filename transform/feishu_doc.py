"""
Build Feishu docx blocks from parsed (block_type, content) and create cloud docs.
Supports inline bold/italic/math, bullet/ordered lists, code blocks, dividers,
images, equations, and native tables.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from lark_oapi.api.docx.v1.model.block import Block
from lark_oapi.api.docx.v1.model.caption import Caption
from lark_oapi.api.docx.v1.model.create_document_block_children_request import (
    CreateDocumentBlockChildrenRequest,
)
from lark_oapi.api.docx.v1.model.create_document_block_children_request_body import (
    CreateDocumentBlockChildrenRequestBody,
)
from lark_oapi.api.docx.v1.model.create_document_block_descendant_request import (
    CreateDocumentBlockDescendantRequest,
)
from lark_oapi.api.docx.v1.model.create_document_block_descendant_request_body import (
    CreateDocumentBlockDescendantRequestBody,
)
from lark_oapi.api.docx.v1.model.create_document_request import CreateDocumentRequest
from lark_oapi.api.docx.v1.model.create_document_request_body import CreateDocumentRequestBody
from lark_oapi.api.docx.v1.model.divider import Divider
from lark_oapi.api.docx.v1.model.equation import Equation
from lark_oapi.api.docx.v1.model.image import Image
from lark_oapi.api.docx.v1.model.patch_document_block_request import (
    PatchDocumentBlockRequest,
)
from lark_oapi.api.docx.v1.model.replace_image_request import ReplaceImageRequest
from lark_oapi.api.docx.v1.model.table import Table, TableProperty
from lark_oapi.api.docx.v1.model.table_cell import TableCell
from lark_oapi.api.docx.v1.model.text import Text
from lark_oapi.api.docx.v1.model.text_element import TextElement
from lark_oapi.api.docx.v1.model.text_element_style import TextElementStyle
from lark_oapi.api.docx.v1.model.text_run import TextRun
from lark_oapi.api.docx.v1.model.text_style import TextStyle
from lark_oapi.api.docx.v1.model.update_block_request import UpdateBlockRequest
from lark_oapi.api.drive.v1.model.upload_all_media_request import UploadAllMediaRequest
from lark_oapi.api.drive.v1.model.upload_all_media_request_body import (
    UploadAllMediaRequestBody,
)

from feishu.resilient import call_api

from .constants import (
    DOCX_BLOCK_TYPE_BULLET,
    DOCX_BLOCK_TYPE_CODE,
    DOCX_BLOCK_TYPE_DIVIDER,
    DOCX_BLOCK_TYPE_GRID,
    DOCX_BLOCK_TYPE_HEADING1,
    DOCX_BLOCK_TYPE_HEADING2,
    DOCX_BLOCK_TYPE_HEADING3,
    DOCX_BLOCK_TYPE_IMAGE,
    DOCX_BLOCK_TYPE_ORDERED,
    DOCX_BLOCK_TYPE_TABLE,
    DOCX_BLOCK_TYPE_TABLE_CELL,
    DOCX_BLOCK_TYPE_TEXT,
    TABLE_FULL_WIDTH,
)
from .inline import parse_inline

BlockContent = Union[str, dict[str, str]]
_IMAGE_COMPRESSED_MARKER = "[过大已压缩]"
_TEXT_ALIGN_CENTER = 2
_DEFAULT_IMAGE_UPLOAD_CONCURRENCY = 3
_MAX_IMAGE_UPLOAD_CONCURRENCY = 8
_MAX_IMAGE_UPLOAD_BYTES = 20 * 1024 * 1024
_ALLOWED_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".pdf"}
)


def _has_expected_image_signature(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            magic = stream.read(12)
    except OSError:
        return False
    suffix = path.suffix.lower()
    if suffix == ".png":
        return magic.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return magic.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return magic.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return magic.startswith(b"RIFF") and magic[8:12] == b"WEBP"
    if suffix == ".bmp":
        return magic.startswith(b"BM")
    if suffix == ".tiff":
        return magic.startswith((b"II*\x00", b"MM\x00*"))
    if suffix == ".pdf":
        return magic.startswith(b"%PDF-")
    return False


@dataclass
class _PendingImageUpload:
    temporary_block_id: str
    image_path: Path
    caption_text: str


@dataclass
class _NativeRenderResult:
    descendants: list[Block]
    first_level_ids: list[str]


def _new_temp_block_id(prefix: str = "mrb") -> str:
    return prefix + uuid.uuid4().hex


def _ensure_temp_block_id(block: Block, prefix: str = "mrb") -> str:
    block_id = getattr(block, "block_id", None)
    if not block_id:
        block_id = _new_temp_block_id(prefix)
        block.block_id = block_id
    return block_id


def _markdown_image_fallback(content: BlockContent) -> str:
    if not isinstance(content, dict):
        return ""
    alt = str(content.get("alt") or "").strip()
    path = str(content.get("path") or "").strip()
    if alt and path:
        return f"{alt}: {path}"
    return path or alt


def _join_image_captions(*parts: str | None) -> str:
    captions: list[str] = []
    for part in parts:
        value = str(part or "").strip()
        if value and value not in captions:
            captions.append(value)
    return " ".join(captions)


def _image_caption_text(content: BlockContent) -> str:
    if not isinstance(content, dict):
        return ""
    caption = str(content.get("caption") or "").strip()
    if caption:
        return caption
    return str(content.get("alt") or "").strip()


def _build_image_caption(caption_text: str) -> Caption | None:
    caption_text = caption_text.strip()
    if not caption_text:
        return None
    return Caption.builder().content(caption_text).build()


def _build_image_model(caption_text: str = "") -> Image:
    builder = Image.builder()
    caption = _build_image_caption(caption_text)
    if caption is not None:
        builder.caption(caption)
    return builder.build()


def _build_image_block(caption_text: str = "") -> Block:
    return Block.builder().block_type(DOCX_BLOCK_TYPE_IMAGE).image(_build_image_model(caption_text)).build()


def _resolve_image_path(image_path: str, base_dir: Path | None) -> Path | None:
    """Resolve an untrusted Markdown image path strictly below *base_dir*."""
    if base_dir is None or not image_path.strip():
        return None
    relative = Path(image_path)
    if relative.is_absolute() or relative.anchor or ".." in relative.parts:
        return None
    if relative.suffix.lower() not in _ALLOWED_IMAGE_SUFFIXES:
        return None

    base = Path(base_dir).resolve()
    unresolved = base / relative
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        candidate = unresolved.resolve(strict=True)
        candidate.relative_to(base)
    except (OSError, ValueError):
        return None
    try:
        if (
            not candidate.is_file()
            or candidate.stat().st_size > _MAX_IMAGE_UPLOAD_BYTES
            or not _has_expected_image_signature(candidate)
        ):
            return None
    except OSError:
        return None
    return candidate


def _image_display_size(image_path: Path) -> tuple[int, int] | None:
    """Return (width, height) in pixels.

    Scale to width=800; if the resulting height < 600 the image is landscape
    (width-dominated) so keep width=800.  Otherwise use width=400.
    """
    try:
        import fitz

        doc = fitz.open(str(image_path))
        page = doc[0]
        rect = page.rect
        doc.close()
        w, h = int(rect.width), int(rect.height)
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    h_at_800 = int(h * 800 / w)
    target_w = 800 if h_at_800 < 600 else 400
    return target_w, max(1, int(h * target_w / w))


def _prepare_upload_image(image_path: Path) -> tuple[Path, str | None]:
    if image_path.suffix.lower() != ".pdf":
        return image_path, None

    import fitz  # PyMuPDF

    temp_dir = tempfile.mkdtemp(prefix="maxread-feishu-img-")
    try:
        doc = fitz.open(str(image_path))
        page = doc[0]
        pix = page.get_pixmap()
        png_path = Path(temp_dir) / (image_path.stem + ".png")
        pix.save(str(png_path))
        doc.close()
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"PDF to PNG conversion failed: {e}") from e
    return png_path, temp_dir


def _create_block_child(
    client,
    document_id: str,
    index: int,
    block: Block,
    api_lock: threading.Lock | None = None,
    *,
    retries: int = 3,
):
    children_body = (
        CreateDocumentBlockChildrenRequestBody.builder()
        .children([block])
        .index(index)
        .build()
    )
    add_req = (
        CreateDocumentBlockChildrenRequest.builder()
        .document_id(document_id)
        .block_id(document_id)
        .request_body(children_body)
        .build()
    )
    return call_api(api_lock, client.docx.v1.document_block_children.create, add_req, retries=retries)


def _create_block_descendants(
    client,
    document_id: str,
    index: int,
    children_id: list[str],
    descendants: list[Block],
    api_lock: threading.Lock | None = None,
):
    add_req = (
        CreateDocumentBlockDescendantRequest.builder()
        .document_id(document_id)
        .block_id(document_id)
        .document_revision_id(-1)
        .request_body(
            CreateDocumentBlockDescendantRequestBody.builder()
            .children_id(children_id)
            .descendants(descendants)
            .index(index)
            .build()
        )
        .build()
    )
    return call_api(api_lock, client.docx.v1.document_block_descendant.create, add_req)


def _parse_markdown_image_line(line: str) -> dict[str, str] | None:
    stripped = line.strip()
    match = re.fullmatch(r"!\[((?:[^\]]|\](?!\())*)\]\(([^)]+)\)", stripped)
    if not match:
        return None
    return {"alt": match.group(1).strip(), "path": match.group(2).strip()}


def _split_markdown_segments(
    md_content: str,
) -> list[tuple[str, BlockContent]]:
    segments: list[tuple[str, BlockContent]] = []
    markdown_lines: list[str] = []

    def flush_markdown() -> None:
        if not markdown_lines:
            return
        markdown = "\n".join(markdown_lines).strip()
        markdown_lines.clear()
        if markdown:
            segments.append(("markdown", markdown))

    lines = md_content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        single_dollar = re.fullmatch(r"\$\$(.+?)\$\$", stripped, flags=re.DOTALL)
        single_bracket = re.fullmatch(r"\\\[(.+?)\\\]", stripped, flags=re.DOTALL)
        if single_dollar or single_bracket:
            flush_markdown()
            segments.append(("equation", (single_dollar or single_bracket).group(1).strip()))
            i += 1
            continue

        if stripped in {"$$", r"\["}:
            close = "$$" if stripped == "$$" else r"\]"
            equation_lines: list[str] = []
            j = i + 1
            while j < len(lines) and lines[j].strip() != close:
                equation_lines.append(lines[j])
                j += 1
            if j < len(lines):
                content = "\n".join(equation_lines).strip()
                if content:
                    flush_markdown()
                    segments.append(("equation", content))
                i = j + 1
                continue

        image = _parse_markdown_image_line(line)
        if image is not None:
            flush_markdown()
            if i + 1 < len(lines) and lines[i + 1].strip() == _IMAGE_COMPRESSED_MARKER:
                image["caption"] = _join_image_captions(image.get("alt"), _IMAGE_COMPRESSED_MARKER)
                i += 2
            else:
                image["caption"] = _image_caption_text(image)
                i += 1
            segments.append(("image", image))
            continue
        if line.strip().startswith("## "):
            flush_markdown()
        markdown_lines.append(line)
        i += 1

    flush_markdown()
    return segments


def _extract_leading_h1_title(md_content: str) -> tuple[str | None, str]:
    lines = md_content.splitlines()
    first_non_empty_index: int | None = None
    for idx, line in enumerate(lines):
        if line.strip():
            first_non_empty_index = idx
            break
    if first_non_empty_index is None:
        return None, md_content

    first_line = lines[first_non_empty_index]
    match = re.fullmatch(r"\s*#(?!#)\s+(.+?)\s*", first_line)
    if not match:
        return None, md_content

    extracted_title = match.group(1).strip()
    remaining_lines = lines[:first_non_empty_index] + lines[first_non_empty_index + 1 :]
    while remaining_lines and not remaining_lines[0].strip():
        remaining_lines.pop(0)
    return extracted_title or None, "\n".join(remaining_lines)


_MAX_DESCENDANTS = 950


_INLINE_MATH_RE = re.compile(
    r"(?<![\$\\])\$(?!\$)(.+?)(?<![\$\\])\$(?!\$)|(?<!\\)\\\((.+?)(?<!\\)\\\)"
)


def _inline_math_latex(match: re.Match[str]) -> str:
    return (match.group(1) if match.group(1) is not None else match.group(2)).strip()


def _image_upload_concurrency() -> int:
    raw = os.environ.get("MAXREAD_FEISHU_IMAGE_CONCURRENCY", "").strip()
    if not raw:
        return _DEFAULT_IMAGE_UPLOAD_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_IMAGE_UPLOAD_CONCURRENCY
    return min(_MAX_IMAGE_UPLOAD_CONCURRENCY, max(1, value))


def _escape_latex_angle_brackets(latex: str) -> str:
    return latex.replace("<", "\\lt ").replace(">", "\\gt ")


def _text_style_for_segment(*, bold: bool = False, italic: bool = False) -> TextElementStyle | None:
    if not bold and not italic:
        return None
    builder = TextElementStyle.builder()
    if bold:
        builder.bold(True)
    if italic:
        builder.italic(True)
    return builder.build()


def _text_run_element(content: str, *, bold: bool = False, italic: bool = False) -> TextElement | None:
    if not content:
        return None
    run_builder = TextRun.builder().content(content)
    style = _text_style_for_segment(bold=bold, italic=italic)
    if style is not None:
        run_builder.text_element_style(style)
    return TextElement.builder().text_run(run_builder.build()).build()


def _append_text_elements(elements: list[TextElement], text: str) -> None:
    for segment in parse_inline(text):
        element = _text_run_element(
            segment["text"],
            bold=bool(segment["bold"]),
            italic=bool(segment["italic"]),
        )
        if element is not None:
            elements.append(element)


def _inline_text_elements(text: str) -> list[TextElement]:
    elements: list[TextElement] = []
    pos = 0
    for match in _INLINE_MATH_RE.finditer(text):
        _append_text_elements(elements, text[pos : match.start()])
        latex = _escape_latex_angle_brackets(_inline_math_latex(match))
        if latex:
            elements.append(TextElement.builder().equation(Equation.builder().content(latex).build()).build())
        pos = match.end()
    _append_text_elements(elements, text[pos:])
    if not elements:
        fallback = _text_run_element(text or " ")
        if fallback is not None:
            elements.append(fallback)
    return elements


def _text_model(text: str, *, align: int | None = None) -> Text:
    builder = Text.builder().elements(_inline_text_elements(text))
    if align is not None:
        builder.style(TextStyle.builder().align(align).build())
    return builder.build()


def _text_block(block_type: int, text: str, *, align: int | None = None) -> Block:
    text_obj = _text_model(text, align=align)
    builder = Block.builder().block_type(block_type)
    if block_type == DOCX_BLOCK_TYPE_HEADING1:
        builder.heading1(text_obj)
    elif block_type == DOCX_BLOCK_TYPE_HEADING2:
        builder.heading2(text_obj)
    elif block_type == DOCX_BLOCK_TYPE_HEADING3:
        builder.heading3(text_obj)
    elif block_type == DOCX_BLOCK_TYPE_BULLET:
        builder.bullet(text_obj)
    elif block_type == DOCX_BLOCK_TYPE_ORDERED:
        builder.ordered(text_obj)
    else:
        builder.text(text_obj)
    return builder.build()


def _plain_text_model(text: str) -> Text:
    element = _text_run_element(text or " ")
    elements = [element] if element is not None else []
    return Text.builder().elements(elements).build()


def _code_block(text: str) -> Block:
    return Block.builder().block_type(DOCX_BLOCK_TYPE_CODE).code(_plain_text_model(text)).build()


def _divider_block() -> Block:
    return (
        Block.builder()
        .block_type(DOCX_BLOCK_TYPE_DIVIDER)
        .divider(Divider.builder().build())
        .build()
    )


def _native_equation_block(latex: str) -> Block | None:
    latex = _escape_latex_angle_brackets(latex.strip())
    if not latex:
        return None
    return (
        Block.builder()
        .block_type(DOCX_BLOCK_TYPE_TEXT)
        .text(
            Text.builder()
            .style(TextStyle.builder().align(_TEXT_ALIGN_CENTER).build())
            .elements([
                TextElement.builder()
                .equation(Equation.builder().content(latex).build())
                .build()
            ])
            .build()
        )
        .build()
    )


def _heading_block(line: str) -> Block | None:
    match = re.fullmatch(r"\s*(#{1,6})\s+(.+?)\s*", line)
    if not match:
        return None
    level = len(match.group(1))
    content = match.group(2).strip()
    if not content:
        return None
    if level == 1:
        block_type = DOCX_BLOCK_TYPE_HEADING1
    elif level == 2:
        block_type = DOCX_BLOCK_TYPE_HEADING2
    else:
        block_type = DOCX_BLOCK_TYPE_HEADING3
    return _text_block(block_type, content)


def _bullet_text(line: str) -> str | None:
    stripped = line.lstrip()
    if stripped.startswith("- ") or stripped.startswith("* "):
        return stripped[2:].strip()
    return None


def _ordered_text(line: str) -> str | None:
    match = re.match(r"^\s*\d+\.\s+(.+?)\s*$", line)
    return match.group(1).strip() if match else None


def _parse_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    in_code = False
    in_math = False
    i = 0
    while i < len(stripped):
        ch = stripped[i]
        if escaped:
            if ch != "|":
                buf.append("\\")
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if ch == "`" and not in_math:
            in_code = not in_code
            buf.append(ch)
            i += 1
            continue
        if ch == "$" and not in_code:
            if i + 1 < len(stripped) and stripped[i + 1] == "$":
                in_math = not in_math
                buf.append("$$")
                i += 2
                continue
            in_math = not in_math
            buf.append(ch)
            i += 1
            continue
        if ch == "|" and not in_code and not in_math:
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if escaped:
        buf.append("\\")
    cells.append("".join(buf).strip())
    return cells if cells else None


def _is_table_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in cells)


def _normalize_table_rows(rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        return []
    column_size = max(len(row) for row in rows)
    return [row + [""] * (column_size - len(row)) for row in rows]


def _table_column_widths(column_size: int) -> list[int]:
    base = max(1, TABLE_FULL_WIDTH // column_size)
    remainder = TABLE_FULL_WIDTH - base * column_size
    return [base + (1 if index < remainder else 0) for index in range(column_size)]


def _native_table_result(rows: list[list[str]], *, header_row: bool) -> _NativeRenderResult:
    rows = _normalize_table_rows(rows)
    row_size = len(rows)
    column_size = len(rows[0]) if rows else 0
    if row_size == 0 or column_size == 0:
        return _NativeRenderResult([], [])

    table_id = _new_temp_block_id("mrt")
    cell_ids: list[str] = []
    descendants: list[Block] = []
    cell_descendants: list[Block] = []

    for row in rows:
        for cell_text in row:
            cell_id = _new_temp_block_id("mrc")
            text_id = _new_temp_block_id("mrtx")
            text_block = _text_block(DOCX_BLOCK_TYPE_TEXT, cell_text)
            text_block.block_id = text_id
            cell_block = (
                Block.builder()
                .block_id(cell_id)
                .children([text_id])
                .block_type(DOCX_BLOCK_TYPE_TABLE_CELL)
                .table_cell(TableCell.builder().build())
                .build()
            )
            cell_ids.append(cell_id)
            cell_descendants.extend([cell_block, text_block])

    table_property = (
        TableProperty.builder()
        .row_size(row_size)
        .column_size(column_size)
        .column_width(_table_column_widths(column_size))
        .header_row(header_row)
        .build()
    )
    table_block = (
        Block.builder()
        .block_id(table_id)
        .children(cell_ids)
        .block_type(DOCX_BLOCK_TYPE_TABLE)
        .table(Table.builder().cells(cell_ids).property(table_property).build())
        .build()
    )
    descendants.append(table_block)
    descendants.extend(cell_descendants)
    return _NativeRenderResult(descendants, [table_id])


def _native_table_results(rows: list[list[str]], *, header_row: bool) -> _NativeRenderResult:
    rows = _normalize_table_rows(rows)
    if not rows:
        return _NativeRenderResult([], [])

    column_size = len(rows[0])
    row_cost = column_size * 2
    max_rows_without_header = max(1, (_MAX_DESCENDANTS - 1) // row_cost)

    if len(rows) <= max_rows_without_header:
        return _native_table_result(rows, header_row=header_row)

    if not header_row:
        result = _NativeRenderResult([], [])
        for start in range(0, len(rows), max_rows_without_header):
            chunk = _native_table_result(rows[start : start + max_rows_without_header], header_row=False)
            result.descendants.extend(chunk.descendants)
            result.first_level_ids.extend(chunk.first_level_ids)
        return result

    header_rows = rows[:1]
    data_rows = rows[1:]
    header_cost = 1 + len(header_rows) * row_cost
    max_data_rows = max(1, (_MAX_DESCENDANTS - header_cost) // row_cost)

    result = _NativeRenderResult([], [])
    for start in range(0, len(data_rows), max_data_rows):
        chunk_rows = header_rows + data_rows[start : start + max_data_rows]
        chunk = _native_table_result(chunk_rows, header_row=True)
        result.descendants.extend(chunk.descendants)
        result.first_level_ids.extend(chunk.first_level_ids)
    return result


def _collect_table(lines: list[str], start: int) -> tuple[list[list[str]], bool, int]:
    raw_rows: list[tuple[list[str], bool]] = []
    i = start
    while i < len(lines):
        row = _parse_table_row(lines[i])
        if row is None:
            break
        raw_rows.append((row, _is_table_separator_row(row)))
        i += 1

    separator_index = next((index for index, (_, is_separator) in enumerate(raw_rows) if is_separator), None)
    header_row = separator_index is not None and separator_index > 0
    rows = [row for row, is_separator in raw_rows if not is_separator]
    return rows, header_row, i


def _native_markdown_to_blocks(markdown: str) -> _NativeRenderResult:
    result = _NativeRenderResult([], [])
    if not markdown.strip():
        return result

    paragraph_lines: list[str] = []

    def append_block(block: Block) -> None:
        block_id = _ensure_temp_block_id(block)
        result.descendants.append(block)
        result.first_level_ids.append(block_id)

    def append_result(child_result: _NativeRenderResult) -> None:
        result.descendants.extend(child_result.descendants)
        result.first_level_ids.extend(child_result.first_level_ids)

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return
        text = "\n".join(line.strip() for line in paragraph_lines).strip()
        paragraph_lines.clear()
        if text:
            append_block(_text_block(DOCX_BLOCK_TYPE_TEXT, text))

    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            i += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            append_block(_code_block("\n".join(code_lines).rstrip("\n")))
            continue

        table_row = _parse_table_row(line)
        if table_row is not None:
            flush_paragraph()
            rows, header_row, i = _collect_table(lines, i)
            append_result(_native_table_results(rows, header_row=header_row))
            continue

        if re.fullmatch(r"-{3,}", stripped):
            flush_paragraph()
            append_block(_divider_block())
            i += 1
            continue

        heading = _heading_block(line)
        if heading is not None:
            flush_paragraph()
            append_block(heading)
            i += 1
            continue

        bullet = _bullet_text(line)
        if bullet is not None:
            flush_paragraph()
            append_block(_text_block(DOCX_BLOCK_TYPE_BULLET, bullet))
            i += 1
            continue

        ordered = _ordered_text(line)
        if ordered is not None:
            flush_paragraph()
            append_block(_text_block(DOCX_BLOCK_TYPE_ORDERED, ordered))
            i += 1
            continue

        paragraph_lines.append(line)
        i += 1

    flush_paragraph()
    return result


def _created_block_id_map(resp, children_id: list[str]) -> dict[str, str]:
    data = getattr(resp, "data", None)
    result: dict[str, str] = {}
    for relation in getattr(data, "block_id_relations", None) or []:
        temp_id = getattr(relation, "temporary_block_id", None)
        block_id = getattr(relation, "block_id", None)
        if temp_id and block_id:
            result[temp_id] = block_id

    children = getattr(data, "children", None) or []
    for temp_id, child in zip(children_id, children):
        block_id = getattr(child, "block_id", None)
        if temp_id and block_id:
            result.setdefault(temp_id, block_id)
    return result


def _collect_descendant_group(block_id: str, blocks_by_id: dict[str, Block], seen: set[str]) -> list[Block]:
    if block_id in seen:
        return []
    block = blocks_by_id.get(block_id)
    if block is None:
        return []
    seen.add(block_id)
    group = [block]
    for child_id in getattr(block, "children", None) or []:
        group.extend(_collect_descendant_group(child_id, blocks_by_id, seen))
    return group


def _descendant_groups(descendants: list[Block], first_level_ids: list[str]) -> list[tuple[list[str], list[Block]]]:
    for block in descendants:
        _ensure_temp_block_id(block)
    blocks_by_id = {getattr(block, "block_id"): block for block in descendants if getattr(block, "block_id", None)}

    groups: list[tuple[list[str], list[Block]]] = []
    seen: set[str] = set()
    for first_level_id in first_level_ids:
        group = _collect_descendant_group(first_level_id, blocks_by_id, seen)
        if group:
            groups.append(([first_level_id], group))
    return groups


def _insert_native_blocks(
    client,
    document_id: str,
    index: int,
    descendants: list[Block],
    api_lock: threading.Lock | None = None,
    *,
    first_level_ids: list[str] | None = None,
) -> tuple[int, dict[str, str]] | None:
    if not descendants:
        return 0, {}
    if first_level_ids is None:
        first_level_ids = [_ensure_temp_block_id(block) for block in descendants]

    groups = _descendant_groups(descendants, first_level_ids)
    inserted = 0
    created_block_ids: dict[str, str] = {}

    batch_children: list[str] = []
    batch_descendants: list[Block] = []

    def flush_batch() -> bool:
        nonlocal inserted, batch_children, batch_descendants
        if not batch_descendants:
            return True
        resp = _create_block_descendants(
            client,
            document_id,
            index + inserted,
            batch_children,
            batch_descendants,
            api_lock,
        )
        if getattr(resp, "code", 0) != 0:
            return False
        created_block_ids.update(_created_block_id_map(resp, batch_children))
        inserted += len(batch_children)
        batch_children = []
        batch_descendants = []
        return True

    for group_children, group_descendants in groups:
        if batch_descendants and len(batch_descendants) + len(group_descendants) > _MAX_DESCENDANTS:
            if not flush_batch():
                return None
        batch_children.extend(group_children)
        batch_descendants.extend(group_descendants)

    if not flush_batch():
        return None
    return inserted, created_block_ids


def _upload_and_patch_image(
    client,
    document_id: str,
    block_id: str,
    image_path: Path,
    api_lock: threading.Lock | None = None,
    caption_text: str = "",
) -> bool:
    """Upload an image file to an existing image block and patch it with the file token."""
    upload_path = image_path
    temp_dir: str | None = None
    try:
        upload_path, temp_dir = _prepare_upload_image(image_path)
        upload_body = UploadAllMediaRequestBody()
        upload_body.file_name = upload_path.name
        upload_body.parent_type = "docx_image"
        upload_body.parent_node = block_id
        upload_body.size = upload_path.stat().st_size
        with upload_path.open("rb") as f:
            upload_body.file = f
            upload_req = (
                UploadAllMediaRequest.builder()
                .request_body(upload_body)
                .build()
            )
            try:
                upload_resp = call_api(api_lock, client.drive.v1.media.upload_all, upload_req)
            except Exception:
                return False

        if getattr(upload_resp, "code", 0) != 0:
            return False

        file_token = getattr(getattr(upload_resp, "data", None), "file_token", None)
        if not file_token:
            return False

        display_size = _image_display_size(upload_path)
        replace_image_builder = ReplaceImageRequest.builder().token(file_token)
        if display_size:
            replace_image_builder.width(display_size[0]).height(display_size[1])
        caption = _build_image_caption(caption_text)
        if caption is not None:
            replace_image_builder.caption(caption)
        patch_body = UpdateBlockRequest()
        patch_body.replace_image = replace_image_builder.build()
        patch_req = (
            PatchDocumentBlockRequest.builder()
            .document_id(document_id)
            .block_id(block_id)
            .document_revision_id(-1)
            .request_body(patch_body)
            .build()
        )
        try:
            patch_resp = call_api(api_lock, client.docx.v1.document_block.patch, patch_req, retries=5, backoff=2.0)
        except Exception:
            return False
        if getattr(patch_resp, "code", -1) != 0:
            return False
        return True
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _insert_image_block(
    client,
    document_id: str,
    index: int,
    content: BlockContent,
    base_dir: Path | None,
    api_lock: threading.Lock | None = None,
) -> bool:
    if not isinstance(content, dict):
        return False

    raw_path = str(content.get("path") or "").strip()
    if not raw_path:
        return False

    image_path = _resolve_image_path(raw_path, base_dir)
    if image_path is None:
        return False

    caption_text = _image_caption_text(content)
    image_block = _build_image_block(caption_text)
    create_resp = _create_block_child(client, document_id, index, image_block, api_lock)
    if getattr(create_resp, "code", 0) != 0:
        return False

    children = getattr(getattr(create_resp, "data", None), "children", None) or []
    block_id = getattr(children[0], "block_id", None) if children else None
    if not block_id:
        return False

    return _upload_and_patch_image(client, document_id, block_id, image_path, api_lock, caption_text)


_MAX_GRID_COLUMNS = 5


def _insert_image_grid(
    client,
    document_id: str,
    index: int,
    images: list[BlockContent],
    base_dir: Path | None,
    api_lock: threading.Lock | None = None,
) -> bool:
    """Insert multiple images side-by-side using a Feishu grid block."""
    from lark_oapi.api.docx.v1.model.get_document_block_children_request import (
        GetDocumentBlockChildrenRequest,
    )
    from lark_oapi.api.docx.v1.model.grid import Grid

    valid: list[tuple[Path, str]] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        raw_path = str(img.get("path") or "").strip()
        if not raw_path:
            continue
        path = _resolve_image_path(raw_path, base_dir)
        if path is not None:
            valid.append((path, _image_caption_text(img)))

    if not valid:
        return False

    n = min(len(valid), _MAX_GRID_COLUMNS)
    valid = valid[:n]

    grid_block = (
        Block.builder()
        .block_type(DOCX_BLOCK_TYPE_GRID)
        .grid(Grid.builder().column_size(n).build())
        .build()
    )
    grid_resp = _create_block_child(client, document_id, index, grid_block, api_lock)
    if getattr(grid_resp, "code", 0) != 0:
        return False

    resp_children = getattr(getattr(grid_resp, "data", None), "children", None) or []
    grid_obj = resp_children[0] if resp_children else None
    grid_block_id = getattr(grid_obj, "block_id", None) if grid_obj else None
    if not grid_block_id:
        return False

    column_ids = getattr(grid_obj, "children", None) or []
    if len(column_ids) < n:
        get_req = (
            GetDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(grid_block_id)
            .build()
        )
        get_resp = call_api(api_lock, client.docx.v1.document_block_children.get, get_req)
        if getattr(get_resp, "code", 0) != 0:
            return False
        items = getattr(getattr(get_resp, "data", None), "items", None) or []
        column_ids = [getattr(item, "block_id", None) for item in items]

    if len(column_ids) < n:
        return False

    all_ok = True
    for (img_path, caption_text), col_id in zip(valid, column_ids):
        img_block = _build_image_block(caption_text)
        child_body = (
            CreateDocumentBlockChildrenRequestBody.builder()
            .children([img_block])
            .index(0)
            .build()
        )
        child_req = (
            CreateDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(col_id)
            .request_body(child_body)
            .build()
        )
        child_resp = call_api(api_lock, client.docx.v1.document_block_children.create, child_req)
        if getattr(child_resp, "code", 0) != 0:
            all_ok = False
            continue

        img_children = getattr(getattr(child_resp, "data", None), "children", None) or []
        img_block_id = getattr(img_children[0], "block_id", None) if img_children else None
        if not img_block_id:
            all_ok = False
            continue

        if not _upload_and_patch_image(client, document_id, img_block_id, img_path, api_lock, caption_text):
            all_ok = False

    return all_ok


def _native_image_placeholder(content: BlockContent, base_dir: Path | None) -> tuple[Block, _PendingImageUpload] | None:
    if not isinstance(content, dict):
        return None

    raw_path = str(content.get("path") or "").strip()
    if not raw_path:
        return None

    image_path = _resolve_image_path(raw_path, base_dir)
    if image_path is None:
        return None

    caption_text = _image_caption_text(content)
    temp_block_id = "mri" + uuid.uuid4().hex
    block = _build_image_block(caption_text)
    block.block_id = temp_block_id
    pending = _PendingImageUpload(
        temporary_block_id=temp_block_id,
        image_path=image_path,
        caption_text=caption_text,
    )
    return block, pending


def _upload_pending_images(
    client,
    document_id: str,
    images: list[_PendingImageUpload],
    block_id_map: dict[str, str],
    api_lock: threading.Lock | None,
) -> list[_PendingImageUpload]:
    if not images:
        return []

    upload_items: list[tuple[_PendingImageUpload, str]] = []
    failed: list[_PendingImageUpload] = []
    for image in images:
        block_id = block_id_map.get(image.temporary_block_id)
        if not block_id:
            failed.append(image)
            continue
        upload_items.append((image, block_id))

    if not upload_items:
        return failed

    workers = min(_image_upload_concurrency(), len(upload_items))
    upload_lock = api_lock if workers == 1 else None

    if workers == 1:
        for image, block_id in upload_items:
            ok = _upload_and_patch_image(
                client,
                document_id,
                block_id,
                image.image_path,
                upload_lock,
                image.caption_text,
            )
            if not ok:
                failed.append(image)
        return failed

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="feishu-image") as executor:
        future_to_image = {
            executor.submit(
                _upload_and_patch_image,
                client,
                document_id,
                block_id,
                image.image_path,
                upload_lock,
                image.caption_text,
            ): image
            for image, block_id in upload_items
        }
        for future in as_completed(future_to_image):
            image = future_to_image[future]
            try:
                ok = future.result()
            except Exception:
                ok = False
            if not ok:
                failed.append(image)
    return failed


def _insert_text_fallback_at_index(
    client,
    document_id: str,
    index: int,
    text: str,
    api_lock: threading.Lock | None = None,
) -> int:
    insert_result = _insert_native_blocks(
        client,
        document_id,
        index,
        [_text_block(DOCX_BLOCK_TYPE_TEXT, text)],
        api_lock,
    )
    if insert_result is None:
        return 0
    inserted, _ = insert_result
    return inserted


def _insert_image_group_at_index(
    client,
    document_id: str,
    index: int,
    image_group: list[BlockContent],
    base_dir: Path | None,
    api_lock: threading.Lock | None,
    *,
    image_i: int,
    total_images: int,
) -> tuple[int, int]:
    if len(image_group) == 1:
        image_i += 1
        content = image_group[0]
        if _insert_image_block(client, document_id, index, content, base_dir, api_lock):
            return index + 1, image_i

        fallback = _markdown_image_fallback(content) or "（图片上传失败）"
        return index + _insert_text_fallback_at_index(client, document_id, index, fallback, api_lock), image_i

    image_i += len(image_group)
    if _insert_image_grid(client, document_id, index, image_group, base_dir, api_lock):
        return index + 1, image_i

    for img_content in image_group:
        if _insert_image_block(client, document_id, index, img_content, base_dir, api_lock):
            index += 1
        else:
            fallback = _markdown_image_fallback(img_content) or "（图片上传失败）"
            index += _insert_text_fallback_at_index(client, document_id, index, fallback, api_lock)
    return index, image_i


def _insert_segments_native(
    client,
    document_id: str,
    segments: list[tuple[str, BlockContent]],
    base_dir: Path | None,
    api_lock: threading.Lock | None,
) -> int:
    insert_index = 0
    image_i = 0
    total_images = sum(1 for seg_type, _ in segments if seg_type == "image")
    pending_blocks: list[Block] = []
    pending_first_level_ids: list[str] = []
    pending_images: list[_PendingImageUpload] = []

    def append_pending_block(block: Block) -> None:
        block_id = _ensure_temp_block_id(block)
        pending_blocks.append(block)
        pending_first_level_ids.append(block_id)

    def append_pending_result(rendered: _NativeRenderResult) -> None:
        pending_blocks.extend(rendered.descendants)
        pending_first_level_ids.extend(rendered.first_level_ids)

    def flush_pending() -> None:
        nonlocal insert_index, pending_blocks, pending_first_level_ids, pending_images
        if not pending_blocks:
            return
        insert_result = _insert_native_blocks(
            client,
            document_id,
            insert_index,
            pending_blocks,
            api_lock,
            first_level_ids=pending_first_level_ids,
        )
        if insert_result is None:
            inserted = 0
        else:
            inserted, block_id_map = insert_result
            _upload_pending_images(client, document_id, pending_images, block_id_map, api_lock)
        insert_index += inserted
        pending_blocks = []
        pending_first_level_ids = []
        pending_images = []

    seg_i = 0
    while seg_i < len(segments):
        seg_type, content = segments[seg_i]
        if seg_type == "markdown":
            markdown = str(content)
            append_pending_result(_native_markdown_to_blocks(markdown))
            seg_i += 1
            continue

        if seg_type == "equation":
            block = _native_equation_block(str(content))
            if block is not None:
                append_pending_block(block)
            seg_i += 1
            continue

        image_group: list[BlockContent] = []
        while seg_i < len(segments) and segments[seg_i][0] == "image":
            image_group.append(segments[seg_i][1])
            seg_i += 1
        if len(image_group) > 1:
            flush_pending()
            insert_index, image_i = _insert_image_group_at_index(
                client,
                document_id,
                insert_index,
                image_group,
                base_dir,
                api_lock,
                image_i=image_i,
                total_images=total_images,
            )
            continue

        image_i += 1
        placeholder = _native_image_placeholder(image_group[0], base_dir)
        if placeholder is None:
            fallback = _markdown_image_fallback(image_group[0]) or "（图片上传失败）"
            append_pending_block(_text_block(DOCX_BLOCK_TYPE_TEXT, fallback))
            continue

        image_block, pending_image = placeholder
        append_pending_block(image_block)
        pending_images.append(pending_image)

    flush_pending()
    return insert_index


def create_summary_doc(
    client,
    title: str,
    md_content: str,
    *,
    base_dir: str | Path | None = None,
    arxiv_id: str | None = None,
    api_lock: threading.Lock | None = None,
) -> str | None:
    """
    Create a cloud doc (云文档) from Markdown summary with sections, text, equations,
    lists (bullet/ordered), and native tables (separator row skipped).
    Returns document_id or None.
    """
    try:
        extracted_title, normalized_md_content = _extract_leading_h1_title(md_content)
        doc_title = extracted_title or title
        if arxiv_id:
            doc_title = f"[{arxiv_id}] {doc_title}"
        body = (
            CreateDocumentRequestBody.builder()
            .folder_token("")
            .title(doc_title)
            .build()
        )
        req = CreateDocumentRequest.builder().request_body(body).build()
        resp = call_api(api_lock, client.docx.v1.document.create, req)
        if getattr(resp, "code", 0) != 0:
            return None
        doc = getattr(resp, "data") and getattr(resp.data, "document")
        if not doc:
            return None
        document_id = getattr(doc, "document_id")
        if not document_id:
            return None
        # Keep the platform's private/default permissions. MaxRead deliberately
        # never turns on public-link or anonymous-edit access. Administrators can
        # grant access through normal Feishu folder/document policy if required.
        resolved_base_dir = Path(base_dir).resolve() if base_dir is not None else None
        segments = _split_markdown_segments(normalized_md_content)
        if not segments:
            segments = [("markdown", normalized_md_content.strip() or "（无内容）")]
        _insert_segments_native(client, document_id, segments, resolved_base_dir, api_lock)

        # Subscribe to comment events so the bot can reply to comments
        try:
            from feishu.doc_comments import subscribe_doc_comments
            subscribe_doc_comments(document_id)
        except Exception:
            pass

        return document_id
    except Exception:
        return None


def doc_url(document_id: str, tenant_key: str | None = None) -> str:
    """
    Return the Feishu doc open link (opens in Feishu client or browser).
    open.feishu.cn is API-only; user-facing links use tenant domain when tenant_key is set.
    """
    if tenant_key:
        return f"https://{tenant_key}.feishu.cn/docx/{document_id}"
    return f"https://open.feishu.cn/docx/{document_id}"
