"""推荐化合物排行工具（协调 Agent 用）。

两个工具，分工明确、都不需要把明细搬进上下文：

1. `recommend_compounds(top_n)` —— **算**：从运行产物/共享黑板读全量对接与理化性质，
   算出综合分（对接亲和力 + 配体效率 + 类药性 + 理化窗口）与规则化的筛选建议，
   把完整排行落盘、并只回传前 N 行给模型；
2. `submit_recommendations(recommendations_json)` —— **说理由**：模型对前 N 个分子逐条给
   自然语言理由与推进建议；工具按 smiles/name **核对分子确实存在于本次排行**，
   匹配不上的条目如实回传（不静默丢弃，也不允许模型凭记忆编造分子）。

数值一律来自 `reporting/recommend.py` 的真实计算，模型只补"为什么"，
报告把数值与理由并排展示 —— 数值可信、理由可核。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

from langchain.tools import tool

from docking_agent.runtime import tool_io
from docking_agent.core import POSITIVE_CONTROL_NAME, merge_and_rank
from docking_agent.reporting import rank_molecules
from docking_agent.reporting.recommend import (
    DEFAULT_TOP_N,
    WEIGHT_LABELS,
    WEIGHT_KEYS,
    build_recommendations,
)
from docking_agent.runs import current_run
from docking_agent.runtime.context import AgentContext, active_blackboard, active_run
from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)

from docking_agent.reporting.fields import REPORT_FIELD_LABELS  # noqa: E402

#: 排行榜里回传给模型的字段（明细已在产物文件里，模型只需要能写理由的部分）
_ROW_FIELDS = ("rank", "id", "name", "smiles", "composite", "grade", "grade_gate",
               "affinity_kcal_mol", "ligand_efficiency", "molecular_weight", "logP",
               "tpsa", "rotatable_bonds", "lipinski_violations", "drug_likeness_pass",
               "components", "suggestions", "box_group", "missing",
               # 已写入的理由也要回传：模型才能"接着写"而不是重复写或写反
               "agent_reason", "agent_suggestion")


def _default_top_n() -> int:
    try:
        from docking_agent.config import env_int

        return max(1, env_int("RECOMMEND_TOP_N", DEFAULT_TOP_N))
    except Exception:  # noqa: BLE001
        return DEFAULT_TOP_N


def _rows_from_file(name: str) -> List[Dict[str, Any]]:
    """读工具产物文件里的行（对接/性质），兼容 {receptors:[{results:[]}]} 结构。"""
    payload = tool_io.load(name)
    return _flatten_rows(payload)


def _flatten_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            return [r for r in payload["results"] if isinstance(r, dict)]
        rows: List[Dict[str, Any]] = []
        for block in payload.get("receptors") or []:
            if isinstance(block, dict):
                rows.extend(r for r in (block.get("results") or []) if isinstance(r, dict))
        if rows:
            return rows
        for key in ("rows", "molecules", "assessment"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
    return []


def current_ranking(runtime: Any = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """收集本次运行的完整排序结果（文件优先，其次共享黑板）。

    与落盘层（`agents/persistence.py`）同一口径：文件是 Agent 之间交接数据的总线，
    黑板只用于补救「还没落盘」的中间状态。
    """

    molecules = _rows_from_file("molecules")
    properties = _rows_from_file("properties")
    docking_rows = [r for r in _rows_from_file("docking") if r.get("name") != POSITIVE_CONTROL_NAME]
    binding_rows: List[Dict[str, Any]] = _rows_from_file("binding")

    pos_control: Dict[str, Any] = {}
    board = active_blackboard(runtime)
    if board is not None:
        if not molecules:
            molecules = [m for m in (board.molecules() or []) if isinstance(m, dict)]
        if not properties:
            properties = [p for p in (board.properties() or []) if isinstance(p, dict)]
        if not docking_rows:
            docking_rows = [r for r in (board.docking() or [])
                            if isinstance(r, dict) and r.get("name") != POSITIVE_CONTROL_NAME]
        if not binding_rows:
            binding_rows = [r for r in (board.binding() or []) if isinstance(r, dict)]
        if board.positive_control:
            pos_control["smiles"] = board.positive_control
    if not docking_rows:
        return [], pos_control
    binding = {"rows": binding_rows} if binding_rows else {}
    try:
        ranked = rank_molecules(merge_and_rank(properties or molecules, docking_rows, binding))
    except Exception as e:  # noqa: BLE001
        logger.warning("推荐排行：结果融合失败，退回对接行原始顺序：%s", e)
        ranked = rank_molecules(docking_rows)
    return ranked, pos_control


def _agent_reasons(run: Any) -> List[Dict[str, Any]]:
    if run is None:
        return []
    stored = (getattr(run, "data", None) or {}).get("recommendation_reasons") or []
    return [r for r in stored if isinstance(r, dict)]


def _weights_text() -> str:
    try:
        from docking_agent.config import env

        return env("RANK_WEIGHTS", "") or ""
    except Exception:  # noqa: BLE001
        return ""


def build_for_run(run: Any = None, top_n: int = 0) -> Dict[str, Any]:
    """构建推荐排行（工具与落盘层共用；numeric 全部来自真实计算）。"""
    run = run if run is not None else current_run.get()
    ranking, pos_control = current_ranking()
    limit = int(top_n or 0) or _default_top_n()
    rec = build_recommendations(
        ranking, top_n=limit, weight_text=_weights_text(),
        reasons=_agent_reasons(run),
        positive_control_name=POSITIVE_CONTROL_NAME,
    )
    rec["positive_control"] = pos_control
    if run is not None:
        try:
            run.data["recommendations"] = {
                "top_n": rec.get("top_n"), "weights": rec.get("weights"),
                "scored_total": rec.get("scored_total"), "grades": rec.get("grades"),
                "rows": [{k: r.get(k) for k in _ROW_FIELDS} for r in (rec.get("rows") or [])],
            }
            run.save()
        except Exception as e:  # noqa: BLE001 - 记账失败不影响返回
            logger.debug("推荐排行写入运行数据失败：%s", e)
    return rec


@tool
def recommend_compounds(top_n: int = 0, runtime: ToolRuntime[AgentContext] = None) -> str:
    """计算并返回**推荐化合物排行**（对接亲和力 + 配体效率 + 类药性 + 理化性质 综合分）。

    top_n: 返回前多少名（0 = 用设置页的「推荐排行条数」，默认 10）。

    综合分口径（透明、绝对尺度，不按本批库归一化）：
      综合分 = w1×亲和力分 + w2×配体效率分 + w3×类药性分 + w4×理化性质分
      亲和力分 = clamp(−ΔG / 12, 0, 1)；配体效率分 = clamp(LE / 0.45, 0, 1)（LE = −ΔG/重原子数）；
      类药性分 = clamp(1 − 0.25×Lipinski 违例数, 0, 1)；理化性质分 = (logP∈[0,4] 得分 + TPSA≤120 得分)/2。
      权重默认 0.45/0.20/0.20/0.15（可在设置页改）。亲和力弱于 −6 kcal/mol 的分子等级封顶为 C。

    缺少某项数据的分子该分量按 0 计入并在 missing 中标注；没有对接分数的分子不进排行（计入 excluded）。

    **用法**：先调用本工具拿到前 N 名的真实数值与规则化建议，然后**逐条**用
    `submit_recommendations` 写「为什么推荐/如何推进」的理由（依据只能来自这里的数值与
    产物文件里的事实，不要编造分子或数值）。不要把手算的综合分写进理由。
    """
    try:
        run = active_run(runtime)
        rec = build_for_run(run, top_n)
        if rec.get("status") != "ok":
            return json.dumps({
                "status": "no_data",
                "message": "还没有可排行的对接结果：请先完成对接（run_docking）。",
            }, ensure_ascii=False)
        limit = tool_io.summary_limit()
        rows = rec["rows"][:limit]
        payload = {
            "status": "ok",
            "top_n": rec["top_n"],
            "scored_total": rec["scored_total"],
            "excluded_total": rec["excluded_total"],
            "grades": rec["grades"],
            "weights": rec["weights"],
            "weight_labels": {k: WEIGHT_LABELS[k] for k in WEIGHT_KEYS},
            "criteria": rec["criteria"],
            "rows": [{k: r.get(k) for k in _ROW_FIELDS} for r in rows],
            "notes": rec["notes"],
            "ordering": ("`rank` 已是最终推荐顺序：先按等级 A→B→C，同一等级内按综合分降序。"
                         "请**原样引用 rank**，不要按亲和力重排（那会与报告第 3.1 节的顺序不一致）。"),
            "next_step": ("请对以上每个分子调用 submit_recommendations 写理由"
                          "（至少覆盖前 3 名；理由要引用这里的分量数值或产物里的真实事实）。"),
            "artifacts": tool_io.artifact_refs(run=active_run(runtime)),
        }
        if len(rec["rows"]) > limit:
            payload["detail_omitted"] = True
            payload["notice"] = (f"共 {len(rec['rows'])} 条上榜，这里只回前 {limit} 条；"
                                 "完整排行见 run.data['recommendations'] 与报告。")
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("推荐排行计算失败")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)



# --------------------------------------------------------------------------- #
# 报告定制：让协调 Agent 按用户的具体要求组织输出（骨架不变、内容可变）
# --------------------------------------------------------------------------- #
#: 允许写进报告的**真实字段**（白名单）——与报告渲染共用同一份定义（reporting/fields.py）
REPORT_FIELD_WHITELIST: Dict[str, str] = dict(REPORT_FIELD_LABELS)

_REPORT_SPEC_MAX_HIGHLIGHTS = 5
_REPORT_SPEC_MAX_REQUIREMENTS = 6
_REPORT_SPEC_MAX_NOTES = 600


def _report_coverage(rows: List[Dict[str, Any]], field: str) -> str:
    """该字段在本次数据里的覆盖率（“有多少行真的有值”）——让 Agent 能如实回应用户。"""
    total = len(rows)
    if not total:
        return "0/0"
    have = sum(1 for r in rows if r.get(field) not in (None, "", [], {}))
    return f"{have}/{total}"


@tool
def customize_report(spec_json: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """按**用户的具体要求**定制最终报告（固定骨架不变，内容与呈现方式可变）。

    用户对输出提要求时调用（做完推荐排行之后），例如「带上小分子 ID / 分子式 / 来源文件」
    → `extra_columns: ["id","formula","source_file"]`；「标题写成 XX」→ `title`；
    「结论里点明为什么选 X」→ `highlights`；「说明你有没有按我说的做」→ `requirements`。

    spec_json 字段：title / extra_columns / highlights / requirements[{"ask","response"}] /
    notes（≤600 字，写进报告「本次要求与响应」一节）。
    extra_columns **只能**从工具白名单里选（name/smiles/id/formula/molecular_weight/logP/tpsa/
    hbd/hba/rotatable_bonds/aromatic_rings/lipinski_violations/drug_likeness_pass/
    affinity_kcal_mol/ligand_efficiency/composite/grade/engine/exhaustiveness/box_group/
    similarity_to_positive_control/maccs_tanimoto/structural_consistency/anchor_match/
    source_index/source_file）。

    返回：accepted（已生效）、coverage（每个附加列的覆盖率，如 "id": "30/30"）、rejected（+原因）。
    **覆盖率不足必须如实说明**（如「输入文件里没有 ID 字段，已按名称展示」），不得假装生效或编造字段值。
    """
    try:
        run = active_run(runtime)
        if run is None:
            return json.dumps({"status": "error", "message": "没有运行上下文"}, ensure_ascii=False)
        try:
            spec = json.loads(spec_json) if (spec_json or "").strip() else {}
        except json.JSONDecodeError as e:
            return json.dumps({"status": "invalid_json", "message": f"spec_json 不是合法 JSON：{e}"},
                              ensure_ascii=False)
        if not isinstance(spec, dict):
            return json.dumps({"status": "invalid_json", "message": "spec_json 必须是 JSON 对象"},
                              ensure_ascii=False)

        rows = _current_ranking_rows(run)
        accepted: Dict[str, Any] = {}
        rejected: List[Dict[str, str]] = []
        coverage: Dict[str, str] = {}

        title = str(spec.get("title") or "").strip()
        if title:
            if len(title) > 120:
                rejected.append({"field": "title", "reason": "标题超过 120 字"})
            else:
                accepted["title"] = title

        sections = spec.get("sections") or []
        if isinstance(sections, list) and sections:
            clean_sections = []
            for item in sections[:12]:            # 上限 12 节，避免报告被写爆
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()[:120]
                body = str(item.get("body") or "").strip()[:4000]
                if title or body:
                    clean_sections.append({"title": title, "body": body})
            if clean_sections:
                accepted["sections"] = clean_sections
        columns = spec.get("extra_columns") or []
        if not isinstance(columns, list):
            rejected.append({"field": "extra_columns", "reason": "必须是数组"})
            columns = []
        keep: List[str] = []
        for raw in columns[:12]:
            key = str(raw or "").strip()
            if not key:
                continue
            if key not in REPORT_FIELD_WHITELIST:
                rejected.append({"field": key, "reason": "不在允许的字段白名单里（请改用白名单字段）"})
                continue
            cov = _report_coverage(rows, key)
            coverage[key] = cov
            if cov.split("/")[0] == "0":
                rejected.append({"field": key, "reason": f"本次数据里没有该字段的值（覆盖率 {cov}）"})
                continue
            if key not in keep:
                keep.append(key)
        if keep:
            accepted["extra_columns"] = keep

        highlights = [str(x).strip()[:200] for x in (spec.get("highlights") or [])
                      if str(x or "").strip()][:_REPORT_SPEC_MAX_HIGHLIGHTS]
        if highlights:
            accepted["highlights"] = highlights

        requirements: List[Dict[str, str]] = []
        for item in (spec.get("requirements") or [])[:_REPORT_SPEC_MAX_REQUIREMENTS]:
            if not isinstance(item, dict):
                continue
            ask = str(item.get("ask") or "").strip()[:200]
            response = str(item.get("response") or "").strip()[:400]
            if ask or response:
                requirements.append({"ask": ask, "response": response})
        if requirements:
            accepted["requirements"] = requirements

        notes = str(spec.get("notes") or "").strip()[:_REPORT_SPEC_MAX_NOTES]
        if notes:
            accepted["notes"] = notes

        run.data["report_customization"] = accepted
        run.save()
        logger.info("报告定制已记录：%s", json.dumps(accepted, ensure_ascii=False)[:300])
        return json.dumps({
            "status": "ok" if accepted else "nothing_applied",
            "accepted": accepted,
            "coverage": coverage,
            "rejected": rejected,
            "field_labels": {k: REPORT_FIELD_WHITELIST[k] for k in accepted.get("extra_columns", [])},
            "notice": ("已写入运行记录，最终报告会照此输出。rejected 里的项请在回复里如实说明"
                       "（例如数据里没有该字段），不要假装生效。"),
        }, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("报告定制失败")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


def _current_ranking_rows(run: Any) -> List[Dict[str, Any]]:
    """当前可用的排行行（优先落盘的 recommendations，其次共享黑板/产物），用于核对字段覆盖率。"""
    rows = [r for r in ((getattr(run, "data", None) or {}).get("recommendations") or [])
            if isinstance(r, dict)]
    if rows:
        return rows
    try:
        from docking_agent.runtime import tool_io

        loaded = tool_io.load("ranking", run=run)
        if isinstance(loaded, list) and loaded:
            return [r for r in loaded if isinstance(r, dict)]
    except Exception:  # noqa: BLE001 - 核覆盖率失败不该拦截定制
        logger.debug("读取排行行失败", exc_info=True)
    return []


@tool
def submit_recommendations(recommendations_json: str, runtime: ToolRuntime[AgentContext] = None) -> str:
    """提交**协调 Agent 撰写的推荐理由与推进建议**（报告「推荐化合物排行」一节使用）。

    recommendations_json 形如：
    [{"name": "Fluralaner", "reason": "亲和力 −10.14 kcal/mol 为库内最优，配体效率 0.27…",
      "suggestion": "建议提高 exhaustiveness 复算后优先做结合实验", "priority": 1}, ...]

    每个条目用 `name` 或 `smiles` 指定分子（两者都给最稳）。工具会**核对分子确实在本次排行
    里**：核对不上的条目原样回传在 unmatched 中（不写入报告），请改正后重新提交。
    `reason` 必填且必须基于真实数据（工具输出的分值、分量或产物文件中的事实），
    不要写没有依据的推测；`suggestion` 选填。
    """
    try:
        run = active_run(runtime)
        text = str(recommendations_json or "").strip()
        if not text:
            return json.dumps({"status": "error",
                               "message": "recommendations_json 为空：请提供至少一条理由。"},
                              ensure_ascii=False)
        try:
            items = json.loads(text)
        except json.JSONDecodeError as e:
            return json.dumps({"status": "error",
                               "message": f"recommendations_json 不是合法 JSON：{e}"},
                              ensure_ascii=False)
        if isinstance(items, dict):
            items = items.get("recommendations") or [items]
        if not isinstance(items, list) or not items:
            return json.dumps({"status": "error",
                               "message": "recommendations_json 必须是条目数组。"},
                              ensure_ascii=False)

        rec = build_for_run(run, 0)
        if rec.get("status") != "ok":
            return json.dumps({"status": "no_data",
                               "message": "还没有排行数据：请先完成对接并调用 recommend_compounds。"},
                              ensure_ascii=False)
        valid_smiles = {str(r.get("smiles") or "") for r in rec["rows"]}
        valid_names = {str(r.get("name") or "") for r in rec["rows"]}
        valid_smiles.discard("")
        valid_names.discard("")

        accepted: List[Dict[str, Any]] = []
        unmatched: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                unmatched.append({"item": item, "why": "条目不是对象"})
                continue
            name = str(item.get("name") or "").strip()
            smiles = str(item.get("smiles") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if not (name or smiles):
                unmatched.append({"item": item, "why": "缺少 name/smiles"})
                continue
            if (smiles and smiles in valid_smiles) or (name and name in valid_names):
                if not reason:
                    unmatched.append({"name": name, "smiles": smiles,
                                      "why": "reason 为空：请写明推荐理由（依据真实数值/事实）"})
                    continue
                accepted.append({"name": name, "smiles": smiles, "reason": reason,
                                 "suggestion": str(item.get("suggestion") or "").strip(),
                                 "priority": item.get("priority")})
            else:
                unmatched.append({"name": name, "smiles": smiles,
                                  "why": "该分子不在本次排行里（请用 recommend_compounds 返回的名称/SMILES）"})

        if run is not None and accepted:
            merged = {str(r.get("smiles") or r.get("name")): r
                      for r in _agent_reasons(run)}
            for entry in accepted:
                merged[str(entry.get("smiles") or entry.get("name"))] = entry
            run.data["recommendation_reasons"] = list(merged.values())
            run.save()
            try:
                run.log(f"推荐理由：已记录 {len(accepted)} 条（未匹配 {len(unmatched)} 条）")
            except Exception as e:  # noqa: BLE001 - 运行日志失败不影响结果
                logger.debug("写推荐理由运行日志失败：%s", e)
        return json.dumps({
            "status": "ok" if not unmatched else "partial",
            "saved": len(accepted),
            "saved_names": [e.get("name") or e.get("smiles") for e in accepted],
            "unmatched": unmatched,
            "message": ("理由已写入本次运行，将出现在报告的「推荐化合物排行」一节；"
                        "**不要在最终回答里重复整张排行表**（报告已有），只讲结论与取舍。"),
        }, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("推荐理由提交失败")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
