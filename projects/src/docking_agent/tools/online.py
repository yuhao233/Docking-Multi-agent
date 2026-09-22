"""在线数据库工具：从 RCSB / UniProt / PubChem 获取蛋白质与小分子。

提供两个 @tool 供协调 Agent 使用：
  - ``fetch_protein_structure``：按 **PDB 结构号** 或 **UniProt accession / 基因名 / 蛋白名（含中文）**
    获取蛋白质结构，下载并现场准备为可对接受体；实验结构优先 RCSB，缺失时回退 AlphaFold DB。
  - ``fetch_molecule_record``：按名称/CID/SMILES/InChIKey/InChI 从 PubChem 获取小分子 SMILES
    （名称支持中文别名映射，如 代森猛锌 → Mancozeb）。

设计要点：名称归一化（中英映射 + 物种推断）→ UniProt 多策略检索打分 → 实验结构(RCSB) 优先、
缺失/不可用时回退 AlphaFold → 结果带完整溯源（accession/物种/结构来源/URL/打分理由）；
解析不确定时经 ``tools/choices.py`` 下发结构化 ``choices`` 并在真失败时阻断对接（产品底线）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from langchain.tools import tool

from docking_agent.core import read_receptor_file
from docking_agent.core.ligands import describe_ligand
from docking_agent.core.resolve import (
    candidate_brief,
    normalize_molecule_name,
    receptor_choices,
    resolve_receptor_name,
    summarize_attempts,
)
from docking_agent.paths import cache_dir
from docking_agent.runtime.context import AgentContext, active_run
from docking_agent.tools.choices import (
    choices_payload,
    clear_choices,
    mark_receptor_unresolved,
    mixture_choices,
    publish_choices,
    resolution_failure_json,
)
from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)

RCSB_DOWNLOAD = "https://files.rcsb.org/download/{}.pdb"
RCSB_SEARCH = "https://search.rcsb.org/rcsbsearch/v2/query"
RCSB_ENTRY = "https://data.rcsb.org/rest/v1/core/entry/{}"
ALPHAFOLD_PREDICTION = "https://alphafold.ebi.ac.uk/api/prediction/{}"
PUBCHEM_PROPERTY = ("https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/{}/{}/property/"
                    "IsomericSMILES,CanonicalSMILES,ConnectivitySMILES,MolecularFormula,"
                    "IUPACName,MolecularWeight,Title/JSON")

_USER_AGENT = "docking-agent/0.4 (+local)"
_TIMEOUT = 60
_RETRIES = 2

# UniProt accession（6 或 10 位）与 entry ID（如 EGFR_HUMAN）
_ACCESSION_RE = re.compile(r"^[A-Z0-9]{6,10}$")
_ENTRY_ID_RE = re.compile(r"^[A-Z0-9]{1,10}_[A-Z0-9]{1,10}$")
_PDB_ID_RE = re.compile(r"^[0-9][A-Za-z0-9]{3}$")


class OnlineError(RuntimeError):
    """在线数据库访问失败（带可操作的建议）。"""


# --------------------------------------------------------------------------- #
# HTTP 基础设施
# --------------------------------------------------------------------------- #
def _http_get(url: str, timeout: int = _TIMEOUT) -> bytes:
    last: Optional[Exception] = None
    for attempt in range(_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 410):
                raise OnlineError(_explain_http_error(url, e)) from e
            last = e
            logger.warning("请求失败(%s)，第 %s 次重试：%s", e.code, attempt + 1, url)
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("请求异常，第 %s 次重试：%s（%s）", attempt + 1, url, e)
        if attempt < _RETRIES:
            time.sleep(0.6 * (attempt + 1))
    raise OnlineError(f"在线请求失败（已重试 {_RETRIES} 次）：{url} —— {last}")


def _http_json(url: str, timeout: int = _TIMEOUT) -> Any:
    raw = _http_get(url, timeout)
    if not raw.strip():
        logger.info("在线接口返回空响应（按无结果处理）：%s", url)
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise OnlineError(f"在线返回内容不是合法 JSON：{url} —— {e}") from e


def _http_post_json(url: str, payload: Dict[str, Any], timeout: int = _TIMEOUT) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"User-Agent": _USER_AGENT,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        if not body.strip():  # RCSB 无命中时会返回 204/空体
            logger.info("检索接口返回空响应（按无命中处理）：%s", url)
            return {"result_set": [], "total_count": 0}
        return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (204, 404):  # RCSB 无命中时返回 404/204
            return {"result_set": [], "total_count": 0}
        raise OnlineError(_explain_http_error(url, e)) from e
    except Exception as e:  # noqa: BLE001
        raise OnlineError(f"在线检索失败：{url} —— {e}") from e


def _explain_http_error(url: str, e: urllib.error.HTTPError) -> str:
    host = urllib.parse.urlparse(url).netloc
    detail = ""
    try:
        body = e.read()[:200].decode("utf-8", errors="replace")
        if body:
            detail = f"；返回内容：{body}"
    except Exception as e:  # noqa: BLE001
        logger.debug("读取错误响应体失败：%s", e)
    if e.code == 404:
        return f"{host} 未找到对应记录（HTTP 404）{detail}"
    if e.code == 400:
        return (f"{host} 拒绝了请求（HTTP 400，通常是输入格式不被接受）{detail}。"
                "输入可以是 PDB 结构号(如 3ZBF)、UniProt accession(如 P08922)、"
                "或基因名/蛋白名(如 EGFR)")
    if e.code == 410:
        return f"{host} 记录已废弃（HTTP 410）{detail}"
    return f"{host} 返回 HTTP {e.code}{detail}"


def _download(url: str, dest: str, timeout: int = 150) -> str:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


# --------------------------------------------------------------------------- #
# UniProt
# --------------------------------------------------------------------------- #
def _looks_like_accession(text: str) -> bool:
    return bool(_ACCESSION_RE.match((text or "").strip().upper()))


def _res_num(value: Any) -> float:
    try:
        return float(str(value).replace("Å", "").replace("A", "").strip() or "99")
    except (TypeError, ValueError):
        return 99.0


def _protein_name(entry: Dict[str, Any]) -> str:
    desc = entry.get("proteinDescription") or {}
    rec = ((desc.get("recommendedName") or {}).get("fullName") or {}).get("value")
    if rec:
        return str(rec)
    for alt in (desc.get("alternativeNames") or []):
        v = ((alt or {}).get("fullName") or {}).get("value")
        if v:
            return str(v)
    return str(entry.get("primaryAccession") or "")


def _gene_names(entry: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for gene in (entry.get("genes") or []):
        name = ((gene or {}).get("geneName") or {}).get("value")
        if name:
            out.append(str(name))
        for syn in ((gene or {}).get("synonyms") or []):
            v = (syn or {}).get("value")
            if v:
                out.append(str(v))
    return out


def _pdbs_from_entry(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 UniProt 交叉引用提取 PDB 结构（含实验方法与分辨率）。"""
    refs: List[Dict[str, Any]] = []
    for x in (entry.get("uniProtKBCrossReferences") or []):
        if (x or {}).get("database") != "PDB":
            continue
        props = {p.get("key"): p.get("value") for p in (x.get("properties") or [])}
        refs.append({"pdb": str(x.get("id", "")).upper(),
                     "method": props.get("Method") or "",
                     "resolution": props.get("Resolution") or "",
                     "chains": props.get("Chains") or ""})
    # X-ray 优先，其次分辨率由优到劣
    refs.sort(key=lambda r: (0 if "X-RAY" in str(r["method"]).upper() else 1,
                             _res_num(r["resolution"])))
    return refs


def _uniprot_queries(text: str) -> List[str]:
    """构造检索式候选（按精度从高到低）。

    注意：`accession:` 过滤器会校验格式，把蛋白名拼进去会被 UniProt 直接 400，
    因此只在输入确实像 accession 时才使用；基因符号优先用 `gene_exact`（裸词全文检索
    常常先命中未表征条目）。
    """
    raw = text.strip().strip('"')
    queries: List[str] = []
    if _looks_like_accession(raw):
        queries.append(f"accession:{raw}")
    if " " not in raw and len(raw) <= 20:
        queries.append(f'gene_exact:"{raw}" AND (reviewed:true)')
        queries.append(f'gene_exact:"{raw}"')
    queries.append(f"{raw} AND (reviewed:true)")
    queries.append(raw)
    return queries


# --------------------------------------------------------------------------- #
# RCSB
# --------------------------------------------------------------------------- #
def _rcsb_search_by_uniprot(accession: str, rows: int = 10) -> List[str]:
    """RCSB Search API：按 UniProt accession 反查 PDB 编号（UniProt 无 PDB 引用时的回退）。"""
    payload = {
        "query": {"type": "terminal", "service": "text", "parameters": {
            "attribute": ("rcsb_polymer_entity_container_identifiers."
                          "reference_sequence_identifiers.database_accession"),
            "operator": "exact_match", "value": accession}},
        "return_type": "entry",
        "request_options": {"paginate": {"start": 0, "rows": rows},
                            "scoring_strategy": "combined"},
    }
    data = _http_post_json(RCSB_SEARCH, payload)
    return [str(x.get("identifier", "")).upper() for x in (data.get("result_set") or [])]


def _rcsb_entry_info(pdb_id: str) -> Dict[str, Any]:
    """RCSB Data API：取实验方法与分辨率。"""
    try:
        data = _http_json(RCSB_ENTRY.format(pdb_id))
    except Exception as e:  # noqa: BLE001
        logger.debug("获取 %s 条目信息失败：%s", pdb_id, e)
        return {}
    info = data.get("rcsb_entry_info") or {}
    res = info.get("resolution_combined")
    resolution = ""
    if isinstance(res, list) and res:
        resolution = f"{res[0]} A"
    elif res:
        resolution = f"{res} A"
    return {"method": info.get("experimental_method") or "", "resolution": resolution}


# --------------------------------------------------------------------------- #
# AlphaFold DB（无实验结构时的预测结构回退）
# --------------------------------------------------------------------------- #
def _alphafold_prediction(accession: str) -> Optional[Dict[str, Any]]:
    """按 UniProt accession 查 AlphaFold DB 预测模型；无模型返回 None。

    注意：AlphaFold 的模型版本号会随时间更新（v4 → v6 …），因此**不写死文件名**，
    一律使用接口返回的 ``pdbUrl``，并把模型版本写进溯源字段。
    """
    if not accession:
        return None
    try:
        data = _http_json(ALPHAFOLD_PREDICTION.format(urllib.parse.quote(accession, safe="")))
    except OnlineError as e:
        logger.info("AlphaFold 无 %s 的预测模型：%s", accession, e)
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        return None
    entry = data[0] or {}
    pdb_url = str(entry.get("pdbUrl") or "")
    if not pdb_url:
        return None
    return {"entry_id": entry.get("entryId") or f"AF-{accession}-F1",
            "model_url": pdb_url, "cif_url": entry.get("cifUrl") or "",
            "model_version": entry.get("latestVersion"),
            "model_date": entry.get("modelCreatedDate") or "",
            "description": entry.get("uniprotDescription") or "",
            "organism": entry.get("organismScientificName") or ""}


def _pdb_candidates_for(candidate: Dict[str, Any]) -> List[Dict[str, Any]]:
    """候选蛋白的实验结构列表：优先 UniProt 交叉引用，缺失时用 RCSB Search 反查。"""
    pdb_ids = list(candidate.get("pdb_ids") or [])
    accession = str(candidate.get("accession") or "")
    if not pdb_ids and accession:
        try:
            pdb_ids = _rcsb_search_by_uniprot(accession)
        except Exception as e:  # noqa: BLE001
            logger.info("RCSB 反查 %s 失败：%s", accession, e)
            pdb_ids = []
    out: List[Dict[str, Any]] = []
    for pid in pdb_ids[:5]:
        out.append({"pdb": str(pid).upper(), **_rcsb_entry_info(pid)})
    out.sort(key=lambda r: (0 if "X-RAY" in str(r.get("method", "")).upper() else 1,
                            _res_num(r.get("resolution"))))
    return out


def _download_and_prepare(url: str, dest: str) -> Dict[str, Any]:
    """下载结构文件（已有则复用）并走现有受体准备链（core.receptors.read_receptor_file）。"""
    if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        _download(url, dest)
    return read_receptor_file(dest)


# --------------------------------------------------------------------------- #
# 工具：蛋白结构
# --------------------------------------------------------------------------- #
@tool
def fetch_protein_structure(source: str, runtime: ToolRuntime[AgentContext] = None) -> str:
    """从在线数据库获取蛋白质结构，并现场准备为可直接对接的受体。

    source 支持四种写法（**点名受体时先调本工具，不要先问用户**）：PDB 号（3ZBF）、
    UniProt accession（Q9SJQ6）、英文基因/蛋白名（ROS1）、中文名（「植物去甲基化酶ROS1」，
    自动中英映射 + 物种推断）。结构优先取 RCSB 实验结构，缺失时回退 AlphaFold DB 预测结构
    （版本随接口返回，不写死）。

    返回（受体 spec）：
      ok → {"status":"ok","receptor_file":传给 run_docking 的文件,"box_center":[…],"box_size":[…],
            "structure_source":"rcsb|alphafold","pdb_id","accession","organism","protein",
            "score","score_reasons","structure_url","provenance":{数据库/物种/方法与分辨率…}}
      ambiguous（多个同样合理的候选）→ 给出候选并写成可点选 `choices`，此时**不做任何计算**；
      error/not_found → 给出已尝试的检索；这两种情况都会把本次受体标为 unresolved，
      `run_docking` 护栏拒绝计算。
    """
    src = (source or "").strip()
    if not src:
        return json.dumps({"status": "error",
                           "message": "请提供 PDB 结构号（如 3ZBF）、UniProt accession（如 Q9SJQ6）"
                                      "或基因/蛋白名（如 ROS1、植物去甲基化酶ROS1）。"},
                          ensure_ascii=False)

    cache = cache_dir()
    resolved: Dict[str, Any] = {"status": "ok", "requested": src}

    try:
        # ---- 1. 解析输入：PDB 号直取；其余走名称归一化 + UniProt 多策略检索 ----
        named = False
        named_candidate: Dict[str, Any] = {}
        if _PDB_ID_RE.match(src):
            pdb_id = src.upper()
            resolved.update({"source_db": "pdb", "pdb_id": pdb_id, "structure_source": "rcsb",
                             "structure_url": RCSB_DOWNLOAD.format(pdb_id)})
            candidates: List[Dict[str, Any]] = [{"pdb": pdb_id}]
        else:
            named = True
            resolution = resolve_receptor_name(src)
            resolved["receptor_resolution"] = {
                "status": resolution["status"], "query": resolution["query"],
                "attempt_summary": summarize_attempts(resolution["attempts"]),
                "candidates": [candidate_brief(c) for c in resolution["candidates"]],
            }
            if resolution["status"] != "resolved" or not resolution.get("selected"):
                mark_receptor_unresolved(src, resolution["status"], resolution, runtime=runtime)
                if resolution["status"] in ("ambiguous", "low_confidence"):
                    publish_choices("receptor", receptor_choices(resolution["candidates"]),
                                     note="检索到多个/低置信候选：请选择要使用的受体，系统不替用户决定",
                                     runtime=runtime)
                # 检索不到候选时**不下发任何选项**：系统没有默认受体，预置受体也不是用户可选来源
                # （历史缺陷：这里曾追加「改用系统默认受体 凝血酶」选项，与产品规则/代码守卫冲突）。
                return resolution_failure_json(src, resolution)
            named_candidate = resolution["selected"]
            accession = str(named_candidate.get("accession") or "")
            resolved.update({
                "source_db": "uniprot",
                "resolved_by": named_candidate.get("strategies") or ["search"],
                "accession": accession,
                "entry_id": named_candidate.get("entry_id"),
                "organism": named_candidate.get("organism"),
                "organism_id": named_candidate.get("organism_id"),
                "protein": named_candidate.get("protein"),
                "gene_names": named_candidate.get("gene_names"),
                "score": named_candidate.get("score"),
                "score_reasons": named_candidate.get("reasons"),
                "why_selected": (
                    f"候选打分 {named_candidate.get('score')}："
                    f"{accession}（{named_candidate.get('organism')}，"
                    f"{named_candidate.get('protein')}）；理由："
                    + "、".join(named_candidate.get("reasons") or [])),
                "candidates": [candidate_brief(c) for c in resolution["candidates"]],
            })
            candidates = _pdb_candidates_for(named_candidate)
            resolved["structure_candidates"] = candidates
            if candidates:
                resolved.update({"pdb_id": candidates[0]["pdb"], "structure_source": "rcsb"})
            logger.info("受体名称 %r 解析为 %s（%s），实验结构候选 %s 个",
                        src, accession, named_candidate.get("organism"), len(candidates))

        # ---- 2. 下载并现场准备为受体（候选逐个尝试：部分结构缺原子/含异常残基会准备失败）----
        tried: List[str] = []
        failures: List[str] = []
        spec: Optional[Dict[str, Any]] = None
        structure_source = str(resolved.get("structure_source") or "rcsb")
        structure_url = str(resolved.get("structure_url") or "")
        structure_file = ""
        pdb_id = str(resolved.get("pdb_id") or "")
        method = ""
        resolution_txt = ""
        for cand in (candidates or [])[:5]:
            pid = str(cand.get("pdb") or "").upper()
            if not pid or pid in tried:
                continue
            tried.append(pid)
            url = RCSB_DOWNLOAD.format(pid)
            dest = str(cache / f"{pid}.pdb")
            try:
                spec = _download_and_prepare(url, dest)
                pdb_id, structure_source, structure_url, structure_file = pid, "rcsb", url, dest
                method = cand.get("method") or ""
                resolution_txt = cand.get("resolution") or ""
                if len(tried) > 1:
                    logger.info("前 %s 个候选准备失败，已改用 %s", len(tried) - 1, pid)
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("结构 %s 准备失败，尝试下一个候选：%s", pid, e)
                failures.append(f"{pid}: {str(e)[:160]}")
                spec = None

        # ---- 3. 无实验结构（或实验结构都不可用）→ AlphaFold 预测结构回退 ----
        accession = str(resolved.get("accession") or "")
        if spec is None and named and accession:
            af = _alphafold_prediction(accession)
            if af:
                dest = str(cache / os.path.basename(af["model_url"]))
                try:
                    spec = _download_and_prepare(af["model_url"], dest)
                    structure_source, structure_url, structure_file = "alphafold", af["model_url"], dest
                    pdb_id, method, resolution_txt = "", "AlphaFold 预测模型", ""
                    resolved["alphafold"] = af
                    resolved["pdb_id"] = ""
                    logger.info("回退 AlphaFold 预测结构：%s", af["model_url"])
                except Exception as e:  # noqa: BLE001
                    logger.warning("AlphaFold 结构准备失败：%s", e)
                    failures.append(f"AlphaFold {af['model_url']}: {str(e)[:160]}")
            else:
                failures.append(f"AlphaFold：未找到 {accession} 的预测模型")

        if spec is None:
            if named:
                failed_resolution = resolved.get("receptor_resolution") or {}
                mark_receptor_unresolved(src, "structure_unavailable", failed_resolution, runtime=runtime)
                publish_choices("receptor", receptor_choices(failed_resolution.get("candidates") or []),
                                 note="解析到候选但结构准备失败：可改选其它候选或提供 PDB 文件",
                                 runtime=runtime)
            return json.dumps({
                "status": "error", "reason": "structure_unavailable", "requested": src,
                "accession": accession, "tried": tried,
                "attempts": (resolved.get("receptor_resolution") or {}).get("attempt_summary") or [],
                "message": "未找到可用于对接的结构（实验结构与 AlphaFold 预测均失败）："
                           + "；".join(failures[:3])
                           + "。可改用其他 PDB 结构号/accession，或直接提供本地 .pdb/.ent/.cif/.pdbqt 文件。",
            }, ensure_ascii=False)

        # ---- 4. 组装结果（含完整溯源）----
        resolved.update({
            "pdb_id": pdb_id,
            "structure_source": structure_source,
            "structure_url": structure_url,
            "structure_file": structure_file,
            "file_size": (os.path.getsize(structure_file) if structure_file
                          and os.path.isfile(structure_file) else 0),
            "method": method,
            "resolution": resolution_txt,
            "receptor_file": spec.get("pdbqt"),
            "box_center": spec.get("center"),
            "box_size": spec.get("size"),
            "protein": resolved.get("protein") or spec.get("protein"),
            "name": spec.get("name"),
            "tried": tried,
            "structure_failures": failures,
        })
        resolved["provenance"] = {
            "requested": src,
            "database": "RCSB PDB" if structure_source == "rcsb" else "AlphaFold DB",
            "structure_source": structure_source,
            "accession": accession,
            "entry_id": resolved.get("entry_id") or "",
            "organism": resolved.get("organism") or "",
            "protein": resolved.get("protein") or "",
            "pdb_id": pdb_id,
            "structure_url": structure_url,
            "downloaded_file": structure_file,
            "alphafold_model_version": (resolved.get("alphafold") or {}).get("model_version"),
            "uniprot_attempts": (resolved.get("receptor_resolution") or {}).get("attempt_summary") or [],
            "score_reasons": resolved.get("score_reasons") or [],
        }
        # 选中结构的方法/精度（实验结构才有）：报告 §1.2 要写明「用的是哪个结构、什么分辨率」
        _sel = next((c for c in (resolved.get("structure_candidates") or [])
                     if str(c.get("pdb") or "").upper() == str(pdb_id or "").upper()), {})
        resolved["provenance"]["structure_method"] = _sel.get("method") or ""
        resolved["provenance"]["structure_resolution"] = _sel.get("resolution") or ""
        from docking_agent.runtime.run_facts import note_receptor_provenance

        note_receptor_provenance(active_run(runtime), resolved["provenance"])
        logger.info("蛋白结构已就绪：%s -> %s（来源 %s，候选尝试 %s 个）",
                    src, spec.get("pdbqt"), structure_source, len(tried))
        clear_choices("receptor", runtime=runtime)
        return json.dumps(resolved, ensure_ascii=False)

    except OnlineError as e:
        logger.warning("fetch_protein_structure(%s) 在线访问失败：%s", src, e)
        return json.dumps({"status": "error", "requested": src,
                           "message": f"{e}。建议：改用 PDB 结构号（如 3ZBF）或稍后重试；"
                                      "也可直接提供本地 .pdb/.ent/.cif/.pdbqt 文件路径。"},
                          ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("fetch_protein_structure(%s) 失败", src)
        return json.dumps({"status": "error", "requested": src,
                           "message": f"下载/准备蛋白失败：{e}"}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 工具：小分子
# --------------------------------------------------------------------------- #
@tool
def fetch_molecule_record(query: str, id_type: str = "name", runtime: ToolRuntime[AgentContext] = None) -> str:
    """从 PubChem 在线数据库查询小分子，返回其 SMILES 及理化信息。

    参数：
      - query：查询值（化合物名 / CID / SMILES / InChIKey / InChI）；中文名会自动映射英文
        （如「代森猛锌 / 代森锰锌」→ Mancozeb，「华法林」→ Warfarin）；PubChem 的 name 检索
        本身不认中文，直接查中文会 404，所以先翻译再查；
      - id_type：name(默认) / cid / smiles / inchikey / inchi。

    返回 JSON：{"status":"ok","resolved_query":实际检索名,"alias_used":命中的中文别名,
    "compounds":[{"name","smiles","cid","formula","iupac","molwt",
                  "is_mixture","representative_smiles","components","warnings","facts"}]}。
    `smiles` 即 PubChem 原始 SMILES，可直接用于分子库导入与后续对接。

    **混合物/配位聚合物如实报告**：若命中的是多组分结构（如 Mancozeb = Mn/Zn 与 EBDC 的
    配位聚合物），返回 `is_mixture=true`、`components`（各片段及角色）与 `representative_smiles`
    （最大有机片段，仅作**代表结构**），并给出 `mixture_note` 说明代表结构的取法 —— 绝不臆造单一结构。
    查不到时按分子侧既有规则返回 error 并要求用户补名称/CID/SMILES。
    """
    id_type = (id_type or "name").lower().strip()
    if id_type not in ("name", "cid", "smiles", "inchikey", "inchi"):
        return json.dumps({"status": "error",
                           "message": "id_type 需为: name / cid / smiles / inchikey / inchi"},
                          ensure_ascii=False)
    q = (query or "").strip()
    if not q:
        return json.dumps({"status": "error",
                           "message": "请提供要查询的化合物名/CID/SMILES/InChIKey。"},
                          ensure_ascii=False)

    # 中文名 → 英文名（只翻译、不编造）；失败后按原样再试一次
    attempts: List[str] = []
    if id_type == "name":
        mapped, alias = normalize_molecule_name(q)
        probe = [q] if mapped == q else [q, mapped]
    else:
        mapped, alias = q, ""
        probe = [q]

    data: Optional[Dict[str, Any]] = None
    resolved_query = q
    alias_used = ""
    for cand in probe:
        url = PUBCHEM_PROPERTY.format(id_type, urllib.parse.quote(cand, safe=""))
        try:
            data = _http_json(url)
            resolved_query, alias_used = cand, (alias if cand == mapped and mapped != q else "")
            break
        except OnlineError as e:
            attempts.append(f"{cand}: {e}")
            data = None
        except Exception as e:  # noqa: BLE001
            logger.warning("PubChem 查询失败：%s", e)
            attempts.append(f"{cand}: {e}")
            data = None
    if data is None:
        last = attempts[-1] if attempts else f"{q}: 未知错误"
        if "404" in last:
            msg = f"PubChem 未找到与 {id_type}={q!r} 匹配的化合物"
        elif "400" in last:
            msg = (f"PubChem 拒绝了查询（id_type={id_type} 与输入 {q!r} 不匹配？）"
                   "。name 用化合物名、cid 用数字、smiles 用 SMILES 串")
        else:
            msg = f"PubChem 查询失败：{last}"
        return json.dumps({"status": "error", "query": q, "id_type": id_type,
                           "attempts": attempts, "message": msg}, ensure_ascii=False)

    comps: List[Dict[str, Any]] = []
    for p in ((data.get("PropertyTable") or {}).get("Properties") or []):
        smiles = ""
        for k in ("CanonicalSMILES", "IsomericSMILES", "SMILES", "ConnectivitySMILES"):
            if p.get(k):
                smiles = p[k]
                break
        comp: Dict[str, Any] = {"name": p.get("Title") or resolved_query, "smiles": smiles,
                                "cid": p.get("CID"), "formula": p.get("MolecularFormula", ""),
                                "iupac": p.get("IUPACName", ""), "molwt": p.get("MolecularWeight")}
        if smiles:
            try:
                # 这里是**描述性查询**（把名称解析成结构），不是对接运行：
                # 用 keep 报告数据库原样形式，避免"代表结构"被中和后与用户看到的记录不一致。
                desc = describe_ligand(smiles, protonation="keep")
            except Exception as e:  # noqa: BLE001
                logger.debug("配体体检失败：%s", e)
                desc = None
            if desc and desc.get("ok"):
                facts = desc.get("facts") or {}
                comp["facts"] = facts
                comp["warnings"] = desc.get("warnings") or []
                comp["is_mixture"] = int(facts.get("num_fragments") or 1) > 1
                if comp["is_mixture"]:
                    main = desc.get("smiles") or smiles
                    comp["representative_smiles"] = main
                    comp["components"] = ([{"smiles": main, "role": "最大有机片段（默认代表结构）"}]
                                          + [{"smiles": r.get("smiles"), "role": r.get("kind"),
                                              "heavy_atoms": r.get("heavy_atoms")}
                                             for r in (desc.get("removed_fragments") or [])])
        comps.append(comp)
    if not comps:
        return json.dumps({"status": "error", "query": q,
                           "message": f"PubChem 返回了结果但未解析到化合物属性：{q!r}"},
                          ensure_ascii=False)

    out: Dict[str, Any] = {"status": "ok", "query": q, "id_type": id_type,
                           "resolved_query": resolved_query, "alias_used": alias_used,
                           "attempts": attempts, "compounds": comps}
    mixtures = [c for c in comps if c.get("is_mixture")]
    if mixtures:
        out["mixture_note"] = (
            f"「{resolved_query}」在 PubChem 是**多组分结构/配位聚合物**（CID {mixtures[0].get('cid')}），"
            "不是单一小分子：SMILES 含多个片段与金属离子。本工具返回的是 PubChem 的原始 SMILES；"
            "`representative_smiles` 是最有代表性的**最大有机片段**（由 RDKit 按重原子数选出，"
            "仅供参考），金属/反离子未纳入。若要按原聚合形式建模，请明确指定组分与比例，"
            "或提供构造好的单分子结构 —— 系统不会臆造混合物结构。")
        choices = mixture_choices(mixtures[0], q)
        publish_choices("molecule", choices,
                         note="该名称是多组分/聚合物：代表结构的取法需要用户确认，系统不替用户决定",
                         runtime=runtime)
        # 选项明细只走界面（`run.data["choices"]`）；给**模型**的载荷里不带明细，
        # 否则主管 Agent 会把四个 SMILES 再抄成一张表 —— 同一问题在界面上出现两次（真实反馈）。
        out.update(choices_payload(
            choices,
            message=(f"「{resolved_query}」是多组分结构/配位聚合物：代表结构的取法必须由用户确认，"
                     "本工具**没有**导入任何分子（不替用户选择）。"),
            kind="molecule"))
    else:
        clear_choices("molecule", runtime=runtime)
    return json.dumps(out, ensure_ascii=False)


