"""源码结构回归：机器可判定的「冗余/遮蔽」问题。

为什么值得一组测试：`core/docking.py` 里曾同时存在**两个**顶层 `dock_library`（前者被后者
静默遮蔽，62 行死代码），排查时读到的可能是被遮蔽的那一份 —— 比没有文档更糟。
这类问题静态可判定，因此永久看护：

1. 同一模块内不得出现重复的顶层函数/类名（后定义会静默覆盖前定义）；
2. `core/` 不得反向 import `tools/agents/api`（分层单向下行，见 architecture §10.1）。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"


def _top_level_names(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _py_files() -> List[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_duplicate_top_level_definitions() -> None:
    """重复的顶层定义 = 后一份静默遮蔽前一份（真实缺陷：dock_library 出现两次）。"""
    offenders: Dict[str, List[str]] = {}
    for path in _py_files():
        names = _top_level_names(path)
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            offenders[str(path.relative_to(PROJECT_ROOT))] = dupes
    assert not offenders, f"同一模块内出现重复顶层定义（后者遮蔽前者）：{offenders}"


@pytest.mark.parametrize("layer", ["tools", "agents", "api"])
def test_core_does_not_import_upward(layer: str) -> None:
    """`core/` 是纯计算层：不得 import 上层（否则单测必须拉起整个服务）。"""
    offenders: List[str] = []
    for path in sorted((SRC / "docking_agent" / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(f"docking_agent.{layer}") or \
                        node.module.startswith(f".{layer}"):
                    offenders.append(f"{path.name}:{node.lineno} → {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(f"docking_agent.{layer}"):
                        offenders.append(f"{path.name}:{node.lineno} → {alias.name}")
    assert not offenders, f"core/ 反向依赖 {layer}/：{offenders}"


def test_engine_test_file_list_has_no_rot() -> None:
    """`conftest.ENGINE_TEST_FILES`（CI 跳过的引擎相关文件）不得指向不存在的文件。

    这份名单是 CI 与「本机全量」之间的唯一开关；文件被改名/删除后若忘了同步，
    名单会静默失效 —— 要么 CI 漏跑，要么名单形同虚设却仍然全绿。
    """
    from conftest import ENGINE_TEST_FILES  # type: ignore[import-not-found]

    tests_dir = Path(__file__).resolve().parent
    missing = sorted(n for n in ENGINE_TEST_FILES if not (tests_dir / n).is_file())
    assert missing == [], f"ENGINE_TEST_FILES 里有不存在的文件：{missing}"
    assert len(ENGINE_TEST_FILES) >= 10, "名单过短，疑似被误删"
