"""解析「不确定」时的结构化选择通道与失败回执（供 tools/online.py 调用）。

为什么单独一个模块：受体/分子的自动解析会出现三种结果，除了「高置信直接跑」之外，
另外两种都要把**证据**交回用户：
  1. ``ambiguous`` / ``low_confidence``：检索到多个同样合理的候选（或单一候选置信度不足）
     → 生成 ``choices``（前端可点选的结构化选项），让用户选，绝不替用户决定；
  2. ``not_found``：一个都没查到 → 回执里**逐条列出已尝试的检索**。

同时提供两条「写回运行上下文」的能力：
  * ``mark_receptor_unresolved``：把解析失败写进 ``task_spec.receptor.source``，
    让 ``run_docking`` 的护栏在调用任何对接引擎之前拦下（产品底线：不明确就不算）；
  * ``publish_choices``：把可选项写进 ``run.data``，由 SSE ``choices`` 事件下发前端。

choices 每项结构：``{id, kind, label, value, prompt, detail}``（kind ∈ receptor|molecule），
``label`` 给人看、``value`` 是机器可用值（accession/CID/SMILES）、``prompt`` 是点选后原样发出的追问。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Sequence, Tuple

from docking_agent.core.resolve import candidate_brief, summarize_attempts
from docking_agent.paths import project_root
from docking_agent.runtime.context import active_run

logger = logging.getLogger(__name__)


def mark_receptor_unresolved(source: str, status: str, resolution: Dict[str, Any],
                             runtime: Any = None) -> None:
    """把「点名受体在线自动解析失败/歧义」写回本次运行规约。

    为什么必须写回而不是只靠提示词：``run_docking`` 的护栏读的就是
    ``task_spec.receptor.source == "unresolved"``。主管 Agent 若没照提示停下来提问，
    护栏仍会在调用任何对接引擎之前拦下 —— 产品底线（不明确计算对象时绝不计算）由代码保证，
    而不是靠模型自觉。
    """
    run = active_run(runtime)
    if run is None:
        return
    spec = dict(getattr(run, "data", {}).get("task_spec") or {})
    receptor = dict(spec.get("receptor") or {})
    if str(receptor.get("source") or "") not in ("named", "unresolved"):
        return
    receptor.update({
        "name": receptor.get("name") or source,
        "source": "unresolved",
        "resolution": {"status": status, "requested": source,
                       "attempts": resolution.get("attempts") or [],
                       "candidates": [candidate_brief(c)
                                      for c in (resolution.get("candidates") or [])]},
    })
    spec["receptor"] = receptor
    run.data["task_spec"] = spec
    try:
        run.log(f"受体「{source}」在线自动解析未成功（{status}）：已阻断后续对接，等待用户确认")
    except Exception:  # noqa: BLE001
        logger.debug("写运行日志失败", exc_info=True)


def mark_receptor_input_invalid(source: str, reason: str, message: str,
                                runtime: Any = None) -> None:
    """用户上传的受体文件不可用 / 受体名无法识别 → 把本次运行的受体标为 `unresolved`。

    与 `mark_receptor_unresolved`（在线解析失败）同一目的：让 `run_docking` 的护栏
    **在代码层**拦住后续计算，而不是只靠提示词自觉 —— 用户没重新给出可用受体之前，
    不该有任何引擎被启动（更不该拿预置受体跑完一整套）。
    """
    run = active_run(runtime)
    if run is None:
        return
    data = getattr(run, "data", {})
    spec = dict(data.get("task_spec") or {})
    receptor = dict(spec.get("receptor") or {})
    receptor.update({
        "name": receptor.get("name") or source,
        "source": "unresolved",
        "resolution": {"status": "input_invalid", "reason": reason,
                       "requested": source, "message": message},
    })
    spec["receptor"] = receptor
    data["task_spec"] = spec
    try:
        run.log(f"受体输入不可用（{reason}）：已阻断后续计算，等待用户确认 —— {message[:120]}")
    except Exception:  # noqa: BLE001
        logger.debug("写运行日志失败", exc_info=True)


def receptor_input_problem(reason: str, source: str, message: str,
                           options: Sequence[str] = (), runtime: Any = None) -> str:
    """「受体输入不可用」的统一回答：`needs_user_input` + 阻断本次运行的后续计算。

    产品底线：计算对象不可用时**绝不计算**，也绝不改用任何预置受体。工具层
    （对接 / 口袋）与归一化失败都走这里，保证用户拿到的是**同一种**回答：
    说清原因、给出可选项、把选择权交回用户。
    """
    mark_receptor_input_invalid(source, reason, message, runtime=runtime)
    return json.dumps({
        "status": "needs_user_input",
        "missing": ["receptor"],
        "reason": reason,
        "receptor": source,
        "options": list(options or []),
        "message": message + " 在用户给出可用的受体之前，不要调用 run_docking / "
                             "molecular_docking / run_pocket_analysis 重试。",
    }, ensure_ascii=False)


def receptor_input_guard(exc: Any, runtime: Any = None) -> str:
    """把 `ReceptorInputError` 转成给用户看的回答（见 `receptor_input_problem`）。"""
    payload = getattr(exc, "payload", None) or {}
    return receptor_input_problem(str(getattr(exc, "reason", "") or "input_invalid"),
                                  str(getattr(exc, "source", "") or ""), str(exc),
                                  options=payload.get("options") or (), runtime=runtime)


#: 给**模型**看的说明：选项已经通过界面下发，模型不要在正文里再列一遍。
#: 真实反馈：同一批选项既出现在主管 Agent 的 A/B/C/D 表格里，又出现在界面的可点按钮上，
#: 用户以为系统问了两遍（重复列选项还白占上下文）。
CHOICES_PROSE_RULE = (
    "选项已通过界面下发给用户（可直接点选）。**不要在回复里重复列出选项内容、SMILES、"
    "候选清单或参数**；只用一两句话说明「为什么必须由用户决定」并请用户在界面上点选。")


def choices_payload(choices: List[Dict[str, Any]], *, message: str, kind: str = ""
                    ) -> Dict[str, Any]:
    """构造给**模型**的「等待用户点选」载荷：只给原因与数量，**不给选项明细**。

    选项明细走 `run.data["choices"]` → SSE → 前端按钮（见 `publish_choices`），
    模型不需要也不应该复述它们（复述就会造成「同一问题出现两次」）。
    """
    first = choices[0] if choices else {}
    return {"status": "needs_user_input",
            "message": f"{message} {CHOICES_PROSE_RULE}",
            "choices_published": {"kind": kind or str(first.get("kind") or ""),
                                  "count": len(choices)}}


def publish_choices(kind: str, choices: List[Dict[str, Any]], note: str = "",
                    runtime: Any = None) -> None:
    """把「候选选择项」写进本次运行数据，供 SSE / 运行详情下发给前端点选。

    结构化选择通道（而不是只把候选写进正文段落）：前端在聊天气泡下方渲染成按钮，
    点选后以**同一 conversation_id** 追问一句等价的 `prompt`，从而延续同一段对话继续跑。
    """
    run = active_run(runtime)
    if run is None:
        return
    if not choices:
        run.data.pop("choices", None)
        run.data.pop("choices_note", None)
        run.data.pop("_choices_signatures", None)
        return
    normalized = [{**(c or {}), "kind": (c or {}).get("kind") or kind} for c in choices]
    # 同一个问题在一次运行里**只认第一次发布**（同 kind + 同 id 序列即同一个问题）：
    # 重复发布既不该记日志，也不该改写已下发的候选 —— 真实反馈：「配体选择问了两次」，
    # 且界面上的第二份会**覆盖**第一份，用户点了先出现的那份就与后端当前候选错位。
    # 触发场景：同一工具被两种写法各调用一次（「代森锰锌」/「Mancozeb」），解析到同一 CID，
    # 选项 id 相同但 prompt 里带不同写法 → 旧实现会改写 `choices` 并再发一次 SSE。
    signature = (kind, tuple(str(c.get("id") or "") for c in normalized))
    published = run.data.setdefault("_choices_signatures", [])
    if signature in published:
        return
    published.append(signature)
    run.data["choices"] = normalized
    if note:
        run.data["choices_note"] = note
    try:
        run.log(f"已生成 {len(choices)} 个可选项（kind={kind}）：等待用户在界面上选择")
    except Exception:  # noqa: BLE001
        logger.debug("写运行日志失败", exc_info=True)


def clear_choices(kind: str = "",
                  runtime: Any = None) -> None:
    """解析成功后清掉**同类型**的陈旧候选，避免误显示上一轮的 choices。

    只清 `kind` 指定的那一类（receptor / molecule）：同一次运行里「分子是混合物要选、
    受体已高置信解析」是完全正常的组合，受体的成功不能把分子的可选项一起抹掉。
    """
    run = active_run(runtime)
    if run is None:
        return
    current = run.data.get("choices") or []
    # 被清掉的那一类同时撤销「已发布」标记：同一问题若在本次运行里**再次真的需要**问，
    # 必须能重新下发（否则会被幂等规则静默吞掉）。
    signatures = run.data.get("_choices_signatures") or []
    run.data["_choices_signatures"] = [
        s for s in signatures
        if not (isinstance(s, (list, tuple)) and s and (not kind or s[0] == kind))]
    if not current:
        return
    kept = [c for c in current if not kind or c.get("kind") != kind]
    if kept:
        run.data["choices"] = kept
        return
    run.data.pop("choices", None)
    run.data.pop("choices_note", None)


def resolution_failure_json(src: str, resolution: Dict[str, Any]) -> str:
    """解析失败/歧义时的可操作返回：逐条列出已尝试检索 + 找到的候选。"""
    attempts = summarize_attempts(resolution.get("attempts") or [])
    briefs = [candidate_brief(c) for c in (resolution.get("candidates") or [])]
    if resolution.get("status") == "ambiguous":
        lines = []
        for c in briefs[:6]:
            pdb = "/".join(c.get("pdb_ids") or [])
            source = f"RCSB {pdb}" if pdb else "AlphaFold 预测"
            lines.append(f"{c.get('accession')} | {c.get('organism')} | {c.get('protein')} | "
                         f"结构来源：{source} | 打分 {c.get('score')} | 基因 "
                         f"{'/'.join(c.get('gene_names') or []) or '—'}")
        return json.dumps({
            "status": "ambiguous", "requested": src,
            "attempts": attempts, "candidates": briefs,
            "message": ("检索到多个同样合理的候选，物种/上下文不足以消歧 —— "
                        "**不要自行挑选、不要对接**，请把下列候选列给用户选择：\n"
                        + "\n".join(lines) + "\n用户选定后可再用 accession 直接获取结构。"),
        }, ensure_ascii=False)
    return json.dumps({
        "status": "error", "reason": "not_found", "requested": src,
        "attempts": attempts, "candidates": briefs,
        "message": ("在线数据库未找到与该名称匹配的蛋白。已尝试的检索："
                    + "；".join(attempts)
                    + "。请让用户三选一：① 提供 PDB 编号（如 1DWC）；"
                      "② 提供 UniProt accession（如 Q9SJQ6）；"
                      "③ 上传受体结构文件（.pdb/.ent/.cif/.pdbqt）。"
                      "**系统没有默认受体，也不提供预置受体选项** —— 用户指定之前不要调用任何计算工具。"),
    }, ensure_ascii=False)


def mixture_choices(comp: Dict[str, Any], query: str) -> List[Dict[str, Any]]:
    """为多组分/配位聚合物生成「代表结构怎么取」的可选项（片段重组，不臆造新化学）。

    选项值都是 PubChem 原始 SMILES 里**真实存在**的片段组合：
      ① 原始多组分（Zn/Mn-EBDC 聚合物，如实保留全部片段）；
      ② 每种金属各一条 M-EBDC 单体（金属 + 最大有机片段）；
      ③ 最大有机片段本身（无金属代表结构）。
    """
    name = str(comp.get("name") or query)
    # 追问里的名称优先用用户原始写法（中文名不会被 RDKit 误当成 SMILES 而丢掉）
    display = str(query or name)
    cid = comp.get("cid")
    raw = str(comp.get("smiles") or "")
    main = str(comp.get("representative_smiles") or "")
    components = comp.get("components") or []
    label_suffix = f"（CID {cid}）" if cid else ""
    choices: List[Dict[str, Any]] = []
    if raw:
        choices.append({
            "id": f"molecule:{cid or query}:raw", "kind": "molecule",
            "label": f"{name}{label_suffix} · PubChem 原始多组分结构（{comp.get('formula') or '多片段'}）",
            "value": raw,
            "prompt": f"{display} {raw} （按 PubChem 原始多组分结构继续对接，金属与配体片段原样保留）",
            "detail": {"cid": cid, "mode": "raw-mixture", "formula": comp.get("formula"),
                       "note": "对接引擎通常不支持金属配位，实际分数需谨慎解读"},
        })
    for item in components:
        smi = str(item.get("smiles") or "")
        role = str(item.get("role") or "")
        if not smi or smi == main or "反离子" not in role:
            continue
        metal_hits = re.findall(r"[A-Z][a-z]?", smi)
        metal = metal_hits[0] if metal_hits else "M"
        monomer = f"{main}.{smi}"
        choices.append({
            "id": f"molecule:{cid or query}:{metal}", "kind": "molecule",
            "label": f"{name} · {metal}-EBDC 单体（金属 {smi}，可能被对接引擎剥离）",
            "value": monomer,
            "prompt": f"{display}-{metal} {monomer} （按 {metal}-EBDC 单体继续对接）",
            "detail": {"cid": cid, "mode": "metal-monomer", "metal": smi,
                       "note": "Vina/AD4 对金属配位支持有限，分数不可直接与外面对接结果比较"},
        })
    if main:
        choices.append({
            "id": f"molecule:{cid or query}:organic", "kind": "molecule",
            "label": f"{name} · 最大有机片段 EBDC（无金属代表结构）",
            "value": main,
            "prompt": f"{display}-EBDC {main} （按最大有机片段/无金属代表结构继续对接）",
            "detail": {"cid": cid, "mode": "organic-fragment",
                       "note": "只对有机配体片段建模，忽略 Zn/Mn 配位"},
        })
    return choices

def _ligand_smiles_from_candidates(ligand: Dict[str, Any], paths: List[str]) -> Tuple[str, List[str]]:
    """按候选路径依次尝试解出共晶配体 SMILES，返回 (smiles, 尝试过的路径)。

    为什么需要多个来源：对接用的受体是**已去配体**的准备结构（蛋白-only），
    配体原子在那里已经不存在；原始结构（上传文件 / 在线解析缓存）里才有。
    """
    from docking_agent.core.pockets import cocrystal_ligand_smiles  # noqa: PLC0415

    tried: List[str] = []
    for path in paths:
        if not path:
            continue
        tried.append(path)
        smiles = cocrystal_ligand_smiles(path, ligand)
        if smiles:
            return smiles, tried
    return "", tried


def _ligand_candidate_paths(block: Dict[str, Any], runtime: Any = None) -> List[str]:
    """共晶配体的候选结构来源（按可信度）：原始结构 → 本次对接所用结构 → 请求里的受体文件 → 缓存。

    为什么必须带**原始结构**：对接用的是已去配体的 PDBQT，配体原子在那里根本不存在。
    真实缺陷（用户实测）：受体先被口袋 Agent 准备成 `xxx_ph7.4.pdbqt`，`cocrystal_ligand`
    只剩残基名、有没有原始 PDB 路径都没带出去 → 候选列表为空 → 解不出 SMILES →
    「是否把受体自带配体当阳性对照」的询问**永远不会触发**。
    """
    b = block or {}
    candidates: List[str] = [str(b.get("source_pdb") or ""), str(b.get("receptor_pdb") or "")]
    run = active_run(runtime)
    request = (getattr(run, "data", None) or {}).get("request") or {}
    for key in ("receptor_file", "receptor"):
        value = str(request.get(key) or "").strip()
        if value and not value.startswith(("http://", "https://")) and os.path.isfile(value):
            candidates.append(value)
    # 标签/键里若带 4 位 PDB 编号（如 7YHP，也可能是 `7YHP_<hash>_ph7.4` 这种带后缀），
    # 在线解析的原始结构通常在 assets 缓存里。注意：不能用 `\b` —— 下划线也是词字符，
    # `7YHP_xxx` 里根本切不出边界（这正是缺陷之一）。
    label = " ".join(str(b.get(k) or "") for k in ("receptor", "receptor_key", "name", "label", "key"))
    for token in re.findall(r"(?<![A-Za-z0-9])([0-9][A-Za-z0-9]{3})(?![A-Za-z0-9])", label):
        for cache in (project_root() / "assets" / "cache",
                      project_root() / "assets" / "receptor" / "cache"):
            cached = cache / f"{token.upper()}.pdb"
            if cached.is_file():
                candidates.append(str(cached))
    # 去重、保持顺序。**不过滤不存在的路径**：日志会把这些路径原样列出来 ——
    # 「记录了一个路径但它不存在」正是这次排查的关键线索（`receptor_pdb` 曾只是一个文件名）。
    seen: set = set()
    out: List[str] = []
    for path in candidates:
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def offer_cocrystal_positive_control(blocks: List[Dict[str, Any]], *,
                                     specified_control: str = "",
                                     runtime: Any = None) -> List[Dict[str, Any]]:
    """受体自带共晶配体、且用户没给阳性对照时，询问是否把它当作对照（**不阻塞**筛选）。

    阳性对照只是方法学基线，缺了不影响候选分子的对接结果，因此这里只"询问"，
    让筛选照常出结果；用户的点选会以同一会话发起新一轮并带上对照。
    解不出 SMILES 时不询问（并在运行日志里说明**试过哪些结构**），绝不拿不确定的结构当对照。
    """
    if str(specified_control or "").strip():
        return []                                   # 用户/上游已指定对照：不打扰
    run = active_run(runtime)
    data = getattr(run, "data", None) if run is not None else None
    # 一次运行**只判定一次**，且必须在对接开始前判定（函数名与文档承诺就是「对接前询问」）。
    # 真实缺陷：同一次运行里 run_docking 可能被调用多次（重试/分阶段），早期调用因受体结构
    # 还没准备好而解不出 SMILES（日志写「因此未询问」），后期调用却能解出并发布选项 ——
    # 用户会在对接都快跑完时突然被问一次，且与之前的「不问」自相矛盾。
    if isinstance(data, dict):
        if data.get("cocrystal_check_done"):
            return []
        data["cocrystal_check_done"] = True
    for block in blocks or []:
        ligand = (block or {}).get("cocrystal_ligand") or {}
        resname = str(ligand.get("resname") or "")
        if not resname:
            continue
        smiles, tried = _ligand_smiles_from_candidates(
            ligand, _ligand_candidate_paths(block, runtime))
        parts = [resname]
        if ligand.get("key"):
            parts.append(str(ligand["key"]))
        if ligand.get("n_atoms"):
            parts.append(f"{ligand['n_atoms']} 原子")
        label = "（".join([parts[0], "，".join(parts[1:]) + "）"]) if len(parts) > 1 else parts[0]
        if not smiles:
            try:
                if run is not None:
                    run.log(f"检测到共晶配体 {label}，但在 "
                            f"{'、'.join(tried) or '（无可读结构）'} 中都解不出 SMILES，"
                            "因此未询问是否用作阳性对照")
            except Exception:  # noqa: BLE001 - 记录失败不影响对接
                logger.debug("写共晶配体说明失败", exc_info=True)
            return []
        receptor = str((block or {}).get("receptor") or "")
        choices = [
            {"id": f"positive_control:{ligand.get('key') or resname}", "kind": "positive_control",
             "label": f"把共晶配体 {label} 作为阳性对照，做结合模式对比".replace("） 作为", "）作为"),
             "value": smiles,
             "detail": {"resname": resname, "key": ligand.get("key"),
                        "n_atoms": ligand.get("n_atoms"), "smiles": smiles,
                        "note": "共晶配体来自受体结构本身，是天然的方法学基线"},
             "prompt": (f"把受体 {receptor or ''} 自带的共晶配体（{label}，SMILES {smiles}）"
                        f"作为阳性对照，重新完成对接与结合模式对比分析")},
            {"id": "positive_control:none", "kind": "positive_control",
             "label": "不使用阳性对照，只做候选分子筛选",
             "value": "",
             "detail": {"note": "报告会说明本次未做对照分析"},
             "prompt": "不使用阳性对照，直接完成候选分子筛选与报告（报告里注明未做对照分析）"},
        ]
        note = (f"受体结构自带共晶配体 {label}：是否把它作为阳性对照（结合模式基线）？"
                "选择后系统会以同一会话继续；不选也不影响本次候选分子的对接结果。")
        run = active_run(runtime)
        if run is not None:
            # 记录"问了什么"，报告可据此单列一行（用户/审稿人无需翻对话即可追溯）
            run.data["cocrystal_control_offer"] = {
                "resname": resname, "key": ligand.get("key"),
                "n_atoms": ligand.get("n_atoms"), "smiles": smiles, "label": label}
        publish_choices("positive_control", choices, note=note, runtime=runtime)
        return choices
    return []
