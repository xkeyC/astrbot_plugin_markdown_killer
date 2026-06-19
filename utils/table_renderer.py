"""Markdown table detection, parsing, HTML/CSS generation, and image rendering."""

from __future__ import annotations

import html
import re

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
        cells = re.split(r"\s*\|\s*", s)
        return [c.strip() for c in cells]

    header = parse_row(lines[0])
    # Line 1 is the separator; skip it. Lines 2..N are body rows.
    body = [parse_row(ln) for ln in lines[2:]]
    return (header, body)


def build_table_html(header_cells: list[str], body_rows: list[list[str]]) -> str:
    """Build a complete standalone HTML document with the table styled as a card."""

    def esc(s: str) -> str:
        return html.escape(s, quote=False)

    header_html = "".join(f"<th>{esc(c)}</th>" for c in header_cells)
    body_html = "".join(
        f"<tr>{''.join(f'<td>{esc(c)}</td>' for c in row)}</tr>"
        for row in body_rows
    )

    table_html = (
        f"<table><thead><tr>{header_html}</tr></thead>"
        f"<tbody>{body_html}</tbody></table>"
    )

    # NOTE: cells are escaped with html.escape(quote=False) only; we do NOT
    # convert markdown `` `code` `` syntax inside cells to <code> tags. The
    # ``code`` CSS rule below is forward-compat: if a future commit adds
    # inline-code parsing, the styling will already match GitHub's look.
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
