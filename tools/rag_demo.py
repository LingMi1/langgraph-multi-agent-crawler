"""tools/rag_demo.py — RAG 检索链路演示：爬取页面 → 建索引 → 语义检索

用爬虫落盘的真实 HTML 建一个 TF-IDF 向量索引，跑几条语义查询，
展示"爬虫 → 知识库 → 语义检索"的 RAG 闭环（无外部依赖，离线可跑）。

用法:
  python tools/rag_demo.py                # 用 output/ 下全部站点建索引
  python tools/rag_demo.py hnbn666       # 只用匹配的站点目录
  python tools/rag_demo.py --query "花生油生产工艺"
  python tools/rag_demo.py --top-k 5
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from agents.tools import ToolRegistry
from agents.vector_retriever import VectorIndex, build_rag_tool
from config import LOCAL_BACKUP_DIR


def _collect_html_docs(netloc_filter: str = "") -> list:
    """递归扫描 output/<站点>/ 下的 .html，抽取正文文本。"""
    docs = []
    if not os.path.isdir(LOCAL_BACKUP_DIR):
        return docs
    for site_dir in os.listdir(LOCAL_BACKUP_DIR):
        if netloc_filter and netloc_filter not in site_dir:
            continue
        root = os.path.join(LOCAL_BACKUP_DIR, site_dir)
        if not os.path.isdir(root):
            continue
        for r, _, fs in os.walk(root):
            for name in fs:
                if not name.endswith(".html"):
                    continue
                path = os.path.join(r, name)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        soup = BeautifulSoup(f.read(), "html.parser")
                    text = " ".join(soup.get_text(" ", strip=True).split())
                    if len(text) >= 20:
                        docs.append((path, text))
                except OSError:
                    continue
    return docs


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 检索链路演示")
    parser.add_argument("filter", nargs="?", default="", help="站点目录名过滤")
    parser.add_argument("--query", default="", help="单条查询（默认跑内置示例查询）")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    docs = _collect_html_docs(args.filter)
    if not docs:
        print(f"没有可用的 HTML 落盘数据（{LOCAL_BACKUP_DIR}/）。先跑一次爬取再演示。")
        return 2

    index = VectorIndex()
    for path, text in docs:
        index.add(f"{path}\n{text}")
    index.build()
    print(f"已建索引：{len(index)} 篇文档（n-gram TF-IDF，零外部依赖）")

    # 注册成 Tool，演示 Agent 可调用的工具形态
    reg = ToolRegistry()
    reg.register(build_rag_tool(index, top_k=args.top_k))
    print(f"已注册工具：{reg.names()}")

    queries = [args.query] if args.query else [
        "公司简介与主营业务",
        "产品生产工艺与规格",
        "招聘与联系方式",
    ]
    for q in queries:
        print("\n" + "=" * 70)
        print(f"查询: {q!r}")
        for i, score, snippet in reg.call("rag_search", query=q, k=args.top_k):
            print(f"  [{score:.3f}] {snippet[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
