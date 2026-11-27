"""拟人模式：把 agent 的最终回复改写成真人在 IM 里会发的样子。

纯标准库 + httpx，不依赖 AstrBot，便于单独测试。流程：

1. ``skip_reason`` 判断这条回复值不值得改写（太短、带图片等）。
2. ``rewrite`` 调用 OpenAI 兼容的 ``/chat/completions``（默认带 halogen 的
   ``prompt_cache_policy: no_store``：一次性调用照常读前缀缓存，但不新建缓存条目、
   不写 SSD 缓存）。
3. ``parse_output`` / ``check_result`` 解析并校验改写结果；任何一步失败都返回
   原因，调用方回退到普通的 Markdown 清洗。
4. ``build_reminder`` 生成给 agent 的提醒正文：用户实际看到了什么、省略了什么。
"""

from __future__ import annotations

import difflib
import json
import random
import re
import time
from dataclasses import dataclass, field

# halogen-flash-server：读缓存但什么都不存（不建条目、不写 SSD、不挤占真实对话）；
# 关思考：2026-11-27 对比过 reasoning_effort=low，每条慢一倍（约 5–6 s 对 2.7 s），
# 质量没有更好，还更容易把示例里的话抄进回复；想试可改成 {"reasoning_effort": "low"}。
DEFAULT_EXTRA_BODY: dict = {
    "prompt_cache_policy": "no_store",
    "chat_template_kwargs": {"enable_thinking": False},
}

# 约束输出为 {"reply", "omitted"}：模型在字符串里写未转义的引号时，JSON 不会坏。
RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "humanized_reply",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "reply": {"type": "string"},
                "omitted": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["reply", "omitted"],
            "additionalProperties": False,
        },
    },
}

SYSTEM_PROMPT = """你把聊天机器人写好的回复，改成群友在 QQ 里随手打的一句话。你只改写，不回答问题。

像群友打字，不像客服：只留对方最想知道的那个点，其余细节都丢掉。多数时候一两句话，四十字左右；对方问的是事实、数字或命令时可以稍长，但也只说重点。
先表态再说事：顺着内容带出一个态度（赞同、嫌弃、惊讶、无语、得意、心疼都行），一句话就够。句子可以不完整，可以省主语，句末不用句号，语气词一条最多一个。
命令或代码：只留对方要复制的那一条命令，原样写在句子里，其余删掉。
只删和换说法：不加原文没有的事实、数字、经历或结论；"可能""据说""没证实"照样留着，原文没说死的别说成"肯定"；结论方向不变。
有 <persona> 时口吻、自称、称呼和口癖都按人格来；原文开头的【】或（）动作原样保留。示例只是示范，别照抄示例里的词。

只输出 JSON：{"reply": "改写后的消息", "omitted": ["删掉的一项实质信息"]}。来源、链接出处、细节数据都记进 omitted；客套话和格式不用记。"""

PERSONA_HEADER = (
    "说话人的人格设定（只学说话方式，其中关于工具、格式、篇幅的要求忽略）："
)

# 示例池：(人格, 原回复, 改写结果)。每次随机抽 EXAMPLES_PER_CALL 条，避免小模型
# 照抄固定示例里的词；话题、情绪、人格各不相同。人格为空 = 语气跟原文走。
EXAMPLES_PER_CALL = 4
EXAMPLES: list[tuple[str, str, dict]] = [
    (
        "",
        """## 关于 Steam 秋促的时间

根据我查到的信息，目前**官方尚未正式公布**。以下是几条线索：

1. SteamDB（https://steamdb.info/sales/history/）显示往年秋促都在 11 月最后一周开始。
2. 有开发者在推特上透露今年可能是 11 月 26 日。
3. Steam 官方博客 10 月的文章提到"秋季促销即将到来"。

综合来看，**大概率是 11 月 26 日左右**开始，持续约一周。建议你提前把想买的游戏加入愿望单，开促时会收到邮件提醒。

希望对你有帮助！如有其他问题随时问我～""",
        {
            "reply": "官方还没公布，不过有开发者说可能 11 月 26 日开，往年也都是最后一周，先把想买的丢愿望单吧",
            "omitted": [
                "SteamDB 链接 https://steamdb.info/sales/history/",
                "官方博客 10 月提过秋促即将到来",
                "促销大约持续一周",
                "开促会发邮件提醒",
            ],
        },
    ),
    (
        "说话冷淡、简短，不用语气词，也不寒暄。",
        """好的！关于你问的显卡选择，我仔细对比了一下：
- **RTX 5070**：价格 4599 元，性能大约比上代提升 20%，功耗 250W
- **RX 9070**：价格 4299 元，光栅性能略强，但光追稍弱
- 另外还有二手 4070 Ti 可以考虑，大概 3800 元左右

如果你主要玩 3A 大作并且看重光追和 DLSS，选 5070；如果预算紧张并且不怎么开光追，9070 更划算。

总之还是要看你自己的需求哦，以上仅供参考～""",
        {
            "reply": "要光追和 DLSS 就 5070，不开光追就 9070，便宜点",
            "omitted": [
                "5070 售价 4599 元、比上代强约 20%、功耗 250W",
                "9070 售价 4299 元、光栅略强",
                "二手 4070 Ti 约 3800 元也可以考虑",
            ],
        },
    ),
    (
        "温柔的大姐姐，说话软软的，句尾爱带“呀”“哦”。",
        """您好！关于路由器恢复出厂设置，具体步骤如下：

**第一步**：找到路由器背面标有 RESET 的小孔。
**第二步**：在通电状态下，用牙签或卡针按住小孔约 10 秒。
**第三步**：等待所有指示灯同时闪烁后松开，路由器会自动重启。
**第四步**：重启完成后，在浏览器访问 192.168.1.1，用背面标签上的默认账号密码登录并重新设置。

⚠️ 注意：恢复出厂设置会清除所有配置，包括 Wi-Fi 名称和密码，请提前做好记录。""",
        {
            "reply": "通着电拿卡针戳住背后的 RESET 孔 10 秒，灯一起闪就好啦，再进 192.168.1.1 重新设一下哦，Wi-Fi 密码会清掉的",
            "omitted": ["用背面标签上的默认账号密码登录"],
        },
    ),
    (
        "开头用【】写一个小动作或情绪，说话干脆。",
        """【眨眼】查到了，说明书上写的就是 20000 mAh，你记的没错。

根据官网参数页，2024 新款的电芯容量与老款相同，5V 输出时的额定容量为 12000 mAh。新款的改进集中在两方面：其一是快充协议升级，支持 PD 65W；其二是外壳换成了铝合金。该系列共有 10000、20000、30000 三档。

综上，你此前按 20000 估算的"手机能充 4 次左右"是成立的；我之前提到的"新款容量变小"属于误判，在此更正。以上信息来自官网产品页（example.com/power-bank），并与两家评测的数据交叉核对过。""",
        {
            "reply": "【眨眼】就是 20000 mAh，你记的没错，新款容量没变，之前说变小是我搞错了",
            "omitted": [
                "5V 输出时额定容量 12000 mAh",
                "新款支持 PD 65W、换了铝合金外壳",
                "全系 10000 / 20000 / 30000 三档",
                "按 20000 算能充 4 次左右",
                "来源：官网产品页 example.com/power-bank，两家评测数字一致",
            ],
        },
    ),
    (
        "",
        """如果你想撤销最近一次 commit，可以根据情况选择以下方法：

**情况一：还没有 push**
```bash
git reset --soft HEAD~1
```
这会撤销 commit，但保留你的改动在暂存区。

**情况二：已经 push 到远程**
```bash
git revert HEAD
```
这会创建一个新的 commit 来抵消上一次的修改，不会改写历史，适合多人协作的分支。

需要注意的是，`git reset --hard` 会直接丢弃改动，请谨慎使用。""",
        {
            "reply": "没推的话 git reset --soft HEAD~1，改动还在；推了就老实 git revert HEAD",
            "omitted": [
                "revert 会新建一个抵消用的 commit、不改写历史",
                "git reset --hard 会丢弃改动",
            ],
        },
    ),
    (
        "",
        """这个报错的原因是你当前使用的 Python 版本是 3.8，而该库从 2.0 版本开始要求 Python 3.10 及以上。

解决方法有两种：
1. 使用 pyenv 或 uv 安装 Python 3.10 及以上版本（推荐）；
2. 将该库降级到 1.x 版本，例如 `pip install "somelib<2"`。

另外，建议你为每个项目使用独立的虚拟环境，避免依赖冲突。""",
        {
            "reply": '3.8 太老了，这库 2.0 起要 3.10，拿 uv 装个新的就好，懒得升就 pip install "somelib<2"',
            "omitted": ["pyenv 也能装新版本", "建议每个项目用独立虚拟环境"],
        },
    ),
    (
        "",
        """哈哈，确实挺离谱的！根据你的描述，你玩了 6 个小时的存档因为游戏崩溃而丢失了。

这种情况通常是因为游戏在写入存档时崩溃，导致存档文件损坏。你可以尝试以下方法：
1. 检查游戏目录下是否有 .bak 备份文件；
2. 查看 Steam 云存档是否有更早的版本。

以后建议开启自动存档，并定期手动备份存档文件。真的很心疼你的 6 个小时 😭""",
        {
            "reply": "6 个小时啊…心疼，去存档目录翻翻有没有 .bak，Steam 云存档也看一眼",
            "omitted": ["崩溃时正在写存档导致文件损坏", "以后开自动存档、定期手动备份"],
        },
    ),
]


_URL = re.compile(r"https?://[^\s<>()\[\]{}\"'，。！？、；：）】」』]+")
_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.DOTALL)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# 情绪一上头容易把"可能"改成"肯定"：原文没有这些词，改写里就不许出现。
_CERTAIN_WORDS = ("肯定", "一定", "绝对", "百分百", "必然", "铁定")
_TOOL_SYNTAX = re.compile(r"<tool_call>|<function[=>]|<parameter[=>]|\[TOOL_CALLS\]")


@dataclass
class HumanizeConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    extra_body: dict = field(default_factory=lambda: dict(DEFAULT_EXTRA_BODY))
    temperature: float | None = 0.7
    # 流式读取：只要还在吐 token（正文或思考）就一直等；连续这么久一个 token
    # 都没有（排队、prefill 也算）才放弃。没有总时长上限。
    idle_timeout: float = 60.0
    max_tokens: int = 4096
    json_schema: bool = True
    min_chars: int = 80

    @property
    def ready(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())

    @property
    def endpoint(self) -> str:
        base = self.base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


@dataclass
class HumanizeResult:
    reply: str
    omitted: list[str]
    elapsed: float = 0.0


def parse_extra_body(raw) -> dict:
    """配置里的额外请求体：dict 或 JSON 字符串；空字符串 = 不加任何字段。"""
    if isinstance(raw, dict):
        return dict(raw)
    if raw is None:
        return dict(DEFAULT_EXTRA_BODY)
    text = str(raw).strip()
    if not text:
        return {}
    value = json.loads(text)
    if not isinstance(value, dict):
        raise TypeError("额外请求体必须是 JSON 对象")
    return value


def skip_reason(text: str, min_chars: int) -> str | None:
    """不值得改写时返回原因（调用方回退到普通清洗），否则 None。"""
    stripped = text.strip()
    if not stripped:
        return "空回复"
    if len(stripped) < min_chars:
        return f"短于 {min_chars} 字"
    return None


def _wrap(text: str, persona: str = "") -> str:
    head = ""
    if persona.strip():
        head = f"{PERSONA_HEADER}\n<persona>\n{persona.strip()}\n</persona>\n\n"
    return f"{head}<reply>\n{text.strip()}\n</reply>"


def pick_examples(rng: random.Random | None = None) -> list[tuple[str, str, dict]]:
    """每次调用随机抽几条示例，顺序也打乱。"""
    rng = rng or random
    k = min(EXAMPLES_PER_CALL, len(EXAMPLES))
    return rng.sample(EXAMPLES, k)


def build_messages(
    text: str, persona: str = "", examples: list | None = None
) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for example_persona, source, output in (
        pick_examples() if examples is None else examples
    ):
        messages.append({"role": "user", "content": _wrap(source, example_persona)})
        messages.append(
            {"role": "assistant", "content": json.dumps(output, ensure_ascii=False)}
        )
    messages.append({"role": "user", "content": _wrap(text, persona)})
    return messages


def build_payload(text: str, cfg: HumanizeConfig, persona: str = "") -> dict:
    payload: dict = {
        "model": cfg.model.strip(),
        "messages": build_messages(text, persona),
        "max_tokens": cfg.max_tokens,
        "stream": True,
    }
    if cfg.temperature is not None:
        payload["temperature"] = cfg.temperature
    if cfg.json_schema:
        payload["response_format"] = RESPONSE_FORMAT
    payload.update(cfg.extra_body)
    return payload


def parse_output(raw: str) -> HumanizeResult | None:
    """从模型输出里取出 JSON；容忍代码块包裹和前后杂字。"""
    if not raw:
        return None
    text = _JSON_FENCE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        data = _salvage(text[start : end + 1])
        if data is None:
            return None
    if not isinstance(data, dict):
        return None
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return None
    omitted_raw = data.get("omitted") or []
    if isinstance(omitted_raw, str):
        omitted_raw = [omitted_raw]
    omitted = [
        str(item).strip()
        for item in omitted_raw
        if isinstance(item, (str, int, float)) and str(item).strip()
    ]
    return HumanizeResult(reply=reply.strip(), omitted=omitted)


_SALVAGE = re.compile(
    r'^\{\s*"reply"\s*:\s*"(?P<reply>.*)"\s*,\s*"omitted"\s*:\s*\[(?P<omitted>.*)\]\s*\}$',
    re.DOTALL,
)


def _salvage(text: str) -> dict | None:
    """字符串里有未转义引号的 JSON：按固定的两个字段硬拆。"""
    match = _SALVAGE.match(text)
    if not match:
        return None
    reply = match.group("reply").replace("\\n", "\n").replace('\\"', '"')
    items = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group("omitted"))
    return {"reply": reply, "omitted": [i.replace('\\"', '"') for i in items]}


def _urls(text: str) -> set[str]:
    return {u.rstrip(".,;:!?") for u in _URL.findall(text)}


def check_result(original: str, result: HumanizeResult) -> str | None:
    """改写结果不可信时返回原因。"""
    if len(result.reply) > len(original.strip()) * 1.1 + 20:
        return "改写后反而更长"
    if _TOOL_SYNTAX.search(result.reply):
        return "输出里有工具调用语法"
    invented = _urls(result.reply) - _urls(original)
    if invented:
        return f"出现原文没有的链接: {min(invented)}"
    for word in _CERTAIN_WORDS:
        if word in result.reply and word not in original:
            return f"把话说死了: {word}"
    # 链接里的数字已在上面比对过；其余数字只能删，不能凭空出现或被改掉。
    numbers = set(_NUMBER.findall(_URL.sub(" ", result.reply)))
    new_numbers = numbers - set(_NUMBER.findall(original))
    if new_numbers:
        return f"出现原文没有的数字: {min(new_numbers)}"
    return None


_IGNORED_FOR_DIFF = re.compile(r"[\s*#_`>~|-]+")


def is_unchanged(original: str, result: HumanizeResult) -> bool:
    """模型基本原样返回（只去了格式）：不算一次简化，也不该提醒 agent。"""
    if result.omitted:
        return False
    a = _IGNORED_FOR_DIFF.sub("", original)
    b = _IGNORED_FOR_DIFF.sub("", result.reply)
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.95


def normalize_reply(text: str) -> str:
    """段落之间只留一个空行，去掉行尾空白。"""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


class IdleTimeout(TimeoutError):
    """模型连续 idle_timeout 秒没有吐出任何 token。"""


def _delta_text(chunk: dict) -> tuple[str, bool]:
    """一个 SSE 块里的正文增量，以及它是否带了任何 token（含思考）。"""
    content: list[str] = []
    produced = False
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or choice.get("message") or {}
        piece = delta.get("content")
        if piece:
            content.append(piece)
            produced = True
        if delta.get("reasoning_content") or delta.get("reasoning"):
            produced = True
    return "".join(content), produced


async def _stream_content(client, cfg: HumanizeConfig, payload: dict, headers, timeout):
    parts: list[str] = []
    last_token = time.monotonic()
    async with client.stream(
        "POST", cfg.endpoint, json=payload, headers=headers, timeout=timeout
    ) as resp:
        if resp.status_code >= 400:
            body = (await resp.aread()).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {resp.status_code}: {body[:200]}")
        async for line in resp.aiter_lines():
            now = time.monotonic()
            produced = False
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    chunk = None
                if isinstance(chunk, dict):
                    if chunk.get("error"):
                        raise RuntimeError(f"服务端错误: {str(chunk['error'])[:200]}")
                    piece, produced = _delta_text(chunk)
                    if piece:
                        parts.append(piece)
            if produced:
                last_token = now
            elif now - last_token > cfg.idle_timeout:
                # 只有心跳之类的空行在来，模型本身没有吐字。
                raise IdleTimeout(f"{cfg.idle_timeout:.0f} 秒没有吐字")
    return "".join(parts)


async def rewrite(
    text: str, cfg: HumanizeConfig, persona: str = "", client=None
) -> HumanizeResult:
    """流式调用模型改写；失败抛异常（由调用方回退）。

    ``client``: 可选的 ``httpx.AsyncClient``（测试注入）。
    """
    import httpx

    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if cfg.api_key.strip():
        headers["Authorization"] = f"Bearer {cfg.api_key.strip()}"
    payload = build_payload(text, cfg, persona)
    # read = 两次收到数据之间最多等多久：连接上了但模型一直不吐字，也会在这里断开。
    timeout = httpx.Timeout(connect=15.0, read=cfg.idle_timeout, write=30.0, pool=15.0)
    start = time.perf_counter()
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as own:
                content = await _stream_content(own, cfg, payload, headers, timeout)
        else:
            content = await _stream_content(client, cfg, payload, headers, timeout)
    except httpx.ReadTimeout as e:
        raise IdleTimeout(f"{cfg.idle_timeout:.0f} 秒没有吐字") from e
    result = parse_output(content)
    if result is None:
        raise RuntimeError(f"输出不是约定的 JSON: {content[:200]!r}")
    result.elapsed = time.perf_counter() - start
    return result


def persona_from_anchors(anchors: dict, max_chars: int = 3000) -> str:
    """从 ProviderRequest.context_anchors 取出本次生效的人格提示词和示例对话。"""
    if not isinstance(anchors, dict):
        return ""
    parts: list[str] = []
    prompt = anchors.get("persona")
    if isinstance(prompt, str) and prompt.strip():
        parts.append(prompt.replace("# Persona Instructions", "", 1).strip())
    dialogs = anchors.get("persona_examples")
    if isinstance(dialogs, str) and dialogs.strip():
        parts.append(dialogs.strip())
    text = "\n\n".join(p for p in parts if p)
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n……（人格设定过长，已截断）"
    return text


def build_reminder(sent_text: str, omitted: list[str], when: str) -> str:
    """给 agent 的提醒：随下一次请求以 user 身份带入，不单独触发模型。"""
    lines = [
        (
            f"你在 {when} 的那条回复，发到聊天前被 Markdown 杀手的拟人模式"
            "改写成了更短的口语版。用户实际只看到了下面这段："
        ),
        "<sent>",
        sent_text,
        "</sent>",
    ]
    if omitted:
        lines.append("被省略、用户没有看到的内容：")
        lines.extend(f"- {item}" for item in omitted)
    else:
        lines.append("没有省略实质内容，只是改了说法和格式。")
    lines.append(
        "用户追问时可以直接补充这些内容，不要以为用户看过你的完整原文。"
        "这是系统提醒，不是用户说的话，不需要回应它。"
    )
    return "\n".join(lines)
