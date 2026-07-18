"""LaTeX formula detection and local MathML/Chromium image rendering."""

from __future__ import annotations

import html
import re

from astrbot.api import logger


_BLOCK_FORMULA_RE = re.compile(
    r"(?<!\\)\\\[(?P<bracket>.+?)(?<!\\)\\\]"
    r"|(?<![$\\])\$\$(?P<dollar>.+?)(?<![$\\])\$\$(?!\$)",
    re.DOTALL,
)
_INLINE_FORMULA_RE = re.compile(
    r"(?<!\\)\\\((?P<bracket>.+?)(?<!\\)\\\)"
    r"|(?<![$\\])\$(?![\s$])(?P<dollar>.+?)(?<![\s\\])\$(?![\d$])"
)
_CODE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`+[^\n]*?`+")


def _overlaps_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _code_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _CODE_RE.finditer(text)]


def _formula_source(match: re.Match[str]) -> str:
    return match.group("bracket") or match.group("dollar") or ""


def _inline_matches(text: str) -> list[re.Match[str]]:
    code_spans = _code_spans(text)
    return [
        match
        for match in _INLINE_FORMULA_RE.finditer(text)
        if not _overlaps_any(match.start(), match.end(), code_spans)
    ]


def contains_latex_formulas(text: str) -> bool:
    """Return whether text contains a supported formula outside code spans."""
    code_spans = _code_spans(text)
    if any(
        not _overlaps_any(match.start(), match.end(), code_spans)
        for match in _BLOCK_FORMULA_RE.finditer(text)
    ):
        return True
    return bool(_inline_matches(text))


def _append_segment(
    segments: list[dict], segment_type: str, text: str, **extra
) -> None:
    if not text:
        return
    if segment_type == "text" and segments and segments[-1]["type"] == "text":
        segments[-1]["text"] += text
        return
    segments.append({"type": segment_type, "text": text, **extra})


def _split_inline_lines(text: str, segments: list[dict]) -> None:
    """Turn each physical line containing inline math into one render job.

    Rendering the complete line keeps prose, punctuation, and every inline
    formula on the same baseline. Sending each small formula as an individual
    message image causes most OneBot/QQ adapters to break the sentence.
    """
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        line_ending = line[len(content) :]
        if content and _inline_matches(content):
            _append_segment(
                segments,
                "formula",
                content,
                display=False,
                raw=content,
            )
        else:
            _append_segment(segments, "text", content)
        _append_segment(segments, "text", line_ending)


def split_text_around_formulas(text: str) -> list[dict]:
    """Split text into plain-text and formula-image render segments.

    Block math (``\\[...\\]`` and ``$$...$$``) becomes an independent image.
    A line containing inline math (``\\(...\\)`` or ``$...$``) becomes one
    image so its baseline and surrounding prose remain intact.
    """
    code_spans = _code_spans(text)
    block_matches = [
        match
        for match in _BLOCK_FORMULA_RE.finditer(text)
        if not _overlaps_any(match.start(), match.end(), code_spans)
    ]
    if not block_matches:
        segments: list[dict] = []
        _split_inline_lines(text, segments)
        return segments or [{"type": "text", "text": text}]

    segments = []
    cursor = 0
    for match in block_matches:
        if match.start() < cursor:
            continue
        _split_inline_lines(text[cursor : match.start()], segments)
        _append_segment(
            segments,
            "formula",
            _formula_source(match).strip(),
            display=True,
            raw=match.group(0),
        )
        cursor = match.end()
    _split_inline_lines(text[cursor:], segments)
    return segments


def _convert_latex(latex: str, display: bool) -> str:
    try:
        from latex2mathml.converter import convert
    except ImportError as exc:  # pragma: no cover - exercised by render fallback
        raise RuntimeError("缺少 latex2mathml 依赖") from exc

    mathml = convert(latex.strip())
    if display:
        if re.search(r"<math\b[^>]*\bdisplay=", mathml):
            mathml = re.sub(
                r'(<math\b[^>]*?)\sdisplay="[^"]*"',
                r'\1 display="block"',
                mathml,
                count=1,
            )
        else:
            mathml = re.sub(r"<math(?=[\s>])", '<math display="block"', mathml, count=1)
    return mathml


def build_formula_html(source: str, display: bool) -> str:
    """Build a standalone, local-only HTML document for a formula segment."""
    if display:
        content_html = _convert_latex(source, display=True)
        content_class = "display-formula"
    else:
        pieces: list[str] = []
        cursor = 0
        matches = _inline_matches(source)
        if not matches:
            raise ValueError("行内公式片段中未找到公式")
        for match in matches:
            pieces.append(html.escape(source[cursor : match.start()], quote=False))
            pieces.append(_convert_latex(_formula_source(match), display=False))
            cursor = match.end()
        pieces.append(html.escape(source[cursor:], quote=False))
        content_html = "".join(pieces)
        content_class = "inline-formula-line"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: transparent; }}
.formula-image {{
  display: inline-flex;
  align-items: baseline;
  justify-content: center;
  padding: 9px 12px;
  color: #1f2328;
  background: #ffffff;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans',
    Helvetica, Arial, sans-serif, 'PingFang SC', 'Microsoft YaHei';
  font-size: 18px;
  line-height: 1.55;
  white-space: nowrap;
}}
.display-formula {{ min-width: 120px; text-align: center; }}
.inline-formula-line {{ text-align: left; }}
math {{ font-size: 1.15em; vertical-align: -0.12em; }}
</style></head>
<body><div class="formula-image {content_class}">{content_html}</div></body></html>"""


async def render_formula_to_image_bytes(
    source: str, display: bool, timeout: int = 30000
) -> bytes | None:
    """Render a block formula or an inline-formula line to PNG bytes."""
    try:
        html_content = build_formula_html(source, display)
    except Exception as exc:
        logger.error(f"构建公式 HTML 失败: {exc}")
        return None

    try:
        from .browser import render_html_to_image
    except ImportError:  # pragma: no cover - top-level import fallback
        from browser import render_html_to_image

    try:
        return await render_html_to_image(
            html_content=html_content,
            selector=".formula-image",
            width=1400,
            scale_factor=2,
            timeout=timeout,
        )
    except Exception as exc:
        logger.error(f"渲染公式图片失败: {exc}")
        return None
