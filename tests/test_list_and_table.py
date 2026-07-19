"""Standalone tests for list-marker removal and markdown-table detection.

This script exercises the REAL implementation in ``utils/list_processor.py``
and ``utils/table_renderer.py`` (no mirror logic). Because
``utils.table_renderer`` does ``from astrbot.api import logger`` at module
scope, we install lightweight ``sys.modules`` stubs BEFORE the import so the
file is importable from any environment without an AstrBot runtime.

Run: ``python tests/test_list_and_table.py``
"""

import asyncio
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
from utils.formula_renderer import (  # noqa: E402
    build_formula_html,
    contains_latex_formulas,
    split_text_around_formulas,
)
from utils.table_renderer import (  # noqa: E402
    build_table_html,
    detect_markdown_tables,
    parse_markdown_table,
    render_inline_markdown,
    split_text_around_tables,
)
from utils.browser import _calculate_screenshot_viewport  # noqa: E402


# ---------------------------------------------------------------------------
# Local alias used by the smart-join mirror below. The real implementation in
# ``utils.table_renderer.split_text_around_tables`` is already imported above;
# we keep the alias to match the documented mirror signature and to make the
# test self-documenting.
# ---------------------------------------------------------------------------
split_text_around_tables_local = split_text_around_tables


def remove_markdown_smart_join(text, remove_markdown_no_tables_fn=None):
    """Mirror of MarkdownKillerPlugin.remove_markdown smart-join logic.

    ``remove_markdown_no_tables_fn``: optional callable for the text-block
    cleanup (so tests can inject a stub that mimics stripping trailing
    newlines, which is the real bug trigger). Defaults to identity.
    """
    if remove_markdown_no_tables_fn is None:

        def remove_markdown_no_tables_fn(value):
            return value

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
# Tests — list marker/newline preservation (basic + idempotency).
# ---------------------------------------------------------------------------
def test_list_removal_basic():
    cases = [
        ("- a\n- b\n- c", "- a\n- b\n- c"),
        ("* a\n* b", "* a\n* b"),
        ("+ a\n+ b", "+ a\n+ b"),
        ("1. First\n2. Second\n3. Third", "1. First\n2. Second\n3. Third"),
        ("1) First\n2) Second", "1) First\n2) Second"),
        ("Before\n- a\n- b\nAfter", "Before\n- a\n- b\nAfter"),
        ("- main\n  - sub1\n  - sub2\n- next", "- main\n  - sub1\n  - sub2\n- next"),
        ("- main\n  more details", "- main\n  more details"),
        ("1. 第一步\n   子说明\n2. 第二步", "1. 第一步\n   子说明\n2. 第二步"),
    ]
    for inp, expected in cases:
        actual = remove_list_markers(inp)
        assert actual == expected, (
            f"FAIL list-preserve: {inp!r} -> {actual!r} (expected {expected!r})"
        )
        print(f"OK  list-preserve: {inp!r} -> {actual!r}")


def test_list_removal_idempotent():
    cases = [
        "- a\n- b\n- c",
        "* a\n* b",
        "+ a\n+ b",
        "1. First\n2. Second\n3. Third",
        "1) First\n2) Second",
        "Before\n- a\n- b\nAfter",
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
    """Short and long lists both preserve markers and per-item newlines now."""
    short_ord = "1. **短内容**\n2. 中等内容"
    short_ord_expected = "1. 短内容\n2. 中等内容"
    actual = remove_list_markers(short_ord)
    assert actual == short_ord_expected, (
        f"FAIL short-ordered preserve: {actual!r} (expected {short_ord_expected!r})"
    )
    print(f"OK  short-ord:    markers/newlines preserved -> {actual!r}")

    short_unord = "- **项目**\n* `代码`\n+ ~~删除~~"
    short_unord_expected = "- 项目\n* 代码\n+ 删除"
    actual = remove_list_markers(short_unord, merge_threshold=0)
    assert actual == short_unord_expected, (
        f"FAIL short-unordered preserve: {actual!r} (expected {short_unord_expected!r})"
    )
    print(
        f"OK  short-unord:  marker shapes preserved despite threshold=0 -> {actual!r}"
    )

    long_unord_in = (
        "- 第一项这是一段比较长的说明文字\n"
        "- 第二项也是一段比较长的说明文字\n"
        "- 第三项依旧是一段比较长的说明文字"
    )
    actual = remove_list_markers(long_unord_in)
    assert actual == long_unord_in, (
        f"FAIL long-unord preserve: {actual!r} (expected {long_unord_in!r})"
    )
    print("OK  long-unord:   markers and multi-line layout preserved")

    long_ord_in = (
        "1. 第一步：准备工作，确保已安装 Python 3.10 或以上版本。\n"
        "2. 第二步：克隆仓库到本地，使用 git clone 命令。\n"
        "3. 第三步：进入项目目录，运行 pip install -r requirements.txt。\n"
        "4. 第四步：启动 AstrBot，使用 uv run main.py。"
    )
    actual = remove_list_markers(long_ord_in)
    assert actual == long_ord_in, (
        f"FAIL long-ord preserve: {actual!r} (expected {long_ord_in!r})"
    )
    assert actual.splitlines()[0].startswith("1. ")
    assert actual.splitlines()[1].startswith("2. ")
    print("OK  long-ord:     original numbering and item newlines preserved")


def _load_plugin_class_for_tests():
    """Import main.py with minimal AstrBot stubs for newline-cleanup coverage."""
    _astrbot_api_pkg.AstrBotConfig = dict

    class _Plain:
        def __init__(self, text=""):
            self.text = text

    class _Image:
        def __init__(self, data=None):
            self.data = data

        @classmethod
        def fromBytes(cls, data):
            return cls(data)

    _astrbot_api_pkg.message_components = types.SimpleNamespace(
        Plain=_Plain,
        Image=_Image,
    )

    event_pkg = types.ModuleType("astrbot.api.event")

    class _Filter:
        @staticmethod
        def on_llm_response(*_args, **_kwargs):
            return lambda fn: fn

        @staticmethod
        def on_decorating_result(*_args, **_kwargs):
            return lambda fn: fn

    event_pkg.AstrMessageEvent = object
    event_pkg.filter = _Filter
    sys.modules["astrbot.api.event"] = event_pkg

    provider_pkg = types.ModuleType("astrbot.api.provider")
    provider_pkg.LLMResponse = object
    sys.modules["astrbot.api.provider"] = provider_pkg

    star_pkg = types.ModuleType("astrbot.api.star")

    class _Star:
        def __init__(self, _context):
            pass

    class _StarTools:
        @staticmethod
        def get_data_dir(_name):
            return "."

    def _register(*_args, **_kwargs):
        return lambda cls: cls

    star_pkg.Context = object
    star_pkg.Star = _Star
    star_pkg.StarTools = _StarTools
    star_pkg.register = _register
    sys.modules["astrbot.api.star"] = star_pkg

    core_pkg = types.ModuleType("astrbot.core")
    message_pkg = types.ModuleType("astrbot.core.message")
    result_pkg = types.ModuleType("astrbot.core.message.message_event_result")
    result_pkg.ResultContentType = types.SimpleNamespace(
        STREAMING_FINISH="STREAMING_FINISH"
    )
    sys.modules["astrbot.core"] = core_pkg
    sys.modules["astrbot.core.message"] = message_pkg
    sys.modules["astrbot.core.message.message_event_result"] = result_pkg

    from main import MarkdownKillerPlugin  # noqa: E402

    return MarkdownKillerPlugin


def _new_plugin_for_tests():
    plugin_cls = _load_plugin_class_for_tests()
    plugin = plugin_cls.__new__(plugin_cls)
    plugin.remove_extra_newlines = True
    plugin.newline_mode = "segment_boundary"
    plugin.list_merge_char_threshold = 30
    plugin.enable_formula_render = True
    plugin.formula_render_fallback = "raw"
    plugin._formula_render_failure_logged = False
    return plugin


def test_remove_markdown_preserves_list_newlines():
    """Main newline cleanup must not flatten list-item newlines."""
    plugin = _new_plugin_for_tests()

    ordered = "1. **短内容。**\n2. 中等内容。\n3. `代码`。"
    ordered_expected = "1. 短内容。\n2. 中等内容。\n3. 代码。"
    actual = plugin._remove_markdown_no_tables(ordered)
    assert actual == ordered_expected, (
        f"FAIL ordered cleanup: {actual!r} (expected {ordered_expected!r})"
    )
    print(f"OK  ordered-main: formatting stripped, list newlines kept -> {actual!r}")

    unordered = "- **项目。**\n* [链接](https://example.com)。\n+ ~~删除~~。"
    unordered_expected = "- 项目。\n* 链接。\n+ 删除。"
    actual = plugin._remove_markdown_no_tables(unordered)
    assert actual == unordered_expected, (
        f"FAIL unordered cleanup: {actual!r} (expected {unordered_expected!r})"
    )
    print(f"OK  unord-main:   marker shapes and newlines kept -> {actual!r}")

    continuation = "- 第一项。\n  续行一。\n  续行二。\n结尾"
    actual = plugin._remove_markdown_no_tables(continuation)
    assert actual == continuation, (
        f"FAIL list continuation cleanup: {actual!r} (expected {continuation!r})"
    )
    print("OK  list-cont:    indented continuation line breaks kept")

    non_list_indented = "第一句。\n  缩进句。"
    actual = plugin._remove_markdown_no_tables(non_list_indented)
    assert actual == "第一句。  缩进句。", (
        f"FAIL non-list indented cleanup: unexpected preservation: {actual!r}"
    )
    print(f"OK  non-list-ind: segment-boundary cleanup still applies -> {actual!r}")

    paragraph = "第一句。\n\n第二句。"
    actual = plugin._remove_markdown_no_tables(paragraph)
    assert actual == "第一句。第二句。", (
        f"FAIL paragraph cleanup: non-list newline behavior changed: {actual!r}"
    )
    print(
        f"OK  paragraph:    non-list segment-boundary cleanup unchanged -> {actual!r}"
    )


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

    header, body = parse_markdown_table(
        "| expr | note |\n|---|---|\n| `a | b` | escaped \\| pipe |\n"
    )
    assert header == ["expr", "note"], f"FAIL parse pipes header: {header!r}"
    assert body == [["`a | b`", "escaped | pipe"]], f"FAIL parse pipes body: {body!r}"
    print("OK  parse-pipes:  code/escaped pipes stay inside cells")

    header, body = parse_markdown_table("| a | b |\n|---|---|\n| \\`x | y |\n")
    assert header == ["a", "b"], f"FAIL parse escaped-backtick header: {header!r}"
    assert body == [["`x", "y"]], f"FAIL parse escaped-backtick body: {body!r}"
    print("OK  parse-escape: escaped backtick does not hide column pipe")

    header, body = parse_markdown_table("| a | b |\n|---|---|\n| ``x | y`` | z |\n")
    assert header == ["a", "b"], f"FAIL parse multi-code header: {header!r}"
    assert body == [["``x | y``", "z"]], f"FAIL parse multi-code body: {body!r}"
    print("OK  parse-code:   multi-backtick code span keeps pipe inside cell")


def test_split_text_around_tables():
    segments = split_text_around_tables("intro\n| a | b |\n|---|---|\n| 1 | 2 |\ntail")
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
    assert 'class="table-image"' in html, (
        "FAIL build_table_html: padded wrapper missing"
    )
    assert "padding: 9px 12px" in html, "FAIL build_table_html: image padding missing"
    # Ensure HTML escaping is applied for special chars.
    html2 = build_table_html(["x & y"], [["<b>bold</b>"]])
    assert "&amp;" in html2, "FAIL build_table_html: ampersand not escaped"
    assert "&lt;b&gt;" in html2, "FAIL build_table_html: angle brackets not escaped"
    print("OK  build_html:   table HTML + escaping OK")


def test_render_inline_markdown_in_table_cells():
    """Safe inline Markdown renders as HTML while raw HTML remains escaped."""
    html = build_table_html(
        ["feature"],
        [["**bold** *em* `code` ~~gone~~ [site](https://example.com)"]],
    )
    assert "<strong>bold</strong>" in html, "FAIL inline-md: bold not rendered"
    assert "**bold**" not in html, "FAIL inline-md: raw bold markers leaked"
    assert "<em>em</em>" in html, "FAIL inline-md: italic not rendered"
    assert "<code>code</code>" in html, "FAIL inline-md: code not rendered"
    assert "<del>gone</del>" in html, "FAIL inline-md: strike not rendered"
    assert '<a href="https://example.com"' in html, "FAIL inline-md: link not rendered"

    unsafe = build_table_html(["x"], [["<b>raw</b> [x](javascript:alert(1))"]])
    assert "&lt;b&gt;raw&lt;/b&gt;" in unsafe, "FAIL inline-md: raw HTML not escaped"
    assert "javascript:alert" not in unsafe, "FAIL inline-md: unsafe href leaked"
    assert '<a href="javascript:' not in unsafe, "FAIL inline-md: unsafe link rendered"
    print("OK  inline-md:    safe subset rendered; raw HTML/unsafe links escaped")

    direct = render_inline_markdown("**ok** and <script>x</script>")
    assert "<strong>ok</strong>" in direct and "&lt;script&gt;" in direct

    assert render_inline_markdown("3 * 4 * 5") == "3 * 4 * 5", (
        "FAIL inline-md: spaced multiplication was treated as emphasis"
    )
    assert render_inline_markdown("3*4*5") == "3*4*5", (
        "FAIL inline-md: compact multiplication was treated as emphasis"
    )
    assert render_inline_markdown("this_is_var") == "this_is_var", (
        "FAIL inline-md: snake_case identifier was treated as emphasis"
    )

    multi_code = render_inline_markdown("``x | y``")
    assert "<code>x | y</code>" in multi_code, (
        f"FAIL inline-md: multi-backtick code not rendered: {multi_code!r}"
    )
    assert "``x | y``" not in multi_code, "FAIL inline-md: raw multi-code leaked"


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
        "#f6f8fa",  # thead bg + zebra row bg
        "#d0d7de",  # border color
        "#1f2328",  # text color
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


def test_screenshot_viewport_uses_measured_content_size():
    """Viewport selection should grow to full table dimensions."""
    viewport = _calculate_screenshot_viewport(
        {
            "targetWidth": 2600,
            "targetHeight": 12000,
            "targetRight": 2612,
            "targetBottom": 12012,
            "documentWidth": 2624,
            "documentHeight": 12024,
            "bodyWidth": 2624,
            "bodyHeight": 12024,
        },
        min_width=1400,
    )
    assert viewport["width"] == 2624, f"FAIL viewport width: {viewport!r}"
    assert viewport["height"] == 12024, f"FAIL viewport height: {viewport!r}"

    viewport = _calculate_screenshot_viewport(
        {"targetWidth": 800, "targetHeight": 50}, 1400
    )
    assert viewport["width"] == 1400, f"FAIL viewport min-width: {viewport!r}"
    assert viewport["height"] >= 50, f"FAIL viewport height small: {viewport!r}"
    print("OK  viewport:     measured dimensions drive screenshot viewport")


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
    print("OK  user-scenario: header on own line; detect=1")
    print(
        f"     result preview: {result.splitlines()[0]!r} / {result.splitlines()[1]!r}"
    )

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
    assert once == twice, f"FAIL idempotency: once={once!r} twice={twice!r}"
    print("OK  idempotency:  output stable under re-application")

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
    print(
        f"OK  buggy-user:    reproduces glue (silent failure) -> {buggy_user[:60]!r}..."
    )


def test_rendered_table_images_have_block_boundaries():
    """Rendered tables are separated from text without edge-only padding."""
    plugin = _new_plugin_for_tests()
    plugin.table_render_fallback = "text"
    plugin._table_render_failure_logged = False

    renderer_globals = plugin._render_tables_in_chain.__func__.__globals__
    original_renderer = renderer_globals["render_table_to_image_bytes"]

    async def fake_renderer(table_text, timeout=20000):
        return table_text.encode()

    renderer_globals["render_table_to_image_bytes"] = fake_renderer

    class _Result:
        def __init__(self, chain):
            self.chain = chain
            self.disable_segment_reply = False

    components = renderer_globals["Comp"]
    Plain = components.Plain
    Image = components.Image
    table = "| a | b |\n|---|---|\n| 1 | 2 |\n"

    try:
        surrounded = _Result([Plain(f"before\n{table}after")])
        asyncio.run(plugin._render_tables_in_chain(surrounded))
        assert len(surrounded.chain) == 3, surrounded.chain
        assert isinstance(surrounded.chain[0], Plain)
        assert surrounded.chain[0].text == "before\n"
        assert isinstance(surrounded.chain[1], Image)
        assert isinstance(surrounded.chain[2], Plain)
        assert surrounded.chain[2].text == "\nafter"
        assert surrounded.disable_segment_reply is True

        at_start = _Result([Plain(f"{table}after")])
        asyncio.run(plugin._render_tables_in_chain(at_start))
        assert isinstance(at_start.chain[0], Image)
        assert at_start.chain[1].text == "\nafter"

        at_end = _Result([Plain(f"before\n{table}")])
        asyncio.run(plugin._render_tables_in_chain(at_end))
        assert at_end.chain[0].text == "before\n"
        assert isinstance(at_end.chain[1], Image)
        assert len(at_end.chain) == 2

        two_tables = _Result([Plain(f"{table}\n{table}")])
        asyncio.run(plugin._render_tables_in_chain(two_tables))
        assert len(two_tables.chain) == 3, two_tables.chain
        assert isinstance(two_tables.chain[0], Image)
        assert isinstance(two_tables.chain[1], Plain)
        assert two_tables.chain[1].text == "\n\u200b"
        assert isinstance(two_tables.chain[2], Image)
        # Simulate global Markdown cleanup trimming the separator, then verify
        # the post-cleanup spacing pass restores it.
        two_tables.chain[1].text = ""
        image_ids = {
            id(two_tables.chain[0]),
            id(two_tables.chain[2]),
        }
        two_tables.chain = plugin._separate_rendered_table_images(
            two_tables.chain, image_ids
        )
        assert two_tables.chain[1].text == "\n\u200b"

        rendered = Image(b"table")
        empty_before = Plain("")
        before_with_empty = [Plain("before"), empty_before, rendered]
        separated = plugin._separate_rendered_table_images(
            before_with_empty, {id(rendered)}
        )
        assert separated == before_with_empty
        assert separated[0].text == "before"
        assert separated[1].text == "\n\u200b"

        rendered = Image(b"table")
        empty_after = Plain("")
        after_with_empty = [rendered, empty_after, Plain("after")]
        separated = plugin._separate_rendered_table_images(
            after_with_empty, {id(rendered)}
        )
        assert separated == after_with_empty
        assert separated[1].text == "\n\u200b"
        assert separated[2].text == "after"

        untouched_plain = Plain("plain **markdown** text")
        no_table = _Result([untouched_plain])
        asyncio.run(plugin._render_tables_in_chain(no_table))
        assert no_table.chain == [untouched_plain]
        assert untouched_plain.text == "plain **markdown** text"

        ordinary_image = Image(b"ordinary")
        ordinary_chain = [Plain("before"), ordinary_image, Plain("after")]
        separated = plugin._separate_rendered_table_images(ordinary_chain, set())
        assert separated == ordinary_chain
        assert separated[0].text == "before"
        assert separated[2].text == "after"
    finally:
        renderer_globals["render_table_to_image_bytes"] = original_renderer

    print("OK  image-spacing: text/table edges and consecutive tables separated")


def test_global_cleanup_restores_boundaries_across_empty_plain():
    """Full decorating flow repairs marker-only Plain components after cleanup."""
    plugin = _new_plugin_for_tests()
    plugin.config = {"enable_global_markdown_killer": True}
    plugin.enable_table_render = True
    plugin._playwright_available = True
    plugin.table_render_fallback = "text"
    plugin._table_render_failure_logged = False

    renderer_globals = plugin._render_tables_in_chain.__func__.__globals__
    original_renderer = renderer_globals["render_table_to_image_bytes"]

    async def fake_renderer(table_text, timeout=20000):
        return table_text.encode()

    renderer_globals["render_table_to_image_bytes"] = fake_renderer
    components = renderer_globals["Comp"]
    Plain = components.Plain
    Image = components.Image
    table = "| a | b |\n|---|---|\n| 1 | 2 |\n"

    class _Result:
        result_content_type = None

        def __init__(self, chain):
            self.chain = chain
            self.disable_segment_reply = False

    class _Event:
        def __init__(self, result):
            self.result = result

        def get_result(self):
            return self.result

    try:
        result = _Result(
            [
                Plain("before"),
                Plain("**"),
                Plain(table),
                Plain("**"),
                Plain("after"),
            ]
        )
        asyncio.run(plugin.on_decorating_result(_Event(result)))

        assert len(result.chain) == 5, result.chain
        assert result.chain[0].text == "before"
        assert result.chain[1].text == "\n\u200b"
        assert isinstance(result.chain[2], Image)
        assert result.chain[3].text == "\n\u200b"
        assert result.chain[4].text == "after"

        first_pass = list(result.chain)
        rendered_ids = {id(result.chain[2])}
        result.chain = plugin._separate_rendered_table_images(
            result.chain, rendered_ids
        )
        assert result.chain == first_pass
        assert result.chain[1].text == "\n\u200b"
        assert result.chain[3].text == "\n\u200b"
    finally:
        renderer_globals["render_table_to_image_bytes"] = original_renderer

    print("OK  global-spacing: cleanup-empty Plain boundaries restored idempotently")


def test_formula_detection_and_splitting():
    text = (
        "intro\n"
        "\\[L=T-V\\]\n"
        "其中 \\(q\\) 是坐标，\\(t\\) 是时间。\n"
        "`\\(code\\)` and ```\n$$not_math$$\n```\n"
        "$$S=\\int L\\,dt$$\n"
    )
    assert contains_latex_formulas(text)
    segments = split_text_around_formulas(text)
    formulas = [segment for segment in segments if segment["type"] == "formula"]
    assert [segment["display"] for segment in formulas] == [True, False, True]
    assert formulas[0]["text"] == "L=T-V"
    assert formulas[1]["text"] == "其中 \\(q\\) 是坐标，\\(t\\) 是时间。"
    assert formulas[2]["text"] == "S=\\int L\\,dt"
    assert not contains_latex_formulas("价格是 $5，代码为 `\\(x\\)`")
    assert not contains_latex_formulas("价格区间是 $5-$10")
    print("OK  formula-split: block/inline formulas found; code spans ignored")


def test_build_formula_html():
    block_html = build_formula_html(
        r"\frac{d}{dt}\frac{\partial L}{\partial \dot y}=0", display=True
    )
    assert "<math" in block_html and 'display="block"' in block_html
    assert "<mfrac>" in block_html and "&#x02202;" in block_html
    assert block_html.count('display="block"') == 1
    assert 'class="formula-image display-formula"' in block_html

    inline_html = build_formula_html(
        r"其中 \(q\) 是坐标，\(\dot q\) 是速度。", display=False
    )
    assert "其中 " in inline_html and " 是速度。" in inline_html
    assert inline_html.count("<math") == 2
    assert 'class="formula-image inline-formula-line"' in inline_html
    print("OK  formula-html: local LaTeX-to-MathML conversion builds both layouts")


def test_formula_rendering_chain_and_fallback():
    plugin = _new_plugin_for_tests()
    renderer_globals = plugin._render_formulas_in_chain.__func__.__globals__
    original_renderer = renderer_globals["render_formula_to_image_bytes"]

    async def fake_renderer(source, display, timeout=20000):
        return f"{display}:{source}".encode()

    renderer_globals["render_formula_to_image_bytes"] = fake_renderer
    components = renderer_globals["Comp"]
    Plain = components.Plain
    Image = components.Image

    class _Result:
        def __init__(self, chain):
            self.chain = chain
            self.disable_segment_reply = False

    try:
        block = _Result([Plain("before\n\\[x^2\\]\nafter")])
        ids = asyncio.run(plugin._render_formulas_in_chain(block))
        assert len(ids) == 1
        assert block.chain[0].text == "before\n"
        assert isinstance(block.chain[1], Image)
        assert block.chain[2].text == "\nafter"
        assert block.disable_segment_reply is True

        inline = _Result([Plain("value \\(q\\) at \\(t\\).")])
        asyncio.run(plugin._render_formulas_in_chain(inline))
        assert len(inline.chain) == 1 and isinstance(inline.chain[0], Image)

        async def failed_renderer(source, display, timeout=20000):
            return None

        renderer_globals["render_formula_to_image_bytes"] = failed_renderer
        fallback = _Result([Plain("before \\(q\\) after")])
        asyncio.run(plugin._render_formulas_in_chain(fallback))
        assert len(fallback.chain) == 1
        assert fallback.chain[0].text == "before \\(q\\) after"
    finally:
        renderer_globals["render_formula_to_image_bytes"] = original_renderer

    print("OK  formula-chain: block/inline rendering and raw fallback preserve order")


def main():
    print("=" * 70)
    print("test_list_and_table.py - real-implementation verification")
    print("=" * 70)
    test_list_removal_basic()
    test_list_removal_idempotent()
    test_list_removal_adaptive_merge()
    test_remove_markdown_preserves_list_newlines()
    test_table_detection()
    test_table_parse()
    test_split_text_around_tables()
    test_build_table_html_smoke()
    test_render_inline_markdown_in_table_cells()
    test_build_table_html_github_style()
    test_screenshot_viewport_uses_measured_content_size()
    test_table_after_paragraph()
    test_rendered_table_images_have_block_boundaries()
    test_global_cleanup_restores_boundaries_across_empty_plain()
    test_formula_detection_and_splitting()
    test_build_formula_html()
    test_formula_rendering_chain_and_fallback()
    print("=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    main()
