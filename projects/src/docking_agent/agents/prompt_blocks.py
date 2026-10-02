"""协调 Agent 的条件纪律段：按本次运行的实际情况只注入用得上的小节。

系统提示词在每一次模型调用上重发。「执行纪律」一节占 sp 的 55%（6,181 字符 ≈ 3,960
tokens），而一次运行通常只用得到其中几条：例如「特殊体系（金属/辅因子）」只在受体
掉过杂原子时相关，「两阶段漏斗」只在大库时相关，「用户上传文件处理」只在有上传时相关；
注入未用到的段落会持续占用上下文预算。

提示词文本只有一份来源（`config/agent_llm_config.json` 的 `sp`），条件段以 HTML 注释
`block:KEY` / `end:KEY` 标记包起来（对应下文 `_START` / `_END` 正则）。组装时按条件抽掉不相关的段：

- 配置里没有标记（未分段或标记被删）：原样返回全文；
- 条件判断拿不到事实：注入该段（安全优先）；
- 组装函数内部出现异常：返回全文。

注入结果记进 `run.data["prompt_blocks"]`（随 run.json 落盘），
据此可查本次运行实际注入的纪律段。

对外接口：`parse_blocks` / `strip_blocks` / `unwrap_blocks` 负责标记解析与剥离，
`collect_facts` / `select_blocks` 决定注入集合，`assemble` 完成组装并返回注入信息。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, Set, Tuple

from docking_agent.config import env_int
from docking_agent.runtime import run_facts

logger = logging.getLogger(__name__)

#: 所有条件段（键到说明文字的映射），同时是配置里必须出现的标记集合
BLOCKS: Dict[str, str] = {
    "upload_files": "用户上传文件处理（只在真有上传时相关）",
    "receptor_discipline": "受体纪律 3c（没提受体 / 点名待解析 / 解析失败）",
    "funnel_two_stage": "大库两阶段漏斗（只在库够大且指令里没有规划建议时相关）",
    "special_systems": "特殊体系：金属/辅因子/配体特殊化学（只在体系不干净时相关）",
}

_START = re.compile(r"<!--\s*block:([a-z_]+)\s*-->\n?")
_END = re.compile(r"<!--\s*end:([a-z_]+)\s*-->\n?")


def parse_blocks(text: str) -> Tuple[str, Dict[str, str]]:
    """把带标记的提示词拆成 `(无标记正文, {key: 该段的原文（含标记）})`。

    标记须成对且不交叉；解析不出成对标记的键按「没有条件段」处理（返回空 dict），
    由调用方退回全文。
    """
    found: Dict[str, str] = {}
    for match in _START.finditer(text):
        key = match.group(1)
        end = _END.search(text, match.end())
        if end is None or end.group(1) != key:
            continue
        found.setdefault(key, text[match.start():end.end()])
    return text, found


def strip_blocks(text: str, keys: Iterable[str]) -> str:
    """从提示词里删掉指定的条件段（标记一并删除）。

    仅当所有给定键都能成对解析时才执行删除，否则原样返回，
    即宁可多付 token，也不丢纪律段。
    """
    out = text
    for key in keys:
        start = re.search(rf"<!--\s*block:{re.escape(key)}\s*-->\n?", out)
        if start is None:
            return text
        end = _END.search(out, start.end())
        if end is None or end.group(1) != key:
            return text
        out = out[:start.start()] + out[end.end():]
    return out


def unwrap_blocks(text: str) -> str:
    """删掉保留段的标记（标记只用于组装，不进入模型看到的提示词）。"""
    return _END.sub("", _START.sub("", text))


def funnel_min() -> int:
    """两阶段漏斗的触发阈值（与 `runtime.tool_io.funnel_settings` 同源的环境变量）。"""
    return max(0, env_int("AGENT_FUNNEL_MIN", 500))


def collect_facts(run: Any) -> Dict[str, Any]:
    """收集条件段需要的全部事实（未知项用 `None` 表示，按注入处理）。"""
    data = getattr(run, "data", None) if run is not None else None
    if not isinstance(data, dict):
        return {}
    spec = data.get("task_spec") if isinstance(data.get("task_spec"), dict) else None
    plan = data.get("param_plan") if isinstance(data.get("param_plan"), dict) else {}
    request = data.get("request") if isinstance(data.get("request"), dict) else {}
    facts: Dict[str, Any] = dict(run_facts.read(run))
    if spec is None:
        # 没有规约（CLI / 早期阶段）时关键事实留 None（等同未知，按注入处理）
        facts.setdefault("has_upload", None)
        facts.setdefault("receptor_pending", None)
    else:
        ligands = spec.get("ligands") if isinstance(spec.get("ligands"), dict) else {}
        receptor = spec.get("receptor") if isinstance(spec.get("receptor"), dict) else {}
        facts.setdefault("has_upload", bool(
            ligands.get("file") or receptor.get("file")
            or request.get("molecule_file") or request.get("receptor_file")))
        facts.setdefault("receptor_pending",
                         str(receptor.get("source") or "") in ("default", "named"))
    size = plan.get("library_size")
    if size is None and spec is not None:
        ligands = spec.get("ligands") if isinstance(spec.get("ligands"), dict) else {}
        try:
            size = int(ligands.get("count") or 0)
        except (TypeError, ValueError):
            size = 0
    facts["library_size"] = int(size or 0)
    facts["has_plan"] = bool(plan.get("exhaustiveness") is not None or plan.get("two_stage"))
    return facts


def select_blocks(facts: Dict[str, Any]) -> Set[str]:
    """按给定事实决定注入哪些条件段（返回要保留的键）。"""
    keep: Set[str] = set()
    # 上传处理：仅在确认没有上传时跳过
    if facts.get("has_upload") is not False:
        keep.add("upload_files")
    # 受体纪律 3c：仅在受体已落实（使用者上传 / 表单给定）时跳过
    if facts.get("receptor_pending") is not False:
        keep.add("receptor_discipline")
    # 两阶段漏斗：指令已带自动规划建议，或库明显不大时，段里的口径已在指令中或无需使用
    threshold = funnel_min()
    small = 0 < facts.get("library_size", 0) < threshold
    if not (facts.get("has_plan") or small or threshold == 0):
        keep.add("funnel_two_stage")
    # 特殊体系：仅在已有一份干净对接结果时跳过
    if not (facts.get("docking_seen") and not facts.get("hetero_atoms")
            and not facts.get("special_chemistry")):
        keep.add("special_systems")
    return keep


def assemble(sp: str, run: Any) -> Tuple[str, Dict[str, Any]]:
    """返回 `(本次该用的系统提示词, 注入信息)`；任何异常都退回全文。"""
    try:
        plain = unwrap_blocks(sp)     # 无论走哪条路，给模型看的都不带组装标记
        _body, found = parse_blocks(sp)
        if not found:
            return plain, {"blocks": "none", "reason": "提示词里没有条件段标记（配置未分段）"}
        facts = collect_facts(run)
        keep = select_blocks(facts)
        drop = [key for key in found if key not in keep]
        text = strip_blocks(sp, drop) if drop else sp
        text = unwrap_blocks(text)
        if len(text.strip()) < 200:            # 抽没了必然是解析出了问题
            return plain, {"blocks": "none", "reason": "抽掉条件段后提示词过短，已退回全文"}
        info = {
            "injected": sorted(keep & set(found)),
            "skipped": sorted(set(drop)),
            "full_chars": len(sp),
            "used_chars": len(text),
            "facts": {k: facts.get(k) for k in
                      ("has_upload", "receptor_pending", "library_size", "has_plan",
                       "docking_seen", "hetero_atoms", "special_chemistry")},
        }
        return text, info
    except Exception as e:                     # noqa: BLE001 - 组装失败退回全文，纪律段不丢失
        logger.warning("条件提示词组装失败（退回全文）：%s", e)
        return unwrap_blocks(sp), {"blocks": "none", "reason": f"组装失败：{e}"}


def record(run: Any, info: Dict[str, Any]) -> None:
    """把本次注入情况写进 `run.data`（随 run.json 落盘，供事后核对）。"""
    data = getattr(run, "data", None)
    if not isinstance(data, dict):
        return
    try:
        data["prompt_blocks"] = info
    except Exception as e:                     # noqa: BLE001
        logger.debug("记录注入情况失败：%s", e)


def coordinator_prompt_middleware(base_sp: str) -> Any:
    """构建协调 Agent 的动态系统提示词中间件（每次模型调用按运行事实组装）。"""
    from langchain.agents.middleware import dynamic_prompt

    from docking_agent.runtime.context import active_run

    @dynamic_prompt
    def conditional_discipline(request: Any) -> str:
        run = None
        try:
            run = active_run(getattr(request, "runtime", None))
        except Exception as e:                 # noqa: BLE001
            logger.debug("取当前运行失败（用全文提示词）：%s", e)
        text, info = assemble(base_sp, run)
        record(run, info)
        return text

    return conditional_discipline


__all__ = ["BLOCKS", "parse_blocks", "strip_blocks", "unwrap_blocks", "collect_facts", "select_blocks",
           "assemble", "record", "coordinator_prompt_middleware", "funnel_min"]
