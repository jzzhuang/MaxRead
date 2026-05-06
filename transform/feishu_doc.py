"""
Build Feishu docx blocks from parsed (block_type, content) and create cloud docs.
Supports inline bold/italic, bullet/ordered lists, and native tables.
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Union

from lark_oapi.api.docx.v1.model.block import Block
from lark_oapi.api.docx.v1.model.image import Image
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
from lark_oapi.api.drive.v1.model.upload_all_media_request import UploadAllMediaRequest
from lark_oapi.api.drive.v1.model.upload_all_media_request_body import (
    UploadAllMediaRequestBody,
)
from lark_oapi.api.drive.v1.model.patch_permission_public_request import (
    PatchPermissionPublicRequest,
)
from lark_oapi.api.drive.v1.model.permission_public_request import (
    PermissionPublicRequest,
)


from .constants import (
    DOCX_BLOCK_TYPE_IMAGE,
    DOCX_BLOCK_TYPE_GRID,
)
from .katex_validate import validate_markdown

from feishu.resilient import call_api

logger = logging.getLogger(__name__)

BlockContent = Union[str, dict[str, str]]


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


def _create_block_child(client, document_id: str, index: int, block: Block, api_lock: threading.Lock | None = None):
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
    return call_api(api_lock, client.docx.v1.document_block_children.create, add_req)


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
        if line.strip().startswith("## "):
            flush_markdown()
        markdown_lines.append(line)

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


def _split_large_table(markdown: str) -> list[str]:
    """Split markdown containing large tables into smaller chunks.

    The Feishu create_descendants API rejects requests with >1000 blocks.
    A table with R rows and C columns generates ~R*C*2+1 blocks, so a
    16-column, 36-row table easily exceeds the limit.  This function
    detects such tables and splits them into multiple smaller tables,
    each keeping the original header.
    """
    lines = markdown.split("\n")
    chunks: list[str] = []
    buf: list[str] = []

    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            buf.append(lines[i])
            i += 1
            continue

        text = "\n".join(buf).strip()
        if text:
            chunks.append(text)
        buf = []

        table_start = i
        while i < len(lines) and lines[i].strip().startswith("|"):
            i += 1
        table_lines = lines[table_start:i]

        header_lines: list[str] = []
        sep_line: str | None = None
        sep_idx: int | None = None
        for j, line in enumerate(table_lines):
            if sep_line is None:
                cells = line.strip().strip("|").split("|")
                if all(re.fullmatch(r"\s*:?-+:?\s*", c) for c in cells):
                    sep_line = line
                    sep_idx = j
                else:
                    header_lines.append(line)

        if sep_line is None or sep_idx is None or not header_lines:
            chunks.append("\n".join(table_lines))
            continue

        data_lines = table_lines[sep_idx + 1:]
        num_cols = max(1, header_lines[0].count("|") - 1)
        total_rows = len(header_lines) + len(data_lines)
        est_blocks = total_rows * num_cols * 2 + 1

        if est_blocks <= _MAX_DESCENDANTS:
            chunks.append("\n".join(table_lines))
            continue

        header_cost = len(header_lines) * num_cols * 2 + 1
        row_cost = num_cols * 2
        max_data = max(1, (_MAX_DESCENDANTS - header_cost) // row_cost)

        for start in range(0, len(data_lines), max_data):
            sub = header_lines + [sep_line] + data_lines[start : start + max_data]
            chunks.append("\n".join(sub))

    text = "\n".join(buf).strip()
    if text:
        chunks.append(text)

    return chunks if chunks else [markdown]


_DISPLAY_MATH_RE = re.compile(r'\$\$(.+?)\$\$', re.DOTALL)
_INLINE_MATH_RE = re.compile(r'(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)')


def _escape_math_angle_brackets(markdown: str) -> str:
    """Replace bare ``<``/``>`` inside LaTeX math with ``\\lt``/``\\gt``
    so that Feishu's Markdown converter does not strip them as HTML tags.

    Without this, a formula like ``$P_{<t}$`` is mangled: Feishu interprets
    ``<t>`` as an HTML tag, removes it, and leaves an unclosed brace that
    swallows the surrounding text.
    """

    def _replace(m: re.Match) -> str:
        full = m.group(0)
        if full.startswith('$$'):
            delim, inner = '$$', full[2:-2]
        else:
            delim, inner = '$', full[1:-1]
        inner = inner.replace('<', '\\lt ')
        inner = inner.replace('>', '\\gt ')
        return delim + inner + delim

    result = _DISPLAY_MATH_RE.sub(_replace, markdown)
    result = _INLINE_MATH_RE.sub(_replace, result)
    return result


def _insert_markdown_chunk(
    client,
    document_id: str,
    index: int,
    markdown: str,
    api_lock: threading.Lock | None = None,
) -> int:
    if not markdown.strip():
        return 0

    sub_chunks = _split_large_table(markdown)
    if len(sub_chunks) > 1:
        total = 0
        for chunk in sub_chunks:
            total += _insert_markdown_chunk(client, document_id, index + total, chunk, api_lock)
        return total

    # ── KaTeX structural validation ──────────────────────────────
    # Run the real KaTeX parser to catch genuinely broken formulas
    # (unmatched braces, bad \frac args, etc.).  The angle-bracket
    # escape below is Feishu-specific and stays regardless.
    math_errors = validate_markdown(markdown)
    for err in math_errors:
        kind = "display" if err["display"] else "inline"
        logger.warning(
            "KaTeX validation error (%s math): %s\n  LaTeX: %s",
            kind,
            err["error"],
            err["latex"][:120],
        )

    markdown = _escape_math_angle_brackets(markdown)

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
    convert_resp = call_api(api_lock, client.docx.v1.document.convert, convert_req)
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
    add_resp = _create_block_descendants(client, document_id, index, first_level_ids, descendants, api_lock)
    if getattr(add_resp, "code", 0) != 0:
        block_types = [getattr(b, "block_type", None) for b in descendants]
        logger.warning(
            "Add markdown descendants failed at index %s: %s %s\n"
            "  block_types=%s\n  markdown=%r",
            index,
            getattr(add_resp, "code"),
            getattr(add_resp, "msg"),
            block_types,
            markdown[:500],
        )
        return 0
    return len(first_level_ids)


def _upload_and_patch_image(
    client,
    document_id: str,
    block_id: str,
    image_path: Path,
    api_lock: threading.Lock | None = None,
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
            upload_resp = call_api(api_lock, client.drive.v1.media.upload_all, upload_req)

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

        display_size = _image_display_size(upload_path)
        replace_image_dict: dict = {"token": file_token}
        if display_size:
            replace_image_dict["width"], replace_image_dict["height"] = display_size
        patch_body = UpdateBlockRequest()
        patch_body.replace_image = ReplaceImageRequest(replace_image_dict)
        patch_req = (
            PatchDocumentBlockRequest.builder()
            .document_id(document_id)
            .block_id(block_id)
            .document_revision_id(-1)
            .request_body(patch_body)
            .build()
        )
        patch_resp = call_api(api_lock, client.docx.v1.document_block.patch, patch_req, retries=5, backoff=2.0)
        return getattr(patch_resp, "code", -1) == 0
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
        logger.warning("Image file not found for Feishu upload: %s", raw_path)
        return False

    image_block = Block.builder().block_type(DOCX_BLOCK_TYPE_IMAGE).image(Image.builder().build()).build()
    create_resp = _create_block_child(client, document_id, index, image_block, api_lock)
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

    return _upload_and_patch_image(client, document_id, block_id, image_path, api_lock)


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
    from lark_oapi.api.docx.v1.model.grid import Grid
    from lark_oapi.api.docx.v1.model.get_document_block_children_request import (
        GetDocumentBlockChildrenRequest,
    )

    valid: list[Path] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        raw_path = str(img.get("path") or "").strip()
        if not raw_path:
            continue
        path = _resolve_image_path(raw_path, base_dir)
        if path is not None:
            valid.append(path)

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
        logger.warning(
            "Create grid block failed: %s %s",
            getattr(grid_resp, "code"),
            getattr(grid_resp, "msg"),
        )
        return False

    resp_children = getattr(getattr(grid_resp, "data", None), "children", None) or []
    grid_obj = resp_children[0] if resp_children else None
    grid_block_id = getattr(grid_obj, "block_id", None) if grid_obj else None
    if not grid_block_id:
        logger.warning("Create grid block returned no block_id")
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
            logger.warning(
                "Get grid column children failed: %s %s",
                getattr(get_resp, "code"),
                getattr(get_resp, "msg"),
            )
            return False
        items = getattr(getattr(get_resp, "data", None), "items", None) or []
        column_ids = [getattr(item, "block_id", None) for item in items]

    if len(column_ids) < n:
        logger.warning("Grid has %d columns, expected %d", len(column_ids), n)
        return False

    all_ok = True
    for img_path, col_id in zip(valid, column_ids):
        img_block = Block.builder().block_type(DOCX_BLOCK_TYPE_IMAGE).image(Image.builder().build()).build()
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

        if not _upload_and_patch_image(client, document_id, img_block_id, img_path, api_lock):
            all_ok = False

    return all_ok


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
            logger.warning("Create doc failed: %s %s", getattr(resp, "code"), getattr(resp, "msg"))
            return None
        doc = getattr(resp, "data") and getattr(resp.data, "document")
        if not doc:
            return None
        document_id = getattr(doc, "document_id")
        if not document_id:
            return None
        permission_req = (
            PatchPermissionPublicRequest.builder()
            .type("docx")
            .token(document_id)
            .request_body(
                PermissionPublicRequest.builder()
                .external_access(True)
                .share_entity("anyone")
                .security_entity("anyone_can_edit")
                .link_share_entity("anyone_editable")
                .build()
            )
            .build()
        )
        permission_resp = call_api(api_lock, client.drive.v1.permission_public.patch, permission_req)
        if getattr(permission_resp, "code", 0) != 0:
            logger.warning(
                "Set doc public-edit permission failed: %s %s",
                getattr(permission_resp, "code"),
                getattr(permission_resp, "msg"),
            )
        resolved_base_dir = Path(base_dir).resolve() if base_dir is not None else None
        segments = _split_markdown_segments(normalized_md_content)
        if not segments:
            segments = [("markdown", normalized_md_content.strip() or "（无内容）")]

        insert_index = 0
        seg_i = 0
        while seg_i < len(segments):
            seg_type, content = segments[seg_i]
            if seg_type == "markdown":
                insert_index += _insert_markdown_chunk(client, document_id, insert_index, str(content), api_lock)
                seg_i += 1
                continue

            image_group: list[BlockContent] = []
            while seg_i < len(segments) and segments[seg_i][0] == "image":
                image_group.append(segments[seg_i][1])
                seg_i += 1

            if len(image_group) == 1:
                if _insert_image_block(client, document_id, insert_index, image_group[0], resolved_base_dir, api_lock):
                    insert_index += 1
                else:
                    fallback = _markdown_image_fallback(image_group[0]) or "（图片上传失败）"
                    insert_index += _insert_markdown_chunk(client, document_id, insert_index, fallback, api_lock)
            else:
                if not _insert_image_grid(client, document_id, insert_index, image_group, resolved_base_dir, api_lock):
                    for img_content in image_group:
                        if _insert_image_block(client, document_id, insert_index, img_content, resolved_base_dir, api_lock):
                            insert_index += 1
                        else:
                            fallback = _markdown_image_fallback(img_content) or "（图片上传失败）"
                            insert_index += _insert_markdown_chunk(client, document_id, insert_index, fallback, api_lock)
                else:
                    insert_index += 1
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
