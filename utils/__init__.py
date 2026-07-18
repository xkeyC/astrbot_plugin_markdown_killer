"""Utility package: browser/env, table/formula renderers, and list processing."""

from .browser import close_browser, get_browser, render_html_to_image
from .env_manager import EnvManager
from .list_processor import remove_list_markers
from .formula_renderer import (
    build_formula_html,
    contains_latex_formulas,
    render_formula_to_image_bytes,
    split_text_around_formulas,
)
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
    "build_formula_html",
    "close_browser",
    "contains_latex_formulas",
    "detect_markdown_tables",
    "get_browser",
    "parse_markdown_table",
    "remove_list_markers",
    "render_html_to_image",
    "render_formula_to_image_bytes",
    "render_table_to_image_bytes",
    "split_text_around_tables",
    "split_text_around_formulas",
]
