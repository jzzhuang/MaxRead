"""Feishu-specific LaTeX compatibility checks.

KaTeX accepts more commands than Feishu's Markdown/formula converter.  This
module flags commands that are valid LaTeX/KaTeX but known to fail in Feishu.
"""
from __future__ import annotations

import re

from .katex_validate import extract_math

# Keep this list deliberately small and evidence-based.  Add commands here only
# after seeing Feishu reject or mis-render them.
UNSUPPORTED_COMMANDS: dict[str, str] = {
    r"\ell": "replace with plain l or another Feishu-supported symbol",
    r"\mathbbm": "replace indicator notation with \\mathbf{1}, \\mathbb{1}, or plain 1",
    r"\mathds": "replace indicator notation with \\mathbf{1}, \\mathbb{1}, or plain 1",
    r"\ppl": "write perplexity as \\mathrm{ppl}_{...} or \\operatorname{ppl}_{...}",
    r"\qquad": "remove the spacing command or use normal text spacing",
}

_COMMAND_RE = re.compile(r"\\[A-Za-z]+|\\.")


def find_unsupported_commands(markdown: str) -> list[dict]:
    """Return Feishu-incompatible LaTeX commands found in Markdown math.

    The returned dicts intentionally mirror ``katex_validate.validate_markdown``
    errors so callers can present and repair both issue types with one flow.
    """
    errors: list[dict] = []
    seen: set[tuple[int, str]] = set()

    for expr in extract_math(markdown):
        latex = expr["latex"]
        for match in _COMMAND_RE.finditer(latex):
            command = match.group(0)
            suggestion = UNSUPPORTED_COMMANDS.get(command)
            if not suggestion:
                continue

            key = (expr["id"], command)
            if key in seen:
                continue
            seen.add(key)

            line_offset = latex.count("\n", 0, match.start())
            line = (expr.get("line") or 1) + line_offset
            errors.append({
                "id": expr["id"],
                "latex": latex,
                "display": expr.get("display", False),
                "error": (
                    f"Feishu unsupported LaTeX command {command}: {suggestion}."
                ),
                "start": expr.get("start"),
                "end": expr.get("end"),
                "line": line,
                "context": expr.get("context", ""),
                "command": command,
                "source": "feishu_latex_compat",
            })

    return errors


def validate_feishu_latex(markdown: str) -> list[dict]:
    """One-shot Feishu LaTeX compatibility validation."""
    return find_unsupported_commands(markdown)
