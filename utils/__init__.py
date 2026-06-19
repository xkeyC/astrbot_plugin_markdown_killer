"""Utility package: Playwright browser, env manager, and table renderer."""

from .browser import close_browser, get_browser, render_html_to_image
from .env_manager import EnvManager
from .table_renderer import (
    build_table_html,
    detect_markdown_tables,
    parse_markdown_table,
    render_table_to_image_bytes,
    split_text_around_tables,
)

__all__ = [
    "EnvManager",
    "build_table_html",
    "close_browser",
    "detect_markdown_tables",
    "get_browser",
    "parse_markdown_table",
    "render_html_to_image",
    "render_table_to_image_bytes",
    "split_text_around_tables",
]
