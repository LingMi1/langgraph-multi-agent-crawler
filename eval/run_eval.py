"""
Phase 3: 自动化评估脚本 (Eval)

读取 golden_dataset.json，逐个 URL 调用 tools.py 中的工具进行抓取和提取，
将实际输出与 expected_data 对比，计算准确率并输出评估报告。

用法:
    cd eval && python run_eval.py
    python eval/run_eval.py
"""

import json
import os
import sys
import time
from difflib import SequenceMatcher
from typing import Dict, Any, List

# 确保项目根目录在 Python path 中
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools import fetch_page, clean_and_extract
from schemas import agent_logger


def _load_golden_dataset() -> Dict[str, Any]:
    """加载 golden dataset"""
    json_path = os.path.join(os.path.dirname(__file__), "golden_dataset.json")
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _string_similarity(a: str, b: str) -> float:
    """计算两个字符串的相似度 (0.0 ~ 1.0)"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _evaluate_single(test_case: Dict[str, Any]) -> Dict[str, Any]:
    """
    评估单个测试用例:
    1. 调用 fetch_page 抓取 HTML
    2. 调用 clean_and_extract 提取结构化数据
    3. 与 expected_data 对比

    返回: {"id": str, "url": str, "passed": bool, "details": [...]}
    """
    case_id = test_case.get("id", "unknown")
    url = test_case.get("url", "")
    expected = test_case.get("expected_data", {})
    description = test_case.get("description", "")
    details = []

    agent_logger.info(f"[Eval] 开始测试 {case_id}: {url} | {description}")

    # Step 1: 抓取页面
    fetch_start = time.time()
    fetch_result_str = fetch_page.invoke({"url": url})
    fetch_elapsed = time.time() - fetch_start

    try:
        fetch_result = json.loads(fetch_result_str)
    except json.JSONDecodeError:
        agent_logger.error(f"[Eval] {case_id} fetch_page 返回非法 JSON")
        return {
            "id": case_id,
            "url": url,
            "passed": False,
            "details": [{"field": "fetch", "status": "FAIL", "reason": "fetch_page 返回非法 JSON"}]
        }

    if not fetch_result.get("success"):
        details.append({
            "field": "fetch",
            "status": "FAIL",
            "reason": fetch_result.get("error", "抓取失败"),
            "http_status": fetch_result.get("http_status", 0),
            "elapsed": f"{fetch_elapsed:.1f}s"
        })
        agent_logger.warning(f"[Eval] {case_id} 抓取失败: {fetch_result.get('error')}")
        return {"id": case_id, "url": url, "passed": False, "details": details}

    html = _get_html_from_fetch(fetch_result, url)
    if not html:
        details.append({"field": "fetch", "status": "FAIL", "reason": "HTML 内容为空"})
        return {"id": case_id, "url": url, "passed": False, "details": details}

    details.append({
        "field": "fetch",
        "status": "PASS",
        "html_length": len(html),
        "elapsed": f"{fetch_elapsed:.1f}s"
    })

    # Step 2: 清洗 + 提取结构化数据
    extract_start = time.time()
    extract_result_str = clean_and_extract.invoke({"url": url, "html": html})
    extract_elapsed = time.time() - extract_start

    try:
        extract_result = json.loads(extract_result_str)
    except json.JSONDecodeError:
        details.append({"field": "extract", "status": "FAIL", "reason": "clean_and_extract 返回非法 JSON"})
        return {"id": case_id, "url": url, "passed": False, "details": details}

    if not extract_result.get("success"):
        details.append({
            "field": "extract",
            "status": "FAIL",
            "reason": extract_result.get("validation_error", "提取失败")
        })
        return {"id": case_id, "url": url, "passed": False, "details": details}

    article = extract_result.get("article", {})
    details.append({"field": "extract", "status": "PASS", "elapsed": f"{extract_elapsed:.1f}s"})

    # Step 3: 对比 expected_data
    all_field_passed = True

    # 3a. 标题相似度
    expected_title = expected.get("title", "")
    actual_title = article.get("title", "")
    if expected_title:
        sim = _string_similarity(expected_title, actual_title)
        title_passed = sim >= 0.6  # 60% 相似度即认为通过
        if not title_passed:
            all_field_passed = False
        details.append({
            "field": "title",
            "status": "PASS" if title_passed else "FAIL",
            "expected": expected_title,
            "actual": actual_title,
            "similarity": f"{sim:.2f}"
        })
    else:
        details.append({"field": "title", "status": "SKIP", "actual": actual_title})

    # 3b. 发布时间前缀匹配
    expected_time = expected.get("publish_time", "")
    actual_time = article.get("publish_time", "")
    if expected_time:
        time_passed = actual_time.startswith(expected_time) if actual_time else False
        if not time_passed:
            all_field_passed = False
        details.append({
            "field": "publish_time",
            "status": "PASS" if time_passed else "FAIL",
            "expected_prefix": expected_time,
            "actual": actual_time
        })
    else:
        details.append({"field": "publish_time", "status": "SKIP", "actual": actual_time})

    # 3c. 面包屑包含检查
    expected_breadcrumb = expected.get("breadcrumb", [])
    actual_breadcrumb = article.get("breadcrumb", [])
    if expected_breadcrumb:
        # 检查 actual 是否包含所有 expected 元素
        breadcrumb_passed = all(
            any(_string_similarity(exp, act) >= 0.6 for act in actual_breadcrumb)
            for exp in expected_breadcrumb
        )
        if not breadcrumb_passed:
            all_field_passed = False
        details.append({
            "field": "breadcrumb",
            "status": "PASS" if breadcrumb_passed else "FAIL",
            "expected": expected_breadcrumb,
            "actual": actual_breadcrumb
        })
    else:
        details.append({"field": "breadcrumb", "status": "SKIP", "actual": actual_breadcrumb})

    # 3d. 图片数量检查
    expected_min_images = expected.get("min_images", 0)
    actual_images = article.get("images_count", 0)
    if expected_min_images > 0:
        img_passed = actual_images >= expected_min_images
        if not img_passed:
            all_field_passed = False
        details.append({
            "field": "images_count",
            "status": "PASS" if img_passed else "FAIL",
            "expected_min": expected_min_images,
            "actual": actual_images
        })
    else:
        details.append({"field": "images_count", "status": "SKIP", "actual": actual_images})

    return {"id": case_id, "url": url, "passed": all_field_passed, "details": details}


def _get_html_from_fetch(fetch_result: dict, url: str) -> str:
    """
    从 fetch_page 的返回结果中获取 HTML。
    注：fetch_page 返回的是摘要信息，实际 HTML 内容较大。
    需要直接调用底层抓取逻辑。

    这里复用 tools.py 中的 fetch 逻辑重新获取原始 HTML。
    """
    import requests
    import random
    import urllib3
    from urllib.parse import urlparse
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

    urllib3.disable_warnings()

    _USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    ]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError, requests.HTTPError)),
        reraise=True,
    )
    def _do_fetch(target_url: str) -> requests.Response:
        session = requests.Session()
        parsed_url = urlparse(target_url)
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.3",
            "Referer": parsed_url._replace(path='/', query='', fragment='').geturl(),
        }
        resp = session.get(target_url, headers=headers, timeout=15, verify=False)
        if 400 <= resp.status_code < 500 and resp.status_code not in (429,):
            return resp
        if resp.status_code == 429:
            raise requests.HTTPError("429 Too Many Requests", response=resp)
        resp.raise_for_status()
        return resp

    try:
        resp = _do_fetch(url)
        if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "latin-1"):
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except Exception:
        return ""


def run_evaluation(verbose: bool = True) -> Dict[str, Any]:
    """
    执行完整评估流程。

    Returns:
      {"total": int, "passed": int, "failed": int, "accuracy": float, "results": [...]}
    """
    dataset = _load_golden_dataset()
    test_cases = dataset.get("test_cases", [])

    if not test_cases:
        print("❌ 未找到测试用例")
        return {"total": 0, "passed": 0, "failed": 0, "accuracy": 0.0, "results": []}

    print(f"\n{'='*60}")
    print(f"  📋 Phase 3: 自动化评估")
    print(f"  测试用例数: {len(test_cases)}")
    print(f"{'='*60}\n")

    results = []
    passed_count = 0

    for i, test_case in enumerate(test_cases):
        print(f"  [{i+1}/{len(test_cases)}] 测试 {test_case['id']}: {test_case.get('description', '')}")
        print(f"       URL: {test_case['url']}")

        result = _evaluate_single(test_case)
        results.append(result)

        if result["passed"]:
            passed_count += 1
            status = "✅ PASS"
        else:
            status = "❌ FAIL"

        print(f"       {status}")

        if verbose and result["details"]:
            for detail in result["details"]:
                field = detail.get("field", "?")
                d_status = detail.get("status", "?")
                if d_status == "FAIL":
                    reason = detail.get("reason", "")
                    expected = detail.get("expected", "")
                    actual = detail.get("actual", "")
                    print(f"         ├─ {field}: {d_status}")
                    if reason:
                        print(f"         │  原因: {reason}")
                    if expected:
                        print(f"         │  期望: {expected}")
                    if actual:
                        print(f"         │  实际: {str(actual)[:80]}")
        print()

    total = len(test_cases)
    failed = total - passed_count
    accuracy = (passed_count / total * 100) if total > 0 else 0.0

    # 打印汇总报告
    print(f"{'='*60}")
    print(f"  📊 评估报告")
    print(f"  总用例数: {total} | 成功: {passed_count} | 失败: {failed} | 准确率: {accuracy:.1f}%")
    print(f"{'='*60}")

    return {
        "total": total,
        "passed": passed_count,
        "failed": failed,
        "accuracy": accuracy,
        "results": results
    }


if __name__ == "__main__":
    run_evaluation(verbose=True)