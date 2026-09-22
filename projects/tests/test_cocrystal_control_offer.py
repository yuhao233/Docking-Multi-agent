"""「是否把**受体自带**的共晶配体当阳性对照」的询问链路回归。

## 真实缺陷（用户实测反馈）

功能本来是规划好的：对接前若受体结构自带共晶配体、而用户没给阳性对照，就**问一句**
「要不要把它当阳性对照做结合模式对比」。但在真实运行里它**从来不触发**：

    检测到共晶配体 5CM，但在 （无可读结构） 中都解不出 SMILES，因此未询问是否用作阳性对照

三个原因叠在一起（本文件逐条钉住）：

1. 受体被口袋 Agent 先准备成 `<base>_<hash>_ph7.4.pdbqt`，`_pdbqt_spec` 只从 sidecar 读了
   残基名，**没把原始 PDB 路径带出来** —— 而去配体的 PDBQT 里根本没有配体原子；
2. 旧 sidecar 里只有 `"cocrystal_ligand": "5CM"` 这个字符串，没有 `chain:resid:resname` 的
   `key`，而 `cocrystal_ligand_smiles()` 按 key 取原子 → 即便给了 PDB 也解不出来；
3. 从受体标签里找 4 位 PDB 号的 `\\b([0-9][A-Za-z0-9]{3})\\b` 在下划线后缀
   （`7YHP_f7f8da9b…_ph7.4`）上**匹配不到**（`_` 是词字符，没有词边界）。

于是候选结构列表为空 → 解不出 SMILES → 「不拿不确定结构当对照」的保守分支生效 → 用户永远
不会被问。本文件同时覆盖「旧 sidecar 兼容」与「新 sidecar（完整配体信息）」两条路径。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from docking_agent.core import pockets as pockets_core
from docking_agent.core.receptors import _pdbqt_spec
from docking_agent.runtime.blackboard import Blackboard, current_blackboard
from docking_agent.runs import Run, current_run
from docking_agent.tools import choices


# --------------------------------------------------------------------------- #
# 最小夹具：原始 PDB（含 HETATM 配体）+ 由它准备的 PDBQT + sidecar
# --------------------------------------------------------------------------- #
_PDB = """\
ATOM      1  N   ALA A   1      10.000  10.000  10.000  1.00  0.00           N
ATOM      2  CA  ALA A   1      11.400  10.000  10.000  1.00  0.00           C
HETATM   10  C1  LIG A 101      20.000  20.000  20.000  1.00  0.00           C
HETATM   11  C2  LIG A 101      21.400  20.000  20.000  1.00  0.00           C
HETATM   12  C3  LIG A 101      22.100  21.200  20.000  1.00  0.00           C
HETATM   13  C4  LIG A 101      21.400  22.400  20.000  1.00  0.00           C
HETATM   14  C5  LIG A 101      20.000  22.400  20.000  1.00  0.00           C
HETATM   15  C6  LIG A 101      19.300  21.200  20.000  1.00  0.00           C
CONECT   10   11   15
CONECT   11   12
CONECT   12   13
CONECT   13   14
CONECT   14   15
END
"""

_PDBQT = """\
ATOM      1  N   ALA A   1      10.000  10.000  10.000  1.00  0.00    -0.300 N
ATOM      2  CA  ALA A   1      11.400  10.000  10.000  1.00  0.00     0.100 C
"""


def _make_pdbqt_with_sidecar(tmp_path: Path, *, ligand_value: Any) -> Tuple[Path, Path]:
    """造 `<base>_<hash>_ph7.4.pdbqt` + 基础名 sidecar（内容由 ligand_value 决定新旧格式）。"""
    pdb = tmp_path / "7YHP.pdb"
    pdb.write_text(_PDB, encoding="utf-8")
    pdbqt = tmp_path / "7YHP_deadbeefdeadbeef_ph7.4.pdbqt"
    pdbqt.write_text(_PDBQT, encoding="utf-8")
    sidecar = tmp_path / "7YHP_deadbeefdeadbeef.site.json"
    sidecar.write_text(json.dumps({
        "center": [20.0, 21.2, 20.0], "size": [22.0, 22.0, 22.0],
        "source": "共晶配体(LIG)质心", "origin": "7YHP.pdb",
        "origin_path": str(pdb),
        "cocrystal_ligand": ligand_value,
    }, ensure_ascii=False), encoding="utf-8")
    return pdbqt, pdb


def _offer(spec: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,
           tmp_path: Path) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """用 spec 造一个「对接前预览块」，走真实的询问链路；把 SMILES 解析替换成可观测桩。"""
    calls: List[Tuple[str, str]] = []

    def _fake_smiles(path: str, ligand: Dict[str, Any]) -> str:
        calls.append((path, str((ligand or {}).get("key") or "")))
        return "C1=CC=CC=C1" if path.endswith("7YHP.pdb") else ""

    monkeypatch.setattr(pockets_core, "cocrystal_ligand_smiles", _fake_smiles)
    run = Run(tmp_path, "R-COC", "agent", {"mode": "chat"})
    token = current_run.set(run)
    board_token = current_blackboard.set(Blackboard("R-COC"))
    block = {"receptor": spec.get("key"), "receptor_key": spec.get("key"),
             "source_pdb": spec.get("source_pdb") or "",
             "receptor_pdb": spec.get("pdb") or "",
             "cocrystal_ligand": spec.get("cocrystal_ligand") or {}}
    try:
        offered = choices.offer_cocrystal_positive_control([block])
    finally:
        current_blackboard.reset(board_token)
        current_run.reset(token)
    return offered, calls


# --------------------------------------------------------------------------- #
# 1) 旧 sidecar（只存残基名）→ 必须回到原始结构把 key 补回来，并问出选项
# --------------------------------------------------------------------------- #
def test_legacy_sidecar_still_offers_cocrystal_ligand(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    pdbqt, pdb = _make_pdbqt_with_sidecar(tmp_path, ligand_value="LIG")
    spec = _pdbqt_spec(str(pdbqt))
    assert spec["source_pdb"] == str(pdb), "必须把原始结构路径带出来（去配体的 PDBQT 里没有配体原子）"
    ligand = spec["cocrystal_ligand"]
    assert ligand.get("key"), f"必须补回 chain:resid:resname 的 key：{ligand}"
    assert ligand.get("n_atoms") == 6

    offered, calls = _offer(spec, monkeypatch, tmp_path)
    assert offered, "受体自带共晶配体时必须询问用户是否用作阳性对照"
    assert offered[0]["value"] == "C1=CC=CC=C1" and offered[0]["kind"] == "positive_control"
    assert offered[0]["id"].startswith("positive_control:")
    assert calls and calls[0][0] == str(pdb) and calls[0][1] == ligand["key"], calls
    assert any(c["id"] == "positive_control:none" for c in offered), "必须同时给「不使用」选项"


# --------------------------------------------------------------------------- #
# 2) 新 sidecar（存完整配体 dict）→ 直接用，不必再解析原始结构
# --------------------------------------------------------------------------- #
def test_full_ligand_sidecar_is_used_directly(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    full = {"resname": "LIG", "key": "A:101:LIG", "n_atoms": 6, "center": [20.0, 21.2, 20.0]}
    pdbqt, pdb = _make_pdbqt_with_sidecar(tmp_path, ligand_value=full)
    spec = _pdbqt_spec(str(pdbqt))
    assert spec["cocrystal_ligand"]["key"] == "A:101:LIG"
    assert spec["cocrystal_ligand"]["n_atoms"] == 6
    assert spec["source_pdb"] == str(pdb)
    offered, _calls = _offer(spec, monkeypatch, tmp_path)
    assert offered and offered[0]["detail"]["resname"] == "LIG"


# --------------------------------------------------------------------------- #
# 3) 没有原始结构 / 没带配体信息 → 不询问（保守：绝不给不确定的对照）
# --------------------------------------------------------------------------- #
def test_no_origin_structure_means_no_offer(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    pdbqt = tmp_path / "plain_deadbeefdeadbeef.pdbqt"
    pdbqt.write_text(_PDBQT, encoding="utf-8")
    (tmp_path / "plain_deadbeefdeadbeef.site.json").write_text(json.dumps({
        "origin_path": str(tmp_path / "missing.pdb"), "cocrystal_ligand": "LIG",
    }, ensure_ascii=False), encoding="utf-8")
    spec = _pdbqt_spec(str(pdbqt))
    offered, calls = _offer(spec, monkeypatch, tmp_path)
    assert offered == [], "解不出结构时不能询问（保守分支不能被这次修复放松）"
    # 但**试过哪些路径必须写进日志**：这次缺陷的第一手线索就是
    # 「检测到共晶配体 5CM，但在 （无可读结构） 中都解不出 SMILES」
    assert calls == [(str(tmp_path / "missing.pdb"), "")], calls


# --------------------------------------------------------------------------- #
# 4) PDB 号提取必须容忍 `7YHP_<hash>_ph7.4` 这种后缀（旧正则在 `_` 上匹配不到）
# --------------------------------------------------------------------------- #
def test_pdb_id_extraction_tolerates_hash_suffix(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "assets" / "cache"
    cache.mkdir(parents=True)
    (cache / "7YHP.pdb").write_text(_PDB, encoding="utf-8")
    monkeypatch.setattr(choices, "project_root", lambda: tmp_path)
    paths = choices._ligand_candidate_paths(
        {"receptor": "7YHP_f7f8da9b58da39a3_ph7.4", "cocrystal_ligand": {"resname": "5CM"}}, None)
    assert str(cache / "7YHP.pdb") in paths, paths
