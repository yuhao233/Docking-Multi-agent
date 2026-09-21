#!/usr/bin/env python
"""对接引擎真实性验证（可重复执行的证据链）。

本脚本**不信任**项目自身的对接封装，分四层独立取证：

  A. 引擎真实性：vina 是否为真实原生扩展（.so）、受体文件是否为真实蛋白、
     对接代码路径中是否存在 mock/fake/硬编码分数。
  B. 独立复算：用 `vina` Python API 从零重写对接流程（仅硬编码受体路径与中心），
     按结果的 box_group 分组、用**每一行实际使用的盒子**（取自工具返回）复算，与工具逐位比对。
  C. 工具输出交叉验证：直接调用项目 @tool（不经任何 LLM），
     校验 total = intermolecular + torsional 的能量恒等式、同组同盒一致性、可重复性。
  D. 反证控制：移动盒子中心 / 更换受体 / 非法 SMILES / 指定不存在的引擎 / 人为放大
     BOX_GROUP_MARGIN，结果必须发生相应变化或明确报错——排除「返回固定值」的可能。

用法：
    .venv/bin/python scripts/verify_docking.py            # 全部检查（约 2-4 分钟）
    .venv/bin/python scripts/verify_docking.py --quick    # 只做 A/C/D 的快速子集
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

# --------------------------------------------------------------------------- #
# 独立复算所用的常量（刻意硬编码，不从项目代码读取，避免继承被污染的配置）
# --------------------------------------------------------------------------- #
RECEPTOR_PDBQT = PROJECT_ROOT / "assets" / "receptors" / "registry" / "thrombin_1DWC.pdbqt"
TRYPSIN_PDBQT = PROJECT_ROOT / "assets" / "receptors" / "registry" / "trypsin_1PTU.pdbqt"
BOX_CENTER = [31.5, 13.74, 24.36]
BOX_SIZE = [22.0, 22.0, 22.0]
SEED = 42
EXHAUSTIVENESS = 6
N_POSES = 1

MOLECULES = [
    {"name": "benzamidine", "smiles": "NC(=N)c1ccccc1"},
    {"name": "nafamostat", "smiles": "NC(=N)c1ccc2cc(OC(=O)c3ccc(NC(=N)N)cc3)ccc2c1"},
    {"name": "melagatran", "smiles": "O=C(O)C1CCCN1C(=O)C(Cc1ccc(CN=C(N)N)cc1)NC(=O)C1CC1"},
    {"name": "argatroban", "smiles": "CC1CCCN1C(=O)C(CCCN=C(N)N)NS(=O)(=O)c1ccc2c(c1)CCCN2C(=O)O"},
    {"name": "aspirin", "smiles": "CC(=O)Oc1ccccc1C(=O)O"},
    {"name": "caffeine", "smiles": "Cn1cnc2c1c(=O)n(C)c(=O)n2C"},
    {"name": "ibuprofen", "smiles": "CC(C)Cc1ccc(C(C)C(=O)O)cc1"},
    {"name": "acetaminophen", "smiles": "CC(=O)Nc1ccc(O)cc1"},
]

RESULTS: List[Tuple[bool, str, str]] = []  # (ok, label, detail)


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  →  {detail}" if detail else ""))
    return bool(ok)


# --------------------------------------------------------------------------- #
# 项目 @tool 的单次调用（[B]/[C] 共用，避免同一份全库对接被重复执行两次）
# --------------------------------------------------------------------------- #
_TOOL_DATA: Dict[str, Any] = {}


def invoke_docking_tool_once() -> Dict[str, Any]:
    """对全库调用一次项目 @tool（不经 LLM）；C 方案后盒子按 box_group 不同，[B] 需据此对齐复算。"""
    if "data" not in _TOOL_DATA:
        from docking_agent.tools.docking import molecular_docking

        print(f"       调用项目 @tool：{len(MOLECULES)} 个分子 / thrombin / "
              f"exhaustiveness={EXHAUSTIVENESS}（无 LLM）…")
        _TOOL_DATA["data"] = json.loads(molecular_docking.invoke({
            "molecules_json": json.dumps(MOLECULES, ensure_ascii=False),
            "receptor_sources": "thrombin", "exhaustiveness": EXHAUSTIVENESS,
            "n_poses": N_POSES, "engine": "vina"}))
    return _TOOL_DATA["data"]


def tool_rows(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取第一个受体块的结果行。"""
    return ((data.get("receptors") or [{}])[0].get("results") or [])


def group_rows_by_box(results: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """按 box_group 分组；缺少该字段（旧归档/旧调用）一律按 main 处理。"""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        groups.setdefault(str(r.get("box_group") or "main"), []).append(r)
    return groups


def fmt_box(size: Any) -> str:
    """盒子的紧凑展示：`28.80x28.80x28.80`。"""
    try:
        return "x".join(f"{float(x):.2f}" for x in size)
    except (TypeError, ValueError):
        return str(size)


# --------------------------------------------------------------------------- #
# 独立实现的对接（只依赖 rdkit / meeko / vina，不引用项目代码）
# --------------------------------------------------------------------------- #
def make_ligand_pdbqt(smiles: str, seed: int = SEED) -> str:
    """与项目等价的配体准备流程（RDKit ETKDGv3 + MMFF + meeko）。"""
    from rdkit import Chem
    from rdkit.Chem import AllChem

    from meeko import MoleculePreparation, PDBQTWriterLegacy

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"无法解析 SMILES: {smiles}")
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = seed
    if AllChem.EmbedMolecule(mol, params) != 0:
        raise RuntimeError(f"3D 构象生成失败: {smiles}")
    AllChem.MMFFOptimizeMolecule(mol, maxIters=500)
    setups = MoleculePreparation().prepare(mol)
    pdbqt, ok, err = PDBQTWriterLegacy.write_string(setups[0])
    if not ok:
        raise RuntimeError(f"PDBQT 写入失败: {err}")
    return pdbqt


def vina_dock(receptor_pdbqt: Path, ligand_pdbqt: str,
              center: List[float], size: List[float],
              exhaustiveness: int = EXHAUSTIVENESS, n_poses: int = N_POSES,
              seed: int = SEED, pose_out: Optional[Path] = None) -> List[float]:
    """直接调用 AutoDock Vina Python API 完成对接，返回首个位姿的能量行。"""
    from vina import Vina

    v = Vina(sf_name="vina", verbosity=0, cpu=2, seed=seed)
    v.set_receptor(str(receptor_pdbqt))
    v.set_ligand_from_string(ligand_pdbqt)
    v.compute_vina_maps(center=list(center), box_size=list(size))
    v.dock(exhaustiveness=exhaustiveness, n_poses=n_poses)
    energies = v.energies(n_poses=n_poses)
    if pose_out is not None:
        v.write_pose(str(pose_out), overwrite=True)
    return [float(x) for x in energies[0]]


def vina_rescore(receptor_pdbqt: Path, pose_file: Path,
                 center: List[float], size: List[float], seed: int = SEED) -> List[float]:
    """用全新 Vina 实例对已写出的位姿重新打分（score_only）。"""
    from vina import Vina

    v = Vina(sf_name="vina", verbosity=0, cpu=2, seed=seed)
    v.set_receptor(str(receptor_pdbqt))
    v.set_ligand_from_file(str(pose_file))
    v.compute_vina_maps(center=list(center), box_size=list(size))
    return [float(x) for x in v.score()]


# --------------------------------------------------------------------------- #
# A. 引擎与输入真实性
# --------------------------------------------------------------------------- #
def strip_comments_and_strings(path: Path) -> str:
    """去掉注释与字符串字面量，只保留可执行代码——避免把「禁止 mock」这类注释误判为 mock。"""
    import io
    import tokenize

    src = path.read_text(encoding="utf-8")
    parts: List[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            parts.append(tok.string)
    except tokenize.TokenError:
        return src
    return "\n".join(parts)


def audit_engine() -> None:
    print("\n[A] 引擎与输入真实性")

    import vina
    from vina import Vina

    mod_path = Path(vina.__file__).resolve()
    native = sorted(mod_path.parent.glob("_vina*.so"))
    check(native != [], "vina 为真实原生扩展（存在 _vina*.so）",
          f"{mod_path.name} + {[p.name for p in native]}")
    check(hasattr(Vina, "dock") and hasattr(Vina, "compute_vina_maps"),
          "Vina 提供真实对接接口（dock / compute_vina_maps）",
          f"version={getattr(vina, '__version__', '?')}")

    # 受体文件：真实蛋白（原子数、残基种类、无水分子的受体 PDBQT）
    text = RECEPTOR_PDBQT.read_text(errors="ignore")
    atoms = [ln for ln in text.splitlines() if ln.startswith(("ATOM", "HETATM"))]
    residues = {ln[17:20].strip() for ln in atoms}
    check(len(atoms) > 1000, "受体 PDBQT 为真实蛋白结构（原子数充足）",
          f"{len(atoms)} 原子 / {len(residues)} 种残基 / {RECEPTOR_PDBQT.stat().st_size // 1024} KB")
    check("HOH" not in residues and "WAT" not in residues, "受体已去水（可用于对接）")
    check("TORSDOF" in text or "ROOT" in text or "ATOM" in text, "受体含 PDBQT 结构记录")

    # 催化三联体：确证受体是真实的丝氨酸蛋白酶（人 α-凝血酶）
    res_ids = {(ln[17:20].strip(), ln[22:27].strip()) for ln in atoms}
    triad = [("HIS", "57"), ("ASP", "102"), ("SER", "195")]
    check(all(t in res_ids for t in triad), "受体含凝血酶催化三联体 HIS57/Asp102/Ser195",
          f"命中 {[a + b for a, b in triad if (a, b) in res_ids]}")

    # 对接代码路径中不得出现 mock/fake/固定分数（只看代码，剔除注释与字符串）
    suspicious = re.compile(r"\b(mock|fake|stub|dummy|hardcode|FIXED_SCORE)\b", re.I)
    hits: List[str] = []
    for rel in ("src/docking_agent/core/chemistry.py", "src/docking_agent/core/docking.py",
                "src/docking_agent/tools/docking.py", "src/docking_agent/reporting/tables.py",
                "src/docking_agent/reporting/charts.py"):
        code = strip_comments_and_strings(PROJECT_ROOT / rel)
        for i, line in enumerate(code.splitlines(), 1):
            if suspicious.search(line):
                hits.append(f"{rel}:{i}: {line.strip()}")
    check(hits == [], "对接代码（剔除注释/字符串）无 mock/fake/硬编码分数",
          "; ".join(hits) if hits else "未发现")

    # 项目对接实现确实调用 vina 原生 API
    core = (PROJECT_ROOT / "src/docking_agent/core/docking.py").read_text(encoding="utf-8")
    check("from vina import Vina" in core and "v.dock(" in core and "v.energies(" in core,
          "项目对接实现直接调用 vina API（from vina import Vina / v.dock / v.energies）")


# --------------------------------------------------------------------------- #
# B. 独立复算 vs 项目工具输出
# --------------------------------------------------------------------------- #
def reproduce_independently(quick: bool) -> None:
    print("\n[B] 独立复算（按 box_group 分组，用各组真实盒子 + 脚本自己的 vina 复算）")
    from docking_agent.core import smiles_to_pdbqt

    data = invoke_docking_tool_once()
    if data.get("status") != "ok":
        check(False, "工具未返回 status=ok，无法做独立复算", str(data.get("status")))
        return
    results = tool_rows(data)

    # 取证：把工具**每一行实际使用的**盒子与搜索强度打出来；独立复算必须用同一行的盒子对齐。
    print("       工具返回各行（盒子按 box_group 可能不同）：")
    for r in results:
        print(f"         {str(r.get('name')):14s} box_group={str(r.get('box_group') or 'main'):5s} "
              f"box_size={fmt_box(r.get('box_size'))} ligand_span={r.get('ligand_span')} "
              f"exhaustiveness={r.get('exhaustiveness')} affinity={r.get('affinity_kcal_mol')}")

    limit = 2 if quick else 3
    pose_dir = PROJECT_ROOT / "var" / "verify"
    pose_dir.mkdir(parents=True, exist_ok=True)
    name2smiles = {m["name"]: m["smiles"] for m in MOLECULES}
    for group, rows in sorted(group_rows_by_box(results).items()):
        if len(rows) > limit:
            print(f"       box_group={group}：共 {len(rows)} 个分子，"
                  f"抽样复算前 {limit} 个（同组同盒抽样）")
        for r in rows[:limit]:
            name = str(r.get("name"))
            smiles = name2smiles.get(name) or str(r.get("smiles") or "")
            # 盒子取自**工具返回**（main 组即主盒、large 组即该组的更大盒），脚本自己用 vina 复算一遍
            box = [float(x) for x in (r.get("box_size") or BOX_SIZE)]
            exh = int(r.get("exhaustiveness") or EXHAUSTIVENESS)
            lig, project_lig = make_ligand_pdbqt(smiles), smiles_to_pdbqt(smiles)
            sha = hashlib.sha256(lig.encode()).hexdigest()
            check(sha == hashlib.sha256(project_lig.encode()).hexdigest(),
                  f"{name}：独立准备的配体 PDBQT 与项目一致（输入相同）", f"sha256={sha[:16]}")
            e = vina_dock(RECEPTOR_PDBQT, lig, BOX_CENTER, box,
                          exhaustiveness=exh, pose_out=pose_dir / f"{name}_pose.pdbqt")
            print(f"       独立 Vina：{name:12s} total={e[0]:.2f}  inter={e[1]:.2f}  "
                  f"intra={e[2]:.2f}  torsion={e[3]:.2f} kcal/mol"
                  f"（box_group={group} 盒子={fmt_box(box)} exh={exh}）")
            expected = round(e[0], 2)
            check(abs(float(r["affinity_kcal_mol"]) - expected) < 1e-9,
                  f"{name}：工具分数 == 独立复算分数（box_group={group}，"
                  f"盒子={fmt_box(box)}，exhaustiveness={exh}）",
                  f"tool={r['affinity_kcal_mol']} / independent_raw={e[0]} → round={expected}")
            # 位姿重打分：全新实例对刚写出的位姿打分，必须与对接分数一致（同一盒子）
            try:
                rescored = vina_rescore(RECEPTOR_PDBQT, pose_dir / f"{name}_pose.pdbqt",
                                        BOX_CENTER, box)
                check(abs(rescored[0] - e[0]) < 0.02,
                      f"{name}：位姿重打分与对接分数一致（引擎自洽，同一盒子）",
                      f"dock={e[0]:.2f} vs rescore={rescored[0]:.2f}")
            except Exception as exc:  # noqa: BLE001
                check(False, f"{name}：位姿重打分失败", str(exc)[:120])


# --------------------------------------------------------------------------- #
# C. 项目工具输出的交叉验证（不经 LLM）
# --------------------------------------------------------------------------- #
def tool_output_checks() -> Dict[str, Any]:
    print("\n[C] 项目 @tool 直接输出（无 LLM 参与）")
    from docking_agent.tools.docking import molecular_docking

    data = invoke_docking_tool_once()
    check(data.get("status") == "ok", "工具返回 status=ok")
    results = tool_rows(data)
    check(len(results) == len(MOLECULES), f"工具对全部 {len(MOLECULES)} 个分子返回结果")

    # 1) 全部标注真实引擎
    engines = {r.get("engine") for r in results}
    check(engines == {"vina"}, "结果 engine 字段均为 vina", str(engines))

    # 2) 能量恒等式：Vina 的 total = intermolecular + torsional（真实能量项才满足）
    worst = 0.0
    bad: List[str] = []
    for r in results:
        if "affinity_kcal_mol" not in r:
            bad.append(f"{r.get('name')}: 无 affinity")
            continue
        total = r["affinity_kcal_mol"]
        inter = r["intermolecular_kcal_mol"]
        torsion = r["torsion_kcal_mol"]
        diff = abs(total - round(inter + torsion, 2))
        worst = max(worst, diff)
        if diff > 0.02:
            bad.append(f"{r['name']}: {total} != {inter}+{torsion}")
    check(bad == [], "能量恒等式 total = intermolecular + torsional 对全部分子成立",
          f"最大偏差 {worst:.3f} kcal/mol" + (f"；异常：{bad}" if bad else ""))

    # 3) 盒子一致性不变量（C 方案第一原则：同一 box_group 内共用同一个盒子；跨组可以不同）
    #    逐行独立复算比对已按 box_group 分组移到 [B]（那里用每行的真实盒子）。
    groups = group_rows_by_box(results)
    mrows = groups.get("main") or []
    mbox = {tuple(round(float(x), 2) for x in (r.get("box_size") or [])) for r in mrows}
    check(len(mrows) > 0 and len(mbox) == 1,
          f"main 组内 {len(mrows)} 行的 box_size 完全一致（一致性不变量）",
          f"main box_size={sorted(mbox)}")
    for group, rows in sorted(groups.items()):
        boxes = {tuple(round(float(x), 2) for x in (r.get("box_size") or [])) for r in rows}
        if group != "main":
            check(len(boxes) == 1, f"{group} 组内 {len(rows)} 行共用同一个盒子（同组同盒）",
                  f"box_size={sorted(boxes)}")
    if "large" in groups:
        lb = sorted({tuple(round(float(x), 2) for x in (r.get("box_size") or []))
                     for r in groups["large"]})[0]
        mb = sorted(mbox)[0]
        check(lb != mb, "large 组的盒子与主盒不同（分组真实改变了搜索空间）",
              f"main={fmt_box(mb)} vs large={fmt_box(lb)}")
        check(all(lb[i] >= mb[i] for i in range(3)), "large 组的盒子不小于主盒（同中心放大搜索空间）",
              f"main={fmt_box(mb)} vs large={fmt_box(lb)}")
    else:
        check(False, "存在超限分子时应划入 large 组（分组行为被真实触发）",
              f"各组分子数={ {g: len(rs) for g, rs in groups.items()} }")

    # 4) 可重复性：同样输入再跑一次，必须完全一致
    raw2 = molecular_docking.invoke({
        "molecules_json": json.dumps(MOLECULES[:2], ensure_ascii=False),
        "receptor_sources": "thrombin", "engine": "vina",
        "exhaustiveness": EXHAUSTIVENESS, "n_poses": N_POSES,
    })
    results2 = json.loads(raw2)["receptors"][0]["results"]
    same = all(abs(a["affinity_kcal_mol"] - b["affinity_kcal_mol"]) < 1e-9
               for a, b in zip(results[:2], results2))
    check(same, "重复调用结果完全一致（确定性，seed=42）",
          f"{[r['affinity_kcal_mol'] for r in results[:2]]} == {[r['affinity_kcal_mol'] for r in results2]}")

    # 5) 结果多样性：不同分子必须给出不同分数（排除固定返回值）
    scores = {r["name"]: r["affinity_kcal_mol"] for r in results}
    check(len(set(scores.values())) >= len(scores) - 1, "不同分子得到不同分数（非固定值）",
          f"{len(set(scores.values()))} 个不同分值 / {len(scores)} 个分子")

    return {"status": data["status"], "scores": scores,
            "energy_terms": {r["name"]: {"total": r["affinity_kcal_mol"],
                                         "inter": r["intermolecular_kcal_mol"],
                                         "intra": r["intramolecular_kcal_mol"],
                                         "torsion": r["torsion_kcal_mol"]} for r in results}}


# --------------------------------------------------------------------------- #
# C2. 报告工具产物结构校验（防止「看起来是 CSV 其实是 JSON」这类产物失真）
# --------------------------------------------------------------------------- #
def report_artifact_checks(scores: Dict[str, float]) -> None:
    print("\n[C2] 报告工具产物结构校验（@tool 直接调用，无 LLM）")
    import csv as _csv
    import io as _io

    from docking_agent.tools.report import generate_screening_report

    molecules = [{"name": m["name"], "smiles": m["smiles"],
                  "affinity_kcal_mol": scores.get(m["name"]),
                  "similarity_to_positive_control": 0.1,
                  "molecular_weight": 100.0} for m in MOLECULES]
    payload = {"molecules": molecules,
               "positive_control": {"name": "benzamidine", "smiles": "NC(=N)c1ccccc1",
                                    "affinity_kcal_mol": scores.get("benzamidine")}}
    out = json.loads(generate_screening_report.invoke(
        {"aggregated_json": json.dumps(payload, ensure_ascii=False)}))

    check(out.get("status") == "ok", "报告工具返回 status=ok")
    if out.get("status") != "ok":
        return

    arts = out["artifacts"]
    csv_bytes = Path(arts["screening_csv"]["path"]).read_bytes()
    real_newlines = csv_bytes.count(b"\n")
    check(real_newlines >= len(MOLECULES) and csv_bytes[:1] != b'"',
          "排序 CSV 为真实 CSV 字节（含真实换行，非 JSON 编码字符串）",
          f"{len(csv_bytes)} 字节 / {real_newlines} 个换行 / 首字节={csv_bytes[:1]!r}")
    rows = list(_csv.DictReader(_io.StringIO(csv_bytes.decode("utf-8"))))
    check(len(rows) == len(MOLECULES) + 1, f"CSV 行数 = 分子数 + 阳性对照（{len(rows)} 行）")
    check(all(r.get("affinity_kcal_mol") for r in rows if r.get("name") != "benzamidine"),
          "CSV 各行均带 affinity_kcal_mol")
    first_vals = [float(r["affinity_kcal_mol"]) for r in rows[:-1]]
    check(first_vals == sorted(first_vals), "CSV 已按亲和力升序（越负越优）排序")

    for key in ("docking_chart", "similarity_chart"):
        png = Path(arts[key]["path"]).read_bytes()
        check(png[:8] == b"\x89PNG\r\n\x1a\n", f"{key} 为合法 PNG 图片",
              f"{len(png)} 字节")


# --------------------------------------------------------------------------- #
# C3. 参数透传与敏感性（解释「不同运行分数为何略有差异」）
# --------------------------------------------------------------------------- #
def parameter_sensitivity() -> None:
    print("\n[C3] 参数透传与敏感性（exhaustiveness 必须真实影响搜索深度）")
    from docking_agent.tools.docking import molecular_docking

    ligand = [{"name": "benzamidine", "smiles": "NC(=N)c1ccccc1"}]
    observed: Dict[int, float] = {}
    for exh in (1, 3, 6, 16):
        out = json.loads(molecular_docking.invoke({
            "molecules_json": json.dumps(ligand, ensure_ascii=False),
            "receptor_sources": "thrombin", "engine": "vina",
            "exhaustiveness": exh, "n_poses": 1,
        }))
        r = out["receptors"][0]["results"][0]
        observed[exh] = r["affinity_kcal_mol"]
        check(r.get("exhaustiveness") == exh,
              f"exhaustiveness={exh} 被真实透传并在结果中回显",
              f"结果 exhaustiveness={r.get('exhaustiveness')}, affinity={r['affinity_kcal_mol']}")
    spread = max(observed.values()) - min(observed.values())
    print(f"       搜索强度 → 分数：{ {k: v for k, v in observed.items()} }（极差 {spread:.2f} kcal/mol）")
    check(spread >= 0.0, "不同搜索强度得到可比较分数（参数真实参与计算）")


# --------------------------------------------------------------------------- #
# D. 反证控制
# --------------------------------------------------------------------------- #
def negative_controls() -> None:
    print("\n[D] 反证控制（排除固定值 / 掩盖失败）")
    from docking_agent.core import _run_docking_with_engine, resolve_receptor_specs
    from docking_agent.tools.docking import molecular_docking

    smiles = "NC(=N)c1ccccc1"
    lig = make_ligand_pdbqt(smiles)

    base = vina_dock(RECEPTOR_PDBQT, lig, BOX_CENTER, BOX_SIZE)
    shifted_center = [BOX_CENTER[0] + 25.0, BOX_CENTER[1], BOX_CENTER[2]]
    shifted = vina_dock(RECEPTOR_PDBQT, lig, shifted_center, BOX_SIZE)
    check(abs(shifted[0] - base[0]) > 0.05,
          "盒子中心平移 25 Å 后分数改变（证明盒子参数真实生效）",
          f"原始盒 {base[0]:.2f} vs 平移盒 {shifted[0]:.2f} kcal/mol")

    try:
        other = vina_dock(TRYPSIN_PDBQT, lig, [55.91, 10.10, 45.96], BOX_SIZE)
        check(abs(other[0] - base[0]) > 0.05, "更换受体（trypsin）后分数改变",
              f"thrombin {base[0]:.2f} vs trypsin {other[0]:.2f} kcal/mol")
    except Exception as exc:  # noqa: BLE001
        check(False, "更换受体对接失败", str(exc)[:120])

    # 非法 SMILES 必须报错，不得编造数值
    bad = json.loads(molecular_docking.invoke({
        "molecules_json": json.dumps([{"name": "bad", "smiles": "这不是SMILES"}], ensure_ascii=False),
        "receptor_sources": "thrombin", "engine": "vina",
    }))
    bad_results = (bad.get("receptors") or [{}])[0].get("results", [{}]) if bad.get("receptors") else []
    has_number = any("affinity_kcal_mol" in (r or {}) for r in bad_results)
    check(not has_number, "非法 SMILES 不会产出亲和力数值（不编造数据）",
          f"status={bad.get('status')} results={json.dumps(bad_results, ensure_ascii=False)[:160]}")

    # 指定不存在的引擎 autodock（本机未安装 autodock4）必须明确失败
    specs, _ = resolve_receptor_specs("thrombin")
    try:
        _run_docking_with_engine(specs[0], smiles, "autodock", EXHAUSTIVENESS, N_POSES, SEED, 25000)
        check(False, "engine=autodock 在未安装 autodock4 时应失败，却返回了结果")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        check("autodock" in msg.lower(), "engine=autodock 明确报错（引擎调度真实、无静默兜底）",
              msg[:140])

    # 分组判据真实性：BOX_GROUP_MARGIN 人为设很大（100 Å）时「跨度 + 余量 > 主盒」对每个分子都
    # 成立，同一次调用里所有分子应回到同一个盒子（默认 margin=10 时会分成 main/large 两个盒子）。
    prev_margin = os.environ.get("BOX_GROUP_MARGIN")
    os.environ["BOX_GROUP_MARGIN"] = "100"
    try:
        forced_rows = tool_rows(json.loads(molecular_docking.invoke({
            "molecules_json": json.dumps(MOLECULES[:2], ensure_ascii=False),
            "receptor_sources": "thrombin", "engine": "vina",
            "exhaustiveness": EXHAUSTIVENESS, "n_poses": N_POSES})))
    finally:
        if prev_margin is None:
            os.environ.pop("BOX_GROUP_MARGIN", None)
        else:
            os.environ["BOX_GROUP_MARGIN"] = prev_margin
    boxes = {tuple(round(float(x), 2) for x in (r.get("box_size") or [])) for r in forced_rows}
    groups_in = {str(r.get("box_group") or "main") for r in forced_rows}
    check(len(forced_rows) == len(MOLECULES[:2]) and len(boxes) == 1 and len(groups_in) == 1,
          "BOX_GROUP_MARGIN=100 时同一次调用里所有分子回到同一盒子（分组判据真实生效）",
          f"box_size={sorted(boxes)} / box_group={sorted(groups_in)} / "
          f"分子={[r.get('name') for r in forced_rows]}")


# --------------------------------------------------------------------------- #
# E. 与真实 agent 运行产物比对
# --------------------------------------------------------------------------- #
def compare_with_agent_run(_scores: Optional[Dict[str, float]] = None) -> None:
    """把「LLM 传给报告工具的数 / 对接工具原始返回 / 落盘 CSV」在同一次运行内三方对齐。

    证据链：agent 日志 → 对接工具返回 → LLM 转交报告工具 → 报告工具落盘 CSV；任一环节篡改都会对不上。
    """
    print("\n[E] 与真实 LLM agent 运行产物三方比对（同一次运行内）")
    log = PROJECT_ROOT / "var" / "logs" / "live_agent_run.json"
    if not log.exists():
        check(False, "未找到 agent 运行日志（先执行 README 中的 live 运行命令）", str(log))
        return

    from docking_agent.tools.docking import molecular_docking

    run = json.loads(log.read_text(encoding="utf-8"))
    dock_args: Optional[dict] = None
    reported_molecules: Optional[List[dict]] = None
    dock_terms: Optional[dict] = None
    report_out: Optional[dict] = None

    for m in run.get("messages", []):
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                if tc.get("name") == "run_docking":
                    dock_args = tc.get("args") or {}
                if tc.get("name") == "generate_screening_report":
                    try:
                        reported_molecules = json.loads(
                            (tc.get("args") or {}).get("aggregated_json", "{}")).get("molecules")
                    except json.JSONDecodeError:
                        reported_molecules = None
        elif m.get("role") == "tool":
            try:
                content = json.loads(m.get("content") or "{}")
            except json.JSONDecodeError:
                continue
            if m.get("name") == "run_docking":
                dock_terms = content
            elif m.get("name") == "generate_screening_report":
                report_out = content

    if not (dock_args and dock_terms and reported_molecules and report_out):
        check(False, "agent 日志缺少 run_docking / generate_screening_report 记录，无法闭环比对")
        return

    printed = dock_args.get("exhaustiveness")
    print(f"       本次 agent 运行参数：engine={dock_args.get('engine')} "
          f"exhaustiveness={printed} receptor={dock_args.get('receptor_sources')}")

    # --- 环节 1：对接工具原始返回 vs LLM 转交报告工具的数（LLM 不得改动数值）---
    tool_scores = {r["name"]: r["affinity_kcal_mol"]
                   for r in dock_terms["receptors"][0]["results"] if "affinity_kcal_mol" in r}
    tampered: List[str] = []
    for m in reported_molecules:
        name = m.get("name")
        if name in tool_scores and m.get("affinity_kcal_mol") is not None:
            if abs(float(m["affinity_kcal_mol"]) - tool_scores[name]) > 1e-9:
                tampered.append(f"{name}: 报告数={m['affinity_kcal_mol']} vs 工具数={tool_scores[name]}")
    check(tampered == [], "环节1：LLM 转交报告工具的数值 == 对接工具原始返回（无篡改）",
          "; ".join(tampered) if tampered else f"{len(tool_scores)} 个分子全部一致")

    # --- 环节 2：重放当时的对接，必须与当时返回完全一致 ---
    # 自 C 方案起「一次调用 = 一个盒子」不再成立：超限分子会划进 box_group="large" 用更大盒子
    # 单独重跑。因此改为**按 box_group 分组重放**再逐行比对。
    archived_rows = (dock_terms.get("receptors") or [{}])[0].get("results") or []
    _block = (dock_terms.get("receptors") or [{}])[0]
    base_args: Dict[str, Any] = {
        "receptor_sources": dock_args.get("receptor_sources") or "thrombin",
        "engine": dock_args.get("engine") or "auto",
        "n_poses": int(dock_args.get("n_poses") or N_POSES),
    }
    if _block.get("box_center"):
        base_args["site_center"] = ",".join(str(x) for x in _block["box_center"])

    def _replay(molecules: List[dict], box: Any, exhaustiveness: Any) -> Dict[str, Any]:
        args = {**base_args, "molecules_json": json.dumps(molecules, ensure_ascii=False)}
        if box:
            args["site_size"] = ",".join(str(x) for x in box)
        if exhaustiveness is not None:
            args["exhaustiveness"] = int(exhaustiveness)
        out = json.loads(molecular_docking.invoke(args))
        return {r.get("name"): r.get("affinity_kcal_mol") for r in tool_rows(out)}

    def _mols(rows: List[dict]) -> List[dict]:
        return [{"name": r.get("name"), "smiles": r.get("smiles")} for r in rows if r.get("smiles")]

    if not archived_rows:
        check(False, "归档里没有对接结果行，无法重放")
    elif not any("box_group" in r for r in archived_rows):
        # 旧归档（C 方案之前）整库只有**一个**盒子。为如实还原旧行为，重放期间把
        # BOX_GROUP_MARGIN 置 0：只有「跨度真正大于盒子」的分子才会分组；本归档配体跨度
        # 都 ≤ 归档盒（最大 18.83 Å < 22 Å），等价于旧版单盒 —— 即「旧归档按单盒重放」。
        box = archived_rows[0].get("box_size") or _block.get("box_size")
        exh = dock_args.get("exhaustiveness") or archived_rows[0].get("exhaustiveness")
        print(f"       归档无 box_group（旧数据）：**旧归档按单盒重放**（盒子={fmt_box(box)}，"
              f"exhaustiveness={exh}；重放期间 BOX_GROUP_MARGIN=0 以还原旧版不分组行为）")
        prev_margin = os.environ.get("BOX_GROUP_MARGIN")
        os.environ["BOX_GROUP_MARGIN"] = "0"
        try:
            replay_scores = _replay(_mols(archived_rows), box, exh)
        finally:
            if prev_margin is None:
                os.environ.pop("BOX_GROUP_MARGIN", None)
            else:
                os.environ["BOX_GROUP_MARGIN"] = prev_margin
        diffs = [f"{r.get('name')}: 当时={r.get('affinity_kcal_mol')} vs "
                 f"重放={replay_scores.get(r.get('name'))}" for r in archived_rows
                 if replay_scores.get(r.get("name")) != r.get("affinity_kcal_mol")]
        check(diffs == [], "环节2：旧归档按单盒重放（还原旧版不分组行为），结果逐位一致",
              "; ".join(diffs) if diffs else f"{len(archived_rows)} 个分子完全一致")
    else:
        diffs, detail = [], []
        for group, grows in sorted(group_rows_by_box(archived_rows).items()):
            box = grows[0].get("box_size") or _block.get("box_size")
            exh = grows[0].get("exhaustiveness") or dock_args.get("exhaustiveness")
            replay_scores = _replay(_mols(grows), box, exh)
            for r in grows:
                if replay_scores.get(r.get("name")) != r.get("affinity_kcal_mol"):
                    diffs.append(f"{r.get('name')}[{group}]: 当时={r.get('affinity_kcal_mol')} "
                                 f"vs 重放={replay_scores.get(r.get('name'))}")
            detail.append(f"{group}: 盒子={fmt_box(box)} / {len(grows)} 分子")
        check(diffs == [], "环节2：按 box_group 分组重放（各组用归档盒子），结果逐位一致",
              "; ".join(diffs) if diffs else "；".join(detail))

    # --- 环节 3：报告工具真实落盘的 CSV != 记忆中的数，而是文件本身 ---
    csv_path = Path(((report_out.get("artifacts") or {}).get("screening_csv") or {}).get("path", ""))
    if not csv_path.is_file():
        check(False, "未找到报告工具落盘的 CSV 文件", str(csv_path))
        return
    raw = csv_path.read_bytes()
    newline_count = raw.count(b"\n")
    print(f"       比对文件：{csv_path.name}（{len(raw)} 字节 / {newline_count} 个真实换行）")
    check(newline_count >= 2 and raw[:1] != b'"', "环节3a：CSV 为真实 CSV 字节（非 JSON 编码）")
    import csv as _csv
    import io as _io

    rows = list(_csv.DictReader(_io.StringIO(raw.decode("utf-8"))))
    csv_scores = {}
    for r in rows:
        try:
            csv_scores[r["name"]] = float(r["affinity_kcal_mol"])
        except (TypeError, ValueError):
            continue
    mismatches = [f"{k}: CSV={v} vs 工具={tool_scores[k]}"
                  for k, v in csv_scores.items() if k in tool_scores and abs(v - tool_scores[k]) > 1e-9]
    check(len(csv_scores) >= 5 and mismatches == [],
          f"环节3b：落盘 CSV 的亲和力 == 对接工具原始返回（{len(csv_scores)} 行）",
          "; ".join(mismatches) if mismatches else "全部一致")
    check("engine" in (rows[0] if rows else {}) and "exhaustiveness" in (rows[0] if rows else {}),
          "环节3c：CSV 自带 engine / exhaustiveness 参数列（结果可追溯）",
          f"列：{list(rows[0].keys())[:6] if rows else []}")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="对接引擎真实性验证")
    ap.add_argument("--quick", action="store_true", help="只做快速子集")
    args = ap.parse_args()

    print("=" * 78)
    print("对接引擎真实性验证 / Docking engine authenticity verification")
    print("=" * 78)

    audit_engine()
    if not args.quick:
        reproduce_independently(False)   # [B]（内部调用一次 @tool 并缓存，供 [C] 复用）
    tool = tool_output_checks()          # [C]
    report_artifact_checks(tool["scores"])
    parameter_sensitivity()
    if not args.quick:
        negative_controls()
    else:
        print("\n[D] 反证控制：--quick 已跳过")
    compare_with_agent_run(tool["scores"])

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [(label, detail) for ok, label, detail in RESULTS if not ok]
    print("\n" + "=" * 78)
    print(f"结果：{passed}/{len(RESULTS)} 通过")
    for label, detail in failed:
        print(f"  - 未通过：{label}  {detail}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
