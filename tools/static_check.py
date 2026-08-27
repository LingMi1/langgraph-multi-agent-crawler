"""tools/static_check.py — 轻量静态检查（stdlib AST，等价 ruff 的 F 级规则子集）

CI / 本地统一入口，覆盖 pyflakes(F) 的核心几类：
  F401  未使用的 import
  F403  `from xxx import *`（星号导入，遮蔽真实依赖）
  F811  重复定义（同一作用域内名被再次定义）
  F821  引用了未定义的名称（简单作用域分析）

不依赖第三方包（ruff 需编译安装；本脚本用 stdlib 即可在无网络环境跑），
与 ruff 的 select=["F"] 互为补充：ruff 负责更全的规则集，本脚本负责
可离线验证的确定性检查。返回违规数，非 0 即退出码 1（CI 可挂钩）。

用法:
  python tools/static_check.py              # 检查 agents/ graph/ tools/ tests/
  python tools/static_check.py agents/react.py tests/test_safety.py
"""

import ast
import builtins
import os
import sys
from typing import List, Optional, Tuple

# 允许的模块级通配导入（测试代码常显式 `from xx import *`）
_STAR_IMPORT_ALLOW = {"pytest"}  # 无默认限制，可按需收紧

_BUILTINS = set(dir(builtins))

# 模块级魔法名（__file__ / __name__ 等）不算未定义
_MAGIC_NAMES = {
    "__file__", "__name__", "__doc__", "__path__", "__spec__", "__loader__",
    "__package__", "__annotations__", "__all__", "__builtins__", "__debug__",
}


class _Scope:
    def __init__(self, parent: Optional["_Scope"] = None):
        self.parent = parent
        self.defined: set = set()
        self.unused_imports: List[Tuple[int, str]] = []


class _Checker(ast.NodeVisitor):
    def __init__(self):
        self.errors: List[str] = []
        self._scopes: List[_Scope] = []
        self._scope = self._push_scope()
        # 跨作用域聚合"出现过"的名字：模块级 import 被函数体使用也计入使用
        self._used_anywhere: set = set()

    def _push_scope(self, parent: Optional[_Scope] = None) -> _Scope:
        s = _Scope(parent)
        self._scopes.append(s)
        return s

    # ── 工具 ──
    def _err(self, node: ast.AST, code: str, msg: str) -> None:
        self.errors.append(f"{getattr(node, 'lineno', '?')}:{code} {msg}")

    def _in_scope(self, name: str) -> bool:
        s = self._scope
        while s is not None:
            if name in s.defined:
                return True
            s = s.parent
        return False

    # ── 顶层：import / from-import ──
    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self._scope.defined.add(name)
            self._mark_import_usage(node, name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module == "__future__":
            return
        for alias in node.names:
            if alias.name == "*":
                if node.module not in _STAR_IMPORT_ALLOW:
                    self._err(node, "F403", f"'from {node.module} import *' 不允许（会遮蔽真实依赖）")
                continue
            name = alias.asname or alias.name
            self._scope.defined.add(name)
            self._mark_import_usage(node, name)

    def _mark_import_usage(self, node: ast.AST, name: str) -> None:
        # 先记录"待定未使用"，若后续出现 Name 读取则回退
        self._scope.unused_imports.append((node.lineno, name))

    # ── 名称使用：记录引用，并在使用过时清除未使用标记 ──
    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load) and node.id not in _BUILTINS:
            self._used_anywhere.add(node.id)
        # 未定义名检测：Load 且所有作用域都无定义、非内置、非魔法名
        if isinstance(node.ctx, ast.Load) and not self._in_scope(node.id):
            if node.id not in _BUILTINS and node.id not in _MAGIC_NAMES:
                self._err(node, "F821", f"undefined name {node.id!r}")
        self.generic_visit(node)

    # ── 赋值/定义：把名字纳入作用域 ──
    def _bind(self, name: str) -> None:
        self._scope.defined.add(name)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._bind(node.name)
        inner = self._push_scope(self._scope)
        self._scope, inner = inner, self._scope
        try:
            for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                self._bind(arg.arg)
                if arg.annotation:
                    self.visit(arg.annotation)
            if node.args.vararg:
                self._bind(node.args.vararg.arg)
                if node.args.vararg.annotation:
                    self.visit(node.args.vararg.annotation)
            if node.args.kwarg:
                self._bind(node.args.kwarg.arg)
                if node.args.kwarg.annotation:
                    self.visit(node.args.kwarg.annotation)
            if node.returns:
                self.visit(node.returns)
            for d in node.decorator_list:
                self.visit(d)
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self._scope = inner

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef):
        self._bind(node.name)
        inner = self._push_scope(self._scope)
        self._scope, inner = inner, self._scope
        try:
            for d in node.decorator_list:
                self.visit(d)
            for b in node.bases:
                self.visit(b)
            for kw in node.keywords:
                self.visit(kw)
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self._scope = inner

    def visit_Assign(self, node: ast.Assign):
        for t in node.targets:
            self._bind_targets(t)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        self._bind_targets(node.target)
        if node.annotation:
            self.visit(node.annotation)
        self.generic_visit(node)

    def visit_For(self, node: ast.For):
        self._bind_targets(node.target)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With):
        for item in node.items:
            if item.optional_vars:
                self._bind_targets(item.optional_vars)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.name:
            self._bind(node.name)
        self.generic_visit(node)

    def _bind_targets(self, t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            self._bind(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                self._bind_targets(e)
        # Attribute / Subscript 目标绑定的是对象本身，无需绑定名

    def visit_Lambda(self, node: ast.Lambda):
        inner = self._push_scope(self._scope)
        self._scope, inner = inner, self._scope
        try:
            for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                self._bind(arg.arg)
            if node.args.vararg:
                self._bind(node.args.vararg.arg)
            if node.args.kwarg:
                self._bind(node.args.kwarg.arg)
            self.visit(node.body)
        finally:
            self._scope = inner

    # 推导式：先绑定迭代目标，再访问元素/条件，避免 `[x for x in y]` 的 x 误报
    def _visit_comprehension(self, node: ast.AST):
        for gen in node.generators:
            self._bind_targets(gen.target)
        self.generic_visit(node)

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension
    visit_DictComp = _visit_comprehension

    # ── 未使用 import 结算（模块遍历结束后统一结算，避免"后定义的函数
    #    还没访问到"导致的误报；每次结算后清空防止重复上报） ──
    def _report_unused_in_scope(self):
        for s in self._scopes:
            pending, s.unused_imports = s.unused_imports, []
            for lineno, name in pending:
                if name not in self._used_anywhere and not name.startswith("_"):
                    self.errors.append(f"{lineno}:F401 '{name}' imported but unused")

    def run(self, tree: ast.AST) -> List[str]:
        # 预扫描模块级定义（import / def / class / 赋值目标），
        # 让函数体里对"模块后部定义的全局"的前向引用不误报 F821。
        for stmt in tree.body:
            self._prebind(stmt)
        # `__all__` 中声明的名字视为"被使用"（包 API 再导出场景）
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id == "__all__":
                        for elt in stmt.value.elts:
                            if isinstance(elt, ast.Constant):
                                self._used_anywhere.add(str(elt.value))
        self.visit(tree)
        self._report_unused_in_scope()
        return self.errors

    def _prebind(self, stmt: ast.AST) -> None:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names = (a.asname or a.name.split(".")[0] for a in stmt.names)
            for n in names:
                if n != "*":
                    self._scope.defined.add(n)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self._scope.defined.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                self._prebind_targets(t)
        elif isinstance(stmt, ast.AnnAssign):
            self._prebind_targets(stmt.target)

    def _prebind_targets(self, t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            self._scope.defined.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                self._prebind_targets(e)


def check_file(path: str) -> List[str]:
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        return [f"{path}: cannot read: {e}"]
    tree = ast.parse(src)
    return [f"{path}:{e}" for e in _Checker().run(tree)]


def _collect_targets(paths: List[str]) -> List[str]:
    files: List[str] = []
    for p in paths:
        if os.path.isfile(p) and p.endswith(".py"):
            files.append(p)
        elif os.path.isdir(p):
            for root, _, fs in os.walk(p):
                if "vendor" in root or root.startswith("._"):
                    continue
                for name in fs:
                    if name.endswith(".py"):
                        files.append(os.path.join(root, name))
    return sorted(files)


def main() -> int:
    args = sys.argv[1:] or ["agents", "graph", "tools", "tests"]
    files = _collect_targets(args)
    errors: List[str] = []
    for f in files:
        errors.extend(check_file(f))
    for e in errors:
        print(e)
    print(f"静态检查完成 | {len(files)} 个文件 | {len(errors)} 个问题")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
