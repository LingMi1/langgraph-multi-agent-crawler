"""tests/test_breaker_rescue.py — 运行级熔断 + 批量后置抢救链路测试

grill 定稿三条决策的可验证行为：
  1. 熔断：连续 N 次失败开闸（成功清零 / run 内不复位 / 开闸后快速失败）
  2. 热路径零 LLM：llm_locate(allow_llm=False) 缓存未命中不调 LLM
  3. 批量抢救：LLM 可用 → 一次模板定位泛化全组；不可用/熔断 → 降级保存+标记
"""

import asyncio

import pytest

from agents.breaker import CircuitOpenError, LLMCircuitBreaker, llm_breaker
from agents.budget import TokenBudget, TrackedLLM


# ============================================================================
# 熔断器单元行为
# ============================================================================

def test_breaker_opens_after_consecutive_failures():
    br = LLMCircuitBreaker(threshold=3)
    assert br.check() is True
    br.record_failure(TimeoutError("t1"))
    br.record_failure(TimeoutError("t2"))
    assert br.open is False  # 未达阈值不开闸
    br.record_failure(TimeoutError("t3"))
    assert br.open is True
    assert "t3" in br.reason


def test_breaker_success_resets_counter():
    br = LLMCircuitBreaker(threshold=3)
    br.record_failure(TimeoutError("t1"))
    br.record_failure(TimeoutError("t2"))
    br.record_success()  # 单次成功清零
    br.record_failure(TimeoutError("t3"))
    assert br.open is False  # 重新计数，未连续 3 次


def test_breaker_no_reset_within_run_after_open():
    br = LLMCircuitBreaker(threshold=2)
    br.record_failure(TimeoutError("a"))
    br.record_failure(TimeoutError("b"))
    assert br.open is True
    br.record_success()  # 开闸后成功也不复位（run 内无 half-open）
    assert br.open is True
    assert br.check() is False
    br.reset()  # run 级复位
    assert br.open is False


def test_breaker_threshold_floor():
    assert LLMCircuitBreaker(threshold=0).threshold == 1


# ============================================================================
# TrackedLLM 集成：开闸快速失败 + 失败记账
# ============================================================================

class _BoomClient:
    def __init__(self):
        self.calls = 0

    def invoke(self, prompt, **kw):
        self.calls += 1
        raise TimeoutError("endpoint dead")

    async def ainvoke(self, prompt, **kw):
        self.calls += 1
        raise TimeoutError("endpoint dead")


class _OkClient:
    def invoke(self, prompt, **kw):
        return "ok"

    async def ainvoke(self, prompt, **kw):
        return "ok"


def test_tracked_llm_records_failure_and_trips_breaker():
    llm_breaker.reset()
    client = _BoomClient()
    tracked = TrackedLLM(client, TokenBudget(), retries=0, backoff=0.0)
    for _ in range(3):
        with pytest.raises(TimeoutError):
            tracked.invoke("p")
    assert llm_breaker.open is True


def test_tracked_llm_open_raises_circuit_open_without_calling_client():
    llm_breaker.reset()
    client = _BoomClient()
    tracked = TrackedLLM(client, TokenBudget(), retries=0, backoff=0.0)
    llm_breaker.record_failure("e1")
    llm_breaker.record_failure("e2")
    llm_breaker.record_failure("e3")
    assert llm_breaker.open is True
    before = client.calls
    with pytest.raises(CircuitOpenError):
        tracked.invoke("p")
    assert client.calls == before  # 快速失败：根本没碰客户端


def test_tracked_llm_success_keeps_breaker_closed():
    llm_breaker.reset()
    tracked = TrackedLLM(_OkClient(), TokenBudget(), retries=0, backoff=0.0)
    assert tracked.invoke("p") == "ok"
    assert llm_breaker.open is False


def test_tracked_llm_ainvoke_breaker_paths():
    llm_breaker.reset()

    async def _run():
        tracked = TrackedLLM(_BoomClient(), TokenBudget(), retries=0, backoff=0.0)
        with pytest.raises(TimeoutError):
            await tracked.ainvoke("p")
        llm_breaker.record_failure("x2")
        llm_breaker.record_failure("x3")
        assert llm_breaker.open is True
        with pytest.raises(CircuitOpenError):
            await tracked.ainvoke("p")

    asyncio.run(_run())
    llm_breaker.reset()


# ============================================================================
# chat_json 集成：熔断打开直接 None（零等待、零请求）
# ============================================================================

def test_chat_json_returns_none_when_breaker_open(monkeypatch):
    llm_breaker.reset()
    from agents import llm_pipeline

    class _NeverCall:
        def __init__(self):
            self.calls = 0

        async def chat_completions_create(self, **kw):
            self.calls += 1
            raise AssertionError("熔断打开不应发起请求")

    fake = _NeverCall()
    monkeypatch.setattr(llm_pipeline, "_get_llm", lambda: fake)
    monkeypatch.setattr(llm_pipeline, "_parse_json", lambda t: {"ok": True})
    llm_breaker.record_failure("f1")
    llm_breaker.record_failure("f2")
    llm_breaker.record_failure("f3")
    result = asyncio.run(llm_pipeline.chat_json("sys", "user"))
    assert result is None
    assert fake.calls == 0
    llm_breaker.reset()


# ============================================================================
# 热路径零 LLM：_llm_locate_content_selector(allow_llm=False)
# ============================================================================

_LONG_TEXT = "正文" * 200  # >100 字，过质量校验


def _fake_html(selector: str) -> str:
    return (
        "<html><body>"
        f"<div id='nav'><a href='/x'>关于我们</a><a href='/y'>新闻中心</a></div>"
        f"<div {selector}><p>{_LONG_TEXT}</p></div>"
        "</body></html>"
    )


def test_llm_locate_hot_path_cache_hit_without_llm(monkeypatch):
    from graph import nodes

    html = _fake_html("id='content'")
    ckey = "www.example.com|/news/{N}.html"
    nodes._CONTENT_SELECTOR_CACHE[ckey] = "#content"
    called = {"n": 0}

    async def _no_llm(*a, **kw):
        called["n"] += 1
        raise AssertionError("热路径不应调 LLM")

    monkeypatch.setattr(nodes, "chat_json", _no_llm)
    sel = asyncio.run(nodes._llm_locate_content_selector(
        html, "http://www.example.com/news/1.html", "http://www.example.com/", "站点",
        allow_llm=False,
    ))
    assert sel == "#content"  # 缓存命中直接返回
    assert called["n"] == 0
    del nodes._CONTENT_SELECTOR_CACHE[ckey]


def test_llm_locate_hot_path_cache_miss_returns_empty(monkeypatch):
    from graph import nodes

    called = {"n": 0}

    async def _no_llm(*a, **kw):
        called["n"] += 1
        raise AssertionError("热路径不应调 LLM")

    monkeypatch.setattr(nodes, "chat_json", _no_llm)
    sel = asyncio.run(nodes._llm_locate_content_selector(
        _fake_html("class='article'"), "http://www.example.com/news/2.html",
        "http://www.example.com/", "站点", allow_llm=False,
    ))
    assert sel == ""  # 未命中 → 启发式兜底，零 LLM
    assert called["n"] == 0


# ============================================================================
# 批量抢救：分组定位 / 熔断降级保存 / 预算溢出
# ============================================================================

def _rescue_entry(n: int, text_len: int = 40) -> dict:
    return {
        "url": f"http://www.example.com/case/{n}.html",
        "url_key": f"http://www.example.com/case/{n}.html",
        "nav_path": ["工程案例"],
        "depth": 2,
        "reason": "content_too_short",
        "text_len": text_len,
        "imgs": 1,
        "title": f"案例{n}",
    }


def _rescue_state(entries, **over):
    state = {
        "seed_url": "http://www.example.com/",
        "site_name": "测试站",
        "output_dir": "output/www.example.com",
        "crawler_config": {},
        "site_profile": {},
        "seen_hashes": [],
        "rescue_queue": entries,
        "stats": {},
    }
    state.update(over)
    return state


class _FakeFetcher:
    """缓存命中型假 Fetcher：返回可定位正文的固定 HTML。"""

    def __init__(self, html_map=None):
        self.html_map = html_map or {}

    async def fetch(self, url, profile):
        from agents.models import PageData

        html = self.html_map.get(url) or _fake_html("class='article'")
        return PageData(url=url, title="t", html=html)


def test_rescue_degraded_save_when_no_llm(monkeypatch, tmp_path):
    """无 LLM key → 全部降级保存（rescue_degraded），不调任何 LLM。"""
    from graph import nodes

    llm_breaker.reset()
    monkeypatch.setattr(nodes, "_get_fetcher", lambda cfg=None: _FakeFetcher())
    saved = {"paths": []}

    async def _fake_save(page, output_dir):
        saved["paths"].append(page.url)
        return f"{len(saved['paths'])}.html"

    monkeypatch.setattr(nodes, "_save_html_file", _fake_save)
    monkeypatch.setattr(nodes.config, "DEEPSEEK_API_KEY", "")

    entries = [_rescue_entry(1), _rescue_entry(2)]
    rows, stats, _ = asyncio.run(nodes._rescue_pending_pages(_rescue_state(entries)))
    assert stats.get("rescue_degraded") == 2
    assert len(rows) == 2
    assert len(saved["paths"]) == 2  # 降级保存：内容已支付成本不浪费
    llm_breaker.reset()


def test_rescue_degraded_save_when_breaker_open(monkeypatch):
    """熔断打开 → 降级保存 + circuit_open 标记路径。"""
    from graph import nodes

    llm_breaker.reset()
    monkeypatch.setattr(nodes, "_get_fetcher", lambda cfg=None: _FakeFetcher())

    async def _fake_save(page, output_dir):
        return "x.html"

    monkeypatch.setattr(nodes, "_save_html_file", _fake_save)
    # 有 key 但熔断已开
    monkeypatch.setattr(nodes.config, "DEEPSEEK_API_KEY", "sk-test")
    for i in range(3):
        llm_breaker.record_failure(f"e{i}")
    assert llm_breaker.open is True

    rows, stats, _ = asyncio.run(nodes._rescue_pending_pages(_rescue_state([_rescue_entry(1)])))
    assert stats.get("rescue_degraded") == 1
    assert len(rows) == 1
    llm_breaker.reset()


def test_rescue_locates_once_per_template(monkeypatch):
    """同栏目模板 3 页只调 1 次 LLM 定位，命中后全组用 selector 重建。"""
    from graph import nodes

    llm_breaker.reset()
    monkeypatch.setattr(nodes, "_get_fetcher", lambda cfg=None: _FakeFetcher())
    monkeypatch.setattr(nodes.config, "DEEPSEEK_API_KEY", "sk-test")
    locate_calls = {"n": 0}

    async def _fake_locate(html, url, home, gsmc, allow_llm=True):
        locate_calls["n"] += 1
        return ".article"

    monkeypatch.setattr(nodes, "_llm_locate_content_selector", _fake_locate)

    async def _fake_save(page, output_dir):
        return "y.html"

    monkeypatch.setattr(nodes, "_save_html_file", _fake_save)

    entries = [_rescue_entry(1), _rescue_entry(2), _rescue_entry(3)]  # 同 /case/{N}.html 模板
    rows, stats, _ = asyncio.run(nodes._rescue_pending_pages(_rescue_state(entries)))
    assert locate_calls["n"] == 1  # 一次定位泛化全组
    assert stats.get("rescued") == 3
    assert len(rows) == 3
    llm_breaker.reset()


def test_rescue_overflow_beyond_budget(monkeypatch):
    """候选超过 RESCUE_MAX_PAGES → 溢出部分降级保存（budget 标记）。"""
    from graph import nodes

    llm_breaker.reset()
    monkeypatch.setattr(nodes, "_get_fetcher", lambda cfg=None: _FakeFetcher())
    monkeypatch.setattr(nodes.config, "DEEPSEEK_API_KEY", "")
    monkeypatch.setattr(nodes.config, "RESCUE_MAX_PAGES", 2)

    async def _fake_save(page, output_dir):
        return "z.html"

    monkeypatch.setattr(nodes, "_save_html_file", _fake_save)

    entries = [_rescue_entry(i) for i in range(5)]
    rows, stats, _ = asyncio.run(nodes._rescue_pending_pages(_rescue_state(entries)))
    assert stats.get("rescue_candidates") == 5
    assert stats.get("rescue_degraded") == 5
    assert len(rows) == 5
    llm_breaker.reset()


def test_rescue_empty_queue_noop():
    from graph import nodes

    rows, stats, seen = asyncio.run(nodes._rescue_pending_pages(_rescue_state([])))
    assert rows == [] and stats == {} and seen is None  # None=未写回，不清空既有 hash


# ============================================================================
# evaluate_node 接入：评估前抢救、结果并入、队列清空
# ============================================================================

def test_evaluate_node_rescues_before_eval(monkeypatch):
    from graph import nodes

    llm_breaker.reset()
    fake_row = {"url": "http://www.example.com/case/9.html", "title": "案例9",
                "html": "<p>x</p>", "file_path": "a.html"}
    fake_stats = {"rescued": 1, "rescue_candidates": 1}

    async def _fake_rescue(state):
        return [fake_row], fake_stats, ["hash_new_1"]

    monkeypatch.setattr(nodes, "_rescue_pending_pages", _fake_rescue)
    monkeypatch.setattr(nodes, "_get_llm", lambda: None)  # 无 LLM → 启发式评估

    state = _rescue_state([_rescue_entry(9)], crawled_results=[
        {"url": "u1", "title": "t1", "html": "<p>" + _LONG_TEXT + "</p>"},
    ], stats={"fetched": 2, "saved": 1})
    result = asyncio.run(nodes.evaluate_node(state))
    assert result["crawled_results"] == [fake_row]
    assert result["rescue_queue"] == []
    assert result["stats"]["rescued"] == 1
    assert result["stats"]["saved"] == 2  # 抢救行计入 saved
    assert result["seen_hashes"] == ["hash_new_1"]  # 抢救 hash 写回（防二次落盘）
    llm_breaker.reset()
