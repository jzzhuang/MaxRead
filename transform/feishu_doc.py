"""
Build Feishu docx blocks from parsed (block_type, content) and create cloud docs.
Supports inline bold/italic, bullet/ordered lists, and native tables.
"""
import logging
import re
import shutil
import tempfile
import time
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
)

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


def _insert_markdown_chunk(
    client,
    document_id: str,
    index: int,
    markdown: str,
) -> int:
    if not markdown.strip():
        return 0

    sub_chunks = _split_large_table(markdown)
    if len(sub_chunks) > 1:
        total = 0
        for chunk in sub_chunks:
            total += _insert_markdown_chunk(client, document_id, index + total, chunk)
        return total

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

    image_block = Block.builder().block_type(DOCX_BLOCK_TYPE_IMAGE).image(Image.builder().build()).build()
    create_resp = _create_block_child(client, document_id, index, image_block)
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
        for attempt in range(5):
            patch_resp = client.docx.v1.document_block.patch(patch_req)
            if getattr(patch_resp, "code", 0) == 0:
                return True
            if attempt < 4:
                logger.warning(
                    "Replace image failed for %s (attempt %d/5): %s %s, retrying...",
                    upload_path, attempt + 1,
                    getattr(patch_resp, "code"), getattr(patch_resp, "msg"),
                )
                time.sleep(2)
            else:
                logger.warning(
                    "Replace image failed for %s after 5 attempts: %s %s",
                    upload_path,
                    getattr(patch_resp, "code"), getattr(patch_resp, "msg"),
                )
        return False
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def create_summary_doc(
    client,
    title: str,
    md_content: str,
    *,
    base_dir: str | Path | None = None,
    arxiv_id: str | None = None,
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
        permission_resp = client.drive.v1.permission_public.patch(permission_req)
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
