"""
fetcher 合规与安全测试：robots.txt 检查 + TLS 校验配置传递。

覆盖:
  - _robots_allows: 禁止/允许/404/网络失败/开关关闭/域缓存
  - _get_httpx_client: verify 默认开启、配置关闭时显式豁免
  - fetch: robots 禁止时返回 blocked_by_robots 且不触碰去重/缓存
"""

import asyncio
import sys
import types
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import agents.fetcher as fetcher


class _FakeResp:
    def __init__(self, text: str = "", status_code: int = 200):
        self.text = text
        self.status_code = status_code


class _FakeClient:
    """可配置响应的 httpx.AsyncClient 替身（支持 async with + get）。"""

    def __init__(self, resp=None, exc: Exception = None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url: str):
        if self._exc is not None:
            raise self._exc
        return self._resp


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """每个用例清空模块级缓存与客户端单例，避免跨用例污染。"""
    fetcher._ROBOTS_CACHE.clear()
    fetcher._HTTPX_CLIENT = None
    yield
    fetcher._ROBOTS_CACHE.clear()
    fetcher._HTTPX_CLIENT = None


def _install_async_client(monkeypatch, fake_client) -> list:
    """把 httpx.AsyncClient 换成 fake，返回构造调用 kwargs 列表。"""
    calls = []

    def _make(*args, **kwargs):
        calls.append(kwargs)
        return fake_client

    monkeypatch.setattr(httpx, "AsyncClient", _make)
    return calls


def _run(coro):
    """同步测试里运行异步函数。"""
    return asyncio.run(coro)


# ============================================================================
# _robots_allows
# ============================================================================

def test_robots_allows_disallow_path(monkeypatch):
    resp = _FakeResp("User-agent: *\nDisallow: /private\n")
    fake = _FakeClient(resp=resp)
    _install_async_client(monkeypatch, fake)

    assert _run(fetcher._robots_allows("http://x.com/private/page")) is False
    assert _run(fetcher._robots_allows("http://x.com/public/page")) is True


def test_robots_allows_404_defaults_allow(monkeypatch):
    fake = _FakeClient(resp=_FakeResp(status_code=404))
    _install_async_client(monkeypatch, fake)

    assert _run(fetcher._robots_allows("http://x.com/page")) is True


def test_robots_allows_network_error_defaults_allow(monkeypatch):
    fake = _FakeClient(exc=httpx.ConnectError("conn refused"))
    _install_async_client(monkeypatch, fake)

    assert _run(fetcher._robots_allows("http://x.com/page")) is True


def test_robots_allows_disabled_skips_request(monkeypatch):
    monkeypatch.setattr(config, "CRAWLER_RESPECT_ROBOTS", False)
    fake = _FakeClient(resp=_FakeResp("User-agent: *\nDisallow: /\n"))
    calls = _install_async_client(monkeypatch, fake)

    assert _run(fetcher._robots_allows("http://x.com/page")) is True
    assert calls == []  # 开关关闭时零请求


def test_robots_cache_per_netloc(monkeypatch):
    resp = _FakeResp("User-agent: *\nDisallow: /\n")
    fake = _FakeClient(resp=resp)
    calls = _install_async_client(monkeypatch, fake)

    assert _run(fetcher._robots_allows("http://x.com/a")) is False
    assert _run(fetcher._robots_allows("http://x.com/b")) is False  # 命中缓存
    assert len(calls) == 1  # 同域只请求一次 robots.txt


def test_robots_allows_non_http_skips(monkeypatch):
    fake = _FakeClient(resp=_FakeResp("User-agent: *\nDisallow: /\n"))
    calls = _install_async_client(monkeypatch, fake)

    assert _run(fetcher._robots_allows("file:///etc/passwd")) is True
    assert calls == []


# ============================================================================
# fetch 集成：robots 禁止 → blocked_by_robots
# ============================================================================

async def _false(*args, **kwargs):
    return False


def test_fetch_blocked_by_robots(monkeypatch):
    monkeypatch.setattr(fetcher, "_robots_allows", _false)
    f = fetcher.HttpxPlaywrightFetcher()
    page = _run(f.fetch("http://x.com/private", types.SimpleNamespace(needs_js_render=False)))
    assert page.fetch_method == "blocked_by_robots"
    assert page.html == ""


# ============================================================================
# _get_httpx_client: TLS verify
# ============================================================================

def test_httpx_client_verify_default_true(monkeypatch):
    fake = _FakeClient(resp=_FakeResp())
    calls = _install_async_client(monkeypatch, fake)

    fetcher._get_httpx_client()
    assert calls and calls[0]["verify"] is True


def test_httpx_client_verify_false_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "CRAWLER_TLS_VERIFY", False)
    fake = _FakeClient(resp=_FakeResp())
    calls = _install_async_client(monkeypatch, fake)

    fetcher._get_httpx_client()
    assert calls and calls[0]["verify"] is False
