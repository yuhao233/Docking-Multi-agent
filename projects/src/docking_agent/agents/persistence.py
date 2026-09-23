"""多 Agent 运行的结果落盘。

多 Agent 模式下没有「一次调用返回全部结果」的入口，因此运行结束后
从**消息历史中的工具真实返回**（ToolMessage）提取数据并落盘，
既保证中间数据完整，也保证落盘内容全部来自工具而非模型叙述。

注意：协调 Agent 可能把同一类任务拆成多次工具调用（例如先对接候选分子、再单独对接阳性对照），
因此这里对**同名工具的多次返回做合并**，而不是只取最后一次。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from docking_agent.core import merge_and_rank
from docking_agent.reporting import (
    build_markdown_report,
    build_ranking_csv,
    collect_failures,
    copy_receptor_files,
    rank_molecules,
    write_report_charts,
    write_report_pdf,
)
from docking_agent.runtime import tool_io
from docking_agent.core.docking import POSITIVE_CONTROL_NAME
from docking_agent.runtime.payload import as_text
from docking_agent.runs import Run

logger = logging.getLogger(__name__)


# 子 Agent 工具 → 分发工具（同一件事的两级命名，合并时都要看）
DOCKING_TOOLS = ("run_docking", "molecular_docking")
# 口袋分析工具（分发工具 + 子 Agent 内部工具）
POCKET_TOOLS = ("run_pocket_analysis", "predict_binding_pockets",
                "compare_pocket_with_experiment", "set_docking_site")
# 对接结果里必须**以工具原始输出为准**的溯源字段：子 Agent 转述时可能删减，
# 因此合并时只要有哪个 payload 带了就补上（否则报告/界面会丢失盒子来源）
_PROVENANCE_KEYS = ("box_source", "box_chosen_by", "box_validation", "pockets",
                    "box_warnings", "site", "pdbqt", "protein",
                    # C 方案：库级下限/分组信息必须由工具原始输出补回（报告「对接盒」行要用）
                    "box_library_floor", "box_group_sizes", "box_group_counts",
                    # 受体准备丢弃/保留了哪些杂原子：模型转述时容易漏，必须由工具原始输出补回
                    "dropped_hetatm", "kept_hetatm", "dropped_waters", "cocrystal_ligand",
                    "unsupported_hetatm")
PROPERTY_TOOLS = ("run_property_assessment", "molecular_property_assessment")
BINDING_TOOLS = ("run_binding_mode_analysis", "binding_mode_analysis", "positive_control_similarity")


def _agent_models(run: Run) -> Dict[str, Any]:
    """各 Agent 角色实际使用的模型（每个角色一个独立 LLM 实例）。

    以 API 在运行开始时记录的快照为底，再合并**当前**实例登记表：
    这样报告与运行记录里各角色的模型与调用次数与真实调用一致。
    """
    merged: Dict[str, Any] = {}
    recorded = run.data.get("agent_models")
    if isinstance(recorded, dict):
        merged.update(recorded)
    try:
        from docking_agent.runtime.llm import llm_registry

        for role, meta in llm_registry().items():
            entry = dict(merged.get(role) or {})
            entry.update({k: v for k, v in meta.items() if v is not None})
            merged[role] = entry
    except Exception:  # noqa: BLE001
        logger.debug("读取 LLM 实例登记表失败", exc_info=True)
    return merged


def extract_tool_data(messages: List[Any]) -> tuple[Dict[str, List[Any]], Dict[str, Any]]:
    """从消息历史提取 (工具返回列表, 工具调用参数)，同名工具保留**全部**返回。"""
    outputs: Dict[str, List[Any]] = {}
    args: Dict[str, Any] = {}
    for m in messages or []:
        name = type(m).__name__
        if name == "AIMessage":
            for tc in (getattr(m, "tool_calls", None) or []):
                args[tc.get("name")] = tc.get("args") or {}
        elif name == "ToolMessage":
            tool_name = getattr(m, "name", None) or ""
            raw = as_text(getattr(m, "content", ""))
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                parsed = {"raw": raw[:4000]}
            outputs.setdefault(tool_name, []).append(parsed)
    return outputs, args


def _dicts(outputs: Dict[str, List[Any]], names: tuple) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for n in names:
        out.extend([o for o in (outputs.get(n) or []) if isinstance(o, dict)])
    return out


def _docking_rows(payload: Any) -> int:
    """对接返回体里的结果行总数（用于判断「合并后是否真的多了数据」）。"""
    total = 0
    if isinstance(payload, dict):
        for blk in (payload.get("receptors") or []):
            total += len(blk.get("results") or [])
    return total


def _dock_precision(row: Dict[str, Any]) -> tuple:
    """同一分子的两轮结果谁更可信：显式 pass=fine > 搜索强度更高 > 后者优先。"""
    fine = 1 if str(row.get("pass") or "").lower() == "fine" else 0
    try:
        exh = int(row.get("exhaustiveness") or 0)
    except (TypeError, ValueError):
        exh = 0
    return (fine, exh)


def _merge_docking(outputs: Dict[str, List[Any]]) -> Dict[str, Any]:
    """合并多次对接调用（按工具名）→ 见 `_merge_docking_payloads`。"""
    return _merge_docking_payloads(_dicts(outputs, DOCKING_TOOLS))


def _merge_docking_payloads(payloads: List[Any]) -> Dict[str, Any]:
    """合并多次对接调用的**返回体**：按受体归并结果，并按 (name, smiles) 去重。

    漏斗场景下同一分子会出现两次（粗筛 + 精算）：**必须保留精度更高的那条**，
    否则排序里会混入低精度分数；粗筛分值保留在 `affinity_coarse` 里便于对比。
    也用于「工具产物（只有最近一次调用）＋ 消息历史（全部调用）」的合并 —— 否则
    分批对接 / 蛋白质库场景里，早先那次调用的受体块会被静默丢掉。
    """
    blocks: Dict[str, Dict[str, Any]] = {}
    notes: List[str] = []
    for out in payloads:
        notes.extend(out.get("notes") or [])
        for blk in (out.get("receptors") or []):
            key = blk.get("receptor_key") or blk.get("receptor") or "receptor"
            tgt = blocks.setdefault(key, {**blk, "results": []})
            for field in _PROVENANCE_KEYS:      # 工具原始输出优先，补全模型转述时删掉的字段
                value = blk.get(field)
                if value not in (None, "", [], {}) and tgt.get(field) in (None, "", [], {}):
                    tgt[field] = value
            index = {(r.get("name"), r.get("smiles")): i for i, r in enumerate(tgt["results"])}
            for r in (blk.get("results") or []):
                sig = (r.get("name"), r.get("smiles"))
                if sig not in index:
                    index[sig] = len(tgt["results"])
                    tgt["results"].append(r)
                    continue
                pos = index[sig]
                if _dock_precision(r) > _dock_precision(tgt["results"][pos]):
                    previous = tgt["results"][pos]
                    if r.get("affinity_coarse") is None and previous.get("affinity_kcal_mol") is not None:
                        r = {**r, "affinity_coarse": previous.get("affinity_kcal_mol")}
                    tgt["results"][pos] = r
    if not blocks:
        return {}
    return {"status": "ok", "receptors": list(blocks.values()), "notes": sorted(set(notes))}


def _merge_pockets(outputs: Dict[str, List[Any]]) -> Dict[str, Any]:
    """合并口袋分析结果（预测 + 与实验位点比对 + Agent 提交的位点）。"""
    pockets: List[Dict[str, Any]] = []
    engine = ""
    selected: Dict[str, Any] = {}
    validation: Dict[str, Any] = {}
    source = ""
    reference: Dict[str, Any] = {}
    warnings: List[str] = []
    for out in _dicts(outputs, POCKET_TOOLS):
        engine = engine or str(out.get("engine") or "")
        if out.get("pockets") and not pockets:
            pockets = [p for p in out["pockets"] if isinstance(p, dict)]
        for receptor in (out.get("receptors") or []):
            if not isinstance(receptor, dict):
                continue
            if receptor.get("pockets") and not pockets:
                pockets = [p for p in receptor["pockets"] if isinstance(p, dict)]
            engine = engine or str(receptor.get("engine") or "")
            reference = reference or (receptor.get("reference") or {})
            validation = validation or (receptor.get("validation") or {})
            suggested = receptor.get("suggested") or {}
            if suggested.get("source") and not source:
                source = str(suggested["source"])
            warnings.extend(receptor.get("warnings") or [])
        site = out.get("site")
        if isinstance(site, dict) and site.get("center"):
            selected = site
            source = str(site.get("source") or source)
            validation = site.get("validation") or validation
            if not pockets and isinstance(site.get("pocket"), dict) and site["pocket"].get("center"):
                pockets = [site["pocket"]]
    if not (pockets or selected):
        return {}
    return {"engine": engine, "pockets": pockets, "selected": selected,
            "validation": validation, "reference": reference, "source": source,
            "warnings": sorted(set(warnings))}


def _merge_properties(outputs: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    return _merge_properties_payloads(_dicts(outputs, PROPERTY_TOOLS))


def _merge_properties_payloads(payloads: List[Any]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for out in payloads:
        for p in (out.get("assessment") or []):
            if isinstance(p, dict) and p.get("smiles"):
                merged[p["smiles"]] = p
    return list(merged.values())


def _merge_binding(outputs: Dict[str, List[Any]]) -> Dict[str, Any]:
    """合并结合模式结果，并归一化为 {"rows": [...]}（融合与报告统一使用 rows）。"""
    rows: Dict[str, Dict[str, Any]] = {}
    pc_smiles = ""
    for out in _dicts(outputs, BINDING_TOOLS):
        pc_smiles = pc_smiles or (out.get("positive_control") or "")
        for r in (out.get("results") or out.get("rows") or []):
            if isinstance(r, dict) and r.get("smiles"):
                rows.setdefault(r["smiles"], r)
    if not rows:
        return {}
    ordered = sorted(rows.values(),
                     key=lambda r: r.get("similarity_to_positive_control") or 0.0, reverse=True)
    return {"positive_control": pc_smiles, "rows": ordered, "results": ordered}


def _dedupe_molecules(molecules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for m in molecules:
        smi = (m or {}).get("smiles")
        if smi and smi not in seen:
            seen.add(smi)
            out.append({"name": m.get("name") or smi, "smiles": smi})
    return out


#: 模型可见消息日志的规模上限：条数与每条正文截断（只用于"气泡里到底说了什么"的可观测性，
#: 不参与任何计算；真实困扰 2026-09-23：用户反馈"气泡把大量数据对话出去了"，但运行产物里
#: 只有最终结论文本，事后无法回看当时模型看到了/输出了什么）。
MESSAGES_LOG_LIMIT = 200
MESSAGES_LOG_HEAD = 400


def build_messages_log(messages: List[Any], final_text: str = "") -> List[Dict[str, Any]]:
    """把模型可见消息压成**可审计的摘要日志**（角色/工具名/长度/前 N 字符）。

    只记录形状与开头，不落全量正文：既能回答"这一轮模型看到了多大的载荷、输出了什么"，
    又不会把 8 MB 的工具 JSON 再存一份。条数取最后 `MESSAGES_LOG_LIMIT` 条（长任务里早期
    步骤的价值低于最近几步）。
    """
    out: List[Dict[str, Any]] = []
    for message in list(messages or [])[-MESSAGES_LOG_LIMIT:]:
        entry: Dict[str, Any] = {"role": type(message).__name__}
        name = getattr(message, "name", None)
        if name:
            entry["tool"] = str(name)
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            entry["tool_calls"] = [
                {"name": str((c or {}).get("name") or ""),
                 "args_chars": len(json.dumps((c or {}).get("args") or {}, ensure_ascii=False))}
                for c in calls]
        text = as_text(getattr(message, "content", ""))
        entry["chars"] = len(text)
        if text:
            entry["head"] = text[:MESSAGES_LOG_HEAD]
        out.append(entry)
    if final_text:
        out.append({"role": "FinalAnswer", "chars": len(final_text),
                    "head": final_text[:MESSAGES_LOG_HEAD]})
    return out


def persist_agent_run(run: Run, messages: List[Any], final_text: str) -> Dict[str, Any]:
    """把多 Agent 运行的真实工具输出写入运行目录，返回 result 字典。"""
    outputs, args = extract_tool_data(messages)
    # 模型可见消息的形状日志：气泡/载荷争议事后可查（不存全量正文，见 build_messages_log）
    try:
        run.write_json("messages_log", build_messages_log(messages, final_text),
                       label="模型可见消息日志（角色/工具/长度/开头）")
    except Exception:  # noqa: BLE001 - 日志落盘失败不能影响运行收尾
        logger.debug("消息日志落盘失败", exc_info=True)

    # ---- 数据来源：**工具产物优先**，模型回显的工具消息仅作兜底 ----
    # 大库时工具只把「摘要」回传给模型，明细写在这些产物文件里（见 runtime/tool_io.py），
    # 因此落盘/报告不再依赖模型把上万条结果搬运回上下文（那在物理上也不可能）。
    sources: Dict[str, str] = {}

    molecules: List[Dict[str, Any]] = []
    from_file = tool_io.load("molecules", run=run)
    if isinstance(from_file, list) and from_file:
        molecules = list(from_file)
        seen_smiles = {str(m.get("smiles") or "") for m in molecules if isinstance(m, dict)}
        extra: List[Dict[str, Any]] = []
        for out in _dicts(outputs, ("import_molecule_library",)):
            for m in (out.get("molecules") or []):
                if isinstance(m, dict) and str(m.get("smiles") or "") not in seen_smiles:
                    seen_smiles.add(str(m.get("smiles") or ""))
                    extra.append(m)
        if extra:                                   # 多次导入（例如分两批给分子）→ 取并集
            molecules = molecules + extra
            sources["molecules"] = "tool_file+tool_message"
        else:
            sources["molecules"] = "tool_file"
    else:
        for out in _dicts(outputs, ("import_molecule_library",)):
            molecules.extend(out.get("molecules") or [])
        sources["molecules"] = "tool_message"

    from_file = tool_io.load("properties", run=run)
    from_msgs = _merge_properties(outputs)
    if isinstance(from_file, list) and from_file:
        merged_props = {p.get("smiles"): p for p in from_file
                        if isinstance(p, dict) and p.get("smiles")}
        for prop in from_msgs:                     # 消息里多出来的分子补进来（产物优先，不覆盖）
            if isinstance(prop, dict) and prop.get("smiles") and prop["smiles"] not in merged_props:
                merged_props[prop["smiles"]] = prop
        if len(merged_props) > len(from_file):
            properties = list(merged_props.values())
            sources["properties"] = "tool_file+tool_message"
        else:
            properties = from_file
            sources["properties"] = "tool_file"
    else:
        properties = from_msgs
        sources["properties"] = "tool_message"

    from_file = tool_io.load("docking", run=run)
    from_msgs = _merge_docking(outputs)
    if isinstance(from_file, dict) and from_file.get("receptors"):
        # 产物文件只保存**最近一次**调用（tool_io.write_json 覆盖写），历史调用只在消息历史里 →
        # 两者必须合并，否则分批对接 / 蛋白质库的早先受体块会被静默丢掉。
        # 顺序 = 时间顺序：消息历史（全部调用，按发生先后）在前，产物文件（= 最后一次）在后；
        # 同一分子的同精度结果以先到者为准（内容一致），精度更高者覆盖。
        combined = _merge_docking_payloads([from_msgs, from_file])
        if (len(combined.get("receptors") or []) > len(from_file.get("receptors") or [])
                or _docking_rows(combined) > _docking_rows(from_file)):
            docking = combined
            sources["docking"] = "tool_file+tool_message"
        else:
            docking = from_file
            sources["docking"] = "tool_file"
    else:
        docking = from_msgs
        sources["docking"] = "tool_message"

    from_file = tool_io.load("binding")
    if isinstance(from_file, dict) and (from_file.get("rows") or from_file.get("results")):
        rows = from_file.get("rows") or from_file.get("results") or []
        by_smiles = {r.get("smiles"): r for r in rows if isinstance(r, dict) and r.get("smiles")}
        msg_binding = _merge_binding(outputs)
        for row in (msg_binding.get("rows") or []):
            if isinstance(row, dict) and row.get("smiles") and row["smiles"] not in by_smiles:
                by_smiles[row["smiles"]] = row
        merged_rows = list(by_smiles.values()) if len(by_smiles) > len(rows) else rows
        binding = {"positive_control": from_file.get("positive_control", ""),
                   "rows": merged_rows, "results": merged_rows}
        sources["binding"] = "tool_file+tool_message" if merged_rows is not rows else "tool_file"
    else:
        binding = _merge_binding(outputs)
        sources["binding"] = "tool_message"
    if docking or properties or molecules:
        logger.info("多 Agent 落盘数据来源：%s", sources)
    pocket_analysis = _merge_pockets(outputs)
    if not pocket_analysis:
        # 共享黑板是口袋结果的权威来源（口袋工具就写在那里）；
        # 子 Agent 的嵌套调用不进父图消息历史，因此这里优先信任黑板。
        try:
            from docking_agent.runtime.blackboard import get_blackboard

            board = get_blackboard()
            board_pockets = board.get_pockets() if board is not None else []
            if board_pockets:
                pocket_analysis = {"engine": getattr(board, "pocket_engine", "") or "",
                                   "pockets": board_pockets, "selected": {},
                                   "validation": {}, "reference": {},
                                   "source": ((board.get_site() or {}).get("source") or ""),
                                   "warnings": []}
        except Exception:  # noqa: BLE001
            logger.debug("从共享黑板读取口袋结果失败", exc_info=True)
    if not pocket_analysis:
        # 兜底：口袋结果可能只在对接块的溯源字段里（子 Agent 的嵌套调用不进入父图消息历史）
        recovered: List[Dict[str, Any]] = []
        engine = validation = {}
        source = ""
        for block in (docking.get("receptors") or []):
            recovered = recovered or list(block.get("pockets") or [])
            engine = engine or (block.get("site") or {}).get("engine") or ""
            validation = validation or (block.get("box_validation") or {})
            source = source or str(block.get("box_source") or "")
        if recovered:
            pocket_analysis = {"engine": engine, "pockets": recovered, "selected": {},
                               "validation": validation if isinstance(validation, dict) else {},
                               "reference": {}, "source": source, "warnings": []}

    # 阳性对照：优先取协调 Agent 交给报告工具的对照信息（含其对接分值）
    positive_control: Dict[str, Any] = {}
    report_args = args.get("generate_screening_report") or {}
    if report_args.get("aggregated_json"):
        try:
            aggregated = json.loads(report_args["aggregated_json"])
            positive_control = aggregated.get("positive_control") or {}
            if not molecules:
                molecules = [{"name": m.get("name"), "smiles": m.get("smiles")}
                             for m in (aggregated.get("molecules") or [])]
        except json.JSONDecodeError:
            logger.warning("解析报告工具参数失败")

    pc_smiles = (positive_control.get("smiles") or binding.get("positive_control") or "")

    # 分离阳性对照行与候选分子行
    candidate_results: List[Dict[str, Any]] = []
    receptors_block: List[Dict[str, Any]] = []
    for block in (docking.get("receptors") or []):
        keep, pc_row = [], {}
        for r in (block.get("results") or []):
            is_pc = (r.get("name") in (POSITIVE_CONTROL_NAME, "PositiveControl")
                     or (pc_smiles and r.get("smiles") == pc_smiles))
            if is_pc and not pc_row:
                pc_row = r
            elif not is_pc:
                keep.append(r)
        receptors_block.append({**block, "results": keep, "positive_control": pc_row})
        candidate_results.extend(keep)
        if pc_row and not positive_control:
            positive_control = pc_row

    # 用口袋分析的溯源补全受体块（**必须在写 docking.json 之前**，否则落盘会丢字段）
    if pocket_analysis:
        for block in receptors_block:
            if pocket_analysis.get("pockets") and not block.get("pockets"):
                block["pockets"] = pocket_analysis["pockets"]
            if not block.get("box_source"):
                block["box_source"] = (pocket_analysis.get("selected") or {}).get("source") \
                    or pocket_analysis.get("source") or ""
            if not block.get("box_validation") and pocket_analysis.get("validation"):
                block["box_validation"] = pocket_analysis["validation"]
            if pocket_analysis.get("engine") and not (block.get("site") or {}).get("engine"):
                block["site"] = {**(block.get("site") or {}),
                                 "engine": pocket_analysis["engine"],
                                 "pockets": pocket_analysis.get("pockets") or []}

    molecules = _dedupe_molecules(molecules) or _dedupe_molecules(candidate_results)

    # ---- 受体结构入包：整包下载必须能独立复现本次对接（用户实测提问） ----
    # 只把绝对路径记在 docking.json 里是不够的：换台机器就取不到受体。
    # 复制「对接实际使用的 PDBQT + 准备后 PDB + 原始上传文件（有则带）」到 receptor/。
    _request = dict(run.data.get("request") or {})
    _task = dict(run.data.get("task_spec") or {})
    copy_receptor_files(run, receptors_block, source_candidates=[
        _request.get("receptor_file") or "",
        (_task.get("receptor") or {}).get("file") or "",
    ])

    try:
        ranking = rank_molecules(merge_and_rank(properties, candidate_results, binding))
    except Exception as e:  # noqa: BLE001
        logger.warning("多 Agent 结果融合失败: %s", e)
        ranking = []

    # ---- 中间数据落盘 ----
    if molecules:
        run.write_json("molecules", molecules, label="候选分子库")
    if properties:
        run.write_json("properties", properties, label="理化性质（RDKit）")
    if docking:
        run.write_json("docking", {**docking, "receptors": receptors_block}, label="对接明细（含能量项与位姿）")
    if binding:
        run.write_json("binding", binding, label="结合模式分析")
    if pocket_analysis:
        run.write_json("pockets", pocket_analysis, label="结合口袋预测与对接盒溯源")

    result = {
        "status": "ok",
        "run_id": run.id,
        "molecules": molecules,
        "properties": properties,
        "docking": {**docking, "receptors": receptors_block},
        "binding": binding,
        "receptors": receptors_block,
        "ranking": ranking,
        "positive_control": positive_control,
        "task_spec": dict(run.data.get("task_spec") or {}),
        "param_plan": dict(run.data.get("param_plan") or {}),
        "data_sources": sources,
        # 协调 Agent 通过 submit_recommendations 写的逐分子理由（数值一律来自真实计算，
        # 这里只搬运文字；匹配校验已在工具里做过）
        "agent_recommendations": [r for r in (run.data.get("recommendation_reasons") or [])
                                  if isinstance(r, dict)],
        "pockets": pocket_analysis.get("pockets") or [],
        "pocket_analysis": pocket_analysis,
        "notes": docking.get("notes") or [],
        # 协调 Agent 通过 customize_report 记录的「按用户要求定制」（标题/附加列/要点/要求与响应）
        "report_customization": dict(run.data.get("report_customization") or {}),
        # 共晶配体是否用作阳性对照：检测到的配体 + 用户决定（报告正文单列一行，便于单独追溯）
        "cocrystal_control_offer": dict(run.data.get("cocrystal_control_offer") or {}),
        "positive_control_decision": str(
            (run.data.get("request") or {}).get("positive_control_decision") or ""),
        # 条件纪律段注入了哪些（审计：提示词不再是常量，必须能复盘「这次给了模型哪些纪律」）
        "prompt_blocks": dict(run.data.get("prompt_blocks") or {}),
        # 受体溯源（哪个结构/哪来的）：由 `fetch_protein_structure` 记账，报告 §1.2 直接渲染
        "receptor_provenance": dict(run.data.get("receptor_provenance") or {}),
    }

    # ---- 没有真实计算的运行**不产规范报告** ----
    # 真实缺陷（用户实测）：只发一句「你好」，受理层 decision=reject、零工具调用，
    # 却照样写 11 KB 全是空表格的报告 + 4 张空图 + 328 KB PDF，页面还把它当作
    # 「规范报告（report.md · 唯一权威版）」挂到对话气泡上 —— 用户看到的是纯噪声。
    narrative = (final_text or "").strip()
    executed = bool(molecules or properties or candidate_results or pocket_analysis or ranking)
    if not executed:
        decision = str((run.data.get("task_spec") or {}).get("decision") or "")
        # 「停下来等用户点选」不是 no_op：受体可能已经解析成功、工具也确实跑过，只是
        # 配体侧必须由用户确认。标成 no_op 会让历史列表显示 [ SKIP ]、日志说「未调用任何
        # 工具」，与事实不符（真实反馈：界面把「等你选配体」说成「本次未执行计算」）。
        pending_choices = list(run.data.get("choices") or [])
        if pending_choices:
            kinds = "、".join(sorted({str(c.get("kind") or "") for c in pending_choices}))
            reason = ("已向用户提出确认问题（%s，%d 项），等待在界面上选择；未执行任何计算"
                      % (kinds or "choices", len(pending_choices)))
            result["status"] = "needs_user_input"
            result["needs_user_input"] = True
        elif decision in ("reject", "rejected", "unsupported", "out_of_scope"):
            reason = "任务未受理（decision=%s），未执行任何计算" % decision
        elif decision:
            reason = "受理通过但没有任何工具产出（decision=%s），未执行任何计算" % decision
        else:
            reason = "未执行任何计算"
        run.set(no_report_reason=reason)
        if not pending_choices:
            # 运行状态如实标成 no_op：历史列表里显示 [ SKIP ]，而不是一个「成功但什么都没有」的 [ OK ]
            result["status"] = "no_op"
            result["no_op"] = True
        logger.info("run %s: 未执行任何计算（decision=%s, pending_choices=%d），跳过排序 CSV / 图表 / 规范报告",
                    run.id, decision or "-", len(pending_choices))
        # 协调 Agent 的对话回复仍然留档（它就是本次运行的全部产出）
        if narrative:
            run.write_text("agent_report.md", narrative, name="agent_report_md",
                           label="协调 Agent 原始输出（Markdown）",
                           content_type="text/markdown; charset=utf-8")
        run.write_json("result", {k: result.get(k) for k in
                                  ("status", "ranking", "positive_control", "receptors", "notes",
                                   "pockets", "pocket_analysis", "param_plan", "task_spec",
                                   "report_customization", "prompt_blocks",
                                   "receptor_provenance")},
                       label="完整结果（JSON）")
        _finish_run_meta(run, result, molecules, receptors_block, ranking, args)
        return result

    run.write_text("ranking.csv",
                   build_ranking_csv(ranking, positive_control,
                                     failures=collect_failures(result.get("docking"))),
                   name="ranking_csv", label="排序结果 CSV", content_type="text/csv; charset=utf-8")
    run.write_ranking(ranking)

    # 图表：与流水线模式共用同一套图（固定报告里内嵌的图片必须真实存在）
    try:
        write_report_charts(run, ranking, positive_control, result=result)
    except Exception as e:  # noqa: BLE001
        logger.warning("生成报告图表失败：%s", e)

    # 固定格式报告：与流水线模式共用同一模板，协调 Agent 的文字放入固定的「结论与建议」一节
    if narrative:
        run.write_text("agent_report.md", narrative, name="agent_report_md",
                       label="协调 Agent 原始输出（Markdown）",
                       content_type="text/markdown; charset=utf-8")
    report_md = build_markdown_report(
        result, kind="agent", run_id=run.id,
        receptor_label=(receptors_block[0].get("receptor") if receptors_block else ""),
        site=({"center": receptors_block[0].get("box_center"),
               "size": receptors_block[0].get("box_size")} if receptors_block else None),
        artifacts=run.artifacts(),
        agent_narrative=narrative,
        agent_models=_agent_models(run),
        created_at=str(run.data.get("created_at") or ""))
    run.write_text("report.md", report_md, name="report_md", label="分析报告（Markdown，固定格式）",
                   content_type="text/markdown; charset=utf-8")
    # PDF 版报告：与 Markdown 同一内容，失败只记 warning，不影响多 Agent 运行结果
    write_report_pdf(run, result)
    run.write_json("result", {k: result.get(k) for k in
                              ("status", "ranking", "positive_control", "receptors", "notes",
                               "pockets", "pocket_analysis", "param_plan", "task_spec",
                               "report_customization", "cocrystal_control_offer",
                               "positive_control_decision", "prompt_blocks",
                               "receptor_provenance")},
                   label="完整结果（JSON）")

    _finish_run_meta(run, result, molecules, receptors_block, ranking, args)
    return result


def _finish_run_meta(run: Run, result: Dict[str, Any], molecules: List[Dict[str, Any]],
                     receptors_block: List[Dict[str, Any]], ranking: List[Dict[str, Any]],
                     args: Dict[str, Any]) -> None:
    """把「这次实际算了什么」写进运行记录（**报告有无不改变这一步**）。"""
    docking_args = args.get("run_docking") or {}
    run.set(
        molecules=molecules,
        receptor=(receptors_block[0].get("receptor") if receptors_block else None),
        receptor_label=(receptors_block[0].get("receptor") if receptors_block else None),
        site=({"center": receptors_block[0].get("box_center"),
               "size": receptors_block[0].get("box_size")} if receptors_block else None),
        engine=(ranking[0].get("engine") if ranking else docking_args.get("engine")),
        exhaustiveness=(ranking[0].get("exhaustiveness") if ranking else docking_args.get("exhaustiveness")),
        molecule_count=len(molecules),
        top=[{"name": m.get("name"), "affinity_kcal_mol": m.get("affinity_kcal_mol")}
             for m in ranking[:3]],
        notes=result["notes"],
    )


