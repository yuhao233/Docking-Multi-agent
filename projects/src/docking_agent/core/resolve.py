"""名称归一化 + UniProt 多策略检索打分（受体自动解析的「大脑」）。

## 为什么单独一层

用户点名受体时说的是**口语化名称**（「植物去甲基化酶ROS1」「代森猛锌」），
而在线数据库要的是 accession/基因名/英文蛋白名。旧实现把用户输入直接当 accession
拼进 URL，导致 `HTTP 400` 或「解析不到 → 立刻问用户」。本模块把这段「翻译」独立出来，
做成**纯逻辑 + 可注入 HTTP**，因此既能真实联网，也能零网络单测。

职责边界（重要）：
  * 本模块**不下载结构**（RCSB/AlphaFold 回退在 `tools/online.py`）；
  * 本模块**不接入受理层**（受理层保持零网络、确定性）；
  * 检索命中多个**同样合理但物种不同**的候选时返回 ``status="ambiguous"``，
    并附候选清单，由上层向用户提问，绝不替用户猜一个。

## 打分规则（可单测，改动必须同步更新测试）

| 项 | 分值 | 说明 |
| --- | --- | --- |
| Swiss-Prot 已审阅 | +40 | 人工审阅条目优先于 TrEMBL |
| 物种精确匹配 | +30 | 候选 taxid == 线索 taxid（如拟南芥 3702） |
| 同为植物 | +18 | 线索是植物但候选是另一种植物（如线索「植物」、候选水稻） |
| 物种不符 | −25 | 线索是植物却命中人源/鼠源等，压下去 |
| 蛋白名含映射家族词 | +20 | 如 demethylase / DNA glycosylase / kinase |
| 蛋白名不含家族词 | −10 | 只在用户给了家族词时扣 |
| 基因名精确匹配 | +15 | gene_names 里逐字等于线索基因（ROS1 ≠ ROS1A） |
| 注释完整度 | +3×3 | 蛋白名/基因名/物种三项各 +3 |

## 歧义判定

取分数最高的两个候选：若 `second >= best - 8` 且 `best >= 45` 且两者**物种不同**，
返回 ``ambiguous``。物种相同则不算歧义（例如同一物种里的同家族蛋白，由基因名区分）。
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 注入点：单测传假 fetch，真实运行用 _default_fetch_json（带重试）。
FetchJson = Callable[[str], Any]

UNIPROT_SEARCH = "https://rest.uniprot.org/uniprotkb/search"
UNIPROT_ENTRY = "https://rest.uniprot.org/uniprotkb/{}.json"
_SEARCH_FIELDS = ("accession,id,protein_name,gene_names,organism_name,organism_id,"
                  "reviewed,length,xref_pdb")
_USER_AGENT = "docking-agent/0.10 (+local resolve)"
_TIMEOUT = 45
_RETRIES = 2

# 歧义判定阈值（写成常量便于测试与文档引用）
AMBIGUITY_MARGIN = 8.0
AMBIGUITY_MIN_SCORE = 45.0
MIN_ACCEPT_SCORE = 25.0
# 单一候选但置信度不足的分数线（未 reviewed / 物种不符 / 名称部分匹配都会被判低置信）
LOW_CONFIDENCE_SCORE = 55.0

# 候选排序时的物种偏好（只影响**展示顺序**，不影响「选谁」——选谁由用户决定）：
# 常见生物医学模式物种排在前面，让 choices 的头几个最可能是用户想要的。
_SPECIES_RANK = {9606: 0, 10090: 1, 10116: 2, 3702: 3}

# accession（6/10 位）与 entry ID（EGFR_HUMAN）
_ENTRY_ID_RE = re.compile(r"^[A-Z0-9]{1,10}_[A-Z0-9]{1,10}$")
_UNIPROT_ACCESSION_RE = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$",
    re.IGNORECASE)
# 基因/拉丁 token：ROS1 / EGFR / TP53 —— 要求首字母大写，避免把普通小写英文当基因
_GENE_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}")
_GENE_STOPWORDS = {"DNA", "RNA", "AND", "OR", "THE", "PDB", "ATP", "GTP", "NAD", "FAD",
                   "SMILES", "CID", "INCHI", "ID", "AP", "EM", "PCR", "BLAST", "API",
                   "HTTP", "URL", "JSON", "CSV", "PDBQT", "SDF", "MOL", "OK"}


class ResolveError(RuntimeError):
    """UniProt 等在线检索不可用（网络/服务错误）。"""


# --------------------------------------------------------------------------- #
# 名称归一化
# --------------------------------------------------------------------------- #
# 物种线索 → UniProt 物种。顺序即优先级（先具体后泛化）：
# 「拟南芥」先于「植物」，避免把具体物种降级成泛化线索。
SPECIES_HINTS: Tuple[Dict[str, Any], ...] = (
    {"keys": ("拟南芥", "arabidopsis thaliana", "arabidopsis", "thaliana"),
     "name": "Arabidopsis thaliana", "taxid": 3702, "is_plant": True, "matched": "拟南芥"},
    {"keys": ("水稻", "oryza sativa", "oryza", "rice"),
     "name": "Oryza sativa", "taxid": 39947, "is_plant": True, "matched": "水稻"},
    {"keys": ("玉米", "zea mays", "maize", "corn"),
     "name": "Zea mays", "taxid": 4577, "is_plant": True, "matched": "玉米"},
    {"keys": ("大豆", "glycine max", "soybean"),
     "name": "Glycine max", "taxid": 3847, "is_plant": True, "matched": "大豆"},
    {"keys": ("烟草", "nicotiana", "tobacco"),
     "name": "Nicotiana tabacum", "taxid": 4097, "is_plant": True, "matched": "烟草"},
    {"keys": ("番茄", "solanum lycopersicum", "tomato"),
     "name": "Solanum lycopersicum", "taxid": 4081, "is_plant": True, "matched": "番茄"},
    {"keys": ("植物", "plant", "plants", "plantae"),
     "name": "Arabidopsis thaliana", "taxid": 3702, "is_plant": True, "matched": "植物（模式植物拟南芥）"},
    {"keys": ("人类", "人源", "human", "homo sapiens"),
     "name": "Homo sapiens", "taxid": 9606, "is_plant": False, "matched": "人类"},
    {"keys": ("小鼠", "mouse", "mus musculus"),
     "name": "Mus musculus", "taxid": 10090, "is_plant": False, "matched": "小鼠"},
    {"keys": ("大鼠", "rat", "rattus norvegicus"),
     "name": "Rattus norvegicus", "taxid": 10116, "is_plant": False, "matched": "大鼠"},
    {"keys": ("酵母", "yeast", "saccharomyces"),
     "name": "Saccharomyces cerevisiae", "taxid": 559292, "is_plant": False, "matched": "酵母"},
    {"keys": ("大肠杆菌", "escherichia coli", "e. coli"),
     "name": "Escherichia coli", "taxid": 562, "is_plant": False, "matched": "大肠杆菌"},
)

# 候选物种名里的植物关键词（用于「同为植物」加分，而非声称分类学精确）
_PLANT_ORGANISM_KEYS = ("arabidopsis", "oryza", "zea ", "zea mays", "glycine", "solanum",
                        "nicotiana", "populus", "vitis", "sorghum", "brachypodium",
                        "medicago", "physcomitrella", "selaginella", "marchantia",
                        "chlamydomonas", "lactuca", "brassica", "hordeum", "triticum",
                        "phaseolus", "gossypium", "cucumis", "citrus", "prunus", "malus",
                        "spinacia", "daucus", "helianthus", "ricinus", "manihot", "lotus")

# 中文家族词 → 英文检索词（顺序即匹配优先级：长词先匹配并「吃掉」已匹配片段，
# 避免「去甲基化酶」里的「甲基化」被误判成 methylation）。
FAMILY_TERMS_ZH: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("去甲基化酶", ("demethylase", "DNA glycosylase", "glycosylase", "demethylation")),
    ("去甲基化", ("demethylase", "demethylation", "glycosylase")),
    ("甲基转移酶", ("methyltransferase",)),
    ("甲基化酶", ("methyltransferase", "methylase")),
    ("甲基化", ("methylation", "methyltransferase")),
    ("糖基化酶", ("glycosylase",)),
    ("激酶", ("kinase",)),
    ("磷酸酶", ("phosphatase",)),
    ("蛋白酶", ("protease", "peptidase")),
    ("聚合酶", ("polymerase",)),
    ("连接酶", ("ligase",)),
    ("水解酶", ("hydrolase",)),
    ("氧化酶", ("oxidase",)),
    ("还原酶", ("reductase",)),
    ("脱氢酶", ("dehydrogenase",)),
    ("异构酶", ("isomerase",)),
    ("转运蛋白", ("transporter",)),
    ("通道蛋白", ("channel",)),
    ("受体", ("receptor",)),
    ("蛋白", ("protein",)),
    ("酶", ("enzyme",)),
)

# 英文家族词（用户直接写英文时也能被识别）
FAMILY_TERMS_EN: Tuple[str, ...] = (
    "demethylase", "glycosylase", "methyltransferase", "methylase", "kinase",
    "phosphatase", "protease", "peptidase", "polymerase", "ligase", "hydrolase",
    "oxidase", "reductase", "dehydrogenase", "isomerase", "transporter", "channel",
    "receptor", "protein", "enzyme",
)

# 过于宽泛的家族词：**保留在映射表里**（名称归一化要认识「受体/蛋白/酶」），
# 但**不用它构造 UniProt 检索式** —— `(receptor) AND reviewed:true` 会命中成百上千个
# 无关受体，把「查不到的假受体」变成一堆噪声候选。检索式只用具体家族词（demethylase…）。
GENERIC_FAMILY_TERMS = {"protein", "enzyme", "receptor", "channel"}

# 中文常见小分子名 → PubChem 英文名（PubChem 的 name 检索不认中文）。
# 只做「翻译」，绝不臆造 SMILES；命不中就走现有分子侧提问规则。
LIGAND_ALIASES_ZH: Dict[str, str] = {
    "代森锰锌": "Mancozeb", "代森猛锌": "Mancozeb", "代森锌": "Zineb",
    "代森锰": "Maneb", "代森联": "Metiram", "丙森锌": "Propineb",
    "华法林": "Warfarin", "布洛芬": "Ibuprofen", "阿司匹林": "Aspirin",
    "乙醇": "Ethanol", "甲醇": "Methanol", "咖啡因": "Caffeine",
    "二甲双胍": "Metformin", "伊马替尼": "Imatinib", "吉非替尼": "Gefitinib",
}


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip().lower()


def normalize_molecule_name(text: str) -> Tuple[str, str]:
    """中文小分子名 → PubChem 英文名；返回 (检索名, 命中的别名)；未命中原样返回。"""
    raw = str(text or "").strip()
    key = _norm_key(raw)
    for zh, en in LIGAND_ALIASES_ZH.items():
        if _norm_key(zh) == key:
            return en, zh
    return raw, ""


def extract_gene_tokens(text: str) -> List[str]:
    """抽取拉丁/基因 token（`[A-Z][A-Z0-9]{1,9}`），去重并剔除常见全大写缩写。"""
    out: List[str] = []
    for m in _GENE_RE.finditer(str(text or "")):
        token = m.group(0)
        if token in _GENE_STOPWORDS:
            continue
        if token not in out:
            out.append(token)
    return out


def extract_species(text: str) -> Optional[Dict[str, Any]]:
    """从文本推断物种线索（中文/英文），返回含 taxid 与 is_plant 的字典。"""
    low = str(text or "").lower()
    for spec in SPECIES_HINTS:
        for key in spec["keys"]:
            if key.lower() in low:
                return {"name": spec["name"], "taxid": int(spec["taxid"]),
                        "is_plant": bool(spec["is_plant"]), "matched": spec["matched"]}
    return None


def map_family_terms(text: str) -> List[str]:
    """中文/英文家族词 → 英文检索词（长词优先，已匹配片段不再参与后续匹配）。"""
    compact = re.sub(r"\d+", "", str(text or ""))
    remaining = compact
    terms: List[str] = []
    for zh, mapped in sorted(FAMILY_TERMS_ZH, key=lambda kv: -len(kv[0])):
        if zh and zh in remaining:
            terms.extend(mapped)
            remaining = remaining.replace(zh, "　" * len(zh))
    low = compact.lower()
    for en in FAMILY_TERMS_EN:
        if re.search(rf"(?<![a-z]){re.escape(en)}(?![a-z])", low):
            terms.append(en)
    return list(dict.fromkeys(terms))


def normalize_query(text: str) -> Dict[str, Any]:
    """把用户口语化点名归一化成检索线索（基因/物种/家族词/accession）。"""
    raw = str(text or "").strip()
    stripped = raw.strip().strip('"').strip("'")
    upper = stripped.upper()
    accession = stripped.upper() if _UNIPROT_ACCESSION_RE.match(stripped) else ""
    entry_id = upper if _ENTRY_ID_RE.match(upper) else ""
    genes = extract_gene_tokens(stripped)
    species = extract_species(stripped)
    family = map_family_terms(stripped)
    free_bits = list(genes) + list(family)
    if species:
        free_bits.append(species["name"])
    return {"raw": raw, "accession": accession, "entry_id": entry_id,
            "genes": genes, "species": species, "family_terms": family,
            "free_text": " ".join(dict.fromkeys(free_bits))}


# --------------------------------------------------------------------------- #
# HTTP（默认真实实现；单测注入假 fetch）
# --------------------------------------------------------------------------- #
def _default_fetch_json(url: str, timeout: int = _TIMEOUT) -> Any:
    last: Optional[Exception] = None
    for attempt in range(_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            if not body.strip():
                return {}
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 410):
                raise ResolveError(f"UniProt 检索被拒绝（HTTP {e.code}）：{url}") from e
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < _RETRIES:
            time.sleep(0.5 * (attempt + 1))
    raise ResolveError(f"UniProt 检索失败（已重试 {_RETRIES} 次）：{url} —— {last}")


# --------------------------------------------------------------------------- #
# entry → 候选
# --------------------------------------------------------------------------- #
def _protein_name(entry: Dict[str, Any]) -> str:
    desc = entry.get("proteinDescription") or {}
    rec = ((desc.get("recommendedName") or {}).get("fullName") or {}).get("value")
    if rec:
        return str(rec)
    for alt in (desc.get("alternativeNames") or []):
        val = ((alt or {}).get("fullName") or {}).get("value")
        if val:
            return str(val)
    return ""


def _gene_names(entry: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for gene in (entry.get("genes") or []):
        name = ((gene or {}).get("geneName") or {}).get("value")
        if name:
            out.append(str(name))
        for syn in ((gene or {}).get("synonyms") or []):
            val = (syn or {}).get("value")
            if val:
                out.append(str(val))
    return out


def _is_plant_organism(organism: str, taxid: Any) -> bool:
    low = str(organism or "").lower()
    if any(k in low for k in _PLANT_ORGANISM_KEYS):
        return True
    try:
        return int(taxid) == 3702
    except (TypeError, ValueError):
        return False


def entry_to_candidate(entry: Dict[str, Any]) -> Dict[str, Any]:
    """把 UniProt entry（search 或单条）转成统一候选结构。"""
    acc = str(entry.get("primaryAccession") or entry.get("accession") or "").upper()
    org = entry.get("organism") or {}
    taxid = org.get("taxonId")
    organism = str(org.get("scientificName") or "")
    entry_type = str(entry.get("entryType") or "")
    # 注意：不能只判断 `"reviewed" in entry_type` —— "unreviewed (TrEMBL)" 里也含 "reviewed"。
    reviewed = bool(entry.get("reviewed")) or "swiss-prot" in entry_type.lower()
    length = (entry.get("sequence") or {}).get("length") or 0
    try:
        length = int(length)
    except (TypeError, ValueError):
        length = 0
    pdbs: List[str] = []
    for x in (entry.get("uniProtKBCrossReferences") or []):
        if (x or {}).get("database") == "PDB" and x.get("id"):
            pdbs.append(str(x["id"]).upper())
    return {"accession": acc, "entry_id": str(entry.get("uniProtkbId")
                                              or entry.get("id") or ""),
            "protein": _protein_name(entry), "gene_names": _gene_names(entry),
            "organism": organism, "organism_id": taxid, "reviewed": reviewed,
            "length": length, "pdb_ids": list(dict.fromkeys(pdbs)),
            "is_plant": _is_plant_organism(organism, taxid)}


# --------------------------------------------------------------------------- #
# 打分与歧义
# --------------------------------------------------------------------------- #
def score_candidate(candidate: Dict[str, Any], query: Dict[str, Any]) -> Tuple[float, List[str]]:
    """候选打分（纯函数，规则见模块 docstring）。"""
    score = 0.0
    reasons: List[str] = []
    if candidate.get("reviewed"):
        score += 40
        reasons.append("Swiss-Prot 已审阅 +40")
    species = query.get("species")
    if species:
        if candidate.get("organism_id") is not None \
                and str(candidate.get("organism_id")) == str(species["taxid"]):
            score += 30
            reasons.append(f"物种精确匹配 {species['name']}(taxid {species['taxid']}) +30")
        elif species.get("is_plant") and candidate.get("is_plant"):
            score += 18
            reasons.append(f"同为植物（线索 {species['matched']}，候选 {candidate.get('organism')}）+18")
        else:
            score -= 25
            reasons.append(f"物种不符（线索 {species['name']}，候选 {candidate.get('organism')}）-25")
    family = query.get("family_terms") or []
    if family:
        blob = f"{candidate.get('protein', '')} {' '.join(candidate.get('gene_names') or [])}".lower()
        hit = next((t for t in family if t.lower() in blob), None)
        if hit:
            score += 20
            reasons.append(f"蛋白名含家族词 {hit} +20")
        else:
            score -= 10
            reasons.append("蛋白名不含映射到的家族词 -10")
    genes = {g.upper() for g in (query.get("genes") or [])}
    if genes and genes & {g.upper() for g in (candidate.get("gene_names") or [])}:
        score += 15
        reasons.append("基因名精确匹配 +15")
    annotation = 3 * int(bool(candidate.get("protein"))) \
        + 3 * int(bool(candidate.get("gene_names"))) + 3 * int(bool(candidate.get("organism")))
    score += annotation
    reasons.append(f"注释完整度 +{annotation}")
    return round(score, 2), reasons


def decide_status(candidates: Sequence[Dict[str, Any]],
                  query: Optional[Dict[str, Any]] = None) -> str:
    """按打分与线索决定 resolved / ambiguous / low_confidence / not_found。

    三态（对应产品要求：不确定就让用户选）：
      * ``resolved``：唯一候选，且所有**用户给出的**线索都强匹配（reviewed + 基因精确 +
        物种精确 + 蛋白名含家族词）→ 自动继续；
      * ``ambiguous``：有多个分数接近的候选（无法用物种/上下文消歧）→ 列候选让用户选；
      * ``low_confidence``：只有 1 个候选，但置信度不足（未 reviewed / 物种不符 /
        名称只部分匹配）→ 也要列出来让用户确认，不替用户决定；
      * ``not_found``：一个都没查到。
    """
    if not candidates:
        return "not_found"
    best = candidates[0]
    best_score = float(best.get("score") or 0)
    if best_score < MIN_ACCEPT_SCORE:
        return "not_found"
    query = query or {}
    # 精确标识符（accession / entry ID）本身即权威，不再做置信度推断
    if query.get("accession") or query.get("entry_id"):
        return "resolved"
    if len(candidates) >= 2:
        second = candidates[1]
        gap = best_score - float(second.get("score") or 0)
        if gap <= AMBIGUITY_MARGIN and best_score >= AMBIGUITY_MIN_SCORE:
            return "ambiguous"
    if not _is_confident(best, query):
        return "low_confidence"
    return "resolved"


def _gene_exact_match(candidate: Dict[str, Any], query: Dict[str, Any]) -> bool:
    genes = {g.upper() for g in (query.get("genes") or [])}
    return bool(genes) and bool(genes & {g.upper() for g in (candidate.get("gene_names") or [])})


def _family_match(candidate: Dict[str, Any], query: Dict[str, Any]) -> bool:
    family = query.get("family_terms") or []
    if not family:
        return True
    blob = f"{candidate.get('protein', '')} {' '.join(candidate.get('gene_names') or [])}".lower()
    return any(t.lower() in blob for t in family)


def _is_confident(candidate: Dict[str, Any], query: Dict[str, Any]) -> bool:
    """单一候选的置信度判定（用于 low_confidence 状态）。"""
    if float(candidate.get("score") or 0) < LOW_CONFIDENCE_SCORE:
        return False
    if not candidate.get("reviewed"):
        return False
    species = query.get("species")
    if species:
        exact = (candidate.get("organism_id") is not None
                 and str(candidate.get("organism_id")) == str(species["taxid"]))
        plant_ok = bool(species.get("is_plant")) and bool(candidate.get("is_plant"))
        if not (exact or plant_ok):
            return False
    genes = query.get("genes") or []
    if genes and not _gene_exact_match(candidate, query):
        return False
    if not _family_match(candidate, query):
        return False
    return True


# --------------------------------------------------------------------------- #
# 多策略检索
# --------------------------------------------------------------------------- #
def build_uniprot_queries(query: Dict[str, Any]) -> List[Tuple[str, str]]:
    """按精度从高到低构造 UniProt 检索式（strategy, query）。"""
    out: List[Tuple[str, str]] = []

    def _add(strategy: str, q: str) -> None:
        q = q.strip()
        if q and (strategy, q) not in out:
            out.append((strategy, q))

    if query.get("accession"):
        _add("accession", f"accession:{query['accession']}")
        return out
    if query.get("entry_id"):
        _add("entry-id", f"id:{query['entry_id']}")
    species = query.get("species")
    genes = list(query.get("genes") or [])[:3]
    family = [t for t in (query.get("family_terms") or []) if t.lower() not in GENERIC_FAMILY_TERMS]
    for gene in genes:
        if species:
            _add("gene+species", f"gene_exact:{gene} AND organism_id:{species['taxid']}")
        _add("gene+reviewed", f"gene_exact:{gene} AND reviewed:true")
        _add("gene", f"gene_exact:{gene}")
    if family and species:
        _add("family+species", f"({family[0]}) AND (organism_id:{species['taxid']})")
        if species.get("is_plant"):
            _add("family+plant", f"({family[0]}) AND (plant OR Arabidopsis)")
        else:
            _add("family+organism", f"({family[0]}) AND ({species['name']})")
    elif family:
        _add("family", f"({family[0]}) AND (reviewed:true)")
    if query.get("free_text"):
        _add("free-text", query["free_text"])
    return out


def _search_url(query: str, size: int) -> str:
    return (f"{UNIPROT_SEARCH}?query={urllib.parse.quote(query, safe='')}"
            f"&fields={_SEARCH_FIELDS}&format=json&size={int(size)}")


def resolve_receptor_name(text: str, fetch: Optional[FetchJson] = None,
                          size: int = 8, entry_fetch: Optional[FetchJson] = None) -> Dict[str, Any]:
    """把受体点名解析成 UniProt 候选（多策略检索 + 打分 + 歧义判定）。

    返回::

        {"status": "resolved"|"ambiguous"|"not_found",
         "query": {...归一化线索...},
         "selected": 候选或 None, "candidates": [...], "attempts": [...],
         "total_candidates": n}

    `attempts` 逐条记录「试过哪条检索式、命中几条」，失败路径会原样交给用户看，
    满足「提问必须列出已尝试的检索」的要求。
    """
    fetch = fetch or _default_fetch_json
    entry_fetch = entry_fetch or _default_fetch_json
    nq = normalize_query(text)
    candidates: Dict[str, Dict[str, Any]] = {}
    attempts: List[Dict[str, Any]] = []

    # accession / entry-id：直接取单条，最精确
    if nq["accession"] or nq["entry_id"]:
        ident = nq["accession"] or nq["entry_id"]
        url = UNIPROT_ENTRY.format(urllib.parse.quote(ident, safe="")) + \
            f"?fields={_SEARCH_FIELDS}"
        try:
            entry = entry_fetch(url)
            if isinstance(entry, dict) and entry.get("primaryAccession"):
                cand = entry_to_candidate(entry)
                candidates[cand["accession"]] = cand
                attempts.append({"strategy": "accession", "query": ident, "hits": 1})
            else:
                attempts.append({"strategy": "accession", "query": ident, "hits": 0})
        except ResolveError as e:
            attempts.append({"strategy": "accession", "query": ident, "hits": 0,
                             "error": str(e)[:200]})
        # accession 直查命中即用；不命中再退回检索，避免把 accession 当基因名乱搜
        if candidates:
            scored = _score_all(candidates, nq)
            return _result(scored, nq, attempts, size)

    for strategy, q in build_uniprot_queries(nq):
        try:
            data = fetch(_search_url(q, size))
        except ResolveError as e:
            logger.info("UniProt 检索式失败（%s）：%s", q, e)
            attempts.append({"strategy": strategy, "query": q, "hits": 0,
                             "error": str(e)[:200]})
            continue
        results = list((data or {}).get("results") or [])
        attempts.append({"strategy": strategy, "query": q, "hits": len(results)})
        for raw_entry in results:
            if not isinstance(raw_entry, dict):
                continue
            cand = entry_to_candidate(raw_entry)
            if not cand["accession"]:
                continue
            existing = candidates.get(cand["accession"])
            if existing is not None:
                if strategy not in existing["strategies"]:
                    existing["strategies"].append(strategy)
                continue
            cand["strategies"] = [strategy]
            candidates[cand["accession"]] = cand

    scored = _score_all(candidates, nq)
    return _result(scored, nq, attempts, size)


def _score_all(candidates: Dict[str, Dict[str, Any]],
               query: Dict[str, Any]) -> List[Dict[str, Any]]:
    for cand in candidates.values():
        cand.setdefault("strategies", [])
        score, reasons = score_candidate(cand, query)
        cand["score"] = score
        cand["reasons"] = reasons
    return sorted(candidates.values(),
                  key=lambda c: (-float(c["score"]), _species_sort_key(c), str(c["accession"])))


def _species_sort_key(candidate: Dict[str, Any]) -> int:
    try:
        return _SPECIES_RANK.get(int(candidate.get("organism_id")), 9)
    except (TypeError, ValueError):
        return 9


def _result(scored: List[Dict[str, Any]], query: Dict[str, Any],
            attempts: List[Dict[str, Any]], size: int) -> Dict[str, Any]:
    status = decide_status(scored, query)
    top = scored[:max(1, int(size))]
    selected = top[0] if (status == "resolved" and top) else None
    if status == "ambiguous" and len(top) >= 2:
        logger.info("受体解析歧义：top=%s(%s) vs %s(%s)",
                    top[0]["accession"], top[0]["organism"], top[1]["accession"], top[1]["organism"])
    elif status == "low_confidence" and top:
        logger.info("受体解析置信度不足：%s(%s) score=%s",
                    top[0]["accession"], top[0]["organism"], top[0].get("score"))
    return {"status": status, "query": query, "selected": selected, "candidates": top,
            "attempts": attempts, "total_candidates": len(scored)}


def summarize_attempts(attempts: Sequence[Dict[str, Any]]) -> List[str]:
    """把 attempts 渲染成给用户看的「已尝试的检索」清单。"""
    lines: List[str] = []
    for item in attempts or []:
        if not isinstance(item, dict):
            continue
        err = f"（失败：{item.get('error')}）" if item.get("error") else ""
        lines.append(f"[{item.get('strategy')}] {item.get('query')} → {item.get('hits', 0)} 条命中{err}")
    return lines


def candidate_brief(cand: Dict[str, Any]) -> Dict[str, Any]:
    """候选的精简视图（提问/报告用，避免把整条 entry 塞进上下文）。"""
    return {"accession": cand.get("accession"), "entry_id": cand.get("entry_id"),
            "protein": cand.get("protein"), "organism": cand.get("organism"),
            "organism_id": cand.get("organism_id"), "gene_names": cand.get("gene_names"),
            "reviewed": cand.get("reviewed"), "score": cand.get("score"),
            "pdb_ids": cand.get("pdb_ids"), "reasons": cand.get("reasons"),
            "strategies": cand.get("strategies")}


def candidate_structure_hint(cand: Dict[str, Any]) -> str:
    """候选的结构来源提示（有 PDB 交叉引用 → RCSB，否则 AlphaFold 预测）。"""
    pdbs = cand.get("pdb_ids") or []
    return f"RCSB {'/'.join(pdbs[:2])}" if pdbs else "AlphaFold 预测"


def receptor_choices(candidates: Sequence[Dict[str, Any]],
                     limit: int = 5) -> List[Dict[str, Any]]:
    """把受体候选转成前端可点选的结构化选项。

    每项：``{id, kind, label, value, prompt, detail}``
      * ``label``  人看的中文标签（物种 · 蛋白名 · accession · 结构来源 · 打分）；
      * ``value``  机器可用值（UniProt accession）；
      * ``prompt`` 前端点击后原样发出的追问（同一 conversation_id 的下一轮）；
      * ``detail`` accession/物种/蛋白名/结构来源/打分理由，供 UI 展开与报告溯源。

    **不提供「改用系统默认受体」选项**：预置受体只用于内部测试，系统也没有默认受体
    （历史缺陷：这里曾无条件追加一个 `thrombin` 选项，与产品规则和代码守卫直接冲突）。
    """
    choices: List[Dict[str, Any]] = []
    for cand in list(candidates)[:limit]:
        brief = candidate_brief(cand)
        acc = str(brief.get("accession") or "")
        if not acc:
            continue
        structure = candidate_structure_hint(brief)
        genes = "/".join(brief.get("gene_names") or []) or "—"
        choices.append({
            "id": f"receptor:{acc}", "kind": "receptor",
            "label": (f"{brief.get('organism')} {brief.get('protein')} · {acc} · "
                      f"{structure} · 打分 {brief.get('score')}"),
            "value": acc,
            "prompt": (f"用 {acc}（{brief.get('organism')}，{brief.get('protein')}）"
                       "作为受体继续对接筛选"),
            "detail": {"accession": acc, "organism": brief.get("organism"),
                       "protein": brief.get("protein"), "gene_names": brief.get("gene_names"),
                       "structure_source": structure, "pdb_ids": brief.get("pdb_ids") or [],
                       "score": brief.get("score"), "reasons": brief.get("reasons") or [],
                       "reviewed": brief.get("reviewed"), "genes": genes},
        })
    return choices
    return choices
