"""结合模式检测 Agent 的工具：与阳性对照的真实结合模式比较。

两个工具共用同一套真实计算（`core.chemistry.compute_binding_report`）：
  1. `binding_mode_analysis`      —— 完整分析：双指纹（Morgan + MACCS）相似度、
     药效团锚定基团匹配、理化性质差异、结构一致性与结合模式提示。
  2. `positive_control_similarity` —— 轻量版：同一数据源，只返回相似度相关字段。

两者口径一致，因此无论子 Agent 选用哪一个，都不会出现「方法学缩水」的差异。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from langchain.tools import tool

from docking_agent.runtime import tool_io
from docking_agent.core import compute_binding_report
from docking_agent.runtime.context import AgentContext, active_blackboard, active_run
from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)

# 轻量工具输出的字段（完整分析输出的子集）
_SIMILARITY_FIELDS = ("name", "smiles", "similarity_to_positive_control",
                      "morgan_tanimoto", "maccs_tanimoto", "combined_similarity",
                      "structural_consistency")


def _rows_from_docking_file(path: str) -> List[Dict[str, Any]]:
    """从对接产物文件里取出结果行（兼容 `{"receptors":[{"results":[...]}]}` 与裸列表）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读取对接结果文件失败（%s）：%s", path, e)
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    rows: List[Dict[str, Any]] = []
    for block in (data.get("receptors") or []):
        rows.extend([r for r in (block.get("results") or []) if isinstance(r, dict)])
    return rows


def _analyze(molecules_json: str, positive_control_smiles: str,
             molecules_file: str = "", runtime: Any = None) -> dict:
    from docking_agent.runtime.blackboard import board_molecules_json

    # 文件优先：大库按文件交接（运行产物 JSON 或用户上传的分子库文件都行）
    if (molecules_file or "").strip():
        from docking_agent.core.ligands import read_molecules_any

        _fmt, loaded, _norm = read_molecules_any(str(molecules_file).strip())
        if not loaded:
            raise ValueError(f"分子文件里没有可用的分子：{molecules_file}")
        return _analyze(json.dumps(loaded, ensure_ascii=False), positive_control_smiles,
                        runtime=runtime)

    effective = molecules_json
    if not isinstance(effective, str) or not effective.strip():
        # 横向协作：清单留空时取共享黑板（协调 Agent 不必搬运上万条清单）
        effective = board_molecules_json("")
    if not effective:
        raise ValueError("没有待分析的分子：请先导入分子库（共享黑板上也没有分子）")
    molecules = json.loads(effective)
    if not isinstance(molecules, list) or not molecules:
        raise ValueError("molecules_json 必须是非空列表")
    for m in molecules:
        if not isinstance(m, dict) or not m.get("smiles"):
            raise ValueError("每个分子需包含 smiles 字段")
    if not positive_control_smiles or not positive_control_smiles.strip():
        # 阳性对照也是共享黑板上的协作数据：工具参数没给时从黑板取
        # （注意：这里不能再写函数内 import，否则 get_blackboard 会被当成局部名，
        #   导致后面模块级导入的调用变成 UnboundLocalError）
        board = active_blackboard(runtime)
        positive_control_smiles = (board.positive_control if board is not None else "") or ""
    if not positive_control_smiles.strip():
        raise ValueError("缺少阳性对照 SMILES（本次任务未提供阳性对照，按要求跳过对照分析）")
    report = compute_binding_report(molecules, positive_control_smiles.strip())
    board = active_blackboard(runtime)
    if board is not None:
        board.set_binding(report.get("rows") or [])
        board.set_positive_control(report.get("positive_control") or "")
        board.add_note(f"结合模式检测 Agent：完成 {len(report.get('rows') or [])} 个分子的对照比较，"
                       f"结果已写入共享黑板")
    return report


@tool
def positive_control_similarity(molecules_json: str = "", positive_control_smiles: str = "",
                               molecules_file: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """计算每个分子与阳性对照分子的真实指纹相似度（Morgan 与 MACCS，0~1）。

    参数（优先级 文件 > JSON > 共享黑板）：`molecules_file` 可直接给分子库文件或运行产物
    （`molecules_tool.json`）的绝对路径 —— 大库按文件交接；`molecules_json` 适合小库。
    positive_control_smiles: 阳性对照分子 SMILES（留空时取共享黑板/本次任务）。

    返回 JSON：{"status":"ok","positive_control":"...","results":[每分子含
    similarity_to_positive_control（Morgan Tanimoto，越接近 1 越相似）、
    maccs_tanimoto、combined_similarity、structural_consistency]}
    """
    try:
        report = _analyze(molecules_json, positive_control_smiles, molecules_file, runtime=runtime)
        rows = [{k: r.get(k) for k in _SIMILARITY_FIELDS} for r in report["rows"]]
        return json.dumps({"status": "ok", "positive_control": report["positive_control"],
                           "results": rows}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("阳性对照相似度计算失败")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


#: 对照分子属性里给 Agent 的字段（报告 §5.3 只用相似度/一致性/锚定匹配；其余是审计明细）
_CONTROL_PROPERTY_FIELDS = ("smiles", "protonated_smiles", "formula", "molecular_weight", "logP",
                            "tpsa", "hbd", "hba", "rotatable_bonds", "heavy_atoms",
                            "aromatic_rings", "lipinski_violations", "drug_likeness_pass")


def _compact_properties(props: Any) -> Any:
    """压掉对照属性里的逐字段散文（完整内容仍在 `binding_tool.json` 产物里）。"""
    if not isinstance(props, dict):
        return props
    from docking_agent.core.protonation import compact_protonation

    out = {k: props[k] for k in _CONTROL_PROPERTY_FIELDS if props.get(k) is not None}
    prot = compact_protonation(props.get("protonation"))
    if prot:
        out["protonation"] = {k: prot[k] for k in ("policy", "applied") if k in prot}
    return out


@tool
def binding_mode_analysis(molecules_json: str = "", positive_control_smiles: str = "",
                          molecules_file: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """结合模式检测：综合双指纹相似度与药效团特征，对比分子与阳性对照的结合模式。

    参数（优先级 文件 > JSON > 共享黑板）：
      - molecules_file: **推荐**。分子库文件或运行产物路径（`molecules_tool.json`）；
      - molecules_json: [{"name":"M1","smiles":"..."}, ...]（小库可用）；
      - 都留空: 用共享黑板。
    positive_control_smiles: 阳性对照 SMILES（留空时取共享黑板/本次任务）。

    返回 JSON：{"status":"ok","positive_control":"...","control_properties":{...},
    "control_pharmacophore":{...},"results":[每分子含
      similarity_to_positive_control / morgan_tanimoto / maccs_tanimoto / combined_similarity
      （真实指纹相似度）、pharmacophore（脒基/胍基/羧酸/磺酰胺/芳环等 SMARTS 匹配）、
      anchor_match（是否与对照共享 S1 口袋锚定基团）、mw_delta / logp_delta / tpsa_delta、
      structural_consistency（high/medium/low）与 binding_mode_hint（结合模式判断）]}
    """
    try:
        report = _analyze(molecules_json, positive_control_smiles, molecules_file, runtime=runtime)
        rows = report["rows"]
        # 完整明细落盘；大库只回传摘要 + 前 N 条（上下文不被分子数撑爆）
        tool_io.record("binding", report, run=active_run(runtime))
        limit = tool_io.summary_limit()
        if len(rows) > limit:
            base = {
                "status": "ok",
                "positive_control": report["positive_control"],
                "control_properties": report.get("control_properties"),
                "control_pharmacophore": report.get("control_pharmacophore"),
                "results_total": len(rows),
                "results": tool_io.top_rows(rows, limit=limit),
                "detail_omitted": True,
                "summary": {
                    "analyzed": len(rows),
                    "consistent_high": len([r for r in rows
                                            if r.get("structural_consistency") == "high"]),
                    "anchor_match": len([r for r in rows if r.get("anchor_match")]),
                    "mean_similarity": round(
                        sum(float(r.get("similarity_to_positive_control") or 0) for r in rows)
                        / len(rows), 4) if rows else None,
                },
                "artifacts": tool_io.artifact_refs(run=active_run(runtime)),
                "notice": tool_io.big_payload_notice("结合模式结果", len(rows), limit),
            }
            return json.dumps(base, ensure_ascii=False)
        return json.dumps({
            "status": "ok",
            "positive_control": report["positive_control"],
            "control_properties": _compact_properties(report.get("control_properties")),
            "control_pharmacophore": report.get("control_pharmacophore"),
            "results": rows,
        }, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("结合模式分析失败")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


@tool
def check_binding_consistency(docking_file: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """交叉核验：把共享黑板里的**对接结果**与**结合模式结果**对齐，标记需要关注的情形。

    这是跨 Agent 复核：对接结果来自「Docking 执行 Agent」，结合模式结果来自本 Agent，
    两者在共享黑板上对齐后可以给出：
      - affinity_strong_low_similarity：对接强但与对照骨架不像 → 可能是不同结合模式，需确认
      - similar_but_weak_docking：骨架像但对接弱 → 检查位点盒/构象或该分子并非该位点配体
      - missing_docking / missing_binding：数据不完整
      - consistent：两者一致（强对接 + 相似，或弱对接 + 不相似）

    docking_file: 可选。对接结果文件（`docking_tool.json`）路径 —— **文件优先**：大库的对接明细
        直接读文件，不必经黑板搬运（黑板此时可只放计数等小状态）。

    返回 JSON：{"status":"ok","total":n,"flags":{...计数...},"rows":[{name,smiles,affinity,
    similarity,anchor_match,verdict,hint}],"summary":"..."}
    """
    board = active_blackboard(runtime)
    if board is None:
        return json.dumps({"status": "error",
                           "message": "当前没有共享黑板（非多 Agent 运行），无法交叉核验"},
                          ensure_ascii=False)
    docking: Dict[str, Dict[str, Any]] = {}
    if (docking_file or "").strip() and os.path.isfile(str(docking_file).strip()):
        rows = _rows_from_docking_file(str(docking_file).strip())
        if rows:
            docking = {r["smiles"]: r for r in rows if r.get("smiles")}
            logger.info("交叉核验：对接结果按文件读入 %s 条（%s）", len(docking),
                        os.path.basename(str(docking_file)))
    if not docking:
        docking = {r.get("smiles"): r for r in board.docking()}
    binding = {r.get("smiles"): r for r in board.binding()}
    if not docking and not binding:
        return json.dumps({"status": "no_data",
                           "message": "黑板上还没有对接或结合模式结果"}, ensure_ascii=False)

    control = board.positive_control or ""
    pc_affinity = None
    if control and control in docking:
        value = docking[control].get("affinity_kcal_mol")
        if isinstance(value, (int, float)):
            pc_affinity = value
    if pc_affinity is None:   # 兜底：对接结果里名字带「阳性对照」的那条
        for r in docking.values():
            if str(r.get("name") or "").startswith("阳性对照") and \
                    isinstance(r.get("affinity_kcal_mol"), (int, float)):
                pc_affinity = r["affinity_kcal_mol"]
                break

    rows, flags = [], {"consistent": 0, "affinity_strong_low_similarity": 0,
                       "similar_but_weak_docking": 0, "missing_docking": 0, "missing_binding": 0,
                       "control": 0}
    for smiles in sorted(set(docking) | set(binding)):
        d, b = docking.get(smiles, {}), binding.get(smiles, {})
        affinity = d.get("affinity_kcal_mol")
        sim = b.get("similarity_to_positive_control")
        anchor = b.get("anchor_match")
        if control and smiles == control:
            # 阳性对照是基线，不参与「强弱/像不像」的判定
            flags["control"] += 1
            rows.append({"name": d.get("name") or b.get("name") or smiles, "smiles": smiles,
                         "affinity_kcal_mol": affinity, "similarity_to_positive_control": sim,
                         "anchor_match": anchor, "verdict": "control",
                         "hint": "阳性对照（比较基线），不参与一致性判定"})
            continue
        if not d:
            verdict, hint = "missing_docking", "缺少对接结果，无法判断结合强度"
        elif not b:
            verdict, hint = "missing_binding", "缺少结合模式数据，无法与对照比较"
        else:
            strong = isinstance(affinity, (int, float)) and (
                isinstance(pc_affinity, (int, float)) and affinity < pc_affinity)
            similar = isinstance(sim, (int, float)) and sim >= 0.4
            if strong and not similar:
                verdict = "affinity_strong_low_similarity"
                hint = ("对接强于对照但与对照骨架差异较大：可能以不同取向结合"
                        + ("（但共享锚定基团）" if anchor else "（且不共享锚定基团，建议人工确认）"))
            elif similar and not strong:
                verdict = "similar_but_weak_docking"
                hint = "与对照骨架相似但对接不强：建议复核位点盒与构象，或该分子并非该位点配体"
            else:
                verdict = "consistent"
                hint = "对接强度与结构相似性判断一致"
        flags[verdict] = flags.get(verdict, 0) + 1
        rows.append({"name": d.get("name") or b.get("name") or smiles, "smiles": smiles,
                     "affinity_kcal_mol": affinity,
                     "similarity_to_positive_control": sim, "anchor_match": anchor,
                     "verdict": verdict, "hint": hint})

    board.add_note(f"结合模式检测 Agent：完成交叉核验（{len(rows)} 条，"
                   f"可疑 {flags['affinity_strong_low_similarity'] + flags['similar_but_weak_docking']} 条）")
    summary = (f"共核验 {len(rows)} 个分子（对照亲和力 {pc_affinity}）：一致 {flags['consistent']}，"
               f"强对接但骨架不像 {flags['affinity_strong_low_similarity']}，"
               f"骨架像但对接弱 {flags['similar_but_weak_docking']}，"
               f"缺对接 {flags['missing_docking']}，缺结合模式 {flags['missing_binding']}")
    return json.dumps({"status": "ok", "total": len(rows), "flags": flags,
                       "rows": rows, "summary": summary}, ensure_ascii=False)
