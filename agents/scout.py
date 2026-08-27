"""
Agent 1: ScoutAgent — 侦察兵

职责: 接收种子 URL，分析首页 HTML 特征。
输出: SiteProfile (JS渲染需求 / 站点类型 / 反爬强度)
"""

from __future__ import annotations

import re
import asyncio
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .models import SiteProfile
from .interfaces import ScoutAgent as ScoutAgentInterface

from schemas import agent_logger
import config


class PageScout(ScoutAgentInterface):
    """
    基于首页 HTML 分析的站点侦察器。

    分析维度:
      1. JS 渲染需求: 首页 requests 拿回的文本 < 500 字节 → needs_js_render=True
      2. 站点类型:   基于 HTML meta / body 结构判断 cms / spa / portal / blog / ecommerce / other
      3. 反爬强度:   检测验证码、Cloudflare、频率限制等信号
      4. 编码检测:   从 <meta charset> 或响应头提取
    """

    # 站点类型特征库
    _CMS_SIGNATURES = [
        "wordpress", "wp-content", "wp-json", "joomla", "drupal",
        "dedecms", "帝国", "powered by",
    ]
    _SPA_SIGNATURES = [
        "react", "vue", "angular", "__next", "nuxt", "webpack",
        'id="app"', 'id="root"', "create-react-app",
    ]
    _ECOMMERCE_SIGNATURES = [
        "shop", "cart", "product", "price", "buy", "购买",
        "加入购物车", "商品", "shopify", "woocommerce",
    ]
    _PORTAL_SIGNATURES = [
        "portal", "category", "频道", "栏目", "专题",
    ]
    _BLOG_SIGNATURES = [
        "blog", "post", "article", "archives", "tag",
    ]

    async def analyze(self, url: str) -> SiteProfile:
        """分析目标站点首页，返回 SiteProfile"""
        agent_logger.info(f"[ScoutAgent] 开始分析站点: {url}")

        # 1. 抓取首页
        html, fetch_method = await self._fetch_homepage(url)

        # 2. 分析各维度
        needs_js = self._detect_js_dependency(html)
        site_type = self._classify_site(html, url)
        anti_crawl = self._assess_anti_crawl(html, url)
        title, encoding = self._extract_meta(html)

        profile = SiteProfile(
            url=url,
            title=title,
            needs_js_render=needs_js,
            site_type=site_type,
            anti_crawl_level=anti_crawl,
            encoding=encoding,
            has_captcha=self._detect_captcha(html),
            extra={
                "fetch_method": fetch_method,
                "html_length": len(html),
            },
        )

        agent_logger.info(
            f"[ScoutAgent] 分析完成 | js={needs_js} | type={site_type} | "
            f"anti_crawl={anti_crawl} | html_len={len(html)}"
        )
        return profile

    # ==================================================================
    # 内部方法
    # ==================================================================

    async def _fetch_homepage(self, url: str) -> tuple[str, str]:
        """抓取首页 HTML（httpx 优先，失败降级 Playwright）"""
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            verify=False,
        )

        try:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": config.USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            html = resp.text
            if len(html) >= 200:
                return html, "httpx"
        except Exception as e:
            agent_logger.warning(f"[ScoutAgent] httpx 抓取首页失败: {e}")
        finally:
            await client.aclose()

        # 降级 Playwright
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            from agents.fetcher import _ensure_playwright_sync, _fetch_with_playwright_sync

            pw_ok = await loop.run_in_executor(None, _ensure_playwright_sync)
            if pw_ok:
                html, _, err = await loop.run_in_executor(
                    None, _fetch_with_playwright_sync, url
                )
                if html and len(html) >= 200:
                    return html, "playwright"
                agent_logger.warning(f"[ScoutAgent] Playwright 也失败: {err}")
        except Exception as e:
            agent_logger.warning(f"[ScoutAgent] Playwright 降级异常: {e}")

        return "", "failed"

    def _detect_js_dependency(self, html: str) -> bool:
        """
        检测是否强依赖 JS 渲染。

        判定: 纯文本 < 500 字符 & 存在 common JS 框架引用 → True
        """
        if not html:
            return True  # 空 HTML → 假定需要 JS

        try:
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(" ", strip=True)
        except Exception:
            return True

        # 文本过短 → 很可能是 JS 渲染的空壳
        if len(text) < 200:
            return True

        # 检查是否有 SPA 框架引用
        html_lower = html.lower()
        spa_signals = [
            '<div id="app"', '<div id="root"', 'vue', 'react',
            'angular', 'webpack', '__next',
        ]
        for signal in spa_signals:
            if signal in html_lower:
                if len(text) < 1000:
                    return True  # SPA 但内容很少 → 需要渲染

        return False

    def _classify_site(self, html: str, url: str) -> str:
        """根据 HTML 特征分类站点类型"""
        if not html:
            return "other"

        html_lower = html.lower()

        # 检查各类型特征
        scores = {
            "cms": 0,
            "spa": 0,
            "ecommerce": 0,
            "blog": 0,
            "portal": 0,
        }

        for sig in self._CMS_SIGNATURES:
            if sig in html_lower:
                scores["cms"] += 1
        for sig in self._SPA_SIGNATURES:
            if sig in html_lower:
                scores["spa"] += 2  # SPA 权重更高
        for sig in self._ECOMMERCE_SIGNATURES:
            if sig in html_lower:
                scores["ecommerce"] += 1
        for sig in self._BLOG_SIGNATURES:
            if sig in html_lower:
                scores["blog"] += 1
        for sig in self._PORTAL_SIGNATURES:
            if sig in html_lower:
                scores["portal"] += 1

        # 结构化数据检测 (schema.org)
        if 'itemtype' in html_lower:
            if 'product' in html_lower:
                scores["ecommerce"] += 2
            if 'article' in html_lower or 'blogposting' in html_lower:
                scores["blog"] += 2

        best_type = max(scores, key=scores.get)
        if scores[best_type] == 0:
            # 默认按 URL 推测
            domain = urlparse(url).netloc.lower()
            if any(kw in domain for kw in ["shop", "store", "buy", "mall"]):
                return "ecommerce"
            return "cms"  # 默认假定为 CMS

        return best_type

    def _assess_anti_crawl(self, html: str, url: str) -> str:
        """评估反爬强度: low / medium / high"""
        if not html:
            return "high"

        html_lower = html.lower()
        signals = 0

        # 检测 Cloudflare / 安全防护
        cf_signals = ["cloudflare", "cf-", "__cf", "challenge", "ddos", "browser-check",
                      "turnstile", "datadome", "akamai", "incapsula", "just a moment",
                      "attention required", "please wait", "access denied",
                      "请稍候", "正在检查", "安全验证", "抱歉，请稍候",
                      "blocked", "request blocked", "your request"]
        for s in cf_signals:
            if s in html_lower:
                signals += 2
                break

        # 检测验证码
        captcha_signals = ["captcha", "verify", "验证码", "人机验证", "slider", "geetest",
                           "recaptcha", "hcaptcha", "imgcode", "security code"]
        for s in captcha_signals:
            if s in html_lower:
                signals += 2
                break

        # 检测反爬 meta
        try:
            soup = BeautifulSoup(html, "html.parser")
            meta_robots = soup.find("meta", attrs={"name": "robots"})
            if meta_robots:
                content = meta_robots.get("content", "").lower()
                if "noindex" in content or "nofollow" in content:
                    signals += 1
        except Exception:
            pass

        # 检测 JS 质询
        if "<script" in html_lower and "document.cookie" in html_lower:
            signals += 1

        if signals >= 4:
            return "high"
        elif signals >= 2:
            return "medium"
        return "low"

    def _detect_captcha(self, html: str) -> bool:
        """检测是否包含验证码"""
        if not html:
            return False
        html_lower = html.lower()
        captcha_keywords = ["captcha", "verify", "验证码", "人机验证", "slider", "geetest",
                            "recaptcha", "hcaptcha", "imgcode", "security code", "turnstile"]
        return any(kw in html_lower for kw in captcha_keywords)

    def _extract_meta(self, html: str) -> tuple[str, str]:
        """提取标题和编码"""
        title = ""
        encoding = "utf-8"
        if not html:
            return title, encoding

        try:
            soup = BeautifulSoup(html, "html.parser")
            # 标题
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True)[:200]

            # 编码
            meta_charset = soup.find("meta", attrs={"charset": True})
            if meta_charset:
                encoding = meta_charset.get("charset", "utf-8")
            else:
                meta_http = soup.find("meta", attrs={"http-equiv": re.compile("content-type", re.I)})
                if meta_http:
                    content = meta_http.get("content", "")
                    m = re.search(r"charset=([^\s;]+)", content, re.I)
                    if m:
                        encoding = m.group(1)
        except Exception:
            pass

        return title, encoding
