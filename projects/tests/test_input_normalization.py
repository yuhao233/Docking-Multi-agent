"""输入归一化层（`docking_agent.core.normalize`）的真实测试矩阵。

全部使用真实文件、真实 RDKit 解析、真实受体准备（小 PDB），不 mock。
覆盖「脏输入」：GBK/UTF-8 BOM、CSV/TSV/分号、单列表头、InChIKey/InChI、
多记录 SDF、MOL2、错误扩展名、gzip/zip 容器、坏行、重复分子、受体内容嗅探。
"""
from __future__ import annotations

import gzip
import importlib.util
import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rdkit import Chem  # noqa: E402

from docking_agent.core import normalize as N  # noqa: E402

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
IBUPROFEN = "CC(C)Cc1ccc(cc1)C(C)C(=O)O"
CAFFEINE = "Cn1cnc2c1c(=O)n(C)c(=O)n2C"
PARACETAMOL = "CC(=O)Nc1ccc(O)cc1"
ASPIRIN_INCHI = "InChI=1S/C9H8O4/c1-6(10)13-8-5-3-2-4-7(8)9(11)12/h2-5H,1H3,(H,11,12)"
ASPIRIN_KEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"

MOL2_BENZENE = """@<TRIPOS>MOLECULE
BENZENE
 12 12 0 0 0
SMALL
NO_CHARGES

@<TRIPOS>ATOM
      1 C1        1.3960    0.0000    0.0000 C.2       1  BEN      0.0000
      2 C2        0.6980    1.2090    0.0000 C.2       1  BEN      0.0000
      3 C3       -0.6980    1.2090    0.0000 C.2       1  BEN      0.0000
      4 C4       -1.3960    0.0000    0.0000 C.2       1  BEN      0.0000
      5 C5       -0.6980   -1.2090    0.0000 C.2       1  BEN      0.0000
      6 C6        0.6980   -1.2090    0.0000 C.2       1  BEN      0.0000
      7 H1        2.4790    0.0000    0.0000 H         1  BEN      0.0000
      8 H2        1.2400    2.1480    0.0000 H         1  BEN      0.0000
      9 H3       -1.2400    2.1480    0.0000 H         1  BEN      0.0000
     10 H4       -2.4790    0.0000    0.0000 H         1  BEN      0.0000
     11 H5       -1.2400   -2.1480    0.0000 H         1  BEN      0.0000
     12 H6        1.2400   -2.1480    0.0000 H         1  BEN      0.0000
@<TRIPOS>BOND
     1    1    2 ar
     2    2    3 ar
     3    3    4 ar
     4    4    5 ar
     5    5    6 ar
     6    6    1 ar
     7    1    7 1
     8    2    8 1
     9    3    9 1
    10    4   10 1
    11    5   11 1
    12    6   12 1
"""

# 真实的 6 残基小肽（取自 assets/receptors/structures/thrombin.pdb 的 ATOM 记录），
# 用于一次真实但很快的 meeko 受体准备（约 0.3s）。
TINY_PDB = "\n".join([
    "ATOM      1  N   GLU L   1C     63.691  25.940  17.920  1.00 39.73           N  ",
    "ATOM      2  CA  GLU L   1C     64.331  26.594  16.778  1.00 41.21           C  ",
    "ATOM      3  C   GLU L   1C     63.325  27.181  15.823  1.00 43.53           C  ",
    "ATOM      4  O   GLU L   1C     63.253  28.400  15.711  1.00 48.20           O  ",
    "ATOM      5  CB  GLU L   1C     65.333  25.719  16.023  1.00 41.20           C  ",
    "ATOM      6  N   ALA L   1B     62.540  26.309  15.144  1.00 39.72           N  ",
    "ATOM      7  CA  ALA L   1B     61.528  26.767  14.192  1.00 35.59           C  ",
    "ATOM      8  C   ALA L   1B     60.677  27.829  14.830  1.00 34.53           C  ",
    "ATOM      9  O   ALA L   1B     60.267  27.670  15.965  1.00 34.72           O  ",
    "ATOM     10  CB  ALA L   1B     60.675  25.608  13.772  1.00 34.79           C  ",
    "ATOM     11  N   ASP L   1A     60.419  28.936  14.148  1.00 33.71           N  ",
    "ATOM     12  CA  ASP L   1A     59.616  29.916  14.837  1.00 34.84           C  ",
    "ATOM     13  C   ASP L   1A     58.180  29.473  14.803  1.00 32.52           C  ",
    "ATOM     14  O   ASP L   1A     57.740  28.843  13.838  1.00 36.56           O  ",
    "ATOM     15  CB  ASP L   1A     59.843  31.394  14.473  1.00 40.37           C  ",
    "ATOM     16  CG  ASP L   1A     58.777  31.961  13.594  1.00 46.11           C  ",
    "ATOM     17  OD1 ASP L   1A     57.587  32.151  14.206  1.00 47.53           O  ",
    "ATOM     18  OD2 ASP L   1A     58.998  32.224  12.393  1.00 49.59           O  ",
    "ATOM     19  N   CYS L   1      57.464  29.769  15.871  1.00 25.39           N  ",
    "ATOM     20  CA  CYS L   1      56.104  29.368  15.999  1.00 19.29           C  ",
    "ATOM     21  C   CYS L   1      55.465  30.177  17.051  1.00 17.84           C  ",
    "ATOM     22  O   CYS L   1      56.146  30.757  17.853  1.00 18.58           O  ",
    "ATOM     23  CB  CYS L   1      56.102  27.939  16.548  1.00 16.58           C  ",
    "ATOM     24  SG  CYS L   1      56.982  27.807  18.150  1.00 14.20           S  ",
    "ATOM     25  N   GLY L   2      54.159  30.186  17.080  1.00 15.50           N  ",
    "ATOM     26  CA  GLY L   2      53.489  30.931  18.092  1.00 13.49           C  ",
    "ATOM     27  C   GLY L   2      53.388  32.388  17.738  1.00 13.84           C  ",
    "ATOM     28  O   GLY L   2      52.651  33.115  18.393  1.00 17.90           O  ",
    "ATOM     29  N   LEU L   3      54.104  32.832  16.706  1.00 10.01           N  ",
    "ATOM     30  CA  LEU L   3      54.005  34.242  16.319  1.00  9.67           C  ",
    "ATOM     31  C   LEU L   3      53.120  34.437  15.092  1.00  8.37           C  ",
    "ATOM     32  O   LEU L   3      53.507  34.137  13.944  1.00  9.68           O  ",
    "ATOM     33  CB  LEU L   3      55.361  34.898  16.174  1.00 12.28           C  ",
    "ATOM     34  CG  LEU L   3      56.125  34.734  17.469  1.00 15.63           C  ",
    "ATOM     35  CD1 LEU L   3      57.601  34.943  17.212  1.00 16.75           C  ",
    "ATOM     36  CD2 LEU L   3      55.619  35.724  18.515  1.00 16.09           C  ",
]) + "\n"


def canonical(smiles: str) -> str:
    return Chem.MolToSmiles(Chem.MolFromSmiles(smiles))


def write(path: Path, payload, encoding: str = "utf-8") -> Path:
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding=encoding)
    return path


# --------------------------------------------------------------------------- #
# 1. 中文表头 CSV，GBK 编码
# --------------------------------------------------------------------------- #


def test_chinese_header_csv_gbk(tmp_path) -> None:
    path = write(tmp_path / "lib_gbk.csv",
                 "名称,SMILES\n阿司匹林,%s\n布洛芬,%s\n" % (ASPIRIN, IBUPROFEN),
                 encoding="gbk")
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "csv"
    assert norm["encoding"] == "gbk"
    assert norm["delimiter"] == ","
    assert norm["header"] == {"name": "名称", "smiles": "SMILES"}
    assert [m["id"] for m in mols] == ["阿司匹林", "布洛芬"]
    assert [m["name"] for m in mols] == ["阿司匹林", "布洛芬"]
    assert [m["smiles"] for m in mols] == [canonical(ASPIRIN), canonical(IBUPROFEN)]
    assert [m["source_index"] for m in mols] == [1, 2]
    assert norm["records_total"] == 2 and norm["records_ok"] == 2
    assert norm["records_skipped"] == 0 and norm["skipped"] == []


# --------------------------------------------------------------------------- #
# 2. Tab 分隔 TSV
# --------------------------------------------------------------------------- #


def test_tsv_tab_separated(tmp_path) -> None:
    path = write(tmp_path / "lib.tsv", "name\tsmiles\naspirin\t%s\n" % ASPIRIN)
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "tsv"
    assert norm["delimiter"] == "\t"
    assert norm["header"] == {"name": "name", "smiles": "smiles"}
    assert [(m["id"], m["smiles"]) for m in mols] == [("aspirin", canonical(ASPIRIN))]


# --------------------------------------------------------------------------- #
# 3. UTF-8 BOM + 分号分隔
# --------------------------------------------------------------------------- #


def test_bom_semicolon_csv(tmp_path) -> None:
    path = write(tmp_path / "lib_bom.csv",
                 "\ufeff名称;SMILES\n咖啡因;%s\n" % CAFFEINE, encoding="utf-8")
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "csv"
    assert norm["delimiter"] == ";"
    assert norm["encoding"] == "utf-8-sig"
    assert norm["header"] == {"name": "名称", "smiles": "SMILES"}
    assert [(m["id"], m["smiles"]) for m in mols] == [("咖啡因", canonical(CAFFEINE))]


# --------------------------------------------------------------------------- #
# 4. 只有 SMILES 一列（无名称 → MOL1/MOL2/...）
# --------------------------------------------------------------------------- #


def test_smiles_only_column_csv(tmp_path) -> None:
    path = write(tmp_path / "only_smiles.csv", "SMILES\nCCO\nCCN\nCCC\n")
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "csv"
    assert norm["header"] == {"name": "", "smiles": "SMILES"}
    assert [m["id"] for m in mols] == ["MOL1", "MOL2", "MOL3"]
    assert [m["smiles"] for m in mols] == [canonical("CCO"), canonical("CCN"), canonical("CCC")]
    assert all(m["source_file"] == os.path.abspath(str(path)) for m in mols)


# --------------------------------------------------------------------------- #
# 5. 只有 InChIKey 一列 → 解析为 SMILES
# --------------------------------------------------------------------------- #


def test_inchikey_only_column_csv(tmp_path) -> None:
    path = write(tmp_path / "only_key.csv", "InChIKey\n%s\n" % ASPIRIN_KEY)
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "csv"
    assert norm["header"]["smiles"] == ""          # 没有 SMILES 表头，只有 InChIKey
    assert len(mols) == 1
    assert mols[0]["id"] == "MOL1"
    assert mols[0]["smiles"] == canonical(ASPIRIN)
    assert norm["records_ok"] == 1 and norm["records_skipped"] == 0


# --------------------------------------------------------------------------- #
# 6. 多记录 SDF，`_Name` 作为 id
# --------------------------------------------------------------------------- #


def test_multi_record_sdf_with_name(tmp_path) -> None:
    path = tmp_path / "multi.sdf"
    writer = Chem.SDWriter(str(path))
    for name, smiles in (("Ethanol", "CCO"), ("Benzene", "c1ccccc1"), ("Caffeine", CAFFEINE)):
        mol = Chem.MolFromSmiles(smiles)
        mol.SetProp("_Name", name)
        writer.write(mol)
    writer.close()

    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "sdf"
    assert norm["records_total"] == 3 and norm["records_ok"] == 3
    assert [m["id"] for m in mols] == ["Ethanol", "Benzene", "Caffeine"]
    assert [m["name"] for m in mols] == ["Ethanol", "Benzene", "Caffeine"]
    assert [m["smiles"] for m in mols] == [canonical("CCO"), canonical("c1ccccc1"), canonical(CAFFEINE)]
    assert all(m["raw"].strip() for m in mols)
    assert [m["source_index"] for m in mols] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# 7. MOL2
# --------------------------------------------------------------------------- #


def test_mol2_file(tmp_path) -> None:
    path = write(tmp_path / "benzene.mol2", MOL2_BENZENE)
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "mol2"
    assert len(mols) == 1
    assert mols[0]["id"] == "BENZENE"
    assert mols[0]["smiles"] == canonical("c1ccccc1")


# --------------------------------------------------------------------------- #
# 8. 错误扩展名：内容是权威
# --------------------------------------------------------------------------- #


def test_wrong_extension_is_content_driven(tmp_path) -> None:
    # 8a. SDF 内容装在 .txt 里
    sdf_path = tmp_path / "real.sdf"
    writer = Chem.SDWriter(str(sdf_path))
    mol = Chem.MolFromSmiles("CCO")
    mol.SetProp("_Name", "Ethanol")
    writer.write(mol)
    writer.close()
    disguised = write(tmp_path / "disguised.txt", sdf_path.read_bytes())

    mols, norm = N.normalize_ligand_file(str(disguised))
    assert norm["format"] == "sdf"
    assert [(m["id"], m["smiles"]) for m in mols] == [("Ethanol", canonical("CCO"))]

    # 8b. CSV 内容装在 .sdf 里
    disguised2 = write(tmp_path / "disguised.sdf", "name,smiles\naspirin,%s\n" % ASPIRIN)
    mols2, norm2 = N.normalize_ligand_file(str(disguised2))
    assert norm2["format"] == "csv"
    assert [(m["id"], m["smiles"]) for m in mols2] == [("aspirin", canonical(ASPIRIN))]


# --------------------------------------------------------------------------- #
# 9. gzip 压缩的 CSV
# --------------------------------------------------------------------------- #


def test_gzip_csv(tmp_path) -> None:
    path = write(tmp_path / "lib.csv.gz",
                 gzip.compress(("name,smiles\naspirin,%s\n" % ASPIRIN).encode("utf-8")))
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "csv"
    assert [(m["id"], m["smiles"]) for m in mols] == [("aspirin", canonical(ASPIRIN))]
    assert any("gzip" in note for note in norm["notes"])
    assert os.path.isfile(norm["source_file"])


# --------------------------------------------------------------------------- #
# 10. 坏行不中断整个库，并且带精确行号
# --------------------------------------------------------------------------- #


def test_bad_rows_report_exact_line_numbers(tmp_path) -> None:
    lines = [
        "CCO",                       # 1 ok
        "this_is_not_a_smiles!!!",   # 2 bad
        "CCN",                       # 3 ok
        "CCC",                       # 4 ok
        "???",                       # 5 bad
        "c1ccccc1",                  # 6 ok
        "CC(=O)O",                   # 7 ok
        "CCOC(=O)C",                 # 8 ok
        "NaN",                       # 9 bad
    ]
    path = write(tmp_path / "bad_rows.smi", "\n".join(lines) + "\n")
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "smi"
    assert norm["records_ok"] == 6
    assert norm["records_skipped"] == 3
    assert norm["records_total"] == 9
    assert [s["line"] for s in norm["skipped"]] == [2, 5, 9]
    assert all(s["reason"].strip() for s in norm["skipped"])
    # 坏行没有污染好分子
    assert len(mols) == 6
    assert canonical("CCO") in [m["smiles"] for m in mols]


def test_smi_smiles_first_with_name(tmp_path) -> None:
    """.smi 的标准顺序是「SMILES 名称」，名称要被保留。"""
    path = write(tmp_path / "std.smi", "CCO ethanol\nCCN ethylamine\n")
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "smi"
    assert [(m["id"], m["smiles"]) for m in mols] == [
        ("ethanol", canonical("CCO")),
        ("ethylamine", canonical("CCN")),
    ]
    assert norm["records_skipped"] == 0


def test_smi_header_line_is_not_a_bad_record(tmp_path) -> None:
    path = write(tmp_path / "with_header.smi", "SMILES name\nCCO ethanol\n")
    mols, norm = N.normalize_ligand_file(str(path))

    assert [(m["id"], m["smiles"]) for m in mols] == [("ethanol", canonical("CCO"))]
    assert norm["records_total"] == 1
    assert norm["records_skipped"] == 0


# --------------------------------------------------------------------------- #
# 11. 同一规范 SMILES 的重复分子：保留第一条，合并别名
# --------------------------------------------------------------------------- #


def test_duplicate_canonical_smiles_deduped(tmp_path) -> None:
    path = write(tmp_path / "dup.csv",
                 "name,smiles\n阿司匹林,%s\nAspirin,OC(=O)c1ccccc1OC(C)=O\n" % ASPIRIN)
    mols, norm = N.normalize_ligand_file(str(path))

    assert len(mols) == 1
    assert mols[0]["id"] == "阿司匹林"          # 第一条被保留
    assert mols[0]["name"] == "阿司匹林"
    assert mols[0]["smiles"] == canonical(ASPIRIN)
    assert norm["records_total"] == 2
    assert norm["records_ok"] == 1
    assert norm["duplicates_removed"] == 1
    assert set(norm["aliases"]["阿司匹林"]) == {"阿司匹林", "Aspirin"}
    assert len(norm["duplicates"]) == 1
    dup = norm["duplicates"][0]
    assert dup["kept_name"] == "阿司匹林"
    assert set(dup["aliases"]) == {"阿司匹林", "Aspirin"}


# --------------------------------------------------------------------------- #
# 12. 受体侧：非结构被拒绝；`.txt` 里的真 PDB 被识别并准备
# --------------------------------------------------------------------------- #


def test_receptor_rejects_non_structure(tmp_path) -> None:
    path = write(tmp_path / "not_a_receptor.csv", "name,smiles\naspirin,%s\n" % ASPIRIN)
    with pytest.raises(ValueError) as excinfo:
        N.normalize_receptor_source(str(path))
    message = str(excinfo.value)
    assert "csv" in message.lower()
    assert "ATOM" in message or "HETATM" in message
    assert "结构" in message


def test_receptor_pdb_content_in_txt(tmp_path) -> None:
    path = write(tmp_path / "receptor.txt", TINY_PDB)

    # 内容优先：`.txt` 不是结构扩展名，但内容是 PDB
    assert N.sniff_format(str(path)) == "pdb"
    assert N.sniff_format(str(path), path.read_bytes()) == "pdb"

    receptor_path, norm = N.normalize_receptor_source(str(path))
    assert norm["format"] == "pdb"
    assert receptor_path and os.path.isfile(receptor_path)
    assert norm["notes"] and any("pdb" in note.lower() for note in norm["notes"])
    assert norm["source_file"] == os.path.abspath(str(path))
    # 化学溯源字段来自 read_receptor_file 的 spec
    assert set(("dropped_hetatm", "kept_hetatm", "dropped_waters", "unsupported_hetatm")) <= set(norm)


# --------------------------------------------------------------------------- #
# 附加：gzip SDF、InChI 列、xlsx 签名、sniff 矩阵、自由文本 API
# --------------------------------------------------------------------------- #


def test_gzip_sdf(tmp_path) -> None:
    raw = tmp_path / "raw.sdf"
    writer = Chem.SDWriter(str(raw))
    mol = Chem.MolFromSmiles(PARACETAMOL)
    mol.SetProp("_Name", "Paracetamol")
    writer.write(mol)
    writer.close()

    path = write(tmp_path / "packed.sdf.gz", gzip.compress(raw.read_bytes()))
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "sdf"
    assert [(m["id"], m["smiles"]) for m in mols] == [("Paracetamol", canonical(PARACETAMOL))]
    assert any("gzip" in note for note in norm["notes"])


def test_zip_container_single_member(tmp_path) -> None:
    path = tmp_path / "lib.zip"
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("library.csv", "name,smiles\naspirin,%s\n" % ASPIRIN)
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "csv"
    assert [(m["id"], m["smiles"]) for m in mols] == [("aspirin", canonical(ASPIRIN))]
    assert any("zip" in note for note in norm["notes"])


def test_inchi_column_csv(tmp_path) -> None:
    paracetamol_inchi = Chem.MolToInchi(Chem.MolFromSmiles(PARACETAMOL))
    assert paracetamol_inchi.startswith("InChI=")
    # InChI 自带逗号：既测正确加引号的 CSV，也测「未加引号」这种真实脏输入
    quoted = tmp_path / "inchi_quoted.csv"
    with quoted.open("w", encoding="utf-8", newline="") as fh:
        import csv as _csv
        writer = _csv.writer(fh)
        writer.writerow(["name", "InChI"])
        writer.writerow(["aspirin", ASPIRIN_INCHI])
    mols, norm = N.normalize_ligand_file(str(quoted))
    assert norm["format"] == "csv"
    assert norm["header"] == {"name": "name", "smiles": ""}
    assert [(m["id"], m["smiles"]) for m in mols] == [("aspirin", canonical(ASPIRIN))]

    unquoted = write(tmp_path / "inchi_unquoted.csv",
                     "name,InChI\naspirin,%s\nparacetamol,%s\n" % (ASPIRIN_INCHI, paracetamol_inchi))
    mols2, norm2 = N.normalize_ligand_file(str(unquoted))
    assert norm2["format"] == "csv"
    # InChI 的 mobile-H 归一化可能在还原时给出另一个等价互变异构体，因此以
    # 「RDKit 从同一 InChI 还原出的规范 SMILES」为期望值（真实化学行为，非解析缺陷）。
    expected = [Chem.MolToSmiles(Chem.MolFromInchi(inchi))
                for inchi in (ASPIRIN_INCHI, paracetamol_inchi)]
    assert [m["id"] for m in mols2] == ["aspirin", "paracetamol"]
    assert [m["smiles"] for m in mols2] == expected


def test_xlsx_signature_without_openpyxl(tmp_path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
    path = write(tmp_path / "lib.xlsx", buf.getvalue())

    assert N.sniff_format(str(path)) == "xlsx"
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "xlsx"
    assert mols == []
    if importlib.util.find_spec("openpyxl") is None:
        assert any("xlsx" in note.lower() and "CSV" in note for note in norm["notes"])
    else:  # 安装了 openpyxl 的环境：只要不报错并识别为 xlsx 即可
        assert isinstance(norm["notes"], list)


def test_sniff_format_matrix(tmp_path) -> None:
    assert N.sniff_format(str(write(tmp_path / "a.sdf", "x\n  RDKit          2D\nV2000\n$$$$\n"))) == "sdf"
    assert N.sniff_format(str(write(tmp_path / "b.mol", "x\n  RDKit          2D\nV2000\n"))) == "mol"
    assert N.sniff_format(str(write(tmp_path / "c.txt", "ATOM      1  N   GLU L   1C\n"))) == "pdb"
    assert N.sniff_format(str(write(tmp_path / "d.txt", "data_ABC\nloop_\n"))) == "cif"
    assert N.sniff_format(str(write(tmp_path / "e.txt", "CCO\nCCN\n"))) == "smi"
    assert N.sniff_format(str(write(tmp_path / "f.txt", "name,smiles\nx,CCO\n"))) == "csv"
    assert N.sniff_format(str(write(tmp_path / "g.bin", b"\x00\x01\x02binary"))) == "unknown"
    assert N.sniff_format("") == "unknown"


def test_normalize_ligand_text_and_record_shape(tmp_path) -> None:
    mols, norm = N.normalize_ligand_text("阿司匹林:%s\n布洛芬 %s\n" % (ASPIRIN, IBUPROFEN))

    assert norm["source_file"] == ""
    assert norm["format"] == "smi"
    assert [m["id"] for m in mols] == ["阿司匹林", "布洛芬"]
    assert [m["smiles"] for m in mols] == [canonical(ASPIRIN), canonical(IBUPROFEN)]
    assert [m["source_index"] for m in mols] == [1, 2]
    assert all(m["source_file"] == "" for m in mols)

    # 每条记录都恰好包含约定的标准键
    expected = {"id", "name", "smiles", "source_file", "source_index", "raw"}
    assert all(set(m) == expected for m in mols)

    # normalization 摘要的 EXACT 键
    expected_norm = {
        "source_file", "format", "records_total", "records_ok", "records_skipped",
        "skipped", "duplicates_removed", "duplicates", "aliases", "encoding",
        "delimiter", "header", "notes",
    }
    assert set(norm) == expected_norm


def test_comment_and_blank_lines_ignored(tmp_path) -> None:
    path = write(tmp_path / "with_comments.csv",
                 "# 这是注释\n// 也是注释\nname,smiles\n\naspirin,%s\n" % ASPIRIN)
    mols, norm = N.normalize_ligand_file(str(path))

    assert norm["format"] == "csv"
    assert [(m["id"], m["smiles"]) for m in mols] == [("aspirin", canonical(ASPIRIN))]
    assert norm["records_total"] == 1


def test_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        N.normalize_ligand_file(str(tmp_path / "does_not_exist_xyz.csv"))
