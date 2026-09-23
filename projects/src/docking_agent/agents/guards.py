"""对话/编排层的**前置护栏**：计算对象不明确时绝不计算。

从 `agents/dispatch.py` 拆出（该文件已接近 700 行上限）：这里只放"在任何计算工具被调用之前"
就要拦下的判断，返回可直接回给编排层的 JSON 字符串（空串 = 放行）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, List


logger = logging.getLogger(__name__)


def unresolved_receptor_message(run: Any = None) -> str:
    """受理层判定「受体未指定 / 点名但无法解析」时返回给编排层的 JSON；否则返回空串。

    产品底线：**计算对象不明确时绝不计算**。两种情形都在**任何对接/口袋计算被调用之前**拦下，
    返回 `needs_user_input` 并请用户补充：
      · `unresolved` —— 用户点名了受体但无法解析（真实缺陷：系统按「回退默认受体」继续对接，
        计算对象被悄悄换成凝血酶，用户拿到的是答非所问的结果）；
      · `default` —— 用户**根本没指定受体**。预置受体仅供内部测试，系统**没有**默认受体，
        更不允许替用户挑一个靶点开跑。

    `unresolved` 何时产生：不再由受理层预先判定，而是由
    `tools/online.py::fetch_protein_structure` 在**真正在线检索过**之后写回
    （`_mark_receptor_unresolved`）—— 也就是说，走到这里的每个 `unresolved` 都带着
    真实的已尝试检索与候选清单。本护栏的判定逻辑与对外契约保持不变（只多回传这些证据）。
    """
    spec = (getattr(run, "data", None) or {}).get("task_spec") or {}
    receptor = spec.get("receptor") or {}
    source = str(receptor.get("source") or "")
    if source == "default":
        # 没有受体 = 没有计算对象。**不给任何预置受体候选**（它们只用于内部测试）。
        return json.dumps({
            "status": "needs_user_input",
            "missing": ["receptor"],
            "message": (
                "受理层判定：用户**没有指定受体**（系统没有默认受体，也不会替用户挑靶点）。"
                "**本次不执行任何计算**。请让用户三选一："
                "① 提供 PDB 编号或 UniProt accession；"
                "② 写出受体的基因名/蛋白名（中英文均可，系统会去在线数据库检索）；"
                "③ 上传受体结构文件（.pdb/.cif/.pdbqt）。"
                "在用户明确答复前，不要调用 run_pocket_analysis / run_docking。"
            ),
        }, ensure_ascii=False)
    if source != "unresolved":
        return ""
    name = str(receptor.get("name") or "用户点名的受体")
    resolution = receptor.get("resolution") or {}
    if str(resolution.get("status") or "") == "input_invalid":
        # 用户**给了**受体，但那份输入不可用（文件准备失败 / 名字认不出）：
        # 措辞要给出真实原因与可选项，不能套用「你没给受体」那套说法。
        detail = str(resolution.get("message") or resolution.get("reason") or "").strip()
        return json.dumps({
            "status": "needs_user_input",
            "missing": ["receptor"],
            "receptor": name,
            "reason": str(resolution.get("reason") or "input_invalid"),
            "message": (
                f"受理层判定：用户提供的受体「{name}」**无法用于计算**。{detail}"
                "**本次不执行任何计算**，也没有改用任何预置受体。"
                "在用户给出可用的受体之前，不要调用 run_pocket_analysis / run_docking / "
                "molecular_docking 重试。"
            ),
        }, ensure_ascii=False)
    attempts = resolution.get("attempts") or []
    candidates = resolution.get("candidates") or []
    evidence = ""
    if attempts:
        lines = []
        for item in attempts:
            if isinstance(item, dict):
                lines.append(f"[{item.get('strategy')}] {item.get('query')} → "
                             f"{item.get('hits', 0)} 条命中")
            else:
                lines.append(str(item))
        evidence += " 已尝试的检索：" + "；".join(lines) + "。"
    if candidates:
        rows = []
        for c in candidates[:5]:
            if not isinstance(c, dict):
                continue
            pdbs = c.get("pdb_ids") or []
            rows.append(f"{c.get('accession')}（{c.get('organism')}，{c.get('protein')}，"
                        f"结构来源 {'RCSB ' + '/'.join(pdbs[:2]) if pdbs else 'AlphaFold 预测'}，"
                        f"打分 {c.get('score')}）")
        if rows:
            evidence += " 找到的候选：" + "；".join(rows) + " —— 请让用户从中选择。"
    return json.dumps({
        "status": "needs_user_input",
        "receptor": name,
        "attempts": attempts,
        "candidates": candidates,
        "message": (
            f"受理层判定：用户点名了受体「{name}」，但无法解析"
            "（不是 PDB 号、不是 UniProt accession、不是可检索的基因/蛋白名称，"
            "也没有上传受体文件）。"
            + evidence +
            "**本次不执行任何对接计算**，也不得擅自改用系统默认受体。"
            "请让用户三选一：① 提供 PDB 编号或 UniProt accession；"
            "② 写出受体的基因名/蛋白名（中英文均可，系统会在线检索）；"
            "③ 上传受体结构文件（.pdb/.ent/.cif/.pdbqt）。"
            "在用户明确答复前，不要调用 run_docking / molecular_docking。"
        ),
    }, ensure_ascii=False)

# 各子 Agent 返回 JSON 必须包含的关键字段（用于分发边界校验）
# 子 Agent 返回 JSON 的必填字段。注意大库时工具只回传摘要（明细在产物里），
# 因此这里接受「完整结构」与「摘要契约」两种形态。


def docking_readiness(run: Any = None, *, receptor_file: str = "", receptor_sources: str = "",
                      site_center: Any = None, site_size: Any = None) -> List[str]:
    """对接的**前置条件**：缺什么就返回什么（空列表 = 可以开跑）。

    这是**工具层的规则**，不是提示词里的叮嘱 —— 用户反复要求"先确定信息再来"，靠模型自觉
    不可靠；对接工具在任何计算之前调用它，缺条件就直接把"缺什么"交回去（不计算）。
    条件（顺序即补齐顺序）：
      1. 受体：上传文件 / PDB / UniProt / 名称任一来源已给出（受理层 unresolved 另有更细的护栏）；
      2. 配体库：请求里带了分子库（文件或清单）；实际条数由对接层再校验；
      2. 阳性对照：受体自带共晶配体时，用户必须已决定用/不用（否则下发候选并等点选）。
      位点与配体库**不是**硬前置：位点由口袋分析/对接层现场定盒；配体可能来自
      `ligands_text`、黑板或导入工具，空库由对接层自己报 `no_molecules`。
    """
    if run is None:
        return []          # 没有运行上下文（CLI/单测直调）：不做前置检查，由调用方自行保证
    missing: List[str] = []
    data = getattr(run, "data", None) if run is not None else None
    data = data if isinstance(data, dict) else {}
    request = data.get("request") or {}
    spec = data.get("task_spec") or {}
    receptor = spec.get("receptor") or {}

    if not (str(receptor_file or request.get("receptor_file") or "").strip()
            or str(receptor_sources or request.get("receptor_sources") or "").strip()
            or str(receptor.get("file") or "").strip()
            or str(receptor.get("name") or "").strip()):
        missing.append("receptor")

    # 阳性对照：只看**这一次运行**自己的状态（不查 ContextVar，便于直接传 run 断言）
    if str(data.get("choices_blocking") or "") == "positive_control" and data.get("choices"):
        missing.append("positive_control")
    return missing


def readiness_payload(run: Any = None, **kwargs: Any) -> str:
    """把缺失条件转成给模型的简短说明（缺什么、下一步做什么）。"""
    missing = docking_readiness(run, **kwargs)
    if not missing:
        return ""
    next_step = {
        "receptor": "请用户给出受体（上传文件 / PDB 编号 / UniProt / 基因或蛋白名）",
        "positive_control": "已把「共晶配体是否作阳性对照」的选项发给用户，等他点选",
    }
    todo = "；".join(next_step[k] for k in missing if k in next_step)
    return json.dumps({
        # 只要其中包含"需要用户决定"的一项（阳性对照），状态就是 needs_user_input：
        # 界面据此显示"等待用户选择"，而不是把用户甩到"前置条件缺失"的报错上
        "status": ("needs_user_input" if "positive_control" in missing
                   else "precondition_missing"),
        "missing": missing,
        "message": f"对接前置条件未满足（缺 {'、'.join(missing)}）：{todo}。在补齐之前不会开始任何对接计算。",
    }, ensure_ascii=False)
