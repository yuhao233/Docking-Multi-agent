# 多Agent协作分子对接筛选工作台（本地部署版）

> 页面标题：**多Agent协作分子对接筛选工作台**（顶栏中文名 + 下方英文全称 *Multi-Agent Collaborative Molecular
> Docking Screening Workbench*）。默认首页是**简易模式**，顶栏可切到高级模式。

把一个**蛋白质受体的已知结合位点**与**小分子库**做真实分子对接筛选：
由 1 个「主管 Agent」统筹 4 个专业子 Agent（理化性质评估 / 口袋分析 / 对接执行 / 结合模式检测），
对每个小分子给出理化性质、对接能量明细、与阳性对照的结合模式比较，并出具可追溯的筛选报告与排序推荐。

- **一键启动**，自带交互式可视化网页；
- **所有对接数据与中间分析产物**（分子库、性质、对接明细、位姿文件、图表、CSV、报告）逐次运行落盘，可单项或整包下载；
- 所有数值均来自**真实计算引擎**（RDKit / AutoDock Vina / AutoDock4），并有独立取证脚本证明（见 §13）。

**仓库**：<https://github.com/yuhao233/Docking-Multi-agent> ·
**概述与技术文档**：[`docs/技术文档.md`](../docs/技术文档.md) / [PDF](../docs/技术文档.pdf) / [Word](../docs/技术文档.docx)（框架 / 运行原理 / 使用说明）·
**许可**：AGPL-3.0-or-later（见仓库根目录 `LICENSE`）

本文件是**工程主文档**：上手、配置项、接口速查、运行记录格式与门禁验收，偏细节。
历史修复日志（62 条）已迁到 [`CHANGELOG.md`](CHANGELOG.md)。

**文档分工**（避免四份文档互相重复）：

| 文档 | 读者 / 用途 |
|---|---|
| [`../README.md`](../README.md) | 仓库门面：这是什么、一分钟怎么跑起来 |
| [`../docs/技术文档.md`](../docs/技术文档.md) | 读者向概览：框架 / 原理 / 使用说明（PDF/Word 同源） |
| **本文件** | 工程主文档：安装、配置、界面、接口速查、目录结构、门禁与验收 |
| [`docs/architecture.md`](docs/architecture.md) | 开发手册：分层、不变量、开发规范、扩展指南、改进路线 |
| [`docs/api.md`](docs/api.md) | 接口契约：逐端点请求/响应、SSE 字段、错误码 |
| [`docs/技术报告.md`](docs/技术报告.md) | 评审用完整技术报告（含实测数据与来源；PDF/Word 同源） |
| [`CHANGELOG.md`](CHANGELOG.md) | 历史修复日志（按时间编号，保留当时结论） |

## 目录

1. [快速开始（一键启动）](#1-快速开始一键启动)
2. [界面与能力](#2-界面与能力)
3. [前端设计规范](#3-前端设计规范科技感--编程风格--布局稳定)
4. [大库承载（上千～上万分子）](#4-大库承载上千上万分子)
5. [命令行](#5-命令行)
6. [项目结构](#6-项目结构)
7. [中间数据与运行记录](#7-中间数据与运行记录重点)
8. [HTTP API](#8-http-api)
9. [受体与「已知结合位点」](#9-受体与已知结合位点)
10. [配置项（`.env`）](#10-配置项env)
11. [结合位点与对接盒（工具定盒）](#11-结合位点与对接盒工具定盒)
12. [对接引擎](#12-对接引擎)
13. [测试与「真实性取证」](#13-测试与真实性取证)
14. [修复记录（已迁出）](#14-修复记录已迁出)
15. [注意事项](#15-注意事项)

### 对接盒子与参数的自动规划（v0.12）

**盒子（C 方案：库级配体感知 + 超限分组）**

- 一个受体/位点 → **一个盒子，全库共用**（一致性的前提：实测仅换盒子大小，同一配体分数就能差
  **1.34 kcal/mol** 且非单调；逐分子换盒会把盒子效应混进排序）。
- 基数仍由**工具定的口袋**决定（`口袋 extent + 2×padding`，默认 4 Å），新增**库级配体下限**：
  取库内最重的前 K=200 个分子生成 3D、算跨度 P95 + 10 Å 作为尺寸下限（按库内容哈希缓存；失败降级不影响对接）。
  库里有大分子时盒子整体抬高，并在 `box_source` 写明「· 库级下限 X Å」。
- **超限分子单独成组**：某分子「跨度 + 10 Å > 主盒」→ `box_group="large"`，用更大盒子
  （组内最大跨度 + 12 Å）在**完全相同的中心**重跑；主组先跑完。结果行带 `box_group`/`box_size`/`ligand_span`，
  CSV 增 `box_group`、`box_size`，报告有「大配体组（盒子不同，不跨组比较）」小节，默认排序榜只含 main 组。
- **用户显式指定**的盒子（表单/指令坐标）永不抬高（显式优先）；**口袋 Agent 提交的盒子是工具产物**：
  中心照用，尺寸仍按库级下限抬高（只抬不缩）——用户没指定盒子时不该被 Agent 顺手给的 22³ 限制住。
  两种来源都会按超限规则分组。库级下限首次要对最重的前 K 个分子真算 3D 跨度；≥8 个样本自动走
  多进程（实测 5× 加速，120 个大分子从 ~5 min 降到 ~1 min），按库内容哈希缓存，同库重跑即时命中；
  抽样前后都有日志，并把「正在确定对接盒」推给界面，不会长时间看起来像卡死。
- 设置项：`BOX_SPAN_ENABLED`(on) / `BOX_SPAN_SAMPLE`(200) / `BOX_GROUP_MARGIN`(10) / `BOX_LARGE_PADDING`(12)。

**参数（精度优先 + pilot 预算护栏）**

- 用户未给参数时自动规划：`exhaustiveness = clamp(round(base × f_rot × f_box), 2, 32)`；
  `base`=筛选 16 / 结合模式 16（`AUTO_PARAM_BASE_*`）；`f_rot`=P90 可旋转键/5（0.75–2.5）；
  `f_box`=盒体积开立方（1–2，保持单位体积采样密度）；`n_poses` 筛选 1、姿态分析 3；`engine=vina`、`seed=42`。
- **「未指定」是有哨兵的**：`run_docking`/`molecular_docking` 的 `exhaustiveness` 默认值是 **0**
  （不是 16）—— 0/留空 = 用本次自动规划值；显式给值才以调用方为准。用 16 当默认值无法区分
  「用户设了 16」与「没人给值」，会让运行**静默退回** 16（实际用到的强度与来源会写进该次对接的
  `notes`，缺失时明说「本次没有搜索强度规划值」）。
- `N ≥ 500` 自动**两阶段漏斗**（粗筛 exh/4 全库 → 精算前 200，库大且预算允许时上调到 300）。
- **pilot 护栏**（库 ≥ 50）：最贵 3 个分子以 exh=1 实测外推全库耗时；超过 `0.6×RUN_TIMEOUT_SECONDS` 时按
  **精度优先**降级：先降精算头部（下限 100），仍超才降强度且不低于 base/2。
- **参数是运行级/阶段级的**：同一阶段内 `exhaustiveness` 完全一致（回归测试守护）；用户显式参数冻结（`source=user`）。
- 决策全部写入 `param_plan.decisions`（含理由）、随 `result.json` 落盘，并在报告「参数自动规划」一节成表展示；
  CSV 的 `exhaustiveness/seed/seed_policy` 为逐行留痕。

### 界面（代码风主题 · 统一设计 · 动态效果）

- **主题**：深色 IDE / 可观测性控制台风格 —— 等宽字体用于数值、ID、参数、文件名与日志；
  小节标题带 `//` 代码注释符；细边框 + 分层底色 + 克制的强调色；所有面板/按钮/输入框/表格共用同一套
  设计令牌（`web/styles.css` 顶部 `:root`）。
- **布局**：工作台两栏 —— 左侧「任务配置」固定且自身可滚动（长表单不把页面顶长），右侧是
  「多 Agent 编排」+「执行区」两个分区；**「结果与产物」跨两列全宽**（表格与图表需要横向空间）。
  窄屏 ≤1100px 自动单列堆叠。
- **动态效果（平滑顺畅，且尊重 `prefers-reduced-motion`）**：面板入场淡入上移、按钮 hover 微亮/按下回弹、
  编排节点按状态呼吸（`data-state=run/done/wait`）、实测时间轴的时间条从 0 平滑展开、进度条动画条纹、
  日志行淡入、KPI 数字滚动（等宽 `tabular-nums` 防抖）、标签页滑动下划线、设置页锚点平滑滚动。
- **对话框在左栏（对话模式）**：对话历史 + 输入框 + 发送都在左侧「对话模式」里，边聊边配置，运行按钮紧跟其下；
  右侧「执行区」只保留阶段日志、实时分子结果与工具调用轨迹（各自职责更清楚）。
- **精简设计 + 气泡提示（v0.14）**：主流程只留一句必要提示，字段级/长段解释统一收进**工具提示**——
  短解释用 `data-tip`，带结构的用 `data-tip-html`（白名单 `<b>/<br>/<code>/<span class="mono">`，无属性）。
  气泡挂在 `<body>` 上做视口自适应（下方优先、空间不足翻到上方、左右夹紧），因此**不会被面板圆角/滚动或
  `<details>` 的 `overflow:hidden` 裁掉**；同时保留原生 `title` 作为无 JS 兜底（显示期间临时摘掉，避免双重提示）。
- **运行参数常驻在对话框上（v0.25）：对话模式的「运行参数」条不折叠** ——
  一键预设（`快速初筛` exh=4/poses=1 · `平衡` exh=16/poses=1（默认）· `高精度` exh=32/poses=3，
  点击即填入并给出轻提示）+ **恢复系统默认** + 对接引擎 / **质子化态策略 + 目标 pH（默认 ph 7.4，带常用值预设）** /
  位姿数 / **搜索强度（默认勾「自动」，滑块起点 16）** / **阳性对照 SMILES** / **最大分子数**；
  **逐字段**按「默认值」注入：只有真正改动过的字段才下发（`advanced=true`），指令里明确写到的仍优先；
  未改动则保持纯对话（`advanced=false`，参数字段一个都不带，走服务端默认 —— 与界面默认一致，所以"不改也是 16"）。
  展开又收起「其他设置」**不产生任何字段**；「搜索强度：自动」时不下发数值，由服务端 `core/params.py`
  按库柔性 / 盒体积规划（基准 `AUTO_PARAM_BASE_SCREENING=16`）。
  参数模式下同一份 DOM 按顺序回到参数区顶部（受体来源输入框在这里）。
- **共享参数 DOM 与分组**：`#param-common`（运行参数）与 `#params-rest`（配体来源 / 位点盒 / 上传受体）
  由 `app.js::mountParams()` 按模式分别挂载 —— 对话模式：运行参数→对话框上方、其余→「其他设置」折叠；
  参数模式：两块按顺序进参数区。需要专业知识的项（结合位点盒坐标 / 口袋引擎 / 位姿保存 / 上传受体）
  收进「更多参数」二级折叠，摘要行写清里面有什么。
- **对话里直接传文件 / 引用文件**：对话框支持 **附件按钮**、**拖拽上传**与 **`@` 引用**；
  上传走后端 `/api/uploads`（受体 `.pdb/.ent/.pdb1/.cif/.mmcif/.pdbqt` → `receptor_file`，小分子库 `.sdf/.smi/.csv/.mol2` → `molecule_file`），
  附件以 chip 形式显示（等宽文件名 + R/L 标记 + 删除）。发送时：指令里 `@` 命中的附件（或未 @ 时的全部附件）会以
  「引用文件（本次对话已上传，可直接作为工具输入）」清单**带绝对路径**拼进指令，Agent 因此能在工具里真正读取它们；
  **附件属于明确意图**，所以即使高级设置未展开也会下发这两个字段，而**其余参数仍然一个都不带**（保持对话不注入参数的语义）。
  - **气泡只显示你输入的文字** + 紧凑附件 chip（完整路径在悬停提示里）；那份「引用文件」清单**只存在于发给服务端的指令中**，
    不会把绝对路径铺满气泡。助手的长回执**直接全文显示**（不再按行数/字数默认折叠）。
  - **上传成功 = 对话里一定能用**：上传落盘名带时间戳前缀（`<时间戳>-<哈希>-<原名>`），
    `import_molecule_library` / `molecular_docking` 允许只给显示名（`PGR.sdf`），系统会在
    `uploads/cache/assets` 中按「精确名 → 后缀匹配」解析成绝对路径；命中多个候选时**返回候选清单并请用户指定**，
    绝不随便取一个；解析失败时逐条回传实际尝试过的路径与失败原因。受理层在对话折叠模式下也会把请求里的
    `molecule_file`（附件）连同**绝对路径**写进给协调 Agent 的指令。
- **统一输入归一化层（v0.15）**：所有入口共用一套「脏输入 → 标准表示」实现 ——
  内容嗅探优先于扩展名（`.txt` 里是 SDF、`.dat` 里是 PDB 都能识别），支持 gzip/zip 压缩包、
  GBK/UTF-16 等编码自动判定、CSV/TSV 分隔符与中英文表头别名（`名称/编号/化合物`、`smiles/结构/inchikey`）、
  多记录 SDF/MOL2、逐行容错（坏行记行号与原因，不中断整库）、按规范 SMILES 去重并**保留原始 ID 与全部别名**。
  配体标准表示为 `{id, name, smiles, source_file, source_index, raw}`，SDF 的 `_Name` 会作为 ID
  贯穿属性评估 / 对接 / 排序 CSV / 报告（用户要求「输出带上小分子的 ID」）。每次归一化都会写入运行产物
  `input_normalization.json`（前端「中间数据」可下载），上传响应的 `input_normalization` 与之一致，
  且 `count` 与后续 `import_molecule_library` 读到的分子数**相同**。受体侧内容不像结构时给出可操作的错误，
  不会静默回退默认受体。详见 `docs/api.md` §9.5。
- **多轮连续对话**：系统反问后**直接回答即可**——同一段对话里的追问与回答会延续上一轮上下文
  （服务端按稳定的 `conversation_id` 记住历史，受理层还会继承上一轮已给出的分子/受体），不会再另开一段对话、
  也不会把同一个问题重复问一遍。点「新对话」按钮会清空气泡并换一个新会话 id（上一段上下文不带入）；
  会话 id 存在浏览器 `localStorage` 里，**刷新页面后继续聊**。默认 `CHECKPOINT_BACKEND=memory` 时记忆只在进程内，
  **重启服务会丢失多轮上下文**（运行记录已落盘，不受影响）；要跨重启保留请设 `CHECKPOINT_BACKEND=sqlite`。
- **导出条（结果与产物面板右上）**：`报告 PDF` / `报告 MD` / `排序 CSV` / `位姿 ZIP` / `打包下载`，
  按钮旁显示本次运行的**规范文件名前缀**；文件名由后端统一下发（见下），前端不自己拼。
- **报告**：固定 9 章（含「方法与局限」「失败与跳过」）+ `report.pdf`（PDF 版含封面「工具与版本」、
  章节分页、页脚 `run id · 页码 · 生成时间`、内嵌真实图表）；图/表统一编号与题注，正文不含裸 URL；
  报告页有**章节目录**与 run id / 生成时间 / 受体 / 引擎 / 种子抬头（从 Markdown 标题与首表生成，点击平滑跳转）。
- **下载文件命名规范**：`dock_{run_id}_{受体}_{N}mols_{kind}.{ext}`，例如
  `dock_20260914-164720-6383_thrombin_1DWC_2mols_report.pdf`；`kind` ∈
  `report.pdf` / `report.md` / `ranking.csv` / `data.zip` / `poses.zip`。
  受体名只保留 `[A-Za-z0-9._-]`、截断 24 字符（中文/空格/括号降级为 `_`），分子数未知时省略计数段。
  该规则只有一处实现（`runs.download_name()`），并通过 `GET /api/runs/{id}` 的 `downloads` 字段下发给界面，
  避免前后端两套命名逻辑漂移。

### 多 Agent 协作机制

| 机制 | 说明 |
| --- | --- |
| **受理与编排分离** | 「理解用户要什么」与「怎么做到」由两层负责：**任务受理层（intake）**把指令+表单解析成结构化**任务规约**（含 `task_type` / `authority` / `decision` / 假设 / 缺失项），**协调 Agent 只吃规约**做编排。这样「跑还是问」由受理层显式判定，不再出现「含糊就先问用户」与「分子库非空就必须跑完、不得询问」两条规则互相打架；受理逻辑也从三处（API 拼消息 / 提示词两节 / 工具说明）收敛到一处 |
| **共享黑板** | 运行级公共工作区。属性 Agent 写入规范化去重后的分子库，**口袋分析 Agent 写入预测口袋与选定的对接盒**，Docking Agent **直接取用**并把对接结果写回，结合模式 Agent 读取对接结果做**交叉核验**；各 Agent 的判断记入 `notes`（界面展示为「协作记录」） |
| **工具定盒（不靠模型猜坐标）** | 对接盒由**成熟工具**确定：P2Rank（本地部署，机器学习口袋预测）优先，未安装时用**内置几何法**（表面层网格 + 埋藏/包封/疏水特征聚类，已用共晶配体客观标定）；有实验位点（共晶配体/注册表标注）时以实验位点为准，工具预测作为**独立验证**（一致/不一致都记录）；详见 §11.1 |
| **子 Agent 决策空间** | 不再是「只有一个工具 + 原样复述」：属性 Agent 有 2 个工具（可自主决定先规范化再评估）、**口袋分析 Agent 有 5 个**（预测口袋、与实验位点比对、提交位点、查引擎、查受体）、Docking Agent 有 3 个（可自主选受体、必要时在线获取结构）、结合模式 Agent 有 3 个（可自主做跨 Agent 核验）；输出为一个 JSON，可带 `agent_note` 说明判断 |
| **分发边界校验 + 重试** | 协调 Agent 侧校验子 Agent 返回的 JSON 与关键字段，失败**带纠正提示自动重试一次**；两次都失败返回 `agent_output_invalid`，而不是把垃圾文本交上去 |
| **控制权在主管 Agent** | 整条流程（何时分发、发给谁、并行还是串行、要不要重试、最后是否出报告）由协调 Agent 自己决策；服务端**不再**事后接管「自动补齐」，也不再跑独立的复核流程。服务端只做两件事：**记录**实际完成了什么（`completeness`，仅信息用途）与**接收**它自己产出的数据 |
| **每角色独立模型实例** | **6 个角色**（`intake` 受理 / `coordinator` 主管 / `property` / `pocket` / `docking` / `binding`）**各自构建独立的 LLM 实例**（独立登记、可分别配置模型/端点/温度，见「配置 LLM」），不再共用同一个模型对象；4 个子 Agent 另有独立 checkpointer，短期记忆互不干扰 |
| **真并行** | 同一轮的多个子 Agent 分发是**真并行**（实测两个各 3s 的嵌套调用总耗时 3.0s） |
| **受众分离的信息传递** | 工具产物保留全量明细（报告/审计读它），回给模型的载荷只带决策所需字段：逐分子质子化溯源压成 `policy/applied/engine/版本/pH/电荷`（`method`/`variants`/`rules` 留在产物），告警最多 2 条，受体块去掉报告专用的 `box_atom_stats`。实测 3 分子载荷：属性 −45%、对接 −49%、结合模式 −20%。子 Agent 的回执另有契约级约束：`agent_note` 由 pydantic 校验器压成「一句、≤80 字」 |
| **条件纪律段** | 系统提示词每次模型调用都要重发，「执行纪律」占全文 55%，但单次运行通常只用得到几条。`sp` 用 `<!-- block:KEY -->` 标记出四段（上传处理 / 受体纪律 / 两阶段漏斗 / 特殊体系），协调 Agent 的动态提示词中间件按运行事实抽掉不相关的段，注入清单随 `result.prompt_blocks` 落盘。安全方向写死：**没有标记→全文、事实未知→注入、组装异常→全文**。实测常见小库每次调用省 ≈2,549 tokens |

> **如实说明仍然存在的局限**：没有「辩论 / 投票 / 共识」式多方案择优（这是产品取向：本系统的
> 正确性由真实计算工具 + 确定性核验保证，不靠模型投票）。横向协作目前是「黑板共享数据 + 一次
> 交叉核验」，尚未多轮协商。另外，Agent 层的主要价值在自然语言入口与自适应编排——
> **数据正确性并不依赖模型**：所有数值来自真实计算工具，融合排序是确定性的；流程控制权完全在
> 主管 Agent（服务端不事后接管、不做自动补齐），主管的产出由产物完整性与 §13 的门禁脚本验收。
>
> **开发者请注意**：架构、关键不变量、扩展步骤、测试门禁与改进路线图见
> [`docs/architecture.md`](docs/architecture.md)（AI 开发手册）；接口与各子系统契约见
> [`docs/api.md`](docs/api.md)。

---


## 1. 快速开始（一键启动）

> **支持的安装方式**（审计 3.8）：**源码检出 + `pip install -e .`（editable）**。
> `setup.sh` 与 `start.sh` 走的都是 editable 安装；**非 editable 的 `pip install .` 不提供支持**——
> `web/`、`config/`、`assets/`、`var/` 都是运行期读写的目录，不适合塞进 site-packages。
> 装错时服务入口会当场拒绝启动（`paths.assert_runtime_layout(strict=True)`），
> 而不是等你遇到「前端 404」。回归见 `tests/test_packaging.py`。

**新机器三条命令（开箱即用）**：

```bash
tar xzf docking-agent-local.tar.gz && cd projects   # 1) 解包（源码包约 5 MB，不含缓存/用户数据）
bash scripts/doctor.sh                              # 2) 能力自检：有什么、缺什么、缺了降级成什么
bash start.sh                                       # 3) 装依赖（首次）→ 起服务 → 打开网页
```

- `doctor.sh` 退出码：`0` 全能力 / `2` 仅降级（核心流程仍可跑）/ `1` 缺必需组件；
  `--json` 供 CI 使用，`--online` 额外探测 LLM 端点。
- 源码包与部署包的分工见 [`docs/adr/0001-agent-surface.md`](docs/adr/0001-agent-surface.md)
  （产品面 vs 平台面），打包规则见 `scripts/pack.sh`（`--list` 预演）。

```bash
cd projects
bash start.sh
```

`start.sh` 会自动完成：创建虚拟环境 → 安装依赖（首次，几分钟）→ 加载 `.env` → 选择可用端口 →
启动服务 → **自动打开浏览器**。

```
http://127.0.0.1:5000/          # 交互式网页
```

常用参数：

```bash
bash start.sh --port 8000       # 指定端口（默认 5000，被占用时自动顺延）
bash start.sh --no-browser      # 不自动打开浏览器
bash start.sh --check           # 只做环境自检（跑一次真实对接，不启动服务）
bash start.sh --setup           # 强制重装依赖
```

### 环境能力矩阵（缺什么 → 降级成什么）

`bash scripts/doctor.sh` 会逐项列出；下表是"缺了会怎样"的完整口径（**不静默**：降级都写进报告）：

| 组件 | 必需 | 缺失后的行为 |
| --- | --- | --- |
| Python 3.12+、`rdkit`/`vina`/`meeko`/`fastapi`/`uvicorn`/`pydantic`/`matplotlib` | ✅ 必需 | `doctor.sh` 退出码 1，按提示 `bash start.sh --setup` |
| `var/`、`assets/` 可写 | ✅ 必需 | 退出码 1（检查目录权限 / `DOCKING_WORKSPACE`） |
| Java（JRE 8+） | 可选 | P2Rank 无法运行 → 口袋分析用**内置几何法** |
| P2Rank | 可选 | 同上；补齐：`bash scripts/fetch_tools.sh`（工具包 290 MB，不进交付包）或设 `P2RANK_HOME` |
| pdb2pqr | 可选 | 受体**不按目标 pH 重算**质子化态（报告明确告警）；补齐：设 `PDB2PQR_BIN` |
| Dimorphite-DL（配体 pKa 引擎） | 可选 | 配体 pH 处理**回退内置 pKa 规则表**（近似，非 pKa 预测；报告与运行笔记如实说明）；补齐见下方「质子化态（pKa）专业处理」 |
| `autodock4`/`autogrid4` | 可选 | 只有 Vina 可用（`engine=autodock` 会失败）；`apt-get install autodock autogrid` |
| 中文字体（Noto CJK 等） | 可选 | 图表/PDF 中文可能显示为方块；`apt-get install fonts-noto-cjk` |
| LLM（`LLM_API_KEY` + `LLM_BASE_URL`） | **必需** | 对接编排由协调 Agent 驱动；缺失时只能做环境自检与已有记录查看 |
| GPU 对接引擎（用户自行安装） | 可选 | 设置页「外部工具」填写可执行文件路径；填了但检测不通过则**拒绝启动**并提示补齐方法，不会静默改用 CPU |
| 默认端口 5000 空闲 | 可选 | `start.sh` 自动顺延到下一个可用端口（`doctor` 会告知将用哪个） |

内网/本机 LLM 端点下可完全离线运行：上传受体结构文件 + 上传分子库即可
（注册表预置受体 thrombin / trypsin **仅供内部测试**，不是用户可选来源；系统**没有默认受体**）；
只有"点名在线解析受体 / 按名称查分子 / 云端 LLM"需要网络。

### 质子化态（pKa）专业处理

受体与配体都按**同一目标 pH**处理，两侧口径一致；报告会逐分子/逐受体记录用了什么引擎：

| 对象 | 引擎 | 说明 |
| --- | --- | --- |
| **受体** | `pdb2pqr` + **PROPKA** | 以 `--ph-calc-method=propka --with-ph=<pH>` 生成 PQR（逐残基 pKa 判定 His/Asp/Glu/Lys/Cys/Tyr）→ meeko 写 PDBQT。PROPKA 摘要与 PQR 随产物交付；His 实际状态（HID/HIE/HIP）从 PQR 氢原子读出 |
| **配体** | **Dimorphite-DL**（可选，装了就用） | RDKit 的 pKa 规则库，按目标 pH 枚举可电离官能团的微观态。窗口内多微观态时按「\|净电荷\| 最小 → 带电原子最少 → 字典序」选定一个形式对接，规则与全部候选写进 `docking.json` 的 `ligand_facts.protonation` |
| 配体（回退） | 内置 pKa 规则表 | Dimorphite 不可用时自动回退（近似，**不是 pKa 预测**），报告 §6 与运行笔记如实标注并给出安装提示 |

安装专业配体引擎（**必须 `--no-deps`**：Dimorphite-DL 的元数据把 `rdkit` 钉在 `<2026`，
而本项目需要 `rdkit>=2026.3.6`；实测 2.0.2 与 2026.3.6 兼容，用 `--no-deps` 可避免降级 RDKit）：

```bash
cd projects
uv pip install --python .venv/bin/python loguru
uv pip install --python .venv/bin/python --no-deps dimorphite-dl
.venv/bin/python scripts/doctor_probe.py | grep pKa     # 确认已识别
```

相关开关：`LIGAND_PROTONATION`（`ph` / `neutralize` / `keep`）、`LIGAND_PROTONATION_PH`、
`LIGAND_PKA_ENGINE`（`auto` / `dimorphite` / `rules`）、`LIGAND_PKA_WINDOW`、`LIGAND_PKA_PRECISION`。
设置页「检测外部工具」与 `/api/tools/probe` 都会报告该引擎的可用性与版本。

### 配置 LLM（多 Agent 模式必需）

```bash
cp .env.example .env      # 首次
```

```dotenv
LLM_API_KEY=sk-xxxx
LLM_BASE_URL=https://api.deepseek.com     # 任意 OpenAI 兼容端点
LLM_MODEL=deepseek-flash
```

> 对接流程需要 LLM：表单参数为权威参数，但执行由协调 Agent 分发工具完成，
> 因此没有可用的 OpenAI 兼容端点时无法发起对接。

#### 每个 Agent 用各自的模型（可选，已支持）

任务受理、协调 Agent 与 4 个子 Agent（属性评估 / 口袋分析 / 对接执行 / 结合模式）**各自持有独立的
LLM 实例**：不共用同一个模型对象，可以分别指定模型、端点、温度、超时。

两种配置方式（**角色环境变量 > 配置文件 `roles` 段 > 全局环境变量 > 配置文件 `config` 段**）：

```dotenv
# 方式一：环境变量，把全局变量名加上 _<角色大写> 后缀
LLM_MODEL_DOCKING=deepseek-v4-pro      # 对接 Agent 用更强的模型做受体/参数决策
LLM_MODEL_PROPERTY=deepseek-flash      # 属性评估用快模型
LLM_TEMPERATURE_PROPERTY=0             # 抽取类任务用确定性采样
LLM_BASE_URL_BINDING=...               # 甚至可以让某个角色走另一个端点
LLM_API_KEY_INTAKE=...                 # 受理层可用另一把 key / 另一个厂商模型（例如更快的）
```

```jsonc
// 方式二：config/agent_llm_config.json
"roles": {
  "docking":  { "model": "deepseek-v4-pro", "temperature": 0.1 },
  "property": { "temperature": 0.0 },
  "binding":  { "temperature": 0.0 },
  "pocket":   { "temperature": 0 }
}
```

可用的角色名：`intake` / `coordinator` / `property` / `pocket` / `docking` / `binding`；
可覆盖字段：`model` `base_url` `api_key` `temperature` `top_p` `timeout` `thinking`
`max_tokens` `extra_body`。未配置的角色继承全局设置。

- `GET /api/health` 的 `roles` 字段返回每个角色**最终生效**的模型配置；
- 每次多 Agent 运行会把各角色实际使用的模型写入运行记录（`agent_models`，含服务端回执确认的
  `actual_model` 与调用次数 `calls`）并显示在报告首表「各 Agent 模型」一行，可逐次核查。

> **更推荐用网页的「设置」页面改这些**（见 §2.1）：界面会显示每个字段当前生效值的**来源**，
> 保存后立即生效，不需要手动编辑 `.env` / JSON。上面的环境变量写法适合脚本与 CI。

### 任务受理层（intake）与编排层的分工

| 层 | 职责 | 实现 | 是否用模型 |
| --- | --- | --- | --- |
| **受理（intake）** | 理解用户要什么：判定任务类型、谁是权威（表单/指令）、是否需要向用户确认、把指令里提到的分子/受体认出来；产出**任务规约** | `src/docking_agent/intake.py` | **确定性优先**：表单/SMILES/受体名/坐标/参数一律走规则；只有「对话模式 + 自然语言」才调用一次受理模型（角色 `intake`，温度 0） |
| **编排（coordinator）** | 怎么做到：选受体与位点（未给坐标时先 `run_pocket_analysis`）、决定子 Agent 顺序与并行、失败重试、按 smiles 汇总排序、调用报告工具 | `agents/coordinator.py` + 提示词 | 用模型（角色 `coordinator`） |

**`decision` 是这一层的核心字段**（编排层无条件服从）：

| decision | 含义 | 编排层行为 |
| --- | --- | --- |
| `run` | **受体与分子库都已指定**，输入合法且能对接 | **必须完整跑完**（导入 → 属性 → 对接 → 结合模式（有对照时）→ 报告），不得中途询问；唯一例外是点名受体待解析（`receptor.source="named"`），必须先自动解析 |
| `ask` | ① 用户完全没指定受体（`receptor.source="default"`，系统**没有**默认受体）；② 受体**自动解析真失败/歧义**（`receptor.source="unresolved"`）；③ 没有任何可识别的分子来源且未明确同意用示例库 | 只说明缺什么并提问，**不调用任何工具** |
| `reject` | 超出系统能力（闲聊/无关请求） | 友好说明本系统能做什么，**不调用任何工具** |

> 关键设计：**必需项 = 受体 / 分子库 / 任务类型；必需信息不齐 → 只提问、零工具调用。**
> 受体必须由用户指定（PDB 编号 / UniProt accession / 基因或蛋白名 / 上传结构文件），
> 分子库必须由用户提供（或用户**明确**同意用内置示例库）；**系统没有默认受体**，预置受体只用于内部测试。
> 位点由口袋工具自动确定，阳性对照与对接参数仍有默认值。`missing` 是提问与报告用的记录，
> `decision=run` 因此要求受体与分子库**都**已指定。
> （这条规则来自实测：`scripts/intake_eval.py --live` 曾抓到受理模型把 5 个本该 `run` 的用例判成 `ask`，
> 直接违反「输入合法就必须跑完」的产品底线；现在反过来——必需项缺失时必须 `ask`，回归用例同时看护两侧。）

**点名了受体 → 先自动去在线数据库解析，不确定才让用户选（v0.11）。**
用户说「从在线数据库中获取植物去甲基化酶ROS1」时，系统**不会**再反问「请给我 PDB ID」，而是：

| 指令 | 受理层判定 | 行为 |
| --- | --- | --- |
| 完全没提受体 | `receptor.source="default"` | **`decision="ask"`，零计算**：提问并给出三条出路 —— ① PDB 编号或 UniProt accession；② 受体的基因/蛋白名（中英文，系统在线检索）；③ 上传受体结构文件（`.pdb/.cif/.pdbqt`）。预置受体**不再**作为可选项（提问文案里连示例编号都不给：助手回复里的示例编号曾被下一轮当成用户指定） |
| 上传的受体文件**不可用** | 准备失败（格式/内容/缺模板）或文件不存在 | **报错并提问，零计算**：工具返回 `needs_user_input`（含失败原因 + 三选一：换文件 / 给 PDB 号或名称由系统在线解析 / 修正后重试），并把本次受体标为 `unresolved` 阻断后续调用 —— **绝不**改用任何预置受体继续跑 |
| 提到 PDB 号 / UniProt accession / 受体名称 / 上传受体文件 | `receptor.source="user"` | 按用户指定的受体跑 |
| 点名了具体受体（如「植物去甲基化酶ROS1」「ROS1」「EGFR」）但字面不是注册表/PDB/accession | `receptor.source="named"` → `decision="run"` | **第一步调用 `fetch_protein_structure` 自动解析**（中英映射 + 物种推断 + UniProt 多策略检索 + RCSB/AlphaFold 结构获取）；高置信唯一候选 → 继续完整流程，报告写明 accession/物种/结构来源 |
| 检索到**多个同样合理的候选**（如只写 `ROS1`：人源激酶 vs 拟南芥去甲基化酶）或**单一候选置信度不足** | 工具把 `receptor.source` 改回 `unresolved`，并下发 `choices` | **不计算**：把候选列成前端可点按钮（accession / 物种 / 蛋白名 / 结构来源 / 打分），用户点选后以同一 `conversation_id` 继续 |
| **一个都没查到** | 工具把 `receptor.source` 改回 `unresolved`，回执带 `attempts` | **不计算**：提问并**逐条列出已尝试的检索**（UniProt accession/基因名/蛋白名、RCSB、AlphaFold），请用户提供 PDB ID/accession/文件 |

> 真实缺陷：用户点名「植物去甲基化1酶」，UniProt accession 直查与基因/蛋白名称检索都无匹配，
> 系统却按「回退默认受体」**继续对接**，把计算对象悄悄换成了凝血酶。现在这属于产品底线：
> **不明确计算对象时绝不计算**，而且「不确定」的两种形态（多个候选 / 查不到）都要把证据交回用户。
> 分发层还有一道代码级护栏：`run_docking` / `run_pocket_analysis` 一旦看到 `receptor.source`
> 是 `unresolved`（点名解析失败）或 `default`（根本没指定受体），会在调用任何对接/口袋引擎
> **之前**返回 `{"status":"needs_user_input","missing":["receptor"], ...}` —— 即使主管 Agent
> 没照提示停下来问，也绝不可能用默认受体把结果算出来。多轮对话同样受此约束。
> 分子侧同理：多组分/配位聚合物（如代森锰锌 Mancozeb）会给出「原始多组分 / Mn-EBDC 单体 /
> Zn-EBDC 单体 / 最大有机片段」的 `choices`，不臆造单一结构。


**可观测性**：受理结论会写进运行记录（`task_spec`）、SSE `start` 事件与报告首表的「任务受理」一行，
所以任何一次运行都能回答「这次任务到底被理解成了什么」。

**开销（实测，同一台机器）**：

| 模式 | 受理层是否调用模型 | 受理耗时 | 编排 Agent 调用次数 |
| --- | --- | --- | --- |
| 参数模式（manual） | **否**（intake 实例根本不构建） | 0.02 ms（纯规则） | 4（与拆分前一致） |
| 对话模式（chat+advanced） | 是，1 次（角色 `intake`） | ≈2.5 s | 4（与拆分前一致） |

**评估**：

```bash
.venv/bin/python scripts/intake_eval.py            # 14 个用例，确定性规则（零模型，毫秒级）
.venv/bin/python scripts/intake_eval.py --live     # 额外跑真实受理模型，打印 LLM 延迟与判定
```

---

## 2. 界面与能力

网页是单页应用（原生 JS，无外部依赖，离线可用），顶部有**导航栏**在两个页面间切换：

| 页面 | hash | 内容 |
| --- | --- | --- |
| **工作台** | `#chat` / `#manual` | 任务配置、多 Agent 编排、执行区、结果总览 / 分子详情 / 报告 / 中间数据 / 历史运行 |
| **设置** | `#/settings` | 全局参数、API 接入、模型管理、各 Agent 模型与调用参数、对接默认值、运行与性能参数 |

导航栏支持 URL 深链接（直接打开 `http://127.0.0.1:PORT/#/settings` 即进设置页）；
设置页有未保存修改时，导航按钮上会出现一个提示点。

### 2.1 设置页面

字段表由服务端 `GET /api/settings` 下发（`specs`），前端不硬编码字段 —— 新增配置项只改一处。

**六个分组**

1. **LLM 接入（全局）**：Base URL、API Key（只写不读，界面不回显明文）、默认模型、
   温度 / top_p / 超时 / 最大 tokens、思考模式、额外请求头与请求体（JSON）；
2. **模型管理**：「拉取可用模型」直接调端点的 `GET /models`；模型输入框带自动补全，
   点击模型标签可一键填入默认模型；并显示本进程已构建的各 Agent 实例（含服务端确认的模型与调用次数）；
3. **各 Agent 模型与调用参数**：6 个角色各一张卡片（模型 / 温度 / top_p / max_tokens / 超时 /
   思考模式 / 独立端点与密钥），每张卡片带**「测试连通性」**（真发一次最小请求，回报服务端确认的
   模型与延迟）与**「全部继承全局」**；
4. **对接默认值**：引擎、搜索强度、位姿数、是否保存位姿、最大分子数、默认阳性对照 ——
   保存后打开工作台时自动预填表单（用户已手动改过的字段不会被覆盖）；
5. **运行与性能参数**：并行进程数、线程数、位姿与图表上限、SSE 批量与节流、运行超时、
   递归上限、日志级别等，保存后对新的运行立即生效；
6. **部署级参数**：`PORT` 由启动命令决定（界面只读）；`DOCKING_MAX_LIGANDS` / `UPLOAD_MAX_MB`
   在 `.env` **已显式设置**时以 `.env` 为准（界面设置不会放宽上限），未设置时才由界面接管。

**每个字段都标注来源**：`界面设置` / `环境变量 LLM_MODEL` / `内置默认` …
保存失败时出错的字段会**标红并聚焦**；改动里含 `CHECKPOINT_BACKEND` 这类启动期参数时会提示
「需要重启服务」。

**优先级**（越具体越优先）：

```
角色环境变量 LLM_<字段>_<角色>
  > 界面设置（该 Agent）        local_settings.json → roles.<角色>
  > 内置角色默认               agent_llm_config.json → roles.<角色>
  > 界面设置（全局）            local_settings.json → llm
  > .env 全局                   LLM_<字段>
  > 内置默认                    agent_llm_config.json → config
```

运行类字段：**界面设置 > .env > 内置默认**（部署保护项反过来，`.env` 优先）。

**保存到哪里**：`config/local_settings.json`（已被 `.gitignore` 与 `scripts/pack.sh` 排除）。
不想手改文件时，页面上还有：

- **重新加载 Agent**：丢弃已构建的 Agent 与模型实例，使模型/端点改动立即生效（不用重启服务）；
- **清除界面设置**：删除该文件，回到 `.env` + 内置默认。

> 保存模型/端点类设置后，页面会**自动重载 Agent**，所以「改完直接跑」即可。
> 只有 `CHECKPOINT_BACKEND` / `ARTIFACT_BASE_URL` 这类启动期参数需要重启服务。

**关于密钥的三条约定**（设置页会回显配置状态，但不下发明文）：

- `LLM_API_KEY` 与各角色的 `api_key`：**只写不读**，界面只显示「已配置 + 首尾 4 位掩码」；
- `额外请求头` 里名字含 `key` / `token` / `auth` / `secret` 的项，值只回显 `***`；
  在界面上原样保留 `***` 提交 = 不修改原值；
- 调用上游失败时返回的错误信息会**脱敏**（`sk-…`、`Bearer …` 等被替换），
  避免因上游把请求头回显在错误体里而泄露密钥。

> **安全提示**：本服务默认只监听 `127.0.0.1`（`bash start.sh` 与本机浏览器访问）。
> 服务本身**没有鉴权**，而设置接口可以改端点、`/api/settings/test` 会带当前密钥发请求，
> 因此**不要暴露到不可信网络**。确需局域网访问时用
> `python -m docking_agent -m http --host 0.0.0.0`，并自行加反向代理鉴权 / 防火墙限制。
>
> 除默认回环监听外，还有几道与监听地址无关的守卫（详见 `docs/api.md` §12「安全边界」）：
> **写请求同源校验**（带 `Origin` 且与 `Host` 不同源 → 403，可用 `DOCKING_ALLOWED_ORIGINS` 声明
> 反向代理来源）、**CSP 与安全响应头**、**run/线程/会话 id 白名单**（防路径穿越）、
> **「校验文件」限定在 `assets/uploads` 之下**（防任意文件读取）。这些只降低误暴露的代价，
> **不能替代鉴权**。

### 2.2 工作台

**三栏布局（v0.31）**：左栏=参数设置（对话模式参数条 / 参数模式表单 / 其他设置 / 上传受体），
中栏=对话与结果与产物（对话气泡、排序、报告、图表、中间数据、历史），右栏=运行详情
（多 Agent 编排时间轴、阶段日志、工具轨迹、**实时逐分子结果**）。左/右栏顶部都有「收起/展开」，
状态记在本地；窄屏（≤1180px）右栏下沉到中栏之下，≤900px 收成单栏，任何宽度都不出现横向滚动。

**历史任务检索**：结果区的「历史运行」页签支持按关键词（run_id / 受体 / 分子名或 ID，
空格分词为「与」）、状态、类型、受体、时间范围检索并分页；数据全部来自服务端持久化的
`var/runs/`，因此**关闭页面后再打开仍能查到并载入**之前的运行（上次检索条件存在本地）。

配置区分为**两个子页**，彻底避免「聊天指令」与「运行参数」互相冲突：

| 子页 | 用途 | 参数如何生效 |
| --- | --- | --- |
| **对话模式** | 用自然语言向协调 Agent 下达指令；带对话历史；「高级设置」**默认折叠**（**不含受体选择**——受体由指令/系统默认决定，但可上传受体文件） | 折叠时使用**系统默认参数**；展开后使用**高级设置里填写的参数**。两种情况都是「指令已明确指定的以指令为准」 |
| **参数模式** | 手动填写全部参数（受体+已知位点、配体来源、阳性对照、引擎、搜索强度…）+ 可选「目标描述」 | 表单参数是**权威来源**；**目标描述可留空**，系统会自动补全完整任务描述并跑完整流程 |

> **支持上传文件**（所有模式）：小分子库支持 `SDF/SMI/SMILES/CSV/MOL2/MOL`，蛋白质支持 `PDB/PDBQT`；
> **上传只保存文件，不在后台上处理**（v0.22，用户要求）：解析分子数、现场准备受体（含按目标 pH
> 处理受体质子化）、标定位点盒都发生在**你点「校验文件」**或**点「开始运行」**时。
> 想先看一眼再跑，点文件卡片里的「校验文件（解析预览）」。
>
> **阳性对照是可选项**：留空则**跳过**对照分子对接与结合模式比较（结果中如实标注，图表不画对照线）；
> 只有填写了阳性对照 SMILES 才会执行对照分析。

两个子页共用下方结果区：

| 区域 | 内容 |
| --- | --- |
| **任务配置** | **受体来源输入框**（填写 PDB 编号 / UniProt accession / 受体名称，或**上传受体文件**；表单从不预填注册表受体，留空且未上传时校验直接拦下） ；配体来源**四选一**（SMILES 文本 / **上传文件** / 服务端路径或 URL / 示例库）；阳性对照；引擎与搜索强度；表单参数为权威参数，由协调 Agent 执行 |
| **协作记录** | 展示共享黑板统计与各 Agent 的协作备注（多 Agent 模式） |
| **编排示意图** | 实时显示「整体协调 Agent → 口袋分析 / 分子属性评估 / Docking 执行 / 结合模式检测 → 报告生成」各节点状态与**当前任务**；并带一条**实测时间轴**：按服务端事件时间戳（`ts`）绘制每个节点的真实起止条 —— **条带重叠即并行、依次排列即串行**，顶栏徽标给出实测并行度（如 `[ 并行 ×2 ]`）。实测样例：`属性评估 12.9→17.4s` 与 `对接 13.1→23.1s` 重叠 4.3 s（并行），而口袋分析→属性评估、对接→结合模式→报告之间不重叠（串行） |
| **执行** | `■ 停止` 按钮（**真正中断后端对接**，见 §8）；阶段进度条（`已完成 x/y（z%）`、已用时间、**剩余时间 ETA**）、**逐分子实时结果**（批量推送，边算边出；实时表最多保留最近 300 行）、多 Agent 模式下的模型增量文本与工具调用轨迹、可中止运行 |
| **结果总览** | **服务端分页**排名表（每页 50/100/200，表头点击即全库排序），搜索框、「只看优于阳性对照」、聚合统计（命中数/区间/均值/中位数）、「导出完整 CSV」；图表含对接对比图、相似度图、**亲和力分布直方图**、理化性质空间图 |
| **分子详情** | 分页渲染（每页 12/24/48 张），每个分子一张卡片：RDKit 二维结构图、理化性质表、对接能量明细（total / intermolecular / intramolecular / torsional）、**结合模式分析（Morgan 与 MACCS 双指纹相似度、结构一致性、药效团锚定基团匹配、性质差异、结合模式提示）**、位姿下载 |
| **两种界面（同一后端契约）** | **简易模式** `/`（**默认首页**，别名 `/simple`）：只有「对话 + 结果」两块，零参数表单 —— 新手一句话就能跑（质子化态/搜索强度/位点盒全部由默认值与受理层自动规划决定），结果区给前 5 名 + 关键指标 + 报告/CSV/整包链接（**刷新页面后结果区保持空态**，往次结果从「历史运行」显式载入，不把上一次的结果留在屏幕上）；**高级模式** `/advanced`：参数表单、历史检索、报告全文、中间数据与设置页。顶栏一键互切，两套界面都走标准 Agent Protocol（`/threads/{tid}/runs/stream`）与同一批 `/api/*` 产物接口 |
| **动态背景（简易模式）** | 纯 CSS 渐变光斑 + 遮罩网格，只用 `transform/opacity` 动画，无外部资源、无 JS 定时器，`prefers-reduced-motion` 下完全停止；`pointer-events: none` 保证不吃点击 |
| **对话回复** | **只给简短总结**（理解与分配 / 实际执行了什么 / 最关键数值与推荐前 3 名 / 风险与局限 / 产物链接），明细一律在报告产物里 —— 贪多的聊天长文与报告重复，用户看到的是「同一批内容出现两次」；助手回执**按 Markdown 渲染**（标题 / 列表 / 表格 / 代码块 / 安全链接），流式阶段就是渲染后的样子，不会先给用户看一屏 `## / ** / \|` 源码 |
| **思考折叠** | 模型的思维链（`thinking` 领域事件，走 `custom` 帧）**不进正文**：两套界面都收进气泡内的「思考」块，交互同市面主流 —— **推理流式期间展开可见（「思考中 · N 字」）**，**正文一开始就自动收起**成「思考 · N 字 · 用时 X 秒」，随时点开可看全文。**只有推理默认收起；助手回执与报告块一律全文显示**（不再有 24 行 / 1800 字的「展开全文」截断） |
| **渲染只有一份实现** | Markdown → DOM 的渲染器（含 HTML 先转义、链接/图片方案白名单）是 `web/markdown.js` 里的**唯一实现**，`index.html` 与 `simple.html` 都在各自客户端之前加载它 —— 两套界面的渲染规则与 XSS 姿态不会发散 |
| **少打扰用户（产品准则）** | 系统尽可能自动处理：**只有影响对接本身的问题**才让用户决定（受体不可用/歧义、多组分分子的代表结构、共晶配体是否作对照）。执行细节一律自动 —— 跑满步数自动放宽上限（`RECURSION_LIMIT` ×2 ×4，默认 480 封顶）并从 checkpoint 继续；到顶则让主管 Agent 用**已有结果**收尾，照常出报告，**不把 `GRAPH_RECURSION_LIMIT` / 工具异常这类执行错误抛给用户** |
| **候选选择（点选）** | 需要用户决定时（多组分分子的代表结构 / 受体歧义 / 共晶配体作对照）下发结构化选项：**一个问题只问一次、只显示一份** —— 服务端同一问题只认第一次发布；客户端按 `kind` 分组，后到的问题不覆盖先到的；**候选等本轮模型输出结束才挂出**（模型还在输出时点选会与运行抢跑，选择可能取不到）；运行中按钮点不动；刷新或载入历史运行按 `run.choices` 补挂。停下等选择的状态是 `needs_user_input`（`[ ASK ]`），不混进 `no_op`（`[ SKIP ]`） |
| **报告** | **交付文案**：正文不含过程性元话与自我声明，同一件事只写一次（质子化口径 / 同阶段参数一致 / 盒子一致性各只出现在最相关的一节），协调 Agent 的文字进第 8 节前先剥掉「需求理解 / 实际执行 / 报告与产物」等复读小节，只留结论与风险；实测同一运行由 17.9k 字符降到 11.7k（−35%），事实与溯源一项未删 |
| **受体溯源** | 在线解析得到的 accession / 物种 / PDB 号 / 实验方法与分辨率由 `fetch_protein_structure` 记为**运行事实**并随 `result.json` 落盘，报告 §1.2 自己写明「受体来源」一行（不依赖协调 Agent 在结论里复述） |
| **报告骨架** | **固定格式** Markdown 报告（**9** 个固定章节：任务与参数 / 结果排序 / **推荐分子（含 3.1 推荐化合物排行：综合分 + Agent 理由 + 筛选建议 / 3.2 优于阳性对照）** / 理化性质 / 结合模式 / 方法与局限 / 失败与跳过 / 结论与建议 / 数据与产物），**图片以相对路径直接内嵌显示**：对接对比图、亲和力分布、相似度图、**推荐化合物 2D 结构图（PDF 同样内嵌）**、性质空间图（只画推荐 top）、结合模式散点图、分子结构对比网格；图/表带连续编号与题注；第 8 节只摘录 Agent 的结论/建议，**不重复正文数据** |
| **中间数据** | 本次运行的**全部产物清单**（含类型与大小）+ 单项下载 + 整包 `zip` + 位姿 `zip` |
| **历史运行** | 历次运行列表，点击即可回看结果、报告与中间数据 |

---


## 3. 前端设计规范（科技感 · 编程风格 · 布局稳定）

前端是**深色 IDE / 可观测性控制台**基调的单页应用（原生 JS，零外部依赖）。

### 设计令牌（`web/styles.css` 的 `:root`）

| 类别 | 令牌 |
| --- | --- |
| 底色与面板 | `--bg #0b0e14`、`--bg-deep`、`--panel`、`--panel-2/3`、`--border` / `--border-soft` / `--border-strong` |
| 文本层级 | `--text`、`--text-dim`、`--text-mute`、`--text-faint` |
| 强调色（**全站唯一**） | `--accent #22d3ee` + `--accent-dim/soft/line/ring` |
| 语义色 | `--ok`、`--warn`、`--err`、`--hit`（各带 soft/line 变体） |
| 亲和力梯度 | `--aff-strong/good/mid/weak`（表格着色与图例共用） |
| 字体 | `--font-mono`（数字/ID/SMILES/日志/代码，**tabular-nums 不跳动**）、`--font-sans`（正文，含 CJK 回退） |
| 间距 / 字号 / 圆角 | `--sp-1…6`（4 的倍数）、`--fs-2xs…xl`、`--r-xs…pill` |
| 动效 | `--dur .18s`、`--ease`；`prefers-reduced-motion` 下全部关闭 |
| 预留高度 | `--log-h`、`--stream-h`、`--chat-h`、`--panel-min-h` |

### 布局稳定的硬性规则（任何信息流都不许「拉扯」布局）

1. flex/grid 子项一律 `min-width: 0`，容器用 `minmax(0, 1fr)`。
2. 长文本（SMILES / 分子名 / 路径 / 错误）`overflow-wrap` 断行或 `text-overflow: ellipsis` 截断 + `title` 提示。
3. 表格：外层 `overflow-x: auto` + `table-layout: fixed`，列宽稳定不被内容拉扯。
4. 日志 / 模型流 / 对话流 / 实时表：**显式定高 + 内部滚动**，内容增长只在该区域滚动，绝不推高整页。
5. 进度与计数区预留固定高度，数字位数变化不引起跳动。
6. 窄屏（`≤1000px`）单列堆叠且不横向溢出。

### 运行编排示意图（本轮新增）

执行区上方常驻一块**编排示意图**，实时显示多 Agent 协作状态：

```
        ┌──────────────────┐
        │  整体协调 Agent   │  [ RUN ]  正在分发任务
        └────────┬─────────┘
   ┌─────────────┼─────────────┬──────────────┐
┌──┴───┐    ┌────┴────┐   ┌────┴────┐   ┌─────┴─────┐
│属性评估│    │Docking  │   │结合模式 │   │ 报告生成  │
│[ OK ] │    │[ RUN ]  │   │[ SKIP ] │   │ [ WAIT ]  │
└───────┘    └─────────┘   └─────────┘   └───────────┘
当前任务：Docking 执行 Agent · 已完成 120/126（95%）  ETA 00:18
```

- 节点状态用**方括号标签**：`[ WAIT ] [ RUN ] [ OK ] [ SKIP ] [ FAIL ] [ CANCEL ]`，运行中节点有脉冲动效；
- **状态映射**：由 `tool_call` / `tool_result` 与阶段事件驱动时间轴与进度
  （`run_property_assessment`→属性评估、`run_docking`→Docking 执行、`run_binding_mode_analysis`→结合模式、
  `generate_screening_report`→报告生成、`import_molecule_library`/`fetch_*`→协调 Agent）；
- 后端在跳过结合模式分析时会下发 `"skipped": true`，示意图据此显示 `[ SKIP ]`（历史回看同样正确）；
- 指标条显示：已完成/总数、百分比、已用时间、ETA。

### 停止按钮（真正中断）

运行中显示 `■ 停止`：点击后**先**调用 `POST /api/runs/{id}/cancel`（后端协作式取消，终止对接进程池），
再对本地流做 `AbortController.abort()`，并留一小段窗口等待 `cancelled`/`done` 到达；
界面进入「正在停止…」，收到 `cancelled` 后标记 `[ CANCEL ]` 并提示已取消。

### 终端风格元素

- 进度：等宽字符条 `[████████░░░░] 42%  120/126  ETA 00:18`；
- 日志/流式输出：行首时间戳 + `›` 提示符；
- 区块标题：等宽大写 + 字距（`── 执行 ─────`）；
- 数值/ID/SMILES：等宽 + `tabular-nums`，数值右对齐。

### 模式切换（对话 ⇄ 参数）

两个子页**同格叠放**（`.mode-panels` 单列 grid + `min-height: var(--panel-min-h)`），
通过 `.is-active` 类切换 `opacity` + `translateY(4px)`，
并用 `visibility 0s linear var(--dur)` 延迟隐藏——**不使用 `display: none` 硬切**，
因此切换是交叉淡入、结果区不发生跳动，且尊重 `prefers-reduced-motion`。
模式支持 URL hash 深链接：`#chat` / `#manual`。

### 工具提示与紧凑参数栅格（v0.14）

- **触发方式**：任意元素加 `data-tip="一句话"` 或 `data-tip-html="<b>/<br>/<code> 白名单 HTML"`；
  字段旁的 `(?)` 图标为 `<span class="tip-icon" tabindex="0">`，可悬停也可键盘聚焦（Esc 关闭）。
- **气泡实现**：`app.js` 的 `bindTooltips/showTip/hideTip/placeTip` 把 `.tip-box` 挂在 `<body>` 上，
  `position: fixed` + `z-index: 1400`（高于面板层），自动上下翻转并与视口左右夹紧，箭头随锚点定位；
  `tipNodesFromHtml` 用标签白名单重建节点（丢弃一切属性与非白名单标签），避免注入。
- **精简规则**：字段名、必填/错误提示、空状态一句话、关键结论（如「对接盒来源」「库级下限」）留在界面上；
  其余多行解释移入气泡，界面只保留一句必要提示（`check_web.py` 会拦截重新长回来的长段 `.hint`）。
- **高级设置栅格**：`.param-common`（常用，2 列，窄屏单列）+ `.more-body`（更多参数二级折叠）；
  全部复用 `--sp-*` 8px 栅格与既有颜色令牌，不新增颜色体系，不多用边框（用间距 + 极细分隔线替代盒子套盒子）。

### 前端校验

```bash
.venv/bin/python scripts/check_web.py        # 118 项：语法/离线/结构配平/id 对应/布局健壮性/设计令牌/导出条与动效/对话附件契约/多轮会话/工具提示与高级设置栅格
.venv/bin/python scripts/snapshot_ids.py diff # DOM id 清单比对（防误删运行时引用的 id）
node scripts/ui_e2e.js http://127.0.0.1:5102  # 143 项：jsdom + 真实后端的 DOM 级行为验证（含预设 / 恢复默认 / 折叠摘要 / 气泡）
```

`scripts/ui_shot.py shot` 新增 `--hover <selector>`：悬停后再截图，用于拍工具提示气泡，例如
`.venv/bin/python scripts/ui_shot.py shot --base http://127.0.0.1:5102 --page manual --hover "label:has(#exhaustiveness) .tip-icon" --out var/tmp/shot/tip.png`。

---


## 4. 大库承载（上千～上万分子）

系统按「上万分子」设计，实测数据如下（本机 32 核）：

| 场景 | 结果 |
| --- | --- |
| 96 分子真实对接（exhaustiveness=1） | 串行 159.8s → **8 进程 28.2s（5.7×）**，结果**逐位一致** |
| 126 分子真实对接（HTTP 全流程） | **9.4s** 完成（含属性/对接/结合模式/图表/报告） |
| 1 万行结果的分页查询 | 首页/末页/排序/搜索 **3–6 ms**，22 KB/100 行 |
| 1 万分子运行的 `GET /api/runs/{id}` | **138.8 KB**（内联截断为前 200 条） |
| 1 万行 CSV 导出 | 0.74 MB，59 ms |

关键设计：

- **网格图复用**：Vina 的 `compute_vina_maps` 与对接本身同量级开销；现在每个 worker 进程对「受体+盒子」**只算一次**，在整批配体间复用。
- **并发规划按部署机器自动推导（v0.17）**：启动/运行时探测**真实可用算力** —— cgroup 配额（容器 `--cpus=4` 不会被当成宿主的 32 核）> CPU 亲和性（cpuset/taskset）> `cpu_count`，再结合物理核与 `MemAvailable`；据此给每个 worker 打满线程（Vina 单分子的线程扩展约 8 线程饱和，这是引擎性质、与机器无关），进程数取 `ceil(可用核/线程数)`。同一份代码在 4C/8T 笔记本自动得到 3×3、容器限 4 核得到 3×1、64C/128T 得到 16×8，无需人工调参；`GET /api/health` 的 `machine` 与 `docking_plan` 会打出识别结果与来源，运行日志也会记一行。**线程优先**比旧的「多进程 × 1 线程」在 64 分子上**快 1.83×**、总 CPU 少 29%（38.5s / 利用率 58% vs 70.4s / 44.6%）。**所有对接都在 worker 进程里跑**，因此任意分子数（含单分子）都能被「停止」秒级杀掉。库里有极个别超大柔性分子时可调小 `DOCKING_THREADS_PER_WORKER`（=更多进程）以缩小长尾阻塞面。
- **实时推送节流**：逐分子结果按 `SSE_BATCH_SIZE`(25) / `SSE_FLUSH_MS`(200ms) 合并为**批量事件**；`progress` 事件每 `SSE_PROGRESS_MS`(1s) 一次并带 ETA。
- **响应体截断**：`GET /api/runs/{id}` 内联的分子库/性质/对接明细/排序都截断为前 `RESULT_INLINE_LIMIT`(200) 条，其余走分页接口；完整数据仍以产物文件提供。
- **图表自适应**：分子数 > `CHART_BAR_MAX`(50) 时自动改为「Top-`CHART_TOP_N`(20) 对比图 + 亲和力分布直方图」，避免上万个柱子不可读。
- **报告截断**：Markdown 只列 Top-`REPORT_TOP_N`(50)，完整结果以 CSV/JSON 交付。
- **位姿策略**：分子数 > `POSE_SAVE_MAX`(5000) 时只补写每个受体最优的前 `POSE_TOP_N`(200) 个位姿；产物清单最多登记 `POSE_ARTIFACT_MAX`(200) 个位姿，其余按 `poses/pose_<名称>.pdbqt` 命名规则**直接按名下载**，避免清单与响应膨胀。
- **界面**：结果表与分子详情全部服务端分页，只创建当前页 DOM；实时表硬上限 300 行；结构图与图表懒加载；超 500 分子时给出「可先用最大分子数做小样本」提示（不阻止运行）。

---


## 5. 命令行

```bash
bash scripts/local_run.sh -m http  -p 5000        # 启动服务（等价 start.sh）
bash scripts/local_run.sh -m runs      # 查看运行记录（对接请从网页发起）
bash scripts/local_run.sh -m flow   --message "用示例库做一次完整筛选"   # 同步多 Agent
bash scripts/local_run.sh -m agent  --message "用示例库做一次完整筛选"   # 流式多 Agent（终端实时输出）
bash scripts/local_run.sh -m runs   --list 10     # 查看历史运行
bash scripts/local_run.sh -m receptors            # 查看受体与已知位点
```

## 6. 项目结构

```
projects/
├── start.sh                    # ★ 一键启动
├── pyproject.toml              # 包元数据 + 依赖 + console script
├── requirements-local.txt      # 本地依赖清单
├── .env / .env.example         # 本地配置（LLM/端口/对接参数）
├── docs/api.md                 # ★ HTTP API 契约（前端/后端共同遵守）
├── docs/adr/                   # ★ 架构决策记录（0001：产品面 / 平台面定位）
├── scripts/doctor.sh           # ★ 环境能力自检（开箱即用第一步）
├── scripts/pack.sh             # ★ 打包源码（--list 预演；默认不含缓存/用户上传）
├── scripts/fetch_tools.sh      #   可选外部工具（P2Rank）按需获取
├── config/
│   ├── agent_llm_config.json   # 内置模型参数 + 协调 Agent 系统提示词
│   ├── local_settings.json     # ★ 设置页面保存的本地覆盖（gitignore，可含 API Key）
│   └── receptors.json          # ★ 受体注册表与【已知结合位点】
├── assets/
│   ├── tools/                  # ★ 可选外部工具（P2Rank 解压到这里即被自动识别）
│   ├── libraries/              # 示例分子库、阳性对照
│   ├── receptors/              # registry/(可对接受体) structures/(原始 PDB)
│   ├── cache/ uploads/         # 下载缓存 / 用户上传
├── src/docking_agent/          # ★ 单一 Python 包
│   ├── cli.py  paths.py  config.py  logging_setup.py
│   ├── settings.py             # ★ 设置页面：字段表 + 读写 + 优先级/来源解析
│   ├── runs.py                 # ★ 运行记录与中间数据仓库
│   ├── core/                   # 真实计算核心（无 LLM 依赖）
│   │   ├── chemistry.py        #   理化性质、Morgan 指纹、结合模式相似度
│   │   ├── pockets.py          # ★ 结合口袋预测（P2Rank 适配 + 内置几何法）与对接盒决策
│   │   ├── ligands.py          #   分子库解析、3D 构象与 PDBQT 准备
│   │   ├── receptors.py        #   受体注册表（已知位点）、用户受体现场准备
│   │   ├── docking.py          #   Vina / AutoDock4 引擎、位姿导出
│   │   ├── ranking.py          #   结果融合与排序
│   │   └── files.py            #   URL/路径归一化
│   ├── reporting/              # 报告与产物（charts / tables / report / store）
│   ├── agents/                 # 多 Agent 编排
│   │   ├── prompts.py          #   子 Agent 提示词（集中管理）
│   │   ├── prompt_blocks.py    #   条件纪律段（按运行事实抽取提示词子集）
│   │   ├── workers.py          #   4 个子 Agent（无状态执行器，各自独立模型实例）
│   │   ├── coordinator.py      #   整体协调 Agent
│   │   └── persistence.py      #   把 Agent 运行的真实工具输出落盘
│   ├── tools/                  # LangChain @tool（供各 Agent 调用）
│   ├── runtime/                # 运行时（context/llm/payload/streaming/errors/checkpoints）
│   └── api/                    # FastAPI 应用与请求模型
├── web/                        # 前端：高级模式（index.html / app.js / styles.css）
│                             #       + 简易模式（simple.html / simple.js / simple.css）
├── scripts/                    # setup / local_run / http_run / smoke_test / verify_docking /
│                               # check_web / snapshot_ids / ui_e2e.js / pack
├── tests/                      # pytest（核心 + API 契约 + 设置 + 协作）
└── var/                        # 运行数据（runs/ logs/ outputs/ cache/，已 gitignore）
```

---


## 7. 中间数据与运行记录（重点）

每一次运行（网页、CLI 均可）都会创建 `var/runs/<run_id>/`：

```
var/runs/20260912-170508-9720/
├── run.json           # 运行元信息：参数/状态/耗时/产物清单/日志
├── request.json       # 本次请求参数（含受体与已知位点）
├── molecules.json     # 解析后的候选分子库
├── properties.json    # 理化性质（RDKit）
├── docking.json       # 对接明细：每个分子的能量项、引擎、搜索强度、位姿路径
├── binding.json       # 结合模式：与阳性对照的指纹相似度与一致性判断
├── ranking.csv        # 排序结果（含 engine / exhaustiveness / Δ vs 对照，可追溯）
├── report.md          # 固定 9 章报告（两种运行模式共用同一模板；图以相对路径内嵌）
├── report.pdf         # PDF 版报告（封面 + 分页 + 页脚；PNG 真正内嵌）
├── charts/            # docking_chart.png / similarity_chart.png / property_chart.png / affinity_histogram.png / binding_scatter.png / structure_grid.png
└── poses/             # 每个分子的最佳位姿（.pdbqt；AutoDock 为 .dlg）
```

获取方式：

- 网页「中间数据」页签：逐项下载 + `下载全部(zip)` + `下载全部位姿(zip)`；
- HTTP：`GET /api/runs`、`GET /api/runs/{id}`、`GET /api/runs/{id}/artifacts/{name}`、
  `GET /api/runs/{id}/download.zip`、`GET /api/runs/{id}/poses.zip`；
- 磁盘：直接读取 `var/runs/<run_id>/`。

---


## 8. HTTP API

完整契约见 **`docs/api.md`**（前端与后端共同遵守）。摘要：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 交互式网页 |
| GET | `/api/health` | 服务状态（LLM 是否配置、可用引擎） |
| GET | `/api/receptors` | **内部/诊断**端点：注册表预置受体与已知结合位点（返回 `"internal": true`；**不是用户可选来源**，网页界面不再调用） |
| GET | `/api/libraries` | 示例分子库与默认阳性对照 |
| POST | `/api/agent/stream` | 多 Agent 协作（SSE：`token` / `tool_call` / `final` / `done`）；`mode=chat\|manual` 决定指令与参数谁是权威 |
| POST | `/api/runs/{id}/cancel` | **取消运行**：协作式取消，会终止对接进程池并停止后续步骤 |
| GET | `/api/runs` `/api/runs/{id}` | 运行列表 / 运行详情（结果、报告、产物清单；大库自动截断内联数据） |
| GET | `/api/runs/{id}/ranking` | **结果分页**：`offset/limit/sort/order/q/hits_only` + 聚合统计 |
| GET | `/api/runs/{id}/export.csv` | 导出完整排序 CSV（产物缺失时现场生成） |
| GET | `/api/runs/{id}/artifacts/{name}` | 单项产物下载（`?inline=1` 用于图片） |
| GET | `/api/runs/{id}/download.zip` `/poses.zip` | 整包 / 位姿打包下载 |
| GET | `/api/molecule/depict` `/properties` | 二维结构图（PNG）/ 单分子理化性质 |
| POST | `/api/uploads` | **上传小分子/蛋白质文件**：校验解析后返回 `path` / `receptor_file`（含位点盒） |
| POST | `/run` `/stream_run` `/cancel/{id}` | 兼容脚本调用的旧接口 |
| POST | `/v1/chat/completions` | OpenAI 兼容接口 |

```bash
# 标准 Agent Protocol：线程 + 运行流（需要 LLM）
curl -N -X POST localhost:5000/threads/$TID/runs/stream -H 'Content-Type: application/json' -d '{
  "receptor":"4HHB","ligands_text":"阿司匹林:CC(=O)Oc1ccccc1C(=O)O",
  "positive_control":"NC(=N)c1ccccc1","exhaustiveness":6,"engine":"vina"}'

# 多 Agent（需要 LLM）
curl -N -X POST localhost:5000/api/agent/stream -H 'Content-Type: application/json' -d '{
  "message":"用示例分子库完成一次完整筛选并给出排序结论",
  "receptor":"4HHB",
  "exhaustiveness":6,"engine":"vina"}'
```

---


## 9. 受体与「已知结合位点」

在 `config/receptors.json` 中声明受体及其已知位点，**新增靶点无需改代码**：

```json
{
  "default": "thrombin",
  "aliases": {"1dwc": "thrombin", "1ptu": "trypsin"},
  "receptors": {
    "thrombin": {
      "pdb": "1DWC",
      "protein": "人α-凝血酶 (human alpha-thrombin, PDB 1DWC)",
      "pdbqt": "assets/receptors/registry/thrombin_1DWC.pdbqt",
      "site": {
        "center": [31.5, 13.74, 24.36],
        "size": [22.0, 22.0, 22.0],
        "source": "共晶配体质心",
        "description": "凝血酶 S1 口袋 / 催化位点",
        "residues": ["HIS57", "ASP102", "SER195"]
      }
    }
  }
}
```

- 注册表内置：`thrombin`(1DWC)、`trypsin`(1PTU)，位点盒已标定 —— **仅供内部测试，不是用户可选来源**，系统**没有默认受体**
  （JSON 里的 `"default"` 只是注册表内部字段，供诊断与解析链内部兜底使用，**不代表用户未指定时就会用它**）；
- 自定义：传 `.pdbqt` 直接使用；传 `.pdb/.ent/.pdb1/.cif/.mmcif`（大小写不敏感，`.ent` 是 RCSB
  坐标文件的常见后缀）会调用 meeko 的 `mk_prepare_receptor.py` 现场准备，
  盒中心优先取共晶配体质心，其次蛋白质心；也可用 `site_center/site_size` 显式覆盖；
  用户提供的结构文件无法准备为受体时（文件不存在、内容不是结构），解析链**内部仍会回退注册表凝血酶**
  并在结果 `notes` 里写明原因与「这不是你指定的受体」（绝不静默回退）——这是解析链内部的兜底，
  **不是「系统默认受体」**：未指定受体时根本不会走到这里，而是直接报错并请用户指定；
- **上传受体**：`POST /api/uploads` **只保存文件**（`pending:true`）；`POST /api/uploads/inspect`
  （用户主动校验）或运行阶段才现场准备为 PDBQT、按目标 pH 处理受体质子化、标定活性位点盒，
  并写入 `.site.json` sidecar —— 之后**只凭该 PDBQT 路径**也能复原活性位点盒
  （否则会退化成全蛋白质心，把盒子放到错误的位点）。
- 在线获取：协调 Agent 的 `fetch_protein_structure` 与 `fetch_molecule_record`（PubChem）。
  - `fetch_protein_structure(source)` 支持四种输入：**PDB 结构号**（如 `3ZBF`）、
    **UniProt accession**（如 `Q9SJQ6`）、**英文基因/蛋白名**（如 `EGFR`、`epidermal growth factor receptor`）、
    **中文点名的受体**（如「植物去甲基化酶ROS1」「植物去甲基化1酶」）；
  - 名称先经 `core/resolve.py` 归一化：抽取基因 token（`ROS1`）、物种线索
    （植物/拟南芥→*Arabidopsis thaliana* taxid 3702、水稻、玉米、human 9606、mouse 10090…）、
    中文家族词→英文（去甲基化酶→demethylase/DNA glycosylase、激酶→kinase、受体→receptor…）；
  - 再按「`gene_exact`+`organism_id` → `gene_exact`+reviewed → `gene_exact` →
    家族词+物种 → 家族词+植物 → 自由文本」多路检索 UniProt 并按规则打分
    （reviewed +40 / 物种精确 +30 / 同为植物 +18 / 物种不符 −25 / 家族词 +20 / 基因精确 +15 / 注释 +9）；
  - **三态**：唯一高置信候选 → 自动继续；多个/低置信候选 → `choices` 让用户选；
    一个都没查到 → 回执逐条列出已尝试检索（`attempts`）。解析不确定时把运行规约标为
    `unresolved`，`run_docking` 护栏随即阻断对接；
  - 结构来源链：UniProt PDB 交叉引用 / **RCSB Search API** 反查的**实验结构**优先；
    没有实验结构或候选结构都准备失败 → **AlphaFold DB** 预测结构（用接口返回的 `pdbUrl`，
    模型版本不写死）。结果带完整**溯源**（accession / 物种 / 蛋白名 / `structure_source` /
    `structure_url` / 下载文件与大小 / AlphaFold 模型版本 / 打分理由 / 已尝试检索）；
  - 结构准备失败（如缺原子）时**自动换下一个候选**，最多尝试 5 个；处理**交替构象（altloc）**
    （依次尝试 `--default_altloc A/B/C`，这是很多晶体结构准备失败的主因）；已有 PDBQT 会被复用；
  - `fetch_molecule_record(query, id_type)` 支持中文别名映射（代森猛锌/代森锰锌 → Mancozeb）；
    命中多组分/配位聚合物（如 Mancozeb = Zn/Mn-EBDC，PubChem CID 3034368）时**不臆造单一结构**，
    返回原始 SMILES、`is_mixture`、`components`、`representative_smiles` 与 `mixture_note`，
    并给出 `choices` 让用户确认代表结构取法。

---


## 10. 配置项（`.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 空 / 空 / `doubao-seed-2-0-pro-260215` | LLM 端点（兼容 `OPENAI_*`） |
| `LLM_TEMPERATURE` `LLM_TOP_P` `LLM_TIMEOUT` `LLM_THINKING` | 0.2 / 0.9 / 600 / disabled | 采样与超时；`LLM_EXTRA_BODY`/`LLM_EXTRA_HEADERS` 传追加字段 |
| `LLM_<字段>_<角色>` | 继承全局 | **按角色覆盖**（角色：`INTAKE`/`COORDINATOR`/`PROPERTY`/`POCKET`/`DOCKING`/`BINDING`），如 `LLM_MODEL_DOCKING=deepseek-v4-pro`、`LLM_TEMPERATURE_PROPERTY=0`；也可写在 `config/agent_llm_config.json` 的 `roles` 段 |
| `PORT` | 5000 | HTTP 端口（被占用时 `start.sh` 自动顺延） |
| `ARTIFACT_BASE_URL` | `http://127.0.0.1:$PORT` | 产物下载地址前缀 |
| `DOCKING_MAX_LIGANDS` | 0 | 单次对接分子数上限（0=不限） |
| `RUN_TIMEOUT_SECONDS` / `RECURSION_LIMIT` | 900 / 120 | 运行超时 / 图递归上限（子 Agent 调用同样带上它，此前缺省只有 25） |
| `CHECKPOINT_BACKEND` | memory | `memory` 或 `sqlite`（需装 `langgraph-checkpoint-sqlite`） |
| `DOCKING_WORKERS` / `VINA_CPU` | 自动规划（线程优先：`ceil(可用核/8)` 个进程 × 8 线程） / 1 | 对接并行进程数**上限** / 单进程线程数 |
| `SSE_BATCH_SIZE` / `SSE_FLUSH_MS` / `SSE_PROGRESS_MS` | 25 / 200 / 1000 | 实时事件批量与节流参数 |
| `RESULT_INLINE_LIMIT` / `REPORT_TOP_N` / `CHART_TOP_N` / `CHART_BAR_MAX` | 200 / 50 / 20 / 50 | 内联与展示上限 |
| `POSE_SAVE_MAX` / `POSE_TOP_N` / `POSE_ARTIFACT_MAX` | 5000 / 200 / 200 | 位姿保存与登记策略 |
| `AGENT_TOOL_TOP_N` | 20 | 工具回传给**模型**的明细行数（只影响模型视野，不是输入上限；完整明细始终落盘） |
| `AGENT_FUNNEL_MIN` / `AGENT_REFINE_TOP_N` / `AGENT_COARSE_EXHAUSTIVENESS` / `AGENT_FINE_EXHAUSTIVENESS` | 500 / 200 / 1 / 16 | 两阶段漏斗阈值与两轮搜索强度（受理层会按实际分子数把建议写进指令，见 §11.4b） |
| `AGENT_SHARD_MAX` | 8 | 分片调度护栏（片数上限，防止 LLM 往返开销反超对接本身） |
| `POCKET_ENGINE` / `POCKET_TOP_N` | auto / 10 | 口袋引擎（auto/p2rank/geometric/known_site）与保存的预测口袋数 |
| `POCKET_PADDING` / `POCKET_MIN_SIZE` / `POCKET_MAX_SIZE` | 4 / 18 / 30 | 盒子外扩量(Å) 与边长上下限(Å) |
| `P2RANK_HOME` / `P2RANK_THREADS` | 自动探测 / cpu//2 | 本地 P2Rank 目录与线程数 |
| `DOCKING_WORKER_MEM_MB` / `DOCKING_THREADS_PER_WORKER` | 400 / 8 | 每进程内存预算（用于收敛进程数防 OOM）/ 每进程线程数（调小=更多进程） |
| `STREAM_TOOL_RESULT_CHARS` | 2000 | SSE 回传工具原文的字符上限 |
| `HOST` / `ARTIFACT_HOST` / `UVICORN_LOG_LEVEL` | 127.0.0.1 / 127.0.0.1 / info | 监听地址 / 产物 URL 主机 / uvicorn 日志级别 |
| `DOCKING_WORKSPACE` / `LOCAL_SETTINGS_PATH` | 项目根 / `config/local_settings.json` | 工作区根目录 / 设置文件位置覆盖（多实例、测试用） |
| `UPLOAD_MAX_MB` | 200 | 上传文件大小上限（MB） |

> 上表这些参数**都可以在网页「设置」页面里改**（`#/settings`），改完立即生效并显示来源；
> `.env` 仍然是部署级默认值，适合无界面/脚本场景。详见 §2.1。

---


## 11. 结合位点与对接盒（工具定盒）

### 11.1 盒子怎么定（不再「把盒子放蛋白质心」）

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | **用户显式指定** | 表单里的中心/尺寸，或指令里给出的坐标；口袋分析 Agent 也可显式提交坐标 |
| 2 | **实验位点** | 注册表标注的已知位点、或用户上传受体里**共晶配体**的质心（两者都由真实结构数据得出） |
| 3 | **P2Rank 预测** | 成熟的口袋预测工具（随机森林 + 溶剂可及表面特征），本地部署后自动启用 |
| 4 | **内置几何法** | 无外部依赖的兜底：表面层网格 → 埋藏度/包封度/疏水接触三特征打分 → 贪心球聚类 + 有限合并 |
| 5 | 蛋白质心 | 以上都不可用时的最后兜底，**结果里会明确标注「不可靠」**（`box_source` 里能直接看到） |

决策规则（`core/pockets.select_site`，有测试看护）：

- 有**实验位点**时：以实验位点为准（实验数据优先），同时用工具预测做**独立验证**——
  一致（中心相距 ≤ 8 Å）就写进溯源；**不一致则记录警告**并写入 `box_warnings`；
- 只有**低可信兜底位点**（蛋白质心）时：**改用工具预测的 top 口袋**（这正是原先最弱的一环）；
- 完全没有参考位点时：直接用工具预测的 top 口袋；
- 工具不可用则逐级回退，并把「实际使用的来源」如实写进 `box_source`。

盒子尺寸由口袋的空间范围 + 外扩量决定，并夹在 `POCKET_MIN_SIZE`~`POCKET_MAX_SIZE`（默认 18~30 Å）。

### 11.2 内置几何法的质量（客观标定）

在凝血酶 1DWC 上（共晶配体 MIT 中心 `31.5/13.74/24.36`；**精度与网格间距强相关，引用时必须带 spacing**）：

| 指标 | 结果（默认 spacing=1.0 Å） |
| --- | --- |
| top-1 口袋中心距共晶配体 | **2.6 Å** |
| top-2 口袋中心距共晶配体 | 2.97 Å |
| top-1 口袋附近残基 | `GLU192 TRP215 GLU217 CYS191 GLY216 GLY219`（凝血酶 S1 口袋/催化区） |
| 单受体耗时 | ≈1 s（2283 个重原子，1.0 Å 网格），结果按文件内容哈希缓存 |

这条一致性是 `tests/test_pockets.py` 的**永久质量门**（top-1/top-3 必须命中实验位点）。
该用例为速度使用 **1.5 Å 粗网格**（≈0.3 s），此时 top-1 距共晶配体 3.60 Å、top-2 22.8 Å ——
所以质量门的阈值是「top-3 内 ≤8 Å」，而不是把粗网格的数字当成精度指标。

### 11.2b P2Rank 实测（本机已部署 2.5.1）

| 指标 | 结果 |
| --- | --- |
| top-1 口袋中心距共晶配体 | **4.54 Å**（score 7.70，probability 0.407） |
| top-2 口袋中心距共晶配体 | 6.02 Å（score 5.62） |
| 与实验位点共享残基 | 8 个（`H_191 H_192 H_195 H_215 H_216 H_57 H_60F H_99`） |
| 单受体耗时 | ≈7 s（10 线程；结果按内容哈希缓存） |

**「工具真的在定盒子」的端到端证据**：把凝血酶 PDB 的共晶配体删掉（apo 受体，系统只能得到
「蛋白质质心」这个不可靠兜底），系统自动改用 P2Rank 预测的 pocket1：

| | 盒子中心 | 距真实活性位点 |
| --- | --- | --- |
| 旧行为（蛋白质心兜底） | `41.52 / 21.84 / 19.17` | 13.2 Å |
| 现在（P2Rank 预测） | `32.89 / 11.85 / 19.37` | **5.52 Å** |

### 11.3 启用 P2Rank（可选，推荐）

```bash
# 解压到 assets/tools/ 即可被自动识别（也可以设置 P2RANK_HOME 指向解压目录）
cd projects/assets/tools
curl -L -o p2rank.tar.gz https://github.com/rdk/p2rank/releases/download/2.5.1/p2rank_2.5.1.tar.gz
tar xzf p2rank.tar.gz && rm p2rank.tar.gz
.venv/bin/python -c "from docking_agent.core.pockets import available_engines; print(available_engines())"
# {'p2rank': True, 'geometric': True, ...}
```

- 需要系统有 `java`（P2Rank 是 Java 程序）；
- 未安装时系统**自动回退内置几何法**，并在结果/协作记录里说明，不会静默降级；
- 引擎可在「设置 → 运行与性能参数 → 口袋预测引擎」里选 `auto / p2rank / geometric / known_site`，
  也可在工作台的「结合位点来源（口袋引擎）」里按次选择。

### 11.4 新增的「口袋分析 Agent」

新增第 4 个子 Agent（`prompts.POCKET_SP` + `tools/pockets.py`），职责是**分析接口、选口袋、提交给 dock**：

| 工具 | 作用 |
| --- | --- |
| `predict_binding_pockets` | 用 P2Rank/几何法预测口袋，返回每个口袋的 score / 中心 / 范围 / 附近残基 + 参考位点与验证结论 |
| `compare_pocket_with_experiment` | 把候选口袋与实验位点做独立比对（中心距离 + 共享残基）并给出 verdict |
| `set_docking_site` | **把选定盒子写进共享黑板提交给 Docking Agent**（`chosen_by=pocket_agent`，之后不会被受体默认位点覆盖） |
| `list_pocket_engines` | 查口袋预测引擎的可用性 |

协作路径：`协调 Agent → run_pocket_analysis → 口袋分析 Agent（预测+比对+提交）→ 共享黑板 → Docking 子 Agent 按该盒子对接`。
协调 Agent 的执行纪律里明确写了：**未显式给出位点坐标时，必须先做口袋分析，不允许臆造坐标**。

### 11.4b 大库打法：两阶段漏斗（粗筛 → 精算）+ 并发规划

**两阶段漏斗**（算力最优、精度也更好；阈值为 `AGENT_FUNNEL_MIN`，默认 500）：

```
① 全库粗筛  run_docking(exhaustiveness=1)              # 便宜、全量
② 头部精算  run_docking(top_from_previous=200,
                        exhaustiveness=16)             # 只算最好的 200 个
```

- `top_from_previous=N`：工具**直接从共享黑板取上一轮最好的 N 个**，不经过模型上下文；
- 同一分子两轮结果合并时**自动保留精度更高的那条**，粗筛分值保留在 `affinity_coarse` 列，便于对比；
- **不推荐**按固定分子数切成很多片逐片调度：每次分发约 8–10 s LLM 往返，
  10k/200=50 片会凭空多出 ~7 分钟（超过对接本身）；确需分片时受 `AGENT_SHARD_MAX`（默认 8）约束。

**对接并发规划**（`plan_concurrency`）——**由部署机器自动推导**（探测 cgroup 配额 / CPU 亲和性 /
物理核 / 可用内存），规则与旧的「进程优先」相反：**线程优先**。下表左列是旧实现，中间是规则给出的配置，
右列是在 16 物理核 / 32 逻辑核这台机器上的**验证**结果（规则本身不依赖这些数字，完整数据见
`docs/architecture.md` §8.2）：

| 分子数 | 旧（进程优先） | 现在（线程优先） | 实测墙钟 |
| --- | --- | --- | --- |
| 1 | 1 × 8 | 1 × 8 | 2.7s（池 1×1 = 14.0s） |
| 6 | 1 × 8 | **4 × 8** | 7.9s → **4.4s** |
| 8 | 8 × 3 | **4 × 8** | 10.4s → **4.8s** |
| 16 | 16 × 1 | **4 × 8** | 39.4s → **13.5s** |
| 64 | 24 × 1 | **4 × 8** | 70.4s → **38.5s**（CPU 1003 → 716 CPU·s） |
| 1000+ | 24 × 1 | `ceil(可用核/8) × 8` | 进程数只由机器决定，不随分子数漂移 |

同一份代码在不同部署机器上自动得到的配置（`plan_concurrency(1000)`）：

| 部署形态 | 识别结果 | 自动规划 |
| --- | --- | --- |
| 4C/8T 笔记本 | logical=8 | 3 进程 × 3 线程 |
| 容器 `--cpus=4`（宿主 32 核） | logical=4（**来源=cgroup 配额**） | 3 进程 × 1 线程 |
| cpuset 只给 6 核 | logical=6（来源=CPU 亲和性） | 3 进程 × 2 线程 |
| 16C/32T 工作站 | logical=32 / physical=16 | 4 进程 × 8 线程 |
| 64C/128T 服务器 | logical=128 | 16 进程 × 8 线程 |
| 单核容器 | logical=1 | 1 进程 × 1 线程 |

`GET /api/health` 会返回 `machine`（探测结果与来源）与 `docking_plan`（大库/单分子将要用的进程 × 线程），
部署后可直接核对。

> **注意收益随库而变**：均匀小分子库收益接近 **1.8×**；重尾库（个别 MW 600+ / 大环分子）
> 收益降到 ~1.1–1.2×——尾部那一个分子只能靠线程、没法拆到多进程。真实 137 分子库实测：
> 主组 73s → **62s**，整段对接 164s → **154s**。

为什么不是「进程越多越快」：进程越多要重复建网格图/导入模块，而且内存带宽与缓存争用更重
（实测 31 进程比 24 进程还慢）；而单分子内部的搜索能靠线程有效并行（1→8 线程 5.6×）。
另两项不变：**大分子优先调度**（按可旋转键+重原子估成本，从大到小提交，减少尾部拖尾）、
**内存护栏**（按 `MemAvailable` 与 `DOCKING_WORKER_MEM_MB`=400 收敛进程数，防 OOM）。
`DOCKING_THREADS_PER_WORKER`（默认 8，旧名 `DOCKING_SERIAL_THREADS` 兼容）可调；
`DOCKING_WORKERS` 现在只做**上限**。

### 11.4c 特殊化学（金属/辅因子/盐/非标准配体）：工具报事实，Agent 做判断

对接流程的教科书默认前提是「纯蛋白受体 + 中性单片段配体」，真实项目常常不满足。
本项目**不允许静默丢弃信息**，也不让工具替用户做化学决定：

**受体侧（杂原子）**

- 标准流程只保留蛋白质 ATOM 记录，水与杂原子会被剔除 —— 但**剔除了什么、各多少个**会逐条上报：
  `dropped_hetatm`（如 `{"HEM": 43, "SO4": 10, "OXY": 2}`）、`dropped_waters`、`kept_hetatm`；
  这些字段同时出现在上传响应、对接结果块、`notes` 与页面提示里。
- `keep_hetatm="HEM,ZN"`（`molecular_docking` / `run_docking` 参数）可明确要求保留；
  **金属离子（ZN/MG/CA/FE/MN/CL 等）可直接保留**，大辅因子通常需要 meeko 化学模板。
- **受体是「已准备好的 PDBQT」时同样有效**：上传准备会把来源 PDB 的绝对路径写进 `.site.json`，
  因此要求保留时会**回到原始 PDB 重新准备**（实测：1A6M 上传的 PDBQT + `keep_hetatm="OXY"`
  的分数从 −6.97 变为 −7.15，OXY 真的进入了受体）。
- 保留失败不会让整次对接崩掉，也不会假装保留成功：实现会**逐个剔除**无法参数化的残基，
  把结果记进 `unsupported_hetatm` 并给出补救办法（提供模板 SDF / 直接给已备好的 PDBQT）。
  `kept_hetatm` 以**最终受体 PDBQT 内容**为准，绝不出现「说保留了、其实没进对接」的假信息。
- 受体准备缓存按**准备后结构的内容哈希**判定（不是时间戳）：既不会每次重跑 meeko，
  也不会在 `keep_hetatm` 变化时误用旧结果。
- 位点盒仍取**共晶配体质心**，且现在会写清是哪个配体（`共晶配体(MIT)质心`）；
  识别时按 (链:残基号:残基名) 取**最大的一团非水 HETATM**，并排除离子与结晶添加剂
  （SO4/GOL/EDO/PEG…）—— 直接对所有 HETATM 求质心会被硫酸根和甘油把盒子带偏。

**配体侧（盐/金属/电荷/手性）**

- 每个分子对接前做一次确定性「化学体检」（`describe_ligand`），结果写进该行结果：
  `ligand_facts`（片段数、净电荷、MW、重原子、可旋转键、HBD/HBA、TPSA、cLogP、未定义手性中心数）、
  `ligand_warnings`、以及在发生改写时的 `dock_smiles` + `removed_fragments`。
- **多片段（盐/反离子/溶剂）**：按最大有机片段对接，并写明移除了谁、判定为什么
  （如 `移除：[Na+](反离子)`）—— `CC(=O)[O-].[Na+]` 不会再把 Na⁺ 一起丢给 Vina 打分。
- **含金属配体**、**净电荷**、**未定义手性中心**、**偏大分子（MW>800）** 都会给出明确告警，
  由 Agent 判断是否接受、改写输入或换方法。
- 解析失败的 SMILES 返回**原因**（`error` + `ligand_warnings`），不会静默跳过或编造数值。

页面：上传受体后，卡片会直接以告警样式列出「已剔除非水杂原子：HEM×43…」与「未保留的残基」。

### 11.5 溯源与展示

- 对接结果每个受体块带 `box_center / box_size / box_source / box_chosen_by / box_validation / pockets`；
- 固定报告的首表有「对接盒」一行（中心、尺寸、来源），第 1 节附**口袋预测表**（含被采用的那个）；
- 网页：编排图新增「口袋分析」节点；编排面板显示实际使用的盒子与来源；「中间数据」页有「结合位点与口袋预测」区块；
- 运行目录新增产物 `pockets.json`（引擎、来源、验证、top 口袋）。

---

## 12. 对接引擎

- 主引擎 **AutoDock Vina 1.2.7**（Python 原生扩展）；
- 备用 **AutoDock4 CPU**（`autodock4`/`autogrid4`，未安装时按需回退并在结果中说明）；
- 结果中的 `engine` 与 `exhaustiveness` 字段标明实际使用的引擎与搜索强度；
- **不同搜索强度会得到不同分数**（如 −5.78 @6 vs −5.87 @3），这是参数真实参与计算的表现，
  因此排序 CSV 与 `docking.json` 都带这两列，保证可追溯、可复现。要固定参数，请在指令中写明
  「使用默认 exhaustiveness=16」。

---


## 13. 测试与「真实性取证」

**聚合入口**（推荐；把下面这些门禁收拢成一条命令）：

```bash
bash scripts/check.sh              # 全量：lint + 前端契约 + 全部用例（需本机引擎，约 6 min）
bash scripts/check.sh --fast       # 离线/快：跳过需要本机引擎的 engine 组（等价 CI，约 20 s）
bash scripts/check.sh --static     # 只跑两个静态门禁（约 1 s，提交前随手跑）
COVERAGE=1 bash scripts/check.sh --fast   # 追加覆盖率报告（只报告、不设阈值）
```

**逐条门禁**：

```bash
.venv/bin/python -m pytest -q              # 全量用例：核心/API/在线解析/受理/协作/口袋与姿态/盒与参数/特殊化学/多轮会话/并发隔离/文档一致性/源码结构/报告 golden/运行目录损坏注入
DOCKING_ENGINE_TESTS=0 .venv/bin/python -m pytest -q   # 与 CI 等价的快跑（跳过 engine 组，约 20 s）
.venv/bin/python scripts/smoke_test.py     # Fake LLM 驱动多 Agent + 真实对接 + 运行目录
.venv/bin/python scripts/prune_runs.py     # 运行目录保留策略（默认只报告；--apply 才删）
.venv/bin/python scripts/verify_docking.py # 对接引擎真实性取证（按盒分组独立复算，含分组反证）
.venv/bin/python scripts/check_web.py                      # 前端静态校验（语法/离线/id 对应/结构配平/布局健壮性/设计令牌/设置页/导出条与动效/对话附件/多轮会话/工具提示与高级设置栅格/安全姿态）
.venv/bin/python scripts/snapshot_ids.py diff               # 重构前后 DOM id 清单比对（防止误删动态引用的 id）
bash start.sh --check                                      # 一键自检
.venv/bin/python scripts/lint_local.py                      # 静态规范门禁（零依赖；新增违规必须为 0）
.venv/bin/python -m ruff check src/ scripts/ tests/        # ruff：未定义名 / 未使用导入 / 重复定义（F+E9+W）
.venv/bin/python -m mypy                                   # 类型检查：只查 [tool.mypy].files 白名单（当前 7 个模块）
bash scripts/kill_leftovers.sh --dry-run                    # 残留对接进程排查（--dry-run 只看；去掉即清理）
.venv/bin/python scripts/intake_eval.py                     # 任务受理层用例集（确定性，零模型）
```

**CI**：`.github/workflows/gate.yml` 在每次提交/PR 跑「静态门禁 + 离线用例」，并在部署包上跑
`langgraph-deploy/scripts/check.sh && test.sh`。持续集成里跳过的 `engine` 组（真实 Vina / P2Rank /
pdb2pqr）必须在**装了引擎的机器**上全量跑一次：`bash scripts/check.sh`。

**报告 golden 快照**：报告版式改动后重出并**人工 review diff**：

```bash
cd projects && REGEN_REPORT_GOLDEN=1 .venv/bin/python -m pytest -q tests/test_report_golden.py
```

**设置页面 / 导航栏的运行时验证**（jsdom 真加载页面 + 真点按钮 + 真读接口，可选）：

```bash
cd projects && npm install jsdom                 # 仅开发期需要（未安装时脚本会给出提示）
.venv/bin/python scripts/ui_shot.py check --base http://127.0.0.1:5095   # 真浏览器体检：控制台/横向溢出/图片（6 页）
.venv/bin/python scripts/ui_shot.py baseline && .venv/bin/python scripts/ui_shot.py diff  # 视觉回归（改前端前后对比）
.venv/bin/python scripts/shot.py --list                     # 无依赖备选：抓桌面上已打开的窗口
.venv/bin/python scripts/shot.py                            # 抓标题含「分子对接」的窗口 → var/tmp/shot/*.png
node scripts/ui_e2e.js http://127.0.0.1:5000    # 143 项：导航切换 / hash 深链接 / 字段渲染与来源标注 / 对话附件与 @ 引用 / 多轮会话 /
                                                # 高级设置预设与恢复默认 / 折叠摘要 / 工具提示气泡 /
                                                # 密钥只写不读 / 未保存提示 / 保存回环 / 工作台预填 /
                                                # 模型列表拉取；结束时按快照恢复原设置，不破坏用户配置
```

> 静态校验抓不到「渲染出来是空的」「保存没生效」「布尔字段被误判为已修改」这类运行时缺陷，
> 因此设置页面额外用 jsdom 做了 DOM 级验证 —— 上面的 143 项里就有 3 项是这次真实抓到并修掉的
> （布尔字段误判、只读项被一起提交导致保存失败）。

`scripts/verify_docking.py` 不信任项目自身的对接封装，分五层取证：

| 层 | 内容 |
| --- | --- |
| A 引擎真实性 | `vina` 为原生扩展；受体为真实蛋白（2771 原子、含催化三联体 HIS57/Asp102/Ser195、已去水）；对接代码剔除注释后无 `mock/fake/硬编码分数` |
| B 独立复算 | 用 `vina` API **从零重写**对接（仅硬编码受体路径与盒子），与工具输出逐位一致；配体 PDBQT sha256 相同；位姿用全新实例重打分一致 |
| C 工具交叉验证 | 无 LLM 直接调用 `@tool`：能量恒等式 `total = intermolecular + torsional` 对全部 8 分子成立（最大偏差 0.01）、重复调用逐位一致、不同分子分数各异 |
| C2/C3 | 报告产物结构（真实 CSV 字节、行数、升序、合法 PNG）；参数真实透传与敏感性 |
| D 反证控制 | 盒子平移 25 Å / 换受体 → 分数改变；非法 SMILES → 不出数值；`engine=autodock`（未安装）→ 明确报错 |
| E agent 闭环 | 同一次真实 LLM 运行内三方对齐：LLM 转交报告工具的数值 == 对接工具原始返回；以相同参数重放 == 当时返回；落盘 CSV == 工具返回 |

---


## 14. 修复记录（已迁出）

历史修复记录（62 条 / 约 1100 行）已移到 **[`CHANGELOG.md`](CHANGELOG.md)**：
它是**按时间编号的历史日志**，保留当时的结论与门禁数字；**当前**行为与验收口径以
本 README、`docs/architecture.md`、`docs/api.md` 为准。新增修复记录请写进 CHANGELOG。

## 15. 注意事项

- **`/node_run`**：Agent 图由 `create_agent` 内部构建，节点通常无法直接寻址；请用网页、
  `-m flow` / `-m agent`（见 §5），或标准 Agent Protocol 端点（`/threads/{tid}/runs/*`）。
  已下线的确定性流水线端点（`/api/pipeline/stream`、`/pipeline`）不再提供。
- **会话持久化**：默认内存 checkpointer，重启后多轮会话丢失；需要持久化请设 `CHECKPOINT_BACKEND=sqlite`。
- **并发**：子 Agent 在工具内同步调用，单进程串行；多用户高并发建议多进程部署（每进程独立 checkpointer）。
- **长任务**：大分子库会显著耗时，可用 `--max-ligands` 或界面上的「最大分子数」先做小样本验证。
- **运行记录保留**：每次筛选都会往 `var/runs/<id>/` 落盘产物，长期使用会累积（本机实测曾到
  **2.2 GB / 4900+ 目录**，拖慢启动收尾与历史检索）。清理入口：
  `.venv/bin/python scripts/prune_runs.py`（**默认只报告**）→ 确认后加 `--apply` 删除，
  阈值 `--keep-last`（默认 200）/ `--max-age-days`（默认 30，0=不限年龄）；
  删除条件为「不属于最近 N 个」**且**「超过 M 天」，`running` 的运行永不删除。
  想开机自动清理可设 `RUNS_AUTO_PRUNE=1`（默认关闭）。
- **残留对接进程**：大库对接会起多进程池，如果服务/脚本被强杀（连按 Ctrl-C、`kill -9`、断线），
  worker 可能被 init 收养成孤儿并继续吃满 CPU。清理：
  `bash scripts/kill_leftovers.sh --dry-run`（只看）→ `bash scripts/kill_leftovers.sh`（清理），
  加 `--include-server` 会连 http 服务一起停；脚本只处理**当前用户 + 本项目**的进程
  （按 `/proc/PID/exe` 与 cwd 精确识别），默认保护 GUI 服务与自身。
  **必须在宿主 shell 里运行**：容器/PID 命名空间内只能看到自己命名空间的进程，触达不到这些孤儿。
- **密钥安全**：`.env` 已被 `.gitignore` 忽略，`scripts/pack.sh` 打包时也会排除；
  设置页面保存的 `config/local_settings.json`（可能含 API Key）同样已加入 `.gitignore` 与打包排除项。
- **网络暴露**：服务默认只监听 `127.0.0.1`，且**没有鉴权**——设置接口可修改 LLM 端点，
  连通性测试会用当前密钥发请求，因此不要用 `--host 0.0.0.0` 暴露到不可信网络。
- **设置文件**：`config/local_settings.json` 是界面设置的覆盖层，删掉它即回到 `.env` + 内置默认
  （页面上的「清除界面设置」就是这个动作）；文件损坏或结构不对时会自动忽略并回退，不影响启动。
- **测试隔离**：`tests/conftest.py` 会把 `LOCAL_SETTINGS_PATH` 指到临时路径，
  因此本机若保存过界面设置，也不会影响 pytest / smoke / verify_docking 的结果。
