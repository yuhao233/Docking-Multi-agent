"""同一物质的两种 SMILES 写法必须只算一个分子。

真实反馈（2026-09-22，运行 `20260922-203256-6431`）：
「就一个分子，怎么对接出来两个？」

复盘：用户点选的代表结构与在线查询回来的写法是**同一物质的不同 SMILES 拼写**
（`C(CNC(=S)[S-])NC(=S)[S-]…` 与 `S=C([S-])NCCNC(=S)[S-]…`，canonical SMILES 与
InChIKey `CHNQZRKUZPNOOH-UHFFFAOYSA-J` 完全相同）。而黑板里 `add_molecules` 用**规范**
SMILES 做键、`set_properties`/`set_docking`/`set_binding` 用**原始** SMILES 做键 ——
同一物质于是各占一个键，黑板里出现两个「分子」，对接与排行也就出现两行（分数还一模一样）。
"""
from __future__ import annotations

from typing import Any

THIOLATE = "C(CNC(=S)[S-])NC(=S)[S-].C(CNC(=S)[S-])NC(=S)[S-].[Mn+2].[Zn+2]"
THIONE = "S=C([S-])NCCNC(=S)[S-].S=C([S-])NCCNC(=S)[S-].[Mn+2].[Zn+2]"


def test_the_two_spellings_are_chemically_identical() -> None:
    """前提校验：两条 SMILES 的规范形式一致（否则下面的去重断言没有意义）。"""
    from docking_agent.runtime.blackboard import canonical_key

    assert canonical_key(THIOLATE) == canonical_key(THIONE)


def test_canonical_key_falls_back_to_the_raw_text() -> None:
    """无法解析的写法退回原文（可追溯、不丢数据），不抛异常。"""
    from docking_agent.runtime.blackboard import canonical_key

    assert canonical_key("不是SMILES(") == "不是SMILES("
    assert canonical_key("") == ""


def test_properties_do_not_add_a_second_molecule() -> None:
    """属性行用另一种写法登记时，分子库必须仍然只有 1 个分子。"""
    from docking_agent.runtime.blackboard import Blackboard

    board = Blackboard("R-DUP")
    board.add_molecules([{"name": "Mancozeb", "smiles": THIONE}])
    board.set_properties([{"name": "Mancozeb", "smiles": THIOLATE, "molecular_weight": 545.11,
                          "protonation": {"policy": "ph"}}])

    assert len(board.molecules()) == 1, board.molecules()
    assert len(board.properties()) == 1, board.properties()
    # 属性查询两种写法都要命中（键是规范形式，调用方可能拿原始写法来问）
    assert board.get_property(THIOLATE) is not None
    assert board.get_property(THIONE) is not None


def test_docking_and_binding_dedupe_by_identity() -> None:
    """对接/结合模式结果按身份落键：两种写法只留一行（分数一致时不该出现两行）。"""
    from docking_agent.runtime.blackboard import Blackboard

    board = Blackboard("R-DUP2")
    row_a = {"name": "Mancozeb", "smiles": THIONE, "affinity_kcal_mol": -4.13}
    row_b = {"name": "Mancozeb", "smiles": THIOLATE, "affinity_kcal_mol": -4.13}
    board.set_docking([row_a, row_b])
    board.set_binding([row_a, row_b])

    assert len(board.docking()) == 1, board.docking()
    assert len(board.binding()) == 1, board.binding()


def test_dedupe_molecules_helper_reports_what_it_dropped() -> None:
    """工具层兜底：清单里出现同一物质的两种写法时，去重并如实报数。"""
    from docking_agent.runtime.blackboard import dedupe_molecules

    mols: list[dict[str, Any]] = [
        {"name": "M", "smiles": THIONE},
        {"name": "M", "smiles": THIOLATE},
        {"name": "Other", "smiles": "CCO"},
    ]
    kept, dropped = dedupe_molecules(mols)
    assert dropped == 1 and len(kept) == 2, (kept, dropped)
    assert kept[0]["smiles"] == THIONE and kept[1]["smiles"] == "CCO"
    # 空清单与不可解析项不炸
    assert dedupe_molecules([]) == ([], 0)
    assert dedupe_molecules([{"name": "x", "smiles": ""}])[0] == []
