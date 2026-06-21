"""Markdown table detection, parsing, HTML/CSS generation, and image rendering."""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from astrbot.api import logger

# Detection regex: header row + separator row + one-or-more body rows.
# Anchored per-line via re.MULTILINE so that `^` matches start-of-line.
_TABLE_RE = re.compile(
    r"^[ \t]*\|[^\n]+\|[ \t]*\n"  # header row: at least one pipe, ends at EOL
    r"[ \t]*\|[ \t]*:?[-:]+[- :|]*\|[ \t]*\n"  # separator row: e.g. |---|:--:|
    r"(?:[ \t]*\|[^\n]+\|[ \t]*\n?)+$",  # body rows: 1 or more
    re.MULTILINE,
)


def detect_markdown_tables(text: str) -> list[tuple[int, int, str]]:
    """Find all markdown table blocks in ``text``.

    Returns a list of ``(start_index, end_index, table_text)`` tuples.
    """
    return [(m.start(), m.end(), m.group(0)) for m in _TABLE_RE.finditer(text)]


def _is_escaped(text: str, index: int) -> bool:
    """Return whether ``text[index]`` is escaped by an odd backslash run."""
    slash_count = 0
    i = index - 1
    while i >= 0 and text[i] == "\\":
        slash_count += 1
        i -= 1
    return slash_count % 2 == 1


def _count_backtick_run(text: str, index: int) -> int:
    """Count consecutive backticks starting at ``index``."""
    i = index
    while i < len(text) and text[i] == "`":
        i += 1
    return i - index


def _find_closing_backtick_run(text: str, start: int, run_length: int) -> int:
    """Find an unescaped, exact-length closing backtick run, or ``-1``."""
    i = start
    while i < len(text):
        if text[i] != "`":
            i += 1
            continue
        found_length = _count_backtick_run(text, i)
        if found_length == run_length and not _is_escaped(text, i):
            return i
        i += found_length
    return -1


def parse_markdown_table(table_text: str) -> tuple[list[str], list[list[str]]]:
    """Parse a markdown table block into ``(header_cells, body_rows)``.

    Returns ``([], [])`` if the table is too short or malformed. Line 1 is
    always treated as the separator row and skipped.
    """
    lines = [ln for ln in table_text.split("\n") if ln.strip()]
    if len(lines) < 3:
        return ([], [])

    def parse_row(line: str) -> list[str]:
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        cells: list[str] = []
        current: list[str] = []
        code_delimiter_len: int | None = None
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "\\" and code_delimiter_len is None and i + 1 < len(s):
                # Markdown tables use escaped pipes for literal pipe text inside
                # cells. Escaped backticks must also be consumed here so they do
                # not incorrectly open/close a code span and hide later column
                # delimiters.
                if s[i + 1] == "|":
                    current.append("|")
                elif s[i + 1] == "`":
                    current.append("`")
                else:
                    current.append(ch)
                    current.append(s[i + 1])
                i += 2
                continue
            if ch == "`":
                run_length = _count_backtick_run(s, i)
                run_end = i + run_length
                if code_delimiter_len is None:
                    if _find_closing_backtick_run(s, run_end, run_length) != -1:
                        code_delimiter_len = run_length
                elif run_length == code_delimiter_len and not _is_escaped(s, i):
                    code_delimiter_len = None
                current.append(s[i:run_end])
                i = run_end
                continue
            if ch == "|" and code_delimiter_len is None:
                cells.append("".join(current).strip())
                current = []
                i += 1
                continue
            current.append(ch)
            i += 1
        cells.append("".join(current).strip())
        return [c.strip() for c in cells]

    header = parse_row(lines[0])
    # Line 1 is the separator; skip it. Lines 2..N are body rows.
    body = [parse_row(ln) for ln in lines[2:]]
    return (header, body)


_PLACEHOLDER_RE = re.compile(r"\ue000MDK\d+\ue001")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(((?:[^()\s]|\([^()\s]*\))+?)\)")
_SAFE_LINK_SCHEMES = {"http", "https", "mailto"}


def _is_safe_link(url: str) -> bool:
    """Return whether ``url`` is safe to place in an HTML ``href``."""
    parsed = urlparse(url.strip())
    return parsed.scheme.lower() in _SAFE_LINK_SCHEMES and bool(parsed.netloc or parsed.path)


def _render_emphasis_only(text: str) -> str:
    """Render emphasis markers in already-tokenized inline text safely.

    This helper first HTML-escapes user content, then converts a small safe
    subset of inline Markdown. Placeholder tokens created by
    ``render_inline_markdown`` do not contain marker characters and therefore
    pass through unchanged until the final restore step.
    """
    rendered = html.escape(text, quote=False)
    rendered = re.sub(r"~~(?!\s)(.+?)(?<!\s)~~", r"<del>\1</del>", rendered)
    rendered = re.sub(
        r"(?<![\w*])\*\*(?!\s)(.+?)(?<!\s)\*\*(?![\w*])",
        r"<strong>\1</strong>",
        rendered,
    )
    rendered = re.sub(
        r"(?<![\w_])__(?!\s)(.+?)(?<!\s)__(?![\w_])",
        r"<strong>\1</strong>",
        rendered,
    )
    rendered = re.sub(
        r"(?<![\w*])\*(?![\s*])(.+?)(?<!\s)\*(?![\w*])",
        r"<em>\1</em>",
        rendered,
    )
    rendered = re.sub(
        r"(?<![\w_])_(?![\s_])(.+?)(?<!\s)_(?![\w_])",
        r"<em>\1</em>",
        rendered,
    )
    return rendered


def _replace_code_spans(text: str, reserve) -> str:
    """Replace single- or multi-backtick code spans with reserved HTML tokens."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "`" and not _is_escaped(text, i):
            run_length = _count_backtick_run(text, i)
            content_start = i + run_length
            close = _find_closing_backtick_run(text, content_start, run_length)
            if close != -1:
                code_text = text[content_start:close]
                out.append(reserve(f"<code>{html.escape(code_text, quote=False)}</code>"))
                i = close + run_length
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def render_inline_markdown(text: str) -> str:
    """Render a deliberately small, safe subset of inline Markdown.

    Supported syntax: ``**bold**``, ``*italic*``, ``~~strike~~``, inline
    ``code``, and safe ``[links](https://...)``. Raw HTML is always escaped
    before marker conversion, and links are emitted only for http/https/mailto
    URLs. This function is intentionally independent of table row splitting;
    callers should parse table cells first, then render cell contents.
    """
    placeholders: dict[str, str] = {}

    def reserve(rendered_html: str) -> str:
        token = f"\ue000MDK{len(placeholders)}\ue001"
        placeholders[token] = rendered_html
        return token

    tokenized = _replace_code_spans(text, reserve)

    def replace_link(match: re.Match[str]) -> str:
        label = _render_emphasis_only(match.group(1))
        url = match.group(2).strip()
        if not _is_safe_link(url):
            return reserve(label)
        safe_href = html.escape(url, quote=True)
        return reserve(f'<a href="{safe_href}" rel="noreferrer noopener">{label}</a>')

    tokenized = _LINK_RE.sub(replace_link, tokenized)
    rendered = _render_emphasis_only(tokenized)

    # Restore placeholders repeatedly so a link label that contained an inline
    # code placeholder is restored inside the link HTML as well.
    previous = None
    while previous != rendered and _PLACEHOLDER_RE.search(rendered):
        previous = rendered
        for token, replacement in placeholders.items():
            rendered = rendered.replace(token, replacement)
    return rendered


def build_table_html(header_cells: list[str], body_rows: list[list[str]]) -> str:
    """Build a complete standalone HTML document with the table styled as a card."""

    header_html = "".join(f"<th>{render_inline_markdown(c)}</th>" for c in header_cells)
    body_html = "".join(
        f"<tr>{''.join(f'<td>{render_inline_markdown(c)}</td>' for c in row)}</tr>"
        for row in body_rows
    )

    table_html = (
        f"<table><thead><tr>{header_html}</tr></thead>"
        f"<tbody>{body_html}</tbody></table>"
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif, 'PingFang SC', 'Microsoft YaHei', 'Hiragino Sans GB';
  background: transparent;
  padding: 12px;
  display: inline-block;
}}
table {{
  border-collapse: collapse;
  background: #ffffff;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  overflow: hidden;
  font-size: 14px;
  color: #1f2328;
}}
thead {{
  background-color: #f6f8fa;
  border-bottom: 1px solid #d0d7de;
}}
th {{
  padding: 8px 13px;
  font-weight: 600;
  text-align: left;
  border-right: 1px solid #d0d7de;
  white-space: nowrap;
}}
th:last-child {{
  border-right: none;
}}
td {{
  padding: 8px 13px;
  border-top: 1px solid #d0d7de;
  border-right: 1px solid #d0d7de;
  vertical-align: top;
}}
td:last-child {{
  border-right: none;
}}
tbody tr:nth-child(2n) {{
  background-color: #f6f8fa;
}}
code {{
  font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace;
  background: rgba(175, 184, 193, 0.2);
  padding: 0.2em 0.4em;
  border-radius: 6px;
  font-size: 85%;
}}
</style></head>
<body>
{table_html}
</body></html>"""


async def render_table_to_image_bytes(
    table_text: str, timeout: int = 30000
) -> bytes | None:
    """Render a markdown table block to PNG bytes via Playwright.

    Returns ``None`` on parse failure or rendering error.
    """
    try:
        header, body = parse_markdown_table(table_text)
        if not header:
            return None
        html_content = build_table_html(header, body)
    except Exception as e:
        logger.error(f"构建表格 HTML 失败: {e}")
        return None

    try:
        from .browser import render_html_to_image
    except ImportError:
        from browser import render_html_to_image

    try:
        return await render_html_to_image(
            html_content=html_content,
            selector="table",
            width=1400,
            scale_factor=2,
            timeout=timeout,
        )
    except Exception as e:
        logger.error(f"渲染表格图片失败: {e}")
        return None


def split_text_around_tables(text: str) -> list[dict]:
    """Split ``text`` into a list of segment dicts.

    Each segment is one of:
      - ``{"type": "text", "text": <substring>}``
      - ``{"type": "table", "text": <table_md>}``

    Slicing happens at each detected table's ``[start, end)`` boundaries.
    Empty text segments between/around tables are omitted.
    """
    matches = detect_markdown_tables(text)
    if not matches:
        return [{"type": "text", "text": text}]

    segments: list[dict] = []
    last_end = 0
    for start, end, table_text in matches:
        if start > last_end:
            segments.append({"type": "text", "text": text[last_end:start]})
        segments.append({"type": "table", "text": table_text})
        last_end = end
    if last_end < len(text):
        segments.append({"type": "text", "text": text[last_end:]})
    return segments
