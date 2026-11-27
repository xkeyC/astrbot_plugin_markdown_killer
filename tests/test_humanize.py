"""拟人模式测试：改写模块本身 + 插件里的回退与提醒带入。

Run: ``python tests/test_humanize.py``
"""

import asyncio
import json
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# 复用现有测试里的 AstrBot 桩（导入时会装好 sys.modules）。
from test_list_and_table import _load_plugin_class_for_tests

from utils import humanizer

LONG = (
    "## 结论\n\n**可能**是缓存的问题。我查了三个地方：\n"
    "1. 日志里 2026-11-27 有 503 报错\n2. 官方文档 https://example.com/doc 提到排队上限\n"
    "3. issue #42 里也有人遇到\n\n如果还有问题随时问我！"
)


def test_parse_output():
    ok = humanizer.parse_output(
        '```json\n{"reply": "嗯，可能是缓存", "omitted": ["三条线索"]}\n```'
    )
    assert ok and ok.reply == "嗯，可能是缓存" and ok.omitted == ["三条线索"]
    junk = humanizer.parse_output('好的：{"reply": "行", "omitted": "一项"} 完')
    assert junk and junk.omitted == ["一项"]
    assert humanizer.parse_output("不是 JSON") is None
    assert humanizer.parse_output('{"reply": "  ", "omitted": []}') is None
    assert humanizer.parse_output('{"omitted": []}') is None
    print("OK  parse_output tolerates fences/junk, rejects empty or missing reply")


def test_check_result():
    R = humanizer.HumanizeResult
    assert (
        humanizer.check_result(LONG, R("可能是缓存的问题，排队满了会 503。", []))
        is None
    )
    assert "更长" in humanizer.check_result(
        "短短的原文", R("这是一段比原文长很多很多很多很多很多很多很多很多的回复", [])
    )
    assert "链接" in humanizer.check_result(LONG, R("看 https://evil.example/x", []))
    assert "数字" in humanizer.check_result(LONG, R("报错码是 504", []))
    assert "工具" in humanizer.check_result(LONG, R("<tool_call>x", []))
    # 链接原样保留、只删数字都可以
    assert humanizer.check_result(LONG, R("文档在 https://example.com/doc", [])) is None
    print("OK  check_result rejects longer/new URL/new number/tool syntax")


def test_skip_and_payload():
    assert humanizer.skip_reason("好的", 80)
    assert humanizer.skip_reason("x" * 100 + "\n```py\nprint(1)\n```", 80) == "含代码块"
    assert humanizer.skip_reason(LONG, 20) is None
    assert humanizer.parse_extra_body(None) == humanizer.DEFAULT_EXTRA_BODY
    assert humanizer.parse_extra_body("") == {}
    try:
        humanizer.parse_extra_body("[1]")
        raise AssertionError("list accepted")
    except TypeError:
        pass
    cfg = humanizer.HumanizeConfig(base_url="http://h:8731/v1/", model="m")
    assert cfg.endpoint == "http://h:8731/v1/chat/completions"
    payload = humanizer.build_payload(LONG, cfg)
    assert payload["prompt_cache_policy"] == "no_store"
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["model"] == "m" and payload["stream"] is False
    assert LONG.strip() in payload["messages"][-1]["content"]
    print("OK  skip rules, extra body parsing, payload carries no_store + thinking off")


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class _Client:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        return self.resp


def test_rewrite_http():
    cfg = humanizer.HumanizeConfig(base_url="http://h/v1", model="m", api_key="k")
    content = '{"reply": "可能是缓存", "omitted": ["查过的三个地方"]}'
    client = _Client(_Resp(200, {"choices": [{"message": {"content": content}}]}))
    out = asyncio.run(humanizer.rewrite(LONG, cfg, client=client))
    assert out.reply == "可能是缓存" and out.omitted == ["查过的三个地方"]
    url, body, headers = client.calls[0]
    assert url == "http://h/v1/chat/completions"
    assert body["prompt_cache_policy"] == "no_store"
    assert headers["Authorization"] == "Bearer k"
    for bad in (
        _Resp(503, {"error": "busy"}),
        _Resp(200, {"choices": []}),
        _Resp(200, {"choices": [{"message": {"content": "随便"}}]}),
    ):
        try:
            asyncio.run(humanizer.rewrite(LONG, cfg, client=_Client(bad)))
            raise AssertionError("bad response accepted")
        except RuntimeError:
            pass
    print("OK  rewrite posts to /chat/completions, raises on HTTP/format errors")


def _plugin(config):
    cls = _load_plugin_class_for_tests()
    return cls(None, config)


class _Result:
    def __init__(self, chain, typ="LLM_RESULT"):
        self.chain = chain
        self.result_content_type = typ


class _Event:
    unified_msg_origin = "aiocqhttp:GroupMessage:1"

    def __init__(self, result):
        self._result = result

    def get_result(self):
        return self._result


class _Req:
    def __init__(self):
        self.units = []

    def add_persistent_context(self, name, content):
        self.units.append((name, content))


CFG = {
    "enable_humanize": True,
    "humanize_base_url": "http://h/v1",
    "humanize_model": "m",
    "humanize_min_chars": 20,
    "enable_table_render": False,
    "enable_formula_render": False,
}


def test_plugin_humanize_and_reminder():
    plugin = _plugin(dict(CFG))
    import main as main_module

    Plain = main_module.Comp.Plain
    calls = []
    personas = []

    async def fake_rewrite(text, cfg, persona="", client=None):
        calls.append(text)
        personas.append(persona)
        return humanizer.HumanizeResult(
            "可能是缓存的问题。\n\n\n排队满了会报 503。", ["查过的三个地方"], 0.1
        )

    original = main_module.humanizer.rewrite
    main_module.humanizer.rewrite = fake_rewrite
    try:
        # on_llm_response 不再做规则清洗
        resp = types.SimpleNamespace(completion_text=LONG)
        asyncio.run(plugin.on_llm_resp(None, resp))
        assert resp.completion_text == LONG

        # 上一次请求带着人格：插件按会话记下，改写时交给模型
        persona_req = _Req()
        persona_req.context_anchors = {
            "persona": "# Persona Instructions\n\n你是猫娘，自称本喵，句尾带喵。"
        }
        asyncio.run(plugin.on_llm_req(_Event(None), persona_req))
        assert persona_req.units == []

        part1, part2 = Plain(LONG[:30]), Plain(LONG[30:])
        result = _Result([part1, part2])
        event = _Event(result)
        asyncio.run(plugin.on_decorating_result(event))
        assert calls == [LONG]
        assert personas == ["你是猫娘，自称本喵，句尾带喵。"]
        assert len(result.chain) == 1
        assert result.chain[0].text == "可能是缓存的问题。\n\n排队满了会报 503。"

        req = _Req()
        asyncio.run(plugin.on_llm_req(event, req))
        assert len(req.units) == 1 and req.units[0][0] == "reply_simplified"
        note = req.units[0][1]
        assert "排队满了会报 503。" in note and "- 查过的三个地方" in note
        # 只带入一次
        req2 = _Req()
        asyncio.run(plugin.on_llm_req(event, req2))
        assert req2.units == []
    finally:
        main_module.humanizer.rewrite = original
    print(
        "OK  humanize rewrites the chain once and the reminder rides the next request"
    )


def test_plugin_fallbacks():
    _load_plugin_class_for_tests()
    import main as main_module

    Plain = main_module.Comp.Plain

    async def failing(text, cfg, persona="", client=None):
        raise RuntimeError("timeout")

    original = main_module.humanizer.rewrite
    main_module.humanizer.rewrite = failing
    try:
        plugin = _plugin(dict(CFG))
        result = _Result([Plain("**加粗**的一句话，" + "很长" * 20)])
        event = _Event(result)
        asyncio.run(plugin.on_decorating_result(event))
        assert result.chain[0].text.startswith("加粗的一句话")
        req = _Req()
        asyncio.run(plugin.on_llm_req(event, req))
        assert req.units == []  # 失败不留提醒

        # 流式结果原样不动
        streamed = _Result([Plain("**原样**" + "x" * 50)], typ="STREAMING_FINISH")
        asyncio.run(plugin.on_decorating_result(_Event(streamed)))
        assert streamed.chain[0].text.startswith("**原样**")

        # 没填模型时等同关闭：on_llm_response 照常清洗
        off = _plugin({**CFG, "humanize_model": ""})
        resp = types.SimpleNamespace(completion_text="**粗体**")
        asyncio.run(off.on_llm_resp(None, resp))
        assert resp.completion_text == "粗体"

        # 关闭通知时不留提醒
        async def ok(text, cfg, persona="", client=None):
            return humanizer.HumanizeResult("短的", [], 0.0)

        main_module.humanizer.rewrite = ok
        quiet = _plugin({**CFG, "humanize_notify_agent": False})
        event = _Event(_Result([Plain("y" * 60)]))
        asyncio.run(quiet.on_decorating_result(event))
        req = _Req()
        asyncio.run(quiet.on_llm_req(event, req))
        assert req.units == []
    finally:
        main_module.humanizer.rewrite = original
    print("OK  failure/streaming/unconfigured/notify-off fall back without reminders")


def test_prompt_examples_and_persona():
    msgs = humanizer.build_messages(LONG, persona="自称本喵")
    assert msgs[0]["role"] == "system"
    assert "<persona>\n自称本喵\n</persona>" in msgs[-1]["content"]
    assert len(msgs) == 2 + 2 * len(humanizer.EXAMPLES)
    assert msgs[-1]["content"].endswith("<reply>\n" + LONG.strip() + "\n</reply>")
    assert "<persona>" not in humanizer.build_messages(LONG)[-1]["content"]
    # 示例本身要过得了校验，否则是在教模型犯规
    for _persona, source, output in humanizer.EXAMPLES:
        result = humanizer.HumanizeResult(output["reply"], output["omitted"])
        assert humanizer.check_result(source, result) is None, output["reply"]
        assert humanizer.parse_output(msgs[2]["content"]) is not None
    anchors = {
        "persona": "# Persona Instructions\n\n" + "喵" * 50,
        "persona_examples": "Persona example dialogues: []",
    }
    text = humanizer.persona_from_anchors(anchors, 0)
    assert text.startswith("喵") and text.endswith("[]")
    assert humanizer.persona_from_anchors(anchors, 10).startswith("喵" * 10 + "\n……")
    assert humanizer.persona_from_anchors({}, 10) == ""
    print(
        "OK  few-shot examples pass their own checks; persona goes into system prompt"
    )


def main():
    test_parse_output()
    test_check_result()
    test_skip_and_payload()
    test_rewrite_http()
    test_prompt_examples_and_persona()
    test_plugin_humanize_and_reminder()
    test_plugin_fallbacks()
    print("ALL HUMANIZE TESTS PASSED")


if __name__ == "__main__":
    main()
