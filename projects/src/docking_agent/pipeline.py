"""确定性流水线（不依赖 LLM）。

流程：分子库解析 → 理化性质 → 真实分子对接 → 结合模式分析 → 排序与报告产物。
所有中间数据都写入本次运行的目录（见 `docking_agent.runs`），可单独或整体下载。

该模式既用于「无需 LLM 也能跑通全流程」的场景，也是 LLM 多 Agent 模式的对照基线。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from docking_agent.core import (
    plan_concurrency,
    compute_binding_report,
    compute_properties,
    dock_library,
    load_library_file,
    merge_and_rank,
    parse_smiles_text,
    plan_docking_params,
    pose_save_max,
    read_molecule_file,
    resolve_receptor_specs,
    save_poses_for,
)
from docking_agent.paths import libraries_dir
from docking_agent.reporting import (
    build_markdown_report,
    build_ranking_csv,
    collect_failures,
    copy_receptor_files,
    rank_molecules,
    write_report_charts,
    write_report_pdf,
)
from docking_agent.cancellation import CancelledRun, cancel_flag, clear_cancel
from docking_agent.runs import Run, get_run_store, slug

logger = logging.getLogger(__name__)

DEFAULT_POSITIVE_CONTROL = "NC(=N)c1ccccc1"  # 苯甲脒：凝血酶 S1 口袋经典探针
from docking_agent.core import POSITIVE_CONTROL_NAME  # noqa: E402  (常量统一在 core 定义)
POSITIVE_CONTROL_LABEL = "阳性对照"

EventCallback = Callable[[Dict[str, Any]], None]


# --------------------------------------------------------------------------- #
# 分子库与阳性对照
# --------------------------------------------------------------------------- #
def default_library_path():
    return libraries_dir() / "mol_library.csv"


def default_positive_control_path():
    return libraries_dir() / "positive_control.csv"


def load_positive_control() -> str:
    """阳性对照 SMILES：优先 assets/libraries/positive_control.csv，其次内置苯甲脒。"""
    path = default_positive_control_path()
    try:
        if path.exists():
            mols = load_library_file(str(path))
            if mols:
                return mols[0]["smiles"]
    except Exception as e:  # noqa: BLE001
        logger.warning("阳性对照读取失败: %s", e)
    return DEFAULT_POSITIVE_CONTROL


def positive_control_info() -> Dict[str, Any]:
    smiles = load_positive_control()
    name = "benzamidine"
    try:
        mols = load_library_file(str(default_positive_control_path()))
        if mols:
            name = mols[0].get("name") or name
    except Exception as e:  # noqa: BLE001
        logger.debug("读取阳性对照库失败（沿用默认名称）：%s", e)
    return {"name": name, "smiles": smiles,
            "source": str(default_positive_control_path())}


def resolve_molecules(ligands_text: str = "", molecule_file: str = "",
                      allow_example_fallback: bool = False) -> tuple[List[Dict[str, str]], str]:
    """返回 (分子列表, 来源标识)。优先级：文件 > 文本 > 示例库（**仅在明确要求时**）。"""
    if molecule_file:
        fmt, mols = read_molecule_file(molecule_file)
        if mols:
            return mols, f"file:{fmt}"
    if ligands_text and ligands_text.strip():
        mols = parse_smiles_text(ligands_text)
        if mols:
            return mols, "input"
    if allow_example_fallback:
        path = default_library_path()
        if path.exists():
            mols = load_library_file(str(path))
            if mols:
                return mols, "example-library"
    return [], "none"


def _properties(molecules: List[Dict[str, str]], protonation: str = "",
                protonation_ph: Any = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in molecules:
        try:
            prop = compute_properties(m["smiles"], protonation or None, protonation_ph)
            prop["name"] = m.get("name") or prop.get("smiles")
            out.append(prop)
        except Exception as e:  # noqa: BLE001
            out.append({"name": m.get("name"), "smiles": m.get("smiles"), "error": str(e)})
    return out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_pipeline(ligands_text: str = "", molecule_file: str = "",
                 receptor: Any = None, receptor_file: str = "", positive_control: str = "",
                 exhaustiveness: Optional[int] = None, n_poses: Optional[int] = None,
                 engine: str = "auto",
                 pocket_engine: str = "",
                 allow_example_fallback: bool = False,
                 dock_positive_control: bool = True,
                 site: Optional[Dict[str, Any]] = None,
                 max_ligands: Optional[int] = None,
                 save_poses: bool = True,
                 skip_positive_control: bool = False,
                 protonation: str = "", protonation_ph: Any = None,
                 run: Optional[Run] = None,
                 progress_cb: Optional[EventCallback] = None) -> Dict[str, Any]:
    """执行一次完整的确定性筛选流水线。

    只要解析出至少一个分子，就会完整执行「属性 → 对接 → 结合模式 → 排序 → 报告」，
    不会中途停止（阳性对照可选：`skip_positive_control=True` 时跳过结合模式对照）。

    `exhaustiveness` / `n_poses` 为 None 时由 `core/params.plan_docking_params` **自动规划**
    （按任务类型、库柔性、盒体积与预算），规划结果写入 `result["param_plan"]` 与报告；
    显式给出数值时视为用户参数（`source="user"`），自动规划不改写。`N ≥ AGENT_FUNNEL_MIN(500)`
    时自动走两阶段漏斗（粗筛 → 头部精算，复用 `dock_library`）。

    返回结果字典，并附带 `run_id` 与 `artifacts`（本次运行的全部可下载产物）。
    progress_cb 会收到 stage / progress / molecules / molecule 事件，供 SSE 实时推送。
    """
    request = {
        "mode": "pipeline",
        "ligands_text": ligands_text,
        "molecule_file": molecule_file,
        "receptor": receptor if isinstance(receptor, (str, list, type(None))) else str(receptor),
        "receptor_file": receptor_file,
        "positive_control": positive_control,
        "exhaustiveness": exhaustiveness,
        "n_poses": n_poses,
        "engine": engine,
        "site_center": (site or {}).get("center"),
        "site_size": (site or {}).get("size"),
        "save_poses": save_poses,
        "max_ligands": max_ligands,
        "protonation": protonation,
        "protonation_ph": protonation_ph,
    }
    run = run or get_run_store().new("pipeline", request)
    run.data["request"] = request
    run.save()
    cancel_event = cancel_flag(run.id)

    def emit(event: Dict[str, Any]) -> None:
        if progress_cb:
            try:
                progress_cb(event)
            except Exception:  # noqa: BLE001
                logger.debug("progress_cb 异常", exc_info=True)

    # ---- 实时推送节流：大库时逐分子事件必须合并，否则会淹没前端 ----
    import time as _time

    from docking_agent.config import env_int as _env_int

    batch_size = max(1, _env_int("SSE_BATCH_SIZE", 25))
    flush_ms = max(50, _env_int("SSE_FLUSH_MS", 200))
    progress_ms = max(200, _env_int("SSE_PROGRESS_MS", 1000))
    early_notes: List[str] = []
    _buf: List[Dict[str, Any]] = []
    _last_flush = [0.0]
    _last_progress = [0.0]
    _t0 = _time.time()

    def flush(force: bool = False) -> None:
        now = _time.time()
        if not _buf:
            return
        if force or len(_buf) >= batch_size or (now - _last_flush[0]) * 1000 >= flush_ms:
            emit({"type": "molecules", "items": list(_buf)})
            _buf.clear()
            _last_flush[0] = now

    def progress(stage: str, done: int, total: int, message: str = "", force: bool = False) -> None:
        now = _time.time()
        if not force and (now - _last_progress[0]) * 1000 < progress_ms:
            return
        _last_progress[0] = now
        elapsed = now - _t0
        eta = (elapsed / done * (total - done)) if (done and total > done) else None
        emit({"type": "progress", "stage": stage, "done": done, "total": total,
              "percent": round(done * 100.0 / total, 1) if total else 0.0,
              "elapsed_sec": round(elapsed, 1),
              "eta_sec": round(eta, 1) if eta else None,
              "message": message or f"已完成 {done}/{total}"})

    # 用户上传的受体文件优先于注册表受体
    receptor_arg: Any = (receptor_file or "").strip() or receptor

    try:
        # ---- 1. 分子库 ----
        emit({"type": "stage", "stage": "import", "message": "解析候选分子库", "index": 0, "total": 0})
        molecules, source = resolve_molecules(ligands_text, molecule_file, allow_example_fallback)
        if not molecules:
            run.log("未解析到任何有效分子")
            run.finish("error", error="no_molecules",
                       message="未解析到任何有效分子，请提供 SMILES 或分子文件")
            return {
                "status": "no_molecules",
                "run_id": run.id,
                "message": "未解析到任何有效分子。请提供 SMILES 文本 / 小分子文件（SDF/SMI/CSV/MOL2），"
                           "或在 assets/libraries/mol_library.csv 放置示例库。",
                "artifacts": [],
            }
        run.write_json("molecules", molecules, label="候选分子库")
        run.log(f"分子库来源={source}，共 {len(molecules)} 个分子")
        emit({"type": "stage", "stage": "import", "message": f"已解析 {len(molecules)} 个分子（来源：{source}）",
              "index": 0, "total": len(molecules)})

        # 阳性对照是**可选项**：只有用户显式提供了阳性对照 SMILES 才做对照对接与比较
        target = (positive_control or "").strip()
        use_control = bool(dock_positive_control) and not skip_positive_control and bool(target)
        if not use_control:
            reason = "已按要求跳过阳性对照" if skip_positive_control else "未提供阳性对照"
            run.log(f"{reason}，不做对照对接与结合模式比较")
            notes_hint = ("未提供阳性对照，已跳过对照分子对接与结合模式比较"
                          "（如需，请填写阳性对照 SMILES）")
            if skip_positive_control:
                notes_hint = "已按要求跳过阳性对照分析"
            early_notes.append(notes_hint)

        # ---- 2. 理化性质 ----
        emit({"type": "stage", "stage": "properties", "message": "计算理化性质与类药性（RDKit）",
              "index": 0, "total": len(molecules)})
        properties = _properties(molecules, protonation, protonation_ph)
        run.write_json("properties", properties, label="理化性质（RDKit）")
        run.log(f"理化性质计算完成（{len(properties)} 个）")

        # ---- 2b. 结合口袋分析（真实工具定盒子；有显式位点或 known_site 时跳过）----
        pocket_info: Dict[str, Any] = {}
        if not site:
            from docking_agent.core.pockets import engine_settings

            pocket_settings = engine_settings()
            if (pocket_engine or "").strip():
                pocket_settings["engine"] = pocket_engine.strip()
            emit({"type": "stage", "stage": "pocket",
                  "message": f"分析结合口袋并确定对接盒子（引擎={pocket_settings['engine']}）",
                  "index": 0, "total": 0})
            try:
                # 口袋分析必须用**同一个**受体：同一目标 pH + 同一 keep 集。
                # 内容寻址的缓存让这里直接命中上一步的准备结果（≈0.03 s），
                # 既不做重复劳动，也不会出现「分析用的是模板态、对接用的是 pH 态」的口径分叉。
                specs, _notes = resolve_receptor_specs(
                    receptor_arg, protonation=protonation, ph=protonation_ph)
                picked = None
                for spec in specs[:1]:
                    path = str(spec.get("pdbqt") or spec.get("file") or "")
                    if not path:
                        continue
                    from docking_agent.core.pockets import select_site

                    picked = select_site(spec, engine=pocket_settings["engine"],
                                         top_n=pocket_settings["top_n"],
                                         padding=pocket_settings["padding"],
                                         min_size=pocket_settings["min_size"],
                                         max_size=pocket_settings["max_size"])
                if picked:
                    pocket_info = picked
                    run.write_json("pockets", {"engine": picked.get("engine"),
                                               "source": picked.get("source"),
                                               "chosen_by": picked.get("chosen_by"),
                                               "center": picked.get("center"),
                                               "size": picked.get("size"),
                                               "validation": picked.get("validation") or {},
                                               "warnings": picked.get("warnings") or [],
                                               "pockets": picked.get("pockets") or []},
                                   label="结合口袋预测与对接盒溯源")
                    emit({"type": "stage", "stage": "pocket",
                          "message": f"对接盒：{picked.get('source')}",
                          "index": 1, "total": 1,
                          "box_center": picked.get("center"), "box_size": picked.get("size"),
                          "chosen_by": picked.get("chosen_by"),
                          "pocket_count": len(picked.get("pockets") or [])})
                    run.log(f"口袋分析：{picked.get('source')}")
            except Exception as e:  # noqa: BLE001
                logger.warning("口袋分析失败，沿用受体原有点位：%s", e)
                emit({"type": "stage", "stage": "pocket", "skipped": True,
                      "message": f"口袋分析未完成（{e}），沿用受体已注册位点"})

        # ---- 3. 对接参数自动规划（运行级/漏斗阶段级；绝不逐分子）----
        # 用户显式给出的参数（source=user）一律不改；未给出时按任务/库柔性/盒体积/预算规划。
        box_for_plan: Optional[List[float]] = None
        if site and site.get("size"):
            box_for_plan = [float(x) for x in site["size"]]
        elif pocket_info.get("size"):
            box_for_plan = [float(x) for x in pocket_info["size"]]

        def _pilot(candidates: List[Dict[str, Any]]) -> Optional[float]:
            return _pilot_measure(candidates, receptor_arg, site, engine)

        plan = plan_docking_params(
            task_type="screening", molecules=molecules, box_size=box_for_plan,
            user_params={"exhaustiveness": exhaustiveness, "n_poses": n_poses},
            pilot=_pilot,
        )
        run.data["param_plan"] = plan
        run.save()
        fine_exh = int(plan.get("exhaustiveness") or 6)
        np_used = int(plan.get("n_poses") or 1)
        two_stage = bool(plan.get("two_stage") and plan.get("coarse_exhaustiveness"))
        funnel_text = (f"两阶段漏斗（粗筛 exh={plan['coarse_exhaustiveness']} / 精算 exh={fine_exh}，"
                       f"精算前 {plan.get('refine_top_n')}）" if two_stage else "单阶段")
        run.log(f"参数自动规划（source={plan.get('source')}）：exhaustiveness={fine_exh}，"
                f"n_poses={np_used}，{funnel_text}")
        for note in plan.get("warnings") or []:
            run.log(f"参数规划提示：{note}")
        emit({"type": "stage", "stage": "params",
              "message": f"参数自动规划：exhaustiveness={fine_exh}，n_poses={np_used}，{funnel_text}",
              "index": 0, "total": 0, "param_plan": plan})

        # ---- 3b. 分子对接 ----
        docking_pool = list(molecules)
        dock_target = docking_pool + ([{"name": POSITIVE_CONTROL_NAME, "smiles": target}]
                                      if use_control else [])
        emit({"type": "stage", "stage": "docking",
              "message": f"执行真实分子对接（引擎={engine}，搜索强度={fine_exh}，"
                         f"{len(docking_pool)} 个分子，{funnel_text}，并行度 "
                         f"{plan_concurrency(len(docking_pool))['workers']}）",
              "index": 0, "total": len(docking_pool)})
        progress("docking", 0, len(docking_pool), "开始对接", force=True)

        props_by_smiles = {p.get("smiles"): p for p in properties}
        done_count = 0

        def _on_ligand(idx: int, total: int, result: Dict[str, Any]) -> None:
            nonlocal done_count
            # 阳性对照只作为对比基准，不进「候选分子」实时流（其结果随 done/运行详情给出）
            if result.get("name") == POSITIVE_CONTROL_NAME:
                return
            done_count += 1
            prop = props_by_smiles.get(result.get("smiles")) or {}
            sim = _similarity_of(result.get("smiles"), target)
            event = {
                "index": done_count - 1,
                "total": len(docking_pool),
                "name": result.get("name"),
                "smiles": result.get("smiles"),
                "affinity_kcal_mol": result.get("affinity_kcal_mol"),
                "intermolecular_kcal_mol": result.get("intermolecular_kcal_mol"),
                "intramolecular_kcal_mol": result.get("intramolecular_kcal_mol"),
                "torsion_kcal_mol": result.get("torsion_kcal_mol"),
                "engine": result.get("engine"),
                "exhaustiveness": result.get("exhaustiveness"),
                "box_group": result.get("box_group"),
                "box_size": result.get("box_size"),
                "error": result.get("error"),
                "pose_artifact": f"pose_{slug(result.get('name') or result.get('smiles') or 'ligand')}" if result.get("pose_file") else None,
                "properties": {k: v for k, v in prop.items() if k not in ("smiles", "name")},
                "similarity_to_positive_control": sim,
            }
            _buf.append(event)
            flush()
            progress("docking", done_count, len(docking_pool))

        def _coarse_progress(done: int, total: int, result: Dict[str, Any]) -> None:
            progress("docking", done, total, f"粗筛中 {done}/{total}（exh={plan.get('coarse_exhaustiveness')}）")

        pose_root = str(run.dir / "poses") if save_poses else None
        if two_stage:
            coarse_exh = int(plan["coarse_exhaustiveness"])
            refine_n = int(plan.get("refine_top_n") or 200)
            emit({"type": "stage", "stage": "docking",
                  "message": f"两阶段漏斗①：全库粗筛 {len(docking_pool)} 个分子"
                             f"（exhaustiveness={coarse_exh}）",
                  "index": 0, "total": len(docking_pool)})
            coarse = dock_library(
                dock_target, receptor=receptor_arg,
                exhaustiveness=coarse_exh, n_poses=np_used,
                engine=(engine or "auto"), protonation=protonation, protonation_ph=protonation_ph,
                site=site, max_ligands=max_ligands,
                pose_dir=pose_root, progress_cb=_coarse_progress,
                cancel_event=cancel_event,
            )
            if coarse.get("status") != "ok":
                # 粗筛失败不推翻整次运行：退回单阶段（用精算强度跑全库），如实记录
                run.log("粗筛失败 → 退回单阶段对接")
                plan = {**plan, "two_stage": False, "coarse_exhaustiveness": None,
                        "refine_top_n": None,
                        "decisions": list(plan.get("decisions") or [])
                        + [f"粗筛失败 → 退回单阶段（exh={fine_exh} 跑全库）"],
                        "warnings": list(plan.get("warnings") or []) + ["粗筛失败，已退回单阶段"]}
                run.data["param_plan"] = plan
                two_stage = False
                docking = dock_library(
                    dock_target, receptor=receptor_arg,
                    exhaustiveness=fine_exh, n_poses=np_used,
                    engine=(engine or "auto"),
                    site=site, max_ligands=max_ligands,
                    pose_dir=pose_root, progress_cb=_on_ligand,
                    cancel_event=cancel_event,
                )
            else:
                refine_mols = _funnel_refine_list(coarse, refine_n, use_control)
                if not refine_mols:
                    run.log("粗筛没有可精算的有效结果 → 直接采用粗筛结果")
                    docking = coarse
                else:
                    emit({"type": "stage", "stage": "docking",
                          "message": f"两阶段漏斗②：精算头部 {len(refine_mols)} 个分子"
                                     f"（exhaustiveness={fine_exh}，沿用粗筛盒子）",
                          "index": 0, "total": len(refine_mols)})
                    fine_blocks: List[Any] = []
                    for cblk in coarse.get("receptors", []) or []:
                        key = str(cblk.get("receptor_key") or "receptor")
                        site_i = {"center": cblk.get("box_center"), "size": cblk.get("box_size"),
                                  "source": "两阶段漏斗：沿用粗筛盒子（保证阶段间盒子一致）",
                                  "chosen_by": "funnel"}
                        sub_pose = None
                        if pose_root:
                            sub_pose = (os.path.join(pose_root, key)
                                        if len(coarse.get("receptors") or []) > 1 else pose_root)
                        fine_i = dock_library(
                            refine_mols, receptor=(cblk.get("pdbqt") or receptor_arg),
                            exhaustiveness=fine_exh, n_poses=np_used,
                            engine=(engine or "auto"), protonation=protonation, protonation_ph=protonation_ph,
                            site=site_i, max_ligands=max_ligands,
                            pose_dir=sub_pose, progress_cb=_on_ligand,
                            cancel_event=cancel_event,
                        )
                        if fine_i.get("status") == "ok":
                            fine_blocks.append((key, fine_i))
                        else:
                            run.log(f"精算阶段失败（受体 {key}）：{fine_i.get('message')}")
                    docking = _merge_funnel(coarse, fine_blocks, coarse_exh, fine_exh,
                                            refine_n)
        else:
            docking = dock_library(
                dock_target, receptor=receptor_arg,
                exhaustiveness=fine_exh, n_poses=np_used,
                engine=(engine or "auto"), protonation=protonation, protonation_ph=protonation_ph,
                site=site, max_ligands=max_ligands,
                pose_dir=pose_root, progress_cb=_on_ligand,
                cancel_event=cancel_event,
            )
        if docking.get("status") != "ok":
            run.log(f"对接失败：{docking.get('message')}")
            run.finish("error", error=str(docking.get("message")))
            return {"status": docking.get("status", "error"), "run_id": run.id,
                    "message": docking.get("message"), "artifacts": run_artifacts(run)}

        # 拆出阳性对照并登记位姿产物
        for block in docking.get("receptors", []) or []:
            pc_rows = [r for r in block.get("results", []) if r.get("name") == POSITIVE_CONTROL_NAME]
            block["results"] = [r for r in block.get("results", []) if r.get("name") != POSITIVE_CONTROL_NAME]
            pc_row = pc_rows[0] if pc_rows else {}
            if pc_row:
                pc_row["name"] = POSITIVE_CONTROL_LABEL  # 对外展示用友好名称
            block["positive_control"] = pc_row
            pose_artifact_max = _env_int("POSE_ARTIFACT_MAX", 200)
            for r in block["results"]:
                if not r.get("pose_file"):
                    continue
                art_name = f"pose_{slug(r.get('name') or r.get('smiles') or 'ligand')}"
                r["pose_artifact"] = art_name
                r["pose_url"] = f"/api/runs/{run.id}/artifacts/{art_name}"
                # 清单只登记前 N 个：上万分子逐个登记会让产物清单与接口响应膨胀
                if len([a for a in run.artifacts() if a["name"].startswith("pose_")]) < pose_artifact_max:
                    run.add_artifact(_rel(r["pose_file"], run), art_name,
                                     f"位姿：{r.get('name')}", "chemical/x-pdbqt")
        flush(force=True)
        progress("docking", len(docking_pool), len(docking_pool), "对接完成", force=True)
        run.write_json("docking", docking, label="对接明细（含能量项与位姿路径）")
        run.log(f"对接完成（引擎={engine}，搜索强度={exhaustiveness}）")

        # ---- 4. 结合模式分析 ----
        if use_control:
            emit({"type": "stage", "stage": "binding",
                  "message": "计算与阳性对照的双指纹相似度与药效团结合模式",
                  "index": 0, "total": len(molecules)})
            progress("binding", len(molecules), len(molecules), "结合模式分析完成", force=True)
            binding = compute_binding_report(molecules, target)
            run.write_json("binding", binding, label="结合模式分析")
        else:
            emit({"type": "stage", "stage": "binding", "skipped": True,
                  "message": "已跳过阳性对照比较（未提供阳性对照）",
                  "index": 0, "total": len(molecules)})
            binding = {}

        # ---- 5. 汇总排序 ----
        receptor_blocks: List[Dict[str, Any]] = []
        for block in docking.get("receptors", []) or []:
            ranked = rank_molecules(merge_and_rank(properties, block.get("results", []), binding))
            pc_row = dict(block.get("positive_control") or {})
            if pc_row:
                pc_row.setdefault("name", "PositiveControl")
            receptor_blocks.append({**block, "ranking": ranked, "positive_control": pc_row})

        result: Dict[str, Any] = {
            "status": "ok",
            "run_id": run.id,
            "source": source,
            "molecules": molecules,
            "positive_control_smiles": target,
            "properties": properties,
            "task_spec": {"task_type": "screening", "authority": "pipeline",
                          "decision": "run", "source": "rules",
                          "assumptions": [f"确定性流水线（零模型）：对接参数自动规划 "
                                          f"(source={plan.get('source')})，"
                                          f"exhaustiveness={fine_exh}、n_poses={np_used}"]},
            "param_plan": plan,
            "docking": docking,
            "binding": binding,
            "receptors": receptor_blocks,
            "notes": docking.get("notes", []),
        }
        if receptor_blocks:
            result["ranking"] = receptor_blocks[0]["ranking"]
            result["positive_control"] = receptor_blocks[0]["positive_control"]
        else:
            result["ranking"], result["positive_control"] = [], {}

        # ---- 5b. 大库位姿策略：分子数超过上限时只补写每个受体最优的前 N 个 ----
        notes: List[str] = list(result["notes"]) + early_notes
        if save_poses and receptor_blocks and len(molecules) > pose_save_max():
            top_n = max(1, _env_int("POSE_TOP_N", 200))
            notes.append(f"分子数（{len(molecules)}）超过位姿保存上限 {pose_save_max()}，"
                         f"位姿仅保留每个受体亲和力最优的前 {top_n} 个")
            run.log(notes[-1])
            for block in receptor_blocks:
                top_rows = block.get("ranking", [])[:top_n]
                if not top_rows or not block.get("pdbqt"):
                    continue
                spec = {"key": block.get("receptor_key"), "name": block.get("receptor_key"),
                        "pdb": "", "pdbqt": block["pdbqt"],
                        "center": block.get("box_center"), "size": block.get("box_size")}
                pose_dir = run.dir / "poses"
                rows = save_poses_for(
                    spec,
                    [{"name": r.get("name") or r.get("smiles"), "smiles": r.get("smiles")} for r in top_rows],
                    str(pose_dir), engine=(engine or "auto"),
                    exhaustiveness=fine_exh, n_poses=np_used,
                )
                for r in rows:
                    if not r.get("pose_file"):
                        continue
                    art_name = f"pose_{slug(r.get('name') or r.get('smiles') or 'ligand')}"
                    run.add_artifact(_rel(r["pose_file"], run), art_name,
                                     f"位姿：{r.get('name')}", "chemical/x-pdbqt")
                    r["pose_artifact"] = art_name

        # ---- 5c. 把位姿下载信息回填到排序行（界面卡片按需下载） ----
        for block in receptor_blocks:
            pose_by_smiles: Dict[str, str] = {}
            for r in block.get("results", []) or []:
                if r.get("pose_file"):
                    pose_by_smiles[r["smiles"]] = f"pose_{slug(r.get('name') or r.get('smiles') or 'ligand')}"
            for r in block.get("ranking", []) or []:
                art = pose_by_smiles.get(r.get("smiles")) or r.get("pose_artifact")
                if art:
                    r["pose_artifact"] = art
                    r["pose_url"] = f"/api/runs/{run.id}/artifacts/{art}"

        # ---- 5d. 完整排序落盘（分页接口据此读取）+ 聚合统计 ----
        primary_ranking = result.get("ranking") or []
        run.write_ranking(primary_ranking)

        # ---- 6. 报告产物 ----
        emit({"type": "stage", "stage": "report", "message": "生成排序 CSV、图表与报告",
              "index": 0, "total": len(molecules)})
        _write_report_artifacts(run, result)

        result["notes"] = notes
        run.set(
            receptor=(receptor_blocks[0].get("receptor") if receptor_blocks else None),
            receptor_label=(receptor_blocks[0].get("receptor") if receptor_blocks else None),
            site=({"center": receptor_blocks[0].get("box_center"),
                   "size": receptor_blocks[0].get("box_size")} if receptor_blocks else None),
            engine=(result["ranking"][0].get("engine") if result["ranking"] else engine),
            exhaustiveness=fine_exh,
            n_poses=np_used,
            two_stage=bool(plan.get("two_stage")),
            coarse_exhaustiveness=plan.get("coarse_exhaustiveness"),
            param_plan=plan,
            molecule_count=len(molecules),
            top=[{"name": m.get("name"), "affinity_kcal_mol": m.get("affinity_kcal_mol")}
                 for m in result["ranking"][:3]],
            notes=result["notes"],
        )
        run.finish("ok")
        run.log("全部完成")
        clear_cancel(run.id)
        result["artifacts"] = run_artifacts(run)
        flush(force=True)
        progress("done", len(molecules), len(molecules), "完成", force=True)
        emit({"type": "stage", "stage": "done", "message": "完成",
              "index": len(molecules), "total": len(molecules)})
        return result

    except CancelledRun as e:
        logger.info("流水线被取消：%s", e)
        run.log(f"已取消：{e}")
        flush(force=True)
        run.finish("cancelled", error=str(e))
        emit({"type": "cancelled", "run_id": run.id,
              "message": str(e), "summary": run.to_dict()})
        clear_cancel(run.id)
        return {"status": "cancelled", "run_id": run.id, "message": str(e),
                "artifacts": run_artifacts(run)}
    except Exception as e:  # noqa: BLE001
        logger.exception("流水线执行失败")
        run.log(f"执行失败：{e}")
        run.finish("error", error=str(e))
        clear_cancel(run.id)
        raise


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _similarity_of(smiles: Optional[str], target: str) -> Optional[float]:
    from docking_agent.core import compute_tanimoto  # noqa: PLC0415

    if not smiles:
        return None
    try:
        return round(compute_tanimoto(smiles, target), 3)
    except Exception:  # noqa: BLE001
        return None


def _rel(path: str, run: Run) -> str:
    """运行目录内的相对路径（统一实现见 `runs.Run.rel`）。"""
    return run.rel(run.dir / path)


# --------------------------------------------------------------------------- #
# 两阶段漏斗（粗筛 → 头部精算）与 pilot 试跑
# --------------------------------------------------------------------------- #
def _pilot_measure(candidates: List[Dict[str, Any]], receptor: Any,
                   site: Optional[Dict[str, Any]], engine: str) -> Optional[float]:
    """pilot 试跑：对最贵的少数分子以 exhaustiveness=1 真实对接，返回单分子平均秒数。

    只用于**测量**：不写位姿、不进 ranking/黑板；失败返回 None（由规划层退回静态规划）。
    """
    if not candidates:
        return None
    t0 = time.time()
    try:
        out = dock_library(
            list(candidates), receptor=receptor, exhaustiveness=1, n_poses=1,
            engine=(engine or "auto"), protonation=protonation, protonation_ph=protonation_ph, site=site, pose_dir=None,
            progress_cb=None, cancel_event=None,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("pilot 试跑失败（不影响结果，退回静态规划）：%s", e)
        return None
    elapsed = time.time() - t0
    if not isinstance(out, dict) or out.get("status") != "ok":
        logger.warning("pilot 试跑未成功（不影响结果）：%s",
                       (out or {}).get("message") if isinstance(out, dict) else out)
        return None
    rows = [r for blk in (out.get("receptors") or [])
            for r in (blk.get("results") or []) if r.get("affinity_kcal_mol") is not None]
    if not rows:
        return None
    return elapsed / len(rows)


def _funnel_refine_list(coarse: Dict[str, Any], refine_n: int,
                        use_control: bool) -> List[Dict[str, str]]:
    """从粗筛结果里取亲和力最好的前 N 个（含阳性对照），作为精算清单。"""
    rows = [r for blk in (coarse.get("receptors") or [])
            for r in (blk.get("results") or [])
            if r.get("smiles") and r.get("affinity_kcal_mol") is not None and not r.get("error")]
    rows.sort(key=lambda r: float(r["affinity_kcal_mol"]))
    selected = rows[:max(1, int(refine_n))]
    if use_control:
        controls = [r for r in rows if r.get("name") == POSITIVE_CONTROL_NAME]
        picked = {r.get("smiles") for r in selected}
        selected.extend([r for r in controls if r.get("smiles") not in picked])
    out: List[Dict[str, str]] = []
    seen = set()
    for r in selected:
        smi = r.get("smiles")
        if smi and smi not in seen:
            seen.add(smi)
            out.append({"name": r.get("name") or smi, "smiles": smi})
    return out


def _merge_funnel(coarse: Dict[str, Any], fine_blocks: List[Any],
                  coarse_exh: int, fine_exh: int, refine_n: int) -> Dict[str, Any]:
    """合并两阶段结果：**同一分子保留精度更高的精算行**，粗筛分数存 `affinity_coarse`。

    同一 pass 内所有行共用同一盒子/搜索强度（一致性不变量，见 tests/test_param_plan.py）。
    `fine_blocks` 为 `[(粗筛 receptor_key, fine_out), ...]`（按粗筛受体逐一对齐）。
    """
    fine_by_key: Dict[str, List[Dict[str, Any]]] = {}
    for key, fb in fine_blocks:
        rows: List[Dict[str, Any]] = []
        for blk in (fb.get("receptors") or []):
            rows.extend(blk.get("results") or [])
        fine_by_key[str(key)] = rows

    merged: List[Dict[str, Any]] = []
    for cblk in (coarse.get("receptors") or []):
        key = str(cblk.get("receptor_key") or cblk.get("receptor") or "receptor")
        fine_rows = {r.get("smiles"): r for r in fine_by_key.get(key, []) if r.get("smiles")}
        coarse_aff = {r.get("smiles"): r.get("affinity_kcal_mol")
                      for r in (cblk.get("results") or []) if r.get("smiles")}
        rows: List[Dict[str, Any]] = []
        used = set()
        for r in (cblk.get("results") or []):
            smi = r.get("smiles")
            fr = fine_rows.get(smi)
            if fr is not None:
                row = dict(fr)
                row["pass"] = "fine"
                if coarse_aff.get(smi) is not None:
                    row["affinity_coarse"] = coarse_aff[smi]
                rows.append(row)
                used.add(smi)
            else:
                row = dict(r)
                row.setdefault("pass", "coarse")
                rows.append(row)
                used.add(smi)
        for smi, fr in fine_rows.items():     # 精算新增的行（理论上不会发生，保底不丢数据）
            if smi not in used:
                row = dict(fr)
                row["pass"] = "fine"
                rows.append(row)
        merged.append({**cblk, "results": rows})

    total_coarse = sum(len(b.get("results") or []) for b in (coarse.get("receptors") or []))
    refined = sum(1 for b in merged for r in (b.get("results") or []) if r.get("pass") == "fine")
    notes = list(coarse.get("notes") or [])
    notes.append(f"两阶段漏斗：全库粗筛 {total_coarse} 个分子（exhaustiveness={coarse_exh}）→ "
                 f"头部精算 {refined} 个分子（exhaustiveness={fine_exh}，上限 {refine_n}）；"
                 f"同一分子的粗筛分数保留在 affinity_coarse 列，排序采用精度更高的精算值")
    return {"status": "ok", "receptors": merged, "notes": notes,
            "concurrency": coarse.get("concurrency"), "poses_saved": coarse.get("poses_saved"),
            "two_stage": True, "coarse_exhaustiveness": coarse_exh,
            "fine_exhaustiveness": fine_exh, "refine_top_n": refine_n}


def _write_report_artifacts(run: Run, result: Dict[str, Any]) -> None:
    """把排序 CSV、图表、Markdown 报告写入运行目录并登记为产物。"""
    ranking = result.get("ranking") or []
    pc = result.get("positive_control") or {}

    # 报告定制（如由上层写入 run.data，例如复用/重生成报告时）：与多 Agent 路径同口径
    _custom = dict(run.data.get("report_customization") or {})
    if _custom and not result.get("report_customization"):
        result["report_customization"] = _custom

    # 受体结构入包（与多 Agent 路径共用同一实现）：整包下载要能独立复现本次对接
    _request = dict(run.data.get("request") or {})
    copy_receptor_files(run, result.get("receptors") or [], source_candidates=[
        _request.get("receptor_file") or "",
    ])

    csv_text = build_ranking_csv(ranking, pc, failures=collect_failures(result.get("docking")))
    run.write_text("ranking.csv", csv_text, name="ranking_csv", label="排序结果 CSV",
                   content_type="text/csv; charset=utf-8")

    # 图表统一由 reporting.artifacts 生成（两种运行模式共用同一套图）
    write_report_charts(run, ranking, pc, result=result)

    md = build_markdown_report(
        result, kind="pipeline", run_id=run.id,
        receptor_label=(result.get("receptors") or [{}])[0].get("receptor", "") if result.get("receptors") else "",
        site={"center": (result.get("receptors") or [{}])[0].get("box_center"),
              "size": (result.get("receptors") or [{}])[0].get("box_size"),
              "source": "注册表/用户指定"} if result.get("receptors") else None,
        artifacts=run_artifacts(run),
        created_at=str(run.data.get("created_at") or ""),
    )
    run.write_text("report.md", md, name="report_md", label="分析报告（Markdown）",
                   content_type="text/markdown; charset=utf-8")
    # PDF 版报告：失败只记 warning（见 write_report_pdf），不影响已完成的对接结果
    write_report_pdf(run, result)
    run.write_json("result", {k: result.get(k) for k in
                              ("status", "source", "ranking", "positive_control", "receptors",
                               "notes", "param_plan", "task_spec")},
                   label="完整结果（JSON）")


def run_artifacts(run: Run) -> List[Dict[str, Any]]:
    """返回本次运行的产物清单（并落盘 run.json）。"""
    run.save()
    return run.artifacts()
