"""
Parse Markdown into a list of (block_type, content) for Feishu docx.
Supports: headings, display equations (`$$...$$`, `\[...\]`), **bold** and
*italic* (via inline parser), bullet/ordered lists, and tables
(separator row skipped).

Markdown headings of any depth are accepted; levels 4+ are downgraded to Feishu's
deepest supported native heading level.
"""
import re
from typing import Any, Union

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
)

# Content is either plain string or table payload {"rows": [[cell, ...], ...]}
BlockContent = Union[str, dict[str, Any]]


def _is_table_separator(line: str) -> bool:
    """True if line looks like |---|---| (optional spaces). Cells contain only dashes/spaces/colons."""
    row = _parse_table_row(line)
    if not row:
        return False
    return all(re.match(r"^[\s\-:]+$", cell.strip()) for cell in row)


def _parse_table_row(line: str) -> list[str] | None:
    """If line looks like |a|b|c| return ['a','b','c']; else None."""
    stripped = line.strip()
    if "|" not in stripped:
        return None
    parts = [p.strip() for p in stripped.split("|")]
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if not parts:
        return None
    return parts


def _is_bullet_line(line: str) -> bool:
    """Line starts with - or * (single) followed by space."""
    stripped = line.lstrip()
    if stripped.startswith("- "):
        return True
    if len(stripped) >= 2 and stripped[0] == "*" and stripped[1] == " ":
        return True
    return False


def _bullet_content(line: str) -> str:
    """Strip bullet marker and return rest of line."""
    stripped = line.lstrip()
    if stripped.startswith("- "):
        return stripped[2:].strip()
    if stripped.startswith("* "):
        return stripped[2:].strip()
    return stripped


def _is_ordered_line(line: str) -> bool:
    """Line starts with digit(s) + . + space."""
    return bool(re.match(r"^\s*\d+\.\s+", line))


def _ordered_content(line: str) -> str:
    """Strip ordered list marker (1. 2. etc) and return rest."""
    stripped = line.lstrip()
    m = re.match(r"^\d+\.\s+", stripped)
    if m:
        return stripped[m.end() :].strip()
    return stripped


def _is_display_equation(line: str) -> tuple[bool, str]:
    """If line is only a supported display equation, return (True, content)."""
    stripped = line.strip()
    for pattern in (r"\$\$(.*?)\$\$", r"\\\[(.*?)\\\]"):
        m = re.fullmatch(pattern, stripped)
        if m:
            return (True, m.group(1).strip())
    return (False, "")


def _is_divider_line(line: str) -> bool:
    """True if line is a Markdown horizontal rule using hyphens."""
    return bool(re.fullmatch(r"\s*-{3,}\s*", line))


def _parse_heading_line(line: str) -> tuple[int, str] | None:
    """Parse Markdown headings and map level 4+ to heading3."""
    m = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
    if not m:
        return None
    level = len(m.group(1))
    content = m.group(2).strip()
    if not content:
        return None
    if level == 1:
        return (DOCX_BLOCK_TYPE_HEADING1, content)
    if level == 2:
        return (DOCX_BLOCK_TYPE_HEADING2, content)
    return (DOCX_BLOCK_TYPE_HEADING3, content)


def _parse_image_line(line: str) -> dict[str, str] | None:
    """Parse a standalone Markdown image line like ![alt](path)."""
    stripped = line.strip()
    m = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", stripped)
    if not m:
        return None
    return {"alt": m.group(1).strip(), "path": m.group(2).strip()}


def parse_md_to_blocks(md: str) -> list[tuple[int, BlockContent]]:
    """
    Parse Markdown into a list of (block_type, content) for Feishu docx.
    content is str for text/headings/lists, or {"rows": [[cell,...],...]} for table.
    Tables: separator row (|---|---|) is skipped and not emitted.
    Only a whole line that is exactly `$$...$$` or `\[...\]` becomes an equation
    block; inline math stays inside the text block and is rendered later.
    """
    blocks: list[tuple[int, BlockContent]] = []
    lines = md.split("\n")
    para_lines: list[str] = []
    j = 0
    while j < len(lines):
        line = lines[j]
        stripped = line.strip()
        # Whole-line display equation → single equation block
        is_eq, eq_content = _is_display_equation(line)
        if is_eq and eq_content:
            if para_lines:
                t = "\n".join(para_lines).strip()
                if t:
                    blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
                para_lines = []
            blocks.append((DOCX_BLOCK_TYPE_EQUATION, eq_content))
            j += 1
            continue
        if not stripped:
            if para_lines:
                t = "\n".join(para_lines).strip()
                if t:
                    blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
                para_lines = []
            j += 1
            continue
        if _is_divider_line(line):
            if para_lines:
                t = "\n".join(para_lines).strip()
                if t:
                    blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
                para_lines = []
            blocks.append((DOCX_BLOCK_TYPE_DIVIDER, ""))
            j += 1
            continue
        heading = _parse_heading_line(line)
        if heading is not None:
            if para_lines:
                t = "\n".join(para_lines).strip()
                if t:
                    blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
                para_lines = []
            blocks.append(heading)
            j += 1
            continue
        image = _parse_image_line(line)
        if image is not None:
            if para_lines:
                t = "\n".join(para_lines).strip()
                if t:
                    blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
                para_lines = []
            blocks.append((DOCX_BLOCK_TYPE_IMAGE, image))
            j += 1
            continue
        # Table: collect consecutive table lines, skip separator
        row = _parse_table_row(line)
        if row is not None:
            if _is_table_separator(line):
                j += 1
                continue
            # Collect all consecutive table rows
            table_rows = [row]
            k = j + 1
            while k < len(lines):
                next_line = lines[k]
                next_row = _parse_table_row(next_line)
                if next_row is None:
                    break
                if _is_table_separator(next_line):
                    k += 1
                    continue
                table_rows.append(next_row)
                k += 1
            if para_lines:
                t = "\n".join(para_lines).strip()
                if t:
                    blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
                para_lines = []
            blocks.append((DOCX_BLOCK_TYPE_TABLE, {"rows": table_rows}))
            j = k
            continue
        if _is_bullet_line(line):
            if para_lines:
                t = "\n".join(para_lines).strip()
                if t:
                    blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
                para_lines = []
            blocks.append((DOCX_BLOCK_TYPE_BULLET, _bullet_content(line)))
            j += 1
            continue
        if _is_ordered_line(line):
            if para_lines:
                t = "\n".join(para_lines).strip()
                if t:
                    blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
                para_lines = []
            blocks.append((DOCX_BLOCK_TYPE_ORDERED, _ordered_content(line)))
            j += 1
            continue
        para_lines.append(stripped)
        j += 1
    if para_lines:
        t = "\n".join(para_lines).strip()
        if t:
            blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
    return blocks
