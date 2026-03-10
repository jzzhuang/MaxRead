"""
Parse inline Markdown (**bold**, *italic*) into segments for Feishu text runs with styling.
"""
import re
from typing import TypedDict


class TextSegment(TypedDict):
    text: str
    bold: bool
    italic: bool


def parse_inline(text: str) -> list[TextSegment]:
    """
    Split text into segments with bold/italic flags.
    Supports **bold** and *italic* (and __bold__, _italic_).
    Returns list of {"text": str, "bold": bool, "italic": bool}.
    """
    if not text:
        return []
    segments: list[TextSegment] = []
    i = 0
    n = len(text)
    current = ""
    bold = False
    italic = False

    while i < n:
        if i + 2 <= n and text[i : i + 2] == "**":
            if current:
                segments.append({"text": current, "bold": bold, "italic": italic})
                current = ""
            bold = not bold
            i += 2
            continue
        if i + 2 <= n and text[i : i + 2] == "__":
            if current:
                segments.append({"text": current, "bold": bold, "italic": italic})
                current = ""
            bold = not bold
            i += 2
            continue
        if text[i] == "*" and (i + 1 >= n or text[i + 1] != "*"):
            if current:
                segments.append({"text": current, "bold": bold, "italic": italic})
                current = ""
            italic = not italic
            i += 1
            continue
        if text[i] == "_" and (i + 1 >= n or text[i + 1] != "_"):
            if current:
                segments.append({"text": current, "bold": bold, "italic": italic})
                current = ""
            italic = not italic
            i += 1
            continue
        current += text[i]
        i += 1

    if current:
        segments.append({"text": current, "bold": bold, "italic": italic})
    return segments
