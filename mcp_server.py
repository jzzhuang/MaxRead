"""
MaxRead MCP Server — expose paper reading notes as MCP resources and tools.

Usage:
    python mcp_server.py                    # default port 8765
    MCP_PORT=9000 python mcp_server.py      # custom port

Client connects to http://127.0.0.1:8765/mcp
"""

import os
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DATA_DIR = Path(
    os.environ.get("MAXREAD_DATA_DIR", Path(__file__).resolve().parent / "reader" / "data")
)

mcp = FastMCP(
    "MaxRead",
    host="127.0.0.1",
    port=int(os.environ.get("MCP_PORT", "8765")),
)

_SAFE_PAPER_ID_RE = re.compile(r"^[A-Za-z0-9._/-]{1,64}$")


def _paper_dir(arxiv_id: str) -> Path | None:
    if (
        not _SAFE_PAPER_ID_RE.fullmatch(arxiv_id)
        or arxiv_id.startswith("/")
        or ".." in arxiv_id
    ):
        return None
    return DATA_DIR / arxiv_id.replace("/", "_")


def _parse_titles(summary_path: Path) -> tuple[str, str]:
    """Extract (title_zh, title_en) from the first 3 lines of summarize.md."""
    with open(summary_path, encoding="utf-8") as f:
        lines = [f.readline() for _ in range(3)]
    title_zh = lines[0].lstrip("# ").strip() if lines[0] else ""
    title_en = lines[2].strip().strip("*") if len(lines) > 2 else ""
    return title_zh, title_en


def _all_papers() -> list[dict]:
    """Return [{arxiv_id, title_zh, title_en}] for every paper with a summary."""
    results = []
    for md in sorted(DATA_DIR.glob("*/summarize.md")):
        arxiv_id = md.parent.name
        title_zh, title_en = _parse_titles(md)
        results.append({"arxiv_id": arxiv_id, "title_zh": title_zh, "title_en": title_en})
    return results


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_papers(query: str = "") -> list[dict]:
    """列出所有有阅读笔记的论文。可选 query 参数按标题关键词过滤。"""
    papers = _all_papers()
    if query:
        q = query.lower()
        papers = [
            p for p in papers
            if q in p["title_zh"].lower() or q in p["title_en"].lower() or q in p["arxiv_id"]
        ]
    return papers


@mcp.tool()
def read_note(arxiv_id: str) -> str:
    """读取指定论文的阅读笔记（summarize.md 全文）。"""
    paper_dir = _paper_dir(arxiv_id)
    if paper_dir is None:
        return "无效的 arXiv ID"
    md = paper_dir / "summarize.md"
    if not md.is_file():
        return f"未找到论文 {arxiv_id} 的笔记"
    return md.read_text(encoding="utf-8")


@mcp.tool()
def search_notes(keyword: str, max_results: int = 10) -> list[dict]:
    """在所有笔记中搜索关键词，返回匹配的论文及上下文片段。"""
    kw = keyword.lower()
    hits: list[dict] = []
    for md in sorted(DATA_DIR.glob("*/summarize.md")):
        text = md.read_text(encoding="utf-8")
        if kw not in text.lower():
            continue
        title_zh, title_en = _parse_titles(md)
        lines = text.splitlines()
        snippets: list[str] = []
        for i, line in enumerate(lines):
            if kw in line.lower():
                start = max(0, i - 2)
                end = min(len(lines), i + 3)
                snippets.append("\n".join(lines[start:end]))
                if len(snippets) >= 3:
                    break
        hits.append({
            "arxiv_id": md.parent.name,
            "title_zh": title_zh,
            "title_en": title_en,
            "snippets": snippets,
        })
        if len(hits) >= max_results:
            break
    return hits


@mcp.tool()
def summarize_paper(arxiv_id: str) -> str:
    """触发新论文总结。输入 arXiv ID（如 2401.12345），下载并生成阅读笔记。

    注意：这是一个长时间操作，通常需要几分钟完成。
    """
    from reader.pipeline import run_summarize

    paper_dir = _paper_dir(arxiv_id)
    if paper_dir is None:
        return "无效的 arXiv ID"
    existing = paper_dir / "summarize.md"
    if existing.is_file():
        return f"论文 {arxiv_id} 已有笔记，如需重新生成请先删除旧笔记"

    summary = run_summarize(arxiv_id, mode="api")
    return summary


# ---------------------------------------------------------------------------
# Resource template
# ---------------------------------------------------------------------------


@mcp.resource("paper://{arxiv_id}/note.md", mime_type="text/markdown")
def get_paper_note(arxiv_id: str) -> str:
    """获取论文阅读笔记"""
    paper_dir = _paper_dir(arxiv_id)
    if paper_dir is None:
        return "无效的 arXiv ID"
    md = paper_dir / "summarize.md"
    if not md.is_file():
        return f"未找到论文 {arxiv_id} 的笔记"
    return md.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
