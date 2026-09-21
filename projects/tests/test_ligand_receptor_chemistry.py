"""「特殊化学」处理规范的回归测试。

用户要求：**特殊分子/特殊体系本来就该交给 Agent 处理** —— 那么底层工具的责任就是
「不静默丢掉信息、不悄悄改变化学、把事实如实报出来」。本文件看护三件事：

1. 配体侧：盐/反离子、金属、净电荷、未定义手性 → 必须有可解释的处理与告警
   （多片段时按最大有机片段对接，并写清移除了什么、为什么）。
2. 受体侧：金属/辅因子/水被丢弃或按要求保留 → 必须逐残基名计数上报
   （`dropped_hetatm` / `kept_hetatm` / `dropped_waters`），并进入给模型看的 notes。
3. 共晶配体识别：必须取「最大的非水/非添加剂 HETATM 团」，不能被金属离子、
   硫酸根、甘油这类东西把位点中心带偏。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()


# --------------------------------------------------------------------------- #
# 1. 配体化学体检（describe_ligand）
# --------------------------------------------------------------------------- #
def test_plain_ligand_is_untouched():
    """普通中性单片段分子：在 keep 策略下不得被改写，也不该产生噪声告警。

    注意：**默认策略是 ph（生理 pH 7.4）**，阿司匹林的羧酸会被去质子化（这是有意的化学处理，
    见 test_default_ph_policy_deprotonates_acid）。要验证"一字不改"必须显式指定 keep。
    """
    from docking_agent.core.ligands import describe_ligand

    d = describe_ligand("CC(=O)Oc1ccccc1C(=O)O", protonation="keep")   # 阿司匹林
    assert d["ok"] is True
    assert d["smiles"] == d["original_smiles"] == "CC(=O)Oc1ccccc1C(=O)O"
    assert d["removed_fragments"] == []
    assert d["warnings"] == [], d["warnings"]


def test_default_ph_policy_deprotonates_acid() -> None:
    """默认策略 = ph（7.4）：羧酸按生理 pH 去质子化，且必须留痕（不是静默改写）。"""
    from docking_agent.core.ligands import DEFAULT_PH, describe_ligand

    d = describe_ligand("CC(=O)Oc1ccccc1C(=O)O")          # 阿司匹林
    assert d["smiles"] == "CC(=O)Oc1ccccc1C(=O)[O-]"
    assert d["original_smiles"] == "CC(=O)Oc1ccccc1C(=O)O", "原始输入必须保留"
    prot = d["protonation"]
    assert prot["policy"] == "ph" and prot["ph"] == DEFAULT_PH and prot["applied"] is True
    assert [r["name"] for r in prot["rules"]] == ["羧酸"]
    assert any("质子化态已按运行级策略调整" in w for w in d["warnings"])
    assert d["facts"]["num_fragments"] == 1
    assert d["facts"]["formal_charge"] == -1, "pH 7.4 下羧酸去质子化 → 带 -1"
    assert d["facts"]["formal_charge_input"] == 0
    assert d["facts"]["has_metal"] is False


def test_salt_counterion_is_stripped_and_explained():
    """乙酸钠：必须只对接有机片段，写明移除了 Na+（反离子），并按运行级策略处理质子化态。

    契约（v0.20）：`neutralize`（默认）会把带净电荷的有机片段中和后再对接，
    但**原始 SMILES 必须保留**、改动必须**逐分子留痕**；`keep` 时保持输入形式。
    """
    from docking_agent.core.ligands import describe_ligand

    d = describe_ligand("CC(=O)[O-].[Na+]", protonation="neutralize")
    assert d["ok"] is True
    assert d["smiles"] == "CC(=O)O", "neutralize 策略下，带净电荷的片段会被中和后再对接"
    assert d["original_smiles"] == "CC(=O)[O-].[Na+]"
    assert d["input_smiles"] == "CC(=O)[O-]", "拆分后的原始形式要留档"
    prot = d["protonation"]
    assert prot["applied"] is True and prot["policy"] == "neutralize"
    assert prot["charge_before"] == -1 and prot["charge_after"] == 0
    removed = d["removed_fragments"]
    assert len(removed) == 1
    assert "Na" in removed[0]["smiles"] and removed[0]["kind"] == "反离子"
    text = "；".join(d["warnings"])
    assert "2 个片段" in text and "反离子" in text
    assert "质子化态已按运行级策略调整" in text and "净电荷 -1 → +0" in text

    # keep：完全保持输入形式，但如实告警「净电荷 -1 需确认」
    kept = describe_ligand("CC(=O)[O-].[Na+]", protonation="keep")
    assert kept["smiles"] == "CC(=O)[O-]"
    assert "净电荷 -1" in "；".join(kept["warnings"])


def test_metal_containing_ligand_is_flagged_not_guessed():
    """含金属配体：工具不能假装能算，必须明确告警（由 Agent 决定怎么处理）。"""
    from docking_agent.core.ligands import describe_ligand

    d = describe_ligand("c1ccc2ccccc2c1.[Zn+2]")
    assert d["ok"] is True
    assert any("金属" in w for w in d["warnings"]), d["warnings"]


def test_undefined_stereocenter_is_reported():
    from docking_agent.core.ligands import describe_ligand

    d = describe_ligand("CC(N)C(=O)O")                    # 丙氨酸，α-碳未定义
    assert d["facts"]["undefined_stereocenters"] == 1
    assert any("手性" in w for w in d["warnings"])

    defined = describe_ligand("C[C@@H](N)C(=O)O")
    assert defined["facts"]["undefined_stereocenters"] == 0


def test_unparsable_smiles_reports_reason_instead_of_crashing():
    from docking_agent.core.ligands import describe_ligand

    d = describe_ligand("这不是一个SMILES")
    assert d["ok"] is False
    assert d["warnings"] and "无法解析" in d["warnings"][0]


# --------------------------------------------------------------------------- #
# 2. 受体准备：丢弃/保留的杂原子必须如实上报
# --------------------------------------------------------------------------- #
HEM_ATOMS = 8      # 共晶配体质子数（血红素在这里只取 8 个重原子）
GOL_ATOMS = 12     # 甘油（结晶添加剂）故意比 HEM 大


def _pdb_line(record: str, serial: int, name: str, resname: str, chain: str,
              resseq: int, x: float, y: float, z: float, element: str) -> str:
    """按 PDB 固定列宽写一行 —— 列位错了 resName 会被读成别的（测试自己先别踩这个坑）。"""
    return (f"{record:<6}{serial:>5} {name:<4}{'':1}{resname:>3} {chain:1}{resseq:>4}    "
            f"{x:>8.3f}{y:>8.3f}{z:>8.3f}  1.00 20.00          {element:>2}\n")


def _receptor_pdb_text() -> str:
    lines = ["HEADER    TEST RECEPTOR WITH HETATM\n"]
    atoms = [
        ("ATOM", 1, "N", "ALA", "A", 1, 10.000, 10.000, 10.000, "N"),
        ("ATOM", 2, "CA", "ALA", "A", 1, 11.400, 10.000, 10.000, "C"),
        ("ATOM", 3, "C", "ALA", "A", 1, 12.000, 11.400, 10.000, "C"),
        ("ATOM", 4, "O", "ALA", "A", 1, 11.400, 12.500, 10.000, "O"),
        ("ATOM", 5, "CB", "ALA", "A", 1, 11.900, 9.100, 10.700, "C"),
        ("ATOM", 6, "N", "GLY", "A", 2, 13.300, 11.500, 10.000, "N"),
        ("ATOM", 7, "CA", "GLY", "A", 2, 14.100, 12.800, 10.000, "C"),
        ("ATOM", 8, "C", "GLY", "A", 2, 15.500, 12.600, 10.000, "C"),
        ("ATOM", 9, "O", "GLY", "A", 2, 16.200, 13.600, 10.000, "O"),
        # 单原子金属离子：位置故意远离 HEM，用来验证「位点中心不被离子带偏」
        ("HETATM", 10, "ZN", "ZN", "A", 101, 5.000, 5.000, 5.000, "ZN"),
    ]
    # 血红素（共晶配体，HEM_ATOMS 个原子）—— 应当是选中的那一团
    hem = [(14.5, 12.0, 12.0, "FE"), (15.0, 12.5, 12.5, "N"), (15.5, 13.0, 12.0, "C"),
           (16.0, 12.5, 11.5, "C"), (16.5, 12.0, 12.0, "C"), (16.0, 11.5, 12.5, "C"),
           (15.0, 11.5, 11.5, "N"), (15.5, 11.8, 12.2, "N")]
    for i, (x, y, z, el) in enumerate(hem):
        atoms.append(("HETATM", 11 + i, el, "HEM", "A", 102, x, y, z, el))
    # 结晶添加剂（甘油）：原子数故意**多于** HEM。若黑名单失效，位点会被它抢走。
    gol = [(30.0 + 0.4 * i, 30.0, 30.0 + 0.3 * i) for i in range(GOL_ATOMS)]
    for i, (x, y, z) in enumerate(gol):
        atoms.append(("HETATM", 30 + i, "C", "GOL", "A", 103, x, y, z, "C"))
    atoms += [
        ("HETATM", 60, "O", "HOH", "A", 201, 20.000, 20.000, 20.000, "O"),
        ("HETATM", 61, "O", "HOH", "A", 202, 21.000, 20.000, 20.000, "O"),
    ]
    for a in atoms:
        lines.append(_pdb_line(*a))
    lines.append("END\n")
    return "".join(lines)


_RECEPTOR_WITH_HETATM = _receptor_pdb_text()


@pytest.fixture()
def receptor_pdb(tmp_path):
    p = tmp_path / "chem_receptor.pdb"
    p.write_text(_RECEPTOR_WITH_HETATM, encoding="utf-8")
    return str(p)


def test_dropped_hetatm_is_tallied_not_silently_lost(receptor_pdb):
    """标准流程会剔除水与杂原子，但必须逐残基名计数上报（金属/辅因子不能被抹掉）。"""
    from docking_agent.core.receptors import prepare_user_receptor

    spec = prepare_user_receptor(receptor_pdb)
    assert spec["dropped_waters"] == 2
    assert spec["dropped_hetatm"] == {"ZN": 1, "HEM": HEM_ATOMS, "GOL": GOL_ATOMS}
    assert spec["kept_hetatm"] == {}
    prepared = Path(spec["pdbqt"]).with_name(Path(spec["pdbqt"]).stem + "_prot.pdb")
    text = Path(str(Path(spec["pdbqt"]).parent / (Path(spec["pdbqt"]).stem + "_prot.pdb"))).read_text()
    assert "HETATM" not in text, "未指定保留时准备后的 PDB 不应含 HETATM"
    assert "ZN" not in text and "HEM" not in text


def test_keep_hetatm_keeps_metal_ions(receptor_pdb):
    """金属离子是「可直接保留」的一类：指定后必须真的进入受体 PDBQT。"""
    from docking_agent.core.receptors import prepare_user_receptor

    spec = prepare_user_receptor(receptor_pdb, keep_hetatm=("ZN",))
    assert spec["kept_hetatm"] == {"ZN": 1}
    assert spec["dropped_hetatm"] == {"HEM": HEM_ATOMS, "GOL": GOL_ATOMS}
    assert spec["unsupported_hetatm"] == []
    prot = Path(spec["pdbqt"]).parent / (Path(spec["pdbqt"]).stem + "_prot.pdb")
    text = prot.read_text()
    assert "ZN" in text and "HEM" not in text
    assert text.count("HETATM") == 1


def test_untemplatable_cofactor_is_reported_not_silently_dropped(receptor_pdb):
    """缺少 meeko 化学模板的大辅因子（HEM/NAD…）：不能悄悄丢，也不能让整次对接失败。

    期望行为：把能留的（ZN）留下，把留不下的（HEM）记进 `unsupported_hetatm`
    交给 Agent 判断（提供模板 / 换引擎 / 明确接受去辅因子结果）。
    """
    from docking_agent.core.receptors import prepare_user_receptor

    spec = prepare_user_receptor(receptor_pdb, keep_hetatm=("HEM", "ZN"))
    assert spec["kept_hetatm"] == {"ZN": 1}
    assert spec["unsupported_hetatm"] == ["HEM"]
    assert "HEM" in spec["dropped_hetatm"]


def test_receptor_prep_cache_follows_content_not_timestamp(receptor_pdb, tmp_path):
    """缓存必须按准备后结构的内容哈希判定。

    原来的时间戳比较有两个错误方向：prot 每次重写导致缓存恒失效（每次重跑 meeko），
    而 keep_hetatm 变化时又可能误用旧结果。这里用 mtime 不变的写法验证走的是哈希。
    """
    from docking_agent.core import receptors as R

    spec1 = R.prepare_user_receptor(receptor_pdb)
    pdbqt = spec1["pdbqt"]
    stat_before = os.stat(pdbqt)
    # 把输出文件改成一个「不可能由 meeko 生成」的哨兵：若走了缓存就不会被覆盖
    Path(pdbqt).write_text("SENTINEL-CACHED\n", encoding="utf-8")
    os.utime(pdbqt, (stat_before.st_atime, stat_before.st_mtime))
    spec2 = R.prepare_user_receptor(receptor_pdb)
    assert Path(spec2["pdbqt"]).read_text(encoding="utf-8") == "SENTINEL-CACHED\n", \
        "相同输入应复用缓存（按内容哈希）"

    # keep_hetatm 改变 → 内容哈希变化 → 必须重新准备，不能复用上面的哨兵
    spec3 = R.prepare_user_receptor(receptor_pdb, keep_hetatm=("ZN",))
    text = Path(spec3["pdbqt"]).read_text(encoding="utf-8", errors="ignore")
    assert "SENTINEL" not in text, "保留杂原子后必须重新准备受体"
    assert spec3["kept_hetatm"] == {"ZN": 1}


def test_docking_library_notes_report_dropped_hetatm(receptor_pdb):
    """给模型看的 notes 里必须出现「丢了哪些杂原子」，否则 Agent 无从判断。"""
    from docking_agent.core.docking import dock_library

    out = dock_library([{"name": "T1", "smiles": "CCO"}], receptor=receptor_pdb,
                       exhaustiveness=1, n_poses=1)
    assert out["status"] == "ok"
    joined = " ".join(out["notes"])
    assert "丢弃了非水杂原子" in joined, joined
    assert "HEM" in joined and f"HEM×{HEM_ATOMS}" in joined
    block = out["receptors"][0]
    assert block["dropped_hetatm"] == {"ZN": 1, "HEM": HEM_ATOMS, "GOL": GOL_ATOMS}
    assert block["dropped_waters"] == 2


def test_docking_notes_report_untemplatable_kept_residues(receptor_pdb):
    """Agent 要求保留却留不下的残基，必须在 notes 里点名报出（含补救办法）。"""
    from docking_agent.core.docking import dock_library

    out = dock_library([{"name": "T1", "smiles": "CCO"}], receptor=receptor_pdb,
                       exhaustiveness=1, n_poses=1, keep_hetatm=["HEM", "ZN"])
    assert out["status"] == "ok"
    joined = " ".join(out["notes"])
    assert "HEM" in joined and "化学模板" in joined, joined
    assert "ZN" in joined, joined
    block = out["receptors"][0]
    assert block["kept_hetatm"] == {"ZN": 1}
    assert "HEM" in (block.get("unsupported_hetatm") or [])


def test_docking_dropped_hetatm_reaches_tool_payload(tmp_path, monkeypatch):
    """工具层必须把受体杂原子统计带进返回结构（含大库摘要视图）。"""
    from docking_agent.tools.docking import molecular_docking

    p = tmp_path / "rec.pdb"
    p.write_text(_RECEPTOR_WITH_HETATM, encoding="utf-8")
    raw = molecular_docking.invoke({"molecules_json": json.dumps([{"name": "T1", "smiles": "CCO"}]),
                                    "receptor_file": str(p), "exhaustiveness": 1})
    payload = json.loads(raw)
    blocks = payload.get("receptors") or (payload.get("summary") or {}).get("receptors") or []
    assert blocks, payload
    assert blocks[0]["dropped_hetatm"] == {"ZN": 1, "HEM": HEM_ATOMS, "GOL": GOL_ATOMS}


# --------------------------------------------------------------------------- #
# 3. 共晶配体识别：取最大团，不被离子/添加剂带偏
# --------------------------------------------------------------------------- #
def test_cocrystal_ligand_picks_largest_group_over_ions():
    """位点中心不能被 Zn/硫酸根/甘油带偏：应取最大的一团（此处 HEM 4 原子）。"""
    import tempfile

    from docking_agent.core.pockets import cocrystal_ligand

    with tempfile.TemporaryDirectory(dir=str(PROJECT_ROOT / "var" / "tmp")) as td:
        p = Path(td) / "cocrystal.pdb"
        p.write_text(_RECEPTOR_WITH_HETATM, encoding="utf-8")
        lig = cocrystal_ligand(str(p))
    assert lig is not None
    assert lig["resname"] == "HEM"
    assert lig["n_atoms"] == HEM_ATOMS, lig   # ZN 单原子、水 1 原子、GOL 添加剂都不算
    # 质心应落在 HEM 原子附近（~15.5），而不是被 Zn(5,5,5) 或 GOL(30,30,30) 拉走
    assert abs(lig["center"][0] - 15.5) < 1.0, lig["center"]


def test_prepared_receptor_site_names_the_cocrystal_ligand(receptor_pdb):
    """位点来源要写清是哪个共晶配体，否则用户无法核对盒子依据。"""
    from docking_agent.core.receptors import prepare_user_receptor

    spec = prepare_user_receptor(receptor_pdb)
    source = (spec.get("site") or {}).get("source") or ""
    assert "HEM" in source, source
    assert spec.get("cocrystal_ligand", {}).get("resname") == "HEM"


# --------------------------------------------------------------------------- #
# 4. 化学溯源必须一路走到用户能看到的地方（上传响应 → sidecar → 对接 notes）
# --------------------------------------------------------------------------- #
def test_upload_response_reports_chemistry(receptor_pdb) -> None:
    """化学溯源必须走到用户能看到的地方，不能等用户看到分数才发现缺了金属/辅因子。

    v0.22 起上传**只保存文件**（用户要求"不要一上传就处理文件"），因此：
    上传响应只有 `path`（无解析结果）；只有用户主动「校验文件」（`/api/uploads/inspect`）
    或真正开始运行时，才做现场准备并把 `dropped_hetatm` / 共晶配体 / 提示回传。
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from docking_agent.api.app import app

    with TestClient(app) as client:
        with open(receptor_pdb, "rb") as fh:
            resp = client.post("/api/uploads",
                               files={"file": ("chem_rec.pdb", fh, "chemical/x-pdb")},
                               data={"kind": "receptor"})
        assert resp.status_code == 200, resp.text
        uploaded = resp.json()
        # 上传阶段：只保存，不解析、不准备
        assert uploaded["pending"] is True
        assert "receptor_file" not in uploaded and "dropped_hetatm" not in uploaded
        resp = client.post("/api/uploads/inspect",
                           json={"path": uploaded["path"], "kind": "receptor"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["pending"] is False
    assert payload["dropped_hetatm"] == {"ZN": 1, "HEM": HEM_ATOMS, "GOL": GOL_ATOMS}
    assert payload["dropped_waters"] == 2
    assert payload["cocrystal_ligand"]["resname"] == "HEM"
    warning = payload.get("chemistry_warning") or ""
    assert "HEM" in warning and "keep_hetatm" in warning, warning


def test_prepared_pdbqt_keeps_chemistry_provenance(receptor_pdb):
    """下游往往只拿到 .pdbqt：sidecar 必须把化学溯源带过去，否则信息就丢了。"""
    from docking_agent.core.receptors import read_receptor_file

    spec = read_receptor_file(receptor_pdb)
    again = read_receptor_file(spec["pdbqt"])       # 只给 PDBQT 路径
    assert again["dropped_hetatm"] == spec["dropped_hetatm"] == {"ZN": 1, "HEM": HEM_ATOMS,
                                                                "GOL": GOL_ATOMS}
    assert again["dropped_waters"] == 2
    assert again["cocrystal_ligand"].get("resname") == "HEM"
    assert (again.get("site") or {}).get("source", "").find("HEM") >= 0


def test_docking_via_prepared_pdbqt_reports_chemistry_in_notes(receptor_pdb):
    """用「已准备的 PDBQT」对接时，丢弃金属/辅因子的事实仍要出现在 notes 里。"""
    from docking_agent.core.docking import dock_library
    from docking_agent.core.receptors import read_receptor_file

    prepared = read_receptor_file(receptor_pdb)["pdbqt"]
    out = dock_library([{"name": "T1", "smiles": "CCO"}], receptor=prepared,
                       exhaustiveness=1, n_poses=1)
    assert out["status"] == "ok"
    joined = " ".join(out["notes"])
    assert "丢弃了非水杂原子" in joined and "HEM" in joined, joined


def test_keep_hetatm_works_on_prepared_pdbqt_receptor(receptor_pdb):
    """用户上传的常是「已准备好的 PDBQT」，其中的杂原子在上传时就被剔除了。

    此时若 Agent 要求保留，实现必须**回到 sidecar 记录的原始 PDB 重新准备**，
    否则「金属/辅因子被剔除」只能干瞪眼（真实 agent 运行里就遇到过这一情形）。
    """
    from docking_agent.core.receptors import prepare_user_receptor, read_receptor_file

    prepared = prepare_user_receptor(receptor_pdb)
    plain = read_receptor_file(prepared["pdbqt"])          # 只要 PDBQT：沿用现成的
    assert plain["kept_hetatm"] == {}
    assert plain["pdbqt"] == prepared["pdbqt"], "未要求保留时不应重新准备"

    kept = read_receptor_file(prepared["pdbqt"], keep_hetatm=("ZN",))
    assert kept["kept_hetatm"] == {"ZN": 1}, kept
    assert kept["dropped_hetatm"].get("HEM") == HEM_ATOMS


def test_docking_can_keep_metal_via_prepared_pdbqt(receptor_pdb):
    """对接工具层面同样要能生效：传已准备的 PDBQT + keep_hetatm=ZN 后金属真的进了受体。"""
    from docking_agent.core.docking import dock_library
    from docking_agent.core.receptors import prepare_user_receptor

    prepared = prepare_user_receptor(receptor_pdb)
    out = dock_library([{"name": "T1", "smiles": "CCO"}], receptor=prepared["pdbqt"],
                       exhaustiveness=1, n_poses=1, keep_hetatm=["ZN"])
    block = out["receptors"][0]
    assert block["kept_hetatm"] == {"ZN": 1}, block
    assert "HEM" in (block.get("unsupported_hetatm") or []) or "HEM" in block["dropped_hetatm"]


# --------------------------------------------------------------------------- #
# 4. 特殊配位结构（金属配合物）：先尽力生成 3D，再如实报失败原因
# --------------------------------------------------------------------------- #
_MANCOZEB = "S=C([S-])NCCN/C1[S-]->[Mn+2]/[SH]=1"   # run 20260917-114424-6442 的真实失败分子


def test_metal_coordination_complex_gets_random_coords_retry() -> None:
    """ETKDG 距离几何对金属配合物无解 → 必须再试随机坐标，而不是直接判失败。"""
    from docking_agent.core import ligands

    pdbqt = ligands.smiles_to_pdbqt(_MANCOZEB)
    assert "Mn" in pdbqt, "代森锰（含 Mn 配位）应当能生成 PDBQT"
    assert len(pdbqt) > 200


def test_embed_failure_hint_names_the_metal_and_stays_actionable() -> None:
    """真嵌入不了时必须点名金属并给出可操作建议（不再出现空原因/无下一步）。"""
    from rdkit import Chem

    from docking_agent.core import ligands

    mol = Chem.MolFromSmiles("[Fe+3].[Cl-]")
    assert mol is not None
    hint = ligands._embed_failure_hint(mol)
    assert "Fe" in hint
    assert "SDF/MOL2" in hint
    assert "不纳入排序" in hint


def test_plain_ligand_failure_hint_has_no_metal_claim() -> None:
    """对照：不含金属的结构不得被贴上金属标签。"""
    from rdkit import Chem

    from docking_agent.core import ligands

    mol = Chem.MolFromSmiles("CCO")
    assert mol is not None
    hint = ligands._embed_failure_hint(mol)
    assert "金属" not in hint and "SDF/MOL2" in hint
