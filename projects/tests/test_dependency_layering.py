"""分层依赖回归：单向依赖与模块级导入不成环。

`import` 环在运行期靠函数内懒导入掩盖时不会立刻暴露，
直到某个入口先导入环里的另一半，才在其他机器上表现为 `ImportError` 或半初始化模块。
分层契约针对 4 类依赖问题：

    V2     core/ 依赖 runs/ 与 reporting.store      最底层反向依赖持久化与报告层
    V3     runtime/ 依赖 agents/                   被最广泛依赖的 runtime 反向依赖上层
    SCC-1  config 与 settings 函数级双向
    SCC-3  agents.workers 与 tools.dispatch 由函数内导入掩盖的环

分层契约固化为可执行断言，防止回归：

1. 仅用模块级 import 构图，图为 DAG（函数内懒导入不计入）；
2. `core/` 不 import `runs/reporting/tools/agents/api`（纯计算层零持久化依赖）；
3. `runtime/` 不 import `agents/tools/api`；
4. `tools/` 不 import `agents/`（工具层在 Agent 层之下）；
5. `settings` 不 import `config`（读取原语在 `envs`，避免双向依赖）；
6. `envs` / `run_context` 是叶子模块（只允许依赖 `paths`）。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
PKG = "docking_agent"


# --------------------------------------------------------------------------- #
# 导入图工具
# --------------------------------------------------------------------------- #
def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _modules() -> Dict[str, Path]:
    return {_module_name(p): p
            for p in sorted((SRC / PKG).rglob("*.py"))
            if "__pycache__" not in p.parts}


def _raw_imports(path: Path, me: str) -> List[Tuple[str, int, bool]]:
    """返回 (目标模块名, 行号, 是否模块级)。相对导入归一成绝对模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: List[Tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = me.split(".")[:-1] if path.name != "__init__.py" else me.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join(base + (node.module.split(".") if node.module else []))
            else:
                target = node.module or ""
            out.append((target, node.lineno, node.col_offset == 0))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno, node.col_offset == 0))
    return out


def _resolve(target: str, modules: Dict[str, Path]) -> str:
    """把 `docking_agent.a.b` 归到最近的已存在模块（包归到其 `__init__`）。"""
    parts = target.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in modules:
            return candidate
        parts.pop()
    return ""


def _edges(modules: Dict[str, Path], *, module_level_only: bool
           ) -> Dict[str, Set[str]]:
    graph: Dict[str, Set[str]] = {m: set() for m in modules}
    for me, path in modules.items():
        for target, _lineno, top in _raw_imports(path, me):
            if module_level_only and not top:
                continue
            if not target.startswith(PKG + "."):
                continue
            resolved = _resolve(target, modules)
            if resolved and resolved != me:
                graph[me].add(resolved)
    return graph


def _cycles(graph: Dict[str, Set[str]]) -> List[List[str]]:
    """Tarjan 求强连通分量，返回所有 size>1 的分量。"""
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: Set[str] = set()
    result: List[List[str]] = []
    counter = [0]

    def strong(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                result.append(sorted(comp))

    for v in list(graph):
        if v not in index:
            strong(v)
    return result


def _forbidden_imports(prefix: str, forbidden: Tuple[str, ...], *,
                       module_level_only: bool = False) -> List[str]:
    """`prefix` 下的模块 import 了 `forbidden` 里任一前缀时，返回 `文件:行` 与目标模块。"""
    modules = _modules()
    offenders: List[str] = []
    for me, path in modules.items():
        if not (me == prefix or me.startswith(prefix + ".")):
            continue
        for target, lineno, top in _raw_imports(path, me):
            if module_level_only and not top:
                continue
            if any(target == f or target.startswith(f + ".") for f in forbidden):
                rel = path.relative_to(PROJECT_ROOT)
                offenders.append(f"{rel}:{lineno} → {target}")
    return sorted(offenders)


# --------------------------------------------------------------------------- #
# 断言
# --------------------------------------------------------------------------- #
def test_module_level_import_graph_is_acyclic() -> None:
    """模块级 import 不成环；函数内懒导入不计入，该写法用于有意断开环。"""
    cycles = _cycles(_edges(_modules(), module_level_only=True))
    assert cycles == [], f"模块级导入出现循环依赖：{cycles}"


def test_core_has_no_persistence_or_upward_imports() -> None:
    """`core/` 是纯计算层：不依赖 runs/reporting/tools/agents/api（对应 V2）。"""
    offenders = _forbidden_imports(
        f"{PKG}.core",
        (f"{PKG}.runs", f"{PKG}.reporting", f"{PKG}.tools", f"{PKG}.agents", f"{PKG}.api"))
    assert offenders == [], f"core/ 反向依赖上层/持久化层：{offenders}"


def test_runtime_does_not_import_upper_layers() -> None:
    """`runtime/` 是被最广泛依赖的一层：不反向依赖 agents/tools/api（对应 V3）。"""
    offenders = _forbidden_imports(
        f"{PKG}.runtime", (f"{PKG}.agents", f"{PKG}.tools", f"{PKG}.api"))
    assert offenders == [], f"runtime/ 反向依赖上层：{offenders}"


def test_tools_do_not_import_agents() -> None:
    """`tools/` 在 Agent 层之下：不 import `agents/`（对应 SCC-3）。"""
    offenders = _forbidden_imports(f"{PKG}.tools", (f"{PKG}.agents",))
    assert offenders == [], f"tools/ 反向依赖 agents/：{offenders}"


def test_agents_do_not_import_api() -> None:
    """`agents/` 不依赖 HTTP 层（api 只做适配，不被上层反向调用）。"""
    offenders = _forbidden_imports(f"{PKG}.agents", (f"{PKG}.api",))
    assert offenders == [], f"agents/ 反向依赖 api/：{offenders}"


def test_settings_does_not_import_config() -> None:
    """`settings` 只依赖 `envs` 与 `paths`：不 import `config`（对应 SCC-1）。"""
    offenders = _forbidden_imports(f"{PKG}.settings", (f"{PKG}.config",))
    assert offenders == [], f"settings 反向依赖 config：{offenders}"


def test_env_primitives_are_leaf() -> None:
    """`envs` 是环境变量读取原语：只依赖 `paths`，不依赖 config 或 settings。"""
    offenders = _forbidden_imports(
        f"{PKG}.envs",
        (f"{PKG}.config", f"{PKG}.settings", f"{PKG}.runs", f"{PKG}.reporting",
         f"{PKG}.tools", f"{PKG}.agents", f"{PKG}.api"))
    assert offenders == [], f"envs 不是叶子模块：{offenders}"


def test_run_context_registry_imports_nothing() -> None:
    """`run_context` 是 core 与 runs 之间依赖反转的登记点：不 import 任何项目内模块。"""
    offenders = _forbidden_imports(f"{PKG}.run_context", (PKG,))
    assert offenders == [], f"run_context 必须零项目依赖：{offenders}"
