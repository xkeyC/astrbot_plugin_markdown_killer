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


def remove_list_markers(text: str) -> str:
    """合并连续列表项到同一行，避免被分段发送时拆分为多条消息。

    - 无序列表 [-*+]: 项与项之间用 ``"; "`` 连接，如 ``a; b; c``。
    - 有序列表 [N. / N)]: 标记替换为 ``N)`` (无空格)，项之间用空格连接，
      如 ``1)First 2)Second 3)Third``。
    - 空行会中断列表 run，生成多个合并行。
    - 缩进的连续行作为上一项内容的延续 (用空格连接)。
      - 若延续行本身也是列表标记行 (且缩进大于父项)，则只保留标记后面的内容
        (N7 fix: 避免内层 ``-`` 残留为字面字符)。例如 ``- main\\n  - sub`` →
        ``main sub``。
      - 否则按原样追加 (例如 ``- main\\n  more details`` → ``main more details``)。
    - 函数幂等：再次输入输出文本不会进一步改变。
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

        if is_ordered:
            joined = " ".join(f"{num}){content}" for num, content in items)
        else:
            joined = "; ".join(content for _, content in items)
        output_lines.append(joined)

    return "\n".join(output_lines)


__all__ = ["remove_list_markers"]
