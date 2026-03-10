"""
Build Feishu docx blocks from parsed (block_type, content) and create cloud docs.
Supports inline bold/italic, bullet/ordered lists, and native tables.
"""
import logging
import re
from typing import Any, Union

from lark_oapi.api.docx.v1.model.block import Block
from lark_oapi.api.docx.v1.model.text import Text
from lark_oapi.api.docx.v1.model.text_element import TextElement
from lark_oapi.api.docx.v1.model.text_run import TextRun
from lark_oapi.api.docx.v1.model.text_element_style import TextElementStyle
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
from lark_oapi.api.docx.v1.model.get_document_block_children_request import GetDocumentBlockChildrenRequest
from lark_oapi.api.docx.v1.model.text_style import TextStyle

from .constants import (
    DOCX_BLOCK_TYPE_TEXT,
    DOCX_BLOCK_TYPE_HEADING1,
    DOCX_BLOCK_TYPE_HEADING2,
    DOCX_BLOCK_TYPE_HEADING3,
    DOCX_BLOCK_TYPE_EQUATION,
    DOCX_BLOCK_TYPE_BULLET,
    DOCX_BLOCK_TYPE_ORDERED,
    DOCX_BLOCK_TYPE_TABLE,
    TABLE_FULL_WIDTH,
)
from .parser import parse_md_to_blocks
from .inline import parse_inline

logger = logging.getLogger(__name__)

BlockContent = Union[str, dict[str, Any]]


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
    """Build list of TextElement from content that may contain $$...$$ inline equations.
    Splits on $$...$$; text segments get bold/italic via _build_text_elements, equation
    segments become equation elements. Result is a single block's elements (no separate equation blocks)."""
    if not content:
        return [
            TextElement.builder()
            .text_run(TextRun.builder().content(" ").build())
            .build()
        ]
    parts = re.split(r"\$\$([^$]*)\$\$", content)
    elements: list = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # Equation segment
            eq_content = part.strip()
            if eq_content:
                eq = Equation.builder().content(eq_content).build()
                elements.append(TextElement.builder().equation(eq).build())
        else:
            # Text segment (may contain ** and *)
            if part:
                elements.extend(_build_text_elements(part))
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

    # Text, heading, bullet, ordered: content is string, may contain **, *, and inline $$...$$
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


def _fill_table_cells(client, document_id: str, table_block_id: str, rows: list[list[str]]) -> None:
    """After creating a table block, get its cell blocks and add a text child block to each cell.
    Table cells (block_type 32) do not support update_text; content must be in child blocks."""
    if not rows:
        return
    col_count = max(len(r) for r in rows)
    # Flatten row-major: row0_cell0, row0_cell1, ..., row1_cell0, ... (strip to avoid trailing newlines)
    cell_texts = []
    for row in rows:
        padded = [str(c).strip() for c in row] + [""] * (col_count - len(row))
        cell_texts.extend(padded[:col_count])
    # Get table's children (table_cell blocks)
    get_req = (
        GetDocumentBlockChildrenRequest.builder()
        .document_id(document_id)
        .block_id(table_block_id)
        .page_size(500)
        .build()
    )
    get_resp = client.docx.v1.document_block_children.get(get_req)
    if getattr(get_resp, "code", 0) != 0:
        logger.warning("Get table children failed: %s %s", getattr(get_resp, "code"), getattr(get_resp, "msg"))
        return
    items = getattr(get_resp, "data") and getattr(get_resp.data, "items") or []
    # Paginate if has_more
    while getattr(get_resp.data, "has_more") and getattr(get_resp.data, "page_token"):
        get_req = (
            GetDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(table_block_id)
            .page_size(500)
            .page_token(get_resp.data.page_token)
            .build()
        )
        get_resp = client.docx.v1.document_block_children.get(get_req)
        if getattr(get_resp, "code", 0) != 0:
            break
        more = getattr(get_resp, "data") and getattr(get_resp.data, "items") or []
        items.extend(more)
    if len(items) < len(cell_texts):
        logger.warning("Table has %s cells but got %s child blocks", len(cell_texts), len(items))
    # Add a text block as child of each table cell (cells don't support update_text).
    # Normalize content: no newlines (replace with space) to avoid extra line breaks; skip empty cells.
    for i, block in enumerate(items):
        if i >= len(cell_texts):
            break
        raw = cell_texts[i]
        cell_content = (raw if isinstance(raw, str) else str(raw)).strip().replace("\n", " ").replace("\r", " ")
        if not cell_content:
            continue  # Skip adding block for empty cells to avoid trailing blank line
        text_elements = _build_text_elements(cell_content)
        # Use folded=True to avoid extra line break after paragraph in table cells
        cell_text_style = TextStyle.builder().folded(True).build()
        text_obj = Text.builder().elements(text_elements).style(cell_text_style).build()
        text_block = Block.builder().block_type(DOCX_BLOCK_TYPE_TEXT).text(text_obj).build()
        child_body = (
            CreateDocumentBlockChildrenRequestBody.builder()
            .children([text_block])
            .index(0)
            .build()
        )
        add_req = (
            CreateDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(block.block_id)
            .request_body(child_body)
            .build()
        )
        add_resp = client.docx.v1.document_block_children.create(add_req)
        if getattr(add_resp, "code", 0) != 0:
            logger.warning(
                "Add text to table cell %s failed: %s %s",
                block.block_id,
                getattr(add_resp, "code"),
                getattr(add_resp, "msg"),
            )


def create_summary_doc(client, title: str, md_content: str) -> str | None:
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
        parsed = parse_md_to_blocks(md_content)
        if not parsed:
            parsed = [(DOCX_BLOCK_TYPE_TEXT, md_content.strip() or "（无内容）")]
        for index, (btype, content) in enumerate(parsed):
            block = build_block(btype, content)
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
            add_resp = client.docx.v1.document_block_children.create(add_req)
            if getattr(add_resp, "code", 0) != 0:
                logger.warning(
                    "Add block failed at index %s: %s %s",
                    index,
                    getattr(add_resp, "code"),
                    getattr(add_resp, "msg"),
                )
            elif btype == DOCX_BLOCK_TYPE_TABLE and isinstance(content, dict) and content.get("rows"):
                # Fill table cells: get created table block id and update each cell
                data = getattr(add_resp, "data")
                children = getattr(data, "children") if data else None
                if children and len(children) >= 1 and getattr(children[0], "block_id", None):
                    _fill_table_cells(client, document_id, children[0].block_id, content["rows"])
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
