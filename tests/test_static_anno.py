"""tests/test_static_anno.py — 自研静态检查器的类型注解完备性门禁（ANNO）测试

static_check.py 的 ANNO 规则（mypy disallow_untyped_defs 的轻量子集）：
核心模块内函数/方法必须有参数 + 返回注解，self/cls 与 __init__ 豁免。
本测试直接驱动 check_file（显式 strict_anno 开关），验证规则自身行为，
不依赖真实模块内容。
"""

import ast

from tools import static_check


def _check_src(src: str, strict: bool):
    return static_check._Checker(strict_anno=strict).run(ast.parse(src))


def test_untyped_def_flagged_in_strict_file():
    errs = _check_src("def foo(x):\n    return x + 1\n", strict=True)
    assert any("ANNO 缺返回注解: foo" in e for e in errs)
    assert any("缺参数注解 'x' in foo" in e for e in errs)


def test_fully_annotated_def_clean():
    errs = _check_src("def foo(x: int) -> int:\n    return x + 1\n", strict=True)
    assert not any("ANNO" in e for e in errs)


def test_self_and_init_exempt():
    src = ("class C:\n"
           "    def __init__(self):\n"
           "        self.x = 1\n"
           "    def get(self) -> int:\n"
           "        return self.x\n")
    errs = _check_src(src, strict=True)
    assert not any("ANNO" in e for e in errs)


def test_loose_file_not_annotated():
    # 非严格名单：未注解函数不报 ANNO（渐进式门禁的边界）
    errs = _check_src("def helper(x):\n    return x\n", strict=False)
    assert not any("ANNO" in e for e in errs)


def test_annotation_uses_import_not_false_positive():
    # 参数默认值里的名字必须算"使用"（防 Header(None) 式 F401 误报）
    src = ("from typing import Optional\n"
           "from a import Header\n"
           "def f(x: Optional[str] = Header(None)) -> None:\n"
           "    pass\n")
    errs = _check_src(src, strict=False)
    assert not any("F401" in e for e in errs)
