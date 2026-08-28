# -*- coding: utf-8 -*-
"""多 provider 故障转移 + 流式输出测试（P2c）

覆盖：
  1. _providers() 解析：主 + 备用，key/model 缺省回退主配置
  2. chat_json 故障转移：主 provider 失败 → 自动切备用成功
  3. chat_json 全部 provider 失败 → None + 记 1 次连续失败
  4. chat_json 切换后按备用 provider 的 model 请求
  5. chat_stream 逐块拼装 + stream=True 透传
  6. chat_stream create 失败 → 切备用
  7. chat_stream 熔断打开 → 零请求空产出
  8. chat_stream 流中途异常 → 保留已收片段并终止（不切换 provider）
"""

import asyncio

import pytest

import config
from agents import llm_pipeline
from agents.breaker import llm_breaker


# ============================================================================
# 假客户端（OpenAI client.chat.completions.create 形态）
# ============================================================================

class _Completions:
    def __init__(self, owner):
        self._owner = owner

    async def create(self, **kw):
        return await self._owner._create(**kw)


class _Chat:
    def __init__(self, owner):
        self.completions = _Completions(owner)


class _OkClient:
    """create 成功返回固定 JSON 文本；记录 kwargs。"""

    def __init__(self, content='{"ok": true}'):
        self.chat = _Chat(self)
        self.calls = 0
        self.kwargs = []
        self.content = content

    async def _create(self, **kw):
        self.calls += 1
        self.kwargs.append(kw)
        return _Resp(self.content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Message:
    def __init__(self, content):
        self.content = content


class _FailClient:
    """create 抛异常；模拟 provider 宕机/限流。"""

    def __init__(self, exc=None):
        self.chat = _Chat(self)
        self.calls = 0
        self.exc = exc or ConnectionError("provider down")

    async def _create(self, **kw):
        self.calls += 1
        raise self.exc


# ============================================================================
# 流式 chunk 假实现
# ============================================================================

class _StreamChunk:
    def __init__(self, content=None, empty=False):
        self.choices = [] if empty else [_StreamChoice(content)]


class _StreamChoice:
    def __init__(self, content):
        self.delta = _StreamDelta(content)


class _StreamDelta:
    def __init__(self, content):
        self.content = content


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


class _StreamClient(_OkClient):
    async def _create(self, **kw):
        self.calls += 1
        self.kwargs.append(kw)
        return _FakeStream(
            [
                _StreamChunk("hello"),
                _StreamChunk(empty=True),  # choices 为空 → 应跳过
                _StreamChunk(" world"),
            ]
        )


async def _collect(agen):
    out = []
    async for p in agen:
        out.append(p)
    return out


# ============================================================================
# 基础设施
# ============================================================================

@pytest.fixture(autouse=True)
def _clean_state():
    llm_pipeline.reset_llm()
    llm_breaker.reset()
    yield
    llm_pipeline.reset_llm()
    llm_breaker.reset()


@pytest.fixture
def with_backups(monkeypatch):
    monkeypatch.setattr(
        config, "LLM_BACKUP_BASE_URLS", "https://b1.example.com|https://b2.example.com"
    )
    monkeypatch.setattr(config, "LLM_BACKUP_API_KEYS", "sk-b1")
    monkeypatch.setattr(config, "LLM_BACKUP_MODELS", "model-b1|model-b2")


def _install(monkeypatch, fakes):
    """按真实 provider 下标取对应假客户端，保持 _switch_provider 真实生效。"""
    monkeypatch.setattr(
        llm_pipeline,
        "_get_llm",
        lambda: fakes[min(llm_pipeline._llm_provider_idx, len(fakes) - 1)],
    )
    monkeypatch.setattr(llm_pipeline, "_parse_json", lambda t: {"ok": True})


# ============================================================================
# 1. provider 配置解析
# ============================================================================

def test_providers_parse_backups(with_backups):
    providers = llm_pipeline._providers()
    assert len(providers) == 3
    # 主 provider 固定取 DEEPSEEK_*
    assert providers[0]["base_url"] == config.DEEPSEEK_BASE_URL
    assert providers[0]["api_key"] == config.DEEPSEEK_API_KEY
    # 备用按下标对应
    assert providers[1]["base_url"] == "https://b1.example.com"
    assert providers[1]["api_key"] == "sk-b1"
    assert providers[1]["model"] == "model-b1"
    # key 缺省 → 回退主配置；model 有值则用备用
    assert providers[2]["api_key"] == config.DEEPSEEK_API_KEY
    assert providers[2]["model"] == "model-b2"


def test_switch_provider_bounds():
    assert llm_pipeline._switch_provider() is False  # 无备用 → 无法切换
    assert llm_pipeline._llm_provider_idx == 0


# ============================================================================
# 2-4. chat_json 故障转移
# ============================================================================

def test_chat_json_failover_to_backup(with_backups, monkeypatch):
    failing = _FailClient()
    ok = _OkClient()
    _install(monkeypatch, [failing, ok])
    result = asyncio.run(llm_pipeline.chat_json("sys", "user", retries=0))
    assert result == {"ok": True}
    assert failing.calls == 1
    assert ok.calls == 1
    assert llm_pipeline._llm_provider_idx == 1  # 已切到备用
    assert llm_breaker.open is False  # 成功清零，不触发熔断


def test_chat_json_failover_uses_backup_model(with_backups, monkeypatch):
    failing = _FailClient()
    ok = _OkClient()
    _install(monkeypatch, [failing, ok])
    asyncio.run(llm_pipeline.chat_json("sys", "user", retries=0))
    assert ok.kwargs[0]["model"] == "model-b1"  # 切换后用备用 provider 的模型


def test_chat_json_all_providers_fail(with_backups, monkeypatch):
    _install(monkeypatch, [_FailClient(), _FailClient(), _FailClient()])
    result = asyncio.run(llm_pipeline.chat_json("sys", "user", retries=0))
    assert result is None
    assert llm_pipeline._llm_provider_idx == 2  # 0→1→2 全部耗尽
    assert llm_breaker.open is False  # 单次调用只记 1 次失败，未达阈值


def test_chat_json_no_backup_failures_recorded(monkeypatch):
    """无备用 provider 时保持原有单 provider 行为：失败记 1 次。"""
    _install(monkeypatch, [_FailClient()])
    result = asyncio.run(llm_pipeline.chat_json("sys", "user", retries=0))
    assert result is None
    assert llm_breaker.status()["consecutive_failures"] == 1


# ============================================================================
# 5-8. chat_stream 流式输出
# ============================================================================

def test_chat_stream_assembles_chunks(monkeypatch):
    sc = _StreamClient()
    _install(monkeypatch, [sc])
    pieces = asyncio.run(_collect(llm_pipeline.chat_stream("sys", "user")))
    assert "".join(pieces) == "hello world"
    assert sc.calls == 1
    assert sc.kwargs[0]["stream"] is True  # stream=True 透传


def test_chat_stream_failover_create(with_backups, monkeypatch):
    failing = _FailClient()
    sc = _StreamClient()
    _install(monkeypatch, [failing, sc])
    pieces = asyncio.run(_collect(llm_pipeline.chat_stream("sys", "user")))
    assert "".join(pieces) == "hello world"
    assert failing.calls == 1
    assert sc.calls == 1
    assert llm_pipeline._llm_provider_idx == 1


def test_chat_stream_breaker_open_zero_requests(monkeypatch):
    class _Never:
        def __init__(self):
            self.calls = 0
            self.chat = _Chat(self)

        async def _create(self, **kw):
            self.calls += 1
            raise AssertionError("熔断打开不应发起请求")

    never = _Never()
    _install(monkeypatch, [never])
    llm_breaker.record_failure("f1")
    llm_breaker.record_failure("f2")
    llm_breaker.record_failure("f3")
    pieces = asyncio.run(_collect(llm_pipeline.chat_stream("sys", "user")))
    assert pieces == []
    assert never.calls == 0


def test_chat_stream_mid_stream_abort_terminates(monkeypatch):
    class _AbortStream(_FakeStream):
        async def _gen(self):
            yield _StreamChunk("hi")
            raise RuntimeError("connection reset")

    class _AbortClient(_OkClient):
        async def _create(self, **kw):
            self.calls += 1
            return _AbortStream([])

    ab = _AbortClient()
    _install(monkeypatch, [ab])
    pieces = asyncio.run(_collect(llm_pipeline.chat_stream("sys", "user")))
    assert "".join(pieces) == "hi"  # 保留已收片段
    assert ab.calls == 1
    assert llm_pipeline._llm_provider_idx == 0  # 中途异常不切换 provider
    assert llm_breaker.status()["consecutive_failures"] == 1
