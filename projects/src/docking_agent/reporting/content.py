"""报告内容的纯函数层：数字与表格格式化、工具版本、参数规划、失败统计。

从 `report.py` 拆出，避免单文件超过 `scripts/lint_local.py` 的 700 行门禁；
这里不依赖 matplotlib，也不依赖报告模板本身的章节结构，便于单独测试与复用。
"""
from __future__ import annotations

import importlib
import importlib.metadata
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from docking_agent.reporting.tables import collect_failures, format_box_size

CONSISTENCY_ZH = {"high": "高", "medium": "中", "low": "低"}

# 任务来源 / 受理相关的中文文案（把内部枚举值翻译成可读文字）
_AUTHORITY_ZH = {
    "chat": "对话指令（系统默认参数）",
    "chat+advanced": "对话指令（高级设置参数）",
    "manual": "参数表单（用户显式指定）",
    # 历史运行记录里可能仍有该 authority（流水线模式已于记录 60 移除），保留映射以便复看旧报告
    "pipeline": "确定性流水线（历史记录，该模式已移除）",
}
_DECISION_ZH = {"run": "接单执行", "ask": "信息不足，需用户补充", "reject": "超出范围，拒绝"}
_SOURCE_ZH = {"auto": "自动规划", "user": "用户显式指定（未改动）",
              "rules": "规则抽取（未调用 LLM）", "rules+llm": "规则 + 指令解析（LLM）",
              "disabled": "自动规划已关闭（沿用系统默认）", "skipped": "本任务不规划对接参数"}
_PILOT_ZH = {"ok": "完成（真实试跑）", "failed": "失败 → 已退回静态规划", "skipped": "跳过"}
_LIGAND_SOURCE_ZH = {"text": "文本输入", "file": "上传文件", "message": "对话消息中的分子",
                     "mentioned": "对话中提及的分子", "library": "示例分子库",
                     "tool": "工具整理", "prior": "上一轮对话遗留"}
_RECEPTOR_SOURCE_ZH = {"user": "用户指定 / 上传",
                       # 产品底线：系统**没有**默认受体（这里写「系统默认受体」曾与提示词自相矛盾）
                       "default": "未指定（系统无默认受体）",
                       # 点名了但解析不了 / 上传的文件用不了：报告要写清「待用户确认」
                       "unresolved": "不可用（待用户确认）"}
_SITE_SOURCE_ZH = {"user": "用户显式指定", "tool": "口袋预测工具 / 实验位点推断"}

# 正文里禁止出现的裸地址（图片相对路径不受影响）
_URL_RE = re.compile(r"https?://[^\s)>\]]+")
_ABS_PATH_RE = re.compile(r"(?<![\w.])/(?:files|api)/[^\s)>\]]+")


def _num(v: Any, digits: int = 2) -> str:
    """统一数字格式：数值按指定小数位；非数值写「—」（不臆造 0）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "—"
    return f"{v:.{digits}f}"


def _pct(part: int, total: int) -> str:
    """百分比统一 1 位小数；分母为 0 时写「—」。"""
    if total <= 0:
        return "—"
    return f"{part * 100.0 / total:.1f}%"


def _vec(value: Any) -> str:
    """把 [x, y, z] 写成 `x.xx, y.yy, z.zz`；缺省写「—」。"""
    if isinstance(value, (list, tuple)) and value:
        return ", ".join(_num(v, 2) if isinstance(v, (int, float)) else str(v) for v in value)
    return "—"


def _box_size_text(value: Any) -> str:
    text = format_box_size(value)
    return text.replace("x", " × ") + " Å" if text else "—"


def _table(headers: List[str], rows: List[List[Any]], aligns: Optional[List[str]] = None) -> str:
    """生成 Markdown 表格；aligns 取 l/r/c，用分隔行声明对齐（数字列右对齐）。"""
    cols = len(headers)
    marks = {"l": ":---", "r": "---:", "c": ":---:"}
    spec = [(aligns[i] if aligns and i < len(aligns) else "") for i in range(cols)]
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(marks.get(a, "---") for a in spec) + "|"]
    for r in rows:
        cells = ["" if c is None else str(c) for c in r]
        cells += [""] * (cols - len(cells))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def _demote_headings(text: str) -> str:
    """把模型输出里的标题整体降一级。

    协调 Agent 的报告往往自带 `## 1. ...` 这类编号，直接嵌入固定的结尾章节会导致
    章节编号重复、结构混乱；这里统一降级（代码块内不改动）。
    """
    out: List[str] = []
    in_code = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if not in_code and re.match(r"^#{1,5}\s", line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


#: 「结论/建议」类小节标题关键词（命中即保留）
_CONCLUSION_KEYS = ("结论", "建议", "推荐", "优化", "取舍", "风险", "局限", "小结", "总结", "优先",
                    "关键数值")
#: 明确的「数据复读」小节关键词（命中即丢弃 —— 这些内容报告前面各节已经用真实表格写过）
_DATA_DUMP_KEYS = ("参数", "配置摘要", "分子列表", "列表及排序", "排序", "属性评估", "可视化",
                   "参考数据", "数据可用性", "产物", "方法与过程",
                   # 对话式总结里的「需求理解 / 任务分配 / 实际执行」= 报告第 1–2 节，
                   # 「报告与产物」= 第 9 节：都不该在结论节再出现一遍
                   "需求理解", "任务分配", "实际执行", "执行过程", "报告与产物", "数据与产物")

#: 加粗编号小标题（`**3. 关键数值与结论**`）：对话式回复里用它分节，需要当成标题切块
_BOLD_HEADING_RE = re.compile(r"^\*\*\s*\d+[.、)]\s*[^*]{2,60}\*\*")


#: 过程性/元话行（协调 Agent 的回复里常见，但对报告读者没有信息量）
_PROCESS_META_RE = re.compile(
    r"^(已完成全部流程|以下是简短总结|以下是.*总结|明细见报告产物|详见报告产物|"
    r"以上为简短总结|（?decision=|本次报告由)")

#: 句子级的元话（整句删除）
_PROCESS_META_SENT_RE = re.compile(
    r"(明细(均|都)?见报告产物|完整原文见\s*`?agent_report\.md|"
    r"以下是简短总结|已完成全部流程（decision=[^）)]*）?|"
    r"数据与参数见第\s*1[–-]7\s*节|不在此重复)")


def _strip_process_meta(text: str) -> str:
    """去掉结论文本里的**过程性说明**（「以下是简短总结」「明细见报告产物」…）。

    为什么：这些句子对读者没有信息量，出现在正式报告的第 8 节会显得口语化；
    事实与判断一律保留（只删「怎么说」的元话，不删「说了什么」）。
    """
    out: List[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped and _PROCESS_META_RE.match(stripped):
            continue
        line = _PROCESS_META_SENT_RE.sub("", line)
        out.append(line)
    # 只做**不会误伤正文**的清理：空括号、删句后相邻的重复分隔符、多余空行
    # （绝不整体改写标点 —— 中文正文里「，。；」都是有效内容）
    cleaned = "\n".join(out)
    cleaned = re.sub(r"（\s*）", "", cleaned)
    cleaned = re.sub(r"([，,；;])\s*([，,；;。])", r"\2", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


#: AI 腔的过渡词：只去掉词本身，保留其后的事实（用户要求报告"专业性强一些，AI 感控制"）
_AI_FILLER_PREFIXES = ("综上所述", "总的来说", "总而言之", "总之", "值得注意的是", "需要注意的是",
                       "需要指出的是", "值得一提的是", "首先", "其次", "再次", "最后",
                       "另外", "此外", "同时", "可以看到", "不难看出", "由此可见",
                       "让我们", "本文将", "本报告将", "简而言之")
#: 纯过程/元信息句：整句丢弃（不携带任何事实）
_AI_META_PATTERNS = (r"^以上(为|是)", r"^(下面|接下来|以下)(是|将)", r"^如需(进一步|更多)",
                     r"希望(这|本)", r"欢迎(随时)?(提问|交流|指正)", r"^本文", r"^本报告")


def polish_agent_text(text: str) -> str:
    """去掉过渡词、纯过程句，并对**完全重复**的句子去重（标点/空白归一后比较）。

    专业化的做法不是"改写"（那会编造事实），而是**只做减法**：留事实与判断，去掉腔调。
    """
    out: List[str] = []
    seen = set()
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            out.append(raw)
            continue
        if any(re.search(pattern, stripped) for pattern in _AI_META_PATTERNS):
            continue
        for prefix in _AI_FILLER_PREFIXES:
            if stripped.startswith(prefix) and stripped[len(prefix):len(prefix) + 1] in ("，", ",", "：", ":", " "):
                stripped = stripped[len(prefix):].lstrip("，,：: ")
                break
        key = re.sub(r"[\s。，、；：,.;:!?！？*`\-—]", "", stripped)
        if len(key) >= 8 and key in seen:      # 中文一句话约 8 字以上才判重（短单元格不误伤）
            continue
        seen.add(key)
        out.append(stripped)
    return "\n".join(out)


def extract_agent_conclusions(narrative: str) -> str:
    """从协调 Agent 的整段报告里**只取结论/建议类小节**，避免报告内容重复。

    真实问题：早期实现把 Agent 的整份报告（含"参数配置摘要/分子列表/属性评估/可视化/
    对照数据"等小节）整体塞进第 8 节，于是同一批数字在报告里出现两遍，
    用户明确指出"报告中不要重复内容"。

    规则（可核对、可预期）：
      - 按标题切块；只保留标题命中「结论/建议/推荐/优化/风险」等关键词的块；
      - 明确属于数据复读的块（参数/列表/排序/属性/可视化/对照数据/数据可用性）直接丢弃；
      - 段落里**表格**保留（结论类小节里的表通常是取舍矩阵，不属于复读）；
      - 完全没有标题时按"整段都是结论"处理（保持向后兼容）；
      - 无可保留内容时返回空串，报告仍会输出自己那套确定性结论。
    """
    text = str(narrative or "").strip()
    if not text:
        return ""
    blocks: List[tuple] = []          # (标题, 正文)
    current_title, current_body = None, []
    in_code = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code         # 代码围栏内的 # 行不是标题，也不能把围栏切坏
        if not in_code and (re.match(r"^#{1,6}\s", line) or _BOLD_HEADING_RE.match(line.strip())):
            if current_title is not None or current_body:
                blocks.append((current_title, "\n".join(current_body).strip()))
            current_title, current_body = line, []
        else:
            current_body.append(line)
    if current_title is not None or current_body:
        blocks.append((current_title, "\n".join(current_body).strip()))
    if not any(title for title, _ in blocks):
        return polish_agent_text(text)  # 没有标题：按整段结论处理（同样做减法）

    kept: List[str] = []
    for title, body in blocks:
        if title is None:
            # 标题前的前言：只留下有实质内容的第一段，丢掉「以下是简短总结/明细见报告产物」这类
            # 过程性说明（报告本身已经写明数据在哪一节）。
            body = _strip_process_meta(body)
            if body:
                kept.append(body)
            continue
        plain = re.sub(r"^#+\s*", "", title).strip().strip("*").strip()
        plain = re.sub(r"^\d+[.、)]\s*", "", plain)      # `**3. 关键数值与结论**` → 关键数值与结论
        if any(k in plain for k in _DATA_DUMP_KEYS) and not any(k in plain for k in _CONCLUSION_KEYS):
            # 数据复读小节整体丢弃，但如果里面有「一句话结论」这类显式结论段落，
            # 把它摘出来（那是 Agent 的判断，不是数据复读）。
            summary = "\n".join(
                line for line in body.splitlines()
                if re.match(r"^\*\*(一句话结论|结论|总结|总体结论)", line.strip()))
            if summary:
                kept.append(summary.strip())
            continue
        if any(k in plain for k in _CONCLUSION_KEYS):
            body = _strip_process_meta(body)
            kept.append((title + ("\n\n" + body if body else "")).strip())
    return polish_agent_text(_strip_process_meta("\n\n".join(kept).strip()))


def _artifact_size(artifacts: Optional[List[Dict[str, Any]]], name: str) -> str:
    for a in (artifacts or []):
        if a.get("name") == name:
            size = a.get("size") or 0
            return f"{size / 1024:.0f} KB" if size >= 1024 else f"{size} B"
    return "—"


def protonation_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总本次运行的质子化态处理（**只统计工具真实回传的溯源字段**）。

    用于报告「1.3 对接参数」与「4. 理化性质」：策略是运行级的（同一批分子同口径），
    但要如实说明有多少分子被中和、哪些无法中和，且原始 SMILES 始终保留。
    """
    counts: Dict[str, int] = {}
    applied: List[Dict[str, Any]] = []
    rule_counts: Dict[str, int] = {}
    engine_counts: Dict[str, int] = {}
    engine_versions: Dict[str, str] = {}
    ph_values: Dict[float, int] = {}
    observed = charged = 0
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        info: Dict[str, Any] = {}
        for candidate in ((row.get("ligand_facts") or {}).get("protonation"), row.get("protonation")):
            if isinstance(candidate, dict) and candidate:
                info = candidate
                break
        if not info:
            continue
        observed += 1
        policy = str(info.get("policy") or "")
        if policy:
            counts[policy] = counts.get(policy, 0) + 1
        # 专业 pKa 引擎溯源（Dimorphite-DL 等）：报告要能说清「用的是什么引擎 + 哪个版本」
        engine = str(info.get("engine") or "")
        if engine:
            engine_counts[engine] = engine_counts.get(engine, 0) + 1
            if info.get("engine_version"):
                engine_versions[engine] = str(info["engine_version"])
        fallback = str(info.get("engine_fallback_reason") or "")
        if fallback:
            engine_counts["内置规则表（回退）"] = engine_counts.get("内置规则表（回退）", 0) + 1
        before = info.get("charge_before")
        if isinstance(before, (int, float)) and before:
            charged += 1
        for rule in (info.get("rules") or []):
            name = str((rule or {}).get("name") or "")
            if name:
                rule_counts[name] = rule_counts.get(name, 0) + 1
        ph_value = info.get("ph")
        if isinstance(ph_value, (int, float)):
            ph_values[float(ph_value)] = ph_values.get(float(ph_value), 0) + 1
        if info.get("applied"):
            applied.append({"name": row.get("name") or row.get("smiles") or "",
                            "smiles": row.get("smiles") or "",
                            "before": before, "after": info.get("charge_after")})
    policy = max(counts.items(), key=lambda kv: kv[1])[0] if counts else ""
    ph = max(ph_values.items(), key=lambda kv: kv[1])[0] if ph_values else None
    return {"policy": policy, "ph": ph, "observed": observed, "charged": charged,
            "applied": applied, "counts": counts, "rule_counts": rule_counts,
            "engine_counts": engine_counts, "engine_versions": engine_versions}


def _seed_info(result: Dict[str, Any]) -> Tuple[str, str]:
    """从排序行/结果里取对接随机种子；取不到写「未知」而不是省略（复现信息不能缺）。"""
    for row in (result.get("ranking") or []):
        if isinstance(row, dict) and row.get("seed") is not None:
            policy = row.get("seed_policy")
            return str(row["seed"]), str(policy) if policy else "未知"
    seed = result.get("seed")
    policy = result.get("seed_policy")
    return (str(seed) if seed is not None else "未知",
            str(policy) if policy else "未知")


def _strip_urls(text: str) -> str:
    """去掉正文里的裸地址（外链与 /files/、/api/ 路径），保证报告可直接交付。"""
    text = _URL_RE.sub("（外链已省略）", text)
    return _ABS_PATH_RE.sub("（路径已省略）", text)


# --------------------------------------------------------------------------- #
# 工具版本（Markdown 1.4 节与 PDF 封面共用，避免两处漂移）
# --------------------------------------------------------------------------- #
def _package_version(package: str) -> str:
    """从已安装发行版元数据读版本；取不到返回「未知」（不引入新依赖）。"""
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "未知"
    except Exception:  # noqa: BLE001  # 元数据损坏等罕见情况不应中断报告
        return "未知"


def _module_version(module_name: str, *, package: str = "", known: str = "") -> str:
    """优先用模块 `__version__`，其次发行版元数据，最后才用已知常量/未知。"""
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001  # 未安装/导入失败时退回元数据
        return _package_version(package or module_name)
    version = getattr(module, "__version__", "")
    if version:
        return str(version)
    dist = _package_version(package or module_name)
    return dist if dist != "未知" else (known or "未知")


def _p2rank_version() -> str:
    """p2rank 没有 Python 包版本：用本地部署目录名（如 p2rank_2.5.1）标识。"""
    try:
        from docking_agent.core.pockets import p2rank_home  # noqa: PLC0415  # 重依赖，延迟导入

        home = p2rank_home()
    except Exception:  # noqa: BLE001  # 版本探测失败不能影响报告
        return "未知"
    return home.name if home else "未知"


def tool_versions() -> List[List[str]]:
    """本次运行实际用到的工具与版本；取不到写「未知」，不省略行。

    注意：**配体质子化引擎不在这里**（`dimorphite-dl` 是可选安装）。
    它属于「本次运行实际用了什么」，由 `ligand_facts.protonation.engine` 逐分子溯源，
    在报告 §1.3 的「质子化态策略」行里呈现 —— 这样同一份快照在不同机器上渲染一致。
    """
    return [
        ["vina", _module_version("vina", known="1.2.7")],
        ["rdkit", _module_version("rdkit")],
        ["meeko", _module_version("meeko")],
        ["p2rank", _p2rank_version()],
        ["python", sys.version.split()[0]],
        ["matplotlib", _module_version("matplotlib")],
    ]


def tool_versions_markdown() -> List[List[str]]:
    """工具版本表（Markdown 单元格）：版本用行内代码包裹，PDF 会自动去标记。"""
    return [[name, f"`{ver}`"] for name, ver in tool_versions()]


# --------------------------------------------------------------------------- #
# 失败与跳过统计
# --------------------------------------------------------------------------- #
def _failure_reason(raw: str) -> str:
    """把逐分子错误归并成可分组的原因（去掉随分子变化的尾部细节）。"""
    err = str(raw or "").strip()
    if not err:
        return "未知原因"
    low = err.lower()
    if "解析失败" in err or "smiles" in low:
        return "配体化学解析失败（SMILES / 3D 生成）"
    if "位姿" in err:
        return "位姿写入失败"
    if "取消" in err or "cancel" in low:
        return "运行被取消"
    if "超时" in err or "timeout" in low:
        return "对接超时"
    if "对接" in err or "docking" in low:
        return "对接失败（引擎 / 受体 / 参数）"
    return err[:48]


def failure_stats(result: Dict[str, Any]) -> Dict[str, Any]:
    """汇总「输入 / 成功 / 失败 / 跳过」，失败按原因分组。

    统一从 `result["docking"]["receptors"][*]["results"]` 读取逐分子结果
    （两种运行模式都保留了该结构）；`docking` 缺失时退回 `ranking` 的成功行。
    """
    blocks = (result.get("docking") or {}).get("receptors") or []
    rows: List[Dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        rows.extend([r for r in (block.get("results") or []) if isinstance(r, dict)])

    failed: Dict[str, Dict[str, Any]] = {}
    success: set = set()
    for r in rows:
        key = str(r.get("smiles") or r.get("name") or "")
        has_aff = isinstance(r.get("affinity_kcal_mol"), (int, float))
        err = str(r.get("error") or "").strip()
        status = str(r.get("status") or "").lower()
        if has_aff and not err and status != "error" and key:
            success.add(key)
    for r in collect_failures({"receptors": blocks}):
        key = str(r.get("smiles") or r.get("name") or "")
        if not key:
            continue
        err = str(r.get("error") or "").strip()
        status = str(r.get("status") or "").lower()
        reason = err or ("运行被取消" if status == "cancelled"
                         else ("状态 error" if status == "error" else "未产出有效分数"))
        failed.setdefault(key, {**r, "_reason": reason})

    if not rows:  # 旧数据/单测里可能没有 docking 明细，退回 ranking
        for r in (result.get("ranking") or []):
            if isinstance(r, dict) and isinstance(r.get("affinity_kcal_mol"), (int, float)):
                key = str(r.get("smiles") or r.get("name") or "")
                if key:
                    success.add(key)

    input_keys: Dict[str, Dict[str, Any]] = {}
    for m in (result.get("molecules") or []):
        if not isinstance(m, dict):
            continue
        key = str(m.get("smiles") or m.get("name") or "")
        if key:
            input_keys.setdefault(key, m)
    covered = set(success) | set(failed)
    total = len(input_keys) if input_keys else len(covered)
    skipped = max(0, total - len(covered))

    groups: Dict[str, List[str]] = {}
    for r in failed.values():
        groups.setdefault(_failure_reason(r.get("_reason", "")), []).append(
            str(r.get("name") or r.get("smiles") or "—"))
    if skipped:
        groups["未进入对接（在解析/准备阶段被跳过）"] = ["—"]

    group_rows = sorted(
        ({"reason": reason, "count": len(names), "names": names} for reason, names in groups.items()),
        key=lambda g: (-g["count"], g["reason"]))
    return {
        "total": total,
        "success": len(success),
        "failed": len(failed),
        "skipped": skipped,
        "groups": group_rows,
    }


# --------------------------------------------------------------------------- #
# 参数自动规划
# --------------------------------------------------------------------------- #
def _param_plan(result: Dict[str, Any]) -> Dict[str, Any]:
    """取参数规划结果；没有规划（或结构异常）时返回空 dict。"""
    plan = result.get("param_plan") or {}
    return plan if isinstance(plan, dict) and plan else {}


def param_plan_lines(result: Dict[str, Any], caption: str, table_caption: str) -> List[str]:
    """「参数自动规划」小节：参数取值 + decisions 理由链（没有规划结果时不显示）。"""
    plan = _param_plan(result)
    if not plan:
        return []
    L: List[str] = [caption, ""]
    if plan.get("two_stage"):
        funnel = (f"两阶段（粗筛 exhaustiveness={plan.get('coarse_exhaustiveness')} 全库 / "
                  f"精算 exhaustiveness={plan.get('exhaustiveness')} 前 "
                  f"{plan.get('refine_top_n')} 个）")
    else:
        funnel = "单阶段"
    rows: List[List[Any]] = [
        ["来源", _SOURCE_ZH.get(str(plan.get("source")), str(plan.get("source") or "—"))],
        ["任务类型", plan.get("task_type") or "—"],
        ["搜索强度 exhaustiveness",
         plan.get("exhaustiveness") if plan.get("exhaustiveness") is not None else "—"],
        ["漏斗", funnel],
        ["n_poses", plan.get("n_poses") if plan.get("n_poses") is not None else "—"],
        ["引擎 / 种子", f"`{plan.get('engine') or '—'}` / `{plan.get('seed') if plan.get('seed') is not None else '—'}`"],
        ["库规模", plan.get("library_size") if plan.get("library_size") is not None else "—"],
        ["P90 可旋转键 / 柔性系数",
         f"{plan.get('p90_rotatable') if plan.get('p90_rotatable') is not None else '—'} / "
         f"{plan.get('flex_factor') if plan.get('flex_factor') is not None else '—'}"],
        ["盒体积系数", plan.get("box_factor") if plan.get("box_factor") is not None else "—"],
        ["预估耗时 / 预算",
         f"{_num(plan.get('eta_sec'), 0)} s / {_num(plan.get('budget_sec'), 0)} s"],
    ]
    pilot = plan.get("pilot") or {}
    if pilot:
        rows.append(["pilot 预算护栏",
                     f"{_PILOT_ZH.get(str(pilot.get('status')), pilot.get('status'))}"
                     + (f"；{pilot.get('note')}" if pilot.get("note") else "")])
    L.append(f"**{table_caption}**")
    L.append("")
    L.append(_table(["参数", "取值"], rows, aligns=["l", "l"]))
    L.append("")
    decisions = [str(d) for d in (plan.get("decisions") or []) if str(d).strip()]
    if decisions:
        L.append("**决策理由链**：")
        L.append("")
        for i, decision in enumerate(decisions, 1):
            L.append(f"{i}. {decision}")
        L.append("")
    warnings = [str(w) for w in (plan.get("warnings") or []) if str(w).strip()]
    if warnings:
        L.append("**规划提示**：" + "；".join(warnings))
        L.append("")
    return L
