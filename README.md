# AstrBot插件：Markdown杀手

## 简介
这是一个 AstrBot 插件，用于移除聊天输出中的 Markdown 格式。不仅能够自动处理 LLM 生成的内容，也支持开启“全局 Markdown 移除”以清理所有即将发送的话语，并可将 Markdown 表格及 LaTeX 公式渲染为图片后嵌入消息。

## 功能
- 自动检测并移除 LLM 回复中的 Markdown 格式（保留纯文本内容）。
- **全局 Markdown 移除**: 可拦截并清理所有即将发送的消息链中的 Markdown（对非 LLM 生成的消息也生效）。
- 在控制台输出移除 Markdown 的日志提醒，并区分独立来源（如 `[全局过滤]`）以便排查。
- **空行压缩**: 保留模型输出的换行与段落空行，仅把连续多个空行压缩为一个。
- **公式图片渲染**：消息只要包含公式，就会将完整 Markdown（标题、段落、列表、引用、代码、表格及行内样式）与公式一起渲染为单张图片，避免正文重复或排版丢失。支持 `\[...\]`、`$$...$$`、`\(...\)` 和 `$...$`。
- **稳定图片留白**：表格/公式截图自带上下留白，同时在消息链中加入不会被适配器丢弃的边界标记。
- **全面优化的匹配算法**:
  - 智能识别数学公式，避免误删 `3 * 4 = 12`、`3*4*5` 等表达式中的星号。
  - 精确处理代码级别变量，杜绝误伤 `this_is_a_var` 里的下划线。
  - 深度支持格式化嵌套排版（如加粗内含斜体符号、嵌套的多层级引用等）。
  - 全面涵盖多类 Markdown 语法（代码块、行内代码、标题、链接（转为 `描述文本( url )`，保留地址）与图片、引用、无序/有序列表、删除线等）。

## 0.5.3 修复
- 改写改用流式接口，不再设总超时：只要模型还在吐 token（正文或思考）就一直等；连续 `humanize_idle_timeout`（默认 60 秒）没有任何输出才放弃，回退到普通清洗（排队和 prefill 也算在没输出的时间里）。旧的 `humanize_timeout` 不再使用。起因：本地模型和主对话共用 GPU，主对话的 10 万 token 请求被挤出 KV 缓存后重新 prefill，同时进来的改写请求 20 秒里只出了 8 个 token 就被插件断开（如果继续等，约 31 秒能完成），结果白等一场，还是发了未压缩的长消息。
- 改写失败的日志带上异常类型（httpx 超时异常的文本是空的，以前只显示"改写失败: "）。

## 0.5.2 修复
- 拟人模式改在 `on_llm_response` 里改写（以前在 `on_decorating_result`）。异步工具任务完成、后台命令结束、迟到图片、定时任务这些 wake 回合，由 Codex 直接 `send_message` 发出，不经过装饰阶段，以前完全没被改写，连 Markdown 都没清。现在两条路径都会改写：有 `result_chain` 就原地改它，否则改 `completion_text`。
- 错误回复（role 不是 assistant）不改写；流式会话按 `provider_settings.streaming_response` 判断，平台不支持流式且策略为 `turn_off` 时按非流式处理。

## 0.5.1 修复
- 模型原样返回（只去了格式、没省略任何内容）不再算一次简化：按普通清洗发出，也不给 agent 留提醒，日志写"模型认为无需简化"。
- 示例改为四组都需要删减的回复，去掉了"原样输出"的示例（短于 80 字的回复本来就不进改写，那条示例只会教模型照抄），新增一组无 Markdown 但堆满来源和参数的回复；猫娘示例换成没有固定口癖的人格，避免"喵"串到别的回复。
- 请求带 `response_format` json_schema 约束输出；解析时容忍字符串里未转义的引号。
- 仍默认关闭思考：实测 `reasoning_effort: low` 每条慢一倍（约 5–6 秒对 2.7 秒），质量没有更好，还更容易抄示例里的句子；需要时可在 `humanize_extra_body` 里改。

## 新版特性 (0.5.0)：拟人模式
- **独立开关 `enable_humanize`**（默认关）。开启后，LLM 的最终回复不再按规则清洗 Markdown，而是交给一个便宜的模型改写成真人发消息的样子：直接给结论、口语、1 到 3 段、不用 Markdown、不罗列线索和来源。只简化最终发到 IM 的内容，不改 agent 自己的对话记录。
- **示例为主、遵守人格**：改写 prompt 只有几句说明，主要靠 4 组示例（罗列线索的调研回复、带人格的选购对比、步骤教程、本来就口语的短句）教模型怎么说。`humanize_use_persona`（默认开）会把本会话当前生效的人格提示词和示例对话（AstrBot 在 `on_llm_request` 前解析好的 `persona` / `persona_examples` 锚点）放进改写请求，让改写后的消息沿用人格的自称、称呼、口癖和语气；人格里关于工具、格式、篇幅的要求不采用。人格过长时按 `humanize_persona_max_chars`（默认 3000 字）截断。
- **只删不加**：改写结果要过确定性校验，比原文还长、出现原文没有的链接或数字、带工具调用语法，都判为失败。回复太短（`humanize_min_chars`）、含代码块、消息里有图片、超时或模型出错时，回退到原来的规则清洗，原回复照常发出。
- **调用方式**：直接请求 OpenAI 兼容的 `/chat/completions`（`humanize_base_url` / `humanize_model` / `humanize_api_key`）。`humanize_extra_body` 默认是 `{"prompt_cache_policy": "no_store", "chat_template_kwargs": {"enable_thinking": false}}`：halogen 照常读前缀缓存，但这次一次性调用不新建缓存条目、不写 SSD 缓存，也不会挤掉真实对话的缓存；同时关闭思考。其他服务端不认识这些字段时可以清空。
- **告知 agent（`humanize_notify_agent`，默认开）**：改写后插件记下用户实际看到的内容和被省略的要点，在该会话下一次正常请求时，以 user 身份的 `<context_unit name="reply_simplified">` 带入，并随该轮持久化。这是写入标记型提醒，本身不触发 LLM；steer 进正在运行的 turn 的消息不经过 `on_llm_request`，提醒会留到下一次正常请求。每个会话最多攒 3 条，插件重启会丢掉还没带入的提醒。
- **流式输出**时回复已经原样发出，拟人模式无法改写，只在日志里告警一次。

## 0.4.1 修复
- **不再吞掉换行**：移除 `segment_boundary` 换行模式。该模式会删除 `。？！~…` 之后的换行，原本用于规避 AstrBot 早期分段回复把换行留在下一段开头、显示为空行的问题；AstrBot 现已对每个分段 `strip()`（AstrBotDevs/AstrBot#8304），该模式只剩副作用，会把 DeepSeek 等逐句换行的输出挤成一整段。现在始终保留换行，`remove_extra_newlines` 只压缩连续空行；`newline_mode` 已废弃并被忽略。

## 新版特性 (0.4.0)
- **新增 LLM 工具 `chat_send_markdown_img_card`**：模型可主动把 Markdown（标题、列表、表格、代码块、LaTeX 公式）渲染为一张带可选标题栏的图片卡片并直接发送到当前会话，适合结构化长回答。参数：`markdown`（正文）、`title`（可选标题）。
  - 即使关闭了表格/公式自动渲染，首次调用工具时也会按需准备 Playwright Chromium。
  - 渲染或发送失败时工具返回错误信息，模型可改用纯文本回复。可在 AstrBot WebUI 的函数工具管理中禁用。
- **渲染安全加固**：原始 HTML 不再渲染（仅保留 `<br>`），修复代码块中 `<`/`&` 被双重转义；公式 MathML 按白名单重新序列化；截图浏览器禁用 JavaScript 并拦截所有非 `data:` 网络请求。
- **公式兼容**：`aligned`/`gathered` 多行推导正确排版；表格单元格、列表项中的 `$$...$$` 按行内公式处理，不再打断结构。

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
- 表格及公式图片渲染需要 `playwright`、`latex2mathml`、`Markdown` 与 Chromium 浏览器。插件首次启用时会尝试自动安装 Chromium；若安装失败，将自动降级到回退策略，不影响其它功能。
- 流式输出（STREAMING_FINISH）场景下，表格与公式 Markdown 会原样流式输出给用户，插件不会再将其渲染为图片（修复流式图片替换需要更底层的改动，超出本期范围）。

## 安装
1. 将本插件目录放置在 AstrBot 的 `data/plugins` 目录下。
2. 确保 `metadata.yaml` 配置正确。
3. 插件依赖 `playwright`、`latex2mathml` 与 `Markdown`；如未安装，可执行 `pip install -r requirements.txt`。
4. 重启 AstrBot 或重载插件。

## 配置
在 AstrBot 的 WebUI 插件管理面板中支持配置以下项：

- **全局 Markdown 移除 (`enable_global_markdown_killer`)**：布尔开关，默认为关闭。开启后，将从所有即将发送的最终文本消息中严格移除 Markdown 格式（不论该内容最初由谁产生）。
- **压缩多余的空行 (`remove_extra_newlines`)**：布尔开关，默认为开启。启用后，连续多个空行压缩为一个空行并去掉行尾空白；单个换行与段落空行始终保留。
- **换行处理模式 (`newline_mode`)**：已废弃，仅为兼容旧配置保留，设置值会被忽略。
- **列表项合并字数阈值 (`list_merge_char_threshold`)**：已废弃，仅为兼容旧配置保留；当前版本始终保留列表标记/编号与列表项换行。
- **启用表格图片渲染 (`enable_table_render`)**：布尔开关，默认为开启。开启后，LLM 回复中的 Markdown 表格将被 Playwright 渲染为图片并嵌入消息链。需要安装 `playwright` 与 Chromium（首次启用时插件会尝试自动安装）。
- **表格渲染失败回退策略 (`table_render_fallback`)**：默认为 `text`。可选 `text`（转为纯文本，去分隔行）、`raw`（保留原始 Markdown 表格文本）、`remove`（直接丢弃表格内容）。
- **启用公式图片渲染 (`enable_formula_render`)**：默认为开启。含公式的完整消息会保留 Markdown 格式并统一渲染为单张图片。
- **公式渲染失败回退策略 (`formula_render_fallback`)**：默认为 `raw`。可选 `raw`（保留原始公式）、`text`（去定界符保留内容）或 `remove`（丢弃）。
- **拟人模式 (`enable_humanize`)** 及 `humanize_base_url`、`humanize_api_key`、`humanize_model`、`humanize_extra_body`、`humanize_temperature`（默认 0.7，负数表示不传）、`humanize_timeout`（默认 20 秒）、`humanize_min_chars`（默认 80）、`humanize_use_persona`（默认开）、`humanize_persona_max_chars`（默认 3000）、`humanize_notify_agent`（默认开）：见上文"拟人模式"。

## 作者
xkeyC（fork 维护）

原始项目作者：AlanBacker
