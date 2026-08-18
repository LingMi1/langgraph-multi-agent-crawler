"""
Agent 3: FetcherRouter — 下载路由

职责: 根据 SiteProfile 决定下载策略，执行实际页面抓取。

策略 A (主): httpx 异步请求 — 快速、低资源、适合静态/轻度 JS 页面
策略 B (降级): Playwright 渲染 — 处理强 JS 依赖页面
策略 C: 缓存命中 — 直接返回记忆库中的内容
"""

from __future__ import annotations

import asyncio
import re
import random
from typing import List, Optional, AsyncIterator, Tuple
from urllib.parse import urlparse

import httpx
import urllib3
from bs4 import BeautifulSoup

from .models import SiteProfile, PageData
from .interfaces import FetcherRouter as FetcherRouterInterface

from memory import UrlMemory
from schemas import agent_logger
import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================================
# User-Agent 池
# ============================================================================

_USER_AGENTS = [
    # Windows + Chrome 多版本
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    # macOS + Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # macOS + Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    # Windows + Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    # macOS + Firefox
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
    # Windows + Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    # Linux + Chrome
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    # Linux + Firefox
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    # Mobile (iPhone)
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    # Mobile (Android)
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.135 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S9080) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; V2217A) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.122 Mobile Safari/537.36",
]


def _random_ua() -> str:
    return random.choice(_USER_AGENTS)

# ============================================================================
# Playwright Stealth — 浏览器指纹对抗（内联，不依赖外部库）
# ============================================================================

_STEALTH_JS = """
// 覆盖 navigator.webdriver 检测
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// 伪造 Chrome 运行时对象
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {}
};

// 伪造 plugins 数组（正常 Chrome 至少有 3 个）
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
        ];
        plugins.item = (i) => plugins[i];
        plugins.namedItem = (name) => plugins.find(p => p.name === name);
        plugins.refresh = () => {};
        Object.setPrototypeOf(plugins, PluginArray.prototype);
        return plugins;
    }
});

// 伪造 languages
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });

// 伪造 platform
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

// 伪造 hardwareConcurrency
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

// 覆盖 permissions.query（Cloudflare Turnstile 常用检测点）
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
    Promise.resolve({ state: Notification.permission }) :
    originalQuery(parameters)
);

// 覆盖 headless 检测
Object.defineProperty(navigator, 'productSub', { get: () => '20030107' });
Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
Object.defineProperty(navigator, 'vendorSub', { get: () => '' });
"""


def _apply_stealth(page) -> None:
    """注入 stealth 脚本，清除 Playwright 自动化特征"""
    try:
        page.add_init_script(_STEALTH_JS)
    except Exception:
        pass  # 非关键，忽略失败


def _simulate_human_scroll(page) -> None:
    """
    模拟人类滚动行为：分 3~5 段非匀速滚动，每段间隔随机延迟。
    在触发懒加载的同时避免触发无头浏览器检测。
    """
    try:
        import random as _rnd
        steps = _rnd.randint(3, 5)
        total_height = page.evaluate("document.body.scrollHeight")
        for i in range(1, steps + 1):
            target = int(total_height * i / steps)
            page.evaluate(f"window.scrollTo({{top: {target}, behavior: 'smooth'}})")
            page.wait_for_timeout(_rnd.randint(300, 700))
    except Exception:
        # 降级为简单滚动
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)
        except Exception:
            pass


# ============================================================================
# httpx 异步客户端（单例）
# ============================================================================

_HTTPX_CLIENT: Optional[httpx.AsyncClient] = None


def _get_httpx_client() -> httpx.AsyncClient:
    global _HTTPX_CLIENT
    if _HTTPX_CLIENT is None:
        _HTTPX_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout=float(config.REQUEST_TIMEOUT),
                connect=15.0,
            ),
            follow_redirects=True,
            verify=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _HTTPX_CLIENT


# ============================================================================
# Playwright — 必须在一个线程内完成 启动→抓取，保护跨线程报错
# ============================================================================

_PW_AVAILABLE: Optional[bool] = None
_PW_BROWSER = None
_PW_LOCK = None
_PW_USE_SYSTEM_CHROME: bool = False  # 跟踪当前浏览器使用的 channel 模式


def _get_pw_lock():
    """延迟创建 Playwright 线程锁"""
    global _PW_LOCK
    if _PW_LOCK is None:
        import threading
        _PW_LOCK = threading.Lock()
    return _PW_LOCK


def _check_playwright_import() -> bool:
    """检查 Playwright 是否可导入（不影响浏览器实例）"""
    global _PW_AVAILABLE
    if _PW_AVAILABLE is None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
            _PW_AVAILABLE = True
        except ImportError:
            _PW_AVAILABLE = False
            agent_logger.warning("[FetcherRouter] Playwright 未安装，将不使用浏览器渲染")
    return _PW_AVAILABLE


def _fetch_with_playwright_sync(url: str, use_system_chrome: bool = False) -> Tuple[Optional[str], int, str]:
    """
    ★ 在同一个线程内完成：浏览器启动(如需) + 页面抓取。
    通过线程锁保证同一时刻只有一个 executor 线程操作 Playwright 浏览器。
    解决 Playwright sync API 不允许跨线程使用浏览器的限制。
    """
    global _PW_AVAILABLE, _PW_BROWSER, _PW_USE_SYSTEM_CHROME

    lock = _get_pw_lock()
    with lock:
        # 确保浏览器已启动（在持锁线程内）。如果 channel 模式变化则重建浏览器
        need_restart = (
            _PW_BROWSER is not None and _PW_USE_SYSTEM_CHROME != use_system_chrome
        )
        if need_restart:
            try:
                _PW_BROWSER.close()
            except Exception:
                pass
            _PW_BROWSER = None
            agent_logger.info(
                f"[FetcherRouter] channel 模式切换: "
                f"{'系统Chrome' if use_system_chrome else 'Playwright自带Chromium'}"
            )

        if _PW_BROWSER is None:
            try:
                from playwright.sync_api import sync_playwright
                pw = sync_playwright().start()
                launch_kwargs = {
                    "headless": True,
                    "args": [
                        "--no-sandbox",
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                }
                if use_system_chrome:
                    launch_kwargs["channel"] = "chrome"
                    agent_logger.info("[FetcherRouter] 使用系统安装的真实 Chrome 浏览器")
                _PW_BROWSER = pw.chromium.launch(**launch_kwargs)
                _PW_USE_SYSTEM_CHROME = use_system_chrome
                agent_logger.info("[FetcherRouter] Playwright Chromium 已启动")
            except Exception as e:
                agent_logger.warning(f"[FetcherRouter] Playwright 启动失败: {e}")
                _PW_AVAILABLE = False
                return None, 0, str(e)

        # 抓取页面
        try:
            page = _PW_BROWSER.new_page()
            page.set_default_timeout(30000)

            # ★ 注入 stealth 反检测脚本（必须在 goto 之前）
            _apply_stealth(page)

            # ★ 设置视口大小（随机选择常见分辨率）
            page.set_viewport_size({
                "width": random.choice([1366, 1440, 1920, 1536]),
                "height": 768,
            })

            page.set_extra_http_headers({
                "User-Agent": _random_ua(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Upgrade-Insecure-Requests": "1",
                "Cache-Control": "no-cache",
            })
            # ★ SPA 渲染策略：domcontentloaded + 内容稳定轮询
            #   networkidle 对 Vue/React SPA 不可靠：
            #   - 数据 API 刚返回时 Vue 还要一个 tick 才挂 DOM → 提前触发 → 空壳
            #   - 站内有持续后台请求（轮询/统计）→ 永不触发 → 30s 超时
            #   改为 DOM 加载后轮询正文长度，连续 2 次不变即视为渲染完成
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            http_status = response.status if response else 0

            # 内容稳定等待（最长 12s）
            try:
                page.wait_for_function(
                    """() => {
                        const el = document.querySelector('#app') || document.body;
                        const len = (el.innerText || '').length;
                        if (!window.__pw_len) { window.__pw_len = len; window.__pw_stable = 0; return false; }
                        if (len === window.__pw_len && len > 0) {
                            window.__pw_stable += 1;
                        } else {
                            window.__pw_stable = 0;
                        }
                        window.__pw_len = len;
                        return window.__pw_stable >= 2;
                    }""",
                    timeout=12000,
                )
            except Exception:
                # 超时就用当前内容（慢页面至少拿到部分渲染结果，不再整体失败）
                agent_logger.info(f"[FetcherRouter] 内容稳定等待超时，使用当前 DOM | {url[:60]}")

            # ★ 模拟人类滚动（替换原有简单 scrollTo）
            _simulate_human_scroll(page)

            # ★ Stealth 覆盖度自检日志
            try:
                stealth_result = page.evaluate("""() => {
                    const wd = navigator.webdriver;
                    const pluginsLen = navigator.plugins ? navigator.plugins.length : -1;
                    const hasChromeRuntime = typeof window.chrome !== 'undefined' && window.chrome && typeof window.chrome.runtime !== 'undefined';
                    return JSON.stringify({wd: wd, plugins: pluginsLen, chromeRuntime: hasChromeRuntime});
                }""")
                import json
                sc = json.loads(stealth_result)
                agent_logger.info(
                    f"[Stealth Check] webdriver={sc.get('wd')}, "
                    f"plugins={sc.get('plugins')}, "
                    f"chrome.runtime={'存在' if sc.get('chromeRuntime') else '不存在'}"
                )
            except Exception as sc_err:
                agent_logger.warning(f"[Stealth Check] 自检失败: {sc_err}")

            html = page.content()
            page.close()
            return html, http_status, ""
        except Exception as e:
            return None, 0, str(e)


# ============================================================================
# FetcherRouter 实现
# ============================================================================

class HttpxPlaywrightFetcher(FetcherRouterInterface):
    """
    基于 httpx + Playwright 降级的抓取器。

    决策逻辑:
      1. 先检查 URL 记忆库 (UrlMemory)
      2. needs_js_render=False → httpx 直接抓取
      3. needs_js_render=True  → Playwright 渲染
      4. httpx 返回内容 < 500 字节 → 自动触发 Playwright 降级
    """

    def __init__(self, request_delay: float = 1.0, use_system_chrome: bool = False) -> None:
        self._memory = UrlMemory()
        self._semaphore = asyncio.Semaphore(5)  # 并发控制
        self._playwright_checked = False
        self.request_delay = request_delay
        self.use_system_chrome = use_system_chrome

    def configure(self, request_delay: float = None, use_system_chrome: bool = None) -> None:
        """运行时更新反爬配置参数"""
        if request_delay is not None:
            self.request_delay = request_delay
        if use_system_chrome is not None:
            self.use_system_chrome = use_system_chrome

    # ==================================================================
    # fetch — 单页抓取
    # ==================================================================

    async def fetch(self, url: str, profile: SiteProfile) -> PageData:
        """
        抓取单个页面。根据 SiteProfile 选择最优策略。
        """
        # 1. 检查缓存
        if self._memory.is_visited(url):
            cached_html = self._memory.get_cached_html(url)
            if cached_html and len(cached_html) >= 500:
                agent_logger.info(f"[FetcherRouter] 缓存命中: {url[:80]}")
                return await self._html_to_pagedata(url, cached_html, "cached")

        # ★ 请求延迟：模拟人类浏览节奏（Base_Delay * 随机波动系数）
        delay = self.request_delay * random.uniform(0.8, 1.5)
        agent_logger.info(f"[LangGraph MA] 模拟人类思考中... 延迟 {delay:.2f} 秒")
        await asyncio.sleep(delay)

        # 2. 确定抓取策略
        if profile.needs_js_render:
            return await self._fetch_playwright(url)
        else:
            return await self._fetch_httpx(url, profile)

    # ==================================================================
    # fetch_batch — 并发批量抓取
    # ==================================================================

    async def fetch_batch(
        self, urls: List[str], profile: SiteProfile, concurrency: int = 5
    ) -> AsyncIterator[PageData]:
        """
        并发抓取多个 URL，边抓取边产出 PageData。
        """
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _fetch_one(url: str) -> PageData:
            async with semaphore:
                return await self.fetch(url, profile)

        tasks = [asyncio.create_task(_fetch_one(u)) for u in urls]

        for task in asyncio.as_completed(tasks):
            try:
                result = await task
                yield result
            except Exception as e:
                url = ""  # 无法追踪失败 URL
                agent_logger.warning(f"[FetcherRouter] fetch_batch 某一任务失败: {e}")
                yield PageData(
                    url=url,
                    html="",
                    fetch_method="failed",
                    content_quality_score=0.0,
                )

    # ==================================================================
    # 内部方法
    # ==================================================================

    async def _fetch_httpx(self, url: str, profile: SiteProfile) -> PageData:
        """策略 A: httpx 异步请求（带重试）"""
        client = _get_httpx_client()
        last_error = ""

        for attempt in range(3):
            try:
                # ★ 每次 httpx 请求前随机延迟
                hdelay = self.request_delay * random.uniform(0.8, 1.5)
                agent_logger.info(f"[LangGraph MA] 模拟人类思考中... 延迟 {hdelay:.2f} 秒 (httpx)")
                await asyncio.sleep(hdelay)

                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": _random_ua(),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                        "Accept-Encoding": "gzip, deflate, br",
                        "Cache-Control": "no-cache",
                        "Sec-Fetch-Site": "none",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-User": "?1",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                html = resp.text

                # ★ 捕获响应 Cookie（供后续图片下载绕过防盗链）
                resp_cookies = {}
                try:
                    for name, value in resp.cookies.items():
                        resp_cookies[name] = value
                except Exception:
                    pass

                # 短 HTML → 自动降级 Playwright
                if len(html) < 500 and not profile.needs_js_render:
                    agent_logger.info(f"[FetcherRouter] httpx 返回 {len(html)} 字节（过短），降级 Playwright | {url[:80]}")
                    return await self._fetch_playwright(url)

                # ★ 骨架 HTML 检测：JS 渲染站（链接极少 + 正文极短）→ 降级 Playwright
                if not profile.needs_js_render and len(html) >= 500:
                    try:
                        soup = BeautifulSoup(html, "html.parser")
                        link_count = len(soup.find_all("a", href=True))
                        body = soup.find("body")
                        body_text_len = len(body.get_text(" ", strip=True)) if body else 0
                        if link_count < 10 and body_text_len < 400:
                            profile.needs_js_render = True
                            agent_logger.info(
                                f"[FetcherRouter] 检测到骨架 HTML (links={link_count}, body_text={body_text_len})，"
                                f"已标记 needs_js_render=True，降级 Playwright | {url[:80]}"
                            )
                            return await self._fetch_playwright(url)
                    except Exception:
                        pass

                # 标记已访问（缓存长内容）
                _parsed = urlparse(url)
                _base = f"{_parsed.scheme}://{_parsed.netloc}"
                if len(html) >= 500:
                    self._memory.mark_visited(url, status="success", base_url=_base, html_content=html)
                else:
                    self._memory.mark_visited(url, base_url=_base)

                return await self._html_to_pagedata(url, html, "httpx", cookies=resp_cookies)

            except httpx.TimeoutException:
                last_error = f"httpx 超时 (attempt {attempt + 1}/3)"
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                last_error = str(e)
                await asyncio.sleep(2 ** attempt)

        # 3 次重试均失败 → 降级 Playwright
        agent_logger.warning(f"[FetcherRouter] httpx 3次失败 ({last_error})，降级 Playwright")
        return await self._fetch_playwright(url)

    async def _fetch_playwright(self, url: str) -> PageData:
        """策略 B: Playwright 渲染。使用单线程 executor 避免 greenlet 跨线程错误"""
        self._playwright_checked = True

        if not _check_playwright_import():
            agent_logger.error("[FetcherRouter] Playwright 未安装")
            return PageData(
                url=url, html="",
                fetch_method="failed", content_quality_score=0.0,
            )

        # ★ 单线程 executor：Playwright 的 greenlet 绑定到创建线程，必须始终在同一线程调用
        if not hasattr(self, "_pw_executor"):
            import concurrent.futures
            self._pw_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        loop = asyncio.get_running_loop()
        try:
            # ★ Playwright 抓取前随机延迟
            pdelay = self.request_delay * random.uniform(0.8, 1.5)
            agent_logger.info(f"[LangGraph MA] 模拟人类思考中... 延迟 {pdelay:.2f} 秒 (playwright)")
            await asyncio.sleep(pdelay)

            html, http_status, err = await loop.run_in_executor(
                self._pw_executor, _fetch_with_playwright_sync, url, self.use_system_chrome
            )
            if html and len(html) >= 100:
                _parsed = urlparse(url)
                _base = f"{_parsed.scheme}://{_parsed.netloc}"
                self._memory.mark_visited(url, status="success", base_url=_base, html_content=html)
                return await self._html_to_pagedata(url, html, "playwright")
            else:
                raise RuntimeError(err or "Playwright 返回空内容")
        except Exception as e:
            err_msg = str(e)
            # ★ 检测 Playwright 超时 → 可能为反爬拦截
            if "timeout" in err_msg.lower():
                agent_logger.warning(
                    f"[FetcherRouter] Playwright 超时（高级反爬爬不了）: {url[:80]}"
                )
                return PageData(
                    url=url, html="",
                    fetch_method="anti_crawl_blocked", content_quality_score=0.0,
                    extra={"_anti_crawl_reason": f"高级反爬爬不了: {err_msg[:200]}"},
                )
            agent_logger.error(f"[FetcherRouter] Playwright 抓取失败: {e}")
            return PageData(
                url=url, html="",
                fetch_method="playwright_failed", content_quality_score=0.0,
            )

    async def _html_to_pagedata(
        self, url: str, html: str, method: str, cookies: dict = None
    ) -> PageData:
        """将原始 HTML 转为 PageData，提取基础信息"""
        # 异步解析标题（在线程池中执行，避免阻塞事件循环）
        loop = asyncio.get_running_loop()
        title = await loop.run_in_executor(None, _extract_title, html)

        extra = {}
        if cookies:
            extra["_cookies"] = cookies

        return PageData(
            url=url,
            title=title,
            html=html,
            raw_html=html,
            fetch_method=method,
            timestamp="",
            extra=extra,
        )


# ============================================================================
# 辅助函数
# ============================================================================

def _extract_title(html: str) -> str:
    """从 HTML 中提取标题（同步，供 executor 调用）"""
    try:
        soup = BeautifulSoup(html, "html.parser")
        # 优先 h1
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            return h1.get_text(strip=True)[:300]
        # 其次 title
        title_tag = soup.find("title")
        if title_tag and title_tag.get_text(strip=True):
            return title_tag.get_text(strip=True)[:300]
    except Exception:
        pass
    return ""
