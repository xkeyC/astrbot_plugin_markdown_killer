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
        contains_latex_formulas,
        detect_markdown_tables,
        parse_markdown_table,
        render_formula_to_image_bytes,
        render_table_to_image_bytes,
        split_text_around_formulas,
        split_text_around_tables,
    )
    from .utils.list_processor import remove_list_markers as _remove_list_markers_impl
except ImportError:  # pragma: no cover - fallback when loaded as top-level module
    from utils import (  # type: ignore
        EnvManager,
        close_browser,
        contains_latex_formulas,
        detect_markdown_tables,
        parse_markdown_table,
        render_formula_to_image_bytes,
        render_table_to_image_bytes,
        split_text_around_formulas,
        split_text_around_tables,
    )
    from utils.list_processor import (  # type: ignore
        remove_list_markers as _remove_list_markers_impl,
    )

import asyncio
import re
import time

_LIST_ITEM_LINE_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")


def _is_list_context_line(
    line: str, active_list_indent: int | None
) -> tuple[bool, int | None]:
    """Return whether ``line`` belongs to a Markdown list block.

    Besides explicit marker lines, indented non-blank lines after a list marker
    are Markdown list continuations and must keep their physical line breaks.
    """
    marker_match = _LIST_ITEM_LINE_RE.match(line)
    if marker_match:
        return True, len(line) - len(line.lstrip())

    if active_list_indent is not None and line.strip():
        indent = len(line) - len(line.lstrip())
        if indent > active_list_indent:
            return True, active_list_indent

    return False, None


@register(
    "astrbot_plugin_markdown_killer",
    "xkeyC",
    "移除输出中的Markdown格式（保留列表标记换行、支持表格与公式图片渲染）",
    "0.3.0",
    "https://github.com/xkeyC/astrbot_plugin_markdown_killer",
)
class MarkdownKillerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.remove_extra_newlines = self._config_get("remove_extra_newlines", True)
        self.newline_mode = self._config_get("newline_mode", "segment_boundary")
        self.list_merge_char_threshold = self._coerce_int(
            self._config_get("list_merge_char_threshold", 30), default=30
        )
        self.enable_table_render = self._config_get("enable_table_render", True)
        self.table_render_fallback = self._config_get("table_render_fallback", "text")
        self.enable_formula_render = self._config_get("enable_formula_render", True)
        self.formula_render_fallback = self._config_get(
            "formula_render_fallback", "raw"
        )

        # N4: suppress repeated warnings for table-render failures — log the
        # first occurrence at warning level and downgrade subsequent ones to
        # debug, since the same root cause typically affects every render.
        self._table_render_failure_logged: bool = False
        self._formula_render_failure_logged: bool = False

        # Detect Playwright availability up-front; re-checked in initialize().
        self._playwright_available = False
        try:
            import playwright  # noqa: F401

            self._playwright_available = True
        except ImportError:
            logger.warning(
                "[MarkdownKiller] 未安装 playwright，表格与公式渲染功能将被禁用。"
            )

        self._env_manager: EnvManager | None = None

    async def initialize(self) -> None:
        """Called when the plugin is activated. Sets up Playwright env if enabled."""
        if not self.enable_table_render and not self.enable_formula_render:
            return
        try:
            data_dir = str(StarTools.get_data_dir("astrbot_plugin_markdown_killer"))
            self._env_manager = EnvManager(data_dir)

            if not self._env_manager.is_installed():
                logger.info(
                    "[MarkdownKiller] 首次启用图片渲染，正在准备 Playwright Chromium..."
                )
                await self._env_manager.install_dependencies()

            # Re-verify after potential install.
            if self._env_manager.is_installed():
                self._playwright_available = True
            else:
                self._playwright_available = await self._env_manager.verify_playwright()

            if not self._playwright_available:
                logger.warning(
                    "[MarkdownKiller] Playwright 不可用，已自动关闭表格与公式渲染。"
                )
                self.enable_table_render = False
                self.enable_formula_render = False
        except Exception as e:
            logger.error(f"[MarkdownKiller] 初始化 Playwright 失败: {e}")
            self.enable_table_render = False
            self.enable_formula_render = False
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

    def _coerce_int(self, value, default: int) -> int:
        """将配置值 (WebUI 可能传入字符串) 强制转为 int；失败则回退 default。"""
        try:
            return int(value)
        except (TypeError, ValueError):
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

        # Phase 1: extract tables/formulas and render them to images.
        rendered_image_ids: set[int] = set()
        if self.enable_table_render and self._playwright_available:
            rendered_image_ids.update(await self._render_tables_in_chain(result))
        if self.enable_formula_render and self._playwright_available:
            rendered_image_ids.update(await self._render_formulas_in_chain(result))

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

        # Global cleanup can trim the boundary newlines added in phase 1.
        if rendered_image_ids:
            result.chain = self._separate_rendered_images(
                result.chain, rendered_image_ids
            )

    async def _render_tables_in_chain(self, result) -> set[int]:
        """Scan the chain for Plain components containing markdown tables; render as images.

        Tables within the SAME message are rendered concurrently via
        ``asyncio.gather`` (N3) to minimize total wall-clock latency. A single
        summary log line is emitted at info level; per-table success/fallback
        logs are downgraded to debug (N4). The fallback policy
        (``table_render_fallback``: text/raw/remove) is unchanged.
        """
        start_ts = time.perf_counter()

        # Collect render jobs: (comp_index, seg_index_within_comp, table_text).
        # Two Plain comps in the same chain each with a table → two parallel jobs.
        jobs: list[tuple[int, int, str]] = []
        for comp_index, comp in enumerate(result.chain):
            text = getattr(comp, "text", None)
            if (
                isinstance(comp, Comp.Plain)
                and isinstance(text, str)
                and detect_markdown_tables(text)
            ):
                segments = split_text_around_tables(text)
                for seg_index, seg in enumerate(segments):
                    if seg["type"] == "table":
                        jobs.append((comp_index, seg_index, seg["text"]))

        if not jobs:
            return set()

        # Render all tables in parallel; exceptions become None (fallback path).
        render_tasks = [
            render_table_to_image_bytes(job_text, timeout=20000)
            for _, _, job_text in jobs
        ]
        gathered = await asyncio.gather(*render_tasks, return_exceptions=True)
        results: dict[tuple[int, int], bytes | None] = {}
        for (comp_index, seg_index, _), outcome in zip(jobs, gathered):
            if isinstance(outcome, Exception):
                results[(comp_index, seg_index)] = None
            else:
                results[(comp_index, seg_index)] = outcome

        # Reconstruct the chain using rendered images / fallbacks (order preserved).
        new_chain = []
        rendered_image_ids: set[int] = set()
        for comp_index, comp in enumerate(result.chain):
            text = getattr(comp, "text", None)
            if not (
                isinstance(comp, Comp.Plain)
                and isinstance(text, str)
                and detect_markdown_tables(text)
            ):
                new_chain.append(comp)
                continue

            segments = split_text_around_tables(text)
            for seg_index, seg in enumerate(segments):
                if seg["type"] == "text":
                    if seg["text"].strip():
                        new_chain.append(Comp.Plain(seg["text"]))
                elif seg["type"] == "table":
                    image_bytes = results.get((comp_index, seg_index))
                    if image_bytes:
                        image = Comp.Image.fromBytes(image_bytes)
                        new_chain.append(image)
                        rendered_image_ids.add(id(image))
                        logger.debug("[MarkdownKiller] 表格已渲染为图片")
                    else:
                        fallback = self._apply_table_fallback(seg["text"])
                        if fallback is not None:
                            new_chain.append(Comp.Plain(fallback))
                        fallback_msg = (
                            f"[MarkdownKiller] 表格渲染失败，已按 "
                            f"{self.table_render_fallback} 策略回退"
                        )
                        if not self._table_render_failure_logged:
                            logger.warning(fallback_msg)
                            self._table_render_failure_logged = True
                        else:
                            logger.debug(fallback_msg)
        # Image components are inline in AstrBot's message chain. Preserve a
        # block boundary around rendered tables so adjacent text (or another
        # rendered table) is not visually glued to the image. Do not add
        # leading/trailing whitespace for a table at the message boundary.
        result.chain = self._separate_rendered_images(new_chain, rendered_image_ids)

        # Set disable_segment_reply so RespondStage sends the entire chain
        # (text + table images + text) as ONE message instead of splitting
        # each component into a separate message. This preserves the
        # original interleaved order (text → table image → text → ...).
        has_image = any(isinstance(c, Comp.Image) for c in result.chain)
        if has_image:
            result.disable_segment_reply = True

        elapsed = time.perf_counter() - start_ts
        logger.info(f"[MarkdownKiller] 渲染 {len(jobs)} 个表格，耗时 {elapsed:.2f}s")
        return rendered_image_ids

    def _split_formula_blocks(self, text: str) -> list[dict]:
        """Split formulas while leaving any failed/raw Markdown table intact."""
        segments: list[dict] = []
        for table_segment in split_text_around_tables(text):
            if table_segment["type"] == "table":
                segments.append({"type": "text", "text": table_segment["text"]})
            else:
                segments.extend(split_text_around_formulas(table_segment["text"]))
        return segments

    async def _render_formulas_in_chain(self, result) -> set[int]:
        """Render block formulas and inline-formula lines, preserving chain order."""
        start_ts = time.perf_counter()
        jobs: list[tuple[int, int, str, bool]] = []
        component_segments: dict[int, list[dict]] = {}

        for comp_index, comp in enumerate(result.chain):
            text = getattr(comp, "text", None)
            if not (
                isinstance(comp, Comp.Plain)
                and isinstance(text, str)
                and contains_latex_formulas(text)
            ):
                continue
            segments = self._split_formula_blocks(text)
            component_segments[comp_index] = segments
            for seg_index, segment in enumerate(segments):
                if segment["type"] == "formula":
                    jobs.append(
                        (
                            comp_index,
                            seg_index,
                            segment["text"],
                            bool(segment["display"]),
                        )
                    )

        if not jobs:
            return set()

        gathered = await asyncio.gather(
            *[
                render_formula_to_image_bytes(source, display, timeout=20000)
                for _, _, source, display in jobs
            ],
            return_exceptions=True,
        )
        results: dict[tuple[int, int], bytes | None] = {}
        for (comp_index, seg_index, _, _), outcome in zip(jobs, gathered):
            results[(comp_index, seg_index)] = (
                None if isinstance(outcome, Exception) else outcome
            )

        new_chain = []
        rendered_image_ids: set[int] = set()
        for comp_index, comp in enumerate(result.chain):
            segments = component_segments.get(comp_index)
            if segments is None:
                new_chain.append(comp)
                continue
            for seg_index, segment in enumerate(segments):
                if segment["type"] == "text":
                    if segment["text"]:
                        new_chain.append(Comp.Plain(segment["text"]))
                    continue

                image_bytes = results.get((comp_index, seg_index))
                if image_bytes:
                    image = Comp.Image.fromBytes(image_bytes)
                    new_chain.append(image)
                    rendered_image_ids.add(id(image))
                    logger.debug("[MarkdownKiller] 公式已渲染为图片")
                    continue

                fallback = self._apply_formula_fallback(segment)
                if fallback is not None:
                    new_chain.append(Comp.Plain(fallback))
                fallback_msg = (
                    "[MarkdownKiller] 公式渲染失败，已按 "
                    f"{self.formula_render_fallback} 策略回退"
                )
                if not self._formula_render_failure_logged:
                    logger.warning(fallback_msg)
                    self._formula_render_failure_logged = True
                else:
                    logger.debug(fallback_msg)

        result.chain = self._separate_rendered_images(new_chain, rendered_image_ids)
        if rendered_image_ids:
            result.disable_segment_reply = True

        elapsed = time.perf_counter() - start_ts
        logger.info(
            f"[MarkdownKiller] 渲染 {len(jobs)} 个公式片段，耗时 {elapsed:.2f}s"
        )
        return rendered_image_ids

    def _apply_formula_fallback(self, segment: dict) -> str | None:
        mode = self.formula_render_fallback
        if mode == "remove":
            return None
        if mode == "text":
            return segment["text"]
        return segment.get("raw", segment["text"])

    @staticmethod
    def _separate_rendered_images(chain, rendered_image_ids: set[int]):
        """Add stable blank-line boundaries around images rendered here.

        A standalone newline-only Plain component may be discarded by message
        adapters. The zero-width-space marker keeps an image-to-image boundary
        non-empty while the surrounding newlines provide actual visual space.
        """
        boundary_marker = "\n\u200b\n"

        def is_boundary_placeholder(component) -> bool:
            return isinstance(component, Comp.Plain) and (
                not component.text or component.text.strip(" \t\r\n") == "\u200b"
            )

        separated_chain = list(chain)
        image_index = 0
        while image_index < len(separated_chain):
            image = separated_chain[image_index]
            if id(image) not in rendered_image_ids:
                image_index += 1
                continue

            # Markdown cleanup can turn marker-only Plain components into empty
            # placeholders. Look through them for real content, then put the
            # boundary in the placeholder closest to the table image.
            previous_index = image_index - 1
            while previous_index >= 0 and is_boundary_placeholder(
                separated_chain[previous_index]
            ):
                previous_index -= 1

            if previous_index >= 0:
                if previous_index < image_index - 1:
                    separated_chain[image_index - 1].text = boundary_marker
                else:
                    previous = separated_chain[previous_index]
                    if isinstance(previous, Comp.Plain):
                        if previous.text:
                            previous.text = previous.text.rstrip("\n") + "\n\n"
                    else:
                        separated_chain.insert(image_index, Comp.Plain(boundary_marker))
                        image_index += 1

            next_index = image_index + 1
            while next_index < len(separated_chain) and is_boundary_placeholder(
                separated_chain[next_index]
            ):
                next_index += 1

            if next_index < len(separated_chain):
                if next_index > image_index + 1:
                    separated_chain[image_index + 1].text = boundary_marker
                else:
                    following = separated_chain[next_index]
                    if isinstance(following, Comp.Plain):
                        if following.text:
                            following.text = "\n\n" + following.text.lstrip("\n")
                    else:
                        separated_chain.insert(
                            image_index + 1, Comp.Plain(boundary_marker)
                        )

            image_index += 1

        return separated_chain

    @staticmethod
    def _separate_rendered_table_images(chain, rendered_image_ids: set[int]):
        """Backward-compatible alias for callers/tests from v0.2.6."""
        return MarkdownKillerPlugin._separate_rendered_images(chain, rendered_image_ids)

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

    def _log_cleaned_text(
        self, original_text: str, cleaned_text: str, source: str = ""
    ):
        """输出 Markdown 清理日志，source 用于区分全局过滤等来源。"""
        original_preview = original_text[:50].replace("\n", "\\n")
        cleaned_preview = cleaned_text[:50].replace("\n", "\\n")
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

        Solution: Remove newlines right after segment punctuation, except when
        either side of the newline belongs to a Markdown list block. This
        includes explicit marker lines and indented continuation/wrapped lines.
        """
        parts = re.split(r"(\n+)", text)
        if len(parts) == 1:
            return text.strip()

        lines = parts[::2]
        list_context_lines: list[bool] = []
        active_list_indent: int | None = None
        for line in lines:
            is_list_context, active_list_indent = _is_list_context_line(
                line, active_list_indent
            )
            list_context_lines.append(is_list_context)

        result: list[str] = [parts[0]]
        for index in range(1, len(parts), 2):
            newlines = parts[index]
            next_text = parts[index + 1] if index + 1 < len(parts) else ""
            prev_line = result[-1].split("\n")[-1] if result else ""
            line_index = index // 2
            prev_is_list_context = list_context_lines[line_index]
            next_is_list_context = (
                list_context_lines[line_index + 1]
                if line_index + 1 < len(list_context_lines)
                else False
            )

            if (
                prev_line.rstrip().endswith(("。", "？", "！", "~", "…"))
                and not prev_is_list_context
                and not next_is_list_context
            ):
                result.append(next_text)
            else:
                result.append(newlines)
                result.append(next_text)

        return "".join(result).strip()

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
        """保留列表标记与换行，仅清理列表项内容中的行内 Markdown 格式。

        实际逻辑已迁移至 ``utils/list_processor.py`` (纯标准库、可在测试中独立
        导入)，此处仅作薄包装以保留插件实例上的公共方法名 (向后兼容)。

        ``list_merge_char_threshold`` 配置项已废弃，仅为旧配置兼容继续传入。

        行为详见 ``utils.list_processor.remove_list_markers`` 的 docstring：
        - 无序列表 [-*+]: 保留原标记形状与每项换行，如 `- 项目`。
        - 有序列表 [N. / N)]: 保留原编号与分隔符，如 `1. 短内容`。
        - 不再按长短合并列表项，也不再移除列表标记/编号。
        - 函数幂等：再次输入输出文本不会进一步改变。
        """
        return _remove_list_markers_impl(
            text, merge_threshold=self.list_merge_char_threshold
        )

    def remove_markdown(self, text: str) -> str:
        """移除文本中的 Markdown 格式。

        Markdown 表格语法会被保留并交由 on_decorating_result 阶段渲染为图片。
        为保证后续 ``detect_markdown_tables`` 能正确识别（要求表格 header 行
        必须位于行首），文本块与表格块之间必须以换行分隔。

        旧的 ``"".join(out_parts)`` 实现会把表格块直接拼到上一个文本块尾部
        （文本块的尾部换行已被 ``_remove_markdown_no_tables`` 清理），导致
        ``| 功能 |`` 紧贴前文，表格不再被检测到（静默失败）。此处改为智能
        拼接：保证每个表格块均以行首开始、以换行结束。
        """
        blocks = self._split_table_blocks(text)
        result = ""
        for block, is_table in blocks:
            if is_table:
                cleaned = "\n".join(ln.rstrip() for ln in block.split("\n"))
                # Guarantee the table starts on its own line.
                if result and not result.endswith("\n"):
                    result += "\n"
                result += cleaned
                # Guarantee a trailing newline so the next text doesn't glue on.
                if not cleaned.endswith("\n"):
                    result += "\n"
            else:
                result += self._remove_markdown_no_tables(block)
        return result

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

        # 保留列表标记与列表项换行，仅清理列表项内容中的行内 Markdown 格式
        text = self._remove_list_markers(text)

        # Remove extra newlines if enabled
        if self.remove_extra_newlines:
            if self.newline_mode == "global":
                text = self._remove_extra_newlines_global(text)
            else:
                text = self._remove_extra_newlines_segment_boundary(text)

        return text
