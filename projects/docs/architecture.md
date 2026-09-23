# 开发架构与开发要点（AI 开发手册）

> **读者**：后续接手本项目的 AI 与人类开发者。
> **用法**：动手改代码前先读 §2（架构）、§5（不变量）、§10（开发规范）；加功能看 §11（扩展指南）；
> 改完必须走 §12（测试与门禁）；上线/排障看 §13（陷阱）与 §14（文档地图）。
> **文档分工**（本文件是**开发者视角的总图**，只做索引与约束，不重复其它文档的细节）：
> `../../README.md`（仓库门面）· `../../docs/技术文档.md`（读者向概览）· `../README.md`（工程主文档：
> 安装 / 配置 / 接口速查 / 门禁验收）· `api.md`（接口与各子系统契约细节）· `技术报告.md`（评审用完整
> 技术报告，含实测数据）· `../CHANGELOG.md`（历史修复日志）。
>
> **本文档的来历**：2026-09-14 对全仓做了一次独立审计（架构梳理 + 死代码/重复实现 + 代码规范 +
> 文档一致性 + 目标差距），本文档是那次审计的沉淀：§3 是模块地图、§5 是不变量、§10 是规范与门禁、
> §15 是**审计得到的差距清单与推进顺序**。审计同时清理了死代码与重复实现（见 `../CHANGELOG.md` 第 25/26 条）。

---

## 1. 项目目标与边界

### 1.1 目标（产品定义）

> 产品定义与读者向叙述见 `技术报告.md` §1 与 `../README.md` §2；本节只保留**开发者需要的
> 能力 → 落点映射**（改代码时用来定位「这条能力在哪一层实现」）。

拆成可验收的六条能力：

| # | 能力 | 验收标准（可观测） | 现状落点 |
|---|---|---|---|
| G1 | **自动准备** | 用户只给 PDB/小分子文件即可开跑；盒子由工具确定而非臆造 | `core/receptors.py`、`core/ligands.py`、`core/pockets.py` |
| G2 | **自动对接** | 全部候选分子都有真实引擎返回的能量；大库能在有限算力内跑完 | `core/docking.py`、`agents/coordinator` 漏斗纪律 |
| G3 | **自动评估** | 物化性质/类药性/结合模式与对照比较，全部来自工具真实计算 | `core/chemistry.py`、`tools/{properties,binding}.py` |
| G4 | **自动异常处理** | 单分子失败不拖垮整批；失败有原因；特殊体系有事实披露与补救建议 | `runtime/errors.py`、`core/docking.py` 的 note/降级路径 |
| G5 | **总结与报告** | 固定 9 章报告（第 3 章含推荐化合物排行） + 机器可读产物（CSV/JSON/图/位姿），数值可回溯到工具输出 | `reporting/`、`agents/persistence.py`、`runs.py` |
| G6 | **主管协调** | 受理判定 run/ask/reject；分发、并行、漏斗、分片由主管决定；流程不中断 | `intake.py`、`agents/coordinator.py`、`agents/dispatch.py` |

### 1.2 明确的非目标（避免过度工程）

- **不做**分子生成/骨架跃迁/QSAR 建模：本系统是「筛选与评估」而非「设计」。
- **不做**分布式集群调度：目标是**单机多进程**把 32 核吃满（见 §8），不引入 Redis/Celery/K8s。
- **不做**自研对接引擎/打分函数：只做 AutoDock Vina / AutoDock4 的**编排与取证**。
- **不做**多用户 SaaS：默认只听 `127.0.0.1`，无鉴权（见 `../README.md` §15）。
- **不引入**向量库/RAG：任务输入是结构化的分子与受体，不需要语义检索。

> 非目标的读者向表述与理由见 `技术报告.md` §1.2 与 `../../docs/技术文档.md` §1.2（本节只保留
> 改代码时必须遵守的边界）。

### 1.3 Agent 面的定位（ADR-0001 摘要）

系统里有**两个面**，各有明确服务对象（完整决策与守卫见
[`docs/adr/0001-agent-surface.md`](adr/0001-agent-surface.md)）：

| 面 | 位置 | 服务对象 |
| --- | --- | --- |
| **产品面**（`/api/*` + `/threads` 等兼容子集） | `src/docking_agent/api/`（单进程 FastAPI） | 本机网页、同机脚本/集成方 |
| **平台面**（LangGraph Platform） | `langgraph-deploy/`（独立 venv/端口） | Studio 调试、LangGraph SDK、未来 durable 运行 |

两条硬规则：**Agent 编排能力只写在 `agents/`、`tools/`（两个面共用，禁止复制）**；
**协议形状（SSE 帧、run_id 双语义、取消、幂等）只实现一份**。产品面的标准子集**冻结**，
新增能力先判定归属（Agent 编排 → 图/工具；产品形态 → `/api/*`），需要新增协议端点时先改 ADR。

### 1.4 两条贯穿全局的硬约束（改代码时最容易违反）

1. **真实性**：任何数值都必须来自工具真实返回；工具失败要**如实报错**，禁止编造、禁止「看起来跑通了」的假成功。
2. **不静默**：任何被丢弃/改写/降级的东西（金属辅因子、盐的反离子、缺模板的残基、失败的分子）
   都必须在结果里**逐条可见**，并给出补救路径。工具报事实，化学/科学判断交给 Agent 或用户。

---

## 2. 架构总览

### 2.1 分层

```
┌── 入口层 ───────────────────────────────────────────────────────────────┐
│ cli.py / __main__.py           命令行：--check、agent、runs、serve、receptors │
│ api/app.py (FastAPI)           HTTP + SSE；唯一对外接口面                  │
│ api/schemas.py                 请求体校验（pydantic）                      │
├── 受理层 ───────────────────────────────────────────────────────────────┤
│ intake.py                      用户指令+表单 → 结构化 task_spec（run/ask/reject）│
├── 编排层 ───────────────────────────────────────────────────────────────┤
│ agents/coordinator.py          主管 Agent（LangGraph create_agent）        │
│ agents/dispatch.py              发给子 Agent 的「分发工具」（子 Agent 调度面）│
├── 角色层（4 个子 Agent，各自独立模型实例与 checkpointer）─────────────────┤
│ agents/workers.py              property / pocket / docking / binding       │
│ agents/prompts.py              子 Agent 系统提示词                        │
├── 工具层（@tool，模型可见的唯一动作面）─────────────────────────────────┤
│ tools/docking.py tools/properties.py tools/binding.py                     │
│ tools/pockets.py tools/report.py tools/online.py                          │
├── 核心计算层（纯函数/纯算法，不依赖 LLM）───────────────────────────────┤
│ core/docking.py  receptors.py  ligands.py  pockets.py  chemistry.py  ranking.py│
├── 状态与持久化 ─────────────────────────────────────────────────────────┤
│ runtime/blackboard.py           运行内共享黑板（跨 Agent 横向协作，进程内）    │
│ runtime/tool_io.py              工具原始返回落盘 + 给模型的摘要视图            │
│ agents/persistence.py          把工具产物合并成最终结果（唯一的落盘入口）      │
│ runs.py                        运行目录、产物清单、分页/聚合、zip            │
├── 报告层 ───────────────────────────────────────────────────────────────┤
│ reporting/report.py（Markdown 模板）recommend.py（推荐综合分）charts.py（matplotlib）│
│ reporting/tables.py（CSV）artifacts.py（产物登记）pdf.py（PDF 渲染）            │
├── 运行时支撑 ───────────────────────────────────────────────────────────┤
│ runtime/llm.py 按角色模型实例与优先级   streaming.py SSE 封装与节流         │
│ runtime/errors.py 错误分类     checkpoints.py 会话持久化   context.py 请求上下文│
│ settings.py 界面可编辑设置     config.py 环境变量     paths.py 路径  cancellation.py 取消│
└─────────────────────────────────────────────────────────────────────────┘
```

**依赖方向单向向下**：`api/cli → intake/coordinator → tools → core`。
`core/*` **绝不** import `tools/*`、`agents/*`、`api/*`（唯一的例外是 `core/receptors.py` 在函数内
延迟导入 `core/pockets.py` 以避免循环依赖）。新增代码若违反这条，先重构分层。

### 2.2 一次运行的端到端数据流

```
用户/前端
  │  POST /threads/{tid}/runs/stream（标准 Agent Protocol）
  ▼
api/app.py  建 Run（runs.py：运行目录 + run.json）
  │
  ├─ intake.build_task_spec(req)            ← 确定性规则（零模型）
  │     └ 需要时 intake.refine_task_spec()  ← 仅「对话模式 + 自然语言」调用一次 LLM
  │         产出 task_spec{decision,ligands,receptor,site,positive_control,...}
  │         decision=ask/reject → 直接回复，不调用任何工具（省算力、防胡说）
  ▼
coordinator（主管 Agent，LangGraph）
  │  提示词里注入 task_spec 的只读摘要（intake.render_agent_message）
  │  主管在同一轮里可并发发出多个分发工具调用 → ToolNode 并发执行 → 实测并行
  ├─ agents/dispatch.py: import_molecule_library / run_property_assessment /
  │                     run_pocket_analysis / run_docking / run_binding_mode_analysis
  │       每个分发工具 = 调 invoke_worker(子 Agent) → 子 Agent 调自己的 @tool
  ▼
@tool（tools/*.py）
  │  · 大结果：tool_io.record() 全量落盘 + 只回给模型 summary/top（见 §8.4）
  │  · 横向协作：写 runtime/blackboard.py（分子/性质/对接/口袋/位点）
  ▼
core/*（真实计算）
  │  RDKit 构象、meeko PDBQT、Vina 对接、P2Rank/几何法口袋、排序
  ▼
agents/persistence.py（唯一落盘入口，被报告工具/主管收尾触发）
  │  以**工具原始输出**为准合并（_PROVENANCE_KEYS 回填模型漏掉的字段）
  ▼
runs.py 产物：molecules/properties/docking/binding/pockets/result.json + ranking.csv
  │           + report.md（固定 7 章）+ charts/*.png + poses/*
  ▼
SSE（runtime/streaming.py，每个事件带 ts）→ 前端 web/app.js 渲染卡片/表格/编排时间轴
```

### 2.3 两条并存的执行路径

| 路径 | 入口 | 是否用 LLM | 用途 | 注意 |
|---|---|---|---|---|
| **Agent 模式** | `/api/agent/stream`、`-m agent` | 是（主管 + 子 Agent） | 真实产品路径：能理解自然语言、自主决策、可报告过程 | 提示词是**产品行为的一部分**，改提示词等于改行为，必须测试 |

两者产出同一套产物与同一个报告模板（`reporting/report.py`），仅「结论与建议」一节来源不同
（Agent 文字 vs 规则生成）。

---

## 3. 模块地图（改动前先看这张表）

### 3.1 顶层（`src/docking_agent/`，~2966 行）

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `cli.py` | 命令行入口：`--check` / `agent` / `runs` / `serve` / `receptors` | `main()` |
| `intake.py` | **任务受理层**：指令+表单 → `task_spec`；确定性抽取分子；LLM 只补白名单字段 | `build_task_spec`、`refine_task_spec`、`render_agent_message`、`_finalize`、`_resolvable_ligands` |
| `runs.py` | 运行目录/产物清单/`run.json`/分页排序/聚合/zip/**保留策略（`prune`）** | `RunStore`、`Run`、`run_artifact_path` |
| `settings.py` | 界面设置层（`config/local_settings.json`）：字段规格、优先级、掩码、运行时注入 | `SPECS`、`normalize_updates`、`apply_runtime_env`、`runtime_source` |
| `config.py` | 运行时环境 bootstrap；读取原语在 `envs.py`（`env`/`env_int`/`env_bool`/`env_float`） | `env_int`、`ensure_runtime_env`、`load_env` |
| `envs.py` | 环境变量读取的唯一底层实现（从 config 拆出，避免 `config ↔ settings` 循环） | `env`、`env_bool`、`env_int`、`env_float`、`load_env` |
| `run_context.py` | 「当前运行」只读获取点（`runs.py` 注册 provider，`core/` 不再 import 持久化层） | `set_run_provider`、`active_run_or_none`、`run_request_or_empty` |
| `paths.py` | 路径解析（项目根、var/、cache、uploads、runs） | `project_root`、`workspace_dir`、`cache_dir`、`uploads_dir` |
| `cancellation.py` | 协作式取消（线程 Event + 跨进程） | `cancel_flag`、`CancelledRun`、`is_cancelled` |
| `logging_setup.py` | 日志初始化与格式 | `setup_logging` |

### 3.2 核心计算（`core/`，~2951 行）—— 最需要小心的部分

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `docking.py` | 引擎封装与批量调度：`DockingSession`（一次建图多次复用）、`dock_batch`（串/并行、LPT 调度、取消、化学体检）、`dock_library`（受体×配体位点/漏斗）、`plan_concurrency`；`engine ∈ auto/vina/autodock/external`，`external` 走 `external_run` 的执行适配（未登记或未就绪**直接报错**，不回退内置引擎） | `DockingSession.dock`、`dock_batch`、`dock_library`、`plan_concurrency` |
| `external_tools.py` + `external_run.py` | **外部引擎**（用户自行安装的 GPU/CLI）：前者识别类型（unidock / vina-gpu / autodock-gpu / vina-cpu）、探测版本与 GPU 可见性、固定各引擎 argv 形状（`GPU_DEVICE` 0 基 → AutoDock-GPU `--devnum` 1 基）；后者真的把它跑起来 —— `autogrid4` 生成格点图（会话内缓存）、调用二进制、解析 DLG/位姿，产出与内置引擎同形的结果行。`unidock`/`vina-gpu` 只登记不执行（不猜参数） | `collect`、`require_engine`、`build_argv`、`build_grid_maps`、`dock_ligand_external` |
| `normalize.py` + `normalize_io.py` | **统一输入归一化**（唯一入口，见 §9.0）：内容嗅探优先于扩展名、gzip/zip、编码/分隔符/中英文表头自动判定、逐行容错、按 canonical SMILES 去重并保留全部别名、InChI/InChIKey、标准记录 `{id,name,smiles,source_file,source_index,raw}` | `sniff_format`、`normalize_ligand_file`、`normalize_ligand_text`、`normalize_receptor_source`、`record_input_normalization` |
| `receptors.py` | 受体准备：去水/杂原子溯源、`keep_hetatm`、altloc 重试、模板缺失逐个剔除、内容哈希缓存、共晶配体识别、位点盒推断、PDBQT spec | `prepare_user_receptor`、`resolve_receptor_specs`、`read_receptor_file`、`_pdbqt_spec`、`guess_cocrystal_ligand` |
| `ligands.py` | 配体读取与准备：SDF/SMI/CSV/MOL2、`名称:SMILES` 与自由文本抽取、3D 构象(ETKDGv3+MMFF)+meeko、**化学体检** `describe_ligand` | `read_molecule_file`、`parse_smiles_text`、`extract_smiles`、`smiles_to_pdbqt`、`describe_ligand` |
| `pockets.py` | 口袋与盒子：P2Rank 适配、内置几何法（fpocket 思路）、`cocrystal_ligand`、`select_site` 来源优先级与验证、**库级下限 `library_span_bound`（≥8 个样本走进程池，实测 5× 加速）**、`apply_box_floor`、缓存 | `select_site`、`detect_geometric`、`run_p2rank`、`cocrystal_ligand`、`engine_settings` |
| `params.py` | **对接参数自动规划**（运行级/阶段级）：库柔性 2D 统计 + 内容哈希缓存、柔性与盒体积系数、两阶段漏斗、pilot 预算护栏与精度优先降级 | `library_stats`、`plan_docking_params` |
| `chemistry.py` | 性质/指纹/药效团/相似度（RDKit 真实计算） | `compute_properties`、`morgan_fingerprint`、`maccs_fingerprint`、`pharmacophore_hits`、`combined_similarity` |
| `ranking.py` | 合并与排序（性质 ∪ 对接），综合评分 | `merge_and_rank` |
| `files.py` | 文件获取（URL/本地）、slug、扩展名判定 | `_fetch_file`、`slug` |

### 3.3 Agent 层（`agents/`，~1208 行）

| 文件 | 职责 |
|---|---|
| `coordinator.py` | 主管 Agent 构建：绑定系统提示词、**12 个**分发/在线工具、滑动窗口记忆（`MAX_MESSAGES=40`）、图名 `coordinator` |
| `reports.py` | 4 个子 Agent 的**结构化输出契约**（pydantic，`extra=allow`）；`workers` 用 `response_format=ToolStrategy(<Role>Report)` 强制模型调用结构化工具，取代「提示词要求吐 JSON + 服务端重试」 |
| `workers.py` | 4 个子 Agent 构建：**每角色一个 LLM 实例 + 独立 `InMemorySaver`**、图名 = 角色名；`invoke_worker` 每次用**随机 thread** → 子 Agent 是**无状态执行器**（跨步骤信息只走运行产物与黑板，不走子 Agent 记忆） |
| `prompts.py` | 系统提示词（协调兜底 `COORDINATOR_SP` + 属性/口袋/对接/结合模式）。子 Agent 提示词 = **基础提示词 + 该角色自己的 `DATA_HANDOFF_*` 数据交接纪律 + `PARAMS_CONTRACT`** 显式拼接；交接块只点名该角色真的拥有且真接受文件参数的工具（口袋 Agent 不接收文件参数 → 不拼该块） |
| `prompt_blocks.py` | **条件纪律段**：`sp` 里用 `<!-- block:KEY -->` 标记出「只在特定体系/规模下才用得上」的段落（上传处理 / 受体纪律 3c / 两阶段漏斗 / 特殊体系），协调 Agent 的 `dynamic_prompt` 中间件按本次运行事实**抽掉**不相关的段（省每次模型调用的固定开销），并把注入清单写进 `run.data["prompt_blocks"]`（随 `result.json` 落盘）。安全方向写死：**没有标记 → 全文**、**事实未知 → 注入**、**组装异常 → 全文** |
| `blackboard.py` | 运行内共享黑板：`set_receptor/set_site/set_pockets/set_molecules/set_properties/set_docking/set_binding`，快照上限 50，`stats()` 供界面展示协作痕迹。**P2-c 起支持 LangGraph `store` 后端**（`(\\"blackboard\\", run_id)` 命名空间、按字段写、构造时 hydrate）：父图与 4 个子 Agent 共享同一 store，跨图/跨进程可见；CLI/单测仍可用进程内对象（`store=None`）。读取优先级 `runtime.context.blackboard → runtime.store → ContextVar` |
| `tool_io.py` | 工具原始返回落盘（`*_tool.json`）+ 模型可见摘要（`summary_limit()`=`AGENT_TOOL_TOP_N`）+ 漏斗参数 |
| `threads.py` | **工具调用序列自愈**：`dangling_tool_calls()` / `orphan_tool_call_ids()` / `pairing_updates()` / `repair_thread_state()` / `ToolCallPairingMiddleware` —— 中断（停止/失败/限额）会在 checkpoint 留下「模型发了 tool_calls 但没有 ToolMessage」的非法历史，下一轮会被模型端以 400 拒绝；修法是**同 id 原地改写那条 AIMessage**（去掉无回执的调用 + 追加事实说明），孤儿回执直接移除。**不能往中间插消息**：`add_messages` 只把新 id 追加到末尾，插进去的占位回执会落到最新一条人类消息之后，序列依旧非法（实测踩坑）。中间件挂 `before_model`，协调 Agent 与 4 个子 Agent（固定角色线程，取消后最容易留下悬空）共用 |
| `limits.py` | **步数预算**：递归上限的自动放宽阶梯（`base_limit`/`escalate`/`limit_ceiling`）、收尾提示（`WRAP_UP_NOTE`，以 SystemMessage 注入，不进用户可见对话）与统一的 `limit` 事件。产品准则：跑满步数是执行细节，自动放宽并继续（120 → 240 → 480），到顶让主管 Agent 用已有结果收尾，**绝不把 `GraphRecursionError` 抛给用户**。协调 Agent（`runtime/streaming.py`）、子 Agent（`agents/workers.py`）与 legacy 端点（`api/routers/legacy.py`）共用 |
| `persistence.py` | **唯一的结果合并/落盘入口**：`_merge_docking`（精度优先）、`_merge_pockets`、`_PROVENANCE_KEYS` 回填、写 `result.json`/`report.md`；**工具产物文件（只存最近一次调用）与消息历史（全部调用）必须合并**，否则分批对接/蛋白质库的早先受体块会静默丢失（`data_sources` 会标明 `tool_file+tool_message`） |

### 3.4 工具层（`tools/`，~1840 行）

模型能调用的动作**只有**这些函数；每个 `@tool` 的 docstring 就是给模型的说明书，
因此 **docstring 是接口契约的一部分**（改行为必须同步改 docstring）。

| 文件 | 工具 |
|---|---|
| `dispatch.py` | 主管用：`import_molecule_library`、`run_property_assessment`、`run_pocket_analysis`、`run_docking`、`run_binding_mode_analysis`；`list_known_receptors` 为**内部/诊断**工具，**无任何 Agent 绑定** |
| `docking.py` | 子 Agent 用：`molecular_docking`（`available_receptors` 已删除） |
| `pockets.py` | 子 Agent 用：`predict_binding_pockets`、`compare_pocket_with_experiment`、`set_docking_site`、`list_pocket_engines` |
| `properties.py` | `normalize_molecule_library`、`molecular_property_assessment` |
| `binding.py` | `binding_mode_analysis`、`positive_control_similarity`、`check_binding_consistency` |
| `report.py` | `generate_screening_report`（触发 persistence 落盘 + 报告产物） |
| `core/receptor_ph.py` | **受体质子化与配体同 pH**：pdb2pqr(PROPKA) → PQR → meeko PDBQT；逐残基留痕（HIS 状态 / 可滴定残基质子化计数 / 边界残基 / 剔除的不完整残基）；工具缺失或需保留有机辅因子时回退并如实记录 |
| `core/protonation.py` | 运行级质子化态：`neutralize` / `ph` / `keep`，逐分子溯源（策略解析优先级：显式 → 运行请求 → 环境变量 → 默认）；`ph` 优先调用 `core/ligand_pka.py` 的专业引擎 |
| `core/ligand_pka.py` | **配体 pKa 引擎层**：`auto`（默认，装了 Dimorphite-DL 就用）/ `dimorphite` / `rules`；窗口内多微观态按「\|净电荷\| 最小 → 带电原子最少 → 字典序」取一个形式，全部候选与规则写进溯源；不可用时回退内置规则表并给出 `--no-deps` 安装提示（其元数据把 rdkit 钉在 <2026） |
| `recommend.py` | 主管用：`recommend_compounds`（真实综合分排行）、`submit_recommendations`（逐分子推荐理由，按在榜名单核对） |
| `pose.py` | 主管用：`analyze_pose_pocket`（口袋说明 + 每个推荐分子最优位姿的逐残基相互作用：氢键/盐桥/疏水/π/金属配位） |
| `online.py` | `fetch_protein_structure`（RCSB/UniProt）、`fetch_molecule_record`（PubChem 等） |

### 3.5 运行时与 API

| 文件 | 职责 | 要点 |
|---|---|---|
| `runtime/llm.py` | 按角色的模型解析/构建；`ROLES=("intake","coordinator","property","pocket","docking","binding")`；登记表供运行记录溯源；`reset_registry_counters()` | 优先级见 §7 |
| `runtime/streaming.py` | SSE 事件封装（统一附加 `ts`）、批量节流 | `sse_event`、节流参数 `SSE_*` |
| `runtime/payload.py` | 大 payload 截断/摘要工具 | 与 `tool_io` 配合 |
| `runtime/errors.py` | 错误分类（可重试/不可重试/用户可见措辞） | `ErrorClassifier` |
| `runtime/checkpoints.py` | checkpointer（内存/SQLite） | `CHECKPOINT_BACKEND` |
| `runtime/context.py` | 请求上下文（run、取消标志） | `new_context`、`request_context` |
| `runtime/run_facts.py` | 运行级**小事实**记账（`run.data["prompt_facts"]`）：受体是否掉过杂原子、配体是否特殊化学、是否看过对接结果 → 供条件纪律段判断。**只记布尔**、负面信号粘住（`STICKY_TRUE`）；工具层与 Agent 层都能依赖（分层契约要求它落在 `runtime/`） | `docking_facts()`（纯函数）、`note/read` |
| `api/app.py` | 全部 HTTP/SSE 接口（实测 33 条路由，其中 `/api/*` 23 条）与上传、设置、运行查询、产物下载 | 见 `docs/api.md` |
| `api/schemas.py` | 请求体模型 | `AgentRequest`、`PipelineRequest` |

### 3.6 前端（`web/`，~8.6k 行）

| 文件 | 职责 | 约束 |
|---|---|---|
| `index.html` | 结构：导航栏（对话/手动/运行记录/设置）+ 工作台 + 日志 + 结果卡片 + 编排图 | DOM id 被 `scripts/snapshot_ids.py` 快照看护，**删 id 会被门禁拦下** |
| `app.js` | 全部交互：SSE 消费、卡片渲染、设置页 schema 驱动、编排图与**实测时间轴** | 无构建步骤（原生 JS）；改完跑 `node --check` + `scripts/ui_e2e.js` |
| `styles.css` | 设计令牌（颜色/间距/字号）+ 布局 | 颜色必须走令牌，禁止滥用 `!important`（`check_web.py` 检查） |

---

## 4. Agent 角色与契约

### 4.1 角色清单（6 个模型实例）

| 角色 | 作用 | 拥有的工具 | 提示词来源 |
|---|---|---|---|
| `intake` | 任务受理：把自然语言变成结构化规约；**不能给参数、不能编造分子** | 无（纯 LLM 补白名单字段） | `intake.INTAKE_SYSTEM_PROMPT` |
| `coordinator` | 主管：计划、分发、串并行、漏斗、汇总、推荐排行与理由、口袋说明与姿态结合分析、出报告 | 12 个（见 §3.4 dispatch/online/report/recommend/pose） | `config/agent_llm_config.json` 的 `sp` |
| `property` | 分子属性评估 | `normalize_molecule_library`、`molecular_property_assessment` | `agents/prompts.py` |
| `pocket` | 口袋分析：预测 → 与实验位点比对 → 提交盒子 | `predict_binding_pockets`、`compare_pocket_with_experiment`、`set_docking_site`、`list_pocket_engines` | 同上 |
| `docking` | 对接执行 | `molecular_docking`、`fetch_protein_structure` | 同上 |
| `binding` | 结合模式与对照比较 | `binding_mode_analysis`、`positive_control_similarity`、`check_binding_consistency` | 同上 |

**为什么 intake 与 coordinator 分开**（历史决策，别退回）：早期把「受理」和「执行」写在一个提示词里，
「含糊就先问用户」与「分子非空就必须跑完」直接冲突，模型两头不讨好。现在 `decision` 由受理层显式给出，
编排层只执行，两者互不污染。

### 4.2 分发链路（横向协作怎么发生）

```
coordinator ──(dispatch.@tool)──▶ invoke_worker(子 Agent, thread_id=角色名)
                                      │
                                      ├─ 子 Agent 调自己的 @tool（真实计算）
                                      ├─ @tool 写 blackboard（横向共享）
                                      └─ 返回 JSON 原文（经 _invoke_checked 校验关键字段）
```

- **同一轮多个分发调用 → LangGraph `ToolNode` 并发执行**：这是界面「实测并行」的来源（§15 / `docs/api.md` §15）。
- **子 Agent 无状态**：`invoke_worker` 用独立 `thread_id`，避免多次运行共享历史（历史 bug：固定 thread_id 串话）。
- **分发边界校验**：子 Agent 返回必须能解析成 JSON 且含关键字段，否则带纠正提示重试一次，
  两次失败返回 `{"status":"agent_output_invalid"}`，**不把垃圾文本喂给主管**。
- **横向协作靠黑板而不是靠主管转述**：口袋 Agent `set_docking_site` → 对接工具自动使用；
  对接结果 → 结合模式 Agent 交叉核验。主管不需要（也不应该）把明细搬来搬去。

### 4.3 主管的纪律（写在 `config/agent_llm_config.json` 的 `sp` 里，共 12 条）

要点：`decision=run` 必须完整执行不得中途提问；阳性对照是可选项（只有用户给了才做）；
位点盒必须由工具确定；大库走两阶段漏斗；大库明细不进上下文；**特殊体系必须判断并如实披露**。
**改这 12 条 = 改产品行为**，必须同时更新 `tests/test_intake.py` / `smoke_test.py` 的期望。

---

## 4b. Agent 间数据交接（v0.19：**文件优先，黑板只放小状态**）

上限 1 万条分子 / 对接明细时，"把数据塞进消息或上下文"物理上不可行。交接分两类：

| 通道 | 承载什么 | 机制 |
| --- | --- | --- |
| **运行产物文件（首选）** | 大表：分子清单、性质明细、对接明细、口袋、结合模式 | `tool_io.record(name, payload)` 写 `<name>_tool.json` 并把**绝对路径**记进 `run.data["tool_files"]`；`tool_io.artifact_path(name)` / `artifact_refs()` 把它交给模型与子 Agent |
| **共享黑板** | 小状态：受体、位点盒、阳性对照、计数、跨 Agent 标记 | `runtime/blackboard.py`（ContextVar 注入，线程安全） |

**工具侧的"文件参数"契约**（都可留空，留空才回退黑板）：

| 工具 | 文件参数 | 说明 |
| --- | --- | --- |
| `molecular_docking` / `run_docking` | `molecule_file` / `molecules_file` | 用户上传文件**或运行产物 JSON**（`molecules_tool.json`）都能读 |
| `molecular_property_assessment` / `normalize_molecule_library` | `molecules_file` | 同上游产物路径；读取走统一归一化层 |
| `binding_mode_analysis` | `molecules_file` | 同上 |
| `check_binding_consistency` | `docking_file` | 直接读 `docking_tool.json` 的对接行，不必经黑板 |

**谁解析到数据谁就发布**（真实缺陷教训）：协调 Agent 被允许跳过 `import_molecule_library`
直接把文件交给 `run_docking`；若只有对接工具知道分子，属性评估就会读到"黑板上无分子"并
返回 `status=ok` 的空结果。因此 `molecular_docking` 解析出分子后**必须**写黑板；
属性侧还有第二道兜底：黑板为空时退回**本次运行请求里的 `molecule_file`**。

## 5. 关键抽象与不变量（改代码前必须理解）

| 抽象 | 位置 | 不变量 |
|---|---|---|
| **task_spec** | `intake.build_task_spec` → `run.data["task_spec"]` | 只读契约：编排层不得修改；`decision ∈ {run, ask, reject}`；`missing` 不构成阻断 |
| **blackboard** | `runtime/blackboard.py` | 进程内、单次运行隔离；`snapshot()` 上限 50；`set_receptor` 不覆盖已固定的位点（`_site_pinned`） |
| **tool_io** | `runtime/tool_io.py` | 工具原始返回**全量落盘**（`*_tool.json`），给模型的只是**视图**（`summary_limit`）；`AGENT_TOOL_TOP_N` 是视图大小，**不是输入上限** |
| **runs 产物** | `runs.py` | 任何数值都能在产物里找到出处；`result.json` 是落盘白名单；产物清单随运行增长要限流 |
| **persistence** | `agents/persistence.py` | **唯一的合并/落盘入口**；同一工具多次调用按 `(name, smiles)` 去重、**精度优先**；`_PROVENANCE_KEYS` 用工具原始输出回填模型漏掉的字段 |
| **绝对/相对路径** | `runs.py`、`reporting/artifacts.py` | 产物登记一律相对运行目录；对外 URL 由 `ARTIFACT_BASE_URL` 拼 |
| **SSE 事件** | `runtime/streaming.py` | 每个事件带服务端 `ts`；类型：`start/progress/stage/molecules/token/tool_call/tool_result/update/final/done/error/cancelled`；`stage ∈ import/properties/pocket/params/docking/binding/report` |
| **缓存** | `core/receptors.py`、`core/pockets.py` | **按内容哈希**判定（不是时间戳）；缓存键必须包含影响结果的输入（如 `keep_hetatm`） |
| **取消** | `cancellation.py` | 协作式：对接循环里检查 `cancel_event`；进程池在取消时 `_terminate_pool`；取消是正常终态（`status=cancelled`） |
| **param_plan** | `core/params.py` → `run.data["param_plan"]` / `result.param_plan` | 参数是**运行级/阶段级**：同一 `pass` 内 `exhaustiveness`/`box_size`/`box_center` 完全一致；用户显式参数冻结（`source="user"`）；每条决策都有 `decisions` 理由 |
| **运行记录** | `runs.Run` | `run.data` 存 `request/task_spec/created_at/agent_models/completeness/live_progress`；`calls` 计数**每次运行开始时重置** |

---

## 6. 数据来源与真实性准则

1. **工具原始输出优先**：模型可能在转述时丢字段。凡「工具已经算出的事实」都通过
   `_PROVENANCE_KEYS` + `tool_io` 回填（盒子来源、口袋、化学溯源、unsupported 残基…）。
2. **禁止编造**：工具失败必须返回 `error` 或明确的 `status`（`no_molecules` / `file_error` / `unavailable`…），
   **不允许**用默认值或估计值冒充真实结果。`verify_docking.py` 有专门的反证用例看护这一点。
3. **可复现信息必须随结果走**：`engine`、`exhaustiveness`、`seed`、`seed_policy`、`n_poses`、`pose_count`
   以及各 Agent 实际使用的模型（`agent_models`）。
4. **失败要分类且可继续**：单分子失败 → 该行 `error`，其余照常；受体准备失败 → 明确 `RuntimeError` + 补救建议；
   取消 → `CancelledRun`；工具不可用 → `unavailable` 并说明缺什么。

---

## 7. 配置体系与优先级

### 7.1 三层来源

| 层 | 位置 | 谁能改 | 适用 |
|---|---|---|---|
| 内置默认 | `config.py` / `settings.py` 的 `Spec.default` | 开发者 | 全部 |
| `.env` | `projects/.env`（gitignore + 打包排除） | 部署者 | 全部（含部署保护项） |
| 界面设置 | `config/local_settings.json`（gitignore + 打包排除） | 用户（设置页面） | 除只读项外的全部 |

**运行时优先级（越具体越优先）**：

```
角色环境变量 LLM_<字段>_<角色>  >  界面设置(角色)  >  内置角色默认
   >  界面设置(全局)  >  .env  >  内置默认
```

`settings.apply_runtime_env()` 把运行类参数写进 `os.environ`，并记录**注入基线**（`injected_env_keys`），
回滚时只删自己注入的键（历史 bug：`reset` 误删 shell/CI 注入的变量）。

### 7.2 只读（部署保护）项

`PORT`、`DOCKING_MAX_LIGANDS`、`UPLOAD_MAX_MB` 等 `env_priority=True` 的字段：`.env` 优先，
界面只读。`reject_readonly()` 会**整包拒绝**提交（前端也已跳过只读字段，双重保护）。

### 7.3 环境变量清单（代码实际读取）

```
# LLM / 运行
LLM_BASE_URL LLM_API_KEY LLM_MODEL LLM_TEMPERATURE LLM_TOP_P LLM_TIMEOUT LLM_THINKING
LLM_<字段>_<角色>（角色覆盖） INTAKE_LLM LOG_LEVEL PORT HOST
CHECKPOINT_BACKEND RECURSION_LIMIT RUN_TIMEOUT_SECONDS ARTIFACT_BASE_URL
# 对接与并发
DOCKING_MAX_LIGANDS DOCKING_WORKERS DOCKING_THREADS_PER_WORKER DOCKING_SERIAL_THREADS DOCKING_WORKER_MEM_MB VINA_CPU INCHIKEY_ONLINE INCHIKEY_TIMEOUT
POSE_SAVE_MAX POSE_TOP_N POSE_ARTIFACT_MAX
# Agent 上下文与漏斗
AGENT_TOOL_TOP_N AGENT_FUNNEL_MIN AGENT_REFINE_TOP_N AGENT_COARSE_EXHAUSTIVENESS
AGENT_FINE_EXHAUSTIVENESS AGENT_SHARD_MAX
# 对接参数自动规划（core/params.py）
AUTO_PARAM_ENABLED AUTO_PARAM_PILOT AUTO_PARAM_PILOT_N AUTO_PARAM_PILOT_MIN
AUTO_PARAM_BUDGET_RATIO AUTO_PARAM_EXH_MIN AUTO_PARAM_EXH_MAX
AUTO_PARAM_BASE_SCREENING AUTO_PARAM_BASE_BINDING AUTO_PARAM_STATS_SAMPLE
AUTO_PARAM_FLEX_DIVISOR AUTO_PARAM_FLEX_MIN AUTO_PARAM_FLEX_MAX AUTO_PARAM_BOX_REF
AUTO_PARAM_BOX_FACTOR_MAX AUTO_PARAM_REFINE_TOP_MIN AUTO_PARAM_REFINE_TOP_LARGE
AUTO_PARAM_LARGE_LIB AUTO_PARAM_N_POSES_BINDING AUTO_PARAM_N_POSES_MAX
# 口袋
POCKET_TOP_N P2RANK_HOME P2RANK_THREADS
# 报告与流
REPORT_TOP_N CHART_TOP_N CHART_BAR_MAX RESULT_INLINE_LIMIT STREAM_TOOL_RESULT_CHARS
SSE_BATCH_SIZE SSE_FLUSH_MS SSE_PROGRESS_MS
# 其它
UPLOAD_MAX_MB COZE_WORKSPACE_PATH DOCKING_WORKSPACE
```

> **约定**：新增环境变量必须 ① 用 `config.env_int/env_bool/env_str` 读取；② 在 `settings.py` 里加
> `Spec`（这样界面可见、来源可查）；③ 补进 `.env.example`；④ 在 README/本文档登记。
> 四处缺一即视为未完成（`scripts/check_web.py` + `tests/test_settings.py` 会看护一部分）。

---

## 8. 大规模对接的算法要点（性能与稳定性）

### 8.1 两阶段漏斗（`runtime/tool_io.py::funnel_settings`）

```
候选数 ≥ AGENT_FUNNEL_MIN(500)
  ① 全库粗筛 run_docking(exhaustiveness=AGENT_COARSE_EXHAUSTIVENESS=1)
  ② 头部精算 run_docking(top_from_previous=AGENT_REFINE_TOP_N=200,
                        exhaustiveness=AGENT_FINE_EXHAUSTIVENESS=16)
```

- 精算清单**从黑板取**，不经过模型上下文（`top_from_previous`）。
- 合并时**精度优先**：`_dock_precision` = (是否 fine, exhaustiveness)；粗筛分数保留在 `affinity_coarse`。
- 陷阱：`coarse_map` 必须在对接**之前**捕获（对接后黑板会被精算结果覆盖）。

**Agent 路径的两阶段漏斗（v0.12）**：候选数 `N ≥ AGENT_FUNNEL_MIN` 时
时用 `dock_library` 跑「全库粗筛 → 头部精算」，精算**沿用粗筛盒子**（保证阶段间盒子一致），
合并规则与 Agent 路径相同（`_merge_funnel`：精算行替换粗筛行 + `affinity_coarse`）。

### 8.1b 对接参数自动规划（`core/params.py`）

`exhaustiveness` / `n_poses` 留空即自动规划：`base`（screening 12 / binding 16）× 柔性系数
`clamp(P90(可旋转键)/5, 0.75, 2.5)` × 盒体积系数 `clamp((V/22³)^(1/3), 1.0, 2.0)`，
再夹到 `[2, 32]`；`N ≥ 500` 自动两阶段。pilot 预算护栏用最贵的 3 个分子以 `exhaustiveness=1`
真实试跑外推总耗时，超预算**先降精算头部（下限 100）、再降强度（不低于 base/2）**。

- **不变量**：参数是运行级/阶段级 —— 同一 `pass` 内所有分子的 `exhaustiveness`/`box_size`/`box_center`
  完全一致，绝不逐分子不同（换盒子实测就能差 1.34 kcal/mol，采样强度更大；见 §15.7b B0）。
- pilot **只测量**：不写位姿、不进 ranking/黑板，失败退回静态规划（只 warning）。
- 用户显式指定 → `source="user"`，只记录不改写；所有决策写入 `param_plan.decisions` 并在报告成节展示。
- 库统计（2D 描述符）按库内容哈希缓存到 `assets/cache/param_stats/`。
- 回归测试：`tests/test_param_plan.py`（规则/漏斗/预算降级/pilot 失败/缓存/真实对接一致性）。

### 8.2 并发规划（`core/docking.py::plan_concurrency`）—— **线程优先**

> 2026-09-17 在 16 物理核 / 32 逻辑核（Ryzen 9 9950X3D）上重标定，**推翻了旧结论**。
> 测量脚本 `var/tmp/bench_plan.py`（用 `plan_concurrency` 注入点强制配置）；
> 同一批真实分子、`exhaustiveness=16`，**所有配置给出的分数完全一致**（可复现性不受影响）。

实测墙钟（秒，越小越好；括号为 CPU 利用率）：

| 分子数 | 1×8 | 3×8 | 4×7 | 4×8 | 8×4 | 8×3 | 12×2 | 16×1 | 16×2 | 24×1 | 31×1 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **2.7**(17%) | — | — | — | — | — | — | — | — | 池 1×1 = 14.0 | — |
| 2 | 3.4 | — | — | — | — | — | — | — | — | — | — |
| 6 | 7.9(19%) | **4.4**(39%) | — | — | — | — | — | 6×1 = 14.6 | — | — | — |
| 8 | 10.4(18%) | 4.8(44%) | **4.75**(54%) | — | — | 6.5 | — | 8×1 = 16.2 | — | — | — |
| 16 | — | — | **13.5**(47%) | — | — | 18.7 | — | 39.4(20%) | 23.9(39%) | — | — |
| 64 | — | — | — | **38.5**(58%) | 42.7(62%) | — | 48.1 | — | — | 70.4(45%) | 74.3(51%) |

> **真实库上的收益会小一些**（同机、同参数、137 分子真实库 `PGR_137_druglike.sdf`）：
> 主组（130 分子）`24×1 = 73 s` → `4×8 = 62 s`（1.18×）；整段对接 `164 s → 154 s`。
> 原因：该库的成本分布是重尾的 —— large 组里单个超大分子（32.3³ 盒、MW 600+、可旋转键 12）
> 就要 ~90 s，那部分只能靠线程、不能被拆到多个进程，所以整体收益被尾部吃掉。
> 结论仍然是**线程优先**（62 < 73），但**不要**把上面的 1.83× 当成任意库的普适数字：
> 分子越均匀、越小，收益越接近 1.8×；尾越重，收益越接近 1.1×。

| 结论 | 内容 |
|---|---|
| **线程优先** | 单分子搜索能靠线程并行（1/2/4/8 线程 = 7.28/3.64/2.44/1.31 s）→ 每进程给满 8 线程，进程数 = `ceil(可用核/8)`。64 分子上比旧的「24 进程 × 1 线程」**快 1.83×**（38.5 vs 70.4 s），**总 CPU 反而少 29%**（716 vs 1003 CPU·s），利用率 44.6% → 58.1%。 |
| **8 线程是饱和点** | 16/32 线程无额外收益（3.89/3.94 s vs 8 线程 3.94 s 的 3 分子批次）→ 多开的线程只烧 CPU。 |
| **进程越多越慢** | 31 进程比 24 进程更慢（74.3 vs 70.4 s）：重复建图/导入 + 内存带宽争用。故进程数按「可用核 / 线程数」定，不超订。 |
| **小库也要并行** | N=6：1×8 = 7.9 s → 4×8 ≈ 4.4 s；N=8：10.4 s → 4.8 s。 |

- **机器自适应（v0.17）**：规划的唯一输入是 `machine_profile()` —— 部署机器的真实算力。

  | 探测项 | 来源（按优先级） | 为什么要看它 |
  | --- | --- | --- |
  | CPU 配额 | cgroup v2 `/sys/fs/cgroup/cpu.max`、v1 `cpu.cfs_quota_us/period` | 容器 `--cpus=4` 时 `os.cpu_count()` 仍报宿主的 32 —— 按它规划会超订 8 倍 |
  | CPU 亲和性 | `os.sched_getaffinity(0)` | `taskset`/cpuset 只给几个核时以它为准 |
  | 逻辑核 | `os.cpu_count()` 兜底 | 裸机场景 |
  | 物理核 | `/proc/cpuinfo` 的 (physical id, core id) 去重 | 判断是否 SMT（仅用于画像与夹取） |
  | 内存 | `/proc/meminfo` 的 `MemAvailable` | 每 worker 约 400 MB 的网格图与 RDKit |

  结论里附 `source`（`cgroup 配额` / `CPU 亲和性` / `cpu_count`），日志与 `GET /api/health`
  都会打出，部署后一眼能看出「为什么是 4 进程 × 8 线程」。
- **公式**：`budget = logical − 1`（留一核给服务）；
  `threads = min(8, DOCKING_THREADS_PER_WORKER, budget // 2)`（`budget//2` 保证至少 2 个进程）；
  `workers = min(ceil(budget / threads), 分子数, _memory_worker_cap())`；单分子时 `threads = min(8, budget)`、`workers = 1`。
  `DOCKING_WORKERS` 显式给定时按**精确值**（专家模式：宁可多进程也不要多线程）。
- **不同机器上自动得到的配置**（同一份代码，`plan_concurrency(1000)`）：

  | 部署形态 | 识别结果 | 自动规划 |
  | --- | --- | --- |
  | 16C/32T 工作站（本项目实测机） | logical=32, physical=16, 来源=cpu_count | 4 进程 × 8 线程 |
  | 4C/8T 笔记本 | logical=8 | 3 进程 × 3 线程 |
  | 2C/4T 小机 | logical=4 | 3 进程 × 1 线程 |
  | 容器 `--cpus=4`（宿主 32 核） | logical=4，**来源=cgroup 配额** | 3 进程 × 1 线程 |
  | cpuset 只给 6 核 | logical=6，来源=CPU 亲和性 | 3 进程 × 2 线程 |
  | 64C/128T 服务器 | logical=128 | 16 进程 × 8 线程 |
  | 单核容器 | logical=1 | 1 进程 × 1 线程 |
- **所有对接都在 worker 进程里跑**：实测「池 1×8 = 进程内 1×8 = 2.72 s」，因此取消了旧的
  「≤7 分子单进程多线程」分支 —— 换来的是**任意分子数都能被「停止」秒级杀掉**（旧分支里
  进程内 Vina 杀不掉，只能等本分子跑完）。
- **长尾权衡**：库里有极个别超大柔性分子时（单分子十几分钟），把 `DOCKING_THREADS_PER_WORKER`
  调小（=更多进程）可缩小被一个分子堵住的产能比例；这是显式可调的，不写死在代码里。
- **内存护栏**：`_memory_worker_cap()` = `MemAvailable × 0.7 / DOCKING_WORKER_MEM_MB(400)`，防 OOM。
- **LPT 调度**：按 `_ligand_cost`（可旋转键×4 + 重原子）从大到小提交，减少尾部拖尾（实测 7 分子 4.9×）。
- **可复现性**：Vina 每次 `dock()` 用构造种子重新采样 → 分数与批次组成/进程数/线程数无关
  （有永久回归测试 `test_scores_are_independent_of_batch_and_workers`；本次标定的所有配置分数逐位一致）。
  **不要**为了“隔离”给每个分子换种子：那样只会改变分数而不带来收益（已实测并否决）。

### 8.3 上下文预算（大库不撑爆模型）

| 机制 | 位置 | 作用 |
|---|---|---|
| 工具视图截断 | `tool_io.summary_limit()` + `top_rows` | 10k 行 → 模型只见 summary+top（实测 4.69 MB → 3,881 B） |
| 分子库离线化 | `intake._render_big_library` | 大清单写 `ligands_input.csv`，指令里只给条数+预览+路径 |
| 结果内联截断 | `runs.detail` 的 `RESULT_INLINE_LIMIT(200)` | HTTP 详情不整包内联，明细走分页接口/产物下载 |
| 工具返回截断 | `STREAM_TOOL_RESULT_CHARS` | SSE 不回传超长工具原文 |
| 分片护栏 | `AGENT_SHARD_MAX(8)` | 禁止把库切成几十片逐片调度（每片 8–10 s LLM 往返，比对接本身还贵） |

### 8.4 性能测量方法（做性能改动时的规矩）

- 基准必须在**空载**机器上做；先 `uptime` 看 loadavg，被别人的进程干扰时必须说明或重测。
- 每次改动给出「改前/改后 + 数据量 + 参数 + 机器核数」，写进 README §11 / 本文档对应小节。
- 已知无效的度量：无法解析的 `.smi`（0 分子也会“很快”）—— 一定要断言分子数 > 0。

---

## 9. 特殊化学的处理契约（工具报事实，Agent 做判断）

### 9.0 输入归一化（所有入口唯一通道）

用户上传的东西「格式多样且不统一」，因此**任何**读分子的代码都不能自己解析文件，必须走
`core/normalize.py`。契约：

| 规则 | 说明 |
|---|---|
| 内容优先于扩展名 | `sniff_format()` 看字节：`.dat` 里装 PDB 也按受体处理；错扩展名双向都测过 |
| 容器 | gzip / zip 透明解包（内存解包 + 内容寻址落盘到 `assets/cache/norm_<sha1>_<名>`） |
| 编码 | `utf-8-sig → utf-8 → gbk → utf-16 → latin-1` 依次尝试并如实上报命中的编码 |
| 表格 | 分隔符（`,` `\t` `;`）按字段数一致性打分；表头按中英文别名识别（name/名称/编号…，smiles/结构…，inchi/inchikey） |
| 容错 | **逐行**容错：坏行进 `skipped`（带行号 + 原因），不中断整库 |
| 去重 | 按 **canonical SMILES** 去重，保留首条 ID 并把其余 ID 记进 `aliases`；`duplicates` 里逐条记录 `kept_name` |
| 不改化学 | 多片段（盐/溶剂）只写警告与 `removed_fragments`，**绝不**静默改 SMILES 主键 |
| 产物 | 每次归一化写运行产物 `input_normalization.json`（`GET /api/runs/{id}/artifacts/input_normalization`） |

标准分子记录：`{id, name, smiles, source_file, source_index, raw}`（`id == name`，便于 CSV/报告直接输出 ID）。

**三个必须记住的边界**：

1. **附件清单不是名称来源**。前端会把上传文件拼成「引用文件（…）：- 名 → /绝对/路径」附在指令末尾，
   落盘名形如 `<时间戳>-<哈希>-<原名>`。这段是**机器生成**的，路径里的哈希片段曾被受理模型当成
   「用户点名的受体」并阻断整个运行（真实缺陷 `20260917-122453-0404` 的 `C6B872`）。因此：
   - 受理模型看到的是 `_llm_instruction_view()`（**不含绝对路径**，只留文件显示名）；
   - 确定性规则与 LLM 合并都只在 `user_instruction_text()`（剥掉该清单）里找受体名；
   - 丢弃时写进 `llm_notes`，**不静默**。
2. **InChIKey 是单向哈希**，离线只能反解内置映射表里的常见化合物；凡走过这条路的分子的
   `input_normalization.json` notes 会**点名列出**，并建议改用 InChI/SMILES/带坐标的 SDF。
3. **`resolve_molecule_file()` 命中多个候选时不猜**：返回 `candidates` 交给上层报错/提问，
   `attempted` 逐条记录失败原因（裸显示名 `PGR.sdf` → 命中上传落盘名是正常路径，不是特例）。

细节见 `docs/api.md` §16，开发时记住四条：

1. **受体**：`dropped_hetatm`（剔除了什么）/ `kept_hetatm`（**最终 PDBQT 里真的有**）/ `unsupported_hetatm`
   （要求保留但缺模板）/ `cocrystal_ligand`（盒子依据）；`keep_hetatm` 对**已准备好的 PDBQT** 也生效
   （回到 sidecar 记录的原始 PDB 重新准备）。
2. **配体**：`ligand_facts` + `ligand_warnings`；盐/反离子拆分时给 `dock_smiles` + `removed_fragments`
   （原始 `smiles` 仍是合并主键，**不要**改）。
3. **盒子**：`box_source` / `box_chosen_by` / `box_validation` / `box_warnings` + `box_fit_warning`
   （配体跨度接近盒子时告警）。
4. **位姿**：`n_poses>1` 必须真的写出全部位姿（`pose_count`），不能只写最优一个（历史 bug）。

---

## 10. 开发规范

> 规范不是口号：下面每条都对应可执行的检查或明确的门禁。**现状差距**（2026-09-14 审计实测）
> 与清理计划见 §15.9。

### 10.1 工程与代码规范（硬性）

| # | 规则 | 为什么（历史教训） |
|---|---|---|
| 1 | **分层依赖单向下行**：`core/` 不 import `tools/agents/api`；跨层用函数内延迟导入并注明原因 | 曾有 11 处隐性反向依赖，导致测试必须拉起整个服务 |
| 2 | **`from __future__ import annotations` 全量开启**（`__init__.py` 除外） | 未来注解零成本；缺失会让新代码风格分裂 |
| 3 | **公共函数标注参数与返回类型**；模块/公开函数写中文 docstring（说明**为什么**，不只复述参数） | 契约最关键的边界（HTTP 路由、工具入口、Agent 工厂）曾恰好无类型 |
| 4 | **日志**：`logging.getLogger(__name__)`；`src/` 非 CLI **禁止 `print`**；异常分支至少一条日志 | 曾有 `except: pass` 把「引擎缺失」「示例库变空」吞掉，线上最难查 |
| 5 | **禁止静默吞异常**：`except ...: pass` 必须改为日志，或写明 `# 允许静默：<原因>`（解析脏数据、首次无缓存等） | 与上一条同一件事，但要给合法的静默留出口 |
| 6 | **错误表达分层**：`core/` 抛异常（`ValueError` 输入 / `RuntimeError` 环境 / `CancelledRun` 取消）；`tools/` 只返回 `{"status": ..., "message": ...}`，**不把异常甩给模型** | 两层口径混用会逼调用方同时 `try` 与 `json.loads(...)["status"]` 双判 |
| 7 | **配置只走 `config.env_*()`**；新增配置必须同步 `settings.Spec` + `.env.example` + 文档 | 9 处裸 `os.getenv` 曾让变量在设置页看不见、改不了 |
| 8 | **不得臆造数据**：数值只来自工具；缺失就写缺失并说明 | 产品底线（§1.3） |
| 9 | **文件规模**：单文件 ≤700 行；超限先登记豁免并限期拆分（`scripts/lint_local.py` 的 `BIG_FILE_EXEMPT`） | `api/app.py` 已 1237 行、职责 6 类 |
| 10 | **命名**：`snake_case` / 私有 `_` / 常量 `UPPER_SNAKE`；工具名与角色名英文小写下划线 | 全库已一致，保持即可 |
| 11 | **中文文案要写「怎么办」**：用户可见的错误/notes 报告不能只写「失败了」 | 如「HEM 缺模板未纳入计算 → 提供模板或直接用 PDBQT 重跑」 |
| 12 | **行宽 100**：实测 >100 字符仅约 100 行、>120 仅 2 行，100 是既有事实标准 | 统一风格，避免大范围重排 |
| 13 | **一个改动只做一件事**；改行为必须带测试或说明为何不需要 | 便于回溯与回滚 |
| 14 | **测试函数与脚本入口也要标 `-> None`** | `scripts/lint_local.py` 对 `tests/` 同样生效，新用例必须合规（存量在 baseline 里） |

### 10.2 变更门禁（改完必须全绿，缺一不可）

```bash
cd projects
export UV_CACHE_DIR=/home/biolab/Tools/docking-agent/.uv-cache MPLCONFIGDIR=$PWD/var/cache/matplotlib

# 1) 静态门禁（零依赖，秒级）：静默 except / print / 裸 getenv / 缺 future 注解 / 缺返回类型 / 超长文件
.venv/bin/python scripts/lint_local.py                      # 新增问题必须为 0（存量见 --all）

# 2) 单元与接口回归
INTAKE_LLM=off .venv/bin/python -m pytest tests -q           # 当前 643 项（含文档一致性、盒子一致性、化学契约、源码结构、取消语义、机器自适应与实跑异常回归）
# 3) 真实链路取证
INTAKE_LLM=off .venv/bin/python scripts/smoke_test.py        # Fake LLM 多 Agent + 真实 Vina（46 项）
INTAKE_LLM=off .venv/bin/python scripts/verify_docking.py    # 对接真实性与反证控制（47 项）
# 4) 受理层决策样例
.venv/bin/python scripts/intake_eval.py                      # 14 项，零模型
# 5) 前端
.venv/bin/python scripts/check_web.py                        # 78 项静态校验
.venv/bin/python scripts/snapshot_ids.py diff                # DOM id 不得被删（基线 213）
node --check web/app.js && node --check scripts/ui_e2e.js
node scripts/ui_e2e.js http://127.0.0.1:<port>               # 可选：需先起服务（143 项，jsdom 真点按钮）
```

**门禁策略**：静态门禁（1）与测试（2）是**阻断式**；取证脚本（3）在动 `core/docking.py`、
`core/receptors.py`、`core/pockets.py` 时必跑；前端类改动必须跑（5）。
新增用例后基准数只增不减；`scripts/lint_baseline.json` 只允许**收窄**（修好一条就 `--update-baseline`）。

### 10.3 什么时候必须加测试

| 改动类型 | 必须补的测试 |
|---|---|
| 新增/修改工具行为 | `tests/` 里对工具输出的断言（字段、真实数值范围、失败路径） |
| 新增配置/环境变量 | `tests/test_settings.py`（优先级、掩码、只读）+ `.env.example` 登记 |
| 修改提示词中的硬纪律 | `tests/test_intake.py` + `scripts/smoke_test.py`（Fake LLM 走的真实链路） |
| 修 bug | 先写能复现的失败用例，再修（`../CHANGELOG.md` 每条修复都有对应用例） |
| 改前端结构/DOM id | `scripts/check_web.py` + `snapshot_ids.py` + 需要时 `ui_e2e.js` |
| 改并发/性能 | 有数据量、参数、机器信息的实测对比（§8.4） |
| 改文档里的**事实性陈述** | `tests/test_docs_consistency.py`（角色名/环境变量/门禁脚本/承诺行为） |

## 11. 扩展指南（照着做不会漏）

### 11.1 新增一个工具（子 Agent 能力）

1. 在 `tools/<域>.py` 用 `@tool` 定义；docstring 写清参数、返回结构、**失败时的 status**；
2. 若属于某子 Agent：在 `agents/workers.py::init_workers` 的工具列表里挂上；
   若是主管级：加进 `coordinator.build_agent` 的列表并在 `agents/dispatch.py` 里提供分发入口；
3. 大结果先 `tool_io.record("<name>", payload)` 再回摘要（§8.3）；
4. 需要跨 Agent 共享的信息写黑板（`runtime/blackboard.py` 加 setter/getter）；
5. 若结果要进最终产物：在 `agents/persistence.py` 的 `_PROVENANCE_KEYS` 或合并函数里登记；
6. 补测试 + 更新 `docs/api.md` 的工具表 + 若影响提示词则同步 `config/agent_llm_config.json`。

### 11.2 新增一个 Agent 角色

1. `runtime/llm.py::ROLES` 与 `settings.py::ROLES/ROLE_LABEL/ROLE_FIELD_SPECS` 同步加角色（三处必须一致）；
2. `agents/prompts.py` 加系统提示词；`agents/workers.py` 加 LLM 实例、checkpointer、`get_*_agent()`；
3. `agents/dispatch.py` 加分发工具（含边界校验）；`coordinator.build_agent` 挂上；
4. 前端设置页会自动出现该角色的模型字段（schema 驱动，无需改前端）；
5. 补测试：角色实例互不相同、调用计数、失败降级；更新本文档 §4 与 `docs/api.md` §10/§11。

### 11.3 新增一个设置项

`settings.py` 的 `Spec`（path/label/group/kind/default/env/help）→ `.env.example` → README 设置章节。
若是运行类，加入 `RUNTIME_SPECS` 以便 `apply_runtime_env` 注入；若是部署保护项，设 `env_priority=True`。

### 11.4 新增 HTTP 接口

在 `api/app.py` 实现 → `api/schemas.py` 加请求模型 → `docs/api.md` 补文档 → `tests/test_api.py` 加用例；
返回大数组的接口必须分页或截断（§8.3）。

### 11.5 新增前端区块

`index.html` 加结构（id 用 kebab-case）→ `app.js` 渲染（数据来自 SSE 或接口）→ `styles.css` 用令牌
→ 跑 `check_web.py` / `snapshot_ids.py`（若确实删了 id，需在基线里**说明并同步**，不能默默删）。

---

## 12. 测试与验收体系

| 脚本/用例集 | 项数 | 覆盖 | 何时跑 |
|---|---|---|---|
| `scripts/lint_local.py` | 增量 | 静态规范：静默 except、print、裸 getenv、future 注解、返回类型、超长文件 | 每次改动（秒级） |
| `tests/`（pytest） | 643 | 核心算法、API、设置、受理、协作、口袋、**盒子一致性/库级下限/超限分组**、特殊化学、可复现性、文档一致性、**并发隔离**、**源码结构（重复定义/反向依赖）** | 每次改动 |
| `scripts/smoke_test.py` | 46 | Fake LLM 驱动**真实**多 Agent + 真实 Vina + 运行目录检查 | 每次改动（尤其动提示词/编排） |
| `scripts/verify_docking.py` | 52 | 对接真实性取证：独立复算、参数敏感性、反证控制（平移盒子/换受体/非法输入） | 动 core/docking 必跑 |
| `scripts/check_web.py` | 118 | 前端静态：语法、离线可跑、id 对应、设计令牌、布局健壮性 | 动前端必跑 |
| `scripts/snapshot_ids.py` | 241 id（基线 213） | 防止误删动态引用的 DOM id | 动 index.html 必跑 |
| `scripts/ui_e2e.js` | 143 | jsdom 真加载页面 + 真点按钮 + 真读接口 + 合成 SSE 并行场景 + **导出条/KPI/报告目录** | 可选（需起服务） |
| `scripts/ui_shot.py check` | 6 页 | 真浏览器（Chromium）：控制台错误 / 横向溢出（含疑似元素）/ 图片加载失败 | 动前端必跑 |
| `scripts/ui_shot.py diff` | 6 页 | 视觉回归：与基线逐像素比对，输出「改前/改后/红色高亮」三格差异图 | 动前端的验收依据 |
| `scripts/intake_eval.py` | 14 | 受理层决策（run/ask/reject）用例集，零模型 | 动 intake/提示词必跑 |
| `bash start.sh --check` | — | 环境自检（依赖/工具/端口） | 部署前 |

**取证式测试的设计原则**：能独立复算的就独立复算（`verify_docking` 不 import 项目对接代码）；
能反证的必须反证（平移盒子、换受体、非法 SMILES 都必须改变或拒绝结果）。

**怎么让 AI「看见」界面（视觉验证）**：DOM 级用例证明不了「看起来对不对」。本仓库有两条路：

**① 可编程浏览器（首选，改前端时用它）** `scripts/ui_shot.py`（Playwright + Chromium）：

```bash
# 一次性准备（约 400 MB，装在工作区内、已 gitignore；playwright 声明在 pyproject 的 dev 组）
uv pip install playwright
PLAYWRIGHT_BROWSERS_PATH=$PWD/var/cache/ms-playwright .venv/bin/python -m playwright install chromium

.venv/bin/python scripts/ui_shot.py shot --page settings --out var/tmp/shot/settings.png
.venv/bin/python scripts/ui_shot.py check    --base http://127.0.0.1:5095   # 控制台错误/横向溢出/图片失败
.venv/bin/python scripts/ui_shot.py baseline --base http://127.0.0.1:5095   # 改前存基线
.venv/bin/python scripts/ui_shot.py diff     --base http://127.0.0.1:5095   # 改后比对（输出三格差异图）
.venv/bin/python scripts/ui_shot.py diff --update                            # 确认无误后更新基线
```

- 覆盖 6 个固定状态：`chat` / `manual` / `history` / `run-detail` / `settings` / `chat-narrow`（窄屏）；
  历史列表与运行详情是**工作台内的标签页**（不是路由），脚本用点击步骤到达（`PAGES[].steps`）。
- **动态内容会冻结**（`#history-tbody`/`#log-stream`/进度与计时等按 `VOLATILE_SELECTORS` 隐藏），
  否则时钟与运行数据一变就误报；`--no-freeze` 可看真实页面。
- 实测：同一版本连续两次 `diff` = 0.000%（可作门禁）；截图可用会话读图工具直接查看。

**② 无需依赖的桌面抓图** `scripts/shot.py`：`wmctrl` 定位桌面上**已打开**的窗口 → `ffmpeg x11grab`
只抓该窗口矩形 → 带页脚输出 PNG。适合"你屏幕上那一屏"（沙箱内无法自己拉起可见浏览器窗口）。

```bash
.venv/bin/python scripts/shot.py --list                     # 先看有哪些窗口、挑标题
.venv/bin/python scripts/shot.py                            # 默认抓标题含「分子对接」的窗口
.venv/bin/python scripts/shot.py --title DeepSeek --scale 0.5
```

实测结论（别重复踩）：**沙箱内无法自己拉起可见浏览器窗口**（`firefox --kiosk` 与 headful 都不映射窗口），
而 `firefox --headless --screenshot` 在 load 事件抓图 —— SPA 还没渲染，只会得到空页面；
因此可行路径是「抓已打开的窗口」。抓图前会先把目标窗口抬到最前（否则会拍到压在上面的窗口）。
更省事的替代：人自己截图后把 PNG 放进工作区（如 `var/tmp/shot/user.png`），把路径告诉 AI 即可。

**文档也进测试**：`tests/test_docs_consistency.py` 用机器判定「文档说的」与「代码做的」是否一致
（已删除的角色不许复活、角色名必须来自 `ROLES`、代码读到的环境变量必须在 `.env.example` 登记、
README 必须指向本手册、承诺的门禁脚本必须存在、不得再承诺已移除的「自动补齐」）。
这条测试是 2026-09-14 文档审计（30 处不一致）的直接产物：**错误文档比没有文档更糟**。

---

## 13. 已知陷阱与历史踩坑（做类似改动前先读）

| 陷阱 | 后果 | 正确做法 |
|---|---|---|
| `ps`/`kill` 在沙箱 PID 命名空间内看不到宿主进程 | 以为“没有残留进程”，机器被孤儿 worker 吃满 | 清理由宿主 shell 执行 `scripts/kill_leftovers.sh`（见 README §15） |
| `/tmp` 在两次 bash 调用之间不持久 | 中间文件丢失、看似随机失败 | 用 `projects/var/tmp/` |
| `HOME`/`~/.cache`、`/run/user/1000` 只读 | matplotlib/RDKit 报错 | 设 `MPLCONFIGDIR=var/cache/matplotlib`、`UV_CACHE_DIR=.uv-cache` |
| 旧代码服务未重启 → 改动“看不到” | 误判修复无效 | 改完起**新端口**验证，别复用旧进程 |
| 用 `pkill -f <关键字>` 清进程 | 匹配到自己的 shell，命令自杀（exit 143） | 用 `scripts/kill_leftovers.sh` 精确识别 |
| 时间戳比缓存 | prot.pdb 每次重写 → 缓存恒失效（每次重跑 meeko） | 按**内容哈希**判定缓存 |
| 提示词内部两条规则冲突 | 模型行为摇摆（该跑却问） | 用显式 `decision` 字段替代“让模型自己权衡” |
| 工具返回全量明细给模型 | 大库撑爆上下文（4.69 MB） | `tool_io` 摘要视图 + 离线产物（§8.3） |
| 进程累计计数当单次计数 | 无法判断本次调用量 | 运行开始时 `reset_registry_counters()` |
| 在对接后读黑板拿粗筛分数 | 粗筛分被精算覆盖 | 对接**之前**捕获 `coarse_map` |
| 只写最优位姿却宣称 `n_poses=N` | 参数语义不一致 | `n_poses>1` 用 `write_poses` 并报告 `pose_count` |
| 为了让结果“稳定”而每分子换种子 | 分数变化但无收益 | 已验证 Vina 每次 `dock()` 重新采样，批次/进程无关 |
| docstring/README 与行为不一致 | 后续 AI 开发被误导（比没文档更糟） | 改行为必须同步文档；§14 门禁含 `check_web.py` 静态检查 |
| 直接对所有 HETATM 求质心定位盒子 | 硫酸根/甘油/离子把盒子带偏 | 用 `cocrystal_ligand`（分组取最大团 + 过滤添加剂） |
| `asyncio.to_thread` 包装的旧代码在长跑服务里看不到改动 | 修复无效 | 重启服务 / 换端口验证 |
| **用字符串替换搬移 HTML 块时不先断言插入锚点** | 锚点不匹配 → 块被删除但没插入，**整块 DOM 静默丢失**（本次丢了 67 个 id） | 先切块并校验标签配平 → **先插入再删除** → 断言 id 数与无重复；改前先落一份备份（本次靠 Firefox `cache2` 里的缓存响应恢复） |
| **把工具调用轨迹镜像进对话气泡** | 气泡被「调用工具：X／工具返回：X／节点更新：tools」淹没，与右侧面板重复，报告正文被挤到看不见（真实体验问题） | 工具事件只进 `#tool-trace` + 阶段日志；对话流只留用户/助手往来与运行级状态（`appendChatNote` 只用于「运行完成/已取消」）；`scripts/ui_e2e.js` 有 5 条断言看护 |
| 在 jsdom 里提前 `window.close()` | 应用内部仍在 await 的续体访问 `document` → 未处理拒绝把脚本带崩 | 全部断言完成后再统一关窗；**挂着在途 SSE 的页面（parallel/live/run）一律不关**，只冻结 fetch 交给 `process.exit`（高负载下曾把 143/143 变成 crash） |
| **取消只在「有 future 完成」时才被检查** | 一整批都是超大柔性分子时循环转不到，用户点停止后进程池继续满载（实测 load 18→40）；更糟的是 `terminate()` 后执行器会**重新拉起 worker** 啃队列里剩下的分子 | 守护线程盯标志 → `shutdown(cancel_futures=True)` 先取消队列 → 再 terminate → 1 s 后对不响应 SIGTERM 的 worker 强杀；工具层再拒绝「已取消运行」的新对接 |
| **盒子内没有任何受体原子** | Vina **不报错**，直接返回全 0 能量（`affinity=0.0`）；0.0 不是分数而是「什么都没算」，会被当成合法结果排序/写报告（真实事故：盒子来自另一个蛋白，距受体最近原子 75 Å，147 个分子白跑 7.5 分钟） | 开跑前 `box_atom_stats()` 数盒内原子：为 0 → 拒绝该受体 + notes 写明「差多远」；行级再把全 0 能量判为 `error`（Vina/AutoDock 两条路径都拦） |
| **中断留下悬空的 `tool_calls`** | 点「停止」/运行失败时，模型已发出 `tool_calls` 而回执永不产生；checkpointer 记下这条 AIMessage ⇒ **同一会话下一轮**把非法序列发给 OpenAI，直接 `400 insufficient tool messages`，整段对话卡死 | 每轮开始前 `repair_thread_state()`：在所属 AIMessage **正后方**插入占位 `ToolMessage`（如实说明"被中断、没有结果"），再用 `RemoveMessage`+整体重写写回（`add_messages` 只能追加）；三个图入口（对话流 / `/run` / OpenAI 兼容）都调；`tests/test_thread_healing.py` 5 条看护 |
| **工具产物文件是「覆盖写」** | `tool_io.write_json` 每次覆盖 ⇒ `docking_tool.json` 只存**最近一次**调用；落盘若直接采用它，分批对接/蛋白质库的早先受体块会静默消失（文档却写着「多次返回做合并」） | 落盘时按 `receptor_key` 合并「产物文件 + 消息历史里的全部调用」；只有真的多出数据时来源标注才写 `tool_file+tool_message`（既有 `tool_file` 契约不漂移） |
| **工具承诺「留空即用共享黑板」却不写黑板** | 属性评估子 Agent 连续 3 次读到「黑板上无分子」，147 条库只有前 20 条被评估（报告数据缺口），且 `status=ok` 悄悄通过 | 谁受益谁写入：`import_molecule_library` 成功后 `board.add_molecules()`；`normalize_molecule_library` 大库只回前 N 条（清单留在黑板） |
| **同一模块里出现两个同名顶层函数** | 后者静默遮蔽前者：读代码/改代码时命中的可能是死的那份（`core/docking.py` 曾有两个 `dock_library`，62 行死代码） | `tests/test_source_structure.py` 用 AST 看护：重复顶层定义 + `core/` 反向依赖一律失败 |
| 把「工具算出来的盒子」当成「用户显式指定」 | 库级下限被跳过：大配体库被塞进口袋 Agent 顺手给的 22³，多数分子落到互不可比的 large 组 | 只有 `chosen_by == "pocket_agent"` 的工具盒子走 `apply_box_floor` 抬高；用户给的盒子仍一动不动 |
| 长时间纯计算（3D 抽样）期间不打日志/不播报 | 界面看起来卡死；本次 120 个大分子串行抽样 ~5 min 无任何输出 | 计算前后各一条 `logger.info` + `note_cb` 推给界面；能并发的（跨度抽样）默认进程池 |
| 前端自己拼下载文件名 | 与后端命名规则漂移（改一处漏一处） | 命名只在 `runs.download_name()` 一处实现，经 `GET /api/runs/{id}` 的 `downloads` 下发给界面 |
| **共享缓存目录里用「源文件 basename」当键** | 两个并发运行（或同名上传）互相覆盖产物，"我请求保留 ZN，拿回别人的结果"；也是全量 pytest 偶发失败的根因 | 准备产物一律**内容寻址**（文件名带源内容哈希 + 参数哈希），展示名与文件名分离；`tests/test_receptor_race.py` 用 3 线程并发复现看护 |

---

## 14. 文档地图

| 我想知道… | 看这里 |
|---|---|
| **改完代码该跑哪些门禁** | 本文 §10.2 + `scripts/lint_local.py` |
| **下一步该做什么（含优先级与验收标准）** | 本文 §15 |
| 这个项目是什么、一分钟怎么跑起来 | `../../README.md` |
| 框架 / 运行原理 / 使用说明（读者向概览，含 PDF/Word） | `../../docs/技术文档.md` |
| 怎么安装、怎么用、界面每块是什么 | `../README.md` §1–§10 |
| 盒子怎么定的、P2Rank 怎么用、大库怎么打 | `../README.md` §11 |
| HTTP 接口/SSE 字段/多 Agent 协作/设置接口/受理层/特殊化学契约 | `api.md` §1–§16 |
| **代码架构、不变量、扩展步骤、门禁、陷阱、路线图** | **本文件** |
| 评审用完整技术报告（实测数据、来源引用、方法与局限） | `技术报告.md`（PDF/Word 同源） |
| 历史修了什么、为什么这么修 | `../CHANGELOG.md`（逐条含证据与用例） |
| 怎么贡献、改完跑哪些门禁 | `../../CONTRIBUTING.md` |
| 安全模型、已知限制与漏洞报告方式 | `../../SECURITY.md` |
| 关键架构决策的「为什么」 | `adr/README.md`（索引 + 模板） |
| 每个门禁脚本怎么跑、看什么 | 本文 §12 + `../README.md` §13 |
| 平台化部署（LangGraph Platform 镜像） | `../../langgraph-deploy/README.md` |

---

## 15. 目标差距与改进路线

> 评估方法（2026-09-14 一次独立只读审计，含 6 条主线 × 代码证据）：对照 §1.1 的六条能力逐项
> 「代码里已有什么 → 还缺什么」。**总判断**：计算内核与「准备 / 对接 / 评估 / 报告」四段是真实且有
> 测试看护的；**「主管协调」「自动异常处理」「结果可信度」三段目前主要是提示词纪律 + 工具事实上报，
> 缺少代码级强制与兜底**——这就是下一阶段的主战场。
> 优先级 = 对「结论正确性 / 系统可靠性」的影响；工作量 S ≤ 半天、M ≤ 2 天、L > 2 天。

### 15.1 自动准备

| # | 缺口 | 代码证据 | 影响 | 优先级 | 工作量 |
|---|---|---|---|---|---|
| 1.1 | 受体**质子化态/pH**：只做 `--read_pdb -a --default_altloc`，无加氢/质子化 | `core/receptors.py:224` | His/Asp/Lys 质子化错误 → 氢键与静电打分系统性偏差（**最影响物理真实性**） | P0 | M |
| 1.2 | 缺失残基/loop、点突变、二硫键、金属配位几何：只检测不建模 | `grep missing_residue\|disulf\|coordination` 零命中 | 断链口袋变形时结论不可用 | P1 | L（**建议只做检测+披露**，见 §15.8） |
| 1.3 | 配体**互变异构/质子化态枚举**：按输入 SMILES 直接加氢 | `core/ligands.py:264` | 不同互变体打分可差数 kcal/mol，排序不稳 | P0 | M |
| 1.4 | 配体**单一 3D 构象**（ETKDG 单次 + 固定 seed） | `core/ligands.py:258` | 柔性分子可能落在差的局部极小 → 假阴性 | P1 | S |
| 1.5 | 立体化学只告警不处理 | `core/ligands.py:245` | 手性药物对接的是随机对映体 | P1 | S |
| 1.6 | 大分子/肽类阈值过松（仅 MW>800 或重原子>60 告警） | `core/ligands.py:239` | 5 肽不告警，结果无意义 | P1 | S |
| 1.7 | 实验位点一致性判据硬编码 8.0 Å | `core/pockets.py:45` | 大口袋误判「不一致」 | P2 | S |
| 1.8 | 结构水全部剔除（无法保留桥接水/配位水） | `core/receptors.py:_write_prep_pdb` | 部分体系结果偏差 | P1 | M |

### 15.2 自动对接

| # | 缺口 | 代码证据 | 影响 | 优先级 | 工作量 |
|---|---|---|---|---|---|
| 2.1 | 漏斗参数只在提示词（`funnel_advice` 已接入指令，但服务端不强制） | `runtime/tool_io.py`、`config/agent_llm_config.json` 第 11 条 | LLM 不照做就可能一次性 exh=6 跑完万级库 | P1 | M |
| 2.2 | `pass="coarse"` 标记丢失（标在输入行，结果行是新构造的） | `tools/docking.py` vs `core/docking.py` | 轮次不可区分，只能靠 `affinity_coarse` 反推 | P2 | S |
| 2.3 | `top_from_previous` 的兜底来源键名错误 → 兜底永久失效 | `tools/docking.py`（`tool_io.load("docking_rows")`） | 黑板为空时报 `no_previous` | P2 | S |
| 2.4 | 单分子失败**不重试/不降级** | `core/docking.py` 失败分支 | 一个分子准备失败即失去该分子 | P1 | M |
| 2.5 | 无**磁盘**护栏与位姿清点（只限登记数量） | `core/docking.py:127`、`tools/docking.py:212` | 万级库 + 多位姿可写数万小文件 | P1 | S |
| 2.6 | AD4 备用路径缺输入/产物校验，且行里没有 `exhaustiveness/seed` | `core/docking.py:340` | 回退后分数混进同一张排序表 | P2 | S |

### 15.3 自动评估

| # | 缺口 | 代码证据 | 影响 | 优先级 | 工作量 |
|---|---|---|---|---|---|
| 3.1 | **PAINS/反应性基团/聚集剂过滤全缺**（RDKit `FilterCatalog` 已带目录，成本极低） | 全库零命中 | 榜单头部可能是泛检测干扰物 → 湿实验浪费 | **P0** | **S** |
| 3.2 | **打分可信度零手段**：无重打分/共识打分/位姿 RMSD/聚类 | `core/docking.py:405`、`core/ranking.py` | 单一 Vina 分直接决定结论，无不确定性度量 | **P0** | M |
| 3.3 | 无相互作用指纹/接触图/残基能量分解（「结合模式」目前是 2D 相似度） | `core/chemistry.py:160` | 无法解释盐桥/氢键网络 | P1 | L |
| 3.4 | 无富集/显著性（ROC/AUC/EF/p 值） | `runs.py:353` 仅描述统计 | 无法判断「优于对照 0.3 kcal/mol」是信号还是噪声 | P1 | M |
| 3.5 | ADMET 仅 Lipinski（无 QED/SAscore/结构警报） | `core/chemistry.py` | 类药性判断偏窄 | P1 | S |
| 3.6 | 无阴性对照/诱饵（decoy）集 | 全库零命中 | 无法估计假阳性率 | P2 | L |

### 15.4 自动异常处理

| # | 缺口 | 代码证据 | 影响 | 优先级 | 工作量 |
|---|---|---|---|---|---|
| 4.1 | **取消不彻底**：`cancel_event` 未传入对接工具（✅ 已修复：`tools/docking.py` 把 `cancel_flag(run.id)` 传进 `dock_batch(cancel_event=…)`，状态落 `cancelled`） | `api/app.py:521,575,928` | 点「停止」后 Vina 进程池继续吃满 CPU | **P0** | S |
| 4.2 | **崩溃/断线无兜底**：无 reaper/resume/retention，`run.json` 永久 `running`（⚠️ 部分修复：`reconcile_interrupted()` 会把残留 `running` 如实标成 `interrupted`；`RunStore.prune()` + `scripts/prune_runs.py` 提供保留策略。**reaper/resume 仍缺**） | `runs.py:85,167`、`cancellation.py:19` | 前端断流后线程与进程池继续跑，且再也取消不了 | **P0** | M |
| 4.3 | **无端到端墙钟超时**：`RUN_TIMEOUT_SECONDS` 只用于 legacy `/run` | `api/app.py:53,855` | 卡死运行永久占资源 | P1 | S |
| 4.4 | **失败清单不归因、不进报告**：逐分子 `error` 只在 JSON，CSV 只含成功行（✅ 已修复：报告固定第 7 节按原因分组列出失败/跳过，`ranking.csv` 含 `status`/`error` 列） | `reporting/report.py` 无 error/failed | 用户读到「全成功」的假象 | **P0** | S |
| 4.5 | 分发层键校验把子 Agent 的合法 `{"status":"error"}` 改写成 `agent_output_invalid` | `agents/dispatch.py:29,61,91` | 根因被掩盖，误导排障 | P1 | S |
| 4.6 | pockets 工具异常逃逸（`@tool` 抛异常，与其他工具契约不一致） | `tools/pockets.py:96,176` | 模型侧看到的是异常而非结构化错误 | P2 | S |
| 4.7 | LLM 传输层与 `_http_post_json` 无重试/退避；无速率限制 | `runtime/llm.py:177`、`tools/online.py:95` | 端点抖动直接使运行失败 | P1 | S |
| 4.8 | 无运行保留期/磁盘配额（孤儿进程目前靠人工 `scripts/kill_leftovers.sh`） | scripts | 长期无人值守部署必然积压 | P2 | M |

### 15.5 报告与总结

| # | 缺口 | 代码证据 | 影响 | 优先级 | 工作量 |
|---|---|---|---|---|---|
| 5.1 | ~~方法学可复现信息不全~~ **已完成**：PDF 封面列 vina/rdkit/meeko/p2rank/python/matplotlib 版本，报告与 CSV 增 `seed`/`seed_policy` | `reporting/pdf.py`、`reporting/tables.py` | — | ✅ | S |
| 5.2 | ~~失败分子与原因不进报告~~ **已完成**：报告固定第 6 节「方法与局限」+ 第 7 节「失败与跳过」（按 `results[*].error` 归并原因分组） | `reporting/report.py:19-20` | — | ✅ | S |
| 5.3 | 多受体结果被坍缩成第一个受体（`merge_and_rank` 以 smiles 为键） | `core/ranking.py:23` | 声明的「蛋白质库」能力拿不到并列对比 | P1 | M |
| 5.4 | 两轮漏斗的 `pass`/`affinity_coarse` 不进 CSV/报告 | `tables.py:9`、`persistence.py:103` | 报告无法说明粗筛/精算范围（提示词要求说明） | P1 | S |
| 5.5 | `similarity_chart.png` 生成但从未内嵌 | `reporting/artifacts.py:29` vs `report.py:242` | 产物与报告不一致 | P2 | S |
| 5.6 | 报告不含配体准备告警/盐拆分/`box_fit_warning` | `reporting/report.py` 零命中 | 用户不知道实际对接的化学形式 | P1 | S |
| 5.7 | 无跨运行/多轮对比 | `reporting/store.py` 平铺单文件 | 无法比较两批库/两组参数 | P2 | L（**建议先不做**，见 §15.8） |

### 15.6 主管协调

| # | 缺口 | 代码证据 | 影响 | 优先级 | 工作量 |
|---|---|---|---|---|---|
| 6.1 | 计划不是显式产物（无 plan/DAG/step 记录） | `agents/coordinator.py` | 运行不可重放、不可断言「计划被遵守」 | P1 | M |
| 6.2 | 预算与步数控制形同虚设（只有 `recursion_limit`，无 token/费用/墙钟预算） | `api/app.py:582` | 一次运行可能烧掉任意额度 | P1 | M |
| 6.3 | 无动态重规划（失败后不换策略，只有提示词第 6 条「如实报告」） | `agents/dispatch.py:91` | 子 Agent 失败后主管可能仍宣告成功 | **P0** | M |
| 6.4 | 失败升级只到提示词；`_completeness` 明确「只作信息展示，不接管控制」 | `api/app.py:616` | **「保证项目正常工作」的兜底不存在** | **P0** | M |
| 6.5 | 运行图/时间轴不落盘、无 token 计量（usage 硬编码 0） | `api/app.py:950,985` | 事后无法审计「哪一步慢/失败在哪次调用」 | P1 | M |
| 6.6 | 前端未展示后端已有的 `task_spec`/`completeness`/`blackboard_stats` | `web/app.js` 零命中 | 已收集的可信度信息用户看不到 | P1 | S |
| 6.7 | SSE 无重连回放/keepalive，队列无界 | `runtime/streaming.py:24` | 网络抖动即丢事件、无背压 | P2 | M |

> 说明：**横向协作本身是真的**（共享黑板 + 跨 Agent 核验 + 同一轮多个分发调用并发执行，
> 有实测时间轴为证，见 `docs/api.md` §15）；这里列的是「控制与兜底」层面的缺口。

### 15.7 最值得先做的 5 件事（按投入产出排序，含验收标准）

| 顺序 | 事项 | 理由 | 验收标准 |
|---|---|---|---|
| 1 | **PAINS / 反应性基团 / 聚集剂标记**（3.1，S） | 唯一「分析层面直接产出错误结论」的缺口，RDKit 自带目录 | `ranking.csv` 增 `pains_alert` 列；报告单列告警分子；**只标记不静默剔除**；对已知 PAINS 与干净分子各有用例 |
| 2 | **失败可见 + 单分子重试/降级**（4.4 + 2.4 + 4.5，S+M） | 一次修掉「部分失败被静默吞掉」 | 报告新增「失败与跳过清单」（按原因分组 + 失败率）；CSV 含 `status`/`error`；失败先降 exhaustiveness 再降引擎，每次降级写 `notes`；分发层不再掩盖合法 `{"status":"error"}` |
| 3 | **运行级兜底：reaper + 墙钟超时 + Agent 模式可取消**（4.1+4.2+4.3，M） | 「保证项目正常工作」的底线，也是唯一会「卡死且无法恢复」的组合 | 启动时把过期 `running` 标为 `interrupted` 并保留产物；`/api/agent/stream` 注册任务且把 `cancel_event` 传进对接工具（点停止 5 s 内 Vina 进程消失）；`AGENT_RUN_TIMEOUT_SECONDS` 生效并落 `timeout` 状态 |
| 4 | ~~**报告方法学与可复现性补全**（5.1+5.2，S）~~ **本轮已交付 5.1**：PDF 封面工具版本、报告/CSV 的 `seed`+`seed_policy`；**5.2（失败清单章节）仍待做** | — | — |
| 5 | **多受体并列 + 漏斗数据进报告**（5.3+5.4，M） | 能力已实现、价值被丢弃 | `merge_and_rank` 键改 `(receptor_key, smiles)`；报告按受体分表并列出各自盒子；报告说明粗筛范围/精算头部/`affinity_coarse` 分差；「同分子 × 2 受体」有断言 |

### 15.7b 对接盒子尺寸策略（本轮讨论的结论与待办）

现状（`core/pockets.py`）：`size_i = clamp(口袋 extent_i + 2×padding, min_size, max_size)`，默认
`padding=4 Å / min=18 Å / max=30 Å`；优先级 = 显式指定 > 实验位点（用注册表尺寸）> 工具预测（top-1 口袋）> 质心兜底；
Vina 网格间距 0.375 Å，`box_fit_warning` 只在「配体跨度 + 10 Å 超过盒」时**告警**。

| # | 结论/待办 | 理由 | 优先级 |
|---|---|---|---|
| B1 | ~~**盒下限应配体感知**：`min_size` 至少取 `max(库内配体最大 3D 跨度) + 2×5 Å`~~ **已实现（C 方案）**：库级下限 = `P95(库内配体 3D 最大跨度) + 10 Å`（抗单个异常值），默认开 | 现在大配体（肽类/大环）只告警不扩盒，Vina 采样被盒子限制；小配体又白吃 22³ 的算力（耗时 ∝ 盒体积 × exhaustiveness） | **P0** |
| B2 | ~~`box_fit_warning` 升级为「自动扩盒一次 + 记录 `box_source` 变化」~~ **已实现（C 方案）**：超限分子划入 `box_group="large"` 用**同一中心**的更大盒子单独重跑，`box_source` 追加 ` · 库级下限 Y Å`，结果行带 `box_group`/`box_size` | 只告警等于把问题丢给用户；扩盒是可逆且可追溯的动作 | **P0** |
| B3 | 设置页显式暴露 `POCKET_PADDING/MIN_SIZE/MAX_SIZE` 的语义说明（已在 SPECS 里，补文案与推荐值） | 用户不知道 18/30 从哪来、什么时候该改 | P1 |
| B4 | **多口袋并行**：top-N 口袋各跑少量分子，选定最佳口袋后再全库跑 | 未知位点体系里 top-1 未必是真实活性位点 | P1 |
| B5 | 盒中心对齐到 0.375 Å 网格中心 | 减少边缘体素与「体素数奇偶」类问题（历史踩过 `write_maps` 的偶数体素限制） | P2 |
| B0 | **实测：换盒子大小会改变分数（因此不能"每分子一个盒子"）** | 见下表：同一配体在 18³/22³/28³/34³ 下分数最大可差 **1.34 kcal/mol**，且**非单调**；跨分子比较会被盒子效应污染 | — |

**B0 实测数据**（2026-09-14，vina 1.2.7，凝血酶 1DWC 实验位点中心 `31.5/13.74/24.36`，
`exhaustiveness=4, n_poses=1, seed=42, threads=8`；格式 `分数 kcal/mol / 耗时 s`）：

| 配体（3D 跨度 max Å） | 18³ | 22³ | 28³ | 34³ |
|---|---|---|---|---|
| 乙醇（2.4） | -2.83 / 0.5 | -2.81 / 0.6 | -2.81 / 1.0 | -2.85 / 1.5 |
| 阿司匹林（5.4） | -5.87 / 0.8 | -5.87 / 1.0 | -5.86 / 1.3 | -5.88 / 1.8 |
| 华法林（8.4） | **-7.97** / 1.4 | -7.75 / 1.6 | -7.75 / 1.9 | -7.75 / 2.5 |
| 三肽模拟（10.0） | -8.55 / 4.9 | -7.47 / 4.9 | **-8.81** / 6.1 | -7.60 / 5.9 |
| 六肽（16.3） | -7.27 / 12.6 | -7.88 / 11.5 | -8.08 / 12.6 | **-8.21** / 12.8 |

结论：① 分数对盒子大小**敏感且非单调**（三肽模拟 18³→22³ 反而差 1.08），所以「每个配体各自一个盒子」
会直接把盒子效应混进排序；② 小分子耗时随盒子体积增长（0.5→1.5 s，约 3×），
而柔性大配体的耗时主要由可旋转键决定（12.6→12.8 s 几乎不变）；③ 大配体在 22³ 下确实被欠采样
（六肽 22³ -7.88 vs 34³ -8.21），因此需要「库级下限 + 超限分子分组」而不是逐分子自适应。

| B6 | ~~报告里写明「盒为什么是这个大小」（来源 + padding + 夹取区间 + 是否被 B2 扩过）~~ **已实现（C 方案）**：报告元信息「对接盒」行写中心/尺寸/来源/库级下限值与是否因此扩大；第 2 节 main 表上方加盒子一致性说明，large 组单独成节 | 与「工具定盒」的溯源一致，评审可核对 | P1 |

**C 方案实现细节（2026-09-14，代码在 `core/pockets.py` + `core/docking.py`）**：

- **主组盒子唯一（第一原则）**：`select_site` 每个受体只算一次盒；`dock_library` 让 main 组所有
  分子共用 `spec["center"]/spec["size"]`。有回归测试守护（`tests/test_box_sizing.py` 第 1 条）。
- **库级下限**：`library_span_bound()` 用 2D `HeavyAtomCount` 降序取前 `BOX_SPAN_SAMPLE`（默认 200，
  夹在 20–1000；库更小则全算）个分子生成 3D，取跨度 P95（最近秩法：n≤20 时即最大值）+
  `2×5 Å` = `+10 Å`，再与 `POCKET_MIN_SIZE` 取大；按库 SMILES 的 sha1 前 12 位缓存到
  `assets/cache/box_span/`。公式 `size_i = clamp(pocket_extent_i + 2×padding, lib_lower_bound, max_size)`。
  3D 失败/抽样为空一律降级为 `POCKET_MIN_SIZE` + `logger.warning`，绝不因此中断对接。
  **抽样必须并发**（v0.16）：单个大分子 ETKDG+MMFF 要 1–3 s，120 个大分子串行 ~5 min，
  期间界面只能干等；≥ `_SPAN_PARALLEL_MIN`(8) 个样本改走 `ProcessPoolExecutor`（≤8 进程，
  实测 5.0× 加速），进程池不可用时**静默退回串行且结果一致**（有对照测试）。抽样开始/结束
  都有 `logger.info`，并通过 `dock_library(note_cb=...)` 把「正在确定对接盒」推给界面。
- **超限分组**：判据 `any(span_i + BOX_GROUP_MARGIN > box_i)`（默认 10 Å，沿用原 `box_fit_warning`
  口径）；组的盒子 = `clamp(组内最大跨度 + BOX_LARGE_PADDING(默认 12 Å), 主盒, 大配体组上限)`，
  上限允许超过 `POCKET_MAX_SIZE`（否则分组无意义）但受 `BOX_LARGE_MAX_SIZE=60 Å` 硬约束；
  **中心与主组完全相同**。实现为**两遍对接**：先用主盒跑一遍并扣住超限分子的结果（不上报），
  主组即最终主组；随后 large 组用组盒重跑并上报。为空时零额外开销，代价是超限分子会被对接两次
  （跨度直接取自第一遍本来就要生成的配体 PDBQT，**不为全库预生成 3D**）。
- **数据不丢**：结果行带 `box_group`/`box_size`/`ligand_span`；`merge_and_rank` 默认只输出 main 组；
  `ranking.csv` 增 `box_group`/`box_size` 两列（`22.0x22.0x22.0`）；`docking.json`/`result.json` 两组都在。
- **显式优先按来源区分**（v0.16 修正）：**用户**显式指定的盒子（表单/指令坐标）永不被抬高；
  **口袋 Agent 提交的盒子是工具产物** —— 中心必须尊重，尺寸则与自动定盒一样按库级下限抬高
  （`chosen_by == "pocket_agent"` → `apply_box_floor()`，只抬不缩、只夹到 `POCKET_MAX_SIZE`）。
  否则「用户没指定盒子」的大配体库会被塞进 Agent 顺手给的 22³，绝大多数分子只能落到
  互不可比 large 组（真实缺陷：120 个农药大分子库）。两种来源都会走超限分组。
- **为什么是 P95 而不是 max**：单个异常值（如一个超大分子）不应把主组盒子整体推到上限；
  真正超限的分子由 large 组用**同一中心**的更大盒子兜住。
- 新环境变量：`BOX_SPAN_ENABLED`(on) / `BOX_SPAN_SAMPLE`(200) / `BOX_GROUP_MARGIN`(10) /
  `BOX_LARGE_PADDING`(12)，均已进 `settings.py` SPECS 与 `.env.example`。

### 15.8 明确不建议做的事（避免过度工程）

1. **不要**补「缺失残基/loop 建模 + 柔性受体」（1.2，L）：等于内置同源建模 + 诱导契合，
   成本远超收益。正确做法是把「结构断链/缺失残基」作为**局限性写进报告**（S）。
2. **不要**做跨运行/多轮批量对比 UI 与大统计（5.7 + 3.4 的完整版，L）：定位是单次筛选闭环；
   先让**单次运行**可复现、失败可归因。确有需求时先支持「导出两个 run 的 ranking.csv 外部比较」。
3. **不要**继续加码 AD4 与共价/金属对接（2.6 + 1.2 金属部分，M/L）：AD4 与原引擎不可通约，
   回退分数混进同一排序表本身就是风险；共价/金属需要专门引擎与自定义参数。
   更省算力的做法：**AD4 不可用即诚实失败并写进 `notes`**，金属体系只做「事实上报 + 结论标注」。
4. **不要**引入分布式队列/集群调度：单机 32 核是当前瓶颈内的最优形态，运维复杂度不划算。

### 15.9 规范现状与清理计划（对应 §10）

2026-09-14 用 `scripts/lint_local.py` 实测的存量（已写入 `scripts/lint_baseline.json`，只允许收窄）：

| 项 | 存量 | 说明与计划 |
|---|---|---|
| 缺返回类型标注 | 224 | 集中在 `api/app.py`、`runs.py`、`blackboard.py`；**新代码一律要求**，存量按模块分批补（每次顺手改一个文件） |
| 函数内 import 未注明原因 | 366 | 其中真正必要的是重依赖（rdkit/vina/meeko/matplotlib/scipy）与破环（2 处）；标准库延迟导入应上提模块顶部 |
| 静默 `except: pass` | 0 | 已清零（合理解法标了 `# 允许静默：<原因>`） |
| `print`（非 CLI） | 0 | 已清零 |
| 裸 `os.getenv` | 4 | 全部在白名单（settings/paths/logging_setup/load_env），已注释理由 |
| 缺 `from __future__ import annotations` | 0 | 已清零 |
| >700 行文件 | 7 | `api/app.py`(1237)、`core/docking.py`、`core/pockets.py`、`settings.py`、`intake.py`、`core/receptors.py` 已登记豁免；**新文件不得超限**，`api/app.py` 建议按 router 拆分 |
| 依赖单一来源 | — | `requirements-local.txt` 与 `pyproject` 双份维护（内容一致但会漂移）；计划收敛为 `-e .`，测试依赖放 `[dependency-groups]` |

