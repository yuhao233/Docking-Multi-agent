"""分子属性评估 Agent 的工具：真实 RDKit 物化性质计算。"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from langchain.tools import tool

from docking_agent.runtime import tool_io
from docking_agent.runtime.blackboard import board_molecules_json
from docking_agent.core import compute_properties
from docking_agent.runtime.context import AgentContext, active_blackboard, active_run
from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)


#: 逐分子属性里**聚合/展示**需要的字段（报告 §4 表与排序榜都只用这些）
_PROPERTY_DATA_FIELDS = ("smiles", "protonated_smiles", "formula", "molecular_weight", "logP", "tpsa",
                         "hbd", "hba", "rotatable_bonds", "heavy_atoms", "aromatic_rings",
                         "lipinski_violations", "drug_likeness_pass", "error")


def _agent_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """给 Agent 的属性行：数据字段 + 压成聚合口径的质子化溯源。

    完整溯源（`method`/`note`/`variants`/`variant_rule`/`engine_window`/`rules`）留在产物
    `properties_tool.json` 里 —— 逐分子把散文搬进上下文既贵又容易让模型照着复述。
    """
    from docking_agent.core.protonation import compact_protonation

    out = {k: row[k] for k in _PROPERTY_DATA_FIELDS if k in row and row[k] is not None}
    for key in ("id", "source_file", "row", "name"):
        if row.get(key) not in (None, ""):
            out[key] = row[key]
    full = compact_protonation(row.get("protonation"))
    if full:
        # 属性行不需要逐分子重复引擎/电荷口径（那是对接行的聚合字段；报告 §4 只用数值列）
        out["protonation"] = {k: full[k] for k in ("policy", "applied") if k in full}
    return out


@tool
def molecular_property_assessment(molecules_json: str = "", molecules_file: str = "",
                                  protonation: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """对一组分子进行真实物化性质与类药性评估（RDKit 计算）。

    参数（三选一，优先级 文件 > JSON > 共享黑板/本次运行请求）：
      - molecules_file: **推荐**。分子库文件（SDF/CSV/SMI/MOL2…）或运行产物文件
        （如 `/abs/var/runs/<id>/molecules_tool.json`）的路径 —— 大库按文件交接，零 token 成本；
      - molecules_json: JSON 字符串，形如 [{"name":"M1","smiles":"..."}, ...]（小库可用）；
      - 都留空: 用共享黑板（其次退到本次运行请求里的分子库文件）。

    protonation: 可选。运行级质子化态策略（'ph' 默认，按目标 pH；/ 'neutralize' / 'keep'）。
        留空 = 用本次运行的设置。目标 pH 是**运行级**参数（设置页 `docking.protonation_ph`，
        默认 7.4），本工具没有单独的 pH 入参。
        **与对接同口径**：性质按对接实际使用的化学形式计算，并逐分子记录
        policy/applied/charge_before/charge_after（ph 策略下还记录命中的 pKa 规则）；
        原始 SMILES 保留在 `smiles`，实际形式在 `protonated_smiles`。

    返回 JSON：每分子包含 molecular_weight、logP、tpsa、hbd、hba、
    rotatable_bonds、heavy_atoms、aromatic_rings、formula、lipinski_violations、drug_likeness_pass、
    protonation（策略溯源）。
    """
    try:
        # 横向协作：文件优先（大库按文件交接）→ JSON → 共享黑板 → 本次运行请求的分子库文件
        if (molecules_file or "").strip():
            molecules = _coerce_molecule_list(molecules_file, runtime=runtime)
        else:
            effective = molecules_json if (molecules_json or "").strip() else board_molecules_json("")
            molecules = json.loads(effective) if effective else _coerce_molecule_list("", runtime=runtime)
        if not isinstance(molecules, list) or not molecules:
            return json.dumps(
                {"status": "no_molecules",
                 "message": "没有待评估的分子：请先导入分子库（共享黑板上也没有分子）。"},
                ensure_ascii=False)
        results: List[Dict[str, Any]] = []
        for m in molecules:
            smiles = m.get("smiles", "")
            name = m.get("name", smiles)
            try:
                prop = compute_properties(smiles, protonation or None)
                prop["name"] = name
                # 带上输入文件的身份字段（ID / 来源文件 / 序号），报告与 CSV 才能按用户要求展示
                from docking_agent.core.docking import carry_identity

                results.append(carry_identity(prop, m))
            except Exception as e:
                results.append({"name": name, "smiles": smiles, "error": str(e)})
        board = active_blackboard(runtime)
        if board is not None:
            board.set_properties(results)
            board.add_note(f"属性评估 Agent：完成 {len(results)} 个分子的理化性质，并写入共享黑板")
        # 大库：完整明细落盘，只把摘要 + 前 N 条回传给模型（避免上下文被分子数撑爆）
        tool_io.record("properties", results, run=active_run(runtime))
        limit = tool_io.summary_limit()
        view = [_agent_row(r) for r in results]      # 给模型/子 Agent 的视图（产物已全量落盘）
        if len(results) > limit:
            ok = [r for r in results if not r.get("error")]
            payload = {
                "status": "ok",
                "assessment_total": len(results),
                "assessment": tool_io.top_rows(view, limit=limit),
                "detail_omitted": True,
                "summary": {
                    "computed": len(ok),
                    "failed": len(results) - len(ok),
                    "drug_like_pass": len([r for r in ok if r.get("drug_likeness_pass")]),
                    "mean_molecular_weight": round(
                        sum(float(r.get("molecular_weight") or 0) for r in ok) / len(ok), 2
                    ) if ok else None,
                },
                "artifacts": tool_io.artifact_refs(run=active_run(runtime)),
                "notice": tool_io.big_payload_notice("理化性质", len(results), limit),
            }
            return json.dumps(payload, ensure_ascii=False)
        return json.dumps({"status": "ok", "assessment": view}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

@tool
def normalize_molecule_library(molecules_json: str = "", molecules_file: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """规范化并校验候选分子库：统一 SMILES、去重、剔除无效项。

    在做属性评估或对接**之前**调用它，可以避免重复计算与无效输入。

    参数 molecules_json 兼容多种「脏」写法，任一都能用（统一输入归一化层）：
      - `[{"name":"M1","smiles":"..."}, ...]` 或纯 SMILES 列表；
      - 自由文本 `名称:SMILES` / `名称 SMILES` / 逗号/分号/换行分隔的 SMILES 列表；
      - 小分子**文件路径或 URL**（SDF/CSV/TSV/SMI/MOL2，可 gzip/zip）；
      - 留空/无法解析时 → 直接使用**共享黑板**上的分子库（子 Agent 不必搬运清单）。
    返回 JSON：{"status":"ok","count":去重后数量,"duplicates_removed":n,"invalid":[...],
    "molecules":[{"id","name","smiles"}...]}
    """
    try:
        from rdkit import Chem

        raw = _coerce_molecule_list(molecules_file, runtime=runtime) if (molecules_file or "").strip() \
            else _coerce_molecule_list(molecules_json, runtime=runtime)
        if isinstance(raw, list) and raw and isinstance(raw[0], str):
            raw = [{"name": "", "smiles": s} for s in raw]
        if not isinstance(raw, list):
            raise ValueError("molecules_json 必须是列表、可解析的分子文本，或分子文件路径")
        seen, molecules, invalid = set(), [], []
        for m in raw:
            smiles = str((m or {}).get("smiles") or "").strip()
            mol = Chem.MolFromSmiles(smiles) if smiles else None
            if mol is None:
                invalid.append(smiles or str(m))
                continue
            canonical = Chem.MolToSmiles(mol)
            if canonical in seen:
                continue
            seen.add(canonical)
            src = m or {}
            name = str(src.get("id") or src.get("name") or "").strip()
            # 来源文件的附加信息（SDF 的 ID/CAS/自定义字段）必须原样带走：用户要"输出带上 ID 号"，
            # 这些字段只存在于输入记录里，重建字典时丢掉就再也找不回来了（真实缺陷 2026-09-24）。
            extra = {k: src[k] for k in ("id", "cas", "fields", "source_file", "source_index")
                     if src.get(k) not in (None, "", {})}
            molecules.append({**extra, "id": name or canonical,
                              "name": name or canonical, "smiles": canonical})
        board = active_blackboard(runtime)
        if board is not None:
            board.add_molecules(molecules)
            board.add_note(f"属性评估 Agent：规范化分子库 {len(raw)} → {len(molecules)} 条"
                           f"（去重 {len(raw) - len(molecules) - len(invalid)}，无效 {len(invalid)}）")
        dup = max(0, len(raw) - len(molecules) - len(invalid))
        limit = tool_io.summary_limit()
        if len(molecules) > limit:
            # 大库：清单已写入黑板（供 molecular_property_assessment 留空取用），
            # 这里只回摘要 —— 否则 1 万条 SMILES 会把属性子 Agent 的上下文撑爆。
            return json.dumps({
                "status": "ok", "count": len(molecules), "duplicates_removed": dup,
                "invalid": invalid[:20], "molecules": molecules[:limit], "detail_omitted": True,
                "summary": {"count": len(molecules),
                            "first_names": [m["name"] for m in molecules[:10]]},
                "notice": f"共 {len(molecules)} 条，完整清单已写入共享黑板；这里只回前 {limit} 条。"
                          "请直接调用 molecular_property_assessment（分子参数留空）对**全部**分子评估。",
            }, ensure_ascii=False)
        return json.dumps({"status": "ok", "count": len(molecules),
                           "duplicates_removed": dup,
                           "invalid": invalid[:20], "molecules": molecules}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("分子库规范化失败")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


def _coerce_molecule_list(molecules_json: Any, runtime: Any = None) -> Any:
    """把任意输入收敛为分子列表（JSON / 自由文本 / 文件路径 / 空 → 共享黑板）。

    真实缺陷：属性评估子 Agent 曾把非 JSON 文本传给 `normalize_molecule_library`，
    触发 `json.decoder.JSONDecodeError` 并把「分子库规范化失败」写进日志。
    这里统一交给归一化层：能解析就解析，留空就用黑板，绝不因脏输入中断流程。
    """
    from docking_agent.runtime.blackboard import board_molecules_json

    if isinstance(molecules_json, list):
        return molecules_json
    text = str(molecules_json or "").strip()
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:  # 允许静默：不是 JSON，继续按文件路径 / 自由文本处理
            pass
        if os.path.isfile(text):
            # 文件（含运行产物 JSON）统一走归一化层：Agent 之间按文件交接
            from docking_agent.core.ligands import read_molecules_any

            try:
                _fmt, rows, _norm = read_molecules_any(text)
                if rows:
                    return rows
            except Exception as e:  # noqa: BLE001 - 读不到就继续按文本/黑板兜底
                logger.debug("按文件读取分子库失败（%s）：%s", text, e)
        from docking_agent.tools.molecule_paths import looks_like_molecule_path

        if looks_like_molecule_path(text):
            from docking_agent.core import read_molecule_file

            _fmt, mols = read_molecule_file(text)
            return mols
        from docking_agent.core.normalize import normalize_ligand_text

        mols, _norm = normalize_ligand_text(text)
        if mols:
            return mols
    # 留空/无法解析 → 黑板（子 Agent 不需要把清单搬进上下文）
    board_text = board_molecules_json("")
    if board_text:
        parsed = json.loads(board_text) if isinstance(board_text, str) else board_text
        return [{"name": (m or {}).get("name") or (m or {}).get("id") or "",
                 "smiles": (m or {}).get("smiles") or ""} for m in (parsed or [])]
    # 黑板为空时，退到**本次运行请求里带的分子库文件**（与 molecular_docking 同一兜底口径）：
    # 真实缺陷：协调 Agent 可以直接把文件交给 run_docking 而跳过 import，此时黑板一直是空的，
    # 属性评估就会拿到 0 个分子并返回 status=ok（静默降级）。
    run = active_run(runtime)
    request_file = str(((getattr(run, "data", None) or {}).get("request") or {}).get("molecule_file") or "")
    if request_file:
        try:
            from docking_agent.tools.molecule_paths import resolve_molecule_file
            from docking_agent.core import read_molecule_file_normalized

            resolved, _attempts, candidates = resolve_molecule_file(request_file)
            if not candidates and os.path.isfile(resolved):
                _fmt, molecules, _norm = read_molecule_file_normalized(resolved)
                if molecules:
                    board = active_blackboard(runtime)
                    if board is not None:      # 顺手发布：后面按文件的/按黑板的都能用
                        board.add_molecules(molecules)
                        board.add_note(f"属性评估 Agent：从本次运行的分子库文件读取 {len(molecules)} 条"
                                       f"（{os.path.basename(resolved)}）并写入共享黑板")
                    return molecules
        except Exception as e:  # noqa: BLE001 - 兜底失败仍按「没有分子」如实上报
            logger.warning("从运行请求的分子库文件读取失败：%s", e)
    return []
