"""Standalone tests for list-marker removal and markdown-table detection.

This script exercises the REAL implementation in ``utils/list_processor.py``
and ``utils/table_renderer.py`` (no mirror logic). Because
``utils.table_renderer`` does ``from astrbot.api import logger`` at module
scope, we install lightweight ``sys.modules`` stubs BEFORE the import so the
file is importable from any environment without an AstrBot runtime.

Run: ``python tests/test_list_and_table.py``
"""

import logging
import os
import sys
import types

# Repo root on sys.path so that ``import utils.*`` works no matter where the
# script is invoked from.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Stub astrbot.api.logger so utils.table_renderer can be imported standalone.
# (N8: option (a) — sys.modules stub. No real astrbot required.)
# ---------------------------------------------------------------------------
_astrbot_pkg = types.ModuleType("astrbot")
_astrbot_api_pkg = types.ModuleType("astrbot.api")
_logger_stub = types.ModuleType("astrbot.api.logger")

_test_logger = logging.getLogger("test_markdown_killer")
_test_logger.addHandler(logging.NullHandler())
_logger_stub.get_logger = lambda *a, **k: _test_logger
# Make `from astrbot.api import logger` return the stubbed logger object.
_astrbot_api_pkg.logger = _test_logger

sys.modules["astrbot"] = _astrbot_pkg
sys.modules["astrbot.api"] = _astrbot_api_pkg
sys.modules["astrbot.api.logger"] = _logger_stub

# Now safe to import the real modules.
from utils.list_processor import remove_list_markers  # noqa: E402
from utils.table_renderer import (  # noqa: E402
    build_table_html,
    detect_markdown_tables,
    parse_markdown_table,
    split_text_around_tables,
)


# ---------------------------------------------------------------------------
# Tests — list-marker removal (basic + idempotency).
# ---------------------------------------------------------------------------
def test_list_removal_basic():
    cases = [
        ("- a\n- b\n- c", "a; b; c"),
        ("* a\n* b", "a; b"),
        ("1. First\n2. Second\n3. Third", "1)First 2)Second 3)Third"),
        ("1) First\n2) Second", "1)First 2)Second"),
        ("Before\n- a\n- b\nAfter", "Before\na; b\nAfter"),
        # N7: nested sub-list markers stripped when consumed as continuation.
        ("- main\n  - sub1\n  - sub2\n- next", "main sub1 sub2; next"),
        # N7: non-marker continuation, unchanged behavior.
        ("- main\n  more details", "main more details"),
        # N7: ordered + indented non-marker continuation.
        ("1. 第一步\n   子说明\n2. 第二步", "1)第一步 子说明 2)第二步"),
    ]
    for inp, expected in cases:
        actual = remove_list_markers(inp)
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
        # N7 cases must also be stable under re-application.
        "- main\n  - sub1\n  - sub2\n- next",
        "- main\n  more details",
        "1. 第一步\n   子说明\n2. 第二步",
    ]
    for inp in cases:
        once = remove_list_markers(inp)
        twice = remove_list_markers(once)
        assert once == twice, (
            f"FAIL idempotency: input={inp!r} once={once!r} twice={twice!r}"
        )
        print(f"OK  idempotency:  {inp!r} -> {once!r} (stable)")


# ---------------------------------------------------------------------------
# Tests — table detection / parsing / splitting / HTML (real imports).
# ---------------------------------------------------------------------------
def test_table_detection():
    matches = detect_markdown_tables("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert len(matches) == 1, f"FAIL detect: expected 1 match, got {len(matches)}"
    start, end, txt = matches[0]
    assert txt == "| a | b |\n|---|---|\n| 1 | 2 |\n", (
        f"FAIL detect: unexpected table text {txt!r}"
    )
    print(f"OK  detect:       1 match, span=({start},{end})")

    # Chinese content + 2 body rows.
    matches2 = detect_markdown_tables(
        "| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 10   |\n| 橙子 | 20   |\n"
    )
    assert len(matches2) == 1, f"FAIL detect chinese: {len(matches2)} matches"
    print("OK  detect:       chinese table detected")

    # No table.
    matches3 = detect_markdown_tables("just text\nno table here")
    assert len(matches3) == 0, f"FAIL detect no-table: {len(matches3)} matches"
    print("OK  detect:       no-table returns 0 matches")


def test_table_parse():
    header, body = parse_markdown_table("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert header == ["a", "b"], f"FAIL parse header: {header!r}"
    assert body == [["1", "2"]], f"FAIL parse body: {body!r}"
    print(f"OK  parse:        header={header!r} body={body!r}")


def test_split_text_around_tables():
    segments = split_text_around_tables(
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


def test_build_table_html_smoke():
    """build_table_html should escape cells and emit a complete <table>."""
    html = build_table_html(["a", "b"], [["1", "2"]])
    assert "<table>" in html, "FAIL build_table_html: missing <table>"
    assert "<th>a</th>" in html, "FAIL build_table_html: header cell missing"
    assert "<td>1</td>" in html, "FAIL build_table_html: body cell missing"
    # Ensure HTML escaping is applied for special chars.
    html2 = build_table_html(["x & y"], [["<b>bold</b>"]])
    assert "&amp;" in html2, "FAIL build_table_html: ampersand not escaped"
    assert "&lt;b&gt;" in html2, "FAIL build_table_html: angle brackets not escaped"
    print("OK  build_html:   table HTML + escaping OK")


def main():
    print("=" * 70)
    print("test_list_and_table.py - real-implementation verification")
    print("=" * 70)
    test_list_removal_basic()
    test_list_removal_idempotent()
    test_table_detection()
    test_table_parse()
    test_split_text_around_tables()
    test_build_table_html_smoke()
    print("=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    main()
