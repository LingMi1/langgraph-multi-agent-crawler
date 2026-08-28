"""tools/prompt_registry.py — 提示词版本管理（sha256 指纹 + 漂移检测）

面试叙事："提示词也是代码，改它等于改行为"——prompt 影响所有下游输出，但普通
diff 里难以察觉。本工具对生产提示词（llm_pipeline.py 按文件名加载的 .txt）打
sha256 指纹，落盘 `prompts_manifest.json` 作为基线；`--check` 检测工作区漂移，
CI 挂钩后任何 prompt 改动都必须走提交 → 评审 → 更新基线的显式流程。

用法:
  python tools/prompt_registry.py --list     # 列出提示词指纹
  python tools/prompt_registry.py --update   # 更新基线（prompt 变更评审后执行）
  python tools/prompt_registry.py --check    # 校验基线是否漂移（CI 用，退出码 1）
"""

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# llm_pipeline.py 启用并加载的 6 个生产提示词（与 _load_prompt 的文件名一致）
PROMPT_FILES = [
    "导航栏目提取提示词.txt",
    "提取提示词.txt",
    "新闻列表页提示词.txt",
    "图片列表页提示词.txt",
    "清洗提示词.txt",
    "正文渲染代码.txt",
]

MANIFEST_NAME = "prompts_manifest.json"


def sha256_file(path: str) -> str:
    """文件 sha256（流式读取，适配大文件）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(root: str) -> Dict[str, Dict[str, Any]]:
    """扫描根目录提示词，返回 {文件名: {sha256, bytes, lines}}。"""
    manifest: Dict[str, Dict[str, Any]] = {}
    for name in PROMPT_FILES:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            manifest[name] = {"sha256": None, "bytes": None, "lines": None}
            continue
        with open(path, encoding="utf-8") as f:
            content = f.read()
        manifest[name] = {
            "sha256": sha256_file(path),
            "bytes": len(content.encode("utf-8")),
            "lines": content.count("\n") + 1,
        }
    return manifest


def load_manifest(root: str) -> Dict[str, Dict[str, Any]]:
    path = os.path.join(root, MANIFEST_NAME)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_drift(root: str, baseline: Optional[Dict[str, Dict[str, Any]]] = None
                ) -> Tuple[bool, List[str]]:
    """与基线对比：返回 (是否一致, 差异列表)。无基线文件时视为"需初始化"（不一致）。"""
    baseline = baseline if baseline is not None else load_manifest(root)
    current = build_manifest(root)
    if not baseline:
        return False, ["未找到基线 %s，先运行 --update 生成" % MANIFEST_NAME]
    diffs: List[str] = []
    for name in PROMPT_FILES:
        cur, base = current.get(name), baseline.get(name)
        if not cur or not base:
            diffs.append(f"{name}: 基线缺失或新增")
            continue
        if cur["sha256"] != base["sha256"]:
            diffs.append(
                f"{name}: 指纹漂移 {base['sha256'][:8]}.. -> {cur['sha256'][:8]}.."
                f"（{base['bytes']}B -> {cur['bytes']}B，提示词改动需评审后 --update）")
    return len(diffs) == 0, diffs


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="提示词版本管理（sha256 基线 + 漂移检测）")
    parser.add_argument("--list", action="store_true", help="列出提示词指纹")
    parser.add_argument("--update", action="store_true", help="更新基线 prompts_manifest.json")
    parser.add_argument("--check", action="store_true", help="校验基线是否漂移（CI 用）")
    args = parser.parse_args(argv)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if args.list:
        manifest = build_manifest(root)
        for name in PROMPT_FILES:
            m = manifest[name]
            if m["sha256"] is None:
                print(f"  {name:<14} 缺失")
                continue
            print(f"  {name:<14} sha256={m['sha256'][:12]}  {m['bytes']}B {m['lines']}行")
        return 0

    if args.update:
        manifest = build_manifest(root)
        with open(os.path.join(root, MANIFEST_NAME), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print(f"已更新 {MANIFEST_NAME}（{len(manifest)} 个提示词指纹）")
        return 0

    if args.check:
        ok, diffs = check_drift(root)
        for d in diffs:
            print(d)
        if ok:
            print("提示词基线一致，无漂移")
        else:
            print("提示词基线漂移：先评审改动，再 python tools/prompt_registry.py --update")
        return 0 if ok else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
