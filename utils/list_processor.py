"""Pure-stdlib list-item cleanup logic — importable from main.py and tests.

This module MUST NOT depend on ``astrbot`` (or any other third-party package)
so it can be imported standalone from the test-suite without an AstrBot
runtime. ``main.py`` re-exports the public function as
``MarkdownKillerPlugin._remove_list_markers`` for backwards compatibility.
"""

from __future__ import annotations

import re

# Module-level pre-compiled regexes — avoid recompiling per call (N6).
_LIST_ITEM_RE = re.compile(r"^(\s*)((?:[-*+])|(?:\d+[.)]))(\s+)(.*?)(\s*)$")
_INDENTED_RE = re.compile(r"^(\s+)(.*?)(\s*)$")


def _strip_inline_markdown(text: str) -> str:
    """Remove inline Markdown formatting from list-item content only."""
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(
        r"(^|[^\w\*])\*(?!\s)(.*?)(?<!\s)\*(?=$|[^\w\*])",
        r"\1\2",
        text,
    )
    text = re.sub(
        r"(^|[^\w_])_(?!\s)(.*?)(?<!\s)_(?=$|[^\w_])",
        r"\1\2",
        text,
    )
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    return text


def remove_list_markers(text: str, merge_threshold: int = 30) -> str:
    """保留 Markdown 列表标记/编号与换行，仅清理列表内容中的行内格式。

    历史上此函数会移除列表标记，并按 ``merge_threshold`` 将短列表合并到
    一行。新版行为不再合并或删除列表标记：``1. **短内容**`` 会变为
    ``1. 短内容``，``- **项目**`` 会变为 ``- 项目``。有序列表的原始
    编号与分隔符 (``1.`` / ``1)``) 会保留，无序列表的原始标记形状
    (``-`` / ``*`` / ``+``) 也会保留。

    ``merge_threshold`` 参数仅为向后兼容保留，当前不再影响输出。

    函数幂等：再次输入输出文本不会进一步改变。

    Args:
        text: 待处理文本。
        merge_threshold: 已废弃，仅为兼容旧配置/调用签名保留。
    """
    del merge_threshold

    output_lines: list[str] = []
    active_list_indent: int | None = None

    for line in text.split("\n"):
        item_match = _LIST_ITEM_RE.match(line)
        if item_match:
            indent, marker, spacing, content, _trailing = item_match.groups()
            output_lines.append(
                f"{indent}{marker}{spacing}{_strip_inline_markdown(content).rstrip()}"
            )
            active_list_indent = len(indent)
            continue

        continuation_match = _INDENTED_RE.match(line)
        if (
            active_list_indent is not None
            and continuation_match
            and line.strip()
            and len(continuation_match.group(1)) > active_list_indent
        ):
            indent, content, _trailing = continuation_match.groups()
            output_lines.append(f"{indent}{_strip_inline_markdown(content).rstrip()}")
            continue

        if not line.strip():
            active_list_indent = None
        elif continuation_match is None or len(continuation_match.group(1)) <= (
            active_list_indent or -1
        ):
            active_list_indent = None
        output_lines.append(line)

    return "\n".join(output_lines)


__all__ = ["remove_list_markers"]
