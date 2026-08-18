"""
通用企业官网正文提取工具
- 基于标签+内容密度的通用正文识别（无需针对单个网站写选择器）
- 图片自动本地化下载
- 输出标准化纯净HTML文件

用法：python clean.py <目标URL>
示例：python clean.py http://www.sjhyzl.com/sjhyzl/bk_21739616.html
"""

import sys, os, re, time, io
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 配置 ====================
OUTPUT_DIR = "output"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# 通用内嵌CSS
_UNIFIED_CSS = """body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; }
.content-wrapper { max-width: 900px; margin: 0 auto; padding: 20px; }
p { font-size: 16px; line-height: 2; text-indent: 2em; margin: 0 0 10px 0; }
p:has(img) { text-indent: 0; }
img { max-width: 100%; height: auto; display: block; margin: 10px auto; }
video { max-width: 100%; display: block; margin: 10px auto; }
ul, ol { font-size: 16px; line-height: 2; }
li { margin-bottom: 5px; }
table { max-width: 100%; border-collapse: collapse; margin: 10px auto; }
table td, table th { border: 1px solid #ddd; padding: 8px; font-size: 14px; }
h1, h2, h3, h4 { margin: 15px 0 10px 0; }
h1 { text-align: center; font-size: 22px; }
h2 { font-size: 20px; }
h3 { font-size: 18px; }
.row { display: flex; flex-wrap: wrap; align-items: flex-start; }
[class*="col-"] { flex: 1; min-width: 280px; padding: 0 15px; }
@media (max-width: 767px) { .row { flex-direction: column; } }"""

# 头部/页脚/噪音特征排除
_EXCLUDE_TAGS = {"header", "nav", "footer", "aside", "script", "style", "noscript", "iframe"}
_EXCLUDE_CLASS_PATTERNS = [
    "header", "footer", "sidebar", "banner",
    "breadcrumb", "copyright", "foot", "top-bar", "topbar",
    "head-", "backtop", "back-top", "zhichi",
    "popup", "modal", "wxinfo", "share",
]
_EXCLUDE_LINK_TAGS = ["link[rel='stylesheet']", "link[rel='shortcut icon']"]


# ==================== 核心函数 ====================

def fetch(url: str, retries: int = 4) -> str | None:
    """下载网页，含重试"""
    for i in range(retries):
        try:
            # 第2次起加 Referer
            h = dict(HEADERS)
            if i > 0:
                h["Referer"] = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
            resp = requests.get(url, headers=h, timeout=30, verify=False)
            if resp.status_code == 429:
                wait = (i + 1) * 8
                print(f"  429 限流，{wait}秒后重试({i+1}/{retries})...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            # 自动编码
            if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "latin-1"):
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except Exception as e:
            if i < retries - 1:
                time.sleep((i + 1) * 3)
    return None


def _should_exclude(el) -> bool:
    """判断元素是否应该被移除（头部/页脚/噪音）
    基于：标签名 + class/id特征 + 链接密度 + 元数据关键字
    """
    if not hasattr(el, "name") or el.name is None or el.attrs is None:
        return False
    # 标签名
    if el.name in _EXCLUDE_TAGS:
        return True
    # class/id 特征
    cls = " ".join(el.get("class", [])) if el.get("class") else ""
    el_id = el.get("id", "")
    combined = (cls + " " + el_id).lower()
    for pat in _EXCLUDE_CLASS_PATTERNS:
        if pat in combined:
            return True
    # 元数据类名排除
    _META_CLASSES = [
        "author-info", "author", "editor", "reviewer",
        "article-meta", "post-meta", "news-meta",
        "info-source", "source", "meta-info",
        "article-footer", "news-footer", "detail-footer",
        "statement", "declare", "disclaimer",
        "qrcode", "qr-code", "wechat", "wx",
        "contact-info", "company-info", "addr",
        "address", "hotline", "service-tel",
    ]
    for pat in _META_CLASSES:
        if pat in combined:
            return True
    # 链接密度：链接文字占比>60%且总文字<500字，判定为导航
    # 但如果包含图片，说明是内容区（如图片列表/证书墙），不是导航
    links = el.find_all("a")
    text = el.get_text(strip=True)
    if links and len(text) > 0:
        link_text = sum(len(l.get_text(strip=True)) for l in links)
        img_cnt = len(el.find_all("img"))
        if link_text / len(text) > 0.6 and len(text) < 500 and img_cnt == 0:
            return True
    # 元数据文本关键字：文案/编辑/审核/咨询电话 等
    if len(text) < 200:
        _META_KEYWORDS = [
            "文案：", "文案:", "编辑：", "编辑:", "责任编辑：", "责任编辑:",
            "审核：", "审核:", "签发：", "签发:", "核对：", "核对:",
            "作者：", "作者:", "来源：", "来源:", "供稿：", "供稿:",
            "咨询电话", "咨询热线", "服务热线",
            "公司地址", "集团地址", "通讯地址", "联系地址",
            "邮政编码", "传真：", "传真:",
            "扫一扫", "扫码关注", "关注我们",
            "返回列表", "上一篇", "下一篇",
        ]
        for kw in _META_KEYWORDS:
            if kw in text:
                return True
    return False


def _strip_noise(soup: BeautifulSoup):
    """全局剔除头部/页脚/脚本等噪音元素"""
    # 标签级移除
    for tag_name in _EXCLUDE_TAGS:
        for el in list(soup.find_all(tag_name)):
            el.decompose()
    # link 标签
    for sel in _EXCLUDE_LINK_TAGS:
        for el in list(soup.select(sel)):
            el.decompose()
    # class/id 特征移除（遍历 div/section/article/ul/ol）
    for el in list(soup.find_all(["div", "section", "article", "ul", "ol"])):
        if hasattr(el, "name") and _should_exclude(el):
            el.decompose()


def _expand_to_significant_parent(el, max_depth: int = 8):
    """
    向上扩展到有意义的最外层容器：
    检查父级的 ALL 子元素（含兄弟列），有更多图片或文本则扩
    若父级内容量相同（纯包裹层），继续向上不中断
    """
    current = el
    for _ in range(max_depth):
        parent = current.parent
        if not parent or parent.name in ("html", "body", "[document]"):
            break
        if _should_exclude(parent):
            break
        cur_txt = len(current.get_text(strip=True))
        par_txt = len(parent.get_text(strip=True))
        cur_img = len(current.find_all("img"))
        par_img = len(parent.find_all("img"))
        # 父级整体（含兄弟列）有更多图片或显著更多文本 → 扩展
        if par_img > cur_img or par_txt > cur_txt * 1.05:
            current = parent
        # 相同内容量（纯包裹层）→ 继续向上，不中断
        elif par_img == cur_img and abs(par_txt - cur_txt) <= 5:
            current = parent
        else:
            break
    return current


def _get_element_position(el, body) -> float:
    """估算元素在 body 中的纵向位置比例（0.0=顶部, 1.0=底部）"""
    if not body:
        return -1.0
    all_els = list(body.descendants)
    total = len(all_els)
    if total == 0:
        return -1.0
    for i, descendant in enumerate(all_els):
        if descendant is el:
            return i / total
    return -1.0


def _detect_content(soup: BeautifulSoup):
    """
    通用正文识别（多策略融合，不依赖特定类名）：
    策略1: <article> 语义标签
    策略2: 类名关键字 + 结构特征加权
    策略3: 文本密度 + 段落/图片比例 综合评分（向上扩展父级）
    策略4: body 降级
    """
    # 策略1: <article> 标签
    article = soup.find("article")
    if article and len(article.get_text(strip=True)) > 30:
        return article

    body = soup.find("body")
    if not body:
        return soup

    # 策略2: 类名关键字 + DOM位置/结构特征 联合评分
    content_kw = ["content", "main", "article", "detail", "body", "pagebody",
                  "text", "news", "info", "post"]
    candidates = []
    for el in body.find_all(["div", "section"]):
        if _should_exclude(el):
            continue
        txt = el.get_text(strip=True)
        if len(txt) < 40:
            continue
        cls_id = " ".join(el.get("class", [])).lower() + " " + (el.get("id") or "").lower()
        kw_hit = any(kw in cls_id for kw in content_kw)
        p_cnt = len(el.find_all("p"))
        img_cnt = len(el.find_all("img"))
        pos = _get_element_position(el, body)

        score = len(txt) * 0.5 + p_cnt * 100 + img_cnt * 250
        if kw_hit:
            score *= 1.5
        if 0.15 <= pos <= 0.85:
            score *= 1.2
        if score > 200:
            candidates.append((score, el))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        # 向上扩展到有意义的最外层容器
        container = _expand_to_significant_parent(candidates[0][1])
        if container and len(container.get_text(strip=True)) > 40:
            return container
        return candidates[0][1]

    # 策略3: 全局文本密度评分
    best_el, best_score = None, 0
    body_txt_len = len(body.get_text(strip=True))
    for el in body.find_all(["div", "section", "article"]):
        if _should_exclude(el):
            continue
        txt = el.get_text(strip=True)
        if len(txt) < 20:
            continue
        if body_txt_len > 0 and len(txt) > body_txt_len * 0.8:
            is_body_self = el is body or (el.parent and el.parent is body)
            if is_body_self:
                continue
        p_cnt = len(el.find_all("p"))
        img_cnt = len(el.find_all("img"))
        html_len = len(str(el))
        density = len(txt) / max(html_len, 1)
        score = len(txt) * density * 10 + p_cnt * 80 + img_cnt * 200
        if score > best_score and len(txt) > 30:
            best_score = score
            best_el = el

    if best_el:
        return best_el

    # 策略4: body 兜底
    return body


def _download_images(content, page_url: str, img_dir: str):
    """下载正文中的图片到本地 images/ 目录，替换 src"""
    os.makedirs(img_dir, exist_ok=True)
    idx = 0
    parsed_page = urlparse(page_url)
    source_domain = f"{parsed_page.scheme}://{parsed_page.netloc}"
    for img in content.find_all("img"):
        src = (img.get("data-src") or img.get("data-original-src") 
               or img.get("url") or img.get("src") or "")
        if not src:
            continue
        # 转绝对URL
        abs_url = urljoin(page_url, src)
        # 跳过 data: 和空链接
        if abs_url.startswith("data:") or not abs_url.startswith("http"):
            continue
        # 确定扩展名
        path = urlparse(abs_url).path
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"):
            ext = ".jpg"
        idx += 1
        fname = f"{idx}{ext}"
        fpath = os.path.join(img_dir, fname)
        # 下载（带Referer防盗链 + 源站回退）
        img_headers = dict(HEADERS)
        img_headers["Referer"] = page_url
        downloaded = False
        # 要尝试的URL列表：原URL + 源站同路径（若不同域）
        try_urls = [abs_url]
        if urlparse(abs_url).netloc != parsed_page.netloc:
            try_urls.append(source_domain + urlparse(abs_url).path)
        for try_url in try_urls:
            try:
                img_resp = requests.get(try_url, headers=img_headers, timeout=20, verify=False)
                if img_resp.status_code == 200:
                    with open(fpath, "wb") as f:
                        f.write(img_resp.content)
                    img["src"] = f"./images/{fname}"
                    downloaded = True
                    break
            except Exception:
                continue
        if not downloaded:
            img["src"] = abs_url  # 保留远程链接
        # 清理 lazy-load 属性
        for attr in ["data-src", "data-original-src", "data-original", "lazy-src", "url"]:
            if img.has_attr(attr):
                del img[attr]
        img["referrerpolicy"] = "no-referrer"


def _clean_content_links(content, page_url: str):
    """将内容区中的链接 href 转为绝对路径"""
    for a in content.find_all("a", href=True):
        a["href"] = urljoin(page_url, a["href"])


def _fix_css_bg(content, page_url: str):
    """
    CSS 背景图提取：扫描所有元素的内联 style 属性，
    将 background-image: url(...) 中的相对路径补全为绝对路径。
    对 Banner/轮播容器，额外插入 <img> 标签确保图片可见。
    """
    bg_re = re.compile(r'background(?:-image)?\s*:\s*url\(["\']?([^"\'()]+)["\']?\)', re.I)
    banner_patterns = ["banner", "slide", "swiper", "carousel", "hero",
                       "jumbotron", "slider", "ban", "bg-img", "bg-image",
                       "bgimg", "bgimage", "pic", "figure"]

    for el in list(content.find_all(style=True)):
        style = el.get("style", "")
        if not style or "background" not in style.lower():
            continue
        matches = bg_re.findall(style)
        if not matches:
            continue

        cls_str = " ".join(el.get("class", [])).lower() if el.get("class") else ""
        el_id = (el.get("id") or "").lower()
        combined = cls_str + " " + el_id
        is_banner = any(p in combined for p in banner_patterns)

        for bg_url in matches:
            bg_url = bg_url.strip()
            if not bg_url or bg_url.startswith("data:"):
                continue
            abs_url = bg_url if bg_url.startswith(("http://", "https://")) else urljoin(page_url, bg_url)
            for quote in ['"', "'", ""]:
                old_val = f'url({quote}{bg_url}{quote})'
                new_val = f'url({quote}{abs_url}{quote})'
                if old_val in style:
                    style = style.replace(old_val, new_val, 1)
                    break
            el["style"] = style
            if is_banner and not el.find("img"):
                new_img = content.new_tag("img", src=abs_url,
                                          style="width:100%;height:auto;display:block;")
                new_img["referrerpolicy"] = "no-referrer"
                el.insert(0, new_img)

    # 处理 data-background / data-bg 等自定义属性
    for el in list(content.find_all(["div", "section", "li", "figure"])):
        for attr in ["data-background", "data-bg", "data-image", "data-img-src"]:
            val = el.get(attr, "").strip()
            if not val or val.startswith("data:") or val.startswith("#"):
                continue
            is_img_url = any(val.lower().endswith(ext) for ext in
                           [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"])
            has_img_ext = re.search(r'\.(jpe?g|png|gif|webp|bmp|svg)($|\?|#)', val, re.I)
            if not (is_img_url or has_img_ext):
                if not any(kw in val.lower() for kw in ["/upload", "/image", "/img", "/pic", "/photo"]):
                    continue
            abs_url = val if val.startswith(("http://", "https://")) else urljoin(page_url, val)
            cls_str = " ".join(el.get("class", [])).lower() if el.get("class") else ""
            is_banner = any(p in cls_str for p in banner_patterns)
            if is_banner and not el.find("img"):
                new_img = content.new_tag("img", src=abs_url,
                                          style="width:100%;height:auto;display:block;")
                new_img["referrerpolicy"] = "no-referrer"
                el.insert(0, new_img)
            el[attr] = abs_url


def _extract_title(content) -> str:
    """从内容区提取页面标题"""
    # 优先: h1/h2（真正的主标题）
    for tag in ("h1", "h2"):
        h = content.find(tag)
        if h and len(h.get_text(strip=True)) > 1:
            return h.get_text(strip=True)
    # 次选: strong/b/span 中字号>=20px 的短文本，取字号最大的
    candidates = []
    for el in content.find_all(["strong", "b", "span"]):
        txt = el.get_text(strip=True)
        style = el.get("style", "") if el.attrs else ""
        font_match = re.search(r"font-size\s*:\s*(\d+)\s*px", style, re.I)
        if font_match:
            fs = int(font_match.group(1))
            if fs >= 20 and 2 < len(txt) < 50:
                candidates.append((fs, txt))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    # 降级: 内容区第一个有意义的段落
    first_p = content.find("p")
    if first_p:
        t = first_p.get_text(strip=True)
        if len(t) > 2:
            return t[:50]
    return ""


def _extract_time(soup) -> str:
    """从页面提取发布时间"""
    for pat in [r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', r'\d{4}年\d{1,2}月\d{1,2}日']:
        m = re.search(pat, soup.get_text())
        if m:
            return m.group()
    return ""


def process(url: str) -> str:
    """主处理流程：下载→清洗→本地化→输出HTML文件"""
    print(f"正在请求: {url}")
    html = fetch(url)
    if not html:
        print("错误: 无法访问该页面")
        return ""

    soup = BeautifulSoup(html, "html.parser")
    print(f"页面大小: {len(html)} 字符")

    # 1. 去噪音
    _strip_noise(soup)

    # 2. 提取时间（在内容检测前，soup尚未被提取）
    riqi = _extract_time(soup)

    # 3. 检测正文区
    content = _detect_content(soup)
    if not content:
        print("错误: 未找到正文内容")
        return ""
    print(f"正文文本量: {len(content.get_text(strip=True))} 字符, 图片: {len(content.find_all('img'))} 张")

    # 4. 提取标题
    title = _extract_title(content) or urlparse(url).path.split("/")[-1].replace(".html", "")
    print(f"提取标题: {title}")

    # 5. CSS 背景图提取（Banner/轮播图）
    _fix_css_bg(content, url)

    # 6. 图片本地化
    safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:50]
    out_dir = os.path.join(OUTPUT_DIR, safe_title)
    os.makedirs(out_dir, exist_ok=True)
    img_dir = os.path.join(out_dir, "images")
    _download_images(content, url, img_dir) if os.path.exists(out_dir) else None
    _clean_content_links(content, url)

    # 6. 构建HTML
    has_h1 = content.find("h1")
    title_block = f"<h1>{title}</h1>" if not has_h1 else ""
    time_block = f'<p style="color:#999;font-size:14px;text-align:center;margin-bottom:30px;">发布时间：{riqi}</p>' if riqi else ""
    time_comment = f"\n<!-- 发布时间：{riqi} -->" if riqi else ""

    body_html = str(content)
    full_html = f"""<!-- url：{url} -->{time_comment}
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
{time_block}
{body_html}
</div>
</body>
</html>"""

    # 7. 写文件
    out_path = os.path.join(out_dir, f"{safe_title}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(full_html)

    print(f"输出: {out_path}")
    print(f"图片: {img_dir}\\ ({_count_images(content)} 张)")
    return out_path


def _count_images(content) -> int:
    count = 0
    for img in content.find_all("img"):
        src = img.get("src", "")
        if src.startswith("./images/") or src.startswith("images/"):
            count += 1
    return count


# ==================== 入口 ====================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        url = input("请输入目标URL: ").strip()
    else:
        url = sys.argv[1].strip()

    process(url)
