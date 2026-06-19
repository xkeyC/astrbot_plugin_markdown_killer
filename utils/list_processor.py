"""Pure-stdlib list-marker removal logic — importable from main.py and tests.

This module MUST NOT depend on ``astrbot`` (or any other third-party package)
so it can be imported standalone from the test-suite without an AstrBot
runtime. ``main.py`` re-exports the public function as
``MarkdownKillerPlugin._remove_list_markers`` for backwards compatibility.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# Module-level pre-compiled regexes — avoid recompiling per call (N6).
_UNORD_LIST_RE = re.compile(r"^(\s*)[-*+]\s+(.+?)\s*$")
_ORD_LIST_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.+?)\s*$")


def remove_list_markers(text: str, merge_threshold: int = 30) -> str:
    """移除列表标记，并按 ``merge_threshold`` 自适应决定输出布局。

    - 无序列表 [-*+]: 项与项之间用 ``"; "`` 连接，如 ``a; b; c``。
    - 有序列表 [N. / N)]: 标记替换为 ``N)`` (无空格)，项之间用空格连接，
      如 ``1)First 2)Second 3)Third``。
    - 空行会中断列表 run，生成多个合并行。
    - 缩进的连续行作为上一项内容的延续 (用空格连接)。
      - 若延续行本身也是列表标记行 (且缩进大于父项)，则只保留标记后面的内容
        (N7 fix: 避免内层 ``-`` 残留为字面字符)。例如 ``- main\\n  - sub`` →
        ``main sub``。
      - 否则按原样追加 (例如 ``- main\\n  more details`` → ``main more details``)。

    Adaptive merge (adaptive-list-merge feature):
      ``merge_threshold`` 控制一段列表 (同一 run 内所有项) 是否合并到同一行。
      计算公式：``total_chars = sum(len(content) for _, content in items)``，
      按 Unicode 码点计数 (中文字符与英文字符均记 1)，不对内容做 trim/预处理。

      - ``merge_threshold <= 0``: 无条件合并到一行 (旧行为，用作 escape hatch)。
      - ``total_chars <= merge_threshold``: 合并到同一行 (短列表，避免分段发送
        时被拆分)。
      - ``total_chars > merge_threshold``: 保留每项独立一行，仅去除列表标记。
        无序: 每项内容独占一行 (无前缀)。
        有序: 每项内容独占一行 (丢弃数字前缀，行顺序天然保序)。

    函数幂等：再次输入输出文本不会进一步改变。长列表输出已无标记，第二趟会
    当作普通行原样返回 (已验证)。

    Args:
        text: 待处理文本。
        merge_threshold: 字符数阈值。默认 30，覆盖典型短列表。设为 0 强制合并
            (旧行为)。负数也视为无条件合并。
    """
    lines = text.split("\n")
    output_lines: List[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        m_unord = _UNORD_LIST_RE.match(line)
        m_ord = _ORD_LIST_RE.match(line)

        if m_ord:
            is_ordered = True
            indent = m_ord.group(1)
            items: List[Tuple[Optional[str], str]] = [
                (m_ord.group(2), m_ord.group(3))
            ]
            i += 1
        elif m_unord:
            is_ordered = False
            indent = m_unord.group(1)
            items = [(None, m_unord.group(2))]
            i += 1
        else:
            output_lines.append(line)
            i += 1
            continue

        while i < n:
            cur = lines[i]
            m_unord_cur = _UNORD_LIST_RE.match(cur)
            m_ord_cur = _ORD_LIST_RE.match(cur)

            if is_ordered and m_ord_cur and m_ord_cur.group(1) == indent:
                items.append((m_ord_cur.group(2), m_ord_cur.group(3)))
                i += 1
                continue
            if (not is_ordered) and m_unord_cur and m_unord_cur.group(1) == indent:
                items.append((None, m_unord_cur.group(2)))
                i += 1
                continue

            # Indented continuation: append to last item's content.
            indent_match = re.match(r"^(\s*)(\S.*)?$", cur)
            cur_indent = indent_match.group(1) if indent_match else ""
            if cur.strip() and len(cur_indent) > len(indent):
                last_num, last_content = items[-1]
                # N7: if the continuation line is itself a list marker at
                # greater indent, strip the marker prefix and use only the
                # content. Otherwise append the line verbatim (stripped).
                if m_unord_cur:
                    appended = m_unord_cur.group(2)
                elif m_ord_cur:
                    appended = m_ord_cur.group(3)
                else:
                    appended = cur.strip()
                items[-1] = (last_num, f"{last_content} {appended}")
                i += 1
                continue
            break

        total_chars = sum(len(content) for _, content in items)
        if merge_threshold <= 0 or total_chars <= merge_threshold:
            # Merge all items to a single line (preserves original behavior for
            # short lists; merge_threshold<=0 forces merge unconditionally as
            # an escape hatch).
            if is_ordered:
                joined = " ".join(f"{num}){content}" for num, content in items)
            else:
                joined = "; ".join(content for _, content in items)
            output_lines.append(joined)
        else:
            # Long list: preserve multi-line layout, strip ONLY the marker.
            # For ordered lists, drop the number prefix entirely (line order
            # already encodes sequence — matches the markdown-killer intent of
            # removing markdown formatting).
            for _num, content in items:
                output_lines.append(content)

    return "\n".join(output_lines)


__all__ = ["remove_list_markers"]
