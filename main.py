from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.api import logger, AstrBotConfig
import re


@register(
    "astrbot_plugin_markdown_killer",
    "xkeyC",
    "移除输出中的Markdown格式（保留换行处理与全局控制开关）",
    "0.1.1",
    "https://github.com/xkeyC/astrbot_plugin_markdown_killer",
)
class MarkdownKillerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.remove_extra_newlines = self._config_get("remove_extra_newlines", True)
        self.newline_mode = self._config_get("newline_mode", "segment_boundary")

    def _config_get(self, key: str, default=None):
        """兼容 AstrBotConfig 与普通 dict 的配置读取。"""
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse, *args):
        """
        监听LLM回复，移除Markdown格式
        """
        if not resp or not resp.completion_text:
            return

        original_text = resp.completion_text

        # 调试日志：显示收到的原始文本，以便确认 LLM 是否输出了 Markdown
        # original_preview_debug = original_text[:50].replace('\n', '\\n')
        # logger.info(f"[Markdown Killer] 收到 LLM 回复 (前50字符): {original_preview_debug}...")

        cleaned_text = self.remove_markdown(original_text)

        if original_text != cleaned_text:
            resp.completion_text = cleaned_text
            self._log_cleaned_text(original_text, cleaned_text)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        监听所有即将发送出的消息，当开关开启时，进行全局 Markdown 移除。
        """
        if not self._config_get("enable_global_markdown_killer", False):
            return

        result = event.get_result()
        if not result or not hasattr(result, "chain") or not result.chain:
            return

        for comp in result.chain:
            # 只对具有 text 属性的消息段进行处理（例如 Plain 或 TextPart 等）
            text = getattr(comp, "text", None)
            if not isinstance(text, str):
                continue

            cleaned_text = self.remove_markdown(text)
            if cleaned_text != text:
                comp.text = cleaned_text
                self._log_cleaned_text(text, cleaned_text, source="[全局过滤]")

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

    def remove_markdown(self, text: str) -> str:
        """
        移除文本中的Markdown格式
        """
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

        # 移除列表标记 (移除行首的 -, *, +)
        text = re.sub(r"^\s*[-*+]\s+(.*)", r"\1", text, flags=re.MULTILINE)

        # Remove extra newlines if enabled
        if self.remove_extra_newlines:
            if self.newline_mode == "global":
                text = self._remove_extra_newlines_global(text)
            else:
                text = self._remove_extra_newlines_segment_boundary(text)

        return text
