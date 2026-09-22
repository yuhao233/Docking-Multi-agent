"""口袋分析 Agent 的工具：用成熟工具预测结合口袋，并把选定的对接盒提交给 Docking Agent。

设计要点（与「盒子优化」的需求对应）：

- **不由模型猜盒坐标**：预测由真实工具完成（P2Rank，本地部署；不可用时用内置几何法），
  返回的每个口袋都带 score / 中心 / 范围 / 附近残基；
- **有实验位点就用实验位点并做独立验证**：注册表/共晶配体的位点比预测更可信，
  工具的作用变成独立复核（一致/不一致都如实记录）；
- **选择权交给 Agent**：`set_docking_site` 由口袋分析 Agent 明确调用，
  写进共享黑板后 Docking Agent 直接用这个盒子对接（横向协作交接）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from langchain.tools import tool

from docking_agent.core import pockets as P
from docking_agent.runs import current_run
from docking_agent.tools.schemas import CoordArray, floats_to_text
from docking_agent.runtime.context import AgentContext, active_blackboard, active_run
from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)


def _default_receptor_from_run(run: Any = None) -> Dict[str, str]:
    """从本次运行的请求里取受体（表单参数永远是权威来源）。"""
    run = run if run is not None else current_run.get()
    request = (run.data.get("request") or {}) if run is not None else {}
    return {"receptor_file": str(request.get("receptor_file") or ""),
            "receptor_sources": str(request.get("receptor") or "")}


def _resolve_specs(receptor_file: str = "", receptor_sources: str = "",
                  run: Any = None) -> List[Dict[str, Any]]:
    """把受体参数解析成受体 spec 列表（与 docking 工具同一套解析逻辑）。"""
    from docking_agent.core import read_receptor_file, resolve_receptor_specs

    fallback = _default_receptor_from_run(run)
    file_arg = (receptor_file or "").strip() or fallback["receptor_file"]
    source_arg = (receptor_sources or "").strip() or fallback["receptor_sources"]
    if file_arg:
        return [read_receptor_file(file_arg)]
    if source_arg.startswith("["):
        source_arg = json.loads(source_arg)
    specs, _notes = resolve_receptor_specs(source_arg or None)
    return specs


def _pocket_engine_default() -> str:
    from docking_agent.config import env

    return env("POCKET_ENGINE", "auto") or "auto"


@tool
def predict_binding_pockets(receptor_file: str = "", receptor_sources: str = "",
                            pocket_engine: str = "", top_n: int = 8, runtime: ToolRuntime[AgentContext] = None) -> str:
    """用真实的口袋预测工具分析蛋白质受体表面，返回候选结合口袋（含评分与附近残基）。

    何时调用：需要确定「对接盒子放在哪」时先调用本工具。它是**真实计算**，
    不要凭氨基酸序列或经验猜测口袋位置。

    参数：
      receptor_file: 可选。用户上传/提供的蛋白质文件（.pdb/.ent/.pdb1/.cif/.mmcif/.pdbqt，本地路径或 URL）。
      receptor_sources: 可选。预置受体名（thrombin / trypsin）或受体文件路径；留空则用本次任务的受体。
      pocket_engine: 留空=按设置页面/环境变量（默认 auto：优先 P2Rank，不可用则内置几何法）；
          也可显式传 p2rank / geometric / known_site。
      top_n: 返回前 N 个口袋（默认 8）。

    返回 JSON：{status, engine, engine_available, pockets:[{rank,name,score,center,extent,volume,
    residues,burial,enclosure,hydrophobic_contacts,source}], reference, suggested:{center,size,source},
    validation, warnings}
      - pockets[].center 为口袋中心（Å），extent 为口袋空间范围（Å）；
      - reference 为实验/已知位点（若存在），validation 是工具预测与它的一致性判定；
      - suggested 是**按规则建议**的对接盒（实验位点优先、否则用 top 口袋），
        你可以采纳，也可以在理由充分时改选别的口袋（用 set_docking_site 提交）。
    """

    # 未指定受体 → 不执行口袋分析（预置受体仅内部测试用，不能替用户挑靶点）
    from docking_agent.core.receptors import receptor_unspecified  # noqa: PLC0415
    from docking_agent.runtime.context import active_run as _active_run  # noqa: PLC0415

    _run = _active_run(runtime)
    if receptor_unspecified(receptor_sources, _run, receptor_file):
        return json.dumps({
            "status": "needs_user_input",
            "message": ("未指定受体：不能默认挑一个受体做口袋分析。请给出受体来源 —— "
                        "① PDB 编号；② UniProt accession；③ 上传结构文件。"),
            "missing": ["receptor"],
        }, ensure_ascii=False)
    try:
        specs = _resolve_specs(receptor_file, receptor_sources, run=active_run(runtime))
    except Exception as e:  # noqa: BLE001
        from docking_agent.core.receptors import ReceptorInputError  # noqa: PLC0415

        from docking_agent.tools.choices import (  # noqa: PLC0415
            receptor_input_guard, receptor_input_problem)

        if isinstance(e, ReceptorInputError):
            return receptor_input_guard(e, runtime=runtime)
        return receptor_input_problem(
            "receptor_unusable", (receptor_file or receptor_sources or "").strip(),
            f"受体无法用于口袋分析：{e}。本次**不执行任何计算**。"
            "请用户给出可用的受体（PDB 编号 / UniProt accession / 基因或蛋白名 / 结构文件）。",
            options=("replace_file", "resolve_by_name"), runtime=runtime)
    if not specs:
        return json.dumps({"status": "no_receptor",
                           "message": "未指定受体：请提供受体文件或受体名（thrombin/trypsin）。"},
                          ensure_ascii=False)

    # 注意：不能写 (pocket_engine or "auto")：空字符串会被 "auto" 顶掉，
    # 导致设置页面里的 pocket_engine 永远不生效。留空 = 跟随设置。
    engine = (pocket_engine or "").strip() or _pocket_engine_default()
    results = []
    for spec in specs:
        path = str(spec.get("pdbqt") or spec.get("file") or "")
        if not path:
            results.append({"receptor_key": spec.get("key"), "status": "no_path",
                            "message": "该受体没有可用的结构文件路径"})
            continue
        detection = P.detect_pockets(path, engine=engine, top_n=max(1, int(top_n)),
                                    pdb_hint=str(spec.get("pdb") or ""))
        site = P.select_site(spec, engine=engine, top_n=max(1, int(top_n)),
                             override=None)
        results.append({
            "receptor_key": spec.get("key"), "receptor": spec.get("name"),
            "status": detection.get("status"), "engine": detection.get("engine"),
            "pockets": detection.get("pockets") or [],
            "pocket_count": len(detection.get("pockets") or []),
            "reference": site.get("reference"),
            "validation": site.get("validation"),
            "suggested": {"center": site.get("center"), "size": site.get("size"),
                          "source": site.get("source"), "chosen_by": site.get("chosen_by")},
            "warnings": site.get("warnings") or [],
            "attempts": detection.get("attempts") or [],
        })

    board = active_blackboard(runtime)
    if board is not None:
        for block in results:
            if block.get("pockets"):
                board.set_pockets(block["pockets"], engine=block.get("engine") or engine,
                                  receptor_key=str(block.get("receptor_key") or ""))
        best = next((b for b in results if b.get("pockets")), None)
        summary = "口袋分析 Agent："
        if best:
            top = (best["pockets"] or [{}])[0]
            summary += (f"{best.get('engine')} 预测到 {best.get('pocket_count')} 个口袋，"
                        f"top1 中心 {top.get('center')}（score={top.get('score')}），"
                        f"建议盒子来源：{best.get('suggested', {}).get('source')}")
        else:
            summary += "未得到口袋预测结果"
        board.add_note(summary)

    payload = {
        "status": "ok" if any(b.get("pockets") for b in results) else "no_pockets",
        "engine": engine,
        "engine_available": P.available_engines(),
        "receptors": results,
        "hint": ("把选定的口袋用 set_docking_site 提交给 Docking Agent；"
                 "若采纳实验位点或建议盒子，也请显式调用一次以留下交接记录。"),
    }
    if not P.available_engines().get("p2rank"):
        payload["install_hint"] = ("未检测到 P2Rank：可下载发行包解压到 assets/tools/，"
                                  "或设置 P2RANK_HOME；当前使用内置几何法（同样为真实计算）。")
    return json.dumps(payload, ensure_ascii=False)


@tool
def compare_pocket_with_experiment(pocket_rank: int = 1, receptor_file: str = "",
                                   receptor_sources: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """把某个预测口袋与**实验/已知位点**（共晶配体或注册表标注）做独立比对。

    用途：在你决定采纳/改选口袋之前，客观检查它是否与实验位点一致
    （距离 + 共享残基）。若两者相距很远，说明它们指向不同的口袋，
    应在结论里明确说明依据（例如用户要求变构位点、或实验位点缺失）。

    参数：
      pocket_rank: 要比对的预测口袋序号（1 = top1，需先调用 predict_binding_pockets）。
      receptor_file / receptor_sources: 同 predict_binding_pockets。

    返回 JSON：{status, pocket, reference, distance_angstrom, shared_residues, verdict, advice}
    """
    board = active_blackboard(runtime)
    pockets = (board.get_pockets() if board is not None else []) or []
    if not pockets:
        return json.dumps({"status": "no_pockets",
                           "message": "还没有口袋预测结果，请先调用 predict_binding_pockets。"},
                          ensure_ascii=False)
    rank = max(1, int(pocket_rank or 1))
    pocket = next((p for p in pockets if int(p.get("rank") or 0) == rank), None)
    if pocket is None:
        return json.dumps({"status": "not_found",
                           "message": f"没有第 {rank} 号口袋（共 {len(pockets)} 个）。"},
                          ensure_ascii=False)

    try:
        specs = _resolve_specs(receptor_file, receptor_sources, run=active_run(runtime))
    except Exception as e:  # noqa: BLE001
        return json.dumps({"status": "error", "message": f"受体解析失败：{e}"}, ensure_ascii=False)
    reference = P.known_site_from_spec(specs[0]) if specs else None
    validation = P.validate_pocket(pocket, reference)
    ligand = None
    if specs and specs[0].get("pdb"):
        ligand = P.cocrystal_ligand(str(specs[0]["pdb"]))
    if validation.get("status") == "no_reference":
        verdict, advice = "no_reference", "该受体没有实验/已知位点，预测口袋即为唯一依据。"
    elif validation.get("status") == "consistent":
        verdict, advice = "consistent", "预测与实验位点一致，可放心使用该口袋（实验位点优先）。"
    else:
        verdict, advice = ("inconsistent",
                           "预测与实验位点指向不同口袋：若用户未特别要求变构位点，建议采用实验位点；"
                           "若确实要用别处，请在结论里写清理由。")
    return json.dumps({"status": "ok", "pocket": pocket, "reference": reference,
                       "validation": validation,
                       "cocrystal_ligand": ligand,
                       "distance_angstrom": validation.get("distance_angstrom"),
                       "shared_residues": validation.get("shared_residues") or [],
                       "verdict": verdict, "advice": advice}, ensure_ascii=False)


@tool
def set_docking_site(pocket_rank: int = 0, center: CoordArray = None, size: CoordArray = None,
                     reason: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """把选定的对接盒子提交给 Docking Agent（写入共享黑板，横向协作交接）。

    参数（二选一）：
      pocket_rank: 采纳 predict_binding_pockets 返回的第 N 号口袋（推荐用法，来源可追溯）。
      center/size: 直接给出中心与尺寸，形如 [31.5, 13.74, 24.36] / [22, 22, 22]（用于微调）；
        也接受逗号分隔字符串（旧调用方兼容）。
      reason: 选择理由（会写入协作记录与运行报告，供独立复核检查）。

    返回 JSON：{status, site:{center,size,source,chosen_by,pocket,validation}}。
    调用后 Docking Agent 会使用该盒子；未调用时系统按规则自动确定（实验位点优先，否则用工具预测）。
    """
    board = active_blackboard(runtime)
    if board is None:
        return json.dumps({"status": "no_blackboard",
                           "message": "当前不在多 Agent 运行上下文中，无法提交位点。"},
                          ensure_ascii=False)

    def _nums(values: Any, expect: int = 3) -> Optional[List[float]]:
        """坐标规范化：数组或 "a,b,c" 字符串都接受（类型化后仍兼容旧调用方）。"""
        text = floats_to_text(values, expect=expect)
        return [float(v) for v in text.split(",")] if text else None

    center_values = _nums(center)
    size_values = _nums(size)
    pockets = board.get_pockets() or []
    pocket = None
    rank = int(pocket_rank or 0)
    if rank > 0:
        pocket = next((p for p in pockets if int(p.get("rank") or 0) == rank), None)
        if pocket is None:
            return json.dumps({"status": "not_found",
                               "message": f"没有第 {rank} 号口袋，请先 predict_binding_pockets。"},
                              ensure_ascii=False)
        if not center_values:
            center_values, size_values = P.pocket_to_box(pocket)
    if not center_values:
        return json.dumps({"status": "invalid",
                           "message": "请提供 pocket_rank，或直接给出 center（形如 [31.5, 13.74, 24.36]）。"},
                          ensure_ascii=False)

    if pocket is not None:
        source = (f"口袋分析 Agent 选择 {pocket.get('name')}"
                  f"（{pocket.get('source')}，score={pocket.get('score')}）")
    else:
        source = "口袋分析 Agent 指定坐标"
    if reason:
        # source 用于报告/界面一行展示，保持简短；完整理由单独存，便于追溯但不撑破布局
        brief = reason.strip().split("。")[0].split("\n")[0][:140]
        source += f"：{brief}"

    reference = P.known_site_from_spec(board.get_receptor() or {})
    validation = P.validate_pocket(pocket, reference) if pocket else {}
    site = board.set_site(center=center_values, size=size_values, source=source,
                          chosen_by="pocket_agent", pocket=pocket, validation=validation)
    if reason:
        site = {**site, "reason": reason.strip()[:1200]}
    logger.info("口袋分析 Agent 提交对接位点：%s %s", site.get("center"), site.get("size"))
    return json.dumps({"status": "ok", "site": site, "reason": reason,
                       "message": "已提交给 Docking Agent：对接将使用该盒子。"},
                      ensure_ascii=False)


@tool
def list_pocket_engines(runtime: ToolRuntime[AgentContext] = None) -> str:
    """列出可用的口袋预测引擎与启用方式（当 P2Rank 未安装时给出安装指引）。

    返回 JSON：{status, engines:{p2rank,geometric,known_site,centroid}, p2rank_home, install_hint}
    """
    home = P.p2rank_home()
    available = P.available_engines()
    return json.dumps({
        "status": "ok",
        "engines": available,
        "p2rank_home": str(home) if home else "",
        "geometric": "内置几何法（表面层网格 + 埋藏/包封/疏水特征，无外部依赖）",
        "install_hint": ("P2Rank 未安装：把发行包解压到 assets/tools/（或设置 P2RANK_HOME 指向解压目录），"
                         "本系统会自动识别。"),
    }, ensure_ascii=False)
