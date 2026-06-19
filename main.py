from astrbot.api import AstrBotConfig, logger
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import ResultContentType

try:
    from .utils import (
        EnvManager,
        close_browser,
        detect_markdown_tables,
        parse_markdown_table,
        render_table_to_image_bytes,
        split_text_around_tables,
    )
except ImportError:  # pragma: no cover - fallback when loaded as top-level module
    from utils import (  # type: ignore
        EnvManager,
        close_browser,
        detect_markdown_tables,
        parse_markdown_table,
        render_table_to_image_bytes,
        split_text_around_tables,
    )

import re


@register(
    "astrbot_plugin_markdown_killer",
    "xkeyC",
    "移除输出中的Markdown格式（修正列表换行、新增表格图片渲染）",
    "0.2.0",
    "https://github.com/xkeyC/astrbot_plugin_markdown_killer",
)
class MarkdownKillerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.remove_extra_newlines = self._config_get("remove_extra_newlines", True)
        self.newline_mode = self._config_get("newline_mode", "segment_boundary")
        self.enable_table_render = self._config_get("enable_table_render", True)
        self.table_render_fallback = self._config_get("table_render_fallback", "text")

        # Detect Playwright availability up-front; re-checked in initialize().
        self._playwright_available = False
        try:
            import playwright  # noqa: F401

            self._playwright_available = True
        except ImportError:
            logger.warning(
                "[MarkdownKiller] 未安装 playwright，表格渲染功能将被禁用。"
            )

        self._env_manager: EnvManager | None = None

    async def initialize(self) -> None:
        """Called when the plugin is activated. Sets up Playwright env if enabled."""
        if not self.enable_table_render:
            return
        try:
            data_dir = str(StarTools.get_data_dir("astrbot_plugin_markdown_killer"))
            self._env_manager = EnvManager(data_dir)

            if not self._env_manager.is_installed():
                logger.info(
                    "[MarkdownKiller] 首次启用表格渲染，正在准备 Playwright Chromium..."
                )
                await self._env_manager.install_dependencies()

            # Re-verify after potential install.
            if self._env_manager.is_installed():
                self._playwright_available = True
            else:
                self._playwright_available = await self._env_manager.verify_playwright()

            if not self._playwright_available:
                logger.warning(
                    "[MarkdownKiller] Playwright 不可用，已自动关闭表格渲染。"
                )
                self.enable_table_render = False
        except Exception as e:
            logger.error(f"[MarkdownKiller] 初始化 Playwright 失败: {e}")
            self.enable_table_render = False
            self._playwright_available = False

    async def terminate(self) -> None:
        """Called when the plugin is disabled/reloaded. Closes the browser (best-effort)."""
        try:
            await close_browser()
        except Exception as e:
            logger.debug(f"[MarkdownKiller] 关闭浏览器时出错: {e}")

    def _config_get(self, key: str, default=None):
        """兼容 AstrBotConfig 与普通 dict 的配置读取。"""
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse, *args):
        """
        监听LLM回复，移除Markdown格式（保留 Markdown 表格语法，由 on_decorating_result 处理）。
        """
        if not resp or not resp.completion_text:
            return

        original_text = resp.completion_text
        cleaned_text = self.remove_markdown(original_text)

        if original_text != cleaned_text:
            resp.completion_text = cleaned_text
            self._log_cleaned_text(original_text, cleaned_text)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        监听所有即将发送出的消息：
        1. 表格图片渲染（始终尝试，独立于全局开关）。
        2. 全局 Markdown 移除（受 enable_global_markdown_killer 控制）。
        """
        result = event.get_result()
        if not result or not hasattr(result, "chain") or not result.chain:
            return

        # Streaming results were already shown to the user as raw text; skip.
        if result.result_content_type == ResultContentType.STREAMING_FINISH:
            return

        # Phase 1: extract tables and render to images.
        if self.enable_table_render and self._playwright_available:
            await self._render_tables_in_chain(result)

        # Phase 2: global markdown removal (only when enabled).
        if self._config_get("enable_global_markdown_killer", False):
            for comp in result.chain:
                text = getattr(comp, "text", None)
                if not isinstance(text, str):
                    continue
                cleaned_text = self.remove_markdown(text)
                if cleaned_text != text:
                    comp.text = cleaned_text
                    self._log_cleaned_text(text, cleaned_text, source="[全局过滤]")

    async def _render_tables_in_chain(self, result) -> None:
        """Scan the chain for Plain components containing markdown tables; render as images."""
        new_chain = []
        for comp in result.chain:
            text = getattr(comp, "text", None)
            if (
                isinstance(comp, Comp.Plain)
                and isinstance(text, str)
                and detect_markdown_tables(text)
            ):
                segments = split_text_around_tables(text)
                for seg in segments:
                    if seg["type"] == "text":
                        if seg["text"].strip():
                            new_chain.append(Comp.Plain(seg["text"]))
                    elif seg["type"] == "table":
                        image_bytes = await render_table_to_image_bytes(
                            seg["text"], timeout=20000
                        )
                        if image_bytes:
                            new_chain.append(Comp.Image.fromBytes(image_bytes))
                            logger.info("[MarkdownKiller] 表格已渲染为图片")
                        else:
                            fallback = self._apply_table_fallback(seg["text"])
                            if fallback is not None:
                                new_chain.append(Comp.Plain(fallback))
                            logger.warning(
                                f"[MarkdownKiller] 表格渲染失败，已按 "
                                f"{self.table_render_fallback} 策略回退"
                            )
            else:
                new_chain.append(comp)
        result.chain = new_chain

    def _apply_table_fallback(self, table_md: str) -> str | None:
        """Return fallback text for a table block according to table_render_fallback config."""
        mode = self.table_render_fallback
        if mode == "text":
            return self._table_to_text(table_md)
        if mode == "raw":
            return table_md
        if mode == "remove":
            return None
        return self._table_to_text(table_md)

    def _table_to_text(self, table_md: str) -> str:
        """Convert a markdown table block to simple plain text (no separator row)."""
        try:
            header, body = parse_markdown_table(table_md)
        except Exception:
            return table_md
        if not header:
            return table_md
        lines = [" | ".join(header)]
        for row in body:
            lines.append(" | ".join(row))
        return "\n".join(lines)

    def _log_cleaned_text(self, original_text: str, cleaned_text: str, source: str = ""):
        """输出 Markdown 清理日志，source 用于区分全局过滤等来源。"""
        original_preview = original_text[:50].replace('\n', '\\n')
        cleaned_preview = cleaned_text[:50].replace('\n', '\\n')
        source_prefix = f" {source}" if source else ""
        log_msg = (
            "\n[Markdown Killer] --------------------------------------------------"
            f"\n[Markdown Killer]{source_prefix} 检测到Markdown并移除:"
            f"\n[Markdown Killer] 原文: {original_preview}..."
            f"\n[Markdown Killer] 处理: {cleaned_preview}..."
            "\n[Markdown Killer] --------------------------------------------------"
        )
        logger.warning(log_msg)

    def _remove_extra_newlines_segment_boundary(self, text: str) -> str:
        """
        Remove newlines after segment boundaries (punctuation marks).

        AstrBot segments messages at punctuation marks (。？！~…).
        Newlines immediately after punctuation become the START of the next
        segment, appearing as leading blank lines when sent.

        Example:
            Original: "第一句。\n\n第二句。"
            After split: ["第一句。", "\n\n第二句。"]
            Result: Extra blank lines before "第二句。"

        Solution: Remove newlines right after segment punctuation.
        """
        text = re.sub(r"([。？！~…])\n+", r"\1", text)
        return text.strip()

    def _remove_extra_newlines_global(self, text: str) -> str:
        """
        Compress consecutive newlines globally.

        Keeps at most one blank line between content for paragraph structure.
        More aggressive but preserves intentional paragraph breaks.
        """
        lines = text.split("\n")
        result_lines = []
        prev_was_empty = False

        for line in lines:
            stripped = line.rstrip()
            is_empty = not stripped

            if is_empty:
                if not prev_was_empty and result_lines:
                    result_lines.append("")
                prev_was_empty = True
            else:
                result_lines.append(stripped)
                prev_was_empty = False

        while result_lines and not result_lines[-1]:
            result_lines.pop()

        return "\n".join(result_lines)

    def _split_table_blocks(self, text: str) -> list[tuple[str, bool]]:
        """Split text into [(block, is_table), ...] for selective processing."""
        segments = split_text_around_tables(text)
        return [(seg["text"], seg["type"] == "table") for seg in segments]

    def _remove_list_markers(self, text: str) -> str:
        """
        合并连续列表项到同一行，避免被分段发送时拆分为多条消息。

        - 无序列表 [-*+]: 项与项之间用 `"; "` 连接，如 `a; b; c`。
        - 有序列表 [N. / N)]: 标记替换为 `N)`（无空格），项之间用空格连接，
          如 `1)First 2)Second 3)Third`。
        - 空行会中断列表 run，生成多个合并行。
        - 缩进的连续行作为上一项内容的延续（用空格连接）。
        - 函数幂等：再次输入输出文本不会进一步改变。
        """
        lines = text.split("\n")
        output_lines: list[str] = []
        i = 0
        n = len(lines)

        unord_re = re.compile(r"^(\s*)[-*+]\s+(.+?)\s*$")
        ord_re = re.compile(r"^(\s*)(\d+)[.)]\s+(.+?)\s*$")

        while i < n:
            line = lines[i]
            m_unord = unord_re.match(line)
            m_ord = ord_re.match(line)

            if m_ord:
                is_ordered = True
                indent = m_ord.group(1)
                items: list[tuple[str | None, str]] = [
                    (m_ord.group(2), m_ord.group(3))
                ]
                i += 1
            elif m_unord:
                is_ordered = False
                indent = m_unord.group(1)
                items = [(None, m_unord.group(2))]
                i += 1
            else:
                output_lines.append(line)
                i += 1
                continue

            while i < n:
                cur = lines[i]
                m_unord_cur = unord_re.match(cur)
                m_ord_cur = ord_re.match(cur)

                if is_ordered and m_ord_cur and m_ord_cur.group(1) == indent:
                    items.append((m_ord_cur.group(2), m_ord_cur.group(3)))
                    i += 1
                    continue
                if (not is_ordered) and m_unord_cur and m_unord_cur.group(1) == indent:
                    items.append((None, m_unord_cur.group(2)))
                    i += 1
                    continue

                # Indented continuation: append to last item's content.
                indent_match = re.match(r"^(\s*)(\S.*)?$", cur)
                cur_indent = indent_match.group(1) if indent_match else ""
                if cur.strip() and len(cur_indent) > len(indent):
                    last_num, last_content = items[-1]
                    items[-1] = (last_num, f"{last_content} {cur.strip()}")
                    i += 1
                    continue
                break

            if is_ordered:
                joined = " ".join(f"{num}){content}" for num, content in items)
            else:
                joined = "; ".join(content for _, content in items)
            output_lines.append(joined)

        return "\n".join(output_lines)

    def remove_markdown(self, text: str) -> str:
        """
        移除文本中的 Markdown 格式。

        Markdown 表格语法会被保留（仅做行尾空白清理），交由 on_decorating_result
        阶段渲染为图片。
        """
        blocks = self._split_table_blocks(text)
        out_parts: list[str] = []
        for block, is_table in blocks:
            if is_table:
                # Preserve table syntax; only strip trailing whitespace per line.
                out_parts.append(
                    "\n".join(ln.rstrip() for ln in block.split("\n"))
                )
                continue
            out_parts.append(self._remove_markdown_no_tables(block))
        return "".join(out_parts)

    def _remove_markdown_no_tables(self, text: str) -> str:
        """Apply markdown-removal regexes to a table-free block of text."""
        # 移除代码块 (保留内容)
        text = re.sub(r"```(?:[a-zA-Z0-9+\-]*\s+)?([\s\S]*?)```", r"\1", text)

        # 移除行内代码 `code` -> code
        text = re.sub(r"`([^`]+)`", r"\1", text)

        # 移除图片 ![alt](url) -> alt (提前于普通链接处理避免残留 "!")
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)

        # 移除普通链接 [text](url) -> text
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

        # 移除粗体 - 使用非贪婪匹配以支持内部包含特殊符号的情况
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        text = re.sub(r"__(.*?)__", r"\1", text)

        # 移除斜体 - 严格模式，避免误伤数学公式 (3 * 4 = 12 / 3*4*5) 或变量名 (this_is_var)
        text = re.sub(r"(^|[^\w\*])\*(?!\s)(.*?)(?<!\s)\*(?=$|[^\w\*])", r"\1\2", text)
        text = re.sub(r"(^|[^\w_])_(?!\s)(.*?)(?<!\s)_(?=$|[^\w_])", r"\1\2", text)

        # 移除删除线
        text = re.sub(r"~~(.*?)~~", r"\1", text)

        # 移除标题 (包含多级标题)
        text = re.sub(r"^(#{1,6})\s+(.*)", r"\2", text, flags=re.MULTILINE)

        # 移除引用 (处理嵌套情况: >>> text -> text)
        text = re.sub(r"^(?:>\s*)+(.*)", r"\1", text, flags=re.MULTILINE)

        # 移除列表标记 (无序 + 有序)，并将连续列表项合并到同一行
        text = self._remove_list_markers(text)

        # Remove extra newlines if enabled
        if self.remove_extra_newlines:
            if self.newline_mode == "global":
                text = self._remove_extra_newlines_global(text)
            else:
                text = self._remove_extra_newlines_segment_boundary(text)

        return text
