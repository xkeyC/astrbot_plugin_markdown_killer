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
    # 字符串里有未转义引号：按两个字段硬拆
    broken = humanizer.parse_output(
        '{"reply": "路线图只写着"进行中"。\\n\\n等公告吧", "omitted": ["链接"]}'
    )
    assert broken and broken.reply == '路线图只写着"进行中"。\n\n等公告吧'
    assert broken.omitted == ["链接"]
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
    assert payload["model"] == "m" and payload["stream"] is True
    assert payload["response_format"]["type"] == "json_schema"
    no_schema = humanizer.HumanizeConfig(
        base_url="http://h", model="m", json_schema=False
    )
    assert "response_format" not in humanizer.build_payload(LONG, no_schema)
    assert LONG.strip() in payload["messages"][-1]["content"]
    print("OK  skip rules, extra body parsing, payload carries no_store + thinking off")


def _sse(*objs):
    return [f"data: {json.dumps(o, ensure_ascii=False)}" for o in objs] + [
        "data: [DONE]"
    ]


def _chunks(text, size=7, reasoning=""):
    out = []
    if reasoning:
        out.append({"choices": [{"delta": {"reasoning_content": reasoning}}]})
    for i in range(0, len(text), size):
        out.append({"choices": [{"delta": {"content": text[i : i + size]}}]})
    return out


class _StreamResp:
    def __init__(self, status, lines, delay=0.0):
        self.status_code = status
        self._lines = lines
        self._delay = delay

    async def aread(self):
        return "\n".join(self._lines).encode()

    async def aiter_lines(self):
        for line in self._lines:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield line


class _Client:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def stream(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json, headers, timeout))
        resp = self.resp

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def test_rewrite_http():
    cfg = humanizer.HumanizeConfig(base_url="http://h/v1", model="m", api_key="k")
    content = '{"reply": "可能是缓存", "omitted": ["查过的三个地方"]}'
    client = _Client(_StreamResp(200, _sse(*_chunks(content, reasoning="想一下"))))
    out = asyncio.run(humanizer.rewrite(LONG, cfg, client=client))
    assert out.reply == "可能是缓存" and out.omitted == ["查过的三个地方"]
    method, url, body, headers, timeout = client.calls[0]
    assert method == "POST" and url == "http://h/v1/chat/completions"
    assert body["stream"] is True and body["prompt_cache_policy"] == "no_store"
    assert headers["Authorization"] == "Bearer k"
    assert timeout.read == 60.0  # 两次收到数据之间最多 60 秒
    for bad in (
        _StreamResp(503, ['{"error": "busy"}']),
        _StreamResp(200, _sse({"error": {"message": "boom"}})),
        _StreamResp(200, _sse(*_chunks("随便"))),
        _StreamResp(200, ["data: [DONE]"]),
    ):
        try:
            asyncio.run(humanizer.rewrite(LONG, cfg, client=_Client(bad)))
            raise AssertionError("bad response accepted")
        except RuntimeError:
            pass
    print("OK  rewrite streams /chat/completions, raises on HTTP/server/format errors")


def test_idle_timeout_only_when_silent():
    cfg = humanizer.HumanizeConfig(base_url="http://h", model="m", idle_timeout=0.05)
    content = '{"reply": "慢慢吐也行", "omitted": []}'
    # 每 30 ms 吐一块，总时长远超 50 ms：只要在吐字就不断开
    slow = _StreamResp(200, _sse(*_chunks(content, size=3)), delay=0.03)
    out = asyncio.run(humanizer.rewrite(LONG, cfg, client=_Client(slow)))
    assert out.reply == "慢慢吐也行" and out.elapsed > 0.05
    # 只有心跳、没有 token：超过 50 ms 就放弃
    silent = _StreamResp(200, [": ping"] * 6 + _sse(*_chunks(content)), delay=0.03)
    try:
        asyncio.run(humanizer.rewrite(LONG, cfg, client=_Client(silent)))
        raise AssertionError("silent stream accepted")
    except humanizer.IdleTimeout as e:
        assert "没有吐字" in str(e)
    print("OK  idle timeout fires only while nothing is generated")


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


def _llm_resp_class(Plain):
    """模仿 LLMResponse：completion_text 由 result_chain 派生，setter 替换 Plain。"""

    class _LLMResp:
        def __init__(self, chain=None, text=None, role="assistant"):
            self.role = role
            self.result_chain = (
                types.SimpleNamespace(chain=list(chain)) if chain is not None else None
            )
            self._text = text

        @property
        def completion_text(self):
            if self.result_chain:
                return "".join(
                    c.text for c in self.result_chain.chain if isinstance(c, Plain)
                )
            return self._text

        @completion_text.setter
        def completion_text(self, value):
            if self.result_chain:
                kept = [c for c in self.result_chain.chain if not isinstance(c, Plain)]
                self.result_chain.chain = [Plain(value), *kept]
            else:
                self._text = value

    return _LLMResp


def _patch_rewrite(main_module, fn):
    original = main_module.humanizer.rewrite
    main_module.humanizer.rewrite = fn
    return original


def test_plugin_humanize_and_reminder():
    plugin = _plugin(dict(CFG))
    import main as main_module

    Plain = main_module.Comp.Plain
    LLMResp = _llm_resp_class(Plain)
    calls = []
    personas = []

    async def fake_rewrite(text, cfg, persona="", client=None):
        calls.append(text)
        personas.append(persona)
        return humanizer.HumanizeResult(
            "可能是缓存的问题。\n\n\n排队满了会报 503。", ["查过的三个地方"], 0.1
        )

    original = _patch_rewrite(main_module, fake_rewrite)
    try:
        # 上一次请求带着人格：插件按会话记下，改写时交给模型
        persona_req = _Req()
        persona_req.context_anchors = {
            "persona": "# Persona Instructions\n\n你是猫娘，自称本喵，句尾带喵。"
        }
        asyncio.run(plugin.on_llm_req(_Event(None), persona_req))
        assert persona_req.units == []

        # 正常回合：Codex 的 result_chain 被原地改写（发出的就是这个对象）
        part1, part2 = Plain(LONG[:30]), Plain(LONG[30:])
        resp = LLMResp(chain=[part1, part2])
        chain_obj = resp.result_chain
        event = _Event(None)
        asyncio.run(plugin.on_llm_resp(event, resp))
        assert calls == [LONG]
        assert personas == ["你是猫娘，自称本喵，句尾带喵。"]
        assert resp.result_chain is chain_obj and len(chain_obj.chain) == 1
        assert chain_obj.chain[0].text == "可能是缓存的问题。\n\n排队满了会报 503。"

        # 装饰阶段不再改写第二次
        result = _Result(list(chain_obj.chain))
        asyncio.run(plugin.on_decorating_result(_Event(result)))
        assert calls == [LONG]

        req = _Req()
        asyncio.run(plugin.on_llm_req(event, req))
        assert len(req.units) == 1 and req.units[0][0] == "reply_simplified"
        note = req.units[0][1]
        assert "排队满了会报 503。" in note and "- 查过的三个地方" in note
        # 只带入一次
        req2 = _Req()
        asyncio.run(plugin.on_llm_req(event, req2))
        assert req2.units == []

        # 只有 completion_text 的回复（工具循环 runner）也改写
        bare = LLMResp(text=LONG)
        asyncio.run(plugin.on_llm_resp(event, bare))
        assert bare.completion_text == "可能是缓存的问题。\n\n排队满了会报 503。"
    finally:
        main_module.humanizer.rewrite = original
    print(
        "OK  humanize rewrites in on_llm_response; the reminder rides the next request"
    )


def test_plugin_fallbacks():
    _load_plugin_class_for_tests()
    import main as main_module

    Plain = main_module.Comp.Plain
    LLMResp = _llm_resp_class(Plain)
    calls = []

    class ReadTimeout(Exception):
        """像 httpx.ReadTimeout 一样 str() 为空。"""

    async def failing(text, cfg, persona="", client=None):
        calls.append(text)
        raise ReadTimeout()

    original = _patch_rewrite(main_module, failing)
    try:
        plugin = _plugin(dict(CFG))
        event = _Event(None)
        resp = LLMResp(chain=[Plain("**加粗**的一句话，" + "很长" * 20)])
        logged = []
        real_info = main_module.logger.info
        main_module.logger.info = lambda msg, *a, **k: logged.append(msg)
        try:
            asyncio.run(plugin.on_llm_resp(event, resp))
        finally:
            main_module.logger.info = real_info
        assert resp.completion_text.startswith("加粗的一句话")
        assert any("改写失败: ReadTimeout）" in m for m in logged), logged
        req = _Req()
        asyncio.run(plugin.on_llm_req(event, req))
        assert req.units == []  # 失败不留提醒

        # 错误回复、流式会话：不改写，照常清洗
        calls.clear()
        err = LLMResp(chain=[Plain("**出错**了" + "x" * 90)], role="err")
        asyncio.run(plugin.on_llm_resp(event, err))
        assert calls == [] and err.completion_text.startswith("出错了")

        streaming = _plugin(dict(CFG))
        streaming.context = types.SimpleNamespace(
            get_config=lambda umo=None: {
                "provider_settings": {"streaming_response": True}
            }
        )
        streamed = LLMResp(chain=[Plain("**原样**" + "x" * 90)])
        asyncio.run(streaming.on_llm_resp(event, streamed))
        assert calls == [] and streamed.completion_text.startswith("原样")

        # 平台不支持流式且策略为 turn_off：实际按非流式发，照常改写
        streaming.context = types.SimpleNamespace(
            get_config=lambda umo=None: {
                "provider_settings": {
                    "streaming_response": True,
                    "unsupported_streaming_strategy": "turn_off",
                }
            }
        )
        qq = types.SimpleNamespace(
            unified_msg_origin="qq:1",
            platform_meta=types.SimpleNamespace(support_streaming_message=False),
        )
        asyncio.run(streaming.on_llm_resp(qq, LLMResp(chain=[Plain("y" * 90)])))
        assert len(calls) == 1

        # 没填模型时等同关闭：on_llm_response 照常清洗
        off = _plugin({**CFG, "humanize_model": ""})
        bare = types.SimpleNamespace(completion_text="**粗体**")
        asyncio.run(off.on_llm_resp(None, bare))
        assert bare.completion_text == "粗体"

        # 模型原样返回：按普通清洗发出，不留提醒
        async def same(text, cfg, persona="", client=None):
            return humanizer.HumanizeResult(text.replace("**", ""), [], 4.5)

        main_module.humanizer.rewrite = same
        plugin = _plugin(dict(CFG))
        a, b = Plain("**一段**本来就该原样发的文案，"), Plain("内容" * 40)
        resp = LLMResp(chain=[a, b])
        asyncio.run(plugin.on_llm_resp(event, resp))
        assert resp.result_chain.chain == [a, b]
        assert a.text == "一段本来就该原样发的文案，"
        req = _Req()
        asyncio.run(plugin.on_llm_req(event, req))
        assert req.units == []

        # 关闭通知时不留提醒
        async def ok(text, cfg, persona="", client=None):
            return humanizer.HumanizeResult("短的", [], 0.0)

        main_module.humanizer.rewrite = ok
        quiet = _plugin({**CFG, "humanize_notify_agent": False})
        asyncio.run(quiet.on_llm_resp(event, LLMResp(chain=[Plain("y" * 60)])))
        req = _Req()
        asyncio.run(quiet.on_llm_req(event, req))
        assert req.units == []
    finally:
        main_module.humanizer.rewrite = original
    print("OK  failure/err/streaming/unconfigured/unchanged/notify-off fall back")


def test_unchanged_is_not_a_rewrite():
    R = humanizer.HumanizeResult
    src = "**这是**一段本来就该原样发的文案，" + "内容" * 40
    assert humanizer.is_unchanged(src, R(src.replace("**", ""), []))
    assert humanizer.is_unchanged(src, R(src.replace("**", "") + "。", []))
    assert not humanizer.is_unchanged(src, R(src.replace("**", ""), ["一项"]))
    assert not humanizer.is_unchanged(LONG, R("可能是缓存的问题。", []))
    print("OK  a rewrite that returns the original is detected")


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
    test_idle_timeout_only_when_silent()
    test_prompt_examples_and_persona()
    test_unchanged_is_not_a_rewrite()
    test_plugin_humanize_and_reminder()
    test_plugin_fallbacks()
    print("ALL HUMANIZE TESTS PASSED")


if __name__ == "__main__":
    main()
