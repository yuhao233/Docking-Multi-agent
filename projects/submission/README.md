# 多 Agent 协作分子对接筛选工作台（Docking Multi-Agent）

面向虚拟筛选的**分子对接计算与多 Agent 协作编排**系统：输入蛋白质受体结构与候选分子库，
输出可复核的候选清单（对接亲和力、配体效率、实际使用的引擎与版本、备注）。

作品说明（对应《代码提交要求》五「特殊情形说明」）：
本作品**不训练新的深度学习模型**。对接打分由经典物理打分函数（AutoDock Vina / AutoDock4）
完成；大语言模型（LLM）仅用于把自然语言指令转成结构化任务、调度工具与撰写结论文字，
不参与任何打分。因此提交内容为**算法/编排代码 + 一键运行入口 + 示例数据 + 结果示例**。

---

## 1. 目录结构

```
.
├── README.md                 # 本文件：环境、命令、输入输出、模型与第三方工具、复现说明
├── requirements.txt          # Python 依赖与版本
├── screen.py                 # 一键主入口：受体 + 分子库 → results/results.csv
├── data/
│   ├── README.md             # 数据来源、获取方式与许可
│   └── example/              # 小规模示例数据（受体 PDB + 6 个配体）
├── src/docking_agent/        # 核心源代码（计算内核 / 工具层 / Agent 编排 / 服务与前端接口）
├── models/
│   └── MODEL_CARD.md         # 模型与第三方工具说明（版本、调用方式、适用范围与局限）
├── notebooks/
│   └── quickstart.ipynb      # 可执行 Notebook：完整走一遍 设计/输入 → 计算 → 结果清单
├── web/                      # 网页端前端（简易模式 + 高级模式）
├── config/                   # 网页端与 Agent 配置（提示词/工具绑定、设置默认值、受体注册表）
├── assets/                   # 内置资源（受体库、示例分子库、提示词模板）
├── run_web.sh / start.sh     # 网页端启动脚本
├── WEB.md                    # 网页端使用说明（启动方式、接口、配置大模型）
├── results/
│   └── results_example.csv   # 示例运行输出（由 screen.py 真实运行生成）
└── logs/
    ├── run_example.json      # 本次运行的参数、种子、引擎版本、耗时（机器可读）
    └── run_example.log       # 同上的人读日志
```

## 2. 运行环境

| 项 | 要求 |
| --- | --- |
| 操作系统 | Linux（x86-64）；本机实测 Ubuntu 24.04 |
| Python | 3.12（本机 3.12.3） |
| 依赖 | 见 `requirements.txt`（`pip install -r requirements.txt`） |
| GPU | **不要求**。可选：NVIDIA GPU（实测 RTX 5090 / 驱动 590.48.01）用于外部引擎 AutoDock-GPU |
| CPU/内存 | 示例数据 1 分钟内完成；万级分子库建议 ≥ 8 核、≥ 16 GB 内存 |
| 网络 | 仅「在线解析受体 / 按名称检索分子 / 调用 LLM」需要；`screen.py` 全流程离线可跑 |

安装（推荐虚拟环境）：

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## 3. 一键运行（主入口）

```bash
# 示例数据：凝血酶受体 + 6 个配体，指定对接盒
python screen.py \
  --receptor data/example/receptor_demo.pdb \
  --ligands  data/example/ligands_demo.smi \
  --center 31.5,13.74,24.36 --size 22,22,22 \
  --exhaustiveness 1 --n-poses 1 --seed 42 \
  --out results/results.csv --log-dir logs
```

不给位点盒时自动检测结合口袋（P2Rank 可用则优先，否则内置几何法）：

```bash
python screen.py --receptor receptor.pdb --ligands library.sdf --auto-site \
  --exhaustiveness 8 --seed 42 --out results/results.csv
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--receptor` | 受体结构（.pdb/.ent/.cif/.pdbqt） |
| `--ligands` | 候选分子库（.sdf/.smi/.csv/.mol2/.xlsx） |
| `--center` / `--size` | 对接盒中心与边长（Å）；与 `--auto-site` 二选一 |
| `--auto-site` | 用口袋检测自动定盒 |
| `--exhaustiveness` / `--n-poses` | 搜索强度 / 输出位姿数 |
| `--engine` | `vina`（默认）/ `autodock` / `auto` / `external`（设置中登记的外部引擎） |
| `--seed` | 随机种子（默认 42，固定以保证可复现） |
| `--max-ligands` | 限制本次对接分子数（0=全部） |
| `--save-poses` | 保存位姿到 `results/poses/` |
| `--out` / `--log-dir` | 结果清单 / 日志目录 |

## 4. 输入与输出

**输入**：受体结构文件 + 候选分子库（SDF/SMILES/CSV/MOL2/XLSX）；可选给定位点盒坐标。

**输出** `results.csv`（UTF-8，逗号分隔；字段与《候选清单》模板对应）：

| 字段 | 含义 |
| --- | --- |
| `candidate_id` | 候选编号（输入文件里的 ID 字段；没有则用库内序号） |
| `track` | 所属赛道（AI 模型与代码） |
| `name` | 名称 |
| `smiles` | 规范 SMILES 结构 |
| `affinity_kcal_mol` | 对接结合亲和力（kcal/mol，越负结合越强） |
| `ligand_efficiency` | 配体效率（|ΔG| / 重原子数） |
| `engine` / `engine_version` | 实际执行对接的引擎与版本 |
| `seed` / `exhaustiveness` / `n_poses` | 随机种子与搜索参数 |
| `box_center` / `box_size` | 对接盒（Å），记录位点来源 |
| `pose_file` | 位姿文件相对路径（`--save-poses` 时非空） |
| `status` | `ok` / `error` |
| `remark` | 备注：输入文件附加字段、失败原因、配体告警 |

同时写入 `logs/run_example.json|.log`：本次运行的全部参数、随机种子、引擎版本、
Python/平台信息、耗时与成功/失败计数，用于复核与复现。

## 5. 模型与第三方工具（名称、版本、调用方式、来源与许可）

| 类别 | 名称与版本 | 调用方式 | 许可/来源 |
| --- | --- | --- | --- |
| 对接打分（默认） | AutoDock Vina 1.2.7（`vina` Python 包） | `screen.py --engine vina`，进程内调用 | Apache-2.0，https://github.com/ccsb-scripps/AutoDock-Vina |
| 对接打分（可选） | AutoDock4 4.2.6 + AutoGrid4 4.2.7（CPU） | `--engine autodock` | GPL-2.0，https://autodock.scripps.edu |
| 对接打分（可选，GPU） | AutoDock-GPU v1.6（OpenCL/CUDA） | 设置中登记后 `--engine external` | GPL-2.0，https://github.com/ccsb-scripps/AutoDock-GPU |
| 配体准备 | Meeko 0.8.0（+ RDKit 2026.3.6） | 由 `screen.py` 内部调用（SMILES→3D→PDBQT） | Apache-2.0 / BSD-3-Clause |
| 口袋检测（可选） | P2Rank 2.5.1 | `--auto-site` 时优先使用，缺失则回退内置几何法 | MIT，https://github.com/rdk/p2rank |
| 受体质子化（可选） | pdb2pqr + PROPKA | 需要按目标 pH 准备受体时调用（缺失则跳过并提示） | BSD-3-Clause |
| 配体 pKa（可选） | Dimorphite-DL 2.0.2 | 需要按目标 pH 分配配体质子化态时调用 | Apache-2.0 |
| 大语言模型（网页端编排，**不参与打分**） | 由运行者在 `.env` 配置（示例：DeepSeek `deepseek-flash`） | 通过 OpenAI 兼容 HTTP 接口调用 | 由模型提供方许可决定；本作品只调用，不分发权重 |

- 本作品**不包含自训练模型权重**；`models/` 目录仅提供模型说明（Model Card）与外部引擎的获取方式。
- 第三方工具均以**外部依赖**形式调用，未修改其源码；版本与调用参数记录在 `logs/` 与结果清单中。

## 6. 数据来源与许可

| 数据 | 来源 | 说明/许可 |
| --- | --- | --- |
| 示例受体 `data/example/receptor_demo.pdb` | RCSB PDB（凝血酶结构，去除头部的结构记录） | 公共领域（PDB 数据可自由使用，见 https://www.rcsb.org/pages/usage-policy） |
| 示例配体 `data/example/ligands_demo.smi` | 常见小分子（苯甲脒、阿司匹林、华法林、乙醇、萘莫司他、甲苯） | 结构信息本身不受版权保护，仅作流程演示 |
| 可选在线检索 | RCSB PDB / UniProt / PubChem | 各站点公开数据，使用时遵守其使用条款 |
| 隐藏评测数据 | 无（本作品不依赖任何未公开数据） | — |

数据预处理：受体仅保留蛋白质 ATOM 记录（水与杂原子按参数决定是否保留，见下）；配体由
RDKit 读取并规范化为 canonical SMILES，重复物质按身份键去重。详见 `src/docking_agent/core/`。

## 7. 可复现性

- **随机种子**：`--seed`（默认 42）固定 Vina 采样；同一分子单独运行与批量运行的分数一致
  （回归测试 `tests/test_agent_context.py::test_scores_are_independent_of_batch_and_workers`）。
- **参数留痕**：`logs/run_example.json` 记录引擎、版本、搜索强度、盒子坐标、Python/平台与耗时。
- **计算口径**：受体与配体使用同一目标 pH 的质子化态（默认 7.4）；同一批次内参数一致。
- **预期资源与耗时**（实测，Linux/32 核）：示例数据（6 分子，exhaustiveness=1）约 5 秒；
  1000 分子库、exhaustiveness=8、20³ Å 盒约 20–40 分钟（CPU Vina，多进程）。
- **离线复现**：`python screen.py ...` 不需要网络与大模型。

## 8. 已知限制

- 对接分数是**近似结合自由能**（经验打分函数 + 启发式采样），用于同一受体/同一参数下的
  **相对排序**，不可跨受体或跨参数直接比较，也不等同于实验活性。
- 受体准备按标准流程处理：默认剔除水与杂原子，并如实报告被剔除的残基；金属酶/辅因子体系
  需通过参数显式保留（`keep_hetatm`），否则结论仅代表“去辅因子”条件。
- 含 7 元及以上环的配体按**刚性环**准备（不切环、不采样环构象），以兼容经典 AutoDock 格点打分；
  环构象多样性因此未被采样。
- 未做共识打分与位姿重打分；不确定性以“失败/跳过分子计数 + 参数留痕”形式披露。
- LLM 仅用于交互与文字撰写，其输出经结构化校验；本作品的核心结论不依赖 LLM 的数值判断。

## 8.1 网页端（交互界面）

除命令行入口外，作品提供网页端（同一套计算内核）：

```bash
bash run_web.sh              # 简易模式 http://127.0.0.1:5000/
bash run_web.sh --port 8000  # 高级模式 http://127.0.0.1:<端口>/advanced
```

- 简易模式：上传受体与分子库、表单参数或自然语言发起筛选、查看结果与下载产物。
- 高级模式：暴露全部参数（引擎、搜索强度、位点、质子化策略、最大分子数等）、运行记录与
  报告目录，每次运行的输入/参数/结果/日志可在运行记录中追溯。
- **不配置大模型也能使用**：表单参数、运行记录、结果与报告不依赖大模型；只有自然语言对话与
  结论撰写需要按 `WEB.md` §3 配置。大模型不参与打分与排序。
- 网页端与命令行共用 `src/docking_agent/core/`，因此网页端结果可用 `screen.py` 逐条复核。

详见 `WEB.md`；接口契约见仓库 `docs/api.md`。

## 9. 本项目的创新与贡献

1. **两套界面（简易/高级）+ 标准 Agent Protocol 面**共用同一执行链路与同一份产物：自然语言
   任务与表单参数不会互相覆盖，运行记录可离线复核。
2. **计算对象的确定性护栏**：受体未指定、位点未确定、阳性对照待用户决定时，工具**拒绝开跑**
   并给出下一步；引擎不可用时不静默回退（结果行记录真实引擎与版本）。
3. **可复现的批处理内核**：网格图按“受体+盒子”生成一次复用；批次/进程数不影响单分子分数；
   两阶段漏斗（大库先粗筛全库、再精算头部）在计算层内完成并留痕。
4. **外部引擎执行适配**：登记后可由 AutoDock-GPU 执行（自动生成格点图、解析 DLG 并转换为
   PDBQT 位姿），结果与内置引擎同口径，失败分子如实上报。
5. **报告与清单的可追溯性**：参数决策链、受体/配体质子化溯源、失败原因、引擎版本、随机种子
   全部写入产物；报告固定章节结构，文字结论由 Agent 撰写。
