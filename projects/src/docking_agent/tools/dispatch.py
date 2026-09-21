"""整体协调 Agent 的分发工具：分子库导入 + 向 4 个子 Agent 调度任务。

每个工具内部通过调用真实子 Agent（create_agent 实例）完成任务，
实现多 Agent 协作。遵守"@tool 内部不调用 @tool"约束——这里调用的是 Agent，而非 @tool。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, List, Literal, Tuple

from langchain.tools import tool
from docking_agent.runtime.context import AgentContext, active_blackboard, active_request, active_run, new_context, request_context
from docking_agent.tools.schemas import CoordArray, floats_to_text
from docking_agent.paths import libraries_dir, cache_dir, uploads_dir, workspace_dir

from docking_agent.runtime.payload import parse_json_object
from docking_agent.core import (
    parse_smiles_text, load_library_file, read_molecule_file, read_molecule_file_normalized,
)
from docking_agent.agents import tool_io
from docking_agent.agents.blackboard import get_blackboard
from docking_agent.runs import current_run
from docking_agent.agents.workers import (
    invoke_worker,
    get_property_agent,
    get_pocket_agent,
    get_docking_agent,
    get_binding_agent,
)
from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)

# 分子库文件后缀（用于把「路径」与「SMILES 文本」区分开）
_MOLECULE_FILE_EXTS = (".sdf", ".sd", ".smi", ".smiles", ".csv", ".tsv", ".mol2", ".mol",
                       ".txt", ".json")   # .json：运行产物（molecules_tool.json）按文件交接


def looks_like_molecule_path(value: str) -> bool:
    """启发式判断某个参数值是不是「分子文件路径」而不是 SMILES/名称文本。

    真实缺陷 20260917-112206-5017：模型把上传文件的显示名（`PGR.sdf`）塞进参数，
    工具却按纯文本解析，什么都没得到。规则（任一命中即算路径）：
      - http(s) URL；
      - 字符串本身是存在的本地文件；
      - 扩展名属于分子文件格式（SMILES 几乎不会以 .sdf/.smi/.csv/.mol2 结尾）。
    """
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith(("http://", "https://")):
        return True
    try:
        if os.path.isfile(text):
            return True
    except (OSError, ValueError):  # 超长字符串 / 含 NUL：一定不是路径
        return False
    ext = os.path.splitext(text.split("?")[0])[1].lower()
    return ext in _MOLECULE_FILE_EXTS


def molecule_search_dirs() -> List[Path]:
    """解析分子文件时按序查找的目录（cwd → 工作区 → assets → uploads → cache → 示例库）。"""
    candidates = [Path.cwd(), workspace_dir(), workspace_dir() / "assets",
                  uploads_dir(), cache_dir(), libraries_dir()]
    out: List[Path] = []
    seen = set()
    for path in candidates:
        try:
            key = str(path.resolve())
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def resolve_molecule_file(value: str) -> Tuple[str, List[str], List[str]]:
    """把模型/用户给出的分子文件来源解析成**真实存在的本地路径**。

    为什么需要：上传端点的落盘名带时间戳前缀（`20260917-112109-3e5739-PGR.sdf`），
    而模型往往只拿到显示名 `PGR.sdf`，于是把裸文件名当路径传给工具，
    RDKit 报 `Bad input file PGR.sdf`，最终静默退回示例库（真实缺陷 20260917-112206-5017）。
    这里按「绝对/相对 → 各候选目录精确文件名 → `<前缀>-<原名>` 后缀匹配」逐级解析，
    并把每次尝试的候选与原因一并返回，便于如实上报而不是让模型瞎猜。

    返回 `(resolved, attempts, candidates)`：
      - 唯一命中：`resolved` = 本地绝对路径（或 URL），`candidates` 为空；
      - 命中多个不同文件（歧义）：`resolved` 原样返回、**不猜**，`candidates` 为候选绝对路径清单，
        由上层决定报错/向用户提问；
      - 未命中：`resolved` 原样返回，`candidates` 为空，`attempts` 逐条记录失败原因。
    """
    text = str(value or "").strip()
    attempts: List[str] = []
    if not text:
        return text, attempts, []
    if text.startswith(("http://", "https://")):
        return text, attempts, []
    if os.path.isfile(text):
        return os.path.abspath(text), attempts, []

    name = os.path.basename(text.replace("\\", "/"))
    stem = os.path.splitext(name)[0]
    target = name.lower()
    target_stem = stem.lower()
    dirs = molecule_search_dirs()
    empty: List[str] = []

    # ① 候选目录下的精确文件名（相对路径 `assets/uploads/x.sdf` 也会在此命中）
    exact_hits: List[Path] = []
    for directory in dirs:
        candidate = directory / name
        try:
            if candidate.is_file():
                exact_hits.append(candidate)
                continue
        except OSError:  # 允许静默：候选路径不可访问等同于不存在
            pass
        attempts.append(f"{candidate}：不存在")
    hits = _unique_paths(exact_hits)
    if len(hits) == 1:
        return str(hits[0]), attempts, []
    if len(hits) > 1:
        attempts.append(f"{name}：在多个目录命中同名文件，无法确定用哪一个")
        return text, attempts, [str(p) for p in hits]

    # ② 后缀匹配：上传文件形如 `<时间戳>-<哈希>-<原名>`，用原名结尾即可命中；
    #    无扩展名时按词干匹配（`PGR` → `<...>-PGR.sdf`）。
    suffix_hits: List[Path] = []
    for directory in dirs:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            lowered = entry.name.lower()
            ext = os.path.splitext(lowered)[1]
            if lowered == target or lowered.endswith(target):
                suffix_hits.append(entry)
            elif target_stem and ext in _MOLECULE_FILE_EXTS \
                    and os.path.splitext(lowered)[0].endswith(target_stem):
                suffix_hits.append(entry)
    hits = _unique_paths(suffix_hits)
    if len(hits) == 1:
        return str(hits[0]), attempts, []
    if len(hits) > 1:
        attempts.append(f"{name}：后缀匹配到 {len(hits)} 个候选文件，无法确定用哪一个")
        return text, attempts, [str(p) for p in hits]

    attempts.append(f"{name}：在上传/缓存目录中未找到同名或 `<前缀>-{name}` 形式的文件")
    return text, attempts, empty


def _unique_paths(paths: List[Path]) -> List[Path]:
    """按解析后的绝对路径去重并保持顺序（同一文件经不同相对路径命中只算一次）。"""
    out: List[Path] = []
    seen = set()
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


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
    """受理层判定「用户点名的受体无法解析」时，返回给编排层的 JSON；否则返回空串。

    产品底线：**不明确计算对象时绝不计算**。真实缺陷是用户点名「植物去甲基化1酶」，
    UniProt accession 直查与基因/蛋白名称检索都无匹配，系统却按「回退默认受体」继续对接 ——
    计算对象被悄悄换成了凝血酶，用户拿到的是答非所问的结果。
    因此这里在**任何对接引擎被调用之前**拦下：返回 `needs_user_input` 并要求用户补充。

    `source` 何时变 `unresolved`：现在不再由受理层预先判定，而是由
    `tools/online.py::fetch_protein_structure` 在**真正在线检索过**之后写回
    （`_mark_receptor_unresolved`）—— 也就是说，走到这里的每个 `unresolved` 都带着
    真实的已尝试检索与候选清单。本护栏的判定逻辑与对外契约保持不变（只多回传这些证据）。
    """
    spec = (getattr(run, "data", None) or {}).get("task_spec") or {}
    receptor = spec.get("receptor") or {}
    if str(receptor.get("source") or "") != "unresolved":
        return ""
    name = str(receptor.get("name") or "用户点名的受体")
    resolution = receptor.get("resolution") or {}
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
            "（不是注册表受体、不是 PDB 号、不是 UniProt accession，也没有上传受体文件）。"
            + evidence +
            "**本次不执行任何对接计算**，也不得擅自改用系统默认受体。"
            "请让用户三选一：① 提供 PDB ID（如 1DWC）；② 上传受体文件（.pdb/.ent/.cif/.pdbqt）；"
            "③ 明确同意改用系统默认受体 凝血酶（thrombin, 1DWC）。"
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
    ctx = active_request(runtime) or new_context(method="import_molecule_library")
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
    ctx = active_request(runtime) or new_context(method="run_property_assessment")
    agent = get_property_agent()
    mol_file = tool_io.artifact_path("molecules", run=active_run(runtime))
    if (molecules_json or "").strip():
        msg = f"请评估以下分子的物化性质与类药性（molecules_json）：{molecules_json}"
    elif mol_file:
        msg = (f"请评估本次任务的**全部**候选分子（分子清单文件 molecules_file=**{mol_file}**，"
               "这是运行产物绝对路径）。**优先按文件交接**：调用 molecular_property_assessment 时"
               "直接传 molecules_file=该路径（不要先把清单读进上下文再抄一遍），"
               "它会通过统一归一化层读取全量分子；漏传时它会回退共享黑板。")
    else:
        msg = ("请评估**共享黑板上**全部候选分子的物化性质与类药性"
               "（清单留空时用 board_molecules_json 取，不要臆造分子）。")
    return _invoke_checked(agent, msg, thread_id="property", role="property", runtime=runtime)


@tool
def run_pocket_analysis(receptor_file: str = "", receptor_sources: str = "",
                        pocket_engine: str = "", top_n: int = 8, runtime: ToolRuntime[AgentContext] = None) -> str:
    """将「受体结构」下发给「口袋分析 Agent」，用真实工具预测结合口袋并选定对接盒子。

    何时调用：**在 run_docking 之前**。如果用户没有显式给出位点盒（site_center/site_size），
    应先把受体交给口袋分析 Agent：它用 P2Rank（或内置几何法）预测口袋、与实验位点比对，
    然后通过共享黑板把选定的盒子提交给 Docking Agent —— Docking Agent 会自动使用它。

    receptor_file: 可选，用户上传/提供的蛋白质文件（.pdb/.ent/.pdb1/.cif/.mmcif/.pdbqt，本地路径或 URL）。
    receptor_sources: 可选，预置受体名（thrombin/1PTU 等）或受体文件路径；留空则用本次任务的受体。
    pocket_engine: 留空=按设置页面/环境变量（默认 auto：优先 P2Rank，不可用时回退内置几何法）；
        也可显式传 p2rank / geometric / known_site。
    top_n: 返回前 N 个候选口袋（默认 8）。

    返回子 Agent 的真实分析结果 JSON 原文：{status, engine, pockets:[{rank,score,center,extent,residues}],
    selected:{pocket_rank,center,size}, validation:{...}, agent_note}
    """
    ctx = active_request(runtime) or new_context(method="run_pocket_analysis")  # noqa: F841
    agent = get_pocket_agent()
    msg = ("请分析该受体的结合口袋并选定对接盒子："
           f"receptor_file={receptor_file or '（未提供，使用本次任务的受体）'}；"
           f"receptor_sources={receptor_sources or '（未提供）'}；"
           f"pocket_engine={pocket_engine}；top_n={top_n}。"
           "请调用 predict_binding_pockets 预测口袋、compare_pocket_with_experiment 与实验位点比对，"
           "然后用 set_docking_site 提交你选定的盒子（reason 里写明引擎、口袋编号与一致性结论）。")
    return _invoke_checked(agent, msg, thread_id="pocket", role="pocket", runtime=runtime)


@tool
def run_docking(molecules_json: str = "", molecule_file: str = "", molecules_file: str = "",
                receptor_file: str = "", receptor_sources: str = "",
                positive_control_smiles: str = "",
                site_center: CoordArray = None, site_size: CoordArray = None,
                exhaustiveness: int = 16, n_poses: int = 1,
                engine: Literal["auto", "vina", "autodock"] = "auto",
                top_from_previous: int = 0,
                keep_hetatm: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """将分子清单下发给「Docking 执行 Agent」，对指定蛋白质受体（可多受体=蛋白质库）执行真实对接。

    molecules_json: 可选，[{"name":"M1","smiles":"..."}, ...]；可为空（配合 molecule_file 用）。
    molecule_file: 可选，用户上传的小分子文件（SDF/SMILES/CSV/MOL2，本地路径或 URL），自动读取为小分子库。
    molecules_file: 同 molecule_file（**推荐**）：也可以直接给运行产物 `molecules_tool.json` 的绝对路径
        —— Agent 之间按文件交接数据，大库不必把清单搬进上下文。
    receptor_file: 可选，用户上传的蛋白质受体文件（.pdb/.ent/.pdb1/.cif/.mmcif/.pdbqt，本地路径或 URL），自动现场准备。
    receptor_sources: 受体来源，可空或一个受体，或 JSON 数组/分号分隔多受体（蛋白质库）。
        支持 预置受体名(thrombin/1DWC、trypsin/1PTU)、用户 PDB/PDBQT 文件路径或 URL。
        为空时交由子 Agent 按默认受体(凝血酶)处理并在结果中提示未指定受体。
    positive_control_smiles: 可选，阳性对照 SMILES。**一般不必手动传**：对接工具会自动从本次运行的
        请求参数里取用户填写的阳性对照并一并对接（作为方法学基线）。只有当用户在对话中新指定了对照时才需要传。
    site_center: 可选，已知结合位点的盒中心 `[x, y, z]`（Å，覆盖受体注册位点）；
        也接受逗号分隔字符串 "31.5,13.74,24.36"（旧调用方兼容）。
    site_size:   可选，位点盒尺寸 `[x, y, z]`（Å）；也接受 "22,22,22"。
    exhaustiveness: Vina 对接搜索强度（默认16，越大越精细越慢）
    n_poses: 返回构象数（默认1）
    engine: 对接引擎，'auto'(默认：优先 Vina，失败自动回退 AutoDock4 CPU) / 'vina' / 'autodock'(经典 AutoDock4 CPU 模式)
    keep_hetatm: 可选。受体准备时要**保留的非水杂原子残基名**，逗号分隔（如 'HEM,ZN,NAD'）。
        默认空 = 标准流程剔除水与杂原子（被剔除的残基会逐条出现在结果的 dropped_hetatm/notes 里）。
        金属酶/含辅因子体系（血红素、锌指、NAD/FAD 依赖酶）应在判断其重要性后用本参数保留并重跑；
        金属离子通常可直接保留，大辅因子需要 meeko 化学模板（缺模板的会出现在 unsupported_hetatm）。

    返回子 Agent 的真实对接结果 JSON 原文（按受体分组的 受体×配体 全组合结果）。
    """
    ctx = active_request(runtime) or new_context(method="run_docking")
    # 文件交接别名：molecules_file 与 molecule_file 同义（产物路径与用户上传文件都是本地文件）
    molecule_file = (molecule_file or "").strip() or (molecules_file or "").strip()
    # 护栏：受理层判定「用户点名的受体无法解析」时**在调用任何对接引擎之前**返回，
    # 绝不回退默认受体继续算（这是产品底线；见 unresolved_receptor_message）。
    guard = unresolved_receptor_message(active_run(runtime))
    if guard:
        logger.info("拒绝对接：受理层判定用户点名的受体无法解析")
        return guard
    agent = get_docking_agent()
    msg = (f"请对以下分子执行真实对接（molecules_json）：{molecules_json}；"
           f"对接参数 exhaustiveness={exhaustiveness}, n_poses={n_poses}, engine={engine}"
           f"（engine=auto 优先用 Vina，不可用时回退 AutoDock4 CPU；engine=autodock 直接用经典 AutoDock4 CPU 模式）")
    if int(top_from_previous or 0) > 0:
        msg += (f"；**精算轮次**：只对上一轮对接结果里亲和力最好的前 {int(top_from_previous)} 个分子重算"
                f"（在 molecular_docking 里传 top_from_previous={int(top_from_previous)}；"
                "它会直接从共享黑板取清单，不要自己列分子）。")
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
        _mol_path = tool_io.artifact_path("molecules", run=active_run(runtime))
        msg += (f"；分子清单文件 molecules_file=**{_mol_path}**"
                "（运行产物绝对路径，可直接传给 molecular_docking 的 molecule_file，"
                "大库不必把清单搬进上下文）"
                if _mol_path else "")
        msg += (f"；配体来源：用户上传小分子文件 molecule_file=**{resolved_file}**"
                "（这是已解析好的绝对路径，请**原样**传给 molecular_docking 的 molecule_file，"
                "不要自行拼接或猜测路径；它会自动读取为小分子库并执行对接）")
        if os.path.isfile(resolved_file) and resolved_file != molecule_file.strip():
            msg += f"（该路径由 {molecule_file.strip()} 解析而来）"
        elif file_attempts and not os.path.isfile(resolved_file):
            msg += f"（未能解析为本地文件，已尝试：{'；'.join(file_attempts[:4])}）"
    rs = (receptor_sources or "").strip()
    # 本次运行有**上传受体**时，子 Agent 再传注册表受体名（如默认 thrombin）不应被当成"多受体"：
    # 文档承诺 `receptor_file` 优先，否则会把默认凝血酶也 dock 一遍 ——
    # 实测发生（20260918-094607-3195）：白跑 6 个分子，报告里还多出一个受体块，用户会以为跑了两个靶点。
    if receptor_file and receptor_file.strip() and rs:
        from docking_agent.core.receptors import is_structure_source

        if not is_structure_source(rs):
            logger.info("本次运行使用上传受体，忽略子 Agent 传来的受体名 %r（receptor_file 优先）", rs)
            msg += (f"；**本次运行已有用户上传的受体文件**（receptor_file 见下），"
                    f"你之前提到的 receptor_sources={rs} 已被忽略：不要对注册表受体再跑一遍，"
                    "只对接上传的那个受体。")
            rs = ""
    if receptor_file and receptor_file.strip():
        msg += f"；受体来源：用户上传蛋白质文件 receptor_file={receptor_file}（.pdb/.ent/.cif/.pdbqt 等结构文件，请先读取并现场准备）。"
    elif rs:
        msg += f"；受体来源 receptor_sources={rs}（可为预置受体名/PDB文件/多受体蛋白质库）"
    else:
        msg += "；未指定受体，请按默认受体处理并在结果中提示未使用默认受体的情况。"
    if positive_control_smiles and positive_control_smiles.strip():
        msg += (f"；**阳性对照分子也必须一并对接**（SMILES={positive_control_smiles.strip()}），"
                "它是后续结合模式比较的基线；请把对照与候选放在同一批里，保证参数一致")
    center_text = floats_to_text(site_center, expect=3)
    size_text = floats_to_text(site_size, expect=3)
    if center_text:
        msg += f"；已知结合位点盒中心 site_center=[{center_text}]"
    if size_text:
        msg += f"，盒尺寸 site_size=[{size_text}]"
    if keep_hetatm and keep_hetatm.strip():
        msg += (f"；**受体准备要保留的非水杂原子** keep_hetatm={keep_hetatm.strip()}"
                "（请在 molecular_docking 里原样传 keep_hetatm；若该残基缺少化学模板而未能保留，"
                "结果会给出 unsupported_hetatm，请如实说明而不是当作已保留）")
    if center_text or size_text:
        msg += ("（请把这两个参数**按数组形态**原样传给 molecular_docking，"
                "例如 site_center=[31.5, 13.74, 24.36]；字符串形态已废弃）")
    # 给了值但解析不出 3 个坐标 → 如实提醒，绝不静默当成"没给位点"（否则会悄悄换盒子）
    for label, raw, parsed in (("site_center", site_center, center_text),
                               ("site_size", site_size, size_text)):
        if raw not in (None, "", []) and not parsed:
            msg += (f"；**注意**：{label}={raw!r} 无法解析为 3 个坐标"
                    "（应为 [x, y, z] 或 \"x,y,z\"），已忽略该参数，请改用正确格式后重试")
    return _invoke_checked(agent, msg, thread_id="docking", role="docking", runtime=runtime)


@tool
def run_binding_mode_analysis(molecules_json: str, positive_control_smiles: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """将分子清单与阳性对照下发给「结合模式检测 Agent」分析结合模式相似性。

    molecules_json: [{"name":"M1","smiles":"..."}, ...]
    positive_control_smiles: 阳性对照 SMILES，为空时使用示例默认对照。

    返回子 Agent 的真实分析结果 JSON 原文。
    """
    ctx = active_request(runtime) or new_context(method="run_binding_mode_analysis")
    if not positive_control_smiles:
        positive_control_smiles = _load_positive_control()
    agent = get_binding_agent()
    mol_file = tool_io.artifact_path("molecules", run=active_run(runtime))
    dock_file = tool_io.artifact_path("docking", run=active_run(runtime))
    msg = (f"请分析本次候选分子与阳性对照 {positive_control_smiles} 的结合模式相似性。"
           f"**按文件交接**：分子清单 molecules_file=**{mol_file or '（无，见 molecules_json）'}**")
    if (molecules_json or "").strip():
        msg += f"（本次也给了 molecules_json：{molecules_json}）"
    msg += ("；调用 binding_mode_analysis 时优先传 molecules_file（大库不必搬进上下文）。"
            f"完成后调用 check_binding_consistency 做跨 Agent 交叉核验，"
            f"**对接结果按文件传 docking_file={dock_file or '（无，见共享黑板）'}**。")
    return _invoke_checked(agent, msg, thread_id="binding", role="binding", runtime=runtime)

@tool
def list_known_receptors(runtime: ToolRuntime[AgentContext] = None) -> str:
    """列出系统内置的蛋白质受体及其【已知结合位点】（盒中心与尺寸）。

    当用户没有明确给出受体或位点坐标时，先调用本工具查看可用受体；
    需要指定位点时，把返回的 site.center / site.size 以字符串形式
    （如 [31.5, 13.74, 24.36] 与 [22, 22, 22]）传给 run_docking 的 site_center / site_size。

    返回 JSON：{"status":"ok","default":"thrombin","receptors":[{key,name,pdb,protein,site,available}]}
    """
    from docking_agent.core import DEFAULT_RECEPTOR, list_receptors  # noqa: PLC0415

    return json.dumps({"status": "ok", "default": DEFAULT_RECEPTOR, "receptors": list_receptors()},
                      ensure_ascii=False)
