"""tools/check_links.py — Markdown 相对链接检查器（零依赖）

扫描仓库内的 *.md 文件，校验相对路径链接（图片/文件）是否真实存在：
  - 跳过 http(s)://、mailto:、#锚点、<自动链接>
  - 相对路径以当前文件所在目录为基准解析，目录末尾带 / 视为指向目录

用法:
  python tools/check_links.py           # 全库检查，有坏链退出码 1
  python tools/check_links.py README.md # 只查指定文件
"""

import os
import re
import sys
from typing import List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 匹配 markdown 内联链接 / 图片：![alt](target "title") / [text](target)
_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _candidate_targets(line: str) -> List[str]:
    targets = []
    for m in _LINK_RE.finditer(line):
        target = m.group(1).split()[0]  # 去掉可选的 "title"
        target = target.strip()
        if not target or target.startswith(("http://", "https://", "mailto:", "#", "<")):
            continue
        targets.append(target)
    return targets


def check_file(md_path: str) -> Tuple[int, List[str]]:
    """返回 (坏链数, 坏链列表)。"""
    base = os.path.dirname(md_path)
    bad: List[str] = []
    with open(md_path, encoding="utf-8", errors="ignore") as f:
        for line_no, line in enumerate(f, 1):
            for target in _candidate_targets(line):
                rel = target.split("#", 1)[0]  # 锚点忽略
                if not rel:
                    continue
                full = os.path.normpath(os.path.join(base, rel))
                if not os.path.exists(full):
                    bad.append(f"{os.path.relpath(md_path, ROOT)}:{line_no} -> {target}")
    return len(bad), bad


def main(argv: List[str]) -> int:
    targets = [os.path.abspath(p) for p in argv] or [
        os.path.join(r, name)
        for r, _, fs in os.walk(ROOT)
        if ".git" not in r and "node_modules" not in r
        for name in fs
        if name.endswith(".md")
    ]
    total_bad = 0
    for t in targets:
        if not os.path.isfile(t):
            print(f"文件不存在: {t}")
            total_bad += 1
            continue
        n, bad = check_file(t)
        for b in bad:
            print(f"[坏链] {b}")
        total_bad += n
    if total_bad:
        print(f"链接检查失败：{total_bad} 个坏链")
        return 1
    print(f"链接检查通过 | {len(targets)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
