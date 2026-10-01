# 数据说明

本目录只放**示例数据**与数据来源说明；不包含任何受限数据。

## 示例数据

| 文件 | 内容 | 来源与许可 |
| --- | --- | --- |
| `example/receptor_demo.pdb` | 凝血酶（thrombin）受体结构，仅保留 ATOM/TER/END 记录（2441 行） | RCSB PDB（https://www.rcsb.org，公共领域；使用条款见站点 Usage Policy） |
| `example/ligands_demo.smi` | 6 个常见小分子：苯甲脒、阿司匹林、华法林、乙醇、萘莫司他、甲苯 | 结构信息本身不受版权保护；仅用于演示流程 |

## 用户自备数据（推荐用法）

* 受体：PDB / mmCIF / PDBQT 文件；或 PDB 编号、UniProt accession、基因/蛋白名（联网检索）。
* 配体库：SDF / SMILES / CSV / MOL2 / XLSX；支持自定义列（如 `ID`、`CAS`），这些列会带到结果清单的
  `candidate_id` 与 `remark` 字段。

## 预处理与去重

* 受体：按标准流程去水与杂原子（可通过参数保留），被剔除的残基会出现在结果的备注中；
  需要按目标 pH 处理时调用 pdb2pqr + PROPKA。
* 配体：RDKit 读取并规范化为 canonical SMILES；同一物质的不同写法按身份键（规范 SMILES）去重；
  盐/反离子按最大有机片段处理并留痕，原始 SMILES 始终保留。

## 隐藏评测集

本作品不依赖任何未公开数据，也不使用外部隐藏评测集。
