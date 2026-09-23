"""Docking 执行 Agent 的工具：真实 Vina 分子对接计算（支持蛋白质库 × 小分子库）。"""
from __future__ import annotations

import json
from functools import partial
import logging
from typing import Any, Dict, List, Literal, Optional

from langchain.tools import tool

from docking_agent.core.ranking import sort_by_affinity
from docking_agent.runtime import tool_io
from docking_agent.runtime.blackboard import board_molecules_json
from docking_agent.core import (POSITIVE_CONTROL_NAME, dock_library)
from docking_agent.core.params import (DEFAULT_EXHAUSTIVENESS,
                                       resolve_engine, resolve_exhaustiveness)
from docking_agent.runs import slug
from docking_agent.tools.schemas import CoordArray, floats_to_text
from docking_agent.runtime.context import AgentContext, active_blackboard, active_run
from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)


def _parse_site(center: str, size: str) -> Optional[Dict[str, Any]]:
    """解析 "31.5,13.74,24.36" / "22,22,22" 形式的已知位点参数。"""
    def _nums(text: str, expect: int) -> Optional[List[float]]:
        text = (text or "").strip().strip("[]()")
        if not text:
            return None
        parts = [p for p in text.replace(";", ",").split(",") if p.strip()]
        if len(parts) != expect:
            return None
        try:
            return [float(p) for p in parts]
        except ValueError:
            return None

    c = _nums(center, 3)
    s = _nums(size, 3)
    if not c and not s:
        return None
    return {"center": c, "size": s}


# 前端「实时逐分子结果」表所消费的字段（与运行期间逐分子事件同构）
_LIVE_ROW_FIELDS = ("name", "smiles", "affinity_kcal_mol", "intermolecular_kcal_mol",
                    "intramolecular_kcal_mol", "torsion_kcal_mol", "engine",
                    "exhaustiveness", "box_group", "box_size", "error")


def _receptor_unspecified(receptor: Any, run: Any, receptor_file: str = "") -> bool:
    """兼容包装：判定收敛到 `core.receptors.receptor_unspecified`（两个工具共用一份）。"""
    from docking_agent.core.receptors import receptor_unspecified as _impl  # noqa: PLC0415

    return _impl(receptor, run, receptor_file)


def _live_molecule_row(result: Dict[str, Any], index: int, total: int,
                       runtime: Any = None) -> Dict[str, Any]:
    """把一条对接结果整理成实时表的一行（含物化性质，若有）。"""
    row: Dict[str, Any] = {k: result.get(k) for k in _LIVE_ROW_FIELDS}
    row["index"] = index
    row["total"] = total
    board = active_blackboard(runtime)
    props = (board.get_property(result.get("smiles")) if board is not None
             and result.get("smiles") else None) or {}
    if props:
        row["properties"] = {k: v for k, v in props.items() if k not in ("smiles", "name")}
    if result.get("pose_file"):
        row["pose_artifact"] = f"pose_{slug(result.get('name') or result.get('smiles') or 'ligand')}"
    return row


#: 逐分子对接行里给 Agent 的字段（报告/排序只读这些；完整行留在 `docking_tool.json`）
_DOCK_ROW_FIELDS = ("name", "smiles", "id", "source_file", "affinity_kcal_mol", "affinity_coarse",
                    "engine", "exhaustiveness", "n_poses", "seed", "box_group", "box_size",
                    "box_fit_warning", "pass", "pose_file", "pose_url", "pose_artifact",
                    "intermolecular_kcal_mol", "intramolecular_kcal_mol", "torsion_kcal_mol",
                    "ligand_warnings", "error", "status")

#: 受体块里**报告才需要**的字段：给模型的视图不重复（完整块在产物里）
_DOCK_BLOCK_REPORT_ONLY = ("box_atom_stats",)


def _agent_view(out: Dict[str, Any]) -> Dict[str, Any]:
    """给模型/子 Agent 的对接视图：保留溯源与结论所需字段，压掉逐分子散文细节。

    为什么能压：`tool_io.record()` 已经把**完整**结果写进 `docking_tool.json`，
    报告与排序读的是那份产物（`persistence` 以产物优先、消息只作回填），所以视图变薄
    不会丢事实；而逐分子的 `ligand_facts.protonation` 明细（`variants`/`variant_rule`/
    `method`/`note`）在上下文里占大头、正是模型最容易照抄成正文的部分。
    """
    from docking_agent.core.protonation import compact_protonation

    blocks = []
    for block in (out.get("receptors") or []):
        if not isinstance(block, dict):
            continue
        clean = {k: v for k, v in block.items() if k not in _DOCK_BLOCK_REPORT_ONLY}
        rows = []
        for row in (clean.get("results") or []):
            if not isinstance(row, dict):
                continue
            slim = {k: row[k] for k in _DOCK_ROW_FIELDS if row.get(k) not in (None, "")}
            facts = row.get("ligand_facts")
            if isinstance(facts, dict):
                fact_view = {k: facts[k] for k in ("num_fragments", "has_metal", "formal_charge",
                                                   "formal_charge_input", "undefined_stereocenters")
                             if facts.get(k) not in (None, "")}
                compact = compact_protonation(facts.get("protonation"))
                if compact:
                    fact_view["protonation"] = compact
                if fact_view:
                    slim["ligand_facts"] = fact_view
            if isinstance(slim.get("ligand_warnings"), list):
                slim["ligand_warnings"] = slim["ligand_warnings"][:2]
            rows.append(slim)
        clean["results"] = rows
        blocks.append(clean)
    view = dict(out)
    view["receptors"] = blocks
    return view

@tool
def molecular_docking(molecules_json: str = "", molecule_file: str = "",
                      receptor_file: str = "", receptor_sources: str = "",
                      site_center: CoordArray = None, site_size: CoordArray = None,
                      exhaustiveness: int = 0, n_poses: int = 1,
                      engine: Literal["", "auto", "vina", "autodock", "external"] = "",
                      top_from_previous: int = 0,
                      keep_hetatm: str = "", protonation: str = "",
                      protonation_ph: float = 0.0, save_poses: bool = True,
                      max_ligands: int = 0, runtime: ToolRuntime[AgentContext] = None) -> str:
    """对一组小分子配体（小分子库）向一个或多个蛋白质受体（蛋白质库）执行真实 Vina 分子对接。

    参数（配体与受体二选一来源均支持"直接数据"与"上传文件"两种方式）：
      molecules_json: 可选。JSON 字符串，[{"name":"M1","smiles":"..."}, ...]；为空时返回提示。
      molecule_file: 可选。用户上传/提供的小分子文件，支持 SDF/SMILES/.smi/.csv/.mol2（本地路径或 URL），
          自动读取为小分子库；与 molecules_json 二选一（优先 molecule_file）。
      receptor_file: 可选。用户上传/提供的蛋白质受体文件（.pdb/.ent/.pdb1/.cif/.mmcif/.pdbqt，本地路径或 URL），
          自动读取并现场准备为 PDBQT 受体；与 receptor_sources 二选一（优先 receptor_file）。
      receptor_sources: 受体来源，可空(''/default)或一个受体，或 JSON 数组/分号分隔的多个受体（蛋白质库）。
          支持三种形式：
            - PDB 编号 / UniProt accession / 基因或蛋白名（如 4HHB / P08922 / EGFR）
            - 用户上传的蛋白质文件：本地/URL 的 .pdb/.ent/.pdb1/.cif/.mmcif/.pdbqt 路径（结构文件会现场准备为 PDBQT，.cif/.mmcif 先转 PDB）
            - 多个受体组合，例如 '["4HHB","P08922"]'，将按 受体×配体 全组合对接
          **为空或未提供时不再回退任何默认受体**：直接返回 `needs_user_input` 并请用户指定受体
          （预置受体注册表只用于内部测试，不是用户可选来源）。
      site_center: 可选。已知结合位点的盒中心 `[x, y, z]`（Å），用于覆盖受体注册位点；
        也接受逗号分隔字符串 "31.5,13.74,24.36"（旧调用方兼容）。
      site_size: 可选。位点盒尺寸 `[x, y, z]`（Å）；也接受 "22,22,22"。
      exhaustiveness: 对接蒙特卡洛搜索强度。**留空(0)=用本次运行的自动规划值**
          （受理层算好的运行级值，同一阶段内所有分子一致）；本次没有规划值时才落到设置页默认
          （16）。只有刻意做参数对比时才显式给值，并在报告里说明两次的差别。
      n_poses: 返回的构象个数（默认 1）
      engine: 对接引擎，**留空 = 跟随「设置页 → 对接引擎（默认）」**（默认 auto：优先 Vina，失败自动回退
              AutoDock4 CPU）/ 'vina' / 'autodock'(经典 AutoDock4 CPU 模式) /
              'external'(用设置页登记的外部引擎，如 AutoDock-GPU；未登记或未就绪会直接报错，不静默回退)。
              只有用户明确要求换引擎时才显式传值。
      save_poses: 是否把每个分子的最佳位姿写入运行目录（表单里的「保存对接位姿」；False 时不落位姿文件）
      max_ligands: 本次最多对接多少个分子（表单里的「最大分子数」；0=不限制，仍受部署级上限约束）
      protonation: **一般留空**。运行级质子化态策略：'ph'(默认，按目标 pH 分配，见 protonation_ph) /
          'neutralize'(只对带净电荷的分子中和) / 'keep'(保持输入形式，仅告警)。
          留空 = 用**本次运行的设置**（设置页里的
          质子化态策略）—— 这是推荐做法：理化性质与对接必须同口径，逐次覆盖会让两者错位。
          仅在刻意做"离子态 vs 中性态"对比时才传，并必须在报告中说明两者的差异。
          逐分子结果里带 policy/applied/charge 溯源，原始 SMILES 始终保留。
      protonation_ph: 可选。仅当 protonation='ph' 时有意义：目标 pH（默认 7.4，生理 pH）。
          常用值：胃酸 1.5 / 溶酶体 4.5 / 生理 7.4；传 0（默认）表示用本次运行的设置。
      keep_hetatm: 可选。受体准备时**要保留的非水杂原子残基名**，逗号分隔（如 'HEM,ZN,MG,NAD,FAD'）。
          默认空 = 按标准流程只留蛋白质 ATOM 记录（水与杂原子剔除），但这会**默默丢掉金属/辅因子**。
          因此受体里存在非水杂原子时，结果 notes 会如实列出被丢弃的残基名与数量；
          金属酶/含辅因子体系（血红素、锌指、NAD/FAD 依赖酶等）应先据 notes 判断，
          再用本参数指定保留并重跑，不要默认忽略。

    校验：分子对接需要「蛋白质受体 + 小分子配体」两部分。若两者都缺失或配体为空，会返回明确提示，不做计算。
    返回 JSON：{status, notes:[提示信息], receptors:[{receptor_key, receptor, protein,
    box_center, box_size, results:[每分子: name,smiles,engine,affinity_kcal_mol,intermolecular_kcal_mol,
    intramolecular_kcal_mol,torsion_kcal_mol,pose_file,...]}]}
    其中 affinity_kcal_mol 为对接结合亲和力（Vina score 或 AutoDock 自由能估计），越负结合越强。失败分子以 error 字段如实标识。
    """
    # 「未指定」是 0 而不是 16 —— 见 `core/params.UNSET_EXHAUSTIVENESS` 的说明：
    # 默认 16 无法区分「用户设了 16」与「没人给值」，会让运行**静默退回** 16。
    _explicit_exh = False
    try:
        _explicit_exh = int(exhaustiveness or 0) > 0
    except (TypeError, ValueError):
        _explicit_exh = False
    _planned_exh = resolve_exhaustiveness(
        exhaustiveness,
        (getattr(active_run(runtime), "data", None) or {}).get("param_plan"))
    # 引擎：显式参数 > 本次运行请求（表单）> 设置页默认；留空不再等价于硬编码 "auto"
    _run_req_engine = (getattr(active_run(runtime), "data", None) or {}).get("request") or {}
    engine = resolve_engine(engine, _run_req_engine)
    exhaustiveness = int(_planned_exh) if _planned_exh else DEFAULT_EXHAUSTIVENESS
    try:
        molecules = None
        refine_from: List[Dict[str, Any]] = []
        coarse_map: Dict[str, Any] = {}
        if int(top_from_previous or 0) > 0:
            # 漏斗第二阶段：直接从共享黑板/产物里取"上一轮最好的前 N 个"，
            # 这一步**不经过模型上下文**（大库时模型根本拿不到全量明细）。
            board = active_blackboard(runtime)
            previous = board.docking() if board is not None else []
            if not previous:
                previous = tool_io.load("docking_rows", run=active_run(runtime)) or []
            ranked = sort_by_affinity(previous, drop_missing=True)
            # 注意：必须在对接**之前**记下粗筛分数 —— 对接后黑板会被精算结果覆盖
            coarse_map = {r.get("smiles"): r.get("affinity_kcal_mol") for r in previous if r.get("smiles")}
            refine_from = [{"name": r.get("name") or r.get("smiles"), "smiles": r.get("smiles")}
                           for r in ranked[: int(top_from_previous)] if r.get("smiles")]
            if not refine_from:
                return json.dumps({"status": "no_previous",
                                   "message": "没有可精算的上一轮对接结果：请先做一次全库粗筛。"},
                                  ensure_ascii=False)
            molecules = refine_from
        if not (molecule_file and molecule_file.strip()):
            # 兜底：本次运行请求带的上传分子库（对话附件）→ 直接用，不依赖模型是否转发路径。
            _run_req = (getattr(active_run(runtime), "data", None) or {}).get("request") or {}
            _req_mol_file = str(_run_req.get("molecule_file") or "").strip()
            if _req_mol_file:
                molecule_file = _req_mol_file
        if molecule_file and molecule_file.strip():
            # 裸文件名（如上传显示名 `PGR.sdf`）先在上传/缓存目录里解析成真实路径 ——
            # 不要求模型拼绝对路径（真实缺陷 20260917-112206-5017）。
            from docking_agent.tools.molecule_paths import resolve_molecule_file
            resolved, attempts, candidates = resolve_molecule_file(molecule_file.strip())
            if candidates:
                return json.dumps({"status": "needs_user_input",
                                   "message": "配体文件无法确定：请用户在候选清单里指定完整路径。",
                                   "candidates": candidates, "attempted": attempts},
                                  ensure_ascii=False)
            try:
                from docking_agent.core import read_molecule_file_normalized
                from docking_agent.core.normalize import record_input_normalization

                _fmt, molecules, _norm = read_molecule_file_normalized(resolved)
                if _norm:
                    record_input_normalization(_norm, kind="ligand")
            except Exception as fe:
                return json.dumps({"status": "file_error",
                                   "message": f"小分子文件读取失败: {str(fe)}，请确认文件格式正确"
                                              f"（支持 sdf/smi/smiles/csv/mol2）。",
                                   "resolved_path": resolved, "attempted": attempts},
                                  ensure_ascii=False)
            if not molecules:
                message = f"文件 {resolved} 未解析出任何分子（格式不受支持或内容为空）。"
                if attempts:
                    message += "已尝试的路径与失败原因：" + "；".join(attempts[:8])
                return json.dumps({"status": "no_molecules", "message": message,
                                   "resolved_path": resolved, "attempted": attempts},
                                  ensure_ascii=False)
        elif not refine_from:
            # 横向协作：优先使用共享黑板里（属性评估 Agent 规范化去重过）的分子库
            effective = board_molecules_json(molecules_json if isinstance(molecules_json, str) else "")
            molecules = json.loads(effective) if isinstance(effective, str) and effective.strip() else None
        # refine_from 已给出「精算清单」时不要被黑板全量清单覆盖
        if not isinstance(molecules, list) or not molecules:
            return json.dumps(
                {"status": "no_molecules",
                 "message": "未提供待对接的小分子配体：分子对接需要蛋白质受体与小分子配体两部分。"
                            "请提供候选小分子库（SMILES/名称列表），或上传小分子文件（SDF/SMILES/CSV）。"},
                ensure_ascii=False)
        # 身份去重：同一物质的两种 SMILES 写法（PubChem 原始写法 vs 用户点选写法）只能占一行。
        # 真实缺陷（2026-09-22 用户实测）：只给 1 个分子却对接出 2 行 —— 黑板里同一物质以两种
        # 写法各占一个键（见 `runtime/blackboard.canonical_key`），清单到这里仍是 2 条。
        if isinstance(molecules, list) and len(molecules) > 1:
            from docking_agent.runtime.blackboard import dedupe_molecules

            molecules, dropped = dedupe_molecules(molecules)
            if dropped:
                logger.info("配体清单按化学身份去重：去掉 %d 条重复写法", dropped)

        # ---- 阳性对照必须一并真实对接（方法学基线）----
        # 不依赖模型是否记得把对照放进清单：从本次运行的请求参数里取，缺了就补上。
        _run_for_control = active_run(runtime)
        control = ""
        if _run_for_control is not None:
            control = str((_run_for_control.data.get("request") or {}).get("positive_control") or "").strip()
        if control and isinstance(molecules, list):
            from rdkit import Chem as _Chem

            ctrl_mol = _Chem.MolFromSmiles(control)
            if ctrl_mol is not None:
                canonical = _Chem.MolToSmiles(ctrl_mol)
                present = set()
                for m in molecules:
                    mm = _Chem.MolFromSmiles(str((m or {}).get("smiles") or ""))
                    if mm is not None:
                        present.add(_Chem.MolToSmiles(mm))
                if canonical not in present:
                    molecules = list(molecules) + [{"name": POSITIVE_CONTROL_NAME, "smiles": control}]
                    logger.info("阳性对照未在清单中，已自动补入一并对接：%s", control)

        receptor = None
        if receptor_file and receptor_file.strip():
            # 统一归一化：gzip/zip、内容嗅探（扩展名只是提示）、CIF→PDB、杂原子溯源
            try:
                from docking_agent.core.normalize import (normalize_receptor_source,
                                                          record_input_normalization)

                _keep_now = [x.strip() for x in (keep_hetatm or "").replace("；", ",")
                             .replace(";", ",").split(",") if x.strip()]
                receptor_path, receptor_norm = normalize_receptor_source(
                    receptor_file.strip(), keep_hetatm=_keep_now)
                record_input_normalization(receptor_norm, kind="receptor")
                receptor = receptor_path
            except Exception as re_err:  # noqa: BLE001
                # 输入归一化失败 = 受体不可用：**不计算**，把原因与出路交回用户
                # （旧行为只回一个 file_error，主管 Agent 还可能换个受体继续跑）。
                from docking_agent.tools.choices import (  # noqa: PLC0415
                    receptor_input_problem)

                return receptor_input_problem(
                    "normalize_failed", receptor_file.strip(),
                    f"上传的受体文件无法使用：{re_err}。本次**不执行任何计算**。"
                    "请用户三选一：① 换一个结构文件（.pdb/.ent/.cif/.pdbqt）；"
                    "② 给出该受体的 PDB 编号 / UniProt accession / 基因或蛋白名，由系统在线解析；"
                    "③ 按上面的原因修正文件后重试。",
                    options=("replace_file", "resolve_by_name", "fix_and_retry"),
                    runtime=runtime)
        else:
            rs = (receptor_sources or "").strip()
            if rs:
                # 支持 JSON 数组或分号分隔的多受体
                if rs.startswith("["):
                    receptor = json.loads(rs)
                elif ";" in rs or "," in rs:
                    receptor = [x.strip() for x in rs.replace(";", ",").split(",") if x.strip()]
                else:
                    receptor = rs
        # 当前运行（多 Agent 模式由 API 层注入）：位姿写入该次运行目录，作为可下载中间数据
        run = active_run(runtime)
        # 谁解析到分子谁就**发布**：协调 Agent 允许跳过 import 直接把文件交给本工具
        # （提示词明确允许），此时若只有本工具知道分子，属性评估等子 Agent 就会读到「黑板上无分子」
        # —— 真实缺陷：属性评估拿到 0 条却返回 status=ok。发布后再写一条文件交接路径供下游直接读。
        if molecules:
            board_now = active_blackboard(runtime)
            if board_now is not None and not board_now.molecules():
                board_now.add_molecules(molecules)
                board_now.add_note(f"Docking 执行 Agent：从分子库文件读取 {len(molecules)} 条"
                                   f"并写入共享黑板（供属性评估/结合模式等子 Agent 使用）")
        # 已取消/已结束的运行**绝不允许再开始新的对接**：用户点「停止」后，API 的
        # `clear_cancel()` 会清掉取消标志，而图里可能还有一次在途的工具调用 —— 若不拦住，
        # 它会用「新的、未置位的」标志重新跑整库对接（真实缺陷：取消后 load 反而涨到 40）。
        if run is not None and str(run.data.get("status") or "") in ("cancelled", "error"):
            logger.info("运行已 %s，拒绝开始新的对接", run.data.get("status"))
            return json.dumps({
                "status": "cancelled",
                "message": f"本次运行已{'被取消' if run.data.get('status') == 'cancelled' else '失败'}"
                           "，不再开始新的对接计算。如需继续，请重新提交任务。",
            }, ensure_ascii=False)
        # 未指定受体（受理层判定 source==default，或干脆什么都没给）→ **不执行对接**，
        # 先把可选受体交给用户选：用户没要求就替它挑一个受体来跑，等于答非所问
        # （旧实现会静默回退内建默认受体）。
        if _receptor_unspecified(receptor, run, receptor_file):
            # 预置受体清单**只用于内部测试**，不再作为用户可选来源；
            # 因此这里不给 choices，只把三条真出路交给用户（PDB 号 / UniProt / 上传文件）。
            return json.dumps({
                "status": "needs_user_input",
                "message": ("未指定受体：不能默认挑一个受体开跑。请给出受体来源 —— "
                            "① 提供 PDB 编号；② 提供 UniProt accession；"
                            "③ 上传受体结构文件（.pdb/.cif/.pdbqt）。"),
                "missing": ["receptor"],
            }, ensure_ascii=False)
        site = _parse_site(floats_to_text(site_center, expect=3),
                           floats_to_text(site_size, expect=3))
        # 横向协作：口袋分析 Agent 已提交的盒子直接用（用户显式给坐标时以用户为准）
        if site is None:
            board_for_site = active_blackboard(runtime)
            pinned = board_for_site.get_site() if board_for_site is not None else None
            if pinned and pinned.get("center") and pinned.get("chosen_by") == "pocket_agent":
                site = {"center": pinned["center"], "size": pinned.get("size"),
                        "source": pinned.get("source") or "口袋分析 Agent 选定",
                        "chosen_by": "pocket_agent", "pocket": pinned.get("pocket") or {},
                        "validation": pinned.get("validation") or {}}
                logger.info("使用口袋分析 Agent 提交的对接盒：%s", site["center"])


        def _live_progress(done: int, total: int, _result: dict,
                           runtime: Any = None) -> None:
            """把对接进度**与逐分子结果**写进当前运行，供 SSE 心跳实时推送。

            多 Agent（对话）模式下父流程阻塞、没有 token/tool_call 事件，`live_progress`
            与这里的 `live_molecules` 是「实时逐分子结果」表的唯一数据来源：API 层心跳
            每秒把它们转成 progress / molecules 事件。阳性对照只作基线，不进实时流。
            """
            if run is None:
                return
            run.data["live_progress"] = {
                "stage": "docking", "done": done, "total": total,
                "percent": round(done * 100.0 / total, 1) if total else 0.0,
                "message": f"已完成 {done}/{total}",
            }
            if _result.get("name") != POSITIVE_CONTROL_NAME:
                run.data.setdefault("live_molecules", []).append(
                    _live_molecule_row(_result, max(0, done - 1), total,
                                      runtime=runtime))

        if not refine_from:
            for _m in molecules:            # 粗筛轮次标记（合并时按精度取优）
                _m.setdefault("pass", "coarse")
        if run is not None:
            run.data["live_progress"] = {"stage": "docking", "done": 0, "total": len(molecules),
                                         "percent": 0.0,
                                         # 此刻还没进对接：自动定盒/库级跨度抽样要先跑（大库首次几十秒），
                                         # 措辞必须如实，否则界面显示「开始对接」却长时间不动
                                         "message": f"准备对接 {len(molecules)} 个分子"}
        keep = [x.strip() for x in (keep_hetatm or "").replace("；", ",").replace(";", ",").split(",")
                if x.strip()]
        def _live_note(message: str) -> None:
            """准备阶段（定盒 / 库级下限抽样）也要给界面一条真实说明，避免长时间无反馈。"""
            if run is None:
                return
            run.data["live_progress"] = {"stage": "docking", "done": 0,
                                         "total": len(molecules), "percent": 0.0,
                                         "message": message}

        # 真正的「停止」：把本次运行的协作式取消标志接进对接层。
        # 没有它时，chat/多 Agent 模式下按停止只会等工具自己跑完（真实缺陷）。
        from docking_agent.cancellation import cancel_flag  # noqa: PLC0415  # 避免循环导入

        # ---- 对接前询问：受体自带共晶配体且未指定阳性对照时，先问用户是否用作对照 ----
        # 用户要求"在开始对接前询问"：这里在调用任何引擎之前拦下，避免先跑一遍再问，
        # 也避免"跑完又问一次"（选择会由前端作为 positive_control 下发）。
        try:
            from docking_agent.core.receptors import (  # noqa: PLC0415
                ReceptorInputError, resolve_receptor_specs)
            from docking_agent.tools.choices import (  # noqa: PLC0415
                choices_payload, clear_choices, offer_cocrystal_positive_control,
                receptor_input_guard)

            try:
                preview_specs, _notes = resolve_receptor_specs(
                    receptor, keep_hetatm=tuple(keep), protonation=(protonation or None),
                    ph=(protonation_ph or None))
            except ReceptorInputError as exc:
                # 受体输入不可用：在调用任何引擎之前把选择权交回用户（不回退预置受体）
                return receptor_input_guard(exc, runtime=runtime)
            preview_blocks = [{"receptor": s.get("key") or s.get("label") or "",
                               "receptor_key": s.get("key") or "",
                               # 原始结构路径：共晶配体只在原始结构里，去配体的 PDBQT 里没有
                               "source_pdb": str(s.get("source_pdb") or ""),
                               "receptor_pdb": str(s.get("pdb") or ""),
                               "cocrystal_ligand": s.get("cocrystal_ligand") or {}}
                              for s in preview_specs]
            offered = offer_cocrystal_positive_control(
                preview_blocks, specified_control=control, runtime=runtime)
            if offered:
                # 选项明细只走界面；给模型的载荷不带 SMILES/选项内容，避免正文里再抄一遍
                return json.dumps(choices_payload(
                    offered,
                    message=("受体结构自带共晶配体：是否把它作为阳性对照（结合模式基线）"
                             "需要用户点选确认。"),
                    kind="positive_control"), ensure_ascii=False)
            clear_choices("positive_control", runtime=runtime)
        except Exception as exc:  # noqa: BLE001 - 询问失败不阻塞对接
            logger.warning("对接前的共晶配体询问失败（继续对接）：%s", exc)

        out = dock_library(molecules, receptor=receptor, keep_hetatm=keep,
                           exhaustiveness=int(exhaustiveness), n_poses=int(n_poses),
                           engine=engine, protonation=(protonation or ""),
                           protonation_ph=(protonation_ph or None),
                           site=site,
                           # 表单里的这两项此前在 Agent 路径被忽略（恒存位姿、不套上限）→ 真实行为缺口
                           pose_dir=(str(run.dir / "poses")
                                     if (run is not None and save_poses) else None),
                           max_ligands=(int(max_ligands) or None),
                           # 闭包绑定 runtime：回调由 dock_library 以 (done, total, result) 调用
                           progress_cb=(partial(_live_progress, runtime=runtime)
                                        if run is not None else None),
                           note_cb=_live_note if run is not None else None,
                           cancel_event=cancel_flag(run.id) if run is not None else None)
        if run is not None:
            run.data.pop("live_progress", None)


        # ---- 写入共享黑板：供结合模式检测 Agent 交叉核验（横向协作）----
        board = active_blackboard(runtime)
        if board is not None and out.get("status") == "ok":
            for block in out.get("receptors", []) or []:
                board.set_receptor({"key": block.get("receptor_key"), "name": block.get("receptor_key"),
                                    "pdb": "", "pdbqt": block.get("pdbqt"),
                                    "protein": block.get("protein"),
                                    "center": block.get("box_center"), "size": block.get("box_size"),
                                    "site": block.get("site") or {}})
                board.set_docking(block.get("results") or [])
            boxes = "；".join(
                f"{b.get('receptor_key')} 盒子来源={b.get('box_source') or '—'}"
                for b in (out.get("receptors") or [])[:3])
            board.add_note(f"Docking 执行 Agent：完成 {len(out.get('receptors') or [])} 个受体的对接，"
                           f"结果已写入共享黑板（供结合模式检测交叉核验）。{boxes}")

        if run is not None:
            from docking_agent.config import env_int
            from docking_agent.core.library import POSITIVE_CONTROL_LABEL  # noqa: PLC0415

            max_artifacts = max(0, env_int("POSE_ARTIFACT_MAX", 200))
            for block in out.get("receptors", []) or []:
                for r in block.get("results", []):
                    pose = r.get("pose_file")
                    if not pose:
                        continue
                    name = r.get("name") or r.get("smiles") or "ligand"
                    label = POSITIVE_CONTROL_LABEL if r.get("name") == "__positive_control__" else name
                    art_name = f"pose_{slug(name)}"
                    r["pose_artifact"] = art_name
                    r["pose_url"] = f"/api/runs/{run.id}/artifacts/{art_name}"
                    registered = len([a for a in run.artifacts() if a["name"].startswith("pose_")])
                    if registered >= max_artifacts:
                        continue  # 大库不逐个登记，仍可按名直取 + poses.zip
                    try:
                        rel = run.rel(pose)
                        run.add_artifact(rel, art_name, f"位姿：{label}", "chemical/x-pdbqt")
                    except Exception as e:  # noqa: BLE001
                        logger.debug("位姿登记失败（%s）：%s", pose, e)
    except Exception as e:  # noqa: BLE001
        from docking_agent.core.receptors import ReceptorInputError  # noqa: PLC0415

        if isinstance(e, ReceptorInputError):
            from docking_agent.tools.choices import receptor_input_guard  # noqa: PLC0415

            return receptor_input_guard(e, runtime=runtime)
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

    # 搜索强度的来源必须可追溯（审计缺陷：静默退回 16 时报告里看不出来）
    out["notes"] = list(out.get("notes") or []) + [
        f"搜索强度：exhaustiveness={exhaustiveness}"
        f"（{'调用方显式指定' if _explicit_exh else ('受理层自动规划' if _planned_exh else '系统默认（本次无规划值）')}"
        f"；运行级 —— 同一阶段内所有分子一致）"]
    # 记下「本次体系是否需要特殊化学纪律」（条件提示词用；只记布尔，不记明细）
    from docking_agent.runtime import run_facts

    run_facts.note(active_run(runtime), **run_facts.docking_facts(out))

    # 精算轮次：标注 pass 并保留粗筛分数（合并时按精度取优，避免低精度分污染排序）
    if refine_from:
        coarse = coarse_map
        for block in (out.get("receptors") or []):
            for row in (block.get("results") or []):
                row["pass"] = "fine"
                if coarse.get(row.get("smiles")) is not None:
                    row["affinity_coarse"] = coarse[row["smiles"]]
        out["notes"] = list(out.get("notes") or []) + [
            f"精算轮次：对上一轮最好的 {len(refine_from)} 个分子用 exhaustiveness={exhaustiveness} 重算"]

    # ---- 完整结果落盘（不依赖模型搬运），并按规模决定回传给模型的视图 ----
    tool_io.record("docking", out, run=active_run(runtime))
    limit = tool_io.summary_limit()
    blocks = out.get("receptors") or []
    total_rows = sum(len(b.get("results") or []) for b in blocks)
    if total_rows <= limit:
        # 小库：结构保持完整（下游按字段读），但逐分子溯源压成聚合口径 —— 完整明细在产物里
        return json.dumps(_agent_view(out), ensure_ascii=False)
    flat = [r for b in blocks for r in (b.get("results") or [])]
    ranked = sort_by_affinity(flat)
    summary = {
        "status": out.get("status"),
        "receptors": [{"receptor_key": b.get("receptor_key"), "receptor": b.get("receptor"),
                       "box_center": b.get("box_center"), "box_size": b.get("box_size"),
                       "box_source": b.get("box_source"), "molecules": len(b.get("results") or []),
                       "dropped_hetatm": b.get("dropped_hetatm") or {},
                       "kept_hetatm": b.get("kept_hetatm") or {},
                       "unsupported_hetatm": b.get("unsupported_hetatm") or [],
                       "cocrystal_ligand": (b.get("cocrystal_ligand") or {}).get("resname")}
                      for b in blocks],
        "molecules": total_rows,
        "with_affinity": len([r for r in flat if isinstance(r.get("affinity_kcal_mol"), (int, float))]),
        "failed": len([r for r in flat if r.get("error")]),
        "affinity": tool_io.affinity_stats([r.get("affinity_kcal_mol") for r in flat]),
        "engine": (flat[0].get("engine") if flat else None),
        "exhaustiveness": (flat[0].get("exhaustiveness") if flat else None),
    }
    return json.dumps({
        "status": out.get("status", "ok"),
        "summary": summary,
        "top": tool_io.top_rows(ranked, limit=limit, fields=(
            "receptor", "name", "smiles", "affinity_kcal_mol", "engine", "exhaustiveness",
            "intermolecular_kcal_mol", "torsion_kcal_mol", "pose_file", "error")),
        "detail_omitted": True,
        "artifacts": tool_io.artifact_refs(run=active_run(runtime)),
        "notice": tool_io.big_payload_notice("对接结果", total_rows, limit),
        "notes": out.get("notes") or [],
    }, ensure_ascii=False)
