"""
Agent 2: NavAgent — 领航员 & 过滤器

职责: 解析页面的导航结构，提取带有"导航层级深度(1-4)"和"导航路径(nav_path)"标签的 URL。
      在此阶段进行列表页过滤，只保留详情页 URL。

核心逻辑:
  1. 扫描 <nav> / <header> / 含导航特征的<div> 元素
  2. 基于 DOM 嵌套层级计算 depth (1-4)，生成 nav_path 列表
  3. 列表页过滤: URL特征 + DOM特征(链接密度、缺乏长文本) → 丢弃
  4. 首页过滤: 丢弃指向首页的链接
  5. URL 补全为完整 URL，同域过滤
"""

from __future__ import annotations

import re
import asyncio
from typing import List, Set, Dict
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

from .models import SiteProfile, NavLink
from .interfaces import NavAgent as NavAgentInterface

from schemas import agent_logger


class NavigationParser(NavAgentInterface):
    """
    基于 BeautifulSoup 的导航栏解析器。

    深度计算方法:
      - 扫描 <nav>/<header> 内的 `<ul>/<li>` 嵌套层级
      - 层级 1 = 一级菜单，层级 2 = 二级下拉菜单，以此类推
      - depth 最大为 4
      - nav_path 记录从根到当前链接的完整导航标签链
    """

    # 列表页 URL 特征关键词
    _LIST_URL_KEYWORDS = [
        "list", "lists", "category", "categories", "cat",
        "page", "pages", "archive", "archives",
        "tag", "tags", "search", "query",
        "index", "catalog", "grid",
        # 中文拼音常见
        "liebiao", "fenlei", "mululiebiao",
    ]

    # 列表页 DOM 特征
    _LIST_PAGE_STRUCTURE_SIGNALS = [
        "pagination", "pager", "page-nav", "page-number",
        "list-view", "listview", "grid-view",
    ]

    # 不可抓取的后缀
    _SKIP_EXTENSIONS = {
        ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
        ".css", ".js", ".json", ".xml", ".rss", ".atom",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".zip", ".tar", ".gz", ".rar", ".7z",
        ".mp4", ".mp3", ".avi", ".mov", ".wmv",
    }

    async def extract_links(
        self, html: str, profile: SiteProfile, current_depth: int = 0
    ) -> List[NavLink]:
        """
        从 HTML 中提取导航链接，过滤后只保留详情页。

        Args:
            html:           页面 HTML (通常是首页)
            profile:        站点画像 (ScoutAgent 输出)
            current_depth:  当前页面的导航深度（首页为 0）

        Returns:
            NavLink 列表，仅包含详情页链接（is_detail_page=True, is_homepage=False）
        """
        if not html:
            agent_logger.warning("[NavAgent] 输入 HTML 为空")
            return []

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._extract_sync, html, profile, current_depth
        )

    def _extract_sync(
        self, html: str, profile: SiteProfile, current_depth: int
    ) -> List[NavLink]:
        """同步提取逻辑（在线程池中执行）"""
        soup = BeautifulSoup(html, "html.parser")
        base_url = profile.url

        # 1. 找到所有导航区域
        nav_containers = self._find_nav_containers(soup)

        # 2. 从导航区域递归提取链接
        all_links: List[NavLink] = []
        seen_urls: Set[str] = set()

        for container in nav_containers:
            links = self._extract_from_container(
                container, base_url, current_depth, [], seen_urls
            )
            all_links.extend(links)

        # 3. 如果没有找到导航区域，从 body 中提取所有 <a> 作为兜底
        if not all_links:
            agent_logger.info("[NavAgent] 未找到标准导航区域，从 body 提取链接")
            body = soup.find("body")
            if body:
                all_links = self._extract_from_body(body, base_url, seen_urls)

        # 4. 过滤: 去重、去首页、去列表页
        filtered = self._filter_links(all_links, base_url)

        agent_logger.info(
            f"[NavAgent] 链路提取完成 | 原始={len(all_links)} | "
            f"过滤后={len(filtered)} (详情页)"
        )
        return filtered

    # ==================================================================
    # 导航区域定位
    # ==================================================================

    def _find_nav_containers(self, soup: BeautifulSoup) -> List[Tag]:
        """定位页面中的所有导航区域"""
        containers: List[Tag] = []

        # 1. <nav> 标签 (最高优先级)
        for nav in soup.find_all("nav"):
            containers.append(nav)

        # 2. <header> 内的导航
        for header in soup.find_all("header"):
            navs = header.find_all("nav")
            if navs:
                containers.extend(navs)
            else:
                # header 本身可能包含菜单
                containers.append(header)

        # 3. id/class 含导航语义的 div
        nav_patterns = [
            "nav", "menu", "navigation", "navbar", "nav-bar",
            "main-menu", "top-menu", "primary-menu",
        ]
        for kw in nav_patterns:
            for el in soup.find_all(["div", "ul"], class_=re.compile(kw, re.I)):
                if el not in containers:
                    containers.append(el)
            for el in soup.find_all(["div", "ul"], id=re.compile(kw, re.I)):
                if el not in containers:
                    containers.append(el)

        # 去重（保持插入顺序）
        seen = set()
        unique = []
        for c in containers:
            if id(c) not in seen:
                seen.add(id(c))
                unique.append(c)

        return unique

    # ==================================================================
    # 链接提取
    # ==================================================================

    def _extract_from_container(
        self,
        container: Tag,
        base_url: str,
        parent_depth: int,
        parent_path: List[str],
        seen_urls: Set[str],
    ) -> List[NavLink]:
        """
        从导航容器中递归提取链接，构建 nav_path 和 depth。

        核心逻辑: 遍历 <li> / <a> 结构，<ul> 嵌套代表更深一层菜单。
        """
        links: List[NavLink] = []
        current_depth = parent_depth + 1

        if current_depth > 4:
            return links

        # 查找直接子 <li>（优先）或 <a>（兜底）
        li_elements = []
        for child in container.find_all(["li"], recursive=False):
            li_elements.append(child)
        if not li_elements:
            for child in container.find_all(["ul", "ol"], recursive=False):
                li_elements.extend(child.find_all("li", recursive=False))

        if li_elements:
            for li in li_elements:
                link = self._process_li(li, base_url, current_depth, parent_path, seen_urls)
                if link:
                    links.append(link)
        else:
            # 没有 <li> 结构，直接提取 <a>
            for a in container.find_all("a", href=True, recursive=True):
                link = self._process_link(a, base_url, current_depth, parent_path, seen_urls)
                if link:
                    links.append(link)

        return links

    def _process_li(
        self,
        li: Tag,
        base_url: str,
        depth: int,
        parent_path: List[str],
        seen_urls: Set[str],
    ) -> NavLink | None:
        """处理单个 <li> 元素"""
        # 提取该层级的标签文本
        a_tag = li.find("a", href=True, recursive=False)
        if not a_tag:
            a_tag = li.find("a", href=True)
        if not a_tag:
            return None

        text = self._clean_text(a_tag.get_text(" ", strip=True))
        if not text:
            return None

        href = a_tag.get("href", "").strip()
        full_url = self._normalize_url(href, base_url)
        if not full_url:
            return None

        # 去重
        url_key = self._url_key(full_url)
        if url_key in seen_urls:
            return None
        seen_urls.add(url_key)

        # 构建 nav_path
        nav_path = list(parent_path) + [text]

        # 判定页面类型
        is_detail, _ = self._classify_page(full_url, text)
        is_home = self._is_homepage(full_url, base_url)

        return NavLink(
            url=full_url,
            text=text,
            nav_path=nav_path,
            depth=depth,
            is_detail_page=is_detail,
            is_homepage=is_home,
        )

    def _process_link(
        self,
        a: Tag,
        base_url: str,
        depth: int,
        parent_path: List[str],
        seen_urls: Set[str],
    ) -> NavLink | None:
        """处理单个 <a> 标签"""
        text = self._clean_text(a.get_text(" ", strip=True))
        if not text:
            return None

        href = a.get("href", "").strip()
        full_url = self._normalize_url(href, base_url)
        if not full_url:
            return None

        url_key = self._url_key(full_url)
        if url_key in seen_urls:
            return None
        seen_urls.add(url_key)

        nav_path = list(parent_path) + [text]
        is_detail, _ = self._classify_page(full_url, text)
        is_home = self._is_homepage(full_url, base_url)

        return NavLink(
            url=full_url,
            text=text,
            nav_path=nav_path,
            depth=depth,
            is_detail_page=is_detail,
            is_homepage=is_home,
        )

    def _extract_from_body(
        self, body: Tag, base_url: str, seen_urls: Set[str]
    ) -> List[NavLink]:
        """兜底: 从 body 提取所有 <a> 链接"""
        links: List[NavLink] = []
        for a in body.find_all("a", href=True):
            link = self._process_link(a, base_url, 1, [], seen_urls)
            if link:
                links.append(link)
        return links

    # ==================================================================
    # URL 处理
    # ==================================================================

    def _normalize_url(self, href: str, base_url: str) -> str:
        """补全 URL 为完整格式，过滤不可抓取的协议/后缀"""
        if not href:
            return ""

        href = href.strip()

        # 过滤不可抓取的协议
        if href.startswith(("javascript:", "mailto:", "tel:", "ftp:", "#")):
            return ""

        # 补全 URL
        full_url = urljoin(base_url, href)

        # 过滤非 HTTP 协议
        parsed = urlparse(full_url)
        if parsed.scheme not in ("http", "https"):
            return ""

        # 过滤非 HTML 后缀
        path_lower = parsed.path.lower()
        for ext in self._SKIP_EXTENSIONS:
            if path_lower.endswith(ext):
                return ""

        # 去除 fragment
        return urlunparse(parsed._replace(fragment=""))

    def _is_homepage(self, url: str, base_url: str) -> bool:
        """判断是否为首页链接"""
        parsed = urlparse(url)
        base_parsed = urlparse(base_url)

        # 不同域名 → 不是本站首页（也过滤掉外链）
        if parsed.netloc != base_parsed.netloc:
            return False

        # 路径为空或只有 "/"
        path = parsed.path.rstrip("/")
        if not path or path == base_parsed.path.rstrip("/"):
            return True

        # 路径为 /index.html /index.php /home 等
        homepage_paths = ["/index", "/home", "/default", "/main"]
        for hp in homepage_paths:
            if path.lower().endswith(hp):
                return True

        return False

    def _url_key(self, url: str) -> str:
        """生成 URL 去重键（保留查询参数，过滤追踪参数）"""
        from urllib.parse import parse_qsl, urlencode
        parsed = urlparse(url)
        path = parsed.path.rstrip("/").lower()
        if parsed.query:
            TRACKING = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term',
                        'utm_content', '_t', '_', 't', 'token', 'session', 'sid',
                        'random', '_dc', 'nocache', 'v', 'ver', 'timestamp', 'ts'}
            params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                      if k.lower() not in TRACKING]
            if params:
                query = urlencode(sorted(params))
                return f"{parsed.netloc}{path}?{query}".rstrip("/").lower()
        return f"{parsed.netloc}{path}".rstrip("/").lower()

    # ==================================================================
    # 页面分类: 详情页 vs 列表页
    # ==================================================================

    def _classify_page(self, url: str, text: str) -> tuple[bool, str]:
        """
        基于 URL + 链接文本判定页面类型。

        Returns: (is_detail, reason)
          - is_detail=True  → 保留
          - is_detail=False → 列表页，过滤丢弃
        """
        url_lower = url.lower()
        text_lower = text.lower()
        reasons = []

        # ── 检查 0: PHP CMS / 通用详情页强信号（优先于列表关键词） ──
        # 避免 catid= 中的 "cat" 子串被误判为列表页
        # 匹配: a=show&...&id=123 | a=show&id=123 | ?id=123&a=show 等
        if re.search(r'[?&]a=show\b', url_lower) and re.search(r'[?&]id=\d+', url_lower):
            return True, ""

        # ── 检查 1: URL 特征 ──
        for kw in self._LIST_URL_KEYWORDS:
            if kw in url_lower:
                # 特殊处理: "page" 经常出现在详情页 URL 中
                if kw == "page":
                    # "page/123" 或 "page-123" 更可能是分页列表
                    if re.search(r"page[/-]\d+", url_lower):
                        reasons.append(f"URL含列表特征('{kw}')")
                        break
                elif kw == "category":
                    if "/category/" in url_lower or "category_id" in url_lower:
                        reasons.append(f"URL含列表特征('{kw}')")
                        break
                else:
                    reasons.append(f"URL含列表特征('{kw}')")
                    break

        # ── 检查 2: 链接文本特征 ──
        list_text_keywords = ["更多", "全部", "所有", "查看全部", "查看更多",
                             "more", "all", "view all", "read more"]
        for kw in list_text_keywords:
            if kw in text_lower:
                reasons.append(f"链接文本含列表导向词('{kw}')")
                break

        if reasons:
            return False, "; ".join(reasons)

        # ── 检查 3: URL 含常见详情页模式 (强证据) ──
        detail_patterns = [
            r"/\d{4}/\d{2}/\d{2}/",     # /2024/01/15/
            r"/\d{4}-\d{2}-\d{2}",       # /2024-01-15
            r"/\d{5,}",                  # /12345 (长数字ID)
            r"/detail[/-]",              # /detail/
            r"/article[/-]",             # /article/
            r"/news[/-]\d",              # /news/123
            r"/info[/-]\d",              # /info/123
            r"\.html$",                  # .html 结尾
            r"\.shtml$",
        ]
        for pat in detail_patterns:
            if re.search(pat, url_lower):
                return True, ""

        # ── 默认: 文本长度≥3且看起来像标题 → 保留 ──
        if len(text) >= 3:
            return True, ""

        return True, ""  # 默认保留，由 ExtractorAgent 二次拦截

    # ==================================================================
    # 过滤
    # ==================================================================

    def _filter_links(self, links: List[NavLink], base_url: str) -> List[NavLink]:
        """
        对提取的链接进行终过滤:
          1. 去掉首页链接
          2. 去掉列表页（但 depth=1 的一级导航全部保留，由后续 BFS 处理）
          3. 去掉非本站域名的外链
          4. 同 URL 去重
        """
        base_domain = urlparse(base_url).netloc
        seen_urls: Set[str] = set()
        filtered: List[NavLink] = []

        for link in links:
            # 跳过外链
            if urlparse(link.url).netloc != base_domain:
                continue

            # 去重
            url_key = self._url_key(link.url)
            if url_key in seen_urls:
                continue
            seen_urls.add(url_key)

            # 跳过首页
            if link.is_homepage:
                continue

            # 跳过列表页 — 但 depth=1 的一级导航全部保留
            if not link.is_detail_page and link.depth > 1:
                continue

            filtered.append(link)

        return filtered

    # ==================================================================
    # 辅助
    # ==================================================================

    @staticmethod
    def _clean_text(text: str) -> str:
        """清洗链接文本"""
        # 去除空白符、换行
        text = re.sub(r'\s+', ' ', text).strip()
        # 去除首尾特殊符号
        text = text.strip('|·•-─→»> ')
        # 限制长度
        if len(text) > 80:
            text = text[:77] + "..."
        return text
