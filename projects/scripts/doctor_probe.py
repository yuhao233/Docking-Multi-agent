#!/usr/bin/env python
"""环境能力自检（开箱即用第一步）：一次列清「有什么、缺什么、缺了会降级成什么」。

用法：
    .venv/bin/python scripts/doctor_probe.py            # 人读能力矩阵
    .venv/bin/python scripts/doctor_probe.py --json     # 机器可读（CI / 测试用）
    .venv/bin/python scripts/doctor_probe.py --online   # 额外探测 LLM 端点可达性

退出码：
    0 = 全能力；2 = 仅降级（缺可选组件，核心流程仍可跑）；1 = 缺必需组件

为什么要有它：换机器部署时，"缺 P2Rank/pdb2pqr/Java 会怎样" 此前只写在 `.env.example`
的注释里，用户要跑到某一步才知道降级了（或以为跑通了）。这里把探测集中到一处，
且**复用项目自己的探测函数**（`core.pockets.p2rank_command` / `core.receptor_ph.pdb2pqr_bin`
/ `core.docking._autodock_bin`），不另写一套判断逻辑，避免"doctor 说能用、实际不能用"。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 必需依赖：缺任何一个，核心流程都跑不起来
REQUIRED_IMPORTS = [
    ("rdkit", "RDKit（理化性质 / 指纹 / 2D 结构图）"),
    ("vina", "AutoDock Vina Python 绑定（对接引擎）"),
    ("meeko", "Meeko（配体/受体 PDBQT 准备）"),
    ("fastapi", "FastAPI（网页服务）"),
    ("uvicorn", "Uvicorn（ASGI 服务器）"),
    ("pydantic", "Pydantic（请求/报告模型）"),
    ("matplotlib", "Matplotlib（报告图表与 PDF）"),
]


def _item(key: str, label: str, ok: bool, detail: str = "", hint: str = "",
          required: bool = True, degrade: str = "") -> Dict[str, Any]:
    return {"key": key, "label": label, "ok": bool(ok), "detail": detail,
            "hint": hint, "required": bool(required), "degrade": degrade}


def _module_version(name: str) -> Optional[str]:
    try:
        module = importlib.import_module(name)
    except Exception:  # noqa: BLE001 - 缺失也要如实报告，不能中断自检
        return None
    return str(getattr(module, "__version__", "") or "已安装")


def check_python() -> Dict[str, Any]:
    version = "%d.%d.%d" % sys.version_info[:3]
    ok = sys.version_info >= (3, 12)
    return _item("python", "Python ≥ 3.12", ok, version,
                 hint="项目要求 Python 3.12+；用 `uv venv --python 3.12 .venv` 重建虚拟环境")


def check_dependencies() -> List[Dict[str, Any]]:
    out = []
    for name, label in REQUIRED_IMPORTS:
        version = _module_version(name)
        out.append(_item(f"dep:{name}", label, version is not None,
                         version or "缺失",
                         hint=f"bash start.sh --setup（或 .venv/bin/pip install -e .）"))
    return out


def check_workspace() -> List[Dict[str, Any]]:
    out = []
    for rel in ("var", "assets"):
        path = PROJECT_ROOT / rel
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".doctor_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            out.append(_item(f"write:{rel}", f"{rel}/ 可写", True, str(path)))
        except OSError as e:
            out.append(_item(f"write:{rel}", f"{rel}/ 可写", False, str(e),
                             hint=f"检查目录权限：chmod u+w {path}"))
    return out


def _port_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def check_port(port: int, *, window: int = 20) -> Dict[str, Any]:
    """默认端口被占用不算故障：`start.sh` 会自动顺延到下一个可用端口。

    因此这里在 `port..port+window` 里找第一个可用端口，报告「将使用哪个」；
    整段都占满才算降级失败（仍不是必需项）。
    """
    if _port_free(port):
        return _item("port", f"端口 {port} 可用", True, "127.0.0.1 可绑定", required=False)
    for candidate in range(port + 1, port + window + 1):
        if _port_free(candidate):
            return _item("port", f"端口（{port} 起）", True,
                         f"{port} 被占用 → start.sh 会自动用 {candidate}", required=False)
    return _item("port", "可用端口", False,
                 f"{port}..{port + window} 全部被占用",
                 hint="bash start.sh --port 18000 指定一个空闲端口",
                 required=False, degrade="默认端口全部占用，需手动指定端口")


def check_p2rank() -> Dict[str, Any]:
    try:
        from docking_agent.core.pockets import p2rank_command

        command = p2rank_command()
    except Exception as e:  # noqa: BLE001
        return _item("p2rank", "P2Rank（口袋预测）", False, f"探测失败：{e}",
                     hint="bash scripts/fetch_tools.sh（或设 P2RANK_HOME）",
                     required=False, degrade="口袋分析退化：用内置几何法预测口袋")
    if command:
        return _item("p2rank", "P2Rank（口袋预测）", True, " ".join(str(x) for x in command),
                     required=False)
    return _item("p2rank", "P2Rank（口袋预测）", False, "未找到",
                 hint="bash scripts/fetch_tools.sh（解压到 assets/tools/）或设 P2RANK_HOME",
                 required=False, degrade="口袋分析退化：用内置几何法预测口袋")


def check_pdb2pqr() -> Dict[str, Any]:
    try:
        from docking_agent.core.receptor_ph import pdb2pqr_bin

        binary = pdb2pqr_bin()
    except Exception as e:  # noqa: BLE001
        return _item("pdb2pqr", "pdb2pqr（按 pH 准备受体）", False, f"探测失败：{e}",
                     hint="设 PDB2PQR_BIN=/path/to/pdb2pqr", required=False,
                     degrade="受体不按目标 pH 重算质子化态（报告如实告警）")
    if binary:
        return _item("pdb2pqr", "pdb2pqr（按 pH 准备受体）", True, binary, required=False)
    return _item("pdb2pqr", "pdb2pqr（按 pH 准备受体）", False, "未找到",
                 hint="设 PDB2PQR_BIN=/path/to/pdb2pqr（PyMOL 自带可指向 ~/pymol/bin/pdb2pqr）",
                 required=False, degrade="受体不按目标 pH 重算质子化态（报告如实告警）")


def check_autodock() -> Dict[str, Any]:
    try:
        from docking_agent.core.docking import _autodock_bin

        found = {name: _autodock_bin(name) for name in ("autodock4", "autogrid4")}
    except Exception as e:  # noqa: BLE001
        return _item("autodock", "AutoDock4 CPU 引擎（备用）", False, f"探测失败：{e}",
                     required=False, degrade="只有 Vina 可用")
    missing = [k for k, v in found.items() if not v]
    if not missing:
        return _item("autodock", "AutoDock4 CPU 引擎（备用）", True,
                     ", ".join(str(v) for v in found.values()), required=False)
    return _item("autodock", "AutoDock4 CPU 引擎（备用）", False,
                 "未找到 " + ", ".join(missing),
                 hint="apt-get install autodock autogrid（可选）", required=False,
                 degrade="只有 Vina 可用（engine=autodock 会失败）")


def check_java() -> Dict[str, Any]:
    java = shutil.which("java")
    return _item("java", "Java 运行时（P2Rank 前提）", bool(java), java or "未找到",
                 hint="apt-get install default-jre（P2Rank 需要 Java 8+）",
                 required=False, degrade="即使有 P2Rank 也无法运行 → 口袋分析退化为几何法")


def check_cjk_font() -> Dict[str, Any]:
    try:
        from matplotlib import font_manager

        names = {f.name for f in font_manager.fontManager.ttflist}
    except Exception as e:  # noqa: BLE001
        return _item("cjk_font", "中文字体（图表 / PDF）", False, f"探测失败：{e}",
                     hint="apt-get install fonts-noto-cjk", required=False,
                     degrade="图表与 PDF 中的中文可能显示为方块")
    hit = [n for n in ("Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei",
                       "WenQuanYi Micro Hei", "SimHei", "Microsoft YaHei", "PingFang SC")
           if n in names]
    return _item("cjk_font", "中文字体（图表 / PDF）", bool(hit),
                 ", ".join(hit) if hit else f"未找到（已装字体 {len(names)} 个）",
                 hint="apt-get install fonts-noto-cjk", required=False,
                 degrade="图表与 PDF 中的中文可能显示为方块")


def check_llm() -> Dict[str, Any]:
    try:
        from docking_agent.config import env

        key = env("LLM_API_KEY") or env("OPENAI_API_KEY")
        base = env("LLM_BASE_URL") or env("OPENAI_BASE_URL")
    except Exception as e:  # noqa: BLE001
        return _item("llm", "LLM 配置（多 Agent 模式）", False, f"读取配置失败：{e}",
                     hint="cp .env.example .env 并填 LLM_API_KEY / LLM_BASE_URL",
                     required=False, degrade="只有确定性流水线可用（参数模式·流水线）")
    configured = bool(key and base)
    return _item("llm", "LLM 配置（多 Agent 模式）", configured,
                 f"key={'已配置' if key else '缺失'} base={base or '缺失'}",
                 hint="cp .env.example .env 并填 LLM_API_KEY / LLM_BASE_URL",
                 required=False, degrade="只有确定性流水线可用（参数模式·流水线）")


def check_llm_reachable() -> Dict[str, Any]:
    """--online 时才跑：探测 LLM 端点是否可达（不发送任何分子数据）。"""
    try:
        import urllib.error
        import urllib.request

        from docking_agent.config import env

        base = (env("LLM_BASE_URL") or env("OPENAI_BASE_URL") or "").rstrip("/")
        if not base:
            return _item("llm_reachable", "LLM 端点可达", False, "未配置 base_url",
                         required=False, degrade="跳过在线探测")
        request = urllib.request.Request(base + "/models", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=8) as resp:  # noqa: S310 - 用户自己的端点
                return _item("llm_reachable", "LLM 端点可达", True,
                             f"HTTP {resp.status} {base}", required=False)
        except urllib.error.HTTPError as e:
            # 401/403 也说明端点活着（只是没带对凭证）
            return _item("llm_reachable", "LLM 端点可达", e.code < 500,
                         f"HTTP {e.code} {base}", required=False,
                         degrade="端点返回错误，请检查 API Key / 路径")
        except Exception as e:  # noqa: BLE001
            return _item("llm_reachable", "LLM 端点可达", False, f"{type(e).__name__}: {e}",
                         hint="检查网络与 LLM_BASE_URL；离线环境可忽略此项",
                         required=False, degrade="多 Agent 模式不可用")
    except Exception as e:  # noqa: BLE001
        return _item("llm_reachable", "LLM 端点可达", False, str(e), required=False)


def default_port() -> int:
    try:
        from docking_agent.config import env_int

        return int(env_int("PORT", 5000))
    except Exception:  # noqa: BLE001
        return int(os.environ.get("PORT") or 5000)


def collect(*, online: bool = False) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = [check_python()]
    items += check_dependencies()
    items += check_workspace()
    items.append(check_port(default_port()))
    items += [check_java(), check_p2rank(), check_pdb2pqr(), check_autodock(),
              check_cjk_font(), check_llm()]
    if online:
        items.append(check_llm_reachable())
    required_bad = [i for i in items if i["required"] and not i["ok"]]
    optional_bad = [i for i in items if not i["required"] and not i["ok"]]
    exit_code = 1 if required_bad else (2 if optional_bad else 0)
    return {"exit_code": exit_code, "items": items,
            "required_failed": [i["key"] for i in required_bad],
            "optional_missing": [i["key"] for i in optional_bad]}


def render(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("=" * 74)
    lines.append("环境能力自检（doctor）")
    lines.append("=" * 74)
    for label, group in (("必需", [i for i in report["items"] if i["required"]]),
                         ("可选（缺了会降级）", [i for i in report["items"] if not i["required"]])):
        lines.append(f"【{label}】")
        for item in group:
            mark = "✓" if item["ok"] else ("✗" if item["required"] else "○")
            lines.append(f"  [{mark}] {item['label']:<28} {item['detail']}")
            if not item["ok"]:
                if item.get("degrade"):
                    lines.append(f"        影响：{item['degrade']}")
                if item.get("hint"):
                    lines.append(f"        补齐：{item['hint']}")
        lines.append("")
    code = report["exit_code"]
    if code == 0:
        lines.append("结论：全能力可用 —— 直接 `bash start.sh` 即可。")
    elif code == 2:
        lines.append("结论：**核心流程可用**，但缺少可选组件（上方 ○ 项），"
                     "相关能力会降级且报告会如实说明。")
    else:
        lines.append("结论：**缺少必需组件**（上方 ✗ 项），请先按提示补齐："
                     "bash start.sh --setup")
    lines.append("=" * 74)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="环境能力自检（开箱即用第一步）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--online", action="store_true", help="额外探测 LLM 端点可达性")
    args = parser.parse_args(argv)
    report = collect(online=args.online)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
