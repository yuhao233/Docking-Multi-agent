#!/usr/bin/env python
"""部署自检：把 langgraph.json 里声明的**每一个图**真加载一遍，并核对运行环境。

    .venv/bin/python scripts/verify_graphs.py [-v]

检查项：
  1) langgraph.json 可解析，graphs 段每个入口都能 import + 构建；
  2) 每个图都是已编译图、且**没有自带 checkpointer**（Platform 自己托管持久化，
     自带会冲突）；
  3) 协调 Agent 的工具集与项目源码一致（工具名逐个列出，便于人工核对）；
  4) 工作区 / 资产 / 配置目录可达（决定对接能否真的跑起来）；
  5) LLM 配置齐备（只打印是否配置与模型名，**不打印密钥**）；
  6) 真实计算引擎可导入（rdkit / vina / meeko）与 pdb2pqr 是否可用（仅提示）。
退出码非 0 表示部署不可用。
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

FAIL: list[str] = []
WARN: list[str] = []


def ok(msg: str) -> None:
    print(f"  \033[32m[OK]\033[0m   {msg}")


def bad(msg: str) -> None:
    FAIL.append(msg)
    print(f"  \033[31m[FAIL]\033[0m {msg}")


def warn(msg: str) -> None:
    WARN.append(msg)
    print(f"  \033[33m[WARN]\033[0m {msg}")


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def load_graphs() -> dict:
    cfg = json.loads((HERE / "langgraph.json").read_text(encoding="utf-8"))
    section("1) langgraph.json → 图加载")
    built = {}
    for name, spec in (cfg.get("graphs") or {}).items():
        module_name, _, attr = spec.partition(":")
        if not attr:
            bad(f"{name}: 入口写法应为 './module.py:callable'，实际 {spec!r}")
            continue
        path = HERE / module_name.lstrip("./")
        mod_name = ".".join(path.relative_to(HERE).with_suffix("").parts)
        try:
            module = importlib.import_module(mod_name)
        except Exception as e:  # noqa: BLE001
            bad(f"{name}: 模块导入失败 {mod_name} → {type(e).__name__}: {e}")
            continue
        factory = getattr(module, attr, None)
        if factory is None:
            bad(f"{name}: {mod_name} 里没有 {attr}")
            continue
        try:
            graph = factory()
        except Exception as e:  # noqa: BLE001
            bad(f"{name}: 构建失败 → {type(e).__name__}: {e}")
            continue
        if not hasattr(graph, "get_graph"):
            bad(f"{name}: 返回的对象不是已编译图（{type(graph).__name__}）")
            continue
        ckpt = getattr(graph, "checkpointer", None)
        if ckpt is not None:
            bad(f"{name}: 图自带 checkpointer（{type(ckpt).__name__}）—— Platform 会冲突")
            continue
        built[name] = graph
        nodes = ",".join(sorted(getattr(graph, "nodes", {}) or {}))
        ok(f"{name:12s} {spec:44s} nodes=[{nodes}] 无 checkpointer")
    return built


def check_coordinator_tools(built: dict) -> None:
    section("2) 协调 Agent 工具集（部署图 vs 项目源码）")
    graph = built.get("coordinator")
    if graph is None:
        warn("coordinator 未加载成功，跳过工具集核对")
        return
    node = getattr(graph, "nodes", {}).get("agent")
    # 外层包装节点：真正的 create_agent 工具在运行作用域里构建，这里用项目源码静态核对
    try:
        from docking_graphs import graphs as G
        inner = G._build_coordinator_inner()
    except Exception as e:  # noqa: BLE001
        bad(f"内层协调 Agent 构建失败：{type(e).__name__}: {e}")
        return
    tools_node = getattr(inner, "nodes", {}).get("tools")
    tools = sorted(getattr(getattr(tools_node, "bound", None), "tools_by_name", {}) or {})
    if len(tools) < 10:
        bad(f"协调 Agent 工具数异常：{len(tools)} → {tools}")
    else:
        ok(f"协调 Agent 绑定 {len(tools)} 个工具：{', '.join(tools)}")
    workers = {}
    from docking_agent.agents import workers as W
    from docking_agent.runtime.llm import ROLES

    # 子 Agent 清单由 ROLES 派生（单一事实来源）：手工清单漏角色时会静默少检一个
    worker_roles = [r for r in ROLES if r not in ("coordinator", "intake")]
    for role in worker_roles:
        getter = f"get_{role}_agent"
        agent = getattr(W, getter, lambda: None)()
        if agent is None:
            bad(f"子 Agent 未初始化：{role}（{getter}）")
            continue
        wnode = getattr(agent, "nodes", {}).get("tools")
        wtools = sorted(getattr(getattr(wnode, "bound", None), "tools_by_name", {}) or {})
        workers[role] = wtools
        ok(f"{role:8s} 子 Agent 绑定 {len(wtools)} 个工具：{', '.join(wtools)}")
    for role in worker_roles:
        agent = getattr(W, f"get_{role}_agent", lambda: None)()
        if agent is not None and getattr(agent, "checkpointer", None) is not None:
            warn(f"{role} 子 Agent 自带 checkpointer（作为工具调用无妨，Platform 只校验顶层图）")


def check_environment() -> None:
    section("3) 运行环境（工作区 / 资产 / 配置）")
    from docking_agent.paths import assets_dir, project_root, workspace_dir

    print(f"     项目源码：{project_root()}")
    print(f"     工作区  ：{workspace_dir()}")
    for label, path in (("assets", assets_dir()), ("assets/receptors", assets_dir() / "receptors"),
                        ("config", workspace_dir() / "config"),
                        ("var/runs", workspace_dir() / "var" / "runs")):
        if Path(path).exists():
            ok(f"{label:18s} {path}")
        else:
            bad(f"{label:18s} 不存在：{path}")
    registries = sorted(p.name for p in (assets_dir() / "receptors" / "registry").glob("*"))
    if registries:
        ok(f"可对接受体 {len(registries)} 个：{', '.join(registries[:8])}"
           + ("…" if len(registries) > 8 else ""))
    else:
        warn("assets/receptors/registry 为空：只能用上传受体或在线解析")


def check_llm() -> None:
    section("4) LLM 配置（不打印密钥）")
    from docking_agent.runtime.llm import ROLES, build_chat_llm, load_llm_config

    cfg = load_llm_config()
    model = (cfg.get("config") or {}).get("model")
    if model:
        ok(f"模型：{model}")
    else:
        bad("未配置模型（LLM_MODEL / config/agent_llm_config.json）")
    base = os.getenv("LLM_BASE_URL") or (cfg.get("config") or {}).get("base_url") or ""
    ok(f"端点：{base or '（默认 api.openai.com）'}")
    key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    if key:
        ok(f"API Key：已配置（{key[:4]}…{key[-2:]}，长度 {len(key)}）")
    else:
        bad("未配置 API Key（LLM_API_KEY / OPENAI_API_KEY）—— Agent 图会构建成功但调用时报错")
    try:
        for role in ROLES:
            llm = build_chat_llm(None, role=role)
            if llm is None:
                bad(f"角色 {role} 的模型构建返回 None")
        ok(f"6 个角色模型实例均可构建：{', '.join(ROLES)}")
    except Exception as e:  # noqa: BLE001
        bad(f"角色模型构建失败：{type(e).__name__}: {e}")


def check_engines() -> None:
    section("5) 真实计算引擎")
    for mod, label in (("rdkit", "RDKit（物化性质 / 指纹）"), ("vina", "AutoDock Vina"),
                       ("meeko", "meeko（受体/配体准备）"), ("scipy", "SciPy"),
                       ("gemmi", "gemmi（结构解析）"), ("matplotlib", "matplotlib（图表）")):
        try:
            importlib.import_module(mod)
            ok(label)
        except Exception as e:  # noqa: BLE001
            bad(f"{label} 不可用：{type(e).__name__}: {e}")
    try:
        from docking_agent.core.receptor_ph import pdb2pqr_bin

        binary = pdb2pqr_bin()          # PDB2PQR_BIN → PATH → 常见本地安装（PyMOL 自带）
        if binary:
            ok(f"pdb2pqr（受体质子化）：{binary}")
        else:
            warn("pdb2pqr 未找到：受体 pH 质子化会走回退路径（标准准备），其余功能不受影响；"
                 "可用 PDB2PQR_BIN 指定，如 /home/biolab/pymol/bin/pdb2pqr")
    except Exception as e:  # noqa: BLE001
        warn(f"pdb2pqr 探测失败：{type(e).__name__}: {e}")


def main() -> int:
    print("\033[1m=== docking-agent → LangGraph Studio / CLI 部署自检 ===\033[0m")
    print(f"部署目录：{HERE}")
    built = load_graphs()
    if built:
        check_coordinator_tools(built)
    check_environment()
    check_llm()
    check_engines()
    print()
    if FAIL:
        print(f"\033[31m结果：{len(FAIL)} 项失败\033[0m")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print(f"\033[32m结果：全部通过\033[0m" + (f"（{len(WARN)} 条提示）" if WARN else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
