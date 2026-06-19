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

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: 'Microsoft YaHei', 'PingFang SC', 'Hiragino Sans GB', sans-serif;
  background: transparent;
  padding: 12px;
}}
table {{
  border-collapse: collapse;
  background: #ffffff;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 2px 8px rgba(0,0,0,.08);
  font-size: 15px;
  color: #333;
}}
th {{
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: #ffffff;
  padding: 12px 16px;
  font-weight: 600;
  text-align: left;
  border: 1px solid #5a6ab8;
}}
td {{
  padding: 10px 16px;
  border: 1px solid #e0e0e0;
  background: #ffffff;
}}
tbody tr:nth-child(even) td {{
  background: #f8f9ff;
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
