"""
全站爬取主程序 — 按导航栏层级生成目录 + 图片绝对化 + HTML 清洗
用法：python site_crawler.py
"""
import os
import re
import time
import json
import requests
import urllib3
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
from typing import Dict, Any, List, Optional, Tuple

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 配置 ====================
REQUEST_DELAY = 0.8
MAX_PAGES = 200
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
            ".css", ".js", ".pdf", ".doc", ".docx", ".xls", ".zip", ".rar",
            ".mp3", ".mp4", ".avi")

SKIP_SCHEMES = ("mailto:", "javascript:", "tel:", "ftp:")

_UNIFIED_CSS = """
/* ===== 全局重置：强制覆盖原站样式，防止内容重叠 ===== */
* {
    position: static !important;
    float: none !important;
    left: auto !important;
    right: auto !important;
    top: auto !important;
    bottom: auto !important;
}
body {
    font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    color: #333;
    line-height: 1.8;
    background: #fff;
    margin: 0;
    padding: 20px;
}
.content-wrapper { max-width: 860px; margin: 0 auto; word-wrap: break-word; overflow-wrap: break-word; }
h1 { text-align: center; font-size: 24px; margin: 20px 0 30px 0; }
h2 { font-size: 20px; margin: 20px 0 12px 0; }
h3 { font-size: 18px; margin: 16px 0 10px 0; }
p { font-size: 16px; line-height: 2; text-indent: 2em; margin: 8px 0; }
p:has(img) { text-indent: 0; }
img { max-width: 100% !important; height: auto !important; display: block !important; margin: 12px auto !important; }
ul, ol { font-size: 16px; line-height: 2; margin: 8px 0; padding-left: 2em; }
li { margin-bottom: 5px; }
table { max-width: 100%; border-collapse: collapse; margin: 12px auto; }
table td, table th { border: 1px solid #ddd; padding: 8px; font-size: 14px; }
a { color: #1a73e8; text-decoration: underline; word-break: break-all; }
.row, [class*="col-"], .flex, [class*="flex"] { display: block !important; flex: none !important; flex-wrap: nowrap !important; }
@media (max-width: 767px) { body { padding: 10px; } }
"""


# ==================== 网络请求 ====================

def fetch(url: str, retries: int = 4, timeout: int = 15) -> Optional[str]:
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
            if resp.status_code == 429:
                wait = (attempt + 1) * 8
                print(f"  [429] {wait}秒后重试({attempt+1}/{retries})...")
                time.sleep(wait)
                continue
            if 400 <= resp.status_code < 500:
                return None
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code}")
            resp.raise_for_status()
            if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "latin-1"):
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except Exception:
            if attempt < retries - 1:
                time.sleep((attempt + 1) * 4)
    return None


# ==================== 导航菜单提取 ====================

def _extract_li_children(li: BeautifulSoup, base_url: str, max_depth: int,
                         _current_depth: int, seen: set) -> List[Dict]:
    """
    递归提取 <li> 标签内部嵌套子菜单（<ul>/<ol>/<dl> → <li>/<dd> → <a>）
    用于 extract_nav_menu 的递归调用，支持 N 级菜单
    """
    if _current_depth >= max_depth:
        return []

    children = []
    for container in li.find_all(["dl", "ul", "ol"], recursive=False):
        for sub_item in container.find_all(["dd", "li"], recursive=False):
            # 跳过深层嵌套（子元素内的 <li> 交给递归处理）
            if sub_item.parent and sub_item.parent != container:
                continue
            sub_a = sub_item.find("a", href=True)
            if not sub_a:
                continue
            sub_name = sub_a.get_text(strip=True).lstrip(">").lstrip(">").strip()
            sub_href = urljoin(base_url, sub_a.get("href", ""))
            if not sub_name or not sub_href or len(sub_name) > 30:
                continue
            sub_norm = sub_href.rstrip("/")
            if sub_norm in seen:
                continue
            seen.add(sub_norm)
            # 递归提取更深的子菜单
            sub_children = _extract_li_children(sub_item, base_url, max_depth,
                                                _current_depth + 1, seen)
            children.append({"name": sub_name, "url": sub_href, "children": sub_children})
    return children


def extract_nav_menu(soup: BeautifulSoup, base_url: str,
                     max_depth: int = 3, _current_depth: int = 0) -> List[Dict]:
    """提取主导航菜单树（支持任意深度嵌套菜单，默认最大 3 级）"""
    # 超过最大深度 → 返回空
    if _current_depth >= max_depth:
        return []

    nav = None
    for el in soup.find_all(["div", "ul"]):
        cls = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
        if any(kw in cls.lower() for kw in ["nav", "menu", "navbar"]):
            nav = el
            break
    if not nav:
        nav = soup.find("body") or soup

    # 在导航容器内找最外层 <ul>/<ol>，只取直接子 <li>（不递归提取二级菜单）
    top_ul = nav.find(["ul", "ol"])
    if top_ul:
        li_source = top_ul.find_all("li", recursive=False)
    else:
        # 无 <ul> → 在整个容器中取最上层 <li>
        li_source = []
        for child in nav.find_all(recursive=False):
            if child.name == "li":
                li_source.append(child)

    menu = []
    seen = set()

    for li in li_source:

        first_a = None
        p_tag = li.find("p", recursive=False)
        if p_tag:
            first_a = p_tag.find("a", href=True)
        if not first_a:
            first_a = li.find("a", href=True, recursive=False)
        if not first_a:
            first_a = li.find("a", href=True)
        if not first_a:
            continue

        name = first_a.get_text(strip=True)
        href = first_a.get("href", "")
        if not name or not href or href == "#" or name in ("首页", "Home", "home", "网站首页", ""):
            continue
        if len(name) > 30:
            continue

        abs_url = urljoin(base_url, href)
        norm = abs_url.rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)

        children = _extract_li_children(li, base_url, max_depth, _current_depth + 1, seen)

        menu.append({"name": name, "url": abs_url, "children": children})

    return menu


# ==================== HTML 清洗 ====================

def clean_html(html: str, page_url: str) -> str:
    """
    清洗 HTML：
    - 去头：删除 <header>、class/id 含 header/nav/navbar/top-bar/menu 的节点
    - 去尾：删除 <footer>、class/id 含 footer/bottom/copyright 的节点
    - 去侧边栏：删除 class/id 含 sidebar/side/aside/right-bar/left-bar/widget 的节点
    - 保留完整 HTML 结构（p, h1-h6, ul, li, table, img 等）
    - 图片 src 绝对化（含懒加载 data-src, data-original 等）
    - 图片不下载，保留外链
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()

    for el in list(soup.find_all(["header", "nav"])):
        el.decompose()

    _HEADER_PATTERNS = ["header", "nav", "navbar", "top-bar", "topbar",
                        "menu", "head-", "headfull", "headpublic", "webnav"]
    for el in list(soup.find_all(["div", "section", "ul"])):
        if el.attrs is None:
            continue
        cls_id = (" ".join(el.get("class", [])) + " " + (el.get("id") or "")).lower()
        if any(p in cls_id for p in _HEADER_PATTERNS):
            el.decompose()

    for el in list(soup.find_all("footer")):
        el.decompose()

    _FOOTER_PATTERNS = ["footer", "footer-bottom", "footer-top",
                        "copyright", "copyright-info",
                        "footbar", "foot-nav", "foot-navbar",
                        "site-footer", "page-footer"]
    for el in list(soup.find_all(["div", "section"])):
        if el.attrs is None:
            continue
        cls_id = (" ".join(el.get("class", [])) + " " + (el.get("id") or "")).lower()
        text = el.get_text(strip=True)
        if any(p in cls_id for p in _FOOTER_PATTERNS):
            el.decompose()
        elif len(text) < 500 and any(kw in text for kw in ["版权", "Copyright", "ICP备", "备案号"]):
            el.decompose()

    for el in list(soup.find_all("aside")):
        el.decompose()

    _SIDEBAR_PATTERNS = ["sidebar", "side-bar", "side_bar", "aside",
                         "right-bar", "left-bar", "widget", "modal",
                         "popup", "dialog", "overlay", "tooltip"]
    for el in list(soup.find_all(["div", "section"])):
        if el.attrs is None:
            continue
        cls_id = (" ".join(el.get("class", [])) + " " + (el.get("id") or "")).lower()
        if any(p in cls_id for p in _SIDEBAR_PATTERNS):
            el.decompose()

    for el in list(soup.find_all(style=re.compile(r"position\s*:\s*(fixed|sticky)", re.I))):
        el.decompose()

    content = None
    # ★ 优先语义标签和常见正文容器，包含 .main 类名匹配
    for sel in ["article", "main", ".main", ".content", ".main-content",
                "[class*=content]", "[class*=main-content]", "[id*=content]",
                "[class*=article]", "[class*=detail]", "[class*=body]",
                "[class*=main]", "[id*=main]"]:
        for el in soup.select(sel):
            if len(el.get_text(strip=True)) > 50:
                content = el
                break
        if content:
            break

    if not content:
        body = soup.find("body")
        if body:
            best, best_txt = None, 0
            for el in body.find_all(["div", "section"]):
                txt = el.get_text(strip=True)
                if len(txt) > best_txt:
                    best_txt = len(txt)
                    best = el
            content = best or body
        else:
            content = soup

    # ★ 先解包/替换 <a> 标签，后修复图片路径（确保 <a> 内 <img> 不丢失）
    for a_tag in content.find_all("a"):
        if a_tag.find("img") or a_tag.find("video") or a_tag.find("picture") or a_tag.find("svg"):
            a_tag.unwrap()
        else:
            span_tag = soup.new_tag("span")
            span_tag.string = a_tag.get_text()
            span_tag["style"] = "color: inherit !important; text-decoration: none !important; cursor: default; display: inline;"
            a_tag.replace_with(span_tag)

    _fix_images(content, page_url)

    title = _extract_title(content) or urlparse(page_url).path.split("/")[-1] or "page"
    has_h1 = content.find("h1") is not None
    title_block = f"<h1>{title}</h1>" if not has_h1 else ""

    body_html = str(content)
    return f"""<!-- url: {page_url} -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="referrer" content="no-referrer">
<title>{title}</title>
<style>
{_UNIFIED_CSS}
</style>
</head>
<body>
<div class="content-wrapper">
{title_block}
{body_html}
</div>
</body>
</html>"""


def _extract_title(content) -> str:
    for tag in ("h1", "h2"):
        h = content.find(tag)
        if h and len(h.get_text(strip=True)) > 1:
            return h.get_text(strip=True)
    p = content.find("p")
    if p and len(p.get_text(strip=True)) > 2:
        return p.get_text(strip=True)[:50]
    return ""


def _is_placeholder(url: str) -> bool:
    """判断 URL 是否为占位图 / 懒加载过渡图"""
    if not url:
        return False
    lower = url.lower()
    placeholders = [
        "1x1", "blank.gif", "blank.png", "loading.gif", "loading.svg",
        "placeholder", "spacer.gif", "spacer.png", "transparent",
        "pixel.gif", "pixel.png", "empty.gif", "grey.gif",
        "ajax-loader", "lazy-placeholder", "grey-pixel", "white-pixel",
    ]
    return any(p in lower for p in placeholders)


def _fix_images(content, page_url: str):
    """图片路径绝对化，保留所有 <img> 标签，处理懒加载、CSS 背景图、SVG 图片"""
    # ===== 1. 处理 <img> 标签 + 懒加载 =====
    for img in content.find_all("img"):
        src = None

        for attr in ["data-src", "data-original", "data-original-src",
                     "data-lazy-src", "data-url", "data-img", "data-pic",
                     "data-bg", "data-background", "src"]:
            val = img.get(attr, "").strip()
            if val and not val.startswith("data:"):
                if _is_placeholder(val):
                    continue
                src = val
                break

        # 如果 <img> 在 <picture> 内，优先取 <source> 的 srcset
        if (not src or _is_placeholder(img.get("src", ""))) \
                and img.parent and img.parent.name == "picture":
            for source in img.parent.find_all("source"):
                for attr_name in ["srcset", "data-srcset"]:
                    srcset_val = source.get(attr_name, "").strip()
                    if srcset_val:
                        first_part = srcset_val.split(",")[0].strip().split(" ")[0]
                        if first_part and not first_part.startswith("data:") \
                                and not _is_placeholder(first_part):
                            src = first_part
                            break
                if src:
                    break

        if not src:
            continue

        if src.startswith("//"):
            src = "https:" + src
        elif not src.startswith(("http://", "https://")):
            src = urljoin(page_url, src)

        img["src"] = src

        for attr in ["data-src", "data-original", "data-original-src",
                     "data-lazy-src", "data-url", "data-img", "data-pic",
                     "data-bg", "data-background", "srcset", "data-srcset"]:
            if img.has_attr(attr):
                del img[attr]

        for attr in ["width", "height"]:
            if img.has_attr(attr) and img.get(attr, "").isdigit():
                del img[attr]

        img["referrerpolicy"] = "no-referrer"
        img["loading"] = "eager"

    # ===== 2. 处理 <input type="image"> =====
    for btn in content.find_all("input", {"type": "image"}):
        src_val = btn.get("src", "").strip()
        if src_val and not src_val.startswith(("http://", "https://", "data:")):
            btn["src"] = urljoin(page_url, src_val)

    # ===== 3. 处理 <svg> 内嵌 <image> =====
    for svg_img in content.find_all("image"):
        href = svg_img.get("href") or svg_img.get("xlink:href") or ""
        if href and not href.startswith(("http://", "https://", "data:")):
            abs_href = urljoin(page_url, href)
            if svg_img.has_attr("href"):
                svg_img["href"] = abs_href
            if svg_img.has_attr("xlink:href"):
                svg_img["xlink:href"] = abs_href

    # ===== 4. 处理 <picture> 中的 <source> srcset =====
    for picture in content.find_all("picture"):
        for source in picture.find_all("source"):
            for attr in ["srcset", "data-srcset"]:
                val = source.get(attr, "").strip()
                if not val:
                    continue
                parts = re.split(r',\s*', val)
                new_parts = []
                for part in parts:
                    tokens = part.strip().split()
                    if not tokens:
                        continue
                    url_token = tokens[0]
                    if url_token.startswith("//"):
                        url_token = "https:" + url_token
                    elif not url_token.startswith(("http://", "https://", "data:")):
                        url_token = urljoin(page_url, url_token)
                    tokens[0] = url_token
                    new_parts.append(" ".join(tokens))
                source[attr] = ", ".join(new_parts)

    # ===== 5. 处理 CSS background-image =====
    bg_re = re.compile(r'background(?:-image)?\s*:\s*url\(["\']?([^"\'()]+)["\']?\)', re.I)
    for el in list(content.find_all(style=True)):
        style = el.get("style", "")
        if "background" not in style.lower():
            continue
        for bg_url in bg_re.findall(style):
            bg_url = bg_url.strip()
            if not bg_url or bg_url.startswith("data:"):
                continue
            abs_url = bg_url if bg_url.startswith(("http://", "https://")) else urljoin(page_url, bg_url)
            for quote in ['"', "'", ""]:
                old = f'url({quote}{bg_url}{quote})'
                new = f'url({quote}{abs_url}{quote})'
                if old in style:
                    style = style.replace(old, new, 1)
                    break
            el["style"] = style


# ==================== 公司名称提取 ====================

def extract_company_name(soup: BeautifulSoup, domain: str) -> str:
    """
    从首页 HTML 提取公司名称，优先级（逐级尝试）：
    1. <title> 按分隔符拆分，取非"首页"的部分
    2. <meta name="description"> 中用正则匹配"XX公司/集团/企业"模式
    3. <meta name="keywords"> 中取第一个关键词
    4. class/id 含 "logo" 的元素内的文字/alt
    5. 页脚版权信息中 © 后面的文字
    6. <h1> 中含"公司/集团/企业"关键词的文字
    7. 最终兜底：域名
    """
    source = "unknown"
    result = None
    COMPANY_PATTERN = re.compile(r'[\u4e00-\u9fa5()（）\w]{3,}(?:公司|集团|企业|工厂|中心|研究所|有限公司|有限责任)')

    # ===== 1. <title> 分隔符提取（排除"首页"） =====
    title = soup.find("title")
    if title and title.string:
        text = title.string.strip()
        # 标题为"首页"/"Home"/纯英文短词 → 跳过
        if text not in ("首页", "Home", "home", "网站首页") and len(text) > 1:
            for sep in (" - ", " | ", " _ ", "-", "|", "_"):
                if sep in text:
                    parts = [p.strip() for p in text.split(sep)]
                    for part in parts:
                        if part not in ("首页", "Home", "home") and 2 <= len(part) <= 50:
                            result = part
                            source = "title(split)"
                            break
                    if result:
                        break
            if result is None and 2 <= len(text) <= 50:
                result = text
                source = "title(full)"

    # ===== 2. <meta description / og:description 正则匹配公司名 =====
    if result is None:
        meta_desc = (
            soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
            or soup.find("meta", attrs={"property": "og:description"})
        )
        if meta_desc and meta_desc.get("content"):
            desc = meta_desc["content"].strip()
            m = COMPANY_PATTERN.search(desc)
            if m:
                result = m.group()
                source = "meta_description(regex)"

    # ===== 3. <meta name="keywords"> 第一个关键词 =====
    if result is None:
        meta_kw = soup.find("meta", attrs={"name": "keywords"})
        if meta_kw and meta_kw.get("content"):
            kws = meta_kw["content"].strip()
            first_kw = kws.split(",")[0].strip()
            if 2 <= len(first_kw) <= 30:
                result = first_kw
                source = "meta_keywords"

    # ===== 4. Logo 元素文字/alt（过滤无意义词） =====
    if result is None:
        _LOGO_BLACKLIST = {"logo", "banner", "image", "图片", "img", "icon", "pic", "photo"}
        for el in soup.find_all(["img", "div", "span", "a"]):
            cls = " ".join(el.get("class", [])) if el.get("class") else ""
            eid = el.get("id", "") or ""
            if "logo" in (cls + " " + eid).lower():
                alt = el.get("alt", "").strip()
                if 2 <= len(alt) <= 30 and alt.lower() not in _LOGO_BLACKLIST:
                    result = alt
                    source = "logo_alt"
                    break
                txt = el.get_text(strip=True)
                if 2 <= len(txt) <= 30 and txt.lower() not in _LOGO_BLACKLIST:
                    result = txt
                    source = "logo_text"
                    break

    # ===== 5. 页脚版权信息 © 后面 =====
    if result is None:
        footer = soup.find("footer") or soup.find(class_=re.compile(r"footer|foot|copyright", re.I))
        if footer:
            f_text = footer.get_text()
            copy_match = re.search(r'[©\u00a9]\s*(\S.*?)(?:\s|$)', f_text)
            if copy_match:
                candidate = copy_match.group(1).strip()[:30]
                if 2 <= len(candidate) <= 30:
                    result = candidate
                    source = "footer(copy)"

    # ===== 6. <h1> 含公司关键词 =====
    if result is None:
        for h in soup.find_all(["h1", "h2"]):
            h_text = h.get_text(strip=True)
            if any(kw in h_text for kw in ("公司", "集团", "企业", "工厂", "中心", "研究")):
                m = COMPANY_PATTERN.search(h_text)
                if m:
                    result = m.group()
                    source = "h1_company_kw"
                    break

    # ===== 7. 域名兜底 =====
    if result is None:
        result = _safe_filename(domain)
        source = "domain(fallback)"

    print(f"[DEBUG] 公司名提取来源: {source}, 结果: {result}")
    return result


# ==================== 目录路径生成 ====================

def menu_to_folder_map(menu: List[Dict]) -> List[Tuple[str, str]]:
    """
    从导航菜单提取一级导航名 → 基准URL 的列表。

    Returns: [(导航文件夹名, 基准URL), ...]
    示例: [("关于我们", "https://test.com/about"), ("联系我们", "https://test.com/contact")]
    """
    result = []
    for item in menu:
        name = _safe_filename(item["name"])
        url = item["url"].rstrip("/")
        result.append((name, url))
    return result


def classify_url(url: str, base_url: str, nav_folders: List[Tuple[str, str]]) -> List[str]:
    """
    根据 URL 匹配到唯一的一级导航文件夹。

    输入:
      url: 页面完整 URL
      base_url: 网站根 URL（如 https://nfexpo.com）
      nav_folders: [(导航名, 基准URL), ...]

    输出:
      [文件夹名, 文件名.html]

    规则:
      1. 首页：url 等于 base_url 或 base_url + "/" → ["首页", "首页.html"]
      2. 精确匹配：url_no_slash == nav_url → 文件名 = 导航名.html
      3. 前缀匹配：url_no_slash.startswith(nav_url + "/") → 文件名取自 URL 末段
      4. 兜底：归入最后一个导航文件夹
    """
    url_no_slash = url.rstrip("/")
    base_no_slash = base_url.rstrip("/")

    # 1. 首页
    if url_no_slash == base_no_slash:
        result = ["首页", "首页.html"]
        print(f"[DEBUG] classify: {url[:80]} → {result}")
        return result

    # 2. 按基准 URL 长度降序匹配（优先精确匹配，再前缀匹配）
    for folder_name, nav_url in sorted(nav_folders, key=lambda x: -len(x[1])):
        if url_no_slash == nav_url:
            result = [folder_name, folder_name + ".html"]
            print(f"[DEBUG] classify: {url[:80]} → {result}")
            return result
        if url_no_slash.startswith(nav_url + "/"):
            parsed = urlparse(url)
            path_last = parsed.path.strip("/").split("/")[-1]
            if path_last and path_last not in ("", "index.html", "index.htm", "index.asp", "index.php", "index"):
                filename = _safe_filename(path_last) + ".html"
            else:
                filename = folder_name + ".html"
            result = [folder_name, filename]
            print(f"[DEBUG] classify: {url[:80]} → {result}")
            return result

    # 3. 兜底：归入最后一个导航文件夹
    if nav_folders:
        last_name = nav_folders[-1][0]
    else:
        last_name = "首页"
    parsed = urlparse(url)
    path_last = parsed.path.strip("/").split("/")[-1]
    if path_last:
        result = [last_name, _safe_filename(path_last) + ".html"]
    else:
        result = [last_name, "page.html"]
    print(f"[DEBUG] classify: {url[:80]} → {result}")
    return result


def _safe_filename(name: str) -> str:
    """安全文件名：去除非法字符、首尾空格、连续空格，空名兜底为 未命名"""
    if not name:
        return "未命名"
    # 去除首尾空格
    name = name.strip()
    # 替换非法文件名字符
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    # 合并连续空格为单个空格
    name = re.sub(r'\s+', ' ', name)
    # 去除首尾点号
    name = name.strip().strip(".")
    # 清理后若为空，使用兜底命名
    if not name:
        return "未命名"
    return name[:60]


# ==================== 保存文件 ====================

def save_html(content: str, path_segs: List[str], output_root: str = "output"):
    """path_segs 如 ["关于我们", "总经理寄语.html"]"""
    dir_path = os.path.join(output_root, *path_segs[:-1])
    os.makedirs(dir_path, exist_ok=True)
    filename = path_segs[-1]
    if not filename.endswith(".html"):
        filename += ".html"
    filepath = os.path.join(dir_path, filename)
    counter = 1
    while os.path.exists(filepath):
        name, ext = os.path.splitext(filename)
        filepath = os.path.join(dir_path, f"{name}_{counter}{ext}")
        counter += 1
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


# ==================== 主流程 ====================

# ==================== URL 规范化 ====================

_TRACKING_PARAMS = re.compile(r'^(?:utm_|fbclid|gclid|gclsrc|_ga|ref|source)',
                              re.IGNORECASE)


def _normalize_link(href: str, base_url: str) -> Optional[str]:
    """规范化单个链接：绝对化 + 去锚点 + 去追踪参数 + 去尾部斜杠"""
    abs_url = urljoin(base_url, href.strip())
    parsed = urlparse(abs_url)
    # 去锚点
    clean = urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"),
                        parsed.params, "", ""))
    return clean


def _is_same_site(url_netloc: str, base_netloc: str) -> bool:
    """同站判断：www / 非 www 互通 + 子域名允许"""
    if url_netloc == base_netloc:
        return True
    # www.example.com 和 example.com 视为同一站点
    a = url_netloc.replace("www.", "")
    b = base_netloc.replace("www.", "")
    if a == b:
        return True
    # 同主域（如 blog.example.com 归属 example.com）
    if a.endswith("." + b) or b.endswith("." + a):
        return True
    return False


def _extract_all_links(soup: BeautifulSoup, raw_html: str,
                       page_url: str, base_url: str) -> List[str]:
    """
    全面提取页面中的同站链接：
    - <a href>
    - <link rel="canonical" href>
    - <iframe src>, <frame src>
    - <area href>
    - 正则扫描 HTML 中遗漏的 href
    """
    links = []
    base_netloc = urlparse(base_url).netloc.lower()
    seen = set()

    def add(href):
        if not href:
            return
        href = href.strip()
        if any(href.lower().startswith(s) for s in SKIP_SCHEMES):
            return
        if href.lower().endswith(SKIP_EXT):
            return
        if href.startswith("#") or href.startswith("?"):
            return
        try:
            clean = _normalize_link(href, page_url)
        except Exception:
            return
        parsed = urlparse(clean)
        if not _is_same_site(parsed.netloc, base_netloc):
            return
        if clean not in seen:
            seen.add(clean)
            links.append(clean)

    # <a href>
    for a in soup.find_all("a", href=True):
        add(a["href"])
    # <link rel="canonical">
    for lk in soup.find_all("link", rel="canonical", href=True):
        add(lk["href"])
    # <iframe> / <frame>
    for frm in soup.find_all(["iframe", "frame"], src=True):
        add(frm["src"])
    # <area>
    for area in soup.find_all("area", href=True):
        add(area["href"])
    # 正则扫描 HTML 源码中遗漏的 href（JS 动态写入的链接）
    for m in re.finditer(r'''href\s*=\s*["']([^"']+)["']''', raw_html, re.I):
        add(m.group(1))

    return links


def _try_fetch_sitemap(base_url: str) -> List[str]:
    """探测 sitemap.xml 并提取所有 <loc> 链接"""
    urls = []
    for sitemap_path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap.txt"]:
        try:
            resp = requests.get(f"{base_url.rstrip('/')}{sitemap_path}",
                                headers=HEADERS, timeout=10, verify=False)
            if resp.status_code != 200:
                continue
            content = resp.text
            if sitemap_path.endswith(".txt"):
                for line in content.splitlines():
                    u = line.strip()
                    if u.startswith("http"):
                        urls.append(u)
            else:
                soup = BeautifulSoup(content, "xml")
                for loc in soup.find_all("loc"):
                    u = loc.get_text(strip=True)
                    if u:
                        urls.append(u)
            if urls:
                print(f"  📄 从 {sitemap_path} 提取到 {len(urls)} 个链接")
                break
        except Exception:
            pass
    return urls


# ==================== 主流程（重写） ====================

_MAX_PAGES = 500

def crawl(target_url: str, use_llm: bool = False):
    """爬取单个目标网址的全站页面（BFS），返回已处理页面数"""
    _MAX_SINGLE_SITE = 200   # 单站上限
    _MAX_QUEUE_SIZE  = 1000  # 队列容量
    _MAX_URL_DEPTH   = 8     # URL 路径深度上限
    _HEARTBEAT       = 5     # 每 N 页打印一次心跳

    print(f"\n{'='*60}")
    print(f"  █ 开始爬取目标网站")
    print(f"  █ 目标: {target_url}")
    print(f"  █ LLM: {'✅ 已启用' if use_llm else '❌ 默认模式'}")
    print(f"{'='*60}")

    try:
        print("\n[1/5] 获取首页...")
        html = fetch(target_url)
        if not html:
            print("  ❌ 无法访问首页（网络不可达 / 超时 / 服务器拒绝），该网站将被跳过")
            return 0
        soup = BeautifulSoup(html, "html.parser")
        parsed_base = urlparse(target_url)
        base_netloc = parsed_base.netloc.lower()

        # 提取公司名
        from datetime import datetime
        domain = base_netloc.replace("www.", "").replace(":", "_")
        raw_company_name = extract_company_name(soup, domain)
        # 兜底：如果公司名等于域名（纯英文），使用 域名_时间戳 防止多网站覆盖
        if raw_company_name and raw_company_name == _safe_filename(domain):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            raw_company_name = f"{domain}_{timestamp}"
            print(f"[DEBUG] 公司名降级为: {raw_company_name}")
        company_name = _safe_filename(raw_company_name)
        output_root = os.path.join("output", company_name)
        print(f"[DEBUG] 根文件夹名: {company_name}")
        print(f"  公司名: {company_name}")
        print(f"  输出目录: {output_root} (延迟创建)")

        # 提取导航菜单
        print("[2/5] 提取导航菜单...")
        menu = extract_nav_menu(soup, target_url)
        nav_folders = menu_to_folder_map(menu)

        print(f"  一级菜单: {len(menu)} 个")
        for m in menu:
            print(f"    📁 {m['name']} ({len(m['children'])} 子项) → {m['url']}")

        # Sitemap 探测
        print("[3/5] 探测 sitemap...")
        sitemap_links = _try_fetch_sitemap(target_url)
        normalized_sitemap = []
        for u in sitemap_links:
            try:
                if _is_same_site(urlparse(u).netloc.lower(), base_netloc):
                    clean = _normalize_link(u, target_url)
                    if clean and clean not in normalized_sitemap:
                        normalized_sitemap.append(clean)
                else:
                    normalized_sitemap.append(u)
            except Exception:
                pass

        # 初始化 BFS 队列
        visited = set()
        queue = []  # [(url, name, depth)]
        processed = 0
        failed_pages = 0

        def add_to_queue(u, name="", depth=0):
            if len(queue) >= _MAX_QUEUE_SIZE:
                return
            path_slashes = urlparse(u).path.count("/")
            if path_slashes > _MAX_URL_DEPTH:
                return
            if u not in visited:
                visited.add(u)
                queue.append((u, name, depth))

        # 加入首页 + 菜单 + sitemap
        add_to_queue(target_url.rstrip("/"), "首页", 0)
        for m in menu:
            add_to_queue(m["url"].rstrip("/"), m["name"], 1)
        for u in normalized_sitemap:
            add_to_queue(u.rstrip("/"), "sitemap", 2)

        print(f"  待爬取: {len(queue)} 个页面（含菜单 + sitemap）")

        # 首页清洗
        print("\n[4/5] 清洗首页...")
        try:
            home_clean = _process_page(html, target_url, use_llm)
            home_path = save_html(home_clean, ["首页", "首页.html"], output_root)
            print(f"  ✅ 首页 → {home_path}")
        except Exception as e:
            print(f"  ⚠️ 首页清洗异常: {e}")

        # BFS 爬取主循环
        print("\n[5/5] 遍历爬取队列（BFS）...")
        while queue and processed < _MAX_SINGLE_SITE:
            url, name, depth = queue.pop(0)
            processed += 1

            # 心跳日志
            if processed % _HEARTBEAT == 0:
                print(f"  ⏳ 心跳: 已完成 {processed} 页, 队列剩余 {len(queue)} 页")

            print(f"\n  [{processed}/{len(queue)+processed}] {name or url[:60]}: {url[:80]}")

            try:
                page_html = fetch(url)
                if not page_html:
                    print(f"    ❌ 获取失败")
                    failed_pages += 1
                    continue

                cleaned = _process_page(page_html, url, use_llm)

                # ★ 内容质量过滤：文本过少时跳过保存
                if not _check_content_quality(cleaned, url):
                    failed_pages += 1
                    continue

                segs = classify_url(url, target_url, nav_folders)
                filepath = save_html(cleaned, segs, output_root)
                print(f"    ✅ → {filepath}")

                # 提取新链接
                try:
                    sub_soup = BeautifulSoup(page_html, "html.parser")
                    new_links = _extract_all_links(sub_soup, page_html, url, target_url)
                    for link in new_links[:30]:
                        if len(queue) + processed >= _MAX_SINGLE_SITE:
                            break
                        if link not in visited and len(queue) < _MAX_QUEUE_SIZE:
                            visited.add(link)
                            queue.append((link, "", depth + 1))
                except Exception as le:
                    print(f"    ⚠️ 链接提取异常: {le}")

            except Exception as pe:
                print(f"    ❌ 页面处理异常: {pe}")
                failed_pages += 1
                continue

            time.sleep(REQUEST_DELAY)

        # 判断终止原因
        if processed >= _MAX_SINGLE_SITE:
            print(f"\n  ⚠️ 已达到单站上限 {_MAX_SINGLE_SITE} 页，停止爬取")
        elif not queue:
            print(f"\n  ✅ 队列已清空，正常结束")
        print(f"\n{'='*60}")
        print(f"  ✅ {target_url} 爬取完成! 共处理 {processed} 页, 其中失败 {failed_pages} 个")
        print(f"  输出目录: {os.path.abspath(output_root)}")
        print(f"{'='*60}")
        return processed

    except Exception as e:
        print(f"\n❌ 爬取 {target_url} 时发生致命错误: {e}")
        import traceback
        traceback.print_exc()
        return 0


def _process_page(html: str, page_url: str, use_llm: bool) -> str:
    """根据 use_llm 分支处理页面 HTML"""
    return clean_html(html, page_url)  # 当前统一使用规则清洗


# ==================== 内容质量过滤器 ====================

def _check_content_quality(html: str, url: str) -> bool:
    """
    检查清洗后的 HTML 内容质量。
    提取可见文本并计算字符数，低于阈值时返回 False。

    Args:
        html: 清洗后的 HTML 内容
        url:  页面 URL（用于日志）

    Returns:
        True  = 内容质量合格，可以保存
        False = 内容过少，应跳过
    """
    import config
    if not config.CONTENT_QUALITY_FILTER_ENABLED:
        return True
    min_chars = config.CONTENT_QUALITY_MIN_CHARS
    if min_chars <= 0:
        return True

    try:
        soup = BeautifulSoup(html, "html.parser")
        # 去除 script/style 标签后取纯文本
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        # 统计有效字数（排除纯空白）
        char_count = len(text)
        if char_count < min_chars:
            print(f"  ⚠️ [质量过滤] 跳过页面 {url[:100]}，原因：提取内容过少 ({char_count} 字符，阈值 {min_chars})")
            return False
    except Exception as e:
        print(f"  ⚠️ [质量过滤] 检查异常，放行: {e}")
        return True
    return True


# ==================== 爬取前预检 ====================

# 敏感域名黑名单
_BLOCKED_TLD = frozenset([
    ".gov", ".gov.cn", ".mil", ".edu", ".edu.cn", ".ac.cn",
])
_BLOCKED_DOMAIN_KW = [
    "police", "court", "hospital", "bank", "military",
    "gov.", ".gov", "army", "navy", "fbi", "cia",
]

# ==================== 报错信息映射表 ====================
# 将干瘪的技术报错映射为通俗易懂的中文提示

# 高级反爬类型集合（统一显示为 "高级反爬爬不了"）
_ANTI_CRAWL_TYPES = {
    "cloudflare", "waf", "captcha", "403", "access denied",
    "blocked", "challenge", "ddos protection",
}

# 非反爬类报错（网络/SSL/格式错误等，保留原技术提示）
_ERROR_MAP = {
    "404": "❌ [页面丢失] 目标网站页面不存在(404)，请检查网址是否正确，已跳过。",
    "timeout": "⏳ [连接超时] 无法连接到目标服务器，可能是网站已关闭或网络受限，已跳过。",
    "connectionerror": "⏳ [连接超时] 无法连接到目标服务器，可能是网站已关闭或网络受限，已跳过。",
    "connectionrefused": "⏳ [连接超时] 无法连接到目标服务器，可能是网站已关闭或网络受限，已跳过。",
    "sslerror": "🔒 [安全警告] 目标网站SSL证书无效，存在安全隐患，已跳过。",
    "certificate": "🔒 [安全警告] 目标网站SSL证书无效，存在安全隐患，已跳过。",
    "ssl": "🔒 [安全警告] 目标网站SSL证书无效，存在安全隐患，已跳过。",
}

# 反爬统一提示文案
_ANTI_CRAWL_USER_MSG = "高级反爬爬不了"


def get_user_friendly_error(reason: str) -> str:
    """
    统一的用户友好错误提示函数。
    反爬类 → "高级反爬爬不了"
    网络/SSL/格式类 → 保留原技术提示
    其他 → 默认警告前缀
    """
    import logging
    lower = reason.lower()

    # 先检查是否为反爬类型
    for anti_type in _ANTI_CRAWL_TYPES:
        if anti_type in lower:
            logging.debug(f"[AntiCrawl Detail] 类型={anti_type}, reason={reason}")
            return _ANTI_CRAWL_USER_MSG

    # 503 也属于反爬
    if "503" in lower:
        logging.debug(f"[AntiCrawl Detail] 类型=503, reason={reason}")
        return _ANTI_CRAWL_USER_MSG

    # 非反爬类：按原映射表匹配
    for kw, msg in _ERROR_MAP.items():
        if kw in lower:
            return msg

    # 默认保留原信息但加上警告前缀
    return f"⚠️ {reason}"


def _translate_error(reason: str) -> str:
    """将技术报错原因映射为用户友好的中文提示（已委托给 get_user_friendly_error）"""
    return get_user_friendly_error(reason)


def pre_check_url(url: str) -> dict:
    """
    爬取前预检：合规性检测 + 反爬机制检测（保留旧接口兼容性，内部调用 deep_pre_check）。

    Returns:
        {"pass": True,  "url": url, "reason": ""}   — 通过
        {"pass": False, "url": url, "reason": "..."} — 拦截
    """
    return deep_pre_check(url, timeout=10)


def deep_pre_check(url: str, timeout: int = 15) -> dict:
    """
    深度预检（4层体检）：合规性 + 响应体长度 + WAF特征词 + DOM有效性 + 标题异常。

    作为双模式（传统/LangGraph）的统一网关，预检失败时调用方应直接 continue 跳过该网站，
    不生成任何文件夹，不启动任何爬取逻辑。

    Returns:
        {"pass": True,  "url": url, "reason": "预检通过"}  — 通过
        {"pass": False, "url": url, "reason": "..."}       — 拦截
    """
    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    # ===== 1. 合规性：敏感域名黑名单 =====
    if any(domain.endswith(tld) for tld in _BLOCKED_TLD):
        return {"pass": False, "url": url,
                "reason": "疑似政府/军事/教育类网站，禁止爬取"}
    if any(kw in domain for kw in _BLOCKED_DOMAIN_KW):
        return {"pass": False, "url": url,
                "reason": "域名包含敏感关键词（政府/银行/医院等），禁止爬取"}

    # ===== 2. robots.txt 检查已移除：本地工具强制获取所有目标页面 =====

    # ===== 3. HTTP 请求 =====
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=False,
                           allow_redirects=True)
    except requests.exceptions.SSLError:
        return {"pass": False, "url": url,
                "reason": _translate_error("sslerror")}
    except requests.Timeout:
        return {"pass": False, "url": url,
                "reason": _translate_error("timeout")}
    except requests.ConnectionError:
        return {"pass": False, "url": url,
                "reason": _translate_error("connectionerror")}
    except Exception as e:
        err_str = str(e).lower()
        if "ssl" in err_str or "certificate" in err_str:
            return {"pass": False, "url": url,
                    "reason": _translate_error("sslerror")}
        return {"pass": False, "url": url,
                "reason": _translate_error(str(e))}

    # 状态码检测
    if resp.status_code == 404:
        return {"pass": False, "url": url,
                "reason": _translate_error("404")}
    if resp.status_code == 403:
        return {"pass": False, "url": url,
                "reason": _translate_error("403")}
    if resp.status_code == 429:
        return {"pass": False, "url": url,
                "reason": "服务器返回 429，请求频率被限制"}
    if resp.status_code == 503:
        return {"pass": False, "url": url,
                "reason": _translate_error("503")}
    if resp.status_code != 200:
        return {"pass": False, "url": url,
                "reason": _translate_error(f"服务器返回 {resp.status_code}，非正常响应")}

    html = resp.text

    # ===== SPA 空壳检测：JS 渲染站（Vue/React 等），httpx 拿不到正文，放行交给 Playwright 渲染 =====
    _html_lower = html.lower()
    _spa_shell = (
        '<div id="app"' in _html_lower or '<div id="root"' in _html_lower
    ) and (
        '.js' in _html_lower or 'vue' in _html_lower or 'react' in _html_lower
        or 'webpack' in _html_lower or '__next' in _html_lower
    )
    if _spa_shell:
        return {"pass": True, "url": url, "reason": "SPA 站点（JS 渲染），预检通过", "html": html}

    # ===== 🔴 第1层：响应体长度检测（防空白页/极简拦截页） =====
    if len(html) < 1000:
        return {"pass": False, "url": url,
                "reason": f"页面内容过短({len(html)}字节)，疑似拦截页或空白页"}

    # ===== 🔴 第2层：WAF/CDN 挑战页特征词检测 =====
    html_lower = html.lower()
    _WAF_KEYWORDS = [
        "just a moment",
        "checking your browser",
        "cf-browser-verification",
        "cloudflare",
        "captcha",
        "recaptcha",
        "hcaptcha",
        "verify you are human",
        "access denied",
        "ray id",
        "attention required",
        "please enable javascript",
        "ddos protection",
        "blocked",
    ]
    hit_keywords = [kw for kw in _WAF_KEYWORDS if kw in html_lower]
    if hit_keywords:
        return {"pass": False, "url": url,
                "reason": _translate_error("waf/captcha")}

    # ===== 🔴 第3层：DOM 结构有效性检测（防纯 JS 渲染页） =====
    try:
        soup = BeautifulSoup(html, "html.parser")
        body = soup.find("body")
        if body is not None:
            links = body.find_all("a", href=True)
            if len(links) < 3:
                return {"pass": False, "url": url,
                        "reason": f"页面缺乏有效超链接(仅{len(links)}个)，疑似JS渲染或拦截页"}
        else:
            links = soup.find_all("a", href=True)
            if len(links) < 3:
                return {"pass": False, "url": url,
                        "reason": f"页面缺乏有效超链接(仅{len(links)}个)，疑似JS渲染或拦截页"}

        # ===== 🔴 第4层：标题异常检测 =====
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True).lower()
            _BAD_TITLE_KW = ["just a moment", "error", "captcha", "attention",
                            "blocked", "access denied", "challenge"]
            for kw in _BAD_TITLE_KW:
                if kw in title:
                    return {"pass": False, "url": url,
                            "reason": _translate_error(kw)}
    except Exception:
        # BeautifulSoup 解析失败不阻塞，前3层检测已足够
        pass

    # 4层全部通过
    return {"pass": True, "url": url, "reason": "预检通过", "html": html}


# ==================== 入口 ====================

def main(target_url: str, api_key: str = "", base_url: str = "", model_name: str = ""):
    """供外部调用的入口函数（GUI / 命令行均可），返回已处理页面数"""
    # 注入配置到 config 模块（如果提供）
    use_llm = bool(api_key and base_url and model_name)
    if use_llm:
        import config
        config.DEEPSEEK_API_KEY = api_key
        config.DEEPSEEK_BASE_URL = base_url
        config.DEEPSEEK_MODEL = model_name

    url = target_url.strip()
    if not url:
        raise ValueError("网址不能为空")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"无法解析网址: {url}")
    if use_llm:
        print(f"\n🤖 大模型清洗模式已启用")
    print(f"\n解析结果: {url}")
    return crawl(url, use_llm)


if __name__ == "__main__":
    key = input("请输入 API Key（可选，回车跳过）: ").strip()
    base = input("请输入 Base URL（可选，回车跳过）: ").strip()
    model = input("请输入模型名称（可选，回车跳过）: ").strip()
    url = input("请输入要爬取的目标网址 (例如 https://www.example.com): ").strip()
    main(url, key, base, model)
