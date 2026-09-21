"""输入解析能力回归：InChIKey 在线反查 + Excel(.xlsx) 真读。

用户要求「保证分子解析」——本文件看护两条容易被"跳过整行"掩盖的能力：

1. **InChIKey**（单向哈希）：
   - 内置表命中 → 离线可用；
   - 未收录 → 在线反查 PubChem（**必须回算 InChIKey 校验一致**才采用）→ 写本地缓存；
   - `INCHIKEY_ONLINE=off` 或网络失败 → 如实跳过并给出可操作原因，**绝不编造结构**；
   - 结构来源（builtin/cache/pubchem）必须写进 `input_normalization.json` 的 notes。
2. **xlsx**：扫描**所有工作表**并合并，单元格（整数/日期/布尔）规格化，
   坏行带工作表名与行号上报；未装 openpyxl 时给出可操作提示而不是静默空结果。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

# 真实存在于 PubChem、但**不在内置 22 个常见化合物表**里的 InChIKey（Ravuconazole）
RAVU_KEY = "OPAHEYNNJWPQPX-RCDICMHDSA-N"
RAVU_SMILES = "C[C@@H](c1nc(-c2ccc(C#N)cc2)cs1)[C@](O)(Cn1cncn1)c1ccc(F)cc1F"


# --------------------------------------------------------------------------- #
# InChIKey 在线反查
# --------------------------------------------------------------------------- #
def test_builtin_table_hits_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from docking_agent.core import inchikey as IK

    monkeypatch.setenv("INCHIKEY_ONLINE", "off")
    monkeypatch.setattr(IK, "cache_dir", lambda: tmp_path)
    hit = IK.resolve_inchikey("BSYNRYMUTXBXSQ-UHFFFAOYSA-N")   # aspirin
    assert hit["source"] == "builtin" and hit["smiles"]


def test_unknown_key_online_lookup_writes_cache(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    """未收录的键：联网拿到结构 → 回算校验 → 落盘缓存；第二次必须不再联网。"""
    from docking_agent.core import inchikey as IK

    monkeypatch.setattr(IK, "cache_dir", lambda: tmp_path)
    monkeypatch.setenv("INCHIKEY_ONLINE", "on")
    calls = {"n": 0}

    def fake_http(url: str, timeout: int) -> dict:
        calls["n"] += 1
        assert "inchikey" in url and RAVU_KEY in url, url
        return {"PropertyTable": {"Properties": [
            {"CID": 467825, "SMILES": "C[C@@H](C1=NC(=CS1)C2=CC=C(C=C2)C#N)"
                                       "[C@](CN3C=NC=N3)(C4=C(C=C(C=C4)F)F)O"}]}}

    monkeypatch.setattr(IK, "_http_json", fake_http)
    first = IK.resolve_inchikey(RAVU_KEY)
    assert first["source"] == "pubchem" and first["cid"] == 467825, first
    assert calls["n"] == 1
    assert IK.cache_file(RAVU_KEY).is_file(), "在线结果必须落盘缓存"

    second = IK.resolve_inchikey(RAVU_KEY)
    assert second["source"] == "cache" and calls["n"] == 1, "第二次不得再联网"


def test_pubchem_result_is_rejected_when_key_does_not_round_trip(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """回算校验：PubChem 给错结构（或数据不一致）必须拒绝采用，不得悄悄用错分子。"""
    from docking_agent.core import inchikey as IK

    monkeypatch.setattr(IK, "cache_dir", lambda: tmp_path)
    monkeypatch.setenv("INCHIKEY_ONLINE", "on")
    monkeypatch.setattr(IK, "_http_json",
                        lambda url, timeout: {"PropertyTable": {"Properties": [
                            {"CID": 1, "SMILES": "CCO"}]}})
    hit = IK.resolve_inchikey(RAVU_KEY)
    assert hit["smiles"] == "" and "回算" in hit["error"], hit
    assert not IK.cache_file(RAVU_KEY).is_file(), "校验不通过不得写缓存"


def test_offline_mode_skips_with_actionable_reason(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    from docking_agent.core import inchikey as IK

    monkeypatch.setattr(IK, "cache_dir", lambda: tmp_path)
    monkeypatch.setenv("INCHIKEY_ONLINE", "off")
    called = {"n": 0}
    monkeypatch.setattr(IK, "_http_json", lambda url, timeout: called.__setitem__("n", 1) or {})
    hit = IK.resolve_inchikey(RAVU_KEY)
    assert hit["smiles"] == "" and "离线模式" in hit["error"], hit
    assert called["n"] == 0, "关掉后绝不允许发起网络请求"


def test_network_failure_does_not_break_whole_library(monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    """网络异常只能让该行跳过，不能把整库解析带崩。"""
    from docking_agent.core import inchikey as IK
    from docking_agent.core.normalize import normalize_ligand_text

    monkeypatch.setattr(IK, "cache_dir", lambda: tmp_path)
    monkeypatch.setenv("INCHIKEY_ONLINE", "on")

    def boom(url: str, timeout: int) -> dict:
        raise OSError("网络不可达")

    monkeypatch.setattr(IK, "_http_json", boom)
    mols, norm = normalize_ligand_text(
        f"name,inchikey\n乙醇,CCO\n{RAVU_KEY[:0]}Ravuconazole,{RAVU_KEY}\n尿素,NC(N)=O\n",)
    # 乙醇/尿素那一行没有结构列（name,inchikey 表里没 SMILES）→ 只有 RAVU 行进不了
    assert norm["records_total"] == 3
    assert norm["records_skipped"] >= 1
    assert any("PubChem 查询失败" in str(s.get("reason")) for s in norm["skipped"]), norm["skipped"]


def test_table_with_inchikey_column_is_resolved(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    """只有 InChIKey 列的表也必须能解析出分子，并在 notes 里点名来源。"""
    from docking_agent.core import inchikey as IK
    from docking_agent.core.normalize import normalize_ligand_text

    monkeypatch.setattr(IK, "cache_dir", lambda: tmp_path)
    monkeypatch.setenv("INCHIKEY_ONLINE", "on")
    monkeypatch.setattr(IK, "_http_json",
                        lambda url, timeout: {"PropertyTable": {"Properties": [
                            {"CID": 467825, "SMILES": "C[C@@H](C1=NC(=CS1)C2=CC=C(C=C2)C#N)"
                                                       "[C@](CN3C=NC=N3)(C4=C(C=C(C=C4)F)F)O"}]}})
    mols, norm = normalize_ligand_text(f"名称,InChIKey\nRavuconazole,{RAVU_KEY}\n")
    assert [m["id"] for m in mols] == ["Ravuconazole"], mols
    assert "InChIKey" in "".join(norm["notes"])
    assert any("PubChem" in n for n in norm["notes"]), norm["notes"]


# --------------------------------------------------------------------------- #
# xlsx
# --------------------------------------------------------------------------- #
def _write_xlsx(path: Path) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    info = wb.active
    info.title = "说明"
    info.append(["本表为示例分子库"])
    s1 = wb.create_sheet("数据")
    s1.append(["编号", "名称", "结构"])
    s1.append([1, "阿司匹林", "CC(=O)Oc1ccccc1C(=O)O"])
    s1.append([2, "咖啡因", "Cn1c(=O)c2c(ncn2C)n(C)c1=O"])
    s1.append([None, None, None])
    s1.append([3, "坏分子", "这不是SMILES("])
    s2 = wb.create_sheet("补充")
    s2.append(["名称", "SMILES"])
    s2.append(["乙醇", "CCO"])
    s2.append(["尿素", "NC(N)=O"])
    wb.create_sheet("空表")
    wb.save(path)
    return path


def test_xlsx_reads_all_sheets_and_reports_per_sheet(tmp_path: Path) -> None:
    """真实 .xlsx：说明页在前的常见排版也能解析；合并多张数据页并逐表上报。"""
    from docking_agent.core.normalize import normalize_ligand_file

    path = _write_xlsx(tmp_path / "lib.xlsx")
    mols, norm = normalize_ligand_file(str(path))
    assert [m["id"] for m in mols] == ["1", "2", "乙醇", "尿素"], mols
    assert norm["format"] == "xlsx"
    assert any("xlsx 工作表解析" in n and "数据: 2 个" in n and "补充: 2 个" in n
               for n in norm["notes"]), norm["notes"]
    assert norm["records_skipped"] == 1
    assert "工作表「数据」" in str(norm["skipped"][0]["reason"]), norm["skipped"]


def test_xlsx_cell_normalization(tmp_path: Path) -> None:
    """整数不能变成 `2244.0`，日期也不能带 `00:00:00`（都会污染 ID/名称列）。"""
    openpyxl = pytest.importorskip("openpyxl")
    from docking_agent.core.normalize_io import _xlsx_cell_text

    assert _xlsx_cell_text(2244.0) == "2244"
    assert _xlsx_cell_text(3.5) == "3.5"
    assert _xlsx_cell_text(datetime(2026, 9, 17)) == "2026-09-17"
    assert _xlsx_cell_text(datetime(2026, 9, 17, 13, 5)) == "2026-09-17 13:05:00"
    assert _xlsx_cell_text(True) == "TRUE" and _xlsx_cell_text(None) == ""
    assert openpyxl is not None


def test_xlsx_without_openpyxl_gives_actionable_message(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """依赖被移除的环境里也必须给出「装什么/怎么办」，而不是静默解析成 0 个。"""
    import builtins

    from docking_agent.core.normalize import normalize_ligand_file

    path = _write_xlsx(tmp_path / "lib.xlsx")
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "openpyxl":
            raise ImportError("模拟未安装")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    mols, norm = normalize_ligand_file(str(path))
    assert mols == []
    assert any("openpyxl" in n and "安装" in n for n in norm["notes"]), norm["notes"]
