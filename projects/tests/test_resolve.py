"""`core.resolve` 的离线单测：名称归一化、打分、歧义/低置信判定与可选项生成。

全部用**注入的假 fetcher**（不联网）；真实联网链路由 `scripts`/端到端证据覆盖。
规则改动必须同步更新这里（打分表见 `core/resolve.py` 模块 docstring）。
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

from docking_agent.core import resolve  # noqa: E402


# --------------------------------------------------------------------------- #
# 假 UniProt 数据 + 假 fetcher
# --------------------------------------------------------------------------- #
def _entry(acc, protein, org, taxid, genes, reviewed=True, length=1000, pdbs=()):
    return {
        "primaryAccession": acc,
        "uniProtkbId": f"{genes[0]}_{org.split()[0].upper()}",
        "entryType": ("UniProtKB reviewed (Swiss-Prot)" if reviewed
                      else "UniProtKB unreviewed (TrEMBL)"),
        "proteinDescription": {"recommendedName": {"fullName": {"value": protein}}},
        "genes": [{"geneName": {"value": g}} for g in genes],
        "organism": {"scientificName": org, "taxonId": taxid},
        "sequence": {"length": length},
        "uniProtKBCrossReferences": [{"database": "PDB", "id": p} for p in pdbs],
    }


ARAB = _entry("Q9SJQ6", "DNA glycosylase/AP lyase ROS1", "Arabidopsis thaliana", 3702,
              ["ROS1"], pdbs=("7YHP",))
HUMAN = _entry("P08922", "Proto-oncogene tyrosine-protein kinase ROS", "Homo sapiens", 9606,
               ["ROS1"], pdbs=("3ZBF",))
ORYZA = _entry("C7IW64", "Protein ROS1A", "Oryza sativa subsp. japonica", 39947, ["ROS1A"])
UNREVIEWED = _entry("A0JUNK1", "Uncharacterized protein", "Arabidopsis thaliana", 3702,
                    ["ABCX1"], reviewed=False)

ENTRIES = {e["primaryAccession"]: e for e in (ARAB, HUMAN, ORYZA, UNREVIEWED)}

QUERY_MAP = {
    "gene_exact:ROS1 AND organism_id:3702": ["Q9SJQ6"],
    "gene_exact:ROS1 AND reviewed:true": ["P08922", "Q9SJQ6"],
    "gene_exact:ROS1": ["P08922", "Q9SJQ6", "C7IW64"],
    "(demethylase) AND (organism_id:3702)": ["Q9SJQ6"],
    "(demethylase) AND (plant OR Arabidopsis)": ["Q9SJQ6", "C7IW64"],
    "ROS1 demethylase DNA glycosylase glycosylase demethylation Arabidopsis thaliana": ["Q9SJQ6"],
    "gene_exact:ABCX1 AND organism_id:3702": ["A0JUNK1"],
}


def fake_fetch(url: str) -> Dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.path.endswith(".json"):
        ident = parsed.path.rsplit("/", 1)[-1].replace(".json", "").upper()
        return ENTRIES.get(ident, {})
    query = urllib.parse.parse_qs(parsed.query).get("query", [""])[0]
    return {"results": [ENTRIES[a] for a in QUERY_MAP.get(query, [])]}


# --------------------------------------------------------------------------- #
# 名称归一化
# --------------------------------------------------------------------------- #
def test_extract_gene_tokens_and_stopwords() -> None:
    assert resolve.extract_gene_tokens("植物去甲基化酶ROS1") == ["ROS1"]
    assert resolve.extract_gene_tokens("用 EGFR 和 TP53 做对接") == ["EGFR", "TP53"]
    assert "DNA" not in resolve.extract_gene_tokens("DNA glycosylase ROS1")   # 常见缩写不当基因


def test_species_inference_with_chinese_and_english_hints() -> None:
    plant = resolve.extract_species("植物去甲基化酶ROS1")
    assert plant and plant["taxid"] == 3702 and plant["is_plant"] is True
    assert resolve.extract_species("拟南芥")["taxid"] == 3702
    assert resolve.extract_species("human ROS1")["taxid"] == 9606
    assert resolve.extract_species("小鼠 Egfr")["taxid"] == 10090
    assert resolve.extract_species("水稻")["taxid"] == 39947
    assert resolve.extract_species("没有任何线索") is None


def test_family_mapping_does_not_confuse_demethylase_with_methylation() -> None:
    terms = resolve.map_family_terms("植物去甲基化酶")
    assert "demethylase" in terms and "DNA glycosylase" in terms
    assert "methylation" not in terms, "「去甲基化酶」不得被拆成 methylation"
    assert "methyltransferase" not in terms
    assert resolve.map_family_terms("甲基化酶")[0] == "methyltransferase"
    assert "kinase" in resolve.map_family_terms("EGFR kinase")


def test_normalize_query_collects_gene_species_family_and_accession() -> None:
    q = resolve.normalize_query("植物去甲基化酶ROS1")
    assert q["genes"] == ["ROS1"] and q["species"]["taxid"] == 3702
    assert "demethylase" in q["family_terms"] and q["accession"] == ""
    assert resolve.normalize_query("P08922")["accession"] == "P08922"
    assert resolve.normalize_query("P08922")["genes"] == ["P08922"]


def test_generic_family_terms_are_mapped_but_not_used_as_queries() -> None:
    """「蛋白/酶/受体」要能映射（名称归一化），但不能拿去做 UniProt 检索式。"""
    q = resolve.normalize_query("不存在的蛋白ZZZQQ9")
    assert "protein" in q["family_terms"]
    queries = [text for _, text in resolve.build_uniprot_queries(q)]
    assert queries, "至少要尝试基因/自由文本检索"
    assert not any("(protein)" in text or "(enzyme)" in text or "(receptor)" in text
                   for text in queries), queries
    # 具体家族词不受影响
    q2 = resolve.normalize_query("植物去甲基化酶ROS1")
    assert any("(demethylase)" in text for _, text in resolve.build_uniprot_queries(q2))


def test_molecule_chinese_alias_mapping() -> None:
    assert resolve.normalize_molecule_name("代森猛锌") == ("Mancozeb", "代森猛锌")
    assert resolve.normalize_molecule_name("代森锰锌") == ("Mancozeb", "代森锰锌")
    assert resolve.normalize_molecule_name("华法林") == ("Warfarin", "华法林")
    assert resolve.normalize_molecule_name("Mancozeb") == ("Mancozeb", "")


# --------------------------------------------------------------------------- #
# 打分与三态判定
# --------------------------------------------------------------------------- #
def test_score_rules_reviewed_species_family_and_gene() -> None:
    query = resolve.normalize_query("植物去甲基化酶ROS1")
    cand = resolve.entry_to_candidate(ARAB)
    score, reasons = resolve.score_candidate(cand, query)
    joined = " ".join(reasons)
    assert score == 114.0, reasons
    assert "已审阅" in joined and "物种精确匹配" in joined
    assert "家族词" in joined and "基因名精确匹配" in joined
    # 人源 ROS1 激酶：物种不符 + 不含 demethylase/glycosylase → 被压下去
    human_score, human_reasons = resolve.score_candidate(resolve.entry_to_candidate(HUMAN), query)
    assert human_score < score - 50
    assert any("物种不符" in r for r in human_reasons)


def test_species_clue_disambiguates_plant_ros1_from_human_kinase() -> None:
    result = resolve.resolve_receptor_name("植物去甲基化酶ROS1", fetch=fake_fetch)
    assert result["status"] == "resolved"
    best = result["selected"]
    assert best["accession"] == "Q9SJQ6"
    assert best["organism"] == "Arabidopsis thaliana"
    assert "glycosylase" in best["protein"].lower()
    assert best["pdb_ids"] == ["7YHP"]
    assert best["score"] == 114.0
    # 人源激酶也在候选里（诚实列出），但不在选中的位置
    accessions = [c["accession"] for c in result["candidates"]]
    assert "P08922" in accessions
    assert result["candidates"][0]["accession"] == "Q9SJQ6"


def test_no_species_clue_is_ambiguous_and_lists_candidates() -> None:
    result = resolve.resolve_receptor_name("ROS1", fetch=fake_fetch)
    assert result["status"] == "ambiguous", "只写 ROS1、没有物种线索 → 必须让用户选而不是乱选"
    assert result["selected"] is None
    top2 = {c["accession"] for c in result["candidates"][:2]}
    assert top2 == {"P08922", "Q9SJQ6"}, result["candidates"]
    organisms = {c["organism"] for c in result["candidates"][:2]}
    assert "Homo sapiens" in organisms and "Arabidopsis thaliana" in organisms


def test_low_confidence_single_candidate_is_not_auto_selected() -> None:
    """单一候选但未 reviewed/置信度不足 → low_confidence（也要让用户确认）。"""
    result = resolve.resolve_receptor_name("植物 ABCX1", fetch=fake_fetch)
    assert result["status"] == "low_confidence"
    assert result["selected"] is None
    assert result["candidates"][0]["accession"] == "A0JUNK1"
    assert result["candidates"][0]["reviewed"] is False


def test_not_found_records_every_attempt() -> None:
    result = resolve.resolve_receptor_name("ZZZQQ9", fetch=fake_fetch)
    assert result["status"] == "not_found"
    assert result["candidates"] == [] and result["selected"] is None
    assert len(result["attempts"]) >= 2
    lines = resolve.summarize_attempts(result["attempts"])
    assert any("gene_exact:ZZZQQ9" in line for line in lines)
    assert all("0 条命中" in line for line in lines)


def test_accession_direct_lookup_is_always_resolved() -> None:
    result = resolve.resolve_receptor_name("P08922", fetch=fake_fetch, entry_fetch=fake_fetch)
    assert result["status"] == "resolved"
    assert result["selected"]["accession"] == "P08922"


def test_decide_status_thresholds() -> None:
    assert resolve.decide_status([]) == "not_found"
    only_low = [{"accession": "X", "score": 10.0, "organism_id": 1, "reviewed": False,
                 "protein": "p", "gene_names": ["g"], "organism": "o"}]
    assert resolve.decide_status(only_low, {"genes": ["g"]}) == "not_found"


# --------------------------------------------------------------------------- #
# 结构化选项（choices）
# --------------------------------------------------------------------------- #
def test_receptor_choices_are_structured_and_never_offer_a_default_receptor() -> None:
    """候选选项只含真实检索结果 —— **不再追加「改用系统默认受体」**。

    历史缺陷：这里曾无条件追加一个 `thrombin` 选项，与「预置受体只用于内部测试 /
    系统没有默认受体」的产品规则（以及 `run_docking` 的护栏）直接冲突：
    用户可以点一个预置测试受体当研究靶点跑出无关结果。
    """
    resolved = resolve.resolve_receptor_name("植物去甲基化酶ROS1", fetch=fake_fetch)
    choices = resolve.receptor_choices(resolved["candidates"])
    assert len(choices) >= 1
    first = choices[0]
    assert first["kind"] == "receptor"
    assert first["value"] == "Q9SJQ6"
    assert "Q9SJQ6" in first["label"] and "打分" in first["label"]
    assert first["prompt"].startswith("用 Q9SJQ6")
    assert first["detail"]["accession"] == "Q9SJQ6"
    assert first["detail"]["pdb_ids"] == ["7YHP"]
    # 任何一项都不得是预置/默认受体
    assert all("默认受体" not in str(c.get("label") or "") for c in choices), choices
    assert all(str(c.get("value") or "") != "thrombin" for c in choices), choices
    # 没有候选时不下发任何选项（由提问文案给出三条出路）
    assert resolve.receptor_choices([]) == []


def test_candidate_structure_hint_prefers_rcsb_then_alphafold() -> None:
    assert resolve.candidate_structure_hint(resolve.entry_to_candidate(ARAB)) == "RCSB 7YHP"
    assert resolve.candidate_structure_hint(resolve.entry_to_candidate(ORYZA)) == "AlphaFold 预测"
