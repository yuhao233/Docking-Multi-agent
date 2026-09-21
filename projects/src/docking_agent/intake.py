"""任务受理层（Intake）：把「用户指令 + 表单参数」变成**任务规约**。

## 为什么单独一层

1. **两套价值观原本塞在同一个提示词里，必然打架**：
   受理要「信息不足就问」，执行要「授权后不许停」。旧协调 Agent 提示词的
   `# 输入合理性审查`（"若输入含糊不清…先礼貌询问用户"）与 `# 执行纪律` 1
   （"只要候选分子库非空…就**不得**在中途停下来询问用户"）在
   「分子库有、但意图含糊」时直接矛盾。现在把「跑还是问」变成受理层的**显式判定**（`decision`），
   编排层只是执行它 —— 矛盾从规则冲突变成了一次字段传递。
2. **受理是唯一能被单测的环节**：输入 → 规约(JSON) 是纯解析；编排几乎无法单测。
3. **受理逻辑原本散在三处**（API 拼消息 / 协调提示词两节 / 工具 docstring），改一处就漂移。

## 设计原则：确定性优先，LLM 兜底

- `build_task_spec(req)` 用**纯规则**产出规约：表单、SMILES、受体名、上传文件、站点坐标、参数
  这些结构化信息一律不经过模型；
- 只有 `needs_llm=True`（自然语言指令需要理解：多意图 / 提到化合物名称 / 越界未定 / 与表单冲突）
  才调用 `intake` 角色的模型，且**只允许它补充白名单字段**（任务类型、目标、提到但未给出的分子/受体、
  缺失项、要问的问题、假设、是否越界）；
- 模型**绝不能**产出参数值（exhaustiveness / engine / 坐标 / 阳性对照），也**不能编造分子**：
  它提取的分子名必须逐字出现在用户原文里，否则丢弃；
- 任何异常（无 key、超时、JSON 不合法）都**回退到确定性规约**，绝不因为受理层失败而让整次运行失败。

优先级（与项目既有语义一致，见 docs/api.md §2/§7）：

| 模式 | 谁是权威 | 是否调用受理模型 |
| --- | --- | --- |
| `manual` | 表单参数（指令仅作目标描述） | 否（零额外延迟） |
| `chat` + `advanced=False` | 指令优先，缺省用**系统默认** | 消息非空时需要 |
| `chat` + `advanced=True` | 指令优先，缺省用**高级设置** | 消息非空时需要 |
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from docking_agent.runtime.payload import parse_json_object
from docking_agent.config import env

logger = logging.getLogger(__name__)

DEFAULT_AGENT_TASK = ("请完成一次完整的分子筛选：导入候选分子库 → 物化性质评估 → 分子对接 → "
                      "结合模式分析 → 生成筛选报告并给出按亲和力排序的结论。")

# 任务类型（受理层判定，编排层据此决定调度哪些子 Agent）
TASK_TYPES = ("screening", "properties_only", "docking_only", "binding_only", "report_only", "unknown")

# 受理层可能判定出的「决策」
DECISION_RUN = "run"      # 输入合法 → 必须完整跑完（不得中途询问）
DECISION_ASK = "ask"      # 缺必需数据 → 只提问，不调用任何工具
DECISION_REJECT = "reject"  # 超出系统能力（闲聊/无关） → 礼貌说明，不调用任何工具

# 只有这些字段允许被受理模型补充（其余一律以确定性规约为准）
LLM_ALLOWED_FIELDS = ("out_of_scope", "scope_reason", "task_type", "goal",
                      "mentioned_molecules", "mentioned_receptor", "multi_intent",
                      "needs_user_input", "missing", "questions", "assumptions", "confidence")
# 受理模型**永远不能**提供的字段（含数值参数与科学判定）
LLM_FORBIDDEN_FIELDS = ("params", "site", "receptor", "ligands", "positive_control", "decision",
                        "exhaustiveness", "engine", "n_poses", "pocket_engine", "center", "size")

_TASK_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("properties_only", ("物化性质", "理化性质", "类药性", "成药性", "lipinski", "分子量", "logp",
                         "tpsa", "氢键", "性质评估")),
    ("docking_only", ("对接", "亲和力", "结合能", "结合自由能", "打分", "排序", "docking", "vina",
                      "结合强度")),
    ("binding_only", ("结合模式", "相似度", "阳性对照", "作用方式", "骨架", "药效团", "结合pose",
                      "结合位姿")),
    ("report_only", ("报告", "图表", "导出", "下载")),
)


# --------------------------------------------------------------------------- #
# 确定性规约
# --------------------------------------------------------------------------- #
def _system_default_params() -> Dict[str, Any]:
    """系统默认运行参数（chat 模式折叠高级设置时使用）。"""
    return {"receptor": DEFAULT_RECEPTOR_NAME, "site": None,
            "positive_control": "",   # 阳性对照可选：默认不提供 → 不做对照分析
            "exhaustiveness": 16, "n_poses": 1, "engine": "vina", "pocket_engine": "auto",
            "ligands_text": "", "molecule_file": "", "skip_positive_control": False}


def _params_from_request(req: Any) -> Dict[str, Any]:
    uploaded = (getattr(req, "receptor_file", "") or "").strip()
    return {"receptor": f"用户上传受体文件 {uploaded}" if uploaded else (req.receptor or DEFAULT_RECEPTOR_NAME),
            "receptor_file": uploaded,
            "site": (req.site_center, req.site_size) if req.site_center else None,
            "positive_control": (req.positive_control or "").strip(),
            "exhaustiveness": req.exhaustiveness, "n_poses": req.n_poses,
            "engine": (req.engine or "").strip(),
            "pocket_engine": (getattr(req, "pocket_engine", "") or "").strip(),
            "ligands_text": (req.ligands_text or "").strip(),
            "molecule_file": (req.molecule_file or "").strip(),
            "skip_positive_control": bool(req.skip_positive_control)}


def _guess_task_type(text: str) -> str:
    """按关键词粗判任务类型（确定性，零模型）；判不出则视为综合筛选。"""
    lowered = (text or "").lower()
    hits = [name for name, keys in _TASK_KEYWORDS if any(k.lower() in lowered for k in keys)]
    if not hits:
        return "screening"
    if "screening" in hits or len(hits) > 1:
        return "screening"          # 提到多个维度 → 按综合筛选处理
    return hits[0]


def _count_ligands(text: str) -> int:
    """粗数一下文本里的候选分子（不联网、不建分子）。"""
    if not text:
        return 0
    parts = [p for p in re.split(r"[,\n;；]", text) if p.strip()]
    return len(parts)


def _mentioned_receptor(text: str) -> str:
    """在自然语言里认一下已注册的受体名（确定性；认不出返回空）。"""
    lowered = (text or "").lower()
    for name in ("thrombin", "1dwc", "trypsin", "1ptu"):
        if name in lowered:
            return "thrombin" if name in ("thrombin", "1dwc") else "trypsin"
    if re.search(r"\b[0-9][a-z0-9]{3}\b", lowered):     # 形如 3zbf 的 PDB 号
        return re.search(r"\b[0-9][a-z0-9]{3}\b", lowered).group(0).upper()
    return ""


def _mentioned_accessions(text: str) -> List[str]:
    """抽取指令里出现的全部 UniProt accession（去重、保序）。"""
    out: List[str] = []
    for match in _ACCESSION_IN_TEXT_RE.finditer(str(text or "")):
        acc = match.group(1).upper()
        if acc not in out:
            out.append(acc)
    return out


def _mentioned_accession(text: str) -> str:
    """抽取指令里出现的 UniProt accession（用户点选候选后的追问会带上它）。

    这类输入是**可直接解析**的精确标识符，优先级高于「…酶/…蛋白」式的类别名，
    否则「用 Q9SJQ6（拟南芥 ROS1 去甲基化酶）继续」会被抽成「去甲基化酶」再查一轮。
    """
    hits = _mentioned_accessions(text)
    return hits[0] if hits else ""


DEFAULT_RECEPTOR_NAME = "thrombin"

# 形如 1DWC / 3zbf 的 PDB 号（用户点名 PDB 号时交给在线取结构工具）
_PDB_ID_RE = re.compile(r"^[0-9][A-Za-z0-9]{3}$")
# UniProt accession 形状（6 位或 10 位；如 P00533 / Q9Y6K9）——可直查，不算「不可解析」
_UNIPROT_RE = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$",
    re.IGNORECASE)
# 指令里「点名受体」的确定性模式：中文以 酶/蛋白/受体 结尾，英文以 kinase/receptor 结尾。
# 注意：该模式只用于**发现候选名**，是否可解析由 `_resolve_receptor_name` 判定。
# 末尾可选跟一个基因 token（如「植物去甲基化酶ROS1」），否则中文模式会在「酶」处截断、丢掉基因名。
_NAMED_RECEPTOR_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9\-]{0,30}(?:酶|蛋白|受体)(?:[A-Z][A-Z0-9]{1,9})?"
    r"|[A-Za-z][A-Za-z0-9\-]{1,30}[\s\-]?(?:kinase|receptor)",
    re.IGNORECASE)
# 指令里出现的 UniProt accession（点选候选后的追问会带上它：如「用 Q9SJQ6 …继续」）
_ACCESSION_IN_TEXT_RE = re.compile(
    r"\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b",
    re.IGNORECASE)
# 指示代词/冠词：把「这个受体」「the receptor」这类**没点名**的表述排除掉
_RECEPTOR_DEMONSTRATIVES_ZH = ("这个", "那个", "一种", "某种", "上述", "该", "此", "本", "这", "那")
_RECEPTOR_DEMONSTRATIVES_EN = ("the", "this", "that", "these", "those", "some", "a", "an")
# 中文没有词边界：正则从最左字符开始会吞进「请把/帮我看看」等动词/助词，这里把它们从候选名左侧剥掉
_RECEPTOR_LEAD_FILLERS = (
    "请帮我", "帮我看看", "帮我", "帮忙", "麻烦", "我想要", "我要用", "我要", "我想",
    "针对", "基于", "按照", "根据", "对于", "关于",
    "请在", "请把", "请用", "请对", "请", "把", "将", "使用", "采用", "换成", "选择",
    "分析一下", "看一下", "看看", "看下", "选", "指定", "用", "对", "和", "与", "跟",
    "就", "是", "拿", "以", "看", "换",
)
# 中文受体名 → 注册表 key（否则「用凝血酶对接」会被误判成「点名了未知受体」）
_RECEPTOR_SYNONYMS = {"凝血酶": "thrombin", "人α-凝血酶": "thrombin", "α-凝血酶": "thrombin",
                      "胰蛋白酶": "trypsin", "牛胰蛋白酶": "trypsin",
                      "凝血酶(thrombin)": "thrombin", "胰蛋白酶(trypsin)": "trypsin"}
# 只有类别名词、没有实质名称的表述
_RECEPTOR_GENERIC = {"受体", "蛋白", "蛋白质", "酶", "receptor", "kinase", "protein"}
# 「没点名」的泛指/否定表述：出现这些词说明用户并没有给出一个具体的受体名，
# 不能当成「点名了不可解析的受体」（否则「未指定受体就用系统默认」会被误判成 ask）。
_RECEPTOR_NON_NAME_MARKERS = ("未指定", "没有指定", "未指明", "未说明", "不指定", "不确定",
                              "任意", "任一", "随便", "某个", "某一", "某种",
                              "其它", "其他", "别的", "其余", "默认",
                              "这个", "那个", "上述", "该受", "此受", "本受")


def _is_generic_receptor_name(name: str) -> bool:
    """候选名是否只是泛指/否定（没有点名具体受体）。"""
    text = str(name or "").strip()
    if not text:
        return True
    if text.lower() in _RECEPTOR_GENERIC:
        return True
    return any(marker in text for marker in _RECEPTOR_NON_NAME_MARKERS)


def _known_receptors() -> Tuple[Dict[str, Any], Dict[str, str]]:
    """注册表 key/别名（延迟导入，避免把 core 的导入副作用带进受理层模块导入）。"""
    try:
        from docking_agent.core.receptors import RECEPTOR_ALIASES, RECEPTOR_REGISTRY

        return RECEPTOR_REGISTRY, RECEPTOR_ALIASES
    except Exception:  # noqa: BLE001
        logger.debug("受体注册表不可用（按无可解析受体处理）", exc_info=True)
        return {}, {}


def _resolve_receptor_name(name: str) -> Tuple[bool, str]:
    """判断用户点名的受体能否**直接解析**；返回 (可解析, 规范化名字/注册表 key)。

    可解析 = 注册表 key/别名、PDB 号、UniProt accession 形状之一；
    名称为空、纯类别名词或其它未知名称都视为不可解析（→ 受理层停下提问）。
    """
    key = str(name or "").strip()
    if not key:
        return False, ""
    low = key.lower()
    registry, aliases = _known_receptors()
    if low in registry:
        return True, low
    if low in aliases:
        return True, str(aliases[low])
    if low in _RECEPTOR_SYNONYMS:
        return True, _RECEPTOR_SYNONYMS[low]
    if _PDB_ID_RE.match(key):
        return True, key.upper()          # PDB 号 → 交给 fetch_protein_structure
    if _UNIPROT_RE.match(key):
        return True, key.upper()          # UniProt accession → 交给 accession 直查
    return False, ""


def _strip_demonstrative(name: str) -> str:
    """从候选受体名左侧剥掉动词/助词/指示代词，避免把「请把这个受体」当成点名。

    中文没有词边界，正则从左往右匹配会从「请/帮」开始吞进一串动词与指示代词；
    这里反复剥离已知前缀，直到剩下「植物去甲基化1酶」这类实质名称（或剥成空的类别词）。
    """
    text = str(name or "").strip()
    changed = True
    while changed and text:
        changed = False
        for prefix in _RECEPTOR_LEAD_FILLERS + _RECEPTOR_DEMONSTRATIVES_ZH:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
                break
        if changed:
            continue
        low = text.lower()
        for prefix in _RECEPTOR_DEMONSTRATIVES_EN:
            if low.startswith(prefix + " "):      # 英文只在整词前缀时剥离（不误伤 ABC/Ank…）
                text = text[len(prefix):].strip()
                changed = True
                break
    return text


def _named_receptor_candidate(text: str) -> str:
    """从用户指令里**确定性**抽取「被点名的受体」；没点名（或只是指示代词）则返回空。"""
    raw = str(text or "")
    if not raw:
        return ""
    seen: set = set()
    for match in _NAMED_RECEPTOR_RE.finditer(raw):
        name = _strip_demonstrative(match.group(0))
        if _is_generic_receptor_name(name) or name in seen:
            continue
        seen.add(name)
        # 命中一个真实候选就返回；若它其实是已知受体的中文别名，也在这里返回（交由解析判定）
        return name
    return ""


def _unresolved_receptor_questions(name: str) -> List[str]:
    """点名了具体受体但解析不了时，给用户的三条出路。"""
    return [
        f"未能解析您点名的受体「{name}」（已尝试 UniProt accession 直查与基因/蛋白名称检索，均无匹配）。"
        "请三选一：① 提供 PDB ID（如 1DWC）；② 上传受体文件（.pdb/.pdbqt）；"
        "③ 明确同意改用系统默认受体 凝血酶（thrombin, 1DWC）。",
    ]


def _extract_req_molecules(message: str) -> List[str]:
    """从用户指令里抽出「名称:SMILES」形式（确定性，零模型）。"""
    if not message:
        return []
    try:
        from docking_agent.core.ligands import extract_smiles

        items = extract_smiles(message)
    except Exception:  # noqa: BLE001
        logger.debug("从指令抽取 SMILES 失败", exc_info=True)
        return []
    return [f"{m['name']}:{m['smiles']}" for m in items]


def _prior_contents(prior_turns: Optional[List[Dict[str, Any]]],
                    roles: Tuple[str, ...] = ("user", "assistant")) -> str:
    """把上一轮对话拼成纯文本（只读；顺序保留，便于确定性抽取分子/受体）。"""
    if not prior_turns:
        return ""
    parts: List[str] = []
    for turn in prior_turns:
        if not isinstance(turn, dict):
            continue
        if roles and str(turn.get("role") or "").strip().lower() not in roles:
            continue
        content = str(turn.get("content") or "").strip()
        if content:
            parts.append(content)
    return "\n".join(parts)


def _normalize_prior_turns(prior_turns: Any) -> List[Dict[str, str]]:
    """规整外部传入的上一轮对话（只保留 role + content，role 归一化为 user/assistant）。"""
    out: List[Dict[str, str]] = []
    for turn in (prior_turns or []):
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        if role in ("human",):
            role = "user"
        elif role in ("ai", "agent"):
            role = "assistant"
        if role not in ("user", "assistant"):
            continue
        content = str(turn.get("content") or "").strip()
        if content:
            out.append({"role": role, "content": content})
    return out


def build_task_spec(req: Any, *, raw_request: str = "",
                    prior_turns: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """确定性地产出任务规约（零模型、可单测、无副作用）。

    `prior_turns`（可选）：同一会话的最近若干轮 `[{"role","content"}]`，**只读**——
    受理层据此把上一轮已确认的分子/受体**确定性继承**下来，避免用户回答追问后
    系统又开一段全新对话（并把同样的追问再问一遍）。
    """
    mode = (getattr(req, "mode", "manual") or "manual").strip().lower()
    advanced = bool(getattr(req, "advanced", False))
    message = (getattr(req, "message", "") or "").strip()
    if mode == "chat" and not advanced:
        authority, params = "chat", _system_default_params()
        params_note = "系统默认（对话模式未展开高级设置）"
    elif mode == "chat":
        authority, params = "chat+advanced", _params_from_request(req)
        params_note = "高级设置（仅作为指令未指定时的默认值）"
    else:
        authority, params = "manual", _params_from_request(req)
        params_note = "界面表单（本次任务的权威参数）"

    assumptions: List[str] = []
    missing: List[str] = []
    questions: List[str] = []

    # ---- 上一轮上下文（多轮会话）----
    # 只做确定性继承（SMILES / 受体名），不解读语义；语义留给可选的受理模型。
    prior = _normalize_prior_turns(prior_turns)
    prior_user_text = _prior_contents(prior, ("user",))
    prior_all_text = _prior_contents(prior)
    prior_molecules = _extract_req_molecules(prior_user_text) or _extract_req_molecules(prior_all_text)
    prior_receptor = _mentioned_receptor(prior_user_text) or _mentioned_receptor(prior_all_text)

    # ---- 配体 ----
    text = (params.get("ligands_text") or "").strip()
    mol_file = (params.get("molecule_file") or "").strip()
    if not text and not mol_file:
        # 对话模式（未展开高级设置）用的是系统默认参数，params 里 molecule_file 恒为空。
        # 但**上传的分子库文件与 receptor_file 一样是明确的用户意图**，必须随指令下发，
        # 否则「上传成功」在对话里就用不上。真实缺陷 20260917-112206-5017：
        # PGR.sdf（149 个分子）上传成功，协调 Agent 却拿不到绝对路径，只能凭文件名猜
        # （PGR.sdf / assets/cache/PGR.sdf / assets/PGR.sdf…），最终退回示例分子库。
        # 注意：只接**上传附件**的 molecule_file；表单里的 ligands_text 仍按「对话不注入参数」
        # 的既有契约忽略（见 tests/test_intake.py::test_chat_collapsed_...）。
        mol_file = (getattr(req, "molecule_file", "") or "").strip()
    if text:
        ligands = {"source": "text", "count": _count_ligands(text), "text": text, "file": ""}
    elif mol_file:
        ligands = {"source": "file", "count": 0, "text": "", "file": mol_file}
    else:
        # 表单里没有分子，但用户指令里**可能**直接写了 SMILES（"帮我筛一下：华法林 <smiles>"）。
        # 这里先做确定性抽取：能抽到就直接用，绝不静默退回示例分子库 ——
        # 「用户给了分子却对接了示例库」是真实发生过的坑（示例库里是别人的分子）。
        from_message = _extract_req_molecules(message)
        if from_message:
            ligands = {"source": "message", "count": len(from_message), "text": "", "file": "",
                       "molecules": from_message}
            assumptions.append(f"候选分子取自用户指令中出现的 SMILES（{len(from_message)} 个）")
        elif prior_molecules:
            # 多轮：本轮只是回答上一轮的追问（"用 trypsin"/"就这两个"），分子在上一轮里 → 继承
            ligands = {"source": "message", "count": len(prior_molecules), "text": "", "file": "",
                       "molecules": prior_molecules, "inherited": "prior_turns"}
            assumptions.append(f"候选分子继承自上一轮对话（{len(prior_molecules)} 个，来自历史消息中的 SMILES）")
        else:
            ligands = {"source": "library", "count": 0, "text": "", "file": ""}
            if user_requested_example_library(message):
                assumptions.append("用户明确要求使用内置示例库 → "
                                   "import_molecule_library 时传 allow_example_fallback=true")
            else:
                assumptions.append("未提供候选分子：**默认不使用内置示例库**"
                                   "（那是别人的分子；仅当用户明确要求「用示例库」时才传 "
                                   "allow_example_fallback=true）→ 请向用户索取候选分子库")

    # ---- 受体与位点 ----
    uploaded = (getattr(req, "receptor_file", "") or "").strip()
    # 本轮指令里点名的受体：注册表名/PDB 号（确定性 `_mentioned_receptor`）优先；
    # 其次是 UniProt accession（点选候选后的追问会带上它，可直接解析）；
    # 最后才按「…酶/…蛋白/…受体/kinase/receptor」抽取候选名。
    # 只看**用户自己写的正文**：前端拼接的「引用文件」清单里是上传落盘名（时间戳-哈希-原名），
    # 其哈希片段会被误当成受体名（真实缺陷 20260917-122453-0404）。
    instruction = user_instruction_text(message)
    explicit_now = _mentioned_receptor(instruction)
    accession_now = "" if explicit_now else _mentioned_accession(instruction)
    named_now = "" if (explicit_now or accession_now) else _named_receptor_candidate(instruction)
    if accession_now:
        named_now = accession_now
    named_ok, named_resolved = _resolve_receptor_name(named_now) if named_now else (False, "")
    if uploaded:
        receptor = {"name": "", "file": uploaded, "source": "user"}
    elif named_now and not named_ok:
        # 用户**点名了具体受体**（基因名/蛋白名，可能是中文）但不是注册表 key/别名、不是 PDB 号、
        # 不是 UniProt accession，也没有上传受体文件 → 标为 `named`（**待在线自动解析**）。
        # 关键：受理层**不再直接判 ask**，而是把「先自动去 UniProt/RCSB/AlphaFold 查出来」
        # 交给主管 Agent 的第一步（fetch_protein_structure）；只有查不到、或查出多个同样合理
        # 的候选时，Agent 才带着候选清单回来问用户（见 config/agent_llm_config.json 的 3c 条）。
        # 产品底线仍由代码保证：解析失败时工具会把本条规约的 receptor.source 改成 `unresolved`，
        # `run_docking` 护栏随即拒绝任何对接计算（绝不悄悄换成默认受体）。
        receptor = {"name": named_now, "file": "", "source": "named"}
        assumptions = [a for a in assumptions if "默认受体" not in a]
        assumptions.append(
            f"用户点名了受体「{named_now}」→ 先在线自动解析（UniProt 多策略检索 + RCSB/AlphaFold "
            "结构获取），解析成功即继续；失败或出现多个同样合理的候选时再带候选清单请用户选择")
    else:
        name = str(getattr(req, "receptor", "") or "") or DEFAULT_RECEPTOR_NAME
        if authority in ("chat", "chat+advanced") and (message or prior):
            found = explicit_now
            if not found and named_ok:
                found = named_resolved
                assumptions.append(f"从用户指令识别到受体 {named_resolved}")
            if not found and prior_receptor:
                # 多轮：本轮没提受体，但上一轮提过（或上一轮正是被追问的受体）→ 继承
                found = prior_receptor
                assumptions.append(f"受体继承自上一轮对话 → {found}")
            if not found:
                # 多轮续跑：本轮没提受体，但上一轮（含助手回复）里**正好只有一个**已解析的
                # UniProt accession（例如上一轮已把植物 ROS1 查成 Q9SJQ6）→ 继承它。
                # 为什么需要：用户点选「分子代表结构」后的追问通常只带分子，若不继承就会
                # 退化成「未指定受体 → 默认凝血酶」，把已解析好的受体丢掉。
                prior_accessions = _mentioned_accessions(prior_all_text)
                if len(prior_accessions) == 1:
                    found = prior_accessions[0]
                    assumptions.append(f"受体继承自上一轮已解析结果 → {found}")
            if found:
                receptor = {"name": found, "file": "", "source": "user"}
            else:
                receptor = {"name": name, "file": "", "source": "default"}
                assumptions.append("未指定受体 → 由系统内建默认受体兜底"
                                   "（具体受体名以工具返回的 notes/结果块为准，报告里照抄该名称）")
        else:
            if named_ok:
                name = named_resolved
            receptor = {"name": name, "file": "", "source": "user" if authority == "manual" else "default"}
    if params.get("receptor_file") and not uploaded:
        receptor["file"] = params["receptor_file"]

    site = {"center": [], "size": [], "source": "tool"}
    if params.get("site"):
        center, size = params["site"]
        site = {"center": list(center or []), "size": list(size or []), "source": "user"}

    # ---- 阳性对照 ----
    control = (params.get("positive_control") or "").strip()
    skip_control = bool(params.get("skip_positive_control"))
    if skip_control:
        positive_control = {"smiles": "", "provided_by": "skipped"}
        assumptions.append("本次不使用阳性对照（跳过结合模式对照分析）")
    elif control:
        positive_control = {"smiles": control, "provided_by": "user"}
    else:
        positive_control = {"smiles": "", "provided_by": "none"}
        assumptions.append("未提供阳性对照 → 跳过对照分子对接与结合模式比较")

    task_type = _guess_task_type(message) if message else "screening"
    # 是否需要模型理解：只有「对话模式 + 有自然语言」才有价值（manual 由表单驱动，零延迟）
    needs_llm = bool(message) and authority in ("chat", "chat+advanced")
    if not message and authority != "manual":
        needs_llm = False

    spec: Dict[str, Any] = {
        "task_type": task_type,
        "goal": message or DEFAULT_AGENT_TASK,
        "authority": authority,
        "params_note": params_note,
        "decision": DECISION_RUN,
        "receptor": receptor,
        "site": site,
        "ligands": ligands,
        "positive_control": positive_control,
        "params": {k: params.get(k) for k in
                   ("engine", "exhaustiveness", "n_poses", "pocket_engine",
                    "save_poses", "max_ligands", "skip_positive_control")},
        "missing": missing,
        "assumptions": assumptions,
        "questions": questions,
        "out_of_scope": False,
        "raw_request": raw_request or message,
        "current_message": message,
        "prior_turn_count": len(prior),
        "needs_llm": needs_llm,
        "confidence": 1.0 if not needs_llm else 0.6,
        "source": "rules",
    }
    return _finalize(spec)


def _resolvable_ligands(spec: Dict[str, Any]) -> bool:
    """用户是否已经给出了可识别的分子来源（文本/文件/指令中抽取到的 SMILES/名称）。"""
    return (spec.get("ligands") or {}).get("source") in ("text", "file", "mentioned", "message")


def _can_ask(spec: Dict[str, Any]) -> bool:
    """受理层是否允许判 `ask`（唯一的判定入口）。

    三种成立情形（任一满足即 ask）：
      0. **`receptor.source == "unresolved"`**：用户点名了一个无法解析的受体
         （不是注册表 key/别名、不是 PDB 号、不是 UniProt accession，也没有上传受体文件）。
         这是**产品底线**：不明确计算对象时绝不计算，所以它优先于多轮守卫 ——
         哪怕本轮是在回答上一轮的追问（`prior_turns` 非空、分子已继承），照样要停下来问，
         绝不能因为「上一轮问过」就把受体悄悄换成默认值继续算。
      1. 受理模型**明确要求**用户补充（`needs_user_input=true`）；
      2. 当前规约里**没有任何可识别的分子来源**（含从上一轮继承来的分子）。

    **多轮守卫**（只适用于情形 1/2）：当存在 `prior_turns` 且本轮 `message` 非空时，
    用户通常正是在回答上一轮的问题。此时若上一轮已经给出了分子（`build_task_spec` 会把它
    确定性继承进规约），条件 2 不再成立，于是不会再次判 `ask` —— 否则用户每回答一次就被
    重新追问一次，对话永远无法推进。仅当「模型仍要求补充」且「上一轮与本轮都拿不到任何
    分子来源」时，才允许再问一次。
    """
    if str((spec.get("receptor") or {}).get("source") or "") == "unresolved":
        return True
    if not spec.get("needs_user_input"):
        return False
    return not _resolvable_ligands(spec)


def _finalize(spec: Dict[str, Any]) -> Dict[str, Any]:
    """按规约内容推导最终决策（受理层唯一有权改 decision 的地方）。

    注意：**`missing` 本身不构成「不能跑」**。系统对受体、阳性对照、对接参数都有默认值，
    分子也有内置示例库兜底；把「没提供分子」当成阻断会直接违反产品底线
    （输入合法且能对接就必须完整跑完）。因此 `ask` 成立的情形只有两种：
    ① 用户**点名了具体受体但无法解析**（`receptor.source == "unresolved"`，含多轮——
       计算对象不明确时绝不能算）；② 受理模型**明确要求用户补充**（`needs_user_input=true`）
    且用户没有给出任何可识别的分子来源 —— 也就是「用户显然想分析某个特定东西，但我们完全
    无法识别」。多轮语义见 `_can_ask`。
    """
    if spec.get("out_of_scope"):
        spec["decision"] = DECISION_REJECT
    elif _can_ask(spec):
        spec["decision"] = DECISION_ASK
        if spec.get("prior_turn_count"):
            spec["ask_is_followup"] = True     # 可观测：这是多轮里确实无法识别的追问
        if not spec.get("questions"):
            if str((spec.get("receptor") or {}).get("source") or "") == "unresolved":
                name = str((spec.get("receptor") or {}).get("name") or "用户点名的受体")
                spec["questions"] = _unresolved_receptor_questions(name)
            else:
                spec["questions"] = ["请问您想分析哪些分子？（可给名称或 SMILES，或上传分子文件）"]
    else:
        spec["decision"] = DECISION_RUN
        if spec.get("prior_turn_count") and spec.get("current_message") \
                and not spec.get("needs_user_input"):
            spec["multi_turn_answer"] = True
    return spec


# --------------------------------------------------------------------------- #
# 受理模型（可选，仅补白名单字段）
# --------------------------------------------------------------------------- #
INTAKE_SYSTEM_PROMPT = """你是分子筛选系统的「任务受理 Agent」。你的唯一职责是**理解用户到底想要什么**，
不做任何计算、不给出任何数值参数、不编造任何分子。

只输出一个 JSON 对象（不要 Markdown 代码块、不要解释），字段如下（缺省即省略）：
{
  "out_of_scope": false,                       // true = 闲聊/与分子筛选无关（订餐、写诗、天气…）
  "scope_reason": "为何判定越界",
  "task_type": "screening|properties_only|docking_only|binding_only|report_only|unknown",
  "goal": "用一句话复述用户目标",
  "mentioned_molecules": ["原文中逐字出现的分子名称或 SMILES"],
  "mentioned_receptor": "用户点名的受体（逐字：注册表名/PDB 号/UniProt accession/基因或蛋白名称）或 ''",
  "multi_intent": false,                       // 用户是否同时要了多个维度
  "missing": ["仍缺少的必需数据"],
  "questions": ["需要向用户确认的问题（最多 3 条）"],
  "assumptions": ["你的合理假设"],
  "confidence": 0.0
}

硬约束：
1. mentioned_molecules 必须是用户原文里**逐字出现**的名称或 SMILES，不得改写、翻译、补全或推测；
   原文里没提到的分子一律不要写。你不能自己写 SMILES。
2. **不要输出任何运行参数**（exhaustiveness / engine / n_poses / 坐标 / 盒子尺寸 / 阳性对照 SMILES 等）——
   参数由界面表单与系统默认决定，不由你决定。
3. 只有在**确实与分子筛选无关**时才把 out_of_scope 设为 true（闲聊、天气、订餐、写诗…）；
   看不出意图但可能是分子任务时不要判越界。
4. **系统对缺失信息普遍有默认值与兜底，所以「缺东西」通常不是阻断**：
   - 分子缺失 → 由用户补充（**默认不使用内置示例库**；仅当用户明确要求「用示例库」时才用）；
   - 受体缺失 → 用默认受体；阳性对照缺失 → 跳过对照分析；参数缺失 → 用系统默认；
   - 位点坐标缺失 → 由口袋预测工具自动确定。
   因此**不要**因为"用户没给分子/受体/参数"而设置 `needs_user_input`，也不要把它写进 missing。
5. 只有当「用户显然想分析某个**特定**对象，但原文里没有任何可识别的分子（名称/SMILES/文件）」，
   以至于直接执行会答非所问时，才设 `needs_user_input: true` 并在 questions 里给出要问的问题。
6. `missing` 只是**记录**（不影响是否执行），请只写真正无法用默认值补齐的信息。
7. **多轮对话**：输入里可能带有 `上一轮对话`。当用户本轮是在**回答上一轮的追问**（例如上一轮你问了
   「用哪个受体？」，本轮只回了「用 trypsin」）时：**不要**再设 `needs_user_input`、也不要重复追问；
   上一轮已给出的分子/受体/意图请直接沿用，本轮只补上新增信息即可。
8. **受体要如实提取**：用户**点名**了某个受体（哪怕只是一个基因名/蛋白名，如「植物去甲基化酶ROS1」、
   「EGFR」）时，请把该名称**逐字**写进 `mentioned_receptor`（不得改写/翻译/补全）；用户没点名受体时
   留空。**不要**因为你不确定它能否解析就漏掉它 —— 能否解析由受理层规则判定：受理层会把它标为
   `named`（待解析），随后由主管 Agent **自动**去 UniProt/RCSB/AlphaFold 查；只有查不到、或查出
   多个同样合理的候选时才会带着候选清单问用户，因此你**不要**在这里判定「解析不了」。
9. **附件清单不是名称来源**：指令末尾可能有 `[附件清单…]`（上传文件）。那里的文件名/路径片段
   （时间戳、6~12 位十六进制哈希、`PGR_120` 这类带数字的库名）**不是**分子名、更**不是**受体名；
   用户没在正文里点名受体时，`mentioned_receptor` 必须留空（系统会用默认受体并在报告里注明）。"""


def _llm_enabled() -> bool:
    value = str(env("INTAKE_LLM", "on") or "on").strip().lower()
    return value not in ("off", "0", "false", "no", "disabled")


def _verbatim_in(raw: str, value: str) -> bool:
    """判断某个名称/SMILES 是否**逐字**出现在用户原文里（防止模型编造分子）。"""
    needle = re.sub(r"\s+", "", str(value or ""))
    if not needle:
        return False
    haystack = re.sub(r"\s+", "", str(raw or ""))
    return needle.lower() in haystack.lower()


#: 前端在发送前拼进指令的附件清单标题（`web/app.js: appendFileRefs`）。
#: 这段是**机器生成**的：里面的绝对路径含上传落盘名的时间戳/哈希片段，
#: 只能作为工具读文件的线索，绝不能当成「用户点名的受体」。
_ATTACH_REF_RE = re.compile(r"\n*引用文件（[^）]*）：[\s\S]*$")
_ATTACH_LINE_RE = re.compile(r"^-\s*(?P<name>[^（(\n]+?)\s*[（(][^\n]*$", re.MULTILINE)


#: 用户**明确**要求使用内置示例库的表述（默认绝不自动使用示例库/示例受体）
_EXAMPLE_LIB_REQUEST_RE = re.compile(
    r"示例\s*(分子)?\s*库|示例分子|demo\s*librar|example\s*librar|测试(用)?\s*(分子)?库", re.I)


def user_requested_example_library(raw: str) -> bool:
    """用户是否**明确**点了内置示例库（只认用户自己写的正文，附件清单不算）。"""
    return bool(_EXAMPLE_LIB_REQUEST_RE.search(user_instruction_text(raw)))


def user_instruction_text(raw: str) -> str:
    """用户**自己输入**的指令部分（剥掉机器拼接的「引用文件」清单）。"""
    return _ATTACH_REF_RE.sub("", str(raw or "")).strip()


def _llm_instruction_view(raw: str) -> str:
    """给受理模型看的指令：附件清单只留文件名，绝对路径换成系统登记说明。

    真实缺陷 20260917-122453-0404：上传库落盘名 `.../20260917-122453-c6b872-...-PGR_120.sdf`
    里的哈希片段被受理模型当成了「用户点名的受体 C6B872」，随后在线解析失败 → 整个运行被
    阻断成「请选择受体」。路径本来就通过结构化字段（`ligands.file` / `receptor.file`）
    传给编排层，受理模型不需要看到它，因此这里直接不喂。
    """
    text = str(raw or "")
    block = _ATTACH_REF_RE.search(text)
    if block is None:
        return text
    names = [m.group("name").strip() for m in _ATTACH_LINE_RE.finditer(block.group(0))]
    lines = "".join(f"- {n}\n" for n in dict.fromkeys(names) if n)
    return (user_instruction_text(text)
            + "\n\n[附件清单（仅文件显示名；真实路径已由系统登记在结构化字段里，"
              "不要据此推断受体名或分子名）]\n" + lines).strip()


def merge_llm_understanding(spec: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """把受理模型的输出合并进规约：只接受白名单字段，且逐条校验。"""
    merged = dict(spec)
    merged["source"] = "rules+llm"
    notes: List[str] = []

    for field in LLM_ALLOWED_FIELDS:
        if field in payload and payload[field] not in (None, "", [], {}):
            merged[f"llm_{field}"] = payload[field]
    for field in LLM_FORBIDDEN_FIELDS:
        if field in payload:
            notes.append(f"忽略模型给出的受限字段 {field}")

    out_of_scope = payload.get("out_of_scope")
    if isinstance(out_of_scope, bool):
        merged["out_of_scope"] = out_of_scope
        if out_of_scope:
            merged["scope_reason"] = str(payload.get("scope_reason") or "").strip()

    task_type = str(payload.get("task_type") or "").strip()
    if task_type in TASK_TYPES and task_type != "unknown":
        merged["task_type"] = task_type        # unknown 不覆盖确定性推断，避免编排退化成观望
    goal = str(payload.get("goal") or "").strip()
    if goal:
        merged["goal"] = goal

    # 分子：必须逐字出现在原文里（模型不得编造）
    raw = str(spec.get("raw_request") or "")
    extracted: List[str] = []
    dropped: List[str] = []
    for item in payload.get("mentioned_molecules") or []:
        text = str(item or "").strip()
        if not text:
            continue
        (extracted if _verbatim_in(raw, text) else dropped).append(text)
    if dropped:
        merged.setdefault("llm_notes", []).append(f"丢弃原文中不存在的分子：{dropped[:3]}")
    if extracted:
        merged["mentioned_molecules"] = extracted
        ligands = dict(merged.get("ligands") or {})
        if ligands.get("source") == "library":
            ligands = {"source": "mentioned", "count": len(extracted), "text": "",
                       "file": "", "extracted": extracted}
            merged["ligands"] = ligands
            merged["assumptions"] = [a for a in merged.get("assumptions") or []
                                     if "未提供候选分子" not in a and "示例分子库" not in a]
            merged["assumptions"].append(
                "用户指令提到分子但未给 SMILES → 先按名称在线查询（fetch_molecule_record）再对接")

    mentioned_receptor = str(payload.get("mentioned_receptor") or "").strip()
    # 受体名只认**用户自己写的**指令：附件清单里的路径/落盘名（时间戳-哈希-原名）
    # 不是受体来源（真实缺陷 20260917-122453-0404：哈希片段 C6B872 被当成受体并阻断运行）。
    user_text = user_instruction_text(raw)
    if mentioned_receptor and _verbatim_in(user_text, mentioned_receptor):
        receptor = dict(merged.get("receptor") or {})
        # 去掉「这个/该/the」这类指示代词：它们不是用户点名的受体
        named = _strip_demonstrative(mentioned_receptor)
        if not _is_generic_receptor_name(named) and not receptor.get("file"):
            resolvable, resolved = _resolve_receptor_name(named)
            if resolvable:
                # 注册表名/别名、PDB 号、UniProt accession 形状 → 交给真实工具解析
                if str(receptor.get("source") or "") in ("", "default") \
                        or str(receptor.get("name") or "") != resolved:
                    receptor.update({"name": resolved, "source": "user"})
                    merged["assumptions"] = [a for a in merged.get("assumptions") or []
                                             if "默认受体" not in a]
            else:
                # 用户点名了却不在注册表/不是 PDB/accession → 标为 `named`（待在线自动解析），
                # 由主管 Agent 第一步调用 fetch_protein_structure 去 UniProt/RCSB/AlphaFold 查；
                # 查不到或出现多个同样合理的候选时才带候选清单回来问用户（不再一上来就 ask）。
                receptor.update({"name": named, "source": "named"})
                merged["assumptions"] = [a for a in merged.get("assumptions") or []
                                         if "默认受体" not in a]
                merged["assumptions"].append(
                    f"用户点名了受体「{named}」→ 先在线自动解析（UniProt 多策略检索 + RCSB/AlphaFold "
                    "结构获取），解析成功即继续；失败或出现多个同样合理的候选时再带候选清单请用户选择")
        merged["receptor"] = receptor
    elif mentioned_receptor:
        # 如实记录丢弃原因（不静默）：要么模型编造，要么只是从附件路径里抄的片段
        reason = ("只出现在附件路径里，附件清单不作为受体来源"
                  if _verbatim_in(raw, mentioned_receptor) else "原文中不存在")
        merged.setdefault("llm_notes", []).append(
            f"忽略受体名「{mentioned_receptor}」（{reason}）：按未点名受体处理")

    if payload.get("needs_user_input") is True:
        merged["needs_user_input"] = True
    for key in ("missing", "questions", "assumptions"):
        extra = [str(x).strip() for x in (payload.get(key) or []) if str(x).strip()]
        if extra:
            merged[key] = list(dict.fromkeys((merged.get(key) or []) + extra))
    confidence = payload.get("confidence")
    if isinstance(confidence, (int, float)):
        merged["confidence"] = max(0.0, min(1.0, float(confidence)))
    if payload.get("multi_intent"):
        merged.setdefault("llm_notes", []).append("用户同时要求多个分析维度 → 按综合筛选执行")
    if notes:
        merged.setdefault("llm_notes", []).extend(notes)

    return _finalize(merged)


def refine_task_spec(spec: Dict[str, Any], *, timeout: Optional[float] = None,
                     prior_turns: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """在需要时调用受理模型细化规约；任何失败都退回确定性规约（绝不中断运行）。

    `prior_turns` 只是**只读上下文**：让模型知道「本轮是在回答上一轮」，从而不再重复追问；
    它同样不能据此产出参数值（白名单/受限字段校验保持不变）。
    """
    if not spec.get("needs_llm") or not _llm_enabled():
        return spec
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from docking_agent.runtime.llm import build_chat_llm

        llm = build_chat_llm(None, role="intake")
        context = {
            "用户指令": _llm_instruction_view(spec.get("raw_request") or ""),
            "判定模式": spec.get("authority"),
            "表单/默认参数（权威，不要修改）": {
                "受体": (spec.get("receptor") or {}).get("name"),
                "配体来源": (spec.get("ligands") or {}).get("source"),
                "是否给出位点坐标": bool((spec.get("site") or {}).get("center")),
                "阳性对照": (spec.get("positive_control") or {}).get("provided_by"),
            },
            "系统提示": "无位点坐标时系统会用口袋预测工具自动定盒；内置示例库只在用户明确要求时使用，不要默认引用。",
        }
        prior = _normalize_prior_turns(prior_turns)
        if prior:
            # 只读上下文：不得据此修改任何参数，也不得编造分子
            context["上一轮对话（只读上下文，不要修改任何参数）"] = prior[-8:]
            context["系统提示"] += " 若本轮指令是在回答上一轮的追问，请直接沿用上一轮已给出的信息，不要重复追问。"
        reply = llm.invoke([
            SystemMessage(content=INTAKE_SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(context, ensure_ascii=False)),
        ])
        text = getattr(reply, "content", "") or ""
        payload = parse_json_object(text if isinstance(text, str) else str(text))
        if payload is None:
            logger.warning("受理模型输出无法解析为 JSON，沿用确定性规约")
            fallback = dict(spec)
            fallback["llm_status"] = "invalid_json"
            return fallback
        refined = merge_llm_understanding(spec, payload)
        refined["llm_status"] = "ok"
        if refined.get("out_of_scope"):
            logger.info("受理判定为超出系统能力：%s", refined.get("scope_reason", ""))
        return refined
    except Exception as e:  # noqa: BLE001
        logger.warning("受理模型调用失败（沿用确定性规约）：%s", e)
        fallback = dict(spec)
        fallback["llm_status"] = f"error: {type(e).__name__}"
        return fallback


# --------------------------------------------------------------------------- #
# 渲染给编排层的消息
# --------------------------------------------------------------------------- #
def _render_params(params: Dict[str, Any], spec: Dict[str, Any], header: str,
                   run: Any = None) -> str:
    """渲染「运行参数」段（保持既有措辞，避免破坏已有契约与测试）。"""
    lines: List[str] = [header]
    receptor = spec.get("receptor") or {}
    if receptor.get("file"):
        lines.append(f"受体：用户上传受体文件 {receptor['file']}")
    elif str(receptor.get("source") or "") == "named":
        # 用户点名了受体（基因名/蛋白名/中文名）但受理层不做网络解析 → 交给主管 Agent
        # 第一步用 fetch_protein_structure 自动去 UniProt/RCSB/AlphaFold 查出来，再继续完整流程。
        lines.append(
            f"受体：用户点名了「{receptor.get('name')}」——**受理层尚未解析，需要你第一步自动在线解析**。"
            "请立即调用 `fetch_protein_structure(source=\"" + str(receptor.get("name")) + "\")`："
            "① 返回 status=ok → 用它返回的 receptor_file 作为受体继续完整流程，并在报告与最终回复里"
            "写明 accession、物种、蛋白名、结构来源（rcsb/alphafold）、结构 URL 与选择依据；"
            "② 返回 status=ambiguous 或 low_confidence → **不要自行挑选**，向用户说明「检索到多个/"
            "置信度不足的候选」，逐条列出返回的 candidates（accession / 物种 / 蛋白名 / 结构来源 / 打分），"
            "让用户在界面给出的选项中挑选，**此时不做任何对接计算**；"
            "③ 返回 status=error/not_found → 不要对接，向用户提问并逐条列出已尝试的检索"
            "（UniProt accession/基因名/蛋白名、RCSB、AlphaFold）与找到的候选（若有）。"
            "**任何情况下都不得改用系统默认受体开跑。**")
    elif str(receptor.get("source") or "") == "unresolved":
        # 产品底线：用户**点名**的受体解析不了时，计算对象不明确 → 不许对接、不许回退默认。
        # 渲染成明确的「停下提问」指令，配合 decision=ask 阻止编排层继续算。
        lines.append(
            f"受体：**无法解析用户点名的受体「{receptor.get('name')}」**"
            "（不是注册表受体/PDB 号/UniProt accession，也没有上传受体文件；"
            "已尝试 UniProt accession 直查与基因/蛋白名称检索，均无匹配）。"
            "**不要对接、不要回退默认受体、不要臆造结构** —— 请只向用户提问并给出三条出路："
            "① 提供 PDB ID（如 1DWC）；② 上传受体文件（.pdb/.pdbqt）；"
            "③ 明确同意改用系统默认受体 凝血酶（thrombin, 1DWC）。")
    elif str(receptor.get("source") or "") == "default":
        # 关键：`source=="default"` 表示**受理层没从指令/表单拿到受体**，这个名字只是兜底默认值。
        # 旧实现无论来源一律渲染成「受体：thrombin」的权威口吻，编排层于是把示例默认受体
        # 当成用户指定，用户说「用 trypsin」也被覆盖（真实缺陷）。这里必须用弱表述，并明确
        # 要求把 receptor_sources 留空，交由对接工具回退默认（回退时工具会写「未指定受体」note）。
        fallback = receptor.get("name") or DEFAULT_RECEPTOR_NAME
        if fallback == DEFAULT_RECEPTOR_NAME:
            # 不点名具体受体（默认资源不进入 Agent 视野）：名字由工具在运行时给出，报告照抄即可。
            lines.append(
                "受体：**指令未指定**（受理层未从指令中识别到受体；系统会按内建默认受体兜底，"
                "**不是**用户指定的受体）。调用 run_pocket_analysis / run_docking 时，"
                "请把 receptor_sources 与 receptor_file 留空，由工具回退系统默认 —— "
                "工具会在 notes 里写明「未指定受体，已默认使用 <受体名>」，"
                "你必须在最终回复里**照抄工具给出的受体名**，不要自己假定。"
                "若上面的用户指令其实指定了别的受体（名称/PDB 号/文件），以指令为准。")
        else:
            lines.append(
                f"受体：**指令未指定**（{fallback} 来自高级设置，只是指令没提受体时的默认值，"
                "**不是**用户指令）。若指令未指定受体，可用 " + fallback +
                "；若指令里明确提到了其它受体，以指令为准。")
    else:
        lines.append(f"受体：{receptor.get('name') or DEFAULT_RECEPTOR_NAME}")
    site = spec.get("site") or {}
    if site.get("center"):
        size = site.get("size") or [22, 22, 22]
        lines.append("已知结合位点：盒中心 site_center="
                     f"{','.join(str(x) for x in site['center'])}，"
                     f"盒尺寸 site_size={','.join(str(x) for x in size)}"
                     "（请原样传给对接工具，不要自行修改）")
    else:
        engine = (params.get("pocket_engine") or "").strip() or "auto"
        lines.append("已知结合位点：**未指定坐标** —— 请先调用 run_pocket_analysis 让「口袋分析 Agent」"
                     f"用真实工具预测结合口袋并选定对接盒（pocket_engine={engine}），"
                     "它会通过共享黑板把盒子交给 Docking 子 Agent；随后 run_docking 不要自行编造坐标。")
    exh = params.get("exhaustiveness")
    poses = params.get("n_poses")
    # 「系统默认」场景下搜索强度本来就是自动规划（下方会给建议值），不要渲染成一个"固定 16"，
    # 否则用户会以为系统替他指定了强度（真实反馈：我没有指定高级参数，怎么传进来了）。
    system_default = str(spec.get("params_note") or "").startswith("系统默认")
    lines.append(
        "对接参数：搜索强度 exhaustiveness="
        + (f"{exh}" if (exh is not None and not system_default)
           else "**自动（按库柔性/盒体积规划，基准 16）**")
        + "，n_poses=" + ("默认（筛选 1）" if (poses is None or system_default) else f"{poses}")
        + "，engine=" + ("auto" if system_default else str(params.get("engine") or "auto"))
        + f"，pocket_engine={(params.get('pocket_engine') or 'auto')}")
    control = spec.get("positive_control") or {}
    if control.get("provided_by") == "skipped":
        lines.append("阳性对照：本次不使用（跳过结合模式对照分析）")
    elif control.get("smiles"):
        lines.append(f"阳性对照：{control['smiles']}")
    else:
        lines.append("阳性对照：未提供（本次跳过对照分子对接与结合模式比较；"
                     "不要为此停下询问，直接完成其余步骤）")
    ligands = spec.get("ligands") or {}
    if ligands.get("source") == "text" and ligands.get("text"):
        count = ligands.get("count") or _count_ligands(ligands["text"])
        inline_max = 4000
        if len(ligands["text"]) <= inline_max and count <= 20:
            lines.append(f"候选分子库（{count} 条，格式 名称:SMILES）：{ligands['text']}")
        else:
            lines.append(_render_big_library(ligands["text"], count, run))
    elif ligands.get("source") == "file" and ligands.get("file"):
        # 绝对路径必须**原样**进入指令：协调 Agent 不得自行拼接或猜测路径
        # （真实缺陷 20260917-112206-5017 就是猜了 4 个错误路径后放弃）。
        lines.append(
            f"候选分子库来自文件（绝对路径，请**原样**传给 import_molecule_library 的 molecule_file，"
            f"不要自行拼接或猜测路径）：{ligands['file']}。"
            "请立即调用 import_molecule_library(molecule_file=\"" + str(ligands["file"]) +
            "\") 导入（它会写入共享黑板，后续子 Agent 工具留空参数即可使用全量分子）。")
    elif ligands.get("source") == "message" and ligands.get("molecules"):
        joined = "；".join(ligands["molecules"])
        lines.append(f"候选分子库（{ligands.get('count') or len(ligands['molecules'])} 条，"
                     f"取自用户指令，格式 名称:SMILES）：{joined}")
    elif ligands.get("source") == "mentioned" and ligands.get("extracted"):
        lines.append("候选分子：用户在指令中提到了 " + "、".join(ligands["extracted"]) +
                     "（**没有给出 SMILES**：请先用 fetch_molecule_record 按名称查询拿到 SMILES，"
                     "再导入分子库；不要臆造 SMILES）")
    else:
        if user_requested_example_library(spec.get("raw_request") or ""):
            lines.append("候选分子库：用户**明确要求使用内置示例库** → "
                         "调用 import_molecule_library 时传 allow_example_fallback=true。")
        else:
            lines.append("候选分子库：**未提供**。不要擅自使用内置示例库（那是别人的分子）；"
                         "如用户明确要求「用示例库」才在 import_molecule_library 里传 "
                         "allow_example_fallback=true；否则请向用户索取候选分子库。")

    # 参数自动规划：把规划结果作为**建议参数**写进指令，让 Agent 路径与流水线口径一致。
    # 规划成功时它同时覆盖「大库打法」（两阶段漏斗口径一致），避免两条建议打架。
    planned = _planned_params_advice(spec, params)
    if planned:
        lines.append(planned)
    else:
        # 拿不到分子清单时的兜底：只按分子数给出漏斗建议（阈值/参数来自 AGENT_* 环境变量）
        count = int(ligands.get("count") or 0)
        if not count and ligands.get("molecules"):
            count = len(ligands["molecules"])
        if count:
            try:
                from docking_agent.agents.tool_io import funnel_advice

                advice = funnel_advice(count)
                if advice:
                    lines.append("大库打法：" + advice)
            except Exception:  # noqa: BLE001
                logger.debug("生成漏斗建议失败", exc_info=True)
    return "\n".join(lines)


def _ligand_molecules(spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """从规约里尽力拿到分子清单（只做确定性解析，不联网、不读 URL）。"""
    ligands = spec.get("ligands") or {}
    source = ligands.get("source")
    if source == "text" and ligands.get("text"):
        try:
            from docking_agent.core import parse_smiles_text

            return parse_smiles_text(ligands["text"])
        except Exception:  # noqa: BLE001
            logger.debug("解析候选分子库失败", exc_info=True)
            return []
    if source in ("message", "mentioned"):
        out: List[Dict[str, str]] = []
        for item in ligands.get("molecules") or ligands.get("extracted") or []:
            text = str(item or "").strip()
            if not text:
                continue
            if ":" in text:
                name, _, smi = text.partition(":")
                if smi.strip():
                    out.append({"name": name.strip() or smi.strip(), "smiles": smi.strip()})
            elif " " in text:
                smi, _, name = text.partition(" ")
                if smi.strip():
                    out.append({"name": name.strip() or smi.strip(), "smiles": smi.strip()})
        return out
    return []


def _planned_params_advice(spec: Dict[str, Any], params: Dict[str, Any]) -> str:
    """把 `core/params.plan_docking_params` 的结果渲染成「建议参数」一行。

    - 只有能确定分子来源（指令里给出了 SMILES）时才规划；否则保持既有措辞不引入矛盾。
    - `manual` / `chat+advanced` 的表单值视为**用户显式指定**（不自动改，只记录）；
      `chat` 折叠时表单不生效，按纯规则规划。
    """
    molecules = _ligand_molecules(spec)
    if not molecules:
        return ""
    explicit = spec.get("authority") in ("manual", "chat+advanced")
    # 只有**真正给了数值**的字段才算用户参数（留空 = 自动规划，不能被当成"用户指定了 None"）
    user_params = ({k: v for k, v in (("exhaustiveness", params.get("exhaustiveness")),
                                      ("n_poses", params.get("n_poses"))) if v is not None}
                   if explicit else {})
    site = spec.get("site") or {}
    try:
        from docking_agent.core.params import plan_docking_params

        plan = plan_docking_params(
            task_type=spec.get("task_type") or "screening", molecules=molecules,
            box_size=site.get("size") or None, user_params=user_params, pilot=None)
    except Exception:  # noqa: BLE001
        logger.debug("参数自动规划失败（指令退化为系统默认）", exc_info=True)
        return ""
    if plan.get("exhaustiveness") is None:
        return ""
    spec["param_plan"] = plan
    parts = [f"exhaustiveness={plan['exhaustiveness']}", f"n_poses={plan['n_poses']}",
             f"engine={plan['engine']}", f"seed={plan['seed']}"]
    if plan.get("two_stage"):
        from docking_agent.config import env_int

        funnel_min = max(0, env_int("AGENT_FUNNEL_MIN", 500))
        parts.append(f"候选数 {plan['library_size']} ≥ {funnel_min} → "
                     f"两阶段漏斗（先全库粗筛 exhaustiveness={plan['coarse_exhaustiveness']}，"
                     f"再对头部 top_from_previous={plan['refine_top_n']} 用 "
                     f"exhaustiveness={plan['exhaustiveness']} 精算）")
    else:
        parts.append(f"候选数 {plan['library_size']} → 单阶段（跑全库）")
    text = ("本次建议参数（自动规划，运行级/阶段级；同一阶段内所有分子参数一致，"
            "不要逐分子改动）：" + "，".join(parts))
    if plan.get("source") == "user":
        text += "。用户已在表单/高级设置里显式指定的参数**不再改动**（source=user），以上仅为执行口径。"
    return text


def _render_big_library(text: str, count: int, run: Any) -> str:
    """上万条清单不能内嵌进指令（1 万条 ≈ 0.5 MB ≈ 13 万 tokens）。

    改为写入运行目录的分子文件，消息里只给条数、前几条预览与文件路径，
    由协调 Agent 用 import_molecule_library(molecule_file=...) 导入（该工具会写进共享黑板）。
    """
    preview = []
    for line in str(text).replace("；", "\n").replace(";", "\n").replace("，", "\n").splitlines():
        item = line.strip()
        if item:
            preview.append(item)
        if len(preview) >= 5:
            break
    path = ""
    if run is not None:
        try:
            # 写成 CSV（name,smiles 两列）：解析无歧义，且两种读取入口都支持
            import csv as _csv
            import io as _io

            buf = _io.StringIO()
            writer = _csv.writer(buf)
            writer.writerow(["name", "smiles"])
            for raw_line in str(text).replace("；", "\n").replace(";", "\n").splitlines():
                item = raw_line.strip()
                if not item:
                    continue
                if ":" in item:
                    name, _, smi = item.partition(":")
                    writer.writerow([name.strip() or smi.strip(), smi.strip()])
                elif "," in item:
                    name, _, smi = item.partition(",")
                    writer.writerow([name.strip() or smi.strip(), smi.strip()])
                else:
                    writer.writerow([item, item])
            rel = "ligands_input.csv"
            run.write_text(rel, buf.getvalue(), name="ligands_input",
                           label=f"候选分子库输入（{count} 条）",
                           content_type="text/csv; charset=utf-8")
            path = str(run.dir / rel)
        except Exception as e:  # noqa: BLE001
            logger.warning("候选分子库落盘失败：%s", e)
    if path:
        return (f"候选分子库：共 **{count} 条**，已写入文件 `{path}`。"
                f"请调用 import_molecule_library(molecule_file=\"{path}\") 导入（它会同时写入共享黑板，"
                "后续子 Agent 工具留空参数即可使用全量分子）。预览（前 5 条）：" + "；".join(preview)
                + "。**不要**把全量清单复制进其它工具的入参。")
    return (f"候选分子库：共 {count} 条（清单过长，未内嵌）。预览（前 5 条）："
            + "；".join(preview) + "。请先用 import_molecule_library 导入。")


def render_agent_message(spec: Dict[str, Any], run: Any = None) -> str:
    """把任务规约渲染成发给编排层的消息（含**职责边界**与 `decision` 语义）。"""
    authority = spec.get("authority")
    if authority == "chat":
        header = "--- 默认运行参数（系统默认；如上面的指令中已明确指定，以指令为准）---"
    elif authority == "chat+advanced":
        header = "--- 默认运行参数（来自高级设置；如上面的指令中已明确指定，以指令为准）---"
    else:
        header = ("--- 本次运行参数（由界面设定，本次任务的权威参数；"
                  "如与上面的描述冲突，以本节为准，并在回答中说明已按参数执行）---")

    params = spec.get("params") or {}
    lines: List[str] = [spec.get("goal") or DEFAULT_AGENT_TASK, ""]

    lines.append("--- 任务规约（受理层产出；编排层只读，不要修改）---")
    lines.append(f"task_type={spec.get('task_type')}　authority={authority}　"
                 f"decision={spec.get('decision')}　confidence={spec.get('confidence')}")
    if spec.get("assumptions"):
        lines.append("假设：" + "；".join(str(a) for a in spec["assumptions"][:5]))
    if spec.get("missing"):
        lines.append("缺少：" + "；".join(str(m) for m in spec["missing"][:5]))
    if spec.get("questions"):
        lines.append("待向用户确认：" + "；".join(str(q) for q in spec["questions"][:3]))
    lines.append("")

    lines.append(_render_params(params, spec, header, run))
    lines.append("")

    lines.append("--- 你的职责（编排层）---")
    lines.append("只负责「怎么做到」：选择受体与位点（未给坐标时先 run_pocket_analysis）、"
                 "决定子 Agent 的调用顺序与并行、失败时重试或如实上报、按 smiles 合并与排序、"
                 "对接完成后先 recommend_compounds 拿综合分排行、再 submit_recommendations 写逐分子理由，"
                 "最后调用 generate_screening_report。**不要重新解析用户语言，也不要修改上面的参数。**")
    if spec.get("decision") == DECISION_REJECT:
        lines.append("decision=reject：这是受理层的判定 —— 用户输入超出本系统能力。"
                     "**不要调用任何工具**，只用一两句话友好说明本系统能做什么并引导用户提供分子筛选需求。")
    elif spec.get("decision") == DECISION_ASK:
        if str((spec.get("receptor") or {}).get("source") or "") == "unresolved":
            lines.append(
                "decision=ask：这是受理层的判定 —— **用户点名的受体无法解析**，计算对象不明确。"
                "**不要调用任何工具**（尤其不要 run_docking / molecular_docking，也不要改用默认受体）；"
                "只用一两句话说明已尝试 UniProt accession 直查与基因/蛋白名称检索均无匹配，"
                "并请用户三选一：① 提供 PDB ID；② 上传 .pdb/.pdbqt 受体文件；"
                "③ 明确同意改用系统默认受体 凝血酶(thrombin, 1DWC)。"
                "**不得把「已用默认受体跑完」当成完成任务。**")
        else:
            lines.append("decision=ask：这是受理层的判定 —— 缺少必需数据。"
                         "**不要调用任何工具**，只向用户说明缺什么并请他补充（这不算「中途停下」，"
                         "而是受理层已经做出的决定）。")
    else:
        lines.append("decision=run：受理层已确认输入合法 —— 只要候选分子库非空，就必须完整执行"
                     "「导入 → 属性评估 → 对接 → 结合模式分析（有阳性对照时）→ 生成报告」，"
                     "**不得在中途停下来询问用户**，也不得只完成其中一步。")
        if str((spec.get("receptor") or {}).get("source") or "") == "named":
            lines.append(
                "**受体解析是唯一例外**：上面点名的受体字面上不是注册表受体/PDB 号/UniProt accession，"
                "必须先调用 fetch_protein_structure 自动解析后再继续；只有「解析出多个同样合理的候选 / "
                "置信度不足」或「所有自动检索都失败」时，才允许停下来让用户从候选清单里选择 —— "
                "这不算「中途询问」，而是计算对象不明确时的必要确认。**绝不允许退化成默认受体开跑。**")
    return "\n".join(lines)


def build_message(req: Any, *, allow_llm: bool = True, run: Any = None,
                  prior_turns: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, Dict[str, Any]]:
    """受理入口：返回 (给编排层的消息, 任务规约)。

    `run` 用于把超大清单落到运行目录；`prior_turns`（可选）是同一会话的最近若干轮消息，
    用于确定性继承上一轮的分子/受体，并让受理模型知道本轮是在回答上一轮。
    """
    spec = build_task_spec(req, prior_turns=prior_turns)
    if allow_llm:
        spec = refine_task_spec(spec, prior_turns=prior_turns)
    message = render_agent_message(spec, run)
    # 参数自动规划的建议（若已算出）落进运行记录，供报告「参数自动规划」一节展示
    if run is not None and spec.get("param_plan"):
        run.data["param_plan"] = spec["param_plan"]
    return message, spec


def compose_agent_message(req: Any, *, allow_llm: bool = True) -> str:
    """兼容既有调用：只取消息。"""
    return build_message(req, allow_llm=allow_llm)[0]
