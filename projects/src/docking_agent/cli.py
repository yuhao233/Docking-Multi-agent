"""命令行入口。

模式：
    http      启动本地 HTTP 服务（交互式网页 + API）
    flow      同步跑一次多 Agent（返回 JSON）
    agent     流式跑一次多 Agent（终端实时输出）
    runs      查看历史运行记录
    receptors 查看受体注册表与已知结合位点
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from docking_agent import __version__
from docking_agent.config import ensure_runtime_env, env, env_int

ensure_runtime_env()

from docking_agent.logging_setup import LOG_FILE, setup_logging  # noqa: E402
from docking_agent.runtime.context import current_agent_context

# log_level 留空 = 由 setup_logging 在**调用时**读 LOG_LEVEL（此时 .env 与界面设置已生效）
setup_logging(log_file=LOG_FILE, console_output=True)
logger = logging.getLogger("cli")


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="docking-agent",
        description=f"分子对接多 Agent 协作系统 v{__version__}（本地部署版）",
    )
    p.add_argument("-m", "--mode", default="http",
                   choices=["http", "flow", "agent", "runs", "receptors"],
                   help="运行模式（默认 http）")
    p.add_argument("-p", "--port", type=int, default=int(env("PORT") or 5000), help="HTTP 端口")
    p.add_argument("--host", default=env("HOST") or "127.0.0.1",
                   help="监听地址（默认仅本机 127.0.0.1；如需局域网访问用 0.0.0.0，"
                        "但本服务无鉴权，请自行限制网络暴露）")
    p.add_argument("-i", "--input", default="", help="输入：JSON 字符串或纯文本/SMILES")
    p.add_argument("--message", default="", help="多 Agent 模式的自然语言指令")
    p.add_argument("--molecule-file", default="", help="小分子文件（SDF/SMI/CSV/MOL2，路径或 URL）")
    p.add_argument("--receptor", default="", help="受体：thrombin/trypsin、PDB 编号、.pdb/.pdbqt 路径或 URL")
    p.add_argument("--site-center", default="", help="已知结合位点盒中心，如 31.5,13.74,24.36")
    p.add_argument("--site-size", default="", help="位点盒尺寸，如 22,22,22")
    p.add_argument("--positive-control", default="", help="阳性对照 SMILES")
    p.add_argument("--exhaustiveness", type=int, default=16, help="Vina 搜索强度（默认 16）")
    p.add_argument("--n-poses", type=int, default=1, help="输出构象数（默认 1）")
    p.add_argument("--engine", default="vina", choices=["auto", "vina", "autodock"], help="对接引擎")
    p.add_argument("--max-ligands", type=int, default=0, help="最多对接分子数（0=不限）")
    p.add_argument("--no-poses", action="store_true", help="不保存位姿文件")
    p.add_argument("--no-example-fallback", action="store_true", help="未提供分子时不回退示例库")
    p.add_argument("-o", "--output", default="", help="结果 JSON 输出路径")
    p.add_argument("--list", type=int, default=10, help="runs 模式：列出条数")
    p.add_argument("-v", "--version", action="version", version=f"docking-agent {__version__}")
    return p.parse_args(argv)


def _parse_input(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"text": text}
    except json.JSONDecodeError:
        return {"text": text}


def _parse_site(center: str, size: str) -> Optional[Dict[str, Any]]:
    def _nums(t: str):
        t = (t or "").strip()
        if not t:
            return None
        try:
            return [float(x) for x in t.replace(";", ",").split(",")]
        except ValueError:
            return None

    c, s = _nums(center), _nums(size)
    if not c and not s:
        return None
    return {"center": c, "size": s}


# --------------------------------------------------------------------------- #
# 各模式实现
# --------------------------------------------------------------------------- #
def mode_http(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ["PORT"] = str(args.port)
    from docking_agent.api.app import app

    host = args.host or "127.0.0.1"
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    logger.info("启动服务：http://%s:%s  （监听 %s；网页 / ；API 文档 docs/api.md）",
                shown, args.port, host)
    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning("已监听 %s：本服务没有鉴权（设置接口可改端点与密钥），"
                       "请勿暴露到不可信网络", host)
    uvicorn.run(app, host=host, port=args.port, workers=1,
                log_level=env("UVICORN_LOG_LEVEL", "info"))
    return 0


def mode_flow(args: argparse.Namespace) -> int:
    """同步跑一次多 Agent。"""
    from docking_agent.agents.coordinator import build_agent
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import get_run_store

    run = get_run_store().new("agent", {"mode": "flow", "message": _agent_message(args)})
    graph = build_agent(None)
    config = {"configurable": {"thread_id": run.id}, "recursion_limit": env_int("RECURSION_LIMIT", 60)}
    from docking_agent.runtime.context import current_agent_context

    result = graph.invoke({"messages": [{"role": "user", "content": _agent_message(args)}]},
                          config=config, context=current_agent_context())
    messages = result.get("messages", [])
    final = ""
    for m in reversed(messages):
        if type(m).__name__ in ("AIMessage", "AIMessageChunk") and str(getattr(m, "content", "")).strip():
            final = str(m.content)
            break
    persist_agent_run(run, messages, final)
    run.finish("ok")
    _print(_serialize(result))
    print(f"\n[run] {run.id}  运行目录: var/runs/{run.id}/")
    return 0


def mode_agent(args: argparse.Namespace) -> int:
    """流式跑一次多 Agent（终端实时输出）。"""
    from docking_agent.agents.coordinator import build_agent
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import current_run, get_run_store
    from docking_agent.runtime.streaming import parse_sse_data, stream_agent_sse

    message = _agent_message(args)
    run = get_run_store().new("agent", {"mode": "agent", "message": message})

    async def _run() -> None:
        graph = build_agent(None)
        config = {"configurable": {"thread_id": run.id}, "recursion_limit": env_int("RECURSION_LIMIT", 60)}
        token = current_run.set(run)
        try:
            from docking_agent.runtime.context import current_agent_context

            async for chunk in stream_agent_sse(
                    graph, {"messages": [{"role": "user", "content": message}]}, config, run.id,
                    context=current_agent_context()):
                data = parse_sse_data(chunk)
                if not data:
                    continue
                kind = data.get("type")
                if kind == "token":
                    print(data.get("content", ""), end="", flush=True)
                elif kind in ("tool_call", "tool_result"):
                    print(f"\n[{kind}] {data.get('name') or ''} "
                          f"{str(data.get('content') or '')[:300]}", flush=True)
                elif kind == "error":
                    print(f"\n[error] {data.get('error_code')}: {data.get('error_message')}", flush=True)
            print()
            snapshot = await graph.aget_state(config)
            messages = (getattr(snapshot, "values", {}) or {}).get("messages", [])
            final = ""
            for m in reversed(messages):
                if type(m).__name__ in ("AIMessage", "AIMessageChunk") and str(getattr(m, "content", "")).strip():
                    final = str(m.content)
                    break
            persist_agent_run(run, messages, final)
            run.finish("ok")
        finally:
            current_run.reset(token)

    asyncio.run(_run())
    print(f"\n[run] {run.id}  运行目录: var/runs/{run.id}/")
    return 0


def mode_runs(args: argparse.Namespace) -> int:
    from docking_agent.runs import get_run_store

    _print({"runs": get_run_store().list(limit=args.list)})
    return 0


def mode_receptors(args: argparse.Namespace) -> int:
    from docking_agent.core import DEFAULT_RECEPTOR, list_receptors

    _print({"default": DEFAULT_RECEPTOR, "receptors": list_receptors()})
    return 0


def _serialize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if type(obj).__name__.endswith("Message"):
        content = getattr(obj, "content", "")
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        return {"role": {"HumanMessage": "user", "ToolMessage": "tool"}.get(type(obj).__name__, "assistant"),
                "content": content}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


MODES = {
    "http": mode_http,
    "flow": mode_flow,
    "agent": mode_agent,
    "runs": mode_runs,
    "receptors": mode_receptors,
}


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return MODES[args.mode](args)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        logger.exception("执行失败")
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
