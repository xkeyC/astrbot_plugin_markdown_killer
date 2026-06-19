"""Standalone tests for list-marker removal and markdown-table detection.

This script MUST NOT import astrbot. It mirrors the logic from main.py and
utils/table_renderer.py exactly so it can run anywhere with only the standard
library.

Run: ``python tests/test_list_and_table.py``
"""

import re


# ----------------------------------------------------------------------------
# Mirror of MarkdownKillerPlugin._remove_list_markers (from main.py).
# Keep this in sync with the implementation in main.py.
# ----------------------------------------------------------------------------
def remove_list_markers_local(text):
    lines = text.split("\n")
    output_lines = []
    i = 0
    n = len(lines)

    unord_re = re.compile(r"^(\s*)[-*+]\s+(.+?)\s*$")
    ord_re = re.compile(r"^(\s*)(\d+)[.)]\s+(.+?)\s*$")

    while i < n:
        line = lines[i]
        m_unord = unord_re.match(line)
        m_ord = ord_re.match(line)

        if m_ord:
            is_ordered = True
            indent = m_ord.group(1)
            items = [(m_ord.group(2), m_ord.group(3))]
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
            m_unord_cur = unord_re.match(cur)
            m_ord_cur = ord_re.match(cur)

            if is_ordered and m_ord_cur and m_ord_cur.group(1) == indent:
                items.append((m_ord_cur.group(2), m_ord_cur.group(3)))
                i += 1
                continue
            if (not is_ordered) and m_unord_cur and m_unord_cur.group(1) == indent:
                items.append((None, m_unord_cur.group(2)))
                i += 1
                continue

            indent_match = re.match(r"^(\s*)(\S.*)?$", cur)
            cur_indent = indent_match.group(1) if indent_match else ""
            if cur.strip() and len(cur_indent) > len(indent):
                last_num, last_content = items[-1]
                items[-1] = (last_num, f"{last_content} {cur.strip()}")
                i += 1
                continue
            break

        if is_ordered:
            joined = " ".join(f"{num}){content}" for num, content in items)
        else:
            joined = "; ".join(content for _, content in items)
        output_lines.append(joined)

    return "\n".join(output_lines)


# ----------------------------------------------------------------------------
# Mirror of utils/table_renderer.py detection / parsing / splitting.
# ----------------------------------------------------------------------------
_TABLE_RE = re.compile(
    r"^[ \t]*\|[^\n]+\|[ \t]*\n"
    r"[ \t]*\|[ \t]*:?[-:]+[- :|]*\|[ \t]*\n"
    r"(?:[ \t]*\|[^\n]+\|[ \t]*\n?)+$",
    re.MULTILINE,
)


def detect_markdown_tables_local(text):
    return [(m.start(), m.end(), m.group(0)) for m in _TABLE_RE.finditer(text)]


def parse_markdown_table_local(table_text):
    lines = [ln for ln in table_text.split("\n") if ln.strip()]
    if len(lines) < 3:
        return ([], [])

    def parse_row(line):
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        cells = re.split(r"\s*\|\s*", s)
        return [c.strip() for c in cells]

    header = parse_row(lines[0])
    body = [parse_row(ln) for ln in lines[2:]]
    return (header, body)


def split_text_around_tables_local(text):
    matches = detect_markdown_tables_local(text)
    if not matches:
        return [{"type": "text", "text": text}]

    segments = []
    last_end = 0
    for start, end, table_text in matches:
        if start > last_end:
            segments.append({"type": "text", "text": text[last_end:start]})
        segments.append({"type": "table", "text": table_text})
        last_end = end
    if last_end < len(text):
        segments.append({"type": "text", "text": text[last_end:]})
    return segments


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------
def test_list_removal_basic():
    cases = [
        ("- a\n- b\n- c", "a; b; c"),
        ("* a\n* b", "a; b"),
        ("1. First\n2. Second\n3. Third", "1)First 2)Second 3)Third"),
        ("1) First\n2) Second", "1)First 2)Second"),
        ("Before\n- a\n- b\nAfter", "Before\na; b\nAfter"),
    ]
    for inp, expected in cases:
        actual = remove_list_markers_local(inp)
        assert actual == expected, (
            f"FAIL list-removal: {inp!r} -> {actual!r} (expected {expected!r})"
        )
        print(f"OK  list-removal: {inp!r} -> {actual!r}")


def test_list_removal_idempotent():
    cases = [
        "- a\n- b\n- c",
        "* a\n* b",
        "1. First\n2. Second\n3. Third",
        "1) First\n2) Second",
        "Before\n- a\n- b\nAfter",
    ]
    for inp in cases:
        once = remove_list_markers_local(inp)
        twice = remove_list_markers_local(once)
        assert once == twice, (
            f"FAIL idempotency: input={inp!r} once={once!r} twice={twice!r}"
        )
        print(f"OK  idempotency:  {inp!r} -> {once!r} (stable)")


def test_table_detection():
    matches = detect_markdown_tables_local("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert len(matches) == 1, f"FAIL detect: expected 1 match, got {len(matches)}"
    start, end, txt = matches[0]
    assert txt == "| a | b |\n|---|---|\n| 1 | 2 |\n", (
        f"FAIL detect: unexpected table text {txt!r}"
    )
    print(f"OK  detect:       1 match, span=({start},{end})")

    # Chinese content + 2 body rows.
    matches2 = detect_markdown_tables_local(
        "| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 10   |\n| 橙子 | 20   |\n"
    )
    assert len(matches2) == 1, f"FAIL detect chinese: {len(matches2)} matches"
    print("OK  detect:       chinese table detected")

    # No table.
    matches3 = detect_markdown_tables_local("just text\nno table here")
    assert len(matches3) == 0, f"FAIL detect no-table: {len(matches3)} matches"
    print("OK  detect:       no-table returns 0 matches")


def test_table_parse():
    header, body = parse_markdown_table_local("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert header == ["a", "b"], f"FAIL parse header: {header!r}"
    assert body == [["1", "2"]], f"FAIL parse body: {body!r}"
    print(f"OK  parse:        header={header!r} body={body!r}")


def test_split_text_around_tables():
    segments = split_text_around_tables_local(
        "intro\n| a | b |\n|---|---|\n| 1 | 2 |\ntail"
    )
    assert len(segments) == 3, (
        f"FAIL split: expected 3 segments, got {len(segments)}: {segments!r}"
    )
    assert segments[0]["type"] == "text", segments[0]
    assert segments[1]["type"] == "table", segments[1]
    assert segments[2]["type"] == "text", segments[2]
    print(
        "OK  split:        3 segments (text, table, text) -> "
        f"{[s['type'] for s in segments]}"
    )


def main():
    print("=" * 70)
    print("test_list_and_table.py - standalone logic verification")
    print("=" * 70)
    test_list_removal_basic()
    test_list_removal_idempotent()
    test_table_detection()
    test_table_parse()
    test_split_text_around_tables()
    print("=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    main()
