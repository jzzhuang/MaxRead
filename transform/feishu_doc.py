"""
Build Feishu docx blocks from parsed (block_type, content) and create cloud docs.
Supports inline bold/italic, bullet/ordered lists, and native tables.
"""
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4
from typing import Any, Union

from lark_oapi.api.docx.v1.model.block import Block
from lark_oapi.api.docx.v1.model.image import Image
from lark_oapi.api.docx.v1.model.text import Text
from lark_oapi.api.docx.v1.model.text_element import TextElement
from lark_oapi.api.docx.v1.model.text_run import TextRun
from lark_oapi.api.docx.v1.model.text_element_style import TextElementStyle
from lark_oapi.api.docx.v1.model.divider import Divider
from lark_oapi.api.docx.v1.model.equation import Equation
from lark_oapi.api.docx.v1.model.table import Table
from lark_oapi.api.docx.v1.model.table_property import TableProperty
from lark_oapi.api.docx.v1.model.create_document_request import CreateDocumentRequest
from lark_oapi.api.docx.v1.model.create_document_request_body import CreateDocumentRequestBody
from lark_oapi.api.docx.v1.model.create_document_block_children_request import (
    CreateDocumentBlockChildrenRequest,
)
from lark_oapi.api.docx.v1.model.create_document_block_children_request_body import (
    CreateDocumentBlockChildrenRequestBody,
)
from lark_oapi.api.docx.v1.model.convert_document_request import ConvertDocumentRequest
from lark_oapi.api.docx.v1.model.convert_document_request_body import ConvertDocumentRequestBody
from lark_oapi.api.docx.v1.model.create_document_block_descendant_request import (
    CreateDocumentBlockDescendantRequest,
)
from lark_oapi.api.docx.v1.model.create_document_block_descendant_request_body import (
    CreateDocumentBlockDescendantRequestBody,
)
from lark_oapi.api.docx.v1.model.patch_document_block_request import (
    PatchDocumentBlockRequest,
)
from lark_oapi.api.docx.v1.model.replace_image_request import ReplaceImageRequest
from lark_oapi.api.docx.v1.model.update_block_request import UpdateBlockRequest
from lark_oapi.api.docx.v1.model.table_cell import TableCell
from lark_oapi.api.drive.v1.model.upload_all_media_request import UploadAllMediaRequest
from lark_oapi.api.drive.v1.model.upload_all_media_request_body import (
    UploadAllMediaRequestBody,
)

from .constants import (
    DOCX_BLOCK_TYPE_TEXT,
    DOCX_BLOCK_TYPE_HEADING1,
    DOCX_BLOCK_TYPE_HEADING2,
    DOCX_BLOCK_TYPE_HEADING3,
    DOCX_BLOCK_TYPE_EQUATION,
    DOCX_BLOCK_TYPE_BULLET,
    DOCX_BLOCK_TYPE_ORDERED,
    DOCX_BLOCK_TYPE_DIVIDER,
    DOCX_BLOCK_TYPE_TABLE,
    DOCX_BLOCK_TYPE_IMAGE,
    TABLE_FULL_WIDTH,
)
from .inline import parse_inline

logger = logging.getLogger(__name__)

BlockContent = Union[str, dict[str, Any]]
INLINE_EQUATION_RE = re.compile(
    r"""
    \$\$(?P<display_dollar>.+?)\$\$
    |\\\[(?P<display_bracket>.+?)\\\]
    |\\\((?P<inline_paren>.+?)\\\)
    |(?<!\$)\$(?!\$)(?P<inline_dollar>.+?)(?<!\$)\$(?!\$)
    """,
    re.VERBOSE | re.DOTALL,
)


def _build_text_elements(content: str) -> list:
    """Build list of TextElement from content string, parsing **bold** and *italic*."""
    segments = parse_inline(content)
    elements = []
    for seg in segments:
        text = seg["text"]
        if not text:
            continue
        style = TextElementStyle.builder()
        if seg.get("bold"):
            style.bold(True)
        if seg.get("italic"):
            style.italic(True)
        run = TextRun.builder().content(text)
        if seg.get("bold") or seg.get("italic"):
            run = run.text_element_style(style.build())
        elements.append(TextElement.builder().text_run(run.build()).build())
    if not elements:
        elements = [
            TextElement.builder()
            .text_run(TextRun.builder().content(" ").build())
            .build()
        ]
    return elements


def _build_text_elements_with_inline_equations(content: str) -> list:
    """Build TextElements from content with common inline/display LaTeX syntax."""
    if not content:
        return [
            TextElement.builder()
            .text_run(TextRun.builder().content(" ").build())
            .build()
        ]
    elements: list = []
    cursor = 0
    for match in INLINE_EQUATION_RE.finditer(content):
        text_part = content[cursor : match.start()]
        if text_part:
            elements.extend(_build_text_elements(text_part))

        eq_content = next(
            (group.strip() for group in match.groupdict().values() if group and group.strip()),
            "",
        )
        if eq_content:
            eq = Equation.builder().content(eq_content).build()
            elements.append(TextElement.builder().equation(eq).build())
        cursor = match.end()

    trailing_text = content[cursor:]
    if trailing_text:
        elements.extend(_build_text_elements(trailing_text))

    if not elements:
        elements = [
            TextElement.builder()
            .text_run(TextRun.builder().content(" ").build())
            .build()
        ]
    return elements


def build_block(block_type: int, content: BlockContent) -> Block:
    """Build a Feishu Block from block_type and content (string or table dict)."""
    # Equation is a Text Block (type 2) with one equation element (per Feishu doc)
    if block_type == DOCX_BLOCK_TYPE_EQUATION:
        eq_content = (content if isinstance(content, str) else "").strip()
        eq = Equation.builder().content(eq_content).build()
        elem = TextElement.builder().equation(eq).build()
        text_obj = Text.builder().elements([elem]).build()
        return Block.builder().block_type(DOCX_BLOCK_TYPE_TEXT).text(text_obj).build()

    if block_type == DOCX_BLOCK_TYPE_TABLE:
        if not isinstance(content, dict) or "rows" not in content:
            text_obj = Text.builder().elements([
                TextElement.builder().text_run(TextRun.builder().content(" ").build()).build()
            ]).build()
            return Block.builder().block_type(DOCX_BLOCK_TYPE_TEXT).text(text_obj).build()
        rows = content["rows"]
        if not rows:
            text_obj = Text.builder().elements([
                TextElement.builder().text_run(TextRun.builder().content(" ").build()).build()
            ]).build()
            return Block.builder().block_type(DOCX_BLOCK_TYPE_TEXT).text(text_obj).build()
        row_count = len(rows)
        col_count = max(len(r) for r in rows) if rows else 0
        if col_count == 0:
            text_obj = Text.builder().elements([
                TextElement.builder().text_run(TextRun.builder().content(" ").build()).build()
            ]).build()
            return Block.builder().block_type(DOCX_BLOCK_TYPE_TEXT).text(text_obj).build()
        # Equal column widths so table spans document content width on desktop
        width_per_col = TABLE_FULL_WIDTH // col_count
        column_widths = [width_per_col] * col_count
        # Add remainder to first column so total equals TABLE_FULL_WIDTH
        column_widths[0] += TABLE_FULL_WIDTH - sum(column_widths)
        table_prop = (
            TableProperty.builder()
            .row_size(row_count)
            .column_size(col_count)
            .column_width(column_widths)
            .header_row(True)
            .build()
        )
        table = Table.builder().property(table_prop).cells([]).build()
        return Block.builder().block_type(DOCX_BLOCK_TYPE_TABLE).table(table).build()

    if block_type == DOCX_BLOCK_TYPE_IMAGE:
        return Block.builder().block_type(DOCX_BLOCK_TYPE_IMAGE).image(Image.builder().build()).build()

    if block_type == DOCX_BLOCK_TYPE_DIVIDER:
        return Block.builder().block_type(DOCX_BLOCK_TYPE_DIVIDER).divider(Divider.builder().build()).build()

    # Text, heading, bullet, ordered: content may contain **, *, and inline LaTeX.
    text_str = content if isinstance(content, str) else ""
    text_obj = Text.builder().elements(_build_text_elements_with_inline_equations(text_str or " ")).build()
    builder = Block.builder().block_type(block_type)

    if block_type == DOCX_BLOCK_TYPE_HEADING1:
        return builder.heading1(text_obj).build()
    if block_type == DOCX_BLOCK_TYPE_HEADING2:
        return builder.heading2(text_obj).build()
    if block_type == DOCX_BLOCK_TYPE_HEADING3:
        return builder.heading3(text_obj).build()
    if block_type == DOCX_BLOCK_TYPE_BULLET:
        return builder.bullet(text_obj).build()
    if block_type == DOCX_BLOCK_TYPE_ORDERED:
        return builder.ordered(text_obj).build()
    return builder.text(text_obj).build()


def _build_table_descendant_body(
    rows: list[list[str]], index: int
) -> CreateDocumentBlockDescendantRequestBody | None:
    """Build a nested table payload so cell content is created with the table itself."""
    if not rows:
        return None
    row_count = len(rows)
    col_count = max(len(r) for r in rows) if rows else 0
    if row_count == 0 or col_count == 0:
        return None

    width_per_col = TABLE_FULL_WIDTH // col_count
    column_widths = [width_per_col] * col_count
    column_widths[0] += TABLE_FULL_WIDTH - sum(column_widths)

    table_id = f"tbl_{uuid4().hex}"
    descendants: list[Block] = []
    cell_ids: list[str] = []

    for row in rows:
        padded_row = [str(c).strip() for c in row] + [""] * (col_count - len(row))
        for raw_cell in padded_row[:col_count]:
            cell_id = f"cell_{uuid4().hex}"
            normalized = raw_cell.replace("\n", " ").replace("\r", " ").strip()
            child_ids: list[str] = []
            cell_descendants: list[Block] = []

            if normalized:
                text_block_id = f"cell_text_{uuid4().hex}"
                text_block = (
                    Block.builder()
                    .block_id(text_block_id)
                    .block_type(DOCX_BLOCK_TYPE_TEXT)
                    .text(Text.builder().elements(_build_text_elements_with_inline_equations(normalized)).build())
                    .children([])
                    .build()
                )
                cell_descendants.append(text_block)
                child_ids.append(text_block_id)

            cell_block = (
                Block.builder()
                .block_id(cell_id)
                .block_type(32)
                .table_cell(TableCell.builder().build())
                .children(child_ids)
                .build()
            )
            descendants.append(cell_block)
            descendants.extend(cell_descendants)
            cell_ids.append(cell_id)

    table_prop = (
        TableProperty.builder()
        .row_size(row_count)
        .column_size(col_count)
        .column_width(column_widths)
        .header_row(True)
        .build()
    )
    table_block = (
        Block.builder()
        .block_id(table_id)
        .block_type(DOCX_BLOCK_TYPE_TABLE)
        .table(Table.builder().property(table_prop).cells(cell_ids).build())
        .children(cell_ids)
        .build()
    )
    descendants.insert(0, table_block)
    return (
        CreateDocumentBlockDescendantRequestBody.builder()
        .children_id([table_id])
        .descendants(descendants)
        .index(index)
        .build()
    )


def _markdown_image_fallback(content: BlockContent) -> str:
    if not isinstance(content, dict):
        return ""
    alt = str(content.get("alt") or "").strip()
    path = str(content.get("path") or "").strip()
    if alt and path:
        return f"{alt}: {path}"
    return path or alt


def _resolve_image_path(image_path: str, base_dir: Path | None) -> Path | None:
    candidate = Path(image_path).expanduser()
    if not candidate.is_absolute():
        if base_dir is None:
            return None
        candidate = base_dir / candidate
    candidate = candidate.resolve()
    return candidate if candidate.is_file() else None


def _prepare_upload_image(image_path: Path) -> tuple[Path, str | None]:
    if image_path.suffix.lower() != ".pdf":
        return image_path, None

    if shutil.which("pdftoppm") is None:
        raise RuntimeError("pdftoppm not found; cannot convert PDF figures to PNG")

    temp_dir = tempfile.mkdtemp(prefix="maxread-feishu-img-")
    out_prefix = Path(temp_dir) / image_path.stem
    proc = subprocess.run(
        [
            "pdftoppm",
            "-f",
            "1",
            "-l",
            "1",
            "-singlefile",
            "-png",
            str(image_path),
            str(out_prefix),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"PDF to PNG conversion failed: {err}")

    png_path = out_prefix.with_suffix(".png")
    if not png_path.is_file():
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError("PDF to PNG conversion did not produce an output file")
    return png_path, temp_dir


def _create_block_child(client, document_id: str, index: int, block: Block):
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
    return client.docx.v1.document_block_children.create(add_req)


def _create_block_descendants(
    client,
    document_id: str,
    index: int,
    children_id: list[str],
    descendants: list[Block],
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
    return client.docx.v1.document_block_descendant.create(add_req)


def _strip_table_merge_info(blocks: list[Block]) -> None:
    for block in blocks:
        table = getattr(block, "table", None)
        if table is None:
            continue
        table_property = getattr(table, "property", None)
        if table_property is not None:
            table_property.merge_info = None


def _parse_markdown_image_line(line: str) -> dict[str, str] | None:
    stripped = line.strip()
    match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", stripped)
    if not match:
        return None
    return {"alt": match.group(1).strip(), "path": match.group(2).strip()}


def _split_markdown_segments(
    md_content: str,
    base_dir: Path | None,
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

    for line in md_content.split("\n"):
        image = _parse_markdown_image_line(line)
        if image is not None:
            flush_markdown()
            segments.append(("image", image))
            continue
        markdown_lines.append(line)

    flush_markdown()
    return segments


def _insert_markdown_chunk(
    client,
    document_id: str,
    index: int,
    markdown: str,
) -> int:
    if not markdown.strip():
        return 0

    convert_req = (
        ConvertDocumentRequest.builder()
        .request_body(
            ConvertDocumentRequestBody.builder()
            .content_type("markdown")
            .content(markdown)
            .build()
        )
        .build()
    )
    convert_resp = client.docx.v1.document.convert(convert_req)
    if getattr(convert_resp, "code", 0) != 0:
        logger.warning(
            "Convert markdown failed at index %s: %s %s",
            index,
            getattr(convert_resp, "code"),
            getattr(convert_resp, "msg"),
        )
        return 0

    data = getattr(convert_resp, "data", None)
    first_level_ids = list(getattr(data, "first_level_block_ids", None) or [])
    descendants = list(getattr(data, "blocks", None) or [])
    if not first_level_ids or not descendants:
        return 0

    _strip_table_merge_info(descendants)
    add_resp = _create_block_descendants(client, document_id, index, first_level_ids, descendants)
    if getattr(add_resp, "code", 0) != 0:
        logger.warning(
            "Add markdown descendants failed at index %s: %s %s",
            index,
            getattr(add_resp, "code"),
            getattr(add_resp, "msg"),
        )
        return 0
    return len(first_level_ids)


def _insert_image_block(
    client,
    document_id: str,
    index: int,
    content: BlockContent,
    base_dir: Path | None,
) -> bool:
    if not isinstance(content, dict):
        return False

    raw_path = str(content.get("path") or "").strip()
    if not raw_path:
        return False

    image_path = _resolve_image_path(raw_path, base_dir)
    if image_path is None:
        logger.warning("Image file not found for Feishu upload: %s", raw_path)
        return False

    create_resp = _create_block_child(client, document_id, index, build_block(DOCX_BLOCK_TYPE_IMAGE, content))
    if getattr(create_resp, "code", 0) != 0:
        logger.warning(
            "Create image block failed at index %s: %s %s",
            index,
            getattr(create_resp, "code"),
            getattr(create_resp, "msg"),
        )
        return False

    children = getattr(getattr(create_resp, "data", None), "children", None) or []
    block_id = getattr(children[0], "block_id", None) if children else None
    if not block_id:
        logger.warning("Create image block returned no block_id at index %s", index)
        return False

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
            upload_resp = client.drive.v1.media.upload_all(upload_req)

        if getattr(upload_resp, "code", 0) != 0:
            logger.warning(
                "Upload image failed for %s: %s %s",
                upload_path,
                getattr(upload_resp, "code"),
                getattr(upload_resp, "msg"),
            )
            return False

        file_token = getattr(getattr(upload_resp, "data", None), "file_token", None)
        if not file_token:
            logger.warning("Upload image returned no file token for %s", upload_path)
            return False

        patch_body = UpdateBlockRequest()
        patch_body.replace_image = ReplaceImageRequest({"token": file_token})
        patch_req = (
            PatchDocumentBlockRequest.builder()
            .document_id(document_id)
            .block_id(block_id)
            .document_revision_id(-1)
            .request_body(patch_body)
            .build()
        )
        patch_resp = client.docx.v1.document_block.patch(patch_req)
        if getattr(patch_resp, "code", 0) != 0:
            logger.warning(
                "Replace image failed for %s: %s %s",
                upload_path,
                getattr(patch_resp, "code"),
                getattr(patch_resp, "msg"),
            )
            return False
        return True
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def create_summary_doc(
    client,
    title: str,
    md_content: str,
    *,
    base_dir: str | Path | None = None,
) -> str | None:
    """
    Create a cloud doc (云文档) from Markdown summary with sections, text, equations,
    lists (bullet/ordered), and native tables (separator row skipped).
    Returns document_id or None.
    """
    try:
        body = (
            CreateDocumentRequestBody.builder()
            .folder_token("")
            .title(title)
            .build()
        )
        req = CreateDocumentRequest.builder().request_body(body).build()
        resp = client.docx.v1.document.create(req)
        if getattr(resp, "code", 0) != 0:
            logger.warning("Create doc failed: %s %s", getattr(resp, "code"), getattr(resp, "msg"))
            return None
        doc = getattr(resp, "data") and getattr(resp.data, "document")
        if not doc:
            return None
        document_id = getattr(doc, "document_id")
        if not document_id:
            return None
        resolved_base_dir = Path(base_dir).resolve() if base_dir is not None else None
        segments = _split_markdown_segments(md_content, resolved_base_dir)
        if not segments:
            segments = [("markdown", md_content.strip() or "（无内容）")]

        insert_index = 0
        for segment_type, content in segments:
            if segment_type == "markdown":
                insert_index += _insert_markdown_chunk(client, document_id, insert_index, str(content))
                continue

            if _insert_image_block(client, document_id, insert_index, content, resolved_base_dir):
                insert_index += 1
                continue

            fallback = _markdown_image_fallback(content) or "（图片上传失败）"
            insert_index += _insert_markdown_chunk(client, document_id, insert_index, fallback)
        return document_id
    except Exception as e:
        logger.exception("Create summary doc failed: %s", e)
        return None


def doc_url(document_id: str, tenant_key: str | None = None) -> str:
    """
    Return the Feishu doc open link (opens in Feishu client or browser).
    open.feishu.cn is API-only; user-facing links use tenant domain when tenant_key is set.
    """
    if tenant_key:
        return f"https://{tenant_key}.feishu.cn/docx/{document_id}"
    return f"https://open.feishu.cn/docx/{document_id}"
