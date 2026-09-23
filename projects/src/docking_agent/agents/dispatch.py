"""整体协调 Agent 的分发工具：分子库导入 + 向 4 个子 Agent 调度任务。

**模块位置**：本模块属 `agents/` 层（协调 Agent 的工具集，内部调用子 Agent），
原先在 `tools/` 下 —— 那会让 `agents/dispatch.py` 反向 import `agents.workers`，
把 `agents.workers ⇄ agents.dispatch` 依赖环做实（审计 SCC-3）。

每个工具内部通过调用真实子 Agent（create_agent 实例）完成任务，
实现多 Agent 协作。遵守"@tool 内部不调用 @tool"约束——这里调用的是 Agent，而非 @tool。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Literal, Optional, Sequence

from langchain.tools import tool
from docking_agent.runtime.context import AgentContext, active_blackboard, active_run
from docking_agent.tools.schemas import CoordArray, floats_to_text
from docking_agent.tools.molecule_paths import looks_like_molecule_path, resolve_molecule_file
from docking_agent.paths import libraries_dir

from docking_agent.runtime.payload import parse_json_object
from docking_agent.core import (
    parse_smiles_text, load_library_file, read_molecule_file_normalized,
)
from docking_agent.core.params import resolve_engine, resolve_exhaustiveness
from docking_agent.runtime import tool_io
from docking_agent.agents.workers import (
    invoke_worker,
    get_property_agent,
    get_pocket_agent,
    get_docking_agent,
    get_binding_agent,
)
from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)

#: 下发给子 Agent 的**结构化参数块**标记（协调层 ↔ 子 Agent 的唯一参数契约）。
#:
#: 为什么把参数从散文里挪出来：旧消息靠 `exhaustiveness=16, n_poses=1`、
#: `site_center=[…]` 这类散文传递参数，一旦改标点就会静默失配 ——
#: 真实模型"照指令传参"会跟着错，测试也只能用正则反解散文
#: （`tests/support/fake_llm.py` 曾经就是如此，任何措辞微调都会先打坏测试）。
#: 现在参数只有一种形态：**JSON 的键就是工具参数名**，散文只负责解释与告警。
AGENT_PARAMS_MARKER = "任务参数(JSON)："


def _agent_task_message(instruction: str, params: Dict[str, Any],
                        notes: Sequence[str] = ()) -> str:
    """拼一条下发给子 Agent 的指令：自然语言说明 + 注意事项 + 结构化参数 JSON。

    `params` 里为 `None`/空串/空列表的键会被剔除（"未指定"就是不出现，
    子 Agent 侧按"留空即读共享黑板"的既有约定处理）。
    """
    clean = {k: v for k, v in params.items() if v not in (None, "", [], {})}
    parts = [instruction.strip()]
    if notes:
        parts.append("注意事项：\n" + "\n".join(f"- {n}" for n in notes if n))
    parts.append(AGENT_PARAMS_MARKER + json.dumps(clean, ensure_ascii=False, sort_keys=True))
    return "\n\n".join(p for p in parts if p)


def _coords(value: Any) -> Optional[List[float]]:
    """把坐标参数统一成 float 列表（数组/逗号串都接受）；解析不出 3 个值时返回 None。"""
    text = floats_to_text(value, expect=3)
    if not text:
        return None
    try:
        return [float(x) for x in text.split(",")]
    except ValueError:  # pragma: no cover - floats_to_text 已经保证是数字
        return None


def _normalization_digest(normalization: Dict[str, Any]) -> Dict[str, Any]:
    """给模型的归一化摘要（有界）；完整明细落盘为 `input_normalization.json`。"""
    if not normalization:
        return {}
    return {
        "format": normalization.get("format"),
        "source_file": normalization.get("source_file"),
        "encoding": normalization.get("encoding"),
        "delimiter": normalization.get("delimiter"),
        "header": normalization.get("header"),
        "records_total": normalization.get("records_total"),
        "records_ok": normalization.get("records_ok"),
        "records_skipped": normalization.get("records_skipped"),
        "skipped": (normalization.get("skipped") or [])[:10],
        "duplicates_removed": normalization.get("duplicates_removed"),
        "notes": (normalization.get("notes") or [])[:4],
    }


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
_REQUIRED_KEYS = {
    "property": ("assessment",),
    "pocket": ("pockets",),
    "docking": ("receptors", "summary"),
    "binding": ("results",),
}
# 上面这些是「任一命中即可」的候选键
_ANY_OF_KEYS = {"docking"}

_CORRECTION = ("\n\n[系统纠正] 你上一次的回复无法解析为 JSON 对象，或缺少必要字段 {keys}。"
               "请**只输出一个 JSON 对象**（不要 Markdown 代码块、不要额外解释），"
               "并确保包含工具返回的关键字段。")


def _keys_ok(parsed: dict, required: tuple, role: str) -> bool:
    """校验子 Agent 返回是否含必要字段；docking 允许「完整 receptors」或「摘要 summary」。"""
    if role in _ANY_OF_KEYS:
        return any(k in parsed for k in required)
    return all(k in parsed for k in required)


def _invoke_checked(agent, message: str, thread_id: str, role: str,
                    runtime: Any = None) -> str:
    """调用子 Agent 并**校验返回**：解析失败或缺少关键字段时带纠正提示重试一次。

    两次都失败则返回显式的 `agent_output_invalid` 状态（而不是把垃圾文本丢给协调 Agent），
    由协调 Agent 决定重试或如实上报——这是「Agent 级」的失败处理，比运行级兜底更细。
    """
    required = _REQUIRED_KEYS.get(role, ())
    attempts = [(message, thread_id), (message + _CORRECTION.format(keys=list(required)),
                                       f"{thread_id}-retry")]
    last_raw = ""
    for index, (msg, tid) in enumerate(attempts):
        raw = invoke_worker(agent, msg, tid)
        parsed = parse_json_object(raw)
        if parsed is not None and _keys_ok(parsed, required, role):
            if index > 0:
                logger.info("%s 子 Agent 重试后返回合法 JSON", role)
                board = active_blackboard(runtime)
                if board is not None:
                    board.add_note(f"{role} 子 Agent：首次返回不合法 JSON，重试后成功")
            return json.dumps(parsed, ensure_ascii=False)
        last_raw = raw or ""
        logger.warning("%s 子 Agent 返回无法解析（第 %s 次）：%s", role, index + 1, last_raw[:200])

    return json.dumps({"status": "agent_output_invalid", "role": role,
                       "required_keys": list(required), "raw": last_raw[:800],
                       "message": f"{role} 子 Agent 返回的内容无法解析为包含 {list(required)} 的 JSON，"
                                  "已重试一次仍失败"}, ensure_ascii=False)

DEFAULT_LIBRARY = str(libraries_dir() / "mol_library.csv")
DEFAULT_POSITIVE_CONTROL = str(libraries_dir() / "positive_control.csv")


def _load_positive_control() -> str:
    try:
        mols = load_library_file(DEFAULT_POSITIVE_CONTROL)
        if mols:
            return mols[0]["smiles"]
    except Exception as e:
        logger.warning("阳性对照读取失败: %s", e)
    return "NC(=N)c1ccccc1"  # 苯甲脒，凝血酶活性位点经典探针


@tool
def import_molecule_library(query_or_text: str = "", molecule_file: str = "",
                            allow_example_fallback: bool = False, runtime: ToolRuntime[AgentContext] = None) -> str:
    """导入起始分子库：从用户输入文本/JSON，或用户上传的小分子文件解析分子清单。

    参数 query_or_text 为原始文本（可能包含 SMILES 列表或 JSON 数组）。
        **若它其实是文件路径**（存在的文件、绝对/相对路径、或 .sdf/.smi/.csv/.mol2 后缀），
        也会自动按文件读取 —— 模型把路径塞错参数时不会静默失败。
    参数 molecule_file 为上传/提供的小分子文件，支持 SDF/SMILES/.smi/.csv/.mol2（本地路径或 URL）；
        提供后优先从文件读取。**裸文件名会自动在上传/缓存目录里解析**
        （`PGR.sdf` → `<时间戳>-<哈希>-PGR.sdf`），不要求模型写出完整路径。
    参数 allow_example_fallback：仅当用户明确要求"使用示例分子库/示例库/演示"时才传 True，
        用于触发回退到内置示例分子库；默认 False。

    返回 JSON：
    - 检测到分子 -> {"status":"ok","source":"file"/"input"/"input-json"/"example-library","molecules":[...]}
    - 未检测到任何有效分子 -> {"status":"no_molecules","message":"...","attempted":[...],"molecules":[]}
      `attempted` 逐条列出真实尝试过的路径与失败原因（便于如实上报，而不是让模型猜路径）。
    """
    molecules = []
    source = "input"
    attempted: List[str] = []
    ambiguous: List[str] = []
    normalization: Dict[str, Any] = {}
    # ① 归一化「文件来源」：molecule_file 优先；模型也可能把路径塞进 query_or_text。
    #    真实缺陷 20260917-112206-5017：只给显示名 `PGR.sdf`，工具按裸文件名解析失败。
    file_arg = (molecule_file or "").strip()
    text = (query_or_text or "").strip()
    if not file_arg and looks_like_molecule_path(text):
        file_arg, text = text, ""

    def _try_file(label: str, value: str) -> List[dict]:
        """按文件读取 value；成功且解析出分子返回清单，否则把原因记进 attempted。

        使用**统一输入归一化层**（内容嗅探 + 异构表头/编码 + gzip/zip + 逐行容错），
        并把 normalization 摘要落盘 `input_normalization.json`。
        """
        nonlocal ambiguous, normalization
        resolved, attempts, candidates = resolve_molecule_file(value)
        attempted.extend(attempts)
        if candidates:
            # 歧义：多个候选命中同一后缀 → **不猜**，回传候选清单让上层报错/提问
            ambiguous = candidates
            attempted.append(f"{label}={value}：匹配到 {len(candidates)} 个候选文件，"
                             "无法确定用哪一个，请用户明确指定完整路径")
            return []
        if not resolved.startswith(("http://", "https://")) and not os.path.isfile(resolved):
            attempted.append(f"{label}={value}：未能解析为本地文件（已尝试 {len(attempts) or 1} 个候选路径）")
            return []
        try:
            fmt, mols, norm = read_molecule_file_normalized(resolved)
        except Exception as e:  # noqa: BLE001
            attempted.append(f"{label}={value}（解析为 {resolved}）：读取失败（{e}）")
            return []
        if norm:
            normalization = dict(norm)
            normalization["resolved_path"] = resolved
        if mols:
            logger.info("分子库导入成功：%s → %s（格式=%s，%d 个分子）", label, resolved, fmt, len(mols))
            return mols
        attempted.append(f"{label}={value}（解析为 {resolved}）：文件已读取，但未解析出任何分子"
                         "（格式不受支持或内容为空）")
        return []

    if file_arg:
        molecules = _try_file("molecule_file", file_arg)
        if molecules:
            source = "file"
    # ② 文本 / JSON 数组（也走归一化层，兼容 名称:SMILES / 名称 SMILES / JSON）
    if not molecules and text:
        try:
            arr = json.loads(text)
            if isinstance(arr, list):
                molecules = [{"id": m.get("name") or f"MOL{i+1}",
                              "name": m.get("name") or f"MOL{i+1}", "smiles": m["smiles"]}
                             for i, m in enumerate(arr) if m.get("smiles")]
                source = "input-json"
        except Exception as e:  # noqa: BLE001
            logger.debug("molecules_json 不是合法 JSON（按纯文本处理）：%s", e)
    if not molecules and text:
        try:
            from docking_agent.core.normalize import normalize_ligand_text

            molecules, normalization = normalize_ligand_text(text)
            normalization["source_file"] = ""
            if molecules:
                source = "input"
        except Exception as e:  # noqa: BLE001
            logger.debug("归一化文本解析失败，回退旧解析器：%s", e)
            parsed = parse_smiles_text(text)
            if parsed:
                molecules = parsed
    # ③ 兜底：本次运行请求里本就带着上传的分子库文件（对话附件）→ 直接用它。
    #    「上传成功 = 对话里一定能用」：不依赖模型是否把附件路径抄进参数。
    if not molecules:
        run = active_run(runtime)
        request = (getattr(run, "data", None) or {}).get("request") or {}
        request_file = str(request.get("molecule_file") or "").strip()
        if request_file and request_file != file_arg:
            molecules = _try_file("请求携带的 molecule_file", request_file)
            if molecules:
                source = "file"
    if not molecules:
        # 仅在用户明确要求示例库时才回退；否则如实报告缺失，交由协调 Agent 决定
        if allow_example_fallback:
            molecules = load_library_file(DEFAULT_LIBRARY)
            source = "example-library"
        else:
            message = ("未从输入/上传文件中解析到任何有效分子（SMILES），尚不能执行分子筛选。"
                       "请先提供候选分子库（SMILES/名称列表）或上传小分子文件（SDF/SMILES/CSV）。")
            if attempted:
                message += " 已尝试的路径与失败原因：" + "；".join(attempted[:12])
            payload: Dict[str, Any] = {
                "status": "no_molecules",
                "message": message,
                "attempted": attempted,
                "molecules": [],
            }
            if ambiguous:
                payload["candidates"] = ambiguous
                payload["needs_user_input"] = True
            if normalization:
                payload["input_normalization"] = _normalization_digest(normalization)
            return json.dumps(payload, ensure_ascii=False)
    # 归一化溯源落盘（每次输入都并入 input_normalization.json）
    if normalization:
        from docking_agent.core.normalize import record_input_normalization

        record_input_normalization(normalization, kind="ligand")
    # 完整清单落盘 + 小库照旧回全量；大库只回摘要（清单本身也会撑爆上下文：
    # 1 万条 ≈ 0.5 MB ≈ 13 万 tokens）
    tool_io.record("molecules", molecules, run=active_run(runtime))
    # **写入共享黑板**：这样「子 Agent 工具留空参数即用黑板」的承诺才成立。
    # 真实缺陷：此前全仓只有属性评估子 Agent 自己的 normalize 工具会写黑板，导入口从不写，
    # 于是 `run_property_assessment` 按提示词留空 → 子 Agent 读到「黑板上无分子」→
    # 147 条库只有前 20 条被评估（报告出现数据缺口），且以 status=ok 悄悄通过。
    board = active_blackboard(runtime)
    if board is not None:
        added = board.add_molecules(molecules)
        board.add_note(f"分子库导入：{len(molecules)} 条（来源 {source}）已写入共享黑板"
                       f"（新增 {len(added)}）")
    else:
        logger.warning("导入分子库时没有共享黑板：子 Agent 留空分子参数将取不到清单"
                       "（本次运行只能靠显式传入 molecule_file）")
    digest = _normalization_digest(normalization)
    limit = tool_io.summary_limit()
    if len(molecules) > limit:
        names = [m.get("name") for m in molecules[:limit]]
        return json.dumps({
            "status": "ok", "source": source,
            "molecules_total": len(molecules),
            "molecules": molecules[:limit],
            "detail_omitted": True,
            "summary": {"count": len(molecules), "source": source,
                        "first_names": names[:10]},
            "input_normalization": digest,
            "artifacts": tool_io.artifact_refs(run=active_run(runtime)),
            "notice": tool_io.big_payload_notice("候选分子库", len(molecules), limit)
                      + " 完整清单已写入共享黑板：后续子 Agent 工具**留空分子参数**即可直接使用。",
        }, ensure_ascii=False)
    molecules_file = tool_io.artifact_path("molecules", run=active_run(runtime))
    payload = {"status": "ok", "source": source, "molecules": molecules,
               "molecules_file": molecules_file,
               "notice": (f"完整清单已落盘并写入共享黑板；**按文件交接优先**：后续子 Agent 可直接传 "
                          f"molecules_file={molecules_file or '（本次运行目录下的 molecules_tool.json）'}"
                          "（大库不必把清单搬进上下文）；留空分子参数也会用共享黑板。")}
    if digest:
        payload["input_normalization"] = digest
    return json.dumps(payload, ensure_ascii=False)


@tool
def run_property_assessment(molecules_json: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """将分子清单下发给「分子属性评估 Agent」执行真实物化性质与类药性评估。

    molecules_json: 可选。`[{"name":"M1","smiles":"..."}, ...]`；
        **建议留空** —— 留空时子 Agent 会直接使用共享黑板上的分子库，
        这样即使有上万条分子，也不必把清单搬进上下文。
        只有当你要评估的分子**不在**黑板上（例如临时新增的一组）时，才显式传入。

    返回子 Agent 的真实评估结果 JSON 原文（大库时含 summary 与明细产物路径）。
    """
    mol_file = tool_io.artifact_path("molecules", run=active_run(runtime))
    # 分子库是必需项：拿不到任何分子来源时**不执行计算**，先请用户给出分子
    # （旧行为是子 Agent 回退示例库/返回 status=ok 的空结果 —— 都是答非所问）。
    board = active_blackboard(runtime)
    if not (molecules_json or "").strip() and not mol_file \
            and not (board.molecules() if board is not None else None):
        return json.dumps({
            "status": "needs_user_input",
            "missing": ["ligands"],
            "message": ("未指定候选分子库：不能凭空做性质评估。请让用户给出分子来源 —— "
                        "① 输入分子名称或 SMILES；② 上传分子文件（.sdf/.smi/.csv/.mol2）；"
                        "③ 明确同意使用内置示例库。"),
        }, ensure_ascii=False)
    agent = get_property_agent()
    if (molecules_json or "").strip():
        instruction = "请评估以下分子的物化性质与类药性。"
    elif mol_file:
        instruction = ("请评估本次任务的**全部**候选分子（用任务参数里的 molecules_file 按文件交接，"
                       "不要先把清单读进上下文再抄一遍；它会通过统一归一化层读取全量分子，"
                       "漏传时回退共享黑板）。")
    else:
        instruction = ("请评估**共享黑板上**全部候选分子的物化性质与类药性"
                       "（清单留空时用 board_molecules_json 取，不要臆造分子）。")
    params = {
        "molecules_json": (molecules_json or "").strip() or None,
        "molecules_file": mol_file or None,
    }
    msg = _agent_task_message(instruction, params)
    return _invoke_checked(agent, msg, thread_id="property", role="property", runtime=runtime)


@tool
def run_pocket_analysis(receptor_file: str = "", receptor_sources: str = "",
                        pocket_engine: str = "", top_n: int = 8, runtime: ToolRuntime[AgentContext] = None) -> str:
    """将「受体结构」下发给「口袋分析 Agent」，用真实工具预测结合口袋并选定对接盒子。

    何时调用：**在 run_docking 之前**。如果用户没有显式给出位点盒（site_center/site_size），
    应先把受体交给口袋分析 Agent：它用 P2Rank（或内置几何法）预测口袋、与实验位点比对，
    然后通过共享黑板把选定的盒子提交给 Docking Agent —— Docking Agent 会自动使用它。

    receptor_file: 可选，用户上传/提供的蛋白质文件（.pdb/.ent/.pdb1/.cif/.mmcif/.pdbqt，本地路径或 URL）。
    receptor_sources: 可选，受体来源（PDB 编号 / UniProt accession / 基因或蛋白名 / 文件路径）；
        留空则用本次任务规约里的受体。**用户没指定受体时本工具直接返回 needs_user_input**，
        绝不回退任何内建/预置受体（它们只用于内部测试）。
    pocket_engine: 留空=按设置页面/环境变量（默认 auto：优先 P2Rank，不可用时回退内置几何法）；
        也可显式传 p2rank / geometric / known_site。
    top_n: 返回前 N 个候选口袋（默认 8）。

    返回子 Agent 的真实分析结果 JSON 原文：{status, engine, pockets:[{rank,score,center,extent,residues}],
    selected:{pocket_rank,center,size}, validation:{...}, agent_note}
    """
    guard = unresolved_receptor_message(active_run(runtime))
    if guard:
        logger.info("拒绝口袋分析：受理层判定受体未指定或无法解析")
        return guard
    agent = get_pocket_agent()
    msg = _agent_task_message(
        "请分析该受体的结合口袋并选定对接盒子：调用 predict_binding_pockets 预测口袋、"
        "compare_pocket_with_experiment 与实验位点比对，然后用 set_docking_site 提交你选定的盒子"
        "（reason 里写明引擎、口袋编号与一致性结论）。",
        {"receptor_file": (receptor_file or "").strip() or None,
         "receptor_sources": (receptor_sources or "").strip() or None,
         "pocket_engine": (pocket_engine or "").strip() or None,
         "top_n": int(top_n)},
        notes=["receptor_file / receptor_sources 都为空时，用本次任务规约里的受体。"])
    return _invoke_checked(agent, msg, thread_id="pocket", role="pocket", runtime=runtime)


@tool
def run_docking(molecules_json: str = "", molecule_file: str = "", molecules_file: str = "",
                receptor_file: str = "", receptor_sources: str = "",
                positive_control_smiles: str = "",
                site_center: CoordArray = None, site_size: CoordArray = None,
                exhaustiveness: int = 0, n_poses: int = 1,
                engine: Literal["", "auto", "vina", "autodock", "external"] = "",
                top_from_previous: int = 0,
                keep_hetatm: str = "",
                save_poses: bool = True, max_ligands: int = 0,
                runtime: ToolRuntime[AgentContext] = None) -> str:
    """把分子对接任务下发给「Docking 执行 Agent」（真实 Vina/AutoDock，按受体×配体全组合）。

    分子来源（留空即用共享黑板/运行产物，**大库不要搬进上下文**）：
      molecules_json / molecule_file（用户上传文件）/ molecules_file（**推荐**，产物绝对路径，同 molecule_file）。
    受体：receptor_file（用户上传结构，现场准备为 PDBQT）或 receptor_sources
      （PDB 号 / UniProt accession / 基因或蛋白名，或分号或 JSON 数组的多受体=蛋白质库）。
      **没指定受体时直接返回 needs_user_input**，绝不回退任何预置受体（只用于内部测试）。
    位点：site_center / site_size（数组 [x,y,z]，也接受 "31.5,13.74,24.36"）；
      留空 = 口袋分析 Agent 已提交的盒子（黑板）或由工具现场定盒。
    参数：exhaustiveness（**0=用受理层自动规划的运行级值**，显式给值才以你为准，同阶段必须一致）、
      n_poses、engine（留空=跟随设置页「对接引擎（默认）」；可显式 auto/vina/autodock/external）、
      save_poses、max_ligands（0=不限）。
    keep_hetatm：要保留的非水杂原子残基名（如 'ZN,HEM'）。留空=标准流程剔除水与杂原子，
      被剔除的残基会出现在结果的 dropped_hetatm/notes 里；金属酶/辅因子体系判断重要后用本参数重跑。
    positive_control_smiles：一般不必传（工具会自动取本次运行的阳性对照并一并对接）。

    返回子 Agent 的真实对接结果 JSON（按受体分组；大库时给 summary + top + 产物路径）。
    """
    # 文件交接别名：molecules_file 与 molecule_file 同义（产物路径与用户上传文件都是本地文件）
    molecule_file = (molecule_file or "").strip() or (molecules_file or "").strip()
    # 护栏：受理层判定「用户点名的受体无法解析」时**在调用任何对接引擎之前**返回，
    # 绝不回退默认受体继续算（这是产品底线；见 unresolved_receptor_message）。
    guard = unresolved_receptor_message(active_run(runtime))
    if guard:
        logger.info("拒绝对接：受理层判定用户点名的受体无法解析")
        return guard
    agent = get_docking_agent()
    notes: List[str] = []
    # 搜索强度：0/留空 = 未指定 → 用本次运行的自动规划值（没有规划值时才留给下游按设置页默认）。
    # 这里**必须**在 params 里如实区分「未指定」与「显式 16」，否则会静默退回 16（审计缺陷）。
    run = active_run(runtime)
    plan = (getattr(run, "data", None) or {}).get("param_plan") or {}
    planned_exh = resolve_exhaustiveness(exhaustiveness, plan)
    # 引擎：显式参数 > 本次运行请求（表单）> 设置页「对接引擎（默认）」。留空**不再**等价于
    # 硬编码 auto —— 否则用户在设置页选了 external/autodock 也会被工具默认值盖掉。
    engine = resolve_engine(engine, (getattr(run, "data", None) or {}).get("request") or {})
    try:
        explicit_exh = int(exhaustiveness or 0) > 0
    except (TypeError, ValueError):
        explicit_exh = False
    if planned_exh and explicit_exh:
        notes.append(f"你显式指定了搜索强度：exhaustiveness={planned_exh}"
                     "（同一阶段内所有分子必须一致；报告里要说明这次用了非规划值）。")
    elif planned_exh:
        origin = "用户表单/高级设置" if plan.get("source") == "user" else "受理层「自动规划」"
        notes.append(f"搜索强度来自{origin}：exhaustiveness={planned_exh}"
                     "（运行级/阶段级，同一阶段内所有分子必须一致，不要逐分子改动）。")
    else:
        notes.append("**本次没有搜索强度规划值**（未指定且规划不可用）：exhaustiveness 保持未指定，"
                     "由工具按设置页默认执行，不要自己编数字。")
    # 参数**只**通过结构化 JSON 下发（键 = molecular_docking 的参数名），散文只做解释与告警。
    params: Dict[str, Any] = {
        "molecules_json": (molecules_json or "").strip() or None,
        "molecule_file": None,
        "receptor_file": (receptor_file or "").strip() or None,
        "receptor_sources": None,
        "positive_control_smiles": (positive_control_smiles or "").strip() or None,
        "site_center": _coords(site_center),
        "site_size": _coords(site_size),
        "exhaustiveness": planned_exh,
        "n_poses": int(n_poses),
        "engine": engine,
        "top_from_previous": int(top_from_previous or 0) or None,
        "keep_hetatm": (keep_hetatm or "").strip() or None,
        # 表单里的「保存位姿 / 最大分子数」必须一路传下去（曾在这条链路上被静默丢弃）
        "save_poses": bool(save_poses),
        "max_ligands": int(max_ligands or 0) or None,
    }
    if params["exhaustiveness"] is None:
        # 解析不出规划值：**不要**下发 null（子 Agent 的参数契约是「没出现的键 = 未指定」，
        # 而且 null 会被 int 形态的 args_schema 拒掉）—— 删掉这个键，让它按工具默认走。
        params.pop("exhaustiveness", None)
    instruction = (f"请对任务参数里给出的分子清单执行真实对接（engine={engine}："
                   + ("auto 优先用 Vina，不可用时回退 AutoDock4 CPU" if engine == "auto" else
                      "经典 AutoDock4 CPU 模式" if engine == "autodock" else
                      "用设置页登记的外部引擎，未就绪会直接报错、不会静默回退" if engine == "external"
                      else "内置 Vina（不可用即报错，不回退）")
                   + "）。")
    if params["top_from_previous"]:
        instruction += (f"**精算轮次**：只对上一轮对接结果里亲和力最好的前 "
                        f"{params['top_from_previous']} 个分子重算 —— 它会直接从共享黑板取清单，"
                        "不要自己列分子。")
    if molecule_file and molecule_file.strip():
        # 分发消息必须给出**解析后的绝对路径**：子 Agent 不允许自行拼接/猜测路径。
        resolved_file, file_attempts, file_candidates = resolve_molecule_file(molecule_file.strip())
        if file_candidates:
            return json.dumps({
                "status": "needs_user_input",
                "message": "配体文件无法确定：请用户在候选清单里指定完整路径。",
                "candidates": file_candidates,
                "attempted": file_attempts,
            }, ensure_ascii=False)
        params["molecule_file"] = resolved_file
        _mol_path = tool_io.artifact_path("molecules", run=active_run(runtime))
        if _mol_path:
            notes.append(f"分子清单运行产物（可直接按文件交接）：{_mol_path}")
        if os.path.isfile(resolved_file) and resolved_file != molecule_file.strip():
            notes.append(f"molecule_file 由 {molecule_file.strip()} 解析而来。")
        elif file_attempts and not os.path.isfile(resolved_file):
            notes.append(f"molecule_file 未能解析为本地文件，已尝试：{'；'.join(file_attempts[:4])}")
    rs = (receptor_sources or "").strip()
    # 本次运行有**上传受体**时，子 Agent 再传注册表受体名（如默认 thrombin）不应被当成"多受体"：
    # 文档承诺 `receptor_file` 优先，否则会把默认凝血酶也 dock 一遍 ——
    # 实测发生（20260918-094607-3195）：白跑 6 个分子，报告里还多出一个受体块，用户会以为跑了两个靶点。
    if params["receptor_file"] and rs:
        from docking_agent.core.receptors import is_structure_source

        if not is_structure_source(rs):
            logger.info("本次运行使用上传受体，忽略子 Agent 传来的受体名 %r（receptor_file 优先）", rs)
            notes.append(f"**本次运行已有用户上传的受体文件**（receptor_file 见任务参数），"
                         f"你之前提到的 receptor_sources={rs} 已被忽略：不要对注册表受体再跑一遍，"
                         "只对接上传的那个受体。")
            rs = ""
    params["receptor_sources"] = rs or None
    if not params["receptor_file"] and not params["receptor_sources"]:
        # 走到这里说明工具本身没拿到任何受体来源、且受理层也没判 default（否则上面已拦下）。
        # 无论如何都**不许替用户挑受体** —— 系统没有默认受体，预置受体只用于内部测试。
        notes.append("**本次没有任何受体来源**：不要开始对接，也不要自行挑一个受体 —— "
                     "系统没有默认受体。请返回 status=needs_user_input，并请用户给出受体"
                     "（PDB 编号 / UniProt accession / 基因或蛋白名 / 上传结构文件）。")
    if params["positive_control_smiles"]:
        notes.append("阳性对照分子也必须一并对接（它是后续结合模式比较的基线）；"
                     "请把对照与候选放在同一批里，保证参数一致。")
    if params["keep_hetatm"]:
        notes.append("受体准备要保留的非水杂原子见 keep_hetatm；若因缺少化学模板而未能保留，"
                     "结果会给出 unsupported_hetatm —— 请如实说明，不要当作已保留。")
    # 给了值但解析不出 3 个坐标 → 如实提醒，绝不静默当成"没给位点"（否则会悄悄换盒子）
    for label, raw, parsed in (("site_center", site_center, params["site_center"]),
                               ("site_size", site_size, params["site_size"])):
        if raw not in (None, "", []) and not parsed:
            notes.append(f"**注意**：{label}={raw!r} 无法解析为 3 个坐标"
                         "（应为 [x, y, z] 或 \"x,y,z\"），已忽略该参数，请改用正确格式后重试。")
    msg = _agent_task_message(instruction, params, notes)
    return _invoke_checked(agent, msg, thread_id="docking", role="docking", runtime=runtime)


@tool
def run_binding_mode_analysis(molecules_json: str = "", molecules_file: str = "",
                              positive_control_smiles: str = "",
                              runtime: ToolRuntime[AgentContext] = None) -> str:
    """将分子清单与阳性对照下发给「结合模式检测 Agent」分析结合模式相似性。

    molecules_json / molecules_file: 二选一。留空时子 Agent 用共享黑板上的全量分子。
    positive_control_smiles: 阳性对照 SMILES；留空时用本次运行请求里的对照。

    返回子 Agent 的真实分析结果 JSON 原文。
    """
    if not positive_control_smiles:
        positive_control_smiles = _load_positive_control()
    agent = get_binding_agent()
    mol_file = (molecules_file or "").strip() or tool_io.artifact_path(
        "molecules", run=active_run(runtime))
    dock_file = tool_io.artifact_path("docking", run=active_run(runtime))
    msg = _agent_task_message(
        "请分析本次候选分子与阳性对照的结合模式相似性，完成后调用 check_binding_consistency "
        "做跨 Agent 交叉核验（对接结果按文件传 docking_file）。",
        {"molecules_json": (molecules_json or "").strip() or None,
         "molecules_file": mol_file or None,
         "positive_control_smiles": (positive_control_smiles or "").strip() or None,
         "docking_file": dock_file or None},
        notes=["molecules_json / molecules_file 都为空时，用共享黑板上的全量分子。"])
    return _invoke_checked(agent, msg, thread_id="binding", role="binding", runtime=runtime)

@tool
def list_known_receptors(runtime: ToolRuntime[AgentContext] = None) -> str:
    """**内部/诊断用**：列出注册表里的预置受体及其【已知结合位点】（盒中心与尺寸）。

    预置受体**只用于内部测试**：它们不再是用户可选来源，也**没有**任何 Agent 绑定本工具
    （协调 Agent 的工具面里已移除；子 Agent 的同名工具也已删除）。保留它只为
    离线诊断/内部脚本能拿到与注册表一致的一份清单。

    返回 JSON：{"status":"ok","default":"thrombin","receptors":[{key,name,pdb,protein,site,available}]}
    """
    from docking_agent.core.receptors import receptor_catalog  # noqa: PLC0415

    return json.dumps(receptor_catalog(), ensure_ascii=False)
