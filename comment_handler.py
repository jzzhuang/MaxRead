"""Handle document comment events: load paper context, generate AI reply, post it."""
from __future__ import annotations

import logging
from pathlib import Path

import metadata
from feishu import doc_comments
from reader.agent import DEFAULT_MODEL, run_codex_for_stdout

logger = logging.getLogger(__name__)

COMMENT_REPLY_MODEL = DEFAULT_MODEL

_SYSTEM_PROMPT = """\
你是一个论文阅读助手。用户在你生成的论文阅读笔记文档中留下了评论，请基于论文原文和阅读笔记的内容回答用户的问题或回复其评论。

要求：
- 回复简洁、专业、有针对性
- 如果评论涉及论文的具体内容，请引用相关的原文或笔记段落
- 如果评论是一般性讨论或表达感想，礼貌地回复即可
- 如果你无法确定答案，诚实地说明
- 用中文回复
- 直接输出回复内容到标准输出，不要写文件
"""


def handle_comment(
    file_token: str,
    comment_id: str,
    comment_text: str,
    quote: str,
    *,
    reply_id: str | None = None,
) -> bool:
    """Generate and post a reply to a document comment."""
    model = COMMENT_REPLY_MODEL
    arxiv_id = metadata.lookup_paper_by_file_token(file_token)

    def save_log(
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        try:
            metadata.save_comment_reply_log(
                arxiv_id,
                status=status,
                reply_model=model,
                error_recorded=bool(error),
            )
        except Exception as e:
            logger.warning("Failed to save comment reply log for %s/%s: %s", file_token, comment_id, e)

    paper_dir = metadata.lookup_paper_dir_by_file_token(file_token)
    if not paper_dir:
        logger.warning("No paper found for file_token %s, skipping comment", file_token)
        save_log("paper_not_found")
        return False

    summary_file = paper_dir / "summarize.md"
    if not summary_file.is_file():
        logger.warning("No summarize.md in %s, skipping comment", paper_dir)
        save_log("summary_not_found")
        return False

    summary = summary_file.read_text(encoding="utf-8").strip()
    if len(summary) > 8000:
        summary = summary[:8000] + "\n\n…（阅读笔记已截断）"

    user_prompt_parts = []
    if quote:
        user_prompt_parts.append(f"【被评论的文档段落】\n{quote}")
    user_prompt_parts.append(f"【用户评论】\n{comment_text}")
    user_prompt_parts.append("请回复这条评论。")
    user_prompt = "\n\n".join(user_prompt_parts)

    full_prompt = f"{_SYSTEM_PROMPT}\n\n以下是阅读笔记内容（供参考）：\n\n{summary}\n\n---\n\n{user_prompt}"

    try:
        reply_text = _run_codex_for_reply(paper_dir, full_prompt, model=model)
    except Exception as e:
        logger.exception("OpenAI API failed for comment reply: %s", e)
        save_log("generation_failed", error=str(e))
        return False

    if not reply_text:
        logger.warning("Codex returned empty reply for comment %s", comment_id)
        save_log("empty_reply")
        return False

    post_result = doc_comments.post_reply_result(file_token, comment_id, reply_text)
    ok = bool(post_result.get("ok"))
    if ok:
        logger.info("Replied to comment %s on doc %s", comment_id, file_token)
        save_log("posted")
    else:
        logger.warning("Failed to post reply to comment %s on doc %s", comment_id, file_token)
        save_log("post_failed")
    return ok


def _run_codex_for_reply(paper_dir: Path, prompt: str, *, model: str = COMMENT_REPLY_MODEL) -> str:
    """Call the tool-free API with bounded paper context."""
    return run_codex_for_stdout(paper_dir, prompt, model=model, timeout=120)
