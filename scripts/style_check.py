#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖代码规范检查。

只用标准库（`ast` + `pathlib`），不需要安装任何 linter —— 与项目
「零第三方依赖」的定位一致。

检查项
------
1. 行宽 ≤ 88 列
2. 行尾无多余空白、无制表符
3. 所有函数（含方法与嵌套函数）都有返回类型标注
4. 所有非双下划线函数都有 docstring
5. 无未使用的导入
6. 模块的 ``__all__`` 覆盖全部公开定义，且不含未定义项
7. 文件以恰好一个换行结尾

用法
----
    python scripts/style_check.py          # 检查默认目标（src/ 与 demo.py）
    python scripts/style_check.py src demo.py  # 检查指定路径

退出码 0 表示通过，1 表示存在违规。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

MAX_LINE = 88
DEFAULT_TARGETS = ("src", "demo.py")


class Report:
    """收集违规项。"""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def add(self, path: Path, lineno: int, message: str) -> None:
        """记录一条违规。"""
        self.problems.append(f"{path.as_posix()}:{lineno}: {message}")

    @property
    def ok(self) -> bool:
        """是否没有任何违规。"""
        return not self.problems


# --------------------------------------------------------------------------
# 各项检查
# --------------------------------------------------------------------------


def check_layout(path: Path, text: str, report: Report) -> None:
    """行宽、行尾空白、制表符、文件结尾换行。"""
    lines = text.splitlines()

    for lineno, line in enumerate(lines, 1):
        if len(line) > MAX_LINE:
            report.add(path, lineno, f"行宽 {len(line)} 列，超过 {MAX_LINE}")
        if line != line.rstrip():
            report.add(path, lineno, "行尾有多余空白")
        if "\t" in line:
            report.add(path, lineno, "含制表符，请改用空格")

    if text and not text.endswith("\n"):
        report.add(path, len(lines), "文件未以换行结尾")
    elif text.endswith("\n\n"):
        report.add(path, len(lines), "文件结尾有多余空行（只保留一个换行）")


def check_functions(path: Path, tree: ast.Module, report: Report) -> None:
    """函数必须有返回类型标注与非空 docstring。"""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.returns is None:
            report.add(path, node.lineno, f"{node.name}() 缺少返回类型标注")
        if not node.name.startswith("__") and not ast.get_docstring(node):
            report.add(path, node.lineno, f"{node.name}() 缺少 docstring")


def check_imports(path: Path, tree: ast.Module, report: Report) -> None:
    """导入必须被使用。

    注意：即使模块用了 ``from __future__ import annotations``，
    ``ast.parse`` 仍会把类型标注解析成 ``Name`` 节点，
    因此「仅出现在标注里」的导入不会被误报。
    """
    imported: dict[str, int] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[(alias.asname or alias.name).split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    imported[alias.asname or alias.name] = node.lineno

    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)

    for name, lineno in imported.items():
        if name == "annotations":
            continue
        if name not in used:
            report.add(path, lineno, f"未使用的导入：{name}")


def _public_names(tree: ast.Module) -> list[str]:
    """模块的公开定义：非下划线开头的函数/类，以及全大写常量。"""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                name = target.id
                if name.isupper() and not name.startswith("_"):
                    names.append(name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # 带类型标注的模块级常量（`NAME: tuple[...] = (...)`）同样是公开定义。
            # 漏掉这类会让「常量忘了写进 __all__」逃过检查 —— 本项目踩过一次。
            name = node.target.id
            if name.isupper() and not name.startswith("_"):
                names.append(name)
    return names


def _declared_all(tree: ast.Module) -> ast.Assign | None:
    """找到模块顶层的 ``__all__`` 赋值节点。"""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                return node
    return None


def check_all_export(path: Path, tree: ast.Module, report: Report) -> None:
    """``__all__`` 必须与公开定义一致（有则查，无则跳过）。"""
    node = _declared_all(tree)
    if node is None:
        return

    declared = [
        e.value for e in getattr(node.value, "elts", []) if isinstance(e, ast.Constant)
    ]
    public = _public_names(tree)

    for name in public:
        if name not in declared:
            report.add(path, node.lineno, f"__all__ 缺少公开定义：{name}")
    for name in declared:
        if name not in public:
            report.add(path, node.lineno, f"__all__ 含未定义或不公开的项：{name}")


SOURCE_CHECKS = (check_layout,)
"""只看源码文本、不需要 AST 的检查。"""

AST_CHECKS = (check_functions, check_imports, check_all_export)
"""需要遍历 AST 的检查。"""


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def collect_python_files(targets: tuple[str, ...]) -> list[Path]:
    """把目标展开成 .py 文件列表（目录会递归展开）。"""
    files: list[Path] = []
    for target in targets:
        path = Path(target)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        elif path.suffix == ".py" and path.exists():
            files.append(path)
    return [f for f in files if "__pycache__" not in f.parts]


def main(argv: list[str]) -> int:
    """检查目标文件，返回进程退出码。"""
    targets = tuple(argv) if argv else DEFAULT_TARGETS
    files = collect_python_files(targets)

    if not files:
        print(f"未找到可检查的 Python 文件：{' '.join(targets)}")
        return 1

    report = Report()
    for path in files:
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            report.add(path, exc.lineno or 1, f"语法错误：{exc.msg}")
            continue

        for check in SOURCE_CHECKS:
            check(path, text, report)
        for check in AST_CHECKS:
            check(path, tree, report)

    print(f"检查 {len(files)} 个文件：{' '.join(f.as_posix() for f in files)}")
    print(f"检查项：行宽 ≤ {MAX_LINE}、行尾空白、制表符、结尾换行、")
    print("        返回类型标注、docstring、未使用导入、__all__ 一致性")
    print()

    if report.ok:
        print("通过 ✓ 未发现规范问题")
        return 0

    for problem in report.problems:
        print(f"  {problem}")
    print()
    print(f"发现 {len(report.problems)} 处问题")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
