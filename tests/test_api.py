"""FastAPI 服务层单测（TestClient + 假爬虫入口，不真网络）。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from api import server


@pytest.fixture()
def client(monkeypatch):
    server._TASKS.clear()
    server._TASK_ORDER.clear()
    server._SUBMIT_WINDOW.clear()
    monkeypatch.delenv("CRAWLER_API_KEY", raising=False)

    def _fake_crawl(target_url, concurrency=5, log_callback=None,
                    reset_memory=False, progress_callback=None):
        for i in range(3):
            if log_callback:
                log_callback(f"fake log {i}")
        if progress_callback:
            progress_callback(1, 0, target_url, "fetch")
        return 3

    monkeypatch.setattr("main.run_langgraph_crawler", _fake_crawl)
    # _run_crawl 里是局部 import main → 需要让 main 模块属性也被打桩
    import main
    monkeypatch.setattr(main, "run_langgraph_crawler", _fake_crawl)
    # ★ with 上下文：整个会话复用同一 portal/事件循环，后台任务才能跨请求存活
    with TestClient(server.app) as c:
        yield c


def _wait_done(client, task_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/tasks/{task_id}")
        if r.json().get("status") != "running":
            return r.json()
        time.sleep(0.05)
    raise AssertionError("任务未在超时内完成")


def test_submit_and_poll_and_results(client, tmp_path, monkeypatch):
    r = client.post("/crawl", json={"url": "http://www.example.com/"})
    assert r.status_code == 202
    task_id = r.json()["task_id"]

    final = _wait_done(client, task_id)
    assert final["status"] == "done"
    assert final["saved"] == 3
    assert final["logs_tail"] and final["logs_tail"][0].startswith("fake log")
    assert final["progress"]["phase"] == "fetch"

    # 结果接口：写一个假 CSV 到 output/www.example.com/（_find_csv 的定位路径）
    out = Path(server.PROJECT_ROOT) / "output" / "www.example.com"
    out.mkdir(parents=True, exist_ok=True)
    (out / "crawl_results.csv").write_text(
        "title,url,html\n首页,http://www.example.com/," + "x" * 400,
        encoding="utf-8-sig",
    )
    r2 = client.get(f"/tasks/{task_id}/results")
    assert r2.status_code == 200
    body = r2.json()
    assert body["returned"] == 1
    assert body["rows"][0]["html"].endswith("...")  # html 截断预览


def test_single_slot_409(client):
    import threading

    release = threading.Event()
    started = threading.Event()

    def _slow_crawl(target_url, **kw):
        started.set()
        release.wait(5)
        return 1

    import main
    orig = main.run_langgraph_crawler
    main.run_langgraph_crawler = _slow_crawl
    try:
        r1 = client.post("/crawl", json={"url": "http://a.com/"})
        assert r1.status_code == 202
        assert started.wait(3)
        r2 = client.post("/crawl", json={"url": "http://b.com/"})
        assert r2.status_code == 409
        assert r2.json()["detail"].startswith("已有爬取任务")
    finally:
        release.set()
        main.run_langgraph_crawler = orig
        _wait_done(client, r1.json()["task_id"])


def test_api_key_enforced(client, monkeypatch):
    monkeypatch.setenv("CRAWLER_API_KEY", "secret123")
    assert client.post("/crawl", json={"url": "http://x.com/"}).status_code == 401
    r = client.post("/crawl", json={"url": "http://x.com/"},
                    headers={"X-API-Key": "secret123"})
    assert r.status_code == 202
    # 状态接口同样要鉴权
    tid = r.json()["task_id"]
    assert client.get(f"/tasks/{tid}").status_code == 401
    assert client.get(f"/tasks/{tid}", headers={"X-API-Key": "secret123"}).status_code == 200


def test_submit_rate_limit(client):
    for i in range(6):
        url = f"http://rate{i}.com/"
        r = client.post("/crawl", json={"url": url})
        assert r.status_code in (202, 409), r.text
        # 立刻完成后槽释放（假爬虫瞬时）
        tid = r.json().get("task_id")
        if tid:
            _wait_done(client, tid)
    r = client.post("/crawl", json={"url": "http://rate7.com/"})
    assert r.status_code == 429


def test_task_not_found(client):
    assert client.get("/tasks/nope").status_code == 404
    assert client.get("/tasks/nope/results").status_code == 404


def test_running_results_409(client):
    import threading

    release = threading.Event()
    started = threading.Event()

    def _slow(target_url, **kw):
        started.set()
        release.wait(5)
        return 1

    import main
    orig = main.run_langgraph_crawler
    main.run_langgraph_crawler = _slow
    try:
        r = client.post("/crawl", json={"url": "http://slow.com/"})
        tid = r.json()["task_id"]
        assert started.wait(3)
        assert client.get(f"/tasks/{tid}/results").status_code == 409
    finally:
        release.set()
        main.run_langgraph_crawler = orig
        _wait_done(client, tid)


def test_failed_task_reports_error(client, monkeypatch):
    def _boom(target_url, **kw):
        raise RuntimeError("爬虫崩了")

    import main
    monkeypatch.setattr(main, "run_langgraph_crawler", _boom)
    r = client.post("/crawl", json={"url": "http://boom.com/"})
    tid = r.json()["task_id"]
    final = _wait_done(client, tid)
    assert final["status"] == "failed"
    assert "RuntimeError" in final["error"]


# ============================================================================
# /chat/stream SSE 流式端点（chat_stream 基础设施已由 failover 测试覆盖）
# ============================================================================

def _install_stream_fake(monkeypatch, chunks):
    """把 llm_pipeline._get_llm 换成产出 chunks 的流式假客户端。"""
    from agents import llm_pipeline

    class _Delta:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.delta = _Delta(content)

    class _Chunk:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    class _FakeStream:
        def __init__(self, items):
            self._items = items

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for c in self._items:
                yield c

    class _Completions:
        async def create(self, **kw):
            return _FakeStream([_Chunk(c) for c in chunks])

    class _FakeClient:
        chat = type("_Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(llm_pipeline, "_get_llm", lambda: _FakeClient())


def test_chat_stream_sse(client, monkeypatch):
    from agents.breaker import llm_breaker

    llm_breaker.reset()
    _install_stream_fake(monkeypatch, ["你好", "世界"])
    r = client.post("/chat/stream", json={"prompt": "hi"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "data: 你好" in r.text
    assert "data: 世界" in r.text
    assert r.text.endswith("data: [DONE]\n\n")
    llm_breaker.reset()


def test_chat_stream_sse_breaker_open_only_done(client, monkeypatch):
    from agents import llm_pipeline
    from agents.breaker import llm_breaker

    llm_breaker.record_failure("f1")
    llm_breaker.record_failure("f2")
    llm_breaker.record_failure("f3")  # 开闸 → chat_stream 零请求直接空产出

    class _Never:
        def __init__(self):
            self.calls = 0

    never = _Never()
    monkeypatch.setattr(llm_pipeline, "_get_llm", lambda: never)
    r = client.post("/chat/stream", json={"prompt": "hi"})
    assert r.status_code == 200
    assert r.text == "data: [DONE]\n\n"
    assert never.calls == 0
    llm_breaker.reset()
