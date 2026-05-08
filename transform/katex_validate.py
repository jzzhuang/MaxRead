"""
Validate LaTeX math expressions using Node.js KaTeX parser.

This module extracts math from Markdown, runs KaTeX parsing for structural
validation, and reports genuinely broken formulas.  This is orthogonal to
the Feishu-specific ``_escape_math_angle_brackets`` workaround — that one
fixes valid LaTeX that Feishu's HTML converter mangles; this one catches
*invalid* LaTeX that will render as garbage or fail silently.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_SCRIPT = Path(__file__).with_name("validate_katex.js")
_NODE = "node"

# Same regexes used by the rest of the pipeline.
_DISPLAY_MATH_RE = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", re.DOTALL)
_INLINE_MATH_RE = re.compile(r"(?<![\$\\])\$(?!\$)(.+?)(?<![\$\\])\$(?!\$)")


def extract_math(markdown: str) -> list[dict]:
    """Extract all math expressions from Markdown.

    Returns a list of ``{"id": int, "latex": str, "display": bool,
    "start": int, "end": int}`` dicts, ordered by position in the source.
    """
    results: list[dict] = []
    seen_spans: set[tuple[int, int]] = set()

    for m in _DISPLAY_MATH_RE.finditer(markdown):
        span = (m.start(), m.end())
        if span not in seen_spans:
            seen_spans.add(span)
            results.append({
                "id": len(results),
                "latex": m.group(1).strip(),
                "display": True,
                "start": m.start(),
                "end": m.end(),
            })

    for m in _INLINE_MATH_RE.finditer(markdown):
        span = (m.start(), m.end())
        if span not in seen_spans:
            seen_spans.add(span)
            results.append({
                "id": len(results),
                "latex": m.group(1).strip(),
                "display": False,
                "start": m.start(),
                "end": m.end(),
            })

    results.sort(key=lambda r: r["start"])
    for i, r in enumerate(results):
        r["id"] = i
    return results


def validate(expressions: list[dict], *, timeout: float = 10.0) -> list[dict]:
    """Run KaTeX validation on extracted math expressions.

    *expressions* should come from :func:`extract_math`.

    Returns a list of ``{"id": int, "latex": str, "ok": bool, "error"?: str,
    "display": bool}`` for every expression that **failed** validation.
    An empty list means all formulas are valid.
    """
    if not expressions:
        return []

    payload = [{"id": e["id"], "latex": e["latex"]} for e in expressions]
    try:
        proc = subprocess.run(
            [_NODE, str(_SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        logger.warning("Node.js not found — skipping KaTeX validation")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("KaTeX validation timed out after %.0fs", timeout)
        return []

    if proc.returncode != 0:
        logger.warning("KaTeX validation failed: %s", proc.stderr.strip())
        return []

    try:
        results = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("KaTeX returned invalid JSON: %s", proc.stdout[:200])
        return []

    # Build lookup from id → original expression info.
    expr_map = {e["id"]: e for e in expressions}
    errors = []
    for r in results:
        if not r.get("ok"):
            orig = expr_map.get(r["id"], {})
            errors.append({
                "id": r["id"],
                "latex": orig.get("latex", r.get("latex", "")),
                "display": orig.get("display", False),
                "error": r.get("error", "unknown"),
            })
    return errors


def validate_markdown(markdown: str, *, timeout: float = 10.0) -> list[dict]:
    """One-shot: extract math from *markdown* and validate it.

    Returns a list of error dicts (empty = all valid).
    """
    exprs = extract_math(markdown)
    return validate(exprs, timeout=timeout)
