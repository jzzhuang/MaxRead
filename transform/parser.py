"""
Parse Markdown into a list of (block_type, content) for Feishu docx.
Supports: headings, $$...$$ equations, **bold** and *italic* (via inline parser),
bullet/ordered lists, and tables (separator row skipped).
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
    DOCX_BLOCK_TYPE_TABLE,
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
    """If line is only $$...$$ (display equation), return (True, content). Else (False, '')."""
    stripped = line.strip()
    m = re.fullmatch(r"\$\$([^$]*)\$\$", stripped)
    if m:
        return (True, m.group(1).strip())
    return (False, "")


def parse_md_to_blocks(md: str) -> list[tuple[int, BlockContent]]:
    """
    Parse Markdown into a list of (block_type, content) for Feishu docx.
    content is str for text/headings/lists, or {"rows": [[cell,...],...]} for table.
    Tables: separator row (|---|---|) is skipped and not emitted.
    Only a whole line that is exactly $$...$$ becomes an equation block; $$...$$ inside
    a paragraph stays inline and is rendered as equation elements in the same text block.
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
        if stripped.startswith("### "):
            if para_lines:
                t = "\n".join(para_lines).strip()
                if t:
                    blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
                para_lines = []
            blocks.append((DOCX_BLOCK_TYPE_HEADING3, stripped[4:].strip()))
            j += 1
            continue
        if stripped.startswith("## "):
            if para_lines:
                t = "\n".join(para_lines).strip()
                if t:
                    blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
                para_lines = []
            blocks.append((DOCX_BLOCK_TYPE_HEADING2, stripped[3:].strip()))
            j += 1
            continue
        if stripped.startswith("# "):
            if para_lines:
                t = "\n".join(para_lines).strip()
                if t:
                    blocks.append((DOCX_BLOCK_TYPE_TEXT, t))
                para_lines = []
            blocks.append((DOCX_BLOCK_TYPE_HEADING1, stripped[2:].strip()))
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
