"""Pure-stdlib Markdown link handling — importable from main.py and tests.

Links are flattened to ``描述文本(url)`` instead of dropping the URL, and URLs
are shielded from the emphasis-stripping regexes (``_`` / ``*`` / ``~`` inside
a URL must survive untouched).
"""

from __future__ import annotations

import re
from collections.abc import Callable

# ![alt](url "title") — allows one level of balanced parentheses in the URL.
_IMAGE_RE = re.compile(
    r"!\[([^\]\n]*)\]\(\s*<?((?:[^()\s<>]|\([^()\s]*\))+)>?"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)"
)
# [text](url "title")
_LINK_RE = re.compile(
    r"(?<!!)\[([^\]\n]+)\]\(\s*<?((?:[^()\s<>]|\([^()\s]*\))+)>?"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)"
)
# <https://example.com>
_AUTOLINK_RE = re.compile(r"<((?:https?|ftp)://[^\s<>]+|mailto:[^\s<>]+)>")
# Bare URLs, allowing balanced parentheses such as wiki/Foo_(bar).
_BARE_URL_RE = re.compile(
    r"(?:https?|ftp)://(?:[^\s<>()\[\]]|\([^\s<>()\[\]]*\))+", re.IGNORECASE
)
# Trailing characters that are more likely Markdown/punctuation than URL.
_URL_TRAILING_STRIP = "*_~.,;:!?'\""

_PLACEHOLDER = "\x00URL{}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00URL(\d+)\x00")


def _strip_scheme(url: str) -> str:
    return re.sub(r"^(?:[a-z][a-z0-9+.-]*:)(?://)?", "", url, flags=re.IGNORECASE)


def _format_link(label: str, url: str) -> str:
    label = label.strip()
    if not label or label == url or label == _strip_scheme(url):
        return url
    return f"{label}({url})"


def convert_markdown_links(text: str) -> str:
    """``[text](url)`` -> ``text(url)``; ``<url>`` -> ``url``; images -> alt text.

    When the label is empty or is the URL itself, only the URL is kept.
    """
    text = _IMAGE_RE.sub(lambda m: m.group(1), text)
    text = _LINK_RE.sub(lambda m: _format_link(m.group(1), m.group(2)), text)
    text = _AUTOLINK_RE.sub(lambda m: m.group(1), text)
    return text


def protect_urls(text: str) -> tuple[str, Callable[[str], str]]:
    """Replace bare URLs with placeholders; return (text, restore)."""
    urls: list[str] = []

    def stash(match: re.Match[str]) -> str:
        url = match.group(0)
        core = url.rstrip(_URL_TRAILING_STRIP)
        urls.append(core)
        return _PLACEHOLDER.format(len(urls) - 1) + url[len(core) :]

    protected = _BARE_URL_RE.sub(stash, text)

    def restore(value: str) -> str:
        if not urls:
            return value
        return _PLACEHOLDER_RE.sub(lambda m: urls[int(m.group(1))], value)

    return protected, restore


__all__ = ["convert_markdown_links", "protect_urls"]
