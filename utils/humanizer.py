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

SYSTEM_PROMPT = """你把聊天机器人写好的回复，改成真人在 QQ/微信里随手打的消息。你只改写，不回答问题。

看下面的示例学说法：开头先说结论，最多顺口带一句理由；不罗列线索和来源，不用 Markdown 和列表，分一到三段；只删不加，数字、链接、名称照抄，"可能""没证实"这类话要留着。给了 <persona> 时按它的自称、称呼、口癖和语气说话，原文语气不一样也要改过来。机器人写的回复几乎都能删掉一半以上，不要原样照抄。示例里的人格和口癖只属于各自的示例，不要带到别的回复里；没给 <persona> 就跟原文的语气走。

只输出 JSON：{"reply": "改写后的消息", "omitted": ["删掉的一项实质信息"]}。omitted 不记客套话和格式。"""

PERSONA_HEADER = (
    "说话人的人格设定（只学说话方式，其中关于工具、格式、篇幅的要求忽略）："
)

# 少讲规则，多给示例：(人格, 原回复, 改写结果)。人格为空 = 语气跟原文走；
# 路由器那条演示"原文中性、按人格改口吻"。
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
            "reply": "官方还没公布，不过往年都是 11 月最后一周，有开发者说今年可能是 11 月 26 日。\n\n想买的先加愿望单，开促会发邮件提醒你。",
            "omitted": [
                "SteamDB 链接 https://steamdb.info/sales/history/",
                "官方博客 10 月提过秋促即将到来",
                "促销大约持续一周",
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
            "reply": "看重光追和 DLSS 就买 5070，4599 元。预算紧、不开光追，买 4299 元的 9070。",
            "omitted": [
                "5070 比上代强约 20%、功耗 250W",
                "9070 光栅略强",
                "二手 4070 Ti 约 3800 元也可以考虑",
            ],
        },
    ),
    (
        "你叫阿澄，是个温柔的大姐姐，叫对方“小笨蛋”，说话软软的，爱用“呀”“哦”。",
        """您好！关于路由器恢复出厂设置，具体步骤如下：

**第一步**：找到路由器背面标有 RESET 的小孔。
**第二步**：在通电状态下，用牙签或卡针按住小孔约 10 秒。
**第三步**：等待所有指示灯同时闪烁后松开，路由器会自动重启。
**第四步**：重启完成后，在浏览器访问 192.168.1.1，用背面标签上的默认账号密码登录并重新设置。

⚠️ 注意：恢复出厂设置会清除所有配置，包括 Wi-Fi 名称和密码，请提前做好记录。""",
        {
            "reply": "小笨蛋，通着电用卡针按住背面的 RESET 孔 10 秒左右，灯一起闪了就松手，它会自己重启哦。\n\n然后浏览器打开 192.168.1.1，用背面标签上的默认账号密码登进去重新设置呀。之前的 Wi-Fi 名字和密码都会没掉，先记一下哦。",
            "omitted": [],
        },
    ),
    (
        "开头用【】写一个小动作或情绪，说话干脆。",
        """【眨眼】查到了，说明书上写的就是 20000 mAh，你记的没错。

根据官网参数页，2024 新款的电芯容量与老款相同，5V 输出时的额定容量为 12000 mAh。新款的改进集中在两方面：其一是快充协议升级，支持 PD 65W；其二是外壳换成了铝合金。该系列共有 10000、20000、30000 三档。

综上，你此前按 20000 估算的"手机能充 4 次左右"是成立的；我之前提到的"新款容量变小"属于误判，在此更正。以上信息来自官网产品页（example.com/power-bank），并与两家评测的数据交叉核对过。""",
        {
            "reply": "【眨眼】查到了，就是 20000 mAh，你记的没错。\n\n新款只换了快充和外壳，容量跟老款一样，按 20000 算能充 4 次左右。之前说新款容量变小，是我说错了。",
            "omitted": [
                "5V 输出时额定容量 12000 mAh",
                "新款支持 PD 65W",
                "全系有 10000 / 20000 / 30000 三档",
                "来源：官网产品页 example.com/power-bank，两家评测数字一致",
            ],
        },
    ),
]

_CODE_FENCE = re.compile(r"^\s*```", re.MULTILINE)
_URL = re.compile(r"https?://[^\s<>()\[\]{}\"'，。！？、；：）】」』]+")
_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.DOTALL)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_TOOL_SYNTAX = re.compile(r"<tool_call>|<function[=>]|<parameter[=>]|\[TOOL_CALLS\]")


@dataclass
class HumanizeConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    extra_body: dict = field(default_factory=lambda: dict(DEFAULT_EXTRA_BODY))
    temperature: float | None = 0.7
    timeout: float = 30.0
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
    if _CODE_FENCE.search(stripped):
        return "含代码块"
    return None


def _wrap(text: str, persona: str = "") -> str:
    head = ""
    if persona.strip():
        head = f"{PERSONA_HEADER}\n<persona>\n{persona.strip()}\n</persona>\n\n"
    return f"{head}<reply>\n{text.strip()}\n</reply>"


def build_messages(text: str, persona: str = "") -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for example_persona, source, output in EXAMPLES:
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
        "stream": False,
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


async def rewrite(
    text: str, cfg: HumanizeConfig, persona: str = "", client=None
) -> HumanizeResult:
    """调用模型改写；失败抛异常（由调用方回退）。

    ``client``: 可选的 ``httpx.AsyncClient``（测试注入）。
    """
    import httpx

    headers = {"Content-Type": "application/json"}
    if cfg.api_key.strip():
        headers["Authorization"] = f"Bearer {cfg.api_key.strip()}"
    payload = build_payload(text, cfg, persona)
    start = time.perf_counter()
    if client is None:
        async with httpx.AsyncClient(timeout=cfg.timeout, trust_env=False) as own:
            resp = await own.post(cfg.endpoint, json=payload, headers=headers)
    else:
        resp = await client.post(
            cfg.endpoint, json=payload, headers=headers, timeout=cfg.timeout
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"响应格式不对: {str(data)[:200]}") from e
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
