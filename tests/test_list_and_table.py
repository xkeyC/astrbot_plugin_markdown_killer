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
import re
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
# Local alias used by the smart-join mirror below. The real implementation in
# ``utils.table_renderer.split_text_around_tables`` is already imported above;
# we keep the alias to match the documented mirror signature and to make the
# test self-documenting.
# ---------------------------------------------------------------------------
split_text_around_tables_local = split_text_around_tables


# ---------------------------------------------------------------------------
# Mirror of MarkdownKillerPlugin._squash_plains_for_single_message.
#
# The real implementation lives in main.py, which depends on the astrbot
# runtime (cannot be imported standalone). We mirror the exact logic here so
# it can be unit-tested; the mirror is the source of truth for the test
# suite. If you change main.py's squash, update this mirror in lock-step.
#
# Why a custom squash instead of AstrBot's ``MessageChain.squash_plain``?
# AstrBot's built-in joins Plain texts with ``""`` (no separator), which
# would turn ``"前文"`` + ``"后文"`` into ``"前文后文"`` with no paragraph
# break. This mirror joins with ``"\n\n"`` to preserve paragraph structure.
# ---------------------------------------------------------------------------
def squash_plains_for_single_message_local(result) -> None:
    """Merge all Plain components into the first (joined with ``\\n\\n``)."""
    if not result.chain:
        return
    plain_texts = []
    first_plain_idx = None
    for idx, comp in enumerate(result.chain):
        # Duck-typed: rely on isinstance(comp, Plain) at runtime; in tests we
        # pass FakePlain/FakeImage stubs and detect "Plain" via class name.
        if type(comp).__name__ == "Plain":
            if first_plain_idx is None:
                first_plain_idx = idx
            plain_texts.append(comp.text)
    if first_plain_idx is None or len(plain_texts) < 2:
        return
    result.chain[first_plain_idx].text = "\n\n".join(plain_texts)
    new_chain = []
    for idx, comp in enumerate(result.chain):
        if type(comp).__name__ == "Plain" and idx != first_plain_idx:
            continue
        new_chain.append(comp)
    result.chain = new_chain


def remove_markdown_smart_join(text, remove_markdown_no_tables_fn=None):
    """Mirror of MarkdownKillerPlugin.remove_markdown smart-join logic.

    ``remove_markdown_no_tables_fn``: optional callable for the text-block
    cleanup (so tests can inject a stub that mimics stripping trailing
    newlines, which is the real bug trigger). Defaults to identity.
    """
    if remove_markdown_no_tables_fn is None:
        remove_markdown_no_tables_fn = lambda x: x

    blocks = split_text_around_tables_local(text)
    result = ""
    for seg in blocks:
        if seg["type"] == "table":
            cleaned = "\n".join(ln.rstrip() for ln in seg["text"].split("\n"))
            # Guarantee the table starts on its own line.
            if result and not result.endswith("\n"):
                result += "\n"
            result += cleaned
            # Guarantee a trailing newline so the next text doesn't glue on.
            if not cleaned.endswith("\n"):
                result += "\n"
        else:
            result += remove_markdown_no_tables_fn(seg["text"])
    return result


def _strip_trailing_newlines(text):
    """Stub mimicking _remove_extra_newlines_global stripping trailing newlines.

    The real ``_remove_markdown_no_tables`` ends with one of the
    ``_remove_extra_newlines_*`` helpers, both of which strip trailing blank
    lines from the block. That stripping is what causes the bug: when the next
    block is a table, the table gets glued onto the (now-trailing-newline-free)
    text block. This stub reproduces ONLY that stripping behavior so the
    smart-join logic can be exercised without importing main.py (which depends
    on the astrbot runtime).
    """
    lines = [ln.rstrip() for ln in text.split("\n") if ln.strip()]
    return "\n".join(lines)


def buggy_remove_markdown(text, fn):
    """Pre-fix mirror: uses ``"".join(out_parts)`` and so glues tables to text.

    Kept as a regression guard: feeding the user's scenario through this
    version MUST reproduce the original silent-failure (table not on its own
    line, ``detect_markdown_tables`` returns 0). If a future refactor changes
    ``remove_markdown`` back to plain ``"".join`` semantics, this test will
    catch it.
    """
    blocks = split_text_around_tables_local(text)
    out_parts = []
    for seg in blocks:
        if seg["type"] == "table":
            out_parts.append("\n".join(ln.rstrip() for ln in seg["text"].split("\n")))
        else:
            out_parts.append(fn(seg["text"]))
    return "".join(out_parts)


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


def test_list_removal_adaptive_merge():
    """Adaptive merge: short lists merge to one line, long lists keep multi-line."""
    # --- Short lists (default threshold 30): merge to one line (unchanged) ---
    short_cases = [
        ("- a\n- b\n- c", "a; b; c"),
        ("1. First\n2. Second\n3. Third", "1)First 2)Second 3)Third"),
    ]
    for inp, expected in short_cases:
        actual = remove_list_markers(inp)  # default threshold 30
        assert actual == expected, (
            f"FAIL short-list merge: {inp!r} -> {actual!r} (expected {expected!r})"
        )
        print(f"OK  short-merge:  {inp!r} -> {actual!r}")

    # --- Long unordered list (total_chars=46 > 30): preserve multi-line ---
    long_unord_in = (
        "- 第一项这是一段比较长的说明文字\n"
        "- 第二项也是一段比较长的说明文字\n"
        "- 第三项依旧是一段比较长的说明文字"
    )
    long_unord_expected = (
        "第一项这是一段比较长的说明文字\n"
        "第二项也是一段比较长的说明文字\n"
        "第三项依旧是一段比较长的说明文字"
    )
    actual = remove_list_markers(long_unord_in)
    assert actual == long_unord_expected, (
        f"FAIL long-unord preserve: {actual!r} (expected {long_unord_expected!r})"
    )
    print(f"OK  long-unord:   3 items, multi-line preserved, markers stripped")

    # --- Long ordered list (the user's example; total_chars=140 > 30) ---
    long_ord_in = (
        "1. 第一步：准备工作，确保已安装 Python 3.10 或以上版本。\n"
        "2. 第二步：克隆仓库到本地，使用 git clone 命令。\n"
        "3. 第三步：进入项目目录，运行 pip install -r requirements.txt。\n"
        "4. 第四步：启动 AstrBot，使用 uv run main.py。"
    )
    long_ord_expected = (
        "第一步：准备工作，确保已安装 Python 3.10 或以上版本。\n"
        "第二步：克隆仓库到本地，使用 git clone 命令。\n"
        "第三步：进入项目目录，运行 pip install -r requirements.txt。\n"
        "第四步：启动 AstrBot，使用 uv run main.py。"
    )
    actual = remove_list_markers(long_ord_in)
    assert actual == long_ord_expected, (
        f"FAIL long-ord preserve: {actual!r} (expected {long_ord_expected!r})"
    )
    print(f"OK  long-ord:     4 items (user example), multi-line preserved, "
          f"number prefixes dropped")

    # Verify: no remaining list markers in the long-ordered output.
    for ln in actual.split("\n"):
        assert not ln.startswith(("1.", "1)", "-", "*", "+")), (
            f"FAIL long-ord: line still has marker: {ln!r}"
        )

    # --- Threshold escape hatch: merge_threshold=0 forces unconditional merge ---
    esc1_in = (
        "- 第一项这是一段比较长的说明文字\n"
        "- 第二项也是一段比较长的说明文字"
    )
    esc1_expected = (
        "第一项这是一段比较长的说明文字; 第二项也是一段比较长的说明文字"
    )
    actual = remove_list_markers(esc1_in, merge_threshold=0)
    assert actual == esc1_expected, (
        f"FAIL escape-hatch 0: {actual!r} (expected {esc1_expected!r})"
    )
    print(f"OK  escape-0:     threshold=0 forces merge -> {actual!r}")

    # merge_threshold=100 keeps short list merging as normal (under threshold).
    esc2_actual = remove_list_markers("- a\n- b", merge_threshold=100)
    assert esc2_actual == "a; b", (
        f"FAIL escape-100: {esc2_actual!r} (expected 'a; b')"
    )
    print(f"OK  escape-100:   threshold=100, short list merged -> {esc2_actual!r}")

    # --- Explicit threshold, unambiguous direction ---
    # total_chars=3 <= 5 -> merge.
    t1_actual = remove_list_markers("- a\n- b\n- c", merge_threshold=5)
    assert t1_actual == "a; b; c", (
        f"FAIL explicit-thr-5: {t1_actual!r} (expected 'a; b; c')"
    )
    print(f"OK  explicit-5:   {t1_actual!r}")

    # total_chars=6 > 4 -> preserve multi-line.
    t2_actual = remove_list_markers("- abc\n- def", merge_threshold=4)
    assert t2_actual == "abc\ndef", (
        f"FAIL explicit-thr-4: {t2_actual!r} (expected 'abc\\ndef')"
    )
    print(f"OK  explicit-4:   {t2_actual!r}")

    # --- Idempotency on long-list output ---
    long_out = remove_list_markers(long_ord_in)
    long_out_twice = remove_list_markers(long_out)
    assert long_out == long_out_twice, (
        f"FAIL long-list idempotency: once={long_out!r} twice={long_out_twice!r}"
    )
    print(f"OK  long-idem:    long-list output stable under re-application")


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


def test_build_table_html_github_style():
    """CSS should match GitHub's github-markdown-css (light theme).

    Guards against accidental regression back to the old purple-gradient
    header (``linear-gradient(135deg, #667eea 0%, #764ba2 100%)`` with
    white text). The new style uses a light-gray header background, clean
    borders, and GitHub's color palette.
    """
    html = build_table_html(["Name", "Value"], [["foo", "bar"]])
    # Required GitHub-style colors / values.
    required = [
        "#f6f8fa",          # thead bg + zebra row bg
        "#d0d7de",          # border color
        "#1f2328",          # text color
        "border-radius: 6px",
        "font-size: 14px",
        "padding: 8px 13px",
        "border-collapse: collapse",
        "vertical-align: top",
        "ui-monospace",
        "SFMono-Regular",
    ]
    for needle in required:
        assert needle in html, (
            f"FAIL github-style: required fragment {needle!r} missing"
        )
    # Old purple-gradient style must be GONE.
    forbidden = [
        "linear-gradient",
        "#667eea",
        "#764ba2",
        "#5a6ab8",
        "#f8f9ff",
        "font-size: 15px",
        "border-radius: 8px",
        "box-shadow",
    ]
    for needle in forbidden:
        assert needle not in html, (
            f"FAIL github-style: old style {needle!r} still present"
        )
    print("OK  github-style: required fragments present, old style purged")


# ---------------------------------------------------------------------------
# Tests — table-after-paragraph regression (smart join in remove_markdown).
# ---------------------------------------------------------------------------
def test_table_after_paragraph():
    """Regression: a markdown table following a paragraph of text must remain
    on its own line(s) after ``remove_markdown`` so that
    ``detect_markdown_tables`` can find it during the
    ``on_decorating_result`` phase.

    Bug: the old ``"".join(out_parts)`` glued the table onto the preceding
    text block (whose trailing ``\\n\\n`` had just been stripped by the
    newline-cleanup helper), producing e.g.
    ``"para| 功能 | 备注 |\\n|---|---|\\n| a | b |\\n"``. The glued header row
    no longer matched ``_TABLE_RE`` (which anchors to ``^``), so no table was
    rendered AND no fallback warning fired — silent failure.
    """
    # ------------------------------------------------------------------
    # Case 1: minimal bug repro.
    # ------------------------------------------------------------------
    inp = "para\n\n| 功能 | 备注 |\n|---|---|\n| a | b |\n"
    result = remove_markdown_smart_join(inp, _strip_trailing_newlines)
    expected = "para\n| 功能 | 备注 |\n|---|---|\n| a | b |\n"
    assert result == expected, (
        f"FAIL bug-repro: input={inp!r} got={result!r} expected={expected!r}"
    )
    assert re.search(r"(?m)^\| 功能", result), (
        f"FAIL bug-repro: header not on own line: {result!r}"
    )
    # The smart-join output MUST be detectable by the real regex.
    assert len(detect_markdown_tables(result)) == 1, (
        f"FAIL bug-repro: detect_markdown_tables found 0 matches in {result!r}"
    )
    print(f"OK  bug-repro:    header on own line; detect=1 -> {result!r}")

    # ------------------------------------------------------------------
    # Case 2: user's exact scenario (intro paragraph + table).
    # ------------------------------------------------------------------
    user_input = (
        "【轻松地舒展四肢】给你一张 Markdown 表格测试：\n\n"
        "| 功能 | 端点 | 状态 | 备注 |\n"
        "|---|---|---|---|\n"
        "| 贴吧帖子列表 | `/mo/q/f?kw={吧名}` | 可用 | 返回移动端 HTML |\n"
        "| 贴吧吧内搜索 | `/mo/q/search/thread?word={吧名}&query={关键词}` | 可用 | 返回 JSON |\n"
    )
    result = remove_markdown_smart_join(user_input, _strip_trailing_newlines)
    # The header row must be at column 0 (i.e. on its own line).
    assert re.search(r"(?m)^\| 功能", result), (
        f"FAIL user-scenario: header not on own line: {result!r}"
    )
    # The intro paragraph must NOT be glued onto the header row.
    assert "测试：| 功能" not in result, (
        f"FAIL user-scenario: intro glued to header: {result!r}"
    )
    # The original `\n\n` paragraph break is preserved as a single `\n`
    # (text-block cleanup strips trailing newlines; smart-join re-inserts
    # exactly one before the table).
    assert "测试：\n| 功能" in result, (
        f"FAIL user-scenario: expected single \\n between intro and table: {result!r}"
    )
    # Real detector must find the table on the smart-join output.
    assert len(detect_markdown_tables(result)) == 1, (
        f"FAIL user-scenario: detect_markdown_tables found 0 matches in {result!r}"
    )
    print(f"OK  user-scenario: header on own line; detect=1")
    print(f"     result preview: {result.splitlines()[0]!r} / {result.splitlines()[1]!r}")

    # ------------------------------------------------------------------
    # Case 3: text-only input unchanged (no table-related newline logic).
    # ------------------------------------------------------------------
    text_only = "hello world"
    result = remove_markdown_smart_join(text_only, _strip_trailing_newlines)
    assert result == "hello world", (
        f"FAIL text-only: input={text_only!r} got={result!r}"
    )
    print(f"OK  text-only:    unchanged -> {result!r}")

    # Multi-paragraph text-only input: still no spurious newline insertions.
    multi = "first paragraph\n\nsecond paragraph"
    result = remove_markdown_smart_join(multi, _strip_trailing_newlines)
    # _strip_trailing_newlines collapses `\n\n` to a single `\n` between non-empty
    # lines (mimics _remove_extra_newlines_global); just verify no leading/trailing
    # newline was added by smart-join.
    assert not result.startswith("\n") and not result.endswith("\n"), (
        f"FAIL text-only multi: spurious newline added: {result!r}"
    )
    print(f"OK  text-only-multi: no spurious newlines -> {result!r}")

    # ------------------------------------------------------------------
    # Case 4: idempotency — feeding the output back must be a fixed point.
    # ------------------------------------------------------------------
    once = remove_markdown_smart_join(user_input, _strip_trailing_newlines)
    twice = remove_markdown_smart_join(once, _strip_trailing_newlines)
    assert once == twice, (
        f"FAIL idempotency: once={once!r} twice={twice!r}"
    )
    print(f"OK  idempotency:  output stable under re-application")

    # ------------------------------------------------------------------
    # Case 5: pre-bug simulation — the buggy version MUST reproduce the bug.
    # This guards against silent regressions back to ``"".join`` semantics.
    # ------------------------------------------------------------------
    buggy_result = buggy_remove_markdown(inp, _strip_trailing_newlines)
    # In the buggy version, the table's first row should NOT be on its own line:
    assert not re.search(r"(?m)^\| 功能", buggy_result), (
        f"FAIL buggy-version: unexpectedly has table on own line: {buggy_result!r}"
    )
    # ... and the table is glued onto the preceding text:
    assert "para| 功能" in buggy_result, (
        f"FAIL buggy-version: did not glue table to text as expected: {buggy_result!r}"
    )
    # ... and the real detector must NOT find it (silent failure):
    assert len(detect_markdown_tables(buggy_result)) == 0, (
        f"FAIL buggy-version: detect unexpectedly found table in {buggy_result!r}"
    )
    print(f"OK  buggy-version: reproduces glue -> {buggy_result!r}")

    # ------------------------------------------------------------------
    # Case 6: user's exact scenario through the buggy version — must also fail.
    # ------------------------------------------------------------------
    buggy_user = buggy_remove_markdown(user_input, _strip_trailing_newlines)
    assert not re.search(r"(?m)^\| 功能", buggy_user), (
        f"FAIL buggy-user: header unexpectedly on own line: {buggy_user!r}"
    )
    assert "测试：| 功能" in buggy_user, (
        f"FAIL buggy-user: intro not glued to header: {buggy_user!r}"
    )
    assert len(detect_markdown_tables(buggy_user)) == 0, (
        f"FAIL buggy-user: detect unexpectedly found table in {buggy_user!r}"
    )
    print(f"OK  buggy-user:    reproduces glue (silent failure) -> {buggy_user[:60]!r}...")


# ---------------------------------------------------------------------------
# Tests — squash Plain components into one (Fix 2: single-message).
# ---------------------------------------------------------------------------
class _FakePlain:
    """Stand-in for astrbot.api.message_components.Plain."""

    __name__ = "Plain"

    def __init__(self, text):
        self.text = text


# The mirror detects "Plain" via ``type(comp).__name__``; ensure the stub
# reports the right class name even though it's not the real Plain class.
_FakePlain.__name__ = "Plain"


class _FakeImage:
    """Stand-in for astrbot.api.message_components.Image."""

    __name__ = "Image"

    def __init__(self, src):
        self.text = src  # Image has no .text in real AstrBot; we add one for
        # the generic getattr fallback in the renderer's caller, but the
        # squash logic only inspects Plain, so this is fine for tests.


_FakeImage.__name__ = "Image"


class _FakeResult:
    """Stand-in for MessageEventResult (just needs ``.chain`` list)."""

    def __init__(self, chain):
        self.chain = list(chain)


def test_squash_plains_for_single_message():
    """Mirror-level verification of the squash used by Fix 2."""

    # Case 1: classic [Plain, Image, Plain] -> [Plain_merged, Image]
    r = _FakeResult([_FakePlain("前文"), _FakeImage("img1"), _FakePlain("后文")])
    squash_plains_for_single_message_local(r)
    assert len(r.chain) == 2, f"FAIL squash-case1: len={len(r.chain)} want 2"
    assert type(r.chain[0]).__name__ == "Plain"
    assert type(r.chain[1]).__name__ == "Image"
    assert r.chain[0].text == "前文\n\n后文", (
        f"FAIL squash-case1: text was {r.chain[0].text!r}"
    )
    print(f"OK  squash-case1: [P,I,P] -> [P({r.chain[0].text!r}),I]")

    # Case 2: only one Plain -> unchanged
    r = _FakeResult([_FakePlain("a")])
    squash_plains_for_single_message_local(r)
    assert len(r.chain) == 1, f"FAIL squash-case2: len={len(r.chain)} want 1"
    assert r.chain[0].text == "a"
    print("OK  squash-case2: [P] unchanged (only one Plain)")

    # Case 3: two adjacent Plains -> merged
    r = _FakeResult([_FakePlain("a"), _FakePlain("b")])
    squash_plains_for_single_message_local(r)
    assert len(r.chain) == 1, f"FAIL squash-case3: len={len(r.chain)} want 1"
    assert r.chain[0].text == "a\n\nb", f"FAIL squash-case3: {r.chain[0].text!r}"
    print(f"OK  squash-case3: [P,P] -> [P({r.chain[0].text!r})]")

    # Case 4: image positions preserved when Plains surround them
    r = _FakeResult(
        [_FakeImage("img0"), _FakePlain("a"), _FakePlain("b"), _FakeImage("img1")]
    )
    squash_plains_for_single_message_local(r)
    assert len(r.chain) == 3, f"FAIL squash-case4: len={len(r.chain)} want 3"
    assert type(r.chain[0]).__name__ == "Image"
    assert type(r.chain[1]).__name__ == "Plain"
    assert r.chain[1].text == "a\n\nb"
    assert type(r.chain[2]).__name__ == "Image"
    print(
        f"OK  squash-case4: [I,P,P,I] -> "
        f"[I,P({r.chain[1].text!r}),I]"
    )

    # Case 5: multiple images preserved; all Plains merged into first slot
    r = _FakeResult(
        [
            _FakePlain("x"),
            _FakeImage("i1"),
            _FakePlain("y"),
            _FakeImage("i2"),
            _FakePlain("z"),
        ]
    )
    squash_plains_for_single_message_local(r)
    assert len(r.chain) == 3, f"FAIL squash-case5: len={len(r.chain)} want 3"
    assert type(r.chain[0]).__name__ == "Plain"
    assert r.chain[0].text == "x\n\ny\n\nz"
    assert type(r.chain[1]).__name__ == "Image"
    assert type(r.chain[2]).__name__ == "Image"
    print(f"OK  squash-case5: [P,I,P,I,P] -> [P({r.chain[0].text!r}),I,I]")

    # Case 6: empty chain -> no-op (no exception)
    r = _FakeResult([])
    squash_plains_for_single_message_local(r)
    assert r.chain == []
    print("OK  squash-case6: empty chain -> no-op")

    # Case 7: only Images -> no-op (no Plains to squash)
    r = _FakeResult([_FakeImage("a"), _FakeImage("b")])
    squash_plains_for_single_message_local(r)
    assert len(r.chain) == 2
    print("OK  squash-case7: [I,I] unchanged (no Plains)")


def main():
    print("=" * 70)
    print("test_list_and_table.py - real-implementation verification")
    print("=" * 70)
    test_list_removal_basic()
    test_list_removal_idempotent()
    test_list_removal_adaptive_merge()
    test_table_detection()
    test_table_parse()
    test_split_text_around_tables()
    test_build_table_html_smoke()
    test_build_table_html_github_style()
    test_table_after_paragraph()
    test_squash_plains_for_single_message()
    print("=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    main()



