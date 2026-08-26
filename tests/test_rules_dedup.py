"""tests/test_rules_dedup.py — 清洗规则与去重函数单元测试。

覆盖 graph/nodes.py 关键确定性逻辑：
  - _is_pure_image_product_detail（规则 13：RuiQiCMS 纯图详情页判定）
  - _url_key（URL 去重键：追踪参数过滤 / index.php 归一化 / 双斜杠折叠）
  - _is_pagination_url（分页链接识别，防把 uuid-N 详情页误判成分页）
  - _detail_content_ok（detail 页落盘质量校验：空壳 / 列表容器拦截）
"""

from graph.nodes import (
    _is_pure_image_product_detail,
    _url_key,
    _is_pagination_url,
    _detail_content_ok,
)


# ── 规则 13：纯图片产品详情页 ──

def test_pure_img_detail_positive():
    html = '<div class="product_content_title">豫花37号</div><img src="/a.jpg">'
    assert _is_pure_image_product_detail(html) is True


def test_pure_img_detail_missing_img():
    # 有标题容器但无图 → 不是纯图详情页
    html = '<div class="product_content_title">豫花37号</div>'
    assert _is_pure_image_product_detail(html) is False


def test_pure_img_detail_missing_class():
    html = '<div class="title">豫花37号</div><img src="/a.jpg">'
    assert _is_pure_image_product_detail(html) is False


def test_pure_img_detail_empty():
    assert _is_pure_image_product_detail("") is False
    assert _is_pure_image_product_detail(None) is False


# ── URL 去重键 ──

def test_url_key_strips_tracking_params():
    assert _url_key("http://a.com/p?id=1&utm_source=ad&utm_medium=cpc") == "a.com/p?id=1"


def test_url_key_keeps_significant_params():
    assert _url_key("http://a.com/news?id=8308") == "a.com/news?id=8308"


def test_url_key_normalizes_index_php():
    assert _url_key("http://a.com/index.php/news/1.html") == "a.com/news/1.html"


def test_url_key_collapses_double_slash():
    assert _url_key("http://a.com/index.php//news/1.html") == "a.com/news/1.html"


def test_url_key_equivalent_urls_share_key():
    a = _url_key("http://A.com/news/1.html")
    b = _url_key("https://a.com/news/1.html/")
    assert a == b == "a.com/news/1.html"


def test_url_key_trailing_slash_ignored():
    assert _url_key("http://a.com/") == "a.com"


# ── 分页链接识别 ──

def test_pagination_url_positive():
    assert _is_pagination_url("http://a.com/index_2.html") is True
    assert _is_pagination_url("http://a.com/list_3.shtml") is True
    assert _is_pagination_url("http://a.com/news/default_2.asp") is True


def test_pagination_url_uuid_detail_not_matched():
    # uuid-N 结尾是详情页（TRS CMS），不能误判成分页
    assert _is_pagination_url("http://a.com/products/6ab777a9-5e10-4d4e-9c97-46e22420-37bd.html") is False


def test_pagination_url_plain_detail_not_matched():
    assert _is_pagination_url("http://a.com/news/20240101.html") is False
    assert _is_pagination_url("http://a.com/about.html") is False


# ── detail 页质量校验 ──

def test_detail_content_ok_short_text_rejected():
    assert _detail_content_ok("太短了", "<p>太短了</p>") is False


def test_detail_content_ok_no_article_container():
    # 正文够长且无 article.content 容器 → 放行
    body = "这是一段足够长的正文内容，超过八十个字符，用于验证在没有 article.content 容器时正文能被正常放行落盘保存。" * 2
    assert _detail_content_ok(body, f"<div>{body}</div>") is True


def test_detail_content_ok_empty_shell_rejected():
    # article.content 内只有标题/meta（.ar_tit）无实质正文 → 空壳，拦截
    html = (
        '<article class="content">'
        '<div class="ar_tit"><h1>标题</h1><span>发布时间 2026-01-01 浏览量 12</span></div>'
        "</article>"
    )
    assert _detail_content_ok("x" * 200, html) is False


def test_detail_content_ok_link_dense_list_rejected():
    # 列表容器误当正文：链接密度过高 → 拦截
    links = "".join(f'<a href="/n{i}.html">第{i}条新闻标题</a>' for i in range(10))
    html = f'<article class="content">{links}</article>'
    assert _detail_content_ok("y" * 300, html) is False


def test_detail_content_ok_real_content_accepted():
    # 有 <p> 正文、少量链接 → 放行
    html = (
        '<article class="content">'
        '<div class="ar_tit"><h1>公司新闻</h1></div>'
        '<p>这是一段真实的正文内容，包含足够多的文字信息来支撑一篇新闻稿的完整阅读。</p>'
        '<p>第二段继续补充正文内容，确保总长度超过五十个字符的硬性要求。</p>'
        '</article>'
    )
    assert _detail_content_ok("z" * 200, html) is True
