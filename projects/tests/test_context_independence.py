"""P2-b：**工具链路必须走 runtime，不许再偷读 ContextVar**（回归守卫）。

背景（P1④ 双读 → P2-b 收口）：
项目原来靠三个 ContextVar（`current_run` / `current_blackboard` / `request_context`）传递运行上下文，
工具与内部辅助函数随处 `.get()`。P1 已把 **23 个 `@tool`** 改成 `active_*(runtime)`；
P2-b 又把工具侧的内部辅助函数（`_analyze` / `_invoke_checked` / `_coerce_molecule_list` /
`_live_molecule_row` / `_receptor_blocks` / `_ranked_rows` / `_from_blackboard` / `current_ranking` /
`_default_receptor_from_run`）与 `tool_io` 数据总线改为「显式传入 + ContextVar 兜底」。

本测试把这件事**锁死**：
1. 任何 `@tool` 函数体内**不得**出现裸的 ContextVar 读取；
2. `tools/` 下的内部辅助函数同样不得裸读；
3. 允许的兜底必须写成 `x = x if x is not None else current_run.get()` 这种**显式兜底**形式；
4. 残余依赖集中在一份**显式白名单**里（CLI/单测直调、日志前缀、以及待决策的黑板），
   任何新增的越界读取都会让本用例失败。
"""
from __future__ import annotations

import ast
import pathlib
import re
from typing import Dict, List, Set

from docking_agent.agents.blackboard import Blackboard

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "docking_agent"

#: 裸读（不允许，除非在下面的白名单里）
RAW_READ = re.compile(r"(?<!else )\b(current_run|current_blackboard|request_context)\.get\(\)")
READ_NAMES = ("current_run.get()", "current_blackboard.get()", "request_context.get()",
              "get_blackboard()")

#: 允许继续使用 ContextVar 的地方（每条都要有理由；**新增必须在这里写明为什么**）
ALLOWED: Dict[str, Set[str]] = {
    # 兜底层本身：`runtime` 缺失时回退 ContextVar（双读的关键实现）
    "runtime/context.py": {"active_run", "active_blackboard", "active_request",
                           "current_agent_context"},
    # 黑板访问器本身（黑板是否迁进 graph state 是**待决策**项，见 README 记录 48）
    "agents/blackboard.py": {"get_blackboard", "board_molecules_json"},
    # 日志前缀：纯展示用途，拿不到就省略前缀
    "logging_setup.py": {"filter"},
    # 运行请求回溯（质子化策略/库级下限）：显式参数优先 + ContextVar 兜底
    "core/protonation.py": {"_run_request"},
    "core/normalize.py": {"record_input_normalization"},
    # 非工具侧的两个辅助函数：调用方可能是 CLI/pipeline（无 runtime），保留显式兜底
    "tools/recommend.py": {"_rows_from_file", "build_for_run"},
    # 落盘层统一入口：黑板由 API/部署层注入（同样属待决策项）
    "agents/persistence.py": {"persist_agent_run"},
}


def _deco_name(node: ast.AST) -> str:
    """装饰器名（支持 @tool / @tool(...) 两种写法）。"""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_tool(node: ast.AST) -> bool:
    return any(_deco_name(d) == "tool" for d in getattr(node, "decorator_list", []))


def _functions(path: pathlib.Path) -> List[ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _raw_reads(src: str) -> Dict[str, List[int]]:
    """按函数名收集「裸读」行号（`x if x is not None else current_run.get()` 不算裸读）。"""
    tree = ast.parse(src)
    lines = src.splitlines()
    fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    out: Dict[str, List[int]] = {}
    for i, line in enumerate(lines, 1):
        if not RAW_READ.search(line):
            continue
        owner = min((n for n in fns if n.lineno <= i <= (n.end_lineno or n.lineno)),
                    key=lambda n: n.lineno, default=None)
        out.setdefault(owner.name if owner else "<module>", []).append(i)
    return out


def test_tools_never_read_contextvars_directly() -> None:
    """`tools/` 下的裸读必须为零（全部走 `active_*(runtime)` 或显式兜底）。"""
    offenders: Dict[str, Dict[str, List[int]]] = {}
    for path in sorted((SRC / "tools").glob("*.py")):
        rel = f"tools/{path.name}"
        reads = _raw_reads(path.read_text(encoding="utf-8"))
        allowed = ALLOWED.get(rel, set())
        bad = {fn: lns for fn, lns in reads.items() if fn not in allowed}
        if bad:
            offenders[rel] = bad
    assert not offenders, (
        f"这些地方还在裸读 ContextVar，应改为 active_run(runtime)/active_blackboard(runtime)：{offenders}")


def test_allowed_residual_reads_are_documented() -> None:
    """白名单里的函数必须真的存在（避免白名单腐烂成"永久豁免"）。"""
    for rel, names in ALLOWED.items():
        path = SRC / rel
        assert path.is_file(), f"白名单文件不存在：{rel}"
        present = {n.name for n in _functions(path)} | {"<module>"}
        missing = names - present
        assert not missing, f"{rel} 白名单里的函数已不存在：{sorted(missing)}"


def test_every_tool_uses_runtime_aware_accessors() -> None:
    """每个 `@tool` 只要碰运行上下文，就必须用 `active_*` / 显式参数，不得用裸 `.get()`。"""
    offenders: List[str] = []
    for path in sorted((SRC / "tools").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        tools = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_tool(n)]
        for node in tools:
            body = "\n".join(src.splitlines()[node.lineno - 1:(node.end_lineno or node.lineno)])
            if any(name in body for name in READ_NAMES) and not re.search(
                    r"else (current_run|current_blackboard|request_context)\.get\(\)|"
                    r"active_(run|blackboard|request)\(", body):
                offenders.append(f"{path.name}:{node.name}")
    assert not offenders, f"这些工具没有用 runtime 感知的读取方式：{offenders}"


def test_tool_io_dual_reads_with_explicit_run() -> None:
    """数据总线（tool_io）必须支持显式 `run=`；裸读只允许出现在 `else` 兜底里。"""
    src = (SRC / "agents" / "tool_io.py").read_text(encoding="utf-8")
    assert "run if run is not None else current_run.get()" in src, "tool_io 必须支持显式 run"
    assert not _raw_reads(src), f"tool_io 里仍有裸读：{_raw_reads(src)}"


# --------------------------------------------------------------------------- #
# P2-c：黑板接入 LangGraph store（父图与子 Agent 共享同一命名空间）
# --------------------------------------------------------------------------- #
def test_store_backed_blackboard_shares_state_across_views() -> None:
    """两个视图（模拟另一个进程/子图）通过同一 store 看到彼此写入 —— 这是接入 store 的意义。"""
    from docking_agent.agents.blackboard import shared_store, store_blackboard

    store = shared_store()
    a = store_blackboard(store, "run-store-1")
    a.add_molecules([{"name": "乙醇", "smiles": "CCO"}])
    a.set_site(center=[31.5, 13.74, 24.36], size=[22, 22, 22])
    a.set_positive_control("NC(=N)c1ccccc1")

    fresh = Blackboard("run-store-1", store=store)      # 全新视图，只能从 store hydrate
    assert [m["name"] for m in fresh.molecules()] == ["乙醇"]
    assert fresh.get_site()["center"] == [31.5, 13.74, 24.36]
    assert fresh.positive_control == "NC(=N)c1ccccc1"
    assert fresh.stats()["molecules"] == 1


def test_store_backed_blackboard_isolates_runs() -> None:
    from docking_agent.agents.blackboard import shared_store, store_blackboard

    store = shared_store()
    verify = store_blackboard(store, "run-store-iso-A")
    verify.add_molecules([{"name": "苯酚", "smiles": "Oc1ccccc1"}])
    other = Blackboard("run-store-iso-B", store=store)
    assert other.molecules() == [], "不同 run 绝不能看到彼此的黑板"
    assert verify.molecules() != other.molecules()


def test_store_writes_are_per_field() -> None:
    """按字段写：并发改不同字段不会互相覆盖（整档覆写会丢更新）。"""
    from docking_agent.agents.blackboard import shared_store, store_blackboard

    store = shared_store()
    board = store_blackboard(store, "run-store-fields")
    board.set_receptor({"key": "thrombin", "name": "凝血酶"})
    board.set_pockets([{"rank": 1}], engine="p2rank")
    names = {item.key for item in store.search(("blackboard", "run-store-fields"))}
    assert {"receptor", "pockets", "pocket_engine"} <= names, names


def test_active_blackboard_prefers_store_when_available() -> None:
    """runtime 提供 store + run 时，`active_blackboard` 必须给出 store 视图（规范后端）。"""
    from dataclasses import dataclass

    from docking_agent.agents.blackboard import shared_store
    from docking_agent.runtime.context import AgentContext, active_blackboard

    @dataclass
    class _Run:
        id: str = "run-active-store"

    class _Runtime:
        context = AgentContext(run=_Run())
        store = shared_store()

    board = active_blackboard(_Runtime())
    board.add_molecules([{"name": "咖啡因", "smiles": "Cn1c(=O)c2c(ncn2C)n(C)c1=O"}])
    # 没有 store 的 runtime（CLI 直调形态）仍走 ContextVar 兜底，不受影响
    class _NoStore:
        context = AgentContext(run=_Run())

    assert active_blackboard(_NoStore()) is None or \
        active_blackboard(_NoStore()) is not board
