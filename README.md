# AstrBot插件：Markdown杀手

## 简介
这是一个 AstrBot 插件，用于移除聊天输出中的 Markdown 格式。不仅能够自动处理 LLM 生成的内容，也支持开启“全局 Markdown 移除”以清理所有即将发送的话语，并可将 Markdown 表格及 LaTeX 公式渲染为图片后嵌入消息。

## 功能
- 自动检测并移除 LLM 回复中的 Markdown 格式（保留纯文本内容）。
- **全局 Markdown 移除**: 可拦截并清理所有即将发送的消息链中的 Markdown（对非 LLM 生成的消息也生效）。
- 在控制台输出移除 Markdown 的日志提醒，并区分独立来源（如 `[全局过滤]`）以便排查。
- **保留 fork 优化**: 支持移除多余换行，避免分段发送时出现过多空行。
- **公式图片渲染**：块级公式独立渲染；含行内公式的整行合并渲染，避免文字与小图片错位。支持 `\[...\]`、`$$...$$`、`\(...\)` 和 `$...$`。
- **稳定图片留白**：表格/公式截图自带上下留白，同时在消息链中加入不会被适配器丢弃的边界标记。
- **全面优化的匹配算法**:
  - 智能识别数学公式，避免误删 `3 * 4 = 12`、`3*4*5` 等表达式中的星号。
  - 精确处理代码级别变量，杜绝误伤 `this_is_a_var` 里的下划线。
  - 深度支持格式化嵌套排版（如加粗内含斜体符号、嵌套的多层级引用等）。
  - 全面涵盖多类 Markdown 语法（代码块、行内代码、标题、独立链接与图片、引用、无序/有序列表、删除线等）。

## 新版特性 (0.3.0)
- **新增 LaTeX 公式图片链路**：使用本地 `latex2mathml` 转换为 MathML，再由 Playwright Chromium 截图，不依赖在线 KaTeX/MathJax CDN。
- **优化图片换行间距**：截图包含物理留白；相邻图片使用带零宽字符的换行边界，避免纯换行组件被消息适配器忽略。
- **新增配置项**：`enable_formula_render` 与 `formula_render_fallback`。

### 0.3.1 修复
- 图片与前后文字之间只保留一个换行，移除消息链边界额外产生的空行。

## 新版特性 (0.2.5)
- **列表项处理改进**: 保留原始 Markdown 列表标记/编号与列表项换行，不再将列表项合并到一行；仅清理列表项内容中的行内 Markdown 格式。
  - 无序列表 `- **项目**` → `- 项目`（保留 `-`，`*` / `+` 同理）。
  - 有序列表 `1. **短内容**\n2. 中等内容` → `1. 短内容\n2. 中等内容`（保留原编号与 `.` / `)` 分隔符）。
  - `list_merge_char_threshold` 仅为兼容旧配置保留，当前版本不再使用它合并列表。

## 新版特性 (0.2.0)
- **新增表格图片渲染**: 使用 Playwright 将 Markdown 表格渲染为图片后嵌入消息链。
  - 渲染失败时支持三种回退策略：`text`（转为纯文本）、`raw`（保留原始 Markdown）、`remove`（丢弃）。
  - 渲染在 `on_decorating_result` 阶段执行，发生于分段发送之前，因此表格图片不会被分段拆散。
- **新增配置项**:
  - `enable_table_render`（默认 `true`）：是否启用表格图片渲染。
  - `table_render_fallback`（默认 `text`）：表格渲染失败时的回退策略。
- **新增依赖**: `playwright`（首次启用图片渲染时，插件会自动执行 `python -m playwright install chromium` 安装浏览器）。

> 表格图片渲染的灵感与参考来源：[astrbot_plugin_biliVideo](https://github.com/xkeyC/astrbot_plugin_biliVideo) / [astrbot_plugin_bangumi](https://github.com/xkeyC/astrbot_plugin_bangumi)。

## 注意事项
- 插件会尝试智能区分 Markdown 斜体和数学公式，但在极少数复杂边缘情况下可能会有误判。
- 代码块的语言标识符（如 `python`）会被移除，但如果标识符后紧跟内容且无空格（如 ` ```json{...}``` `），可能会保留标识符以避免误删代码内容。
- 表格及公式图片渲染需要 `playwright`、`latex2mathml` 与 Chromium 浏览器。插件首次启用时会尝试自动安装 Chromium；若安装失败，将自动降级到回退策略，不影响其它功能。
- 流式输出（STREAMING_FINISH）场景下，表格与公式 Markdown 会原样流式输出给用户，插件不会再将其渲染为图片（修复流式图片替换需要更底层的改动，超出本期范围）。

## 安装
1. 将本插件目录放置在 AstrBot 的 `data/plugins` 目录下。
2. 确保 `metadata.yaml` 配置正确。
3. 插件依赖 `playwright` 与 `latex2mathml`；如未安装，可执行 `pip install -r requirements.txt`。
4. 重启 AstrBot 或重载插件。

## 配置
在 AstrBot 的 WebUI 插件管理面板中支持配置以下项：

- **全局 Markdown 移除 (`enable_global_markdown_killer`)**：布尔开关，默认为关闭。开启后，将从所有即将发送的最终文本消息中严格移除 Markdown 格式（不论该内容最初由谁产生）。
- **移除多余的换行符 (`remove_extra_newlines`)**：布尔开关，默认为开启。启用后，将移除文本中多余的换行，避免分段发送时出现过多空行。
- **换行处理模式 (`newline_mode`)**：默认为 `segment_boundary`。可选 `segment_boundary`（只移除分段标点后的换行，推荐）或 `global`（全局压缩连续空行）。
- **列表项合并字数阈值 (`list_merge_char_threshold`)**：已废弃，仅为兼容旧配置保留；当前版本始终保留列表标记/编号与列表项换行。
- **启用表格图片渲染 (`enable_table_render`)**：布尔开关，默认为开启。开启后，LLM 回复中的 Markdown 表格将被 Playwright 渲染为图片并嵌入消息链。需要安装 `playwright` 与 Chromium（首次启用时插件会尝试自动安装）。
- **表格渲染失败回退策略 (`table_render_fallback`)**：默认为 `text`。可选 `text`（转为纯文本，去分隔行）、`raw`（保留原始 Markdown 表格文本）、`remove`（直接丢弃表格内容）。
- **启用公式图片渲染 (`enable_formula_render`)**：默认为开启。块级公式独立成图，包含行内公式的完整物理行合并成图。
- **公式渲染失败回退策略 (`formula_render_fallback`)**：默认为 `raw`。可选 `raw`（保留原始公式）、`text`（去定界符保留内容）或 `remove`（丢弃）。

## 作者
xkeyC（fork 维护）

原始项目作者：AlanBacker
