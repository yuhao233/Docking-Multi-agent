# 分子对接多 Agent 协作系统（本地部署版）

把一个**蛋白质受体的已知结合位点**与**小分子库**做真实分子对接筛选：
由 1 个「主管 Agent」统筹 4 个专业子 Agent（理化性质评估 / 口袋分析 / 对接执行 / 结合模式检测），
对每个小分子给出理化性质、对接能量明细、与阳性对照的结合模式比较，并出具可追溯的筛选报告与排序推荐。

- **一键启动**，自带交互式可视化网页；
- **所有对接数据与中间分析产物**（分子库、性质、对接明细、位姿文件、图表、CSV、报告）逐次运行落盘，可单项或整包下载；
- 所有数值均来自**真实计算引擎**（RDKit / AutoDock Vina / AutoDock4），并有独立取证脚本证明（见 §13）。

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
  `base`=筛选 12 / 结合模式 16；`f_rot`=P90 可旋转键/5（0.75–2.5）；`f_box`=盒体积开立方（1–2，保持单位体积采样密度）；
  `n_poses` 筛选 1、姿态分析 3；`engine=vina`、`seed=42`。
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
  参数模式下同一份 DOM 按顺序回到参数区顶部（受体下拉在这里）。
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
    不会把绝对路径铺满气泡。助手的长回执（超过约 24 行 / 1800 字）自动折叠，可点「展开 / 收起」。
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
| `autodock4`/`autogrid4` | 可选 | 只有 Vina 可用（`engine=autodock` 会失败）；`apt-get install autodock autogrid` |
| 中文字体（Noto CJK 等） | 可选 | 图表/PDF 中文可能显示为方块；`apt-get install fonts-noto-cjk` |
| LLM（`LLM_API_KEY` + `LLM_BASE_URL`） | 可选 | 多 Agent 模式不可用，**确定性流水线照常可用**（参数模式·流水线） |
| 默认端口 5000 空闲 | 可选 | `start.sh` 自动顺延到下一个可用端口（`doctor` 会告知将用哪个） |

离线环境同样可用：确定性流水线 + 注册表受体（thrombin / trypsin）+ 上传文件；
只有"点名在线解析受体 / 按名称查分子 / 云端 LLM"需要网络。

### 配置 LLM（多 Agent 模式必需）

```bash
cp .env.example .env      # 首次
```

```dotenv
LLM_API_KEY=sk-xxxx
LLM_BASE_URL=https://api.deepseek.com     # 任意 OpenAI 兼容端点
LLM_MODEL=deepseek-flash
```

> 不配置 LLM 也能用：网页的「确定性流水线」模式与命令行 `-m pipeline` 不依赖任何模型。

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
| `run` | 输入合法且能对接 | **必须完整跑完**（导入 → 属性 → 对接 → 结合模式（有对照时）→ 报告），不得中途询问；唯一例外是点名受体待解析（`receptor.source="named"`），必须先自动解析 |
| `ask` | ① 用户显然想分析某特定对象、但原文里没有任何可识别的分子；② 受体**自动解析真失败/歧义**（`receptor.source="unresolved"`） | 只说明缺什么并提问，**不调用任何工具** |
| `reject` | 超出系统能力（闲聊/无关请求） | 友好说明本系统能做什么，**不调用任何工具** |

> 关键设计：**「缺东西」默认不是阻断**。系统对受体、阳性对照、对接参数都有默认值，位点由口袋工具自动确定；
> **示例库/示例受体不再默认使用**（只有用户明确要求时才用，见 §14 修复记录 30）—— 因此 `missing` 只作记录，
> `ask` 只在受理模型**明确要求用户补充**时才成立。
> （这条规则来自实测：`scripts/intake_eval.py --live` 曾抓到受理模型把 5 个本该 `run` 的用例判成 `ask`，
> 直接违反「输入合法就必须跑完」的产品底线，现已修正并有回归用例看护。）

**点名了受体 → 先自动去在线数据库解析，不确定才让用户选（v0.11）。**
用户说「从在线数据库中获取植物去甲基化酶ROS1」时，系统**不会**再反问「请给我 PDB ID」，而是：

| 指令 | 受理层判定 | 行为 |
| --- | --- | --- |
| 完全没提受体 | `receptor.source="default"` | 用系统默认受体跑，并在报告/回复里**写明**「未指定受体，已使用系统默认 凝血酶(thrombin, 1DWC)」 |
| 提到 thrombin/trypsin/1DWC/PDB 号/UniProt accession/上传受体文件 | `receptor.source="user"` | 按用户指定的受体跑 |
| 点名了具体受体（如「植物去甲基化酶ROS1」「ROS1」「EGFR」）但字面不是注册表/PDB/accession | `receptor.source="named"` → `decision="run"` | **第一步调用 `fetch_protein_structure` 自动解析**（中英映射 + 物种推断 + UniProt 多策略检索 + RCSB/AlphaFold 结构获取）；高置信唯一候选 → 继续完整流程，报告写明 accession/物种/结构来源 |
| 检索到**多个同样合理的候选**（如只写 `ROS1`：人源激酶 vs 拟南芥去甲基化酶）或**单一候选置信度不足** | 工具把 `receptor.source` 改回 `unresolved`，并下发 `choices` | **不计算**：把候选列成前端可点按钮（accession / 物种 / 蛋白名 / 结构来源 / 打分），用户点选后以同一 `conversation_id` 继续 |
| **一个都没查到** | 工具把 `receptor.source` 改回 `unresolved`，回执带 `attempts` | **不计算**：提问并**逐条列出已尝试的检索**（UniProt accession/基因名/蛋白名、RCSB、AlphaFold），请用户提供 PDB ID/accession/文件或同意用默认受体 |

> 真实缺陷：用户点名「植物去甲基化1酶」，UniProt accession 直查与基因/蛋白名称检索都无匹配，
> 系统却按「回退默认受体」**继续对接**，把计算对象悄悄换成了凝血酶。现在这属于产品底线：
> **不明确计算对象时绝不计算**，而且「不确定」的两种形态（多个候选 / 查不到）都要把证据交回用户。
> 分发层还有一道代码级护栏：`run_docking` 一旦看到 `receptor.source=="unresolved"`，会在调用任何对接引擎
> **之前**返回 `{"status":"needs_user_input","attempts":[...],"candidates":[...]}` —— 即使主管 Agent
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
| 确定性流水线 | 否（零模型） | — | 不适用 |

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

### 2.2 工作台

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
| **任务配置** | 受体下拉（显示**已知结合位点**中心/尺寸，可手工微调）或**上传受体文件**；配体来源**四选一**（SMILES 文本 / **上传文件** / 服务端路径或 URL / 示例库）；阳性对照；引擎与搜索强度；运行方式（确定性流水线 / 多 Agent 协作） |
| **协作记录** | 展示共享黑板统计与各 Agent 的协作备注（多 Agent 模式） |
| **编排示意图** | 实时显示「整体协调 Agent → 口袋分析 / 分子属性评估 / Docking 执行 / 结合模式检测 → 报告生成」各节点状态与**当前任务**；并带一条**实测时间轴**：按服务端事件时间戳（`ts`）绘制每个节点的真实起止条 —— **条带重叠即并行、依次排列即串行**，顶栏徽标给出实测并行度（如 `[ 并行 ×2 ]`）。实测样例：`属性评估 12.9→17.4s` 与 `对接 13.1→23.1s` 重叠 4.3 s（并行），而口袋分析→属性评估、对接→结合模式→报告之间不重叠（串行） |
| **执行** | `■ 停止` 按钮（**真正中断后端对接**，见 §8）；阶段进度条（`已完成 x/y（z%）`、已用时间、**剩余时间 ETA**）、**逐分子实时结果**（批量推送，边算边出；实时表最多保留最近 300 行）、多 Agent 模式下的模型增量文本与工具调用轨迹、可中止运行 |
| **结果总览** | **服务端分页**排名表（每页 50/100/200，表头点击即全库排序），搜索框、「只看优于阳性对照」、聚合统计（命中数/区间/均值/中位数）、「导出完整 CSV」；图表含对接对比图、相似度图、**亲和力分布直方图**、理化性质空间图 |
| **分子详情** | 分页渲染（每页 12/24/48 张），每个分子一张卡片：RDKit 二维结构图、理化性质表、对接能量明细（total / intermolecular / intramolecular / torsional）、**结合模式分析（Morgan 与 MACCS 双指纹相似度、结构一致性、药效团锚定基团匹配、性质差异、结合模式提示）**、位姿下载 |
| **报告** | **固定格式** Markdown 报告（**9** 个固定章节：任务与参数 / 结果排序 / **推荐分子（含 3.1 推荐化合物排行：综合分 + Agent 理由 + 筛选建议 / 3.2 优于阳性对照）** / 理化性质 / 结合模式 / 方法与局限 / 失败与跳过 / 结论与建议 / 数据与产物），**图片以相对路径直接内嵌显示**：对接对比图、亲和力分布、相似度图、**推荐化合物 2D 结构图（PDF 同样内嵌）**、性质空间图（只画推荐 top）、结合模式散点图、分子结构对比网格；图/表带连续编号与题注；第 8 节只摘录 Agent 的结论/建议，**不重复正文数据** |
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
- **状态映射**：流水线模式由 `stage` / `progress` 事件驱动；多 Agent 模式由 `tool_call` / `tool_result` 驱动
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
bash scripts/local_run.sh -m pipeline -i "阿司匹林:CC(=O)Oc1ccccc1C(=O)O" --receptor trypsin
bash scripts/local_run.sh -m flow   --message "用示例库做一次完整筛选"   # 同步多 Agent
bash scripts/local_run.sh -m agent  --message "用示例库做一次完整筛选"   # 流式多 Agent（终端实时输出）
bash scripts/local_run.sh -m runs   --list 10     # 查看历史运行
bash scripts/local_run.sh -m receptors            # 查看受体与已知位点
```

`pipeline` 常用参数：

| 参数 | 说明 |
| --- | --- |
| `-i "<SMILES 或 名称:SMILES>"` | 候选分子（逗号/分号/换行分隔） |
| `--molecule-file <路径或URL>` | 小分子文件：`sdf / smi / csv / mol2 / mol` |
| `--receptor <受体>` | `thrombin`、`trypsin`、PDB 编号、`.pdb/.ent/.cif/.pdbqt` 路径或 URL（多受体用 `;` 或 JSON 数组） |
| `--site-center 31.5,13.74,24.36` `--site-size 22,22,22` | 覆盖已知结合位点 |
| `--positive-control <SMILES>` | 阳性对照，默认读 `assets/libraries/positive_control.csv` |
| `--exhaustiveness N` / `--n-poses N` | Vina 搜索强度 / 输出构象数 |
| `--engine auto\|vina\|autodock` | 对接引擎（`auto`：Vina 失败自动回退 AutoDock4 CPU） |
| `--max-ligands N` | 限制对接分子数（0=不限） |
| `--no-poses` | 不保存位姿文件 |

---


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
│   ├── pipeline.py             # 确定性流水线（无需 LLM）
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
│   │   ├── workers.py          #   4 个子 Agent（无状态执行器，各自独立模型实例）
│   │   ├── coordinator.py      #   整体协调 Agent
│   │   └── persistence.py      #   把 Agent 运行的真实工具输出落盘
│   ├── tools/                  # LangChain @tool（供各 Agent 调用）
│   ├── runtime/                # 运行时（context/llm/payload/streaming/errors/checkpoints）
│   └── api/                    # FastAPI 应用与请求模型
├── web/                        # 前端（index.html / app.js / styles.css；工作台 + 设置两个页面）
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
| GET | `/api/receptors` | 受体与**已知结合位点** |
| GET | `/api/libraries` | 示例分子库与默认阳性对照 |
| POST | `/api/pipeline/stream` | 确定性流水线（SSE：`stage` / `molecule` / `done`） |
| POST | `/api/agent/stream` | 多 Agent 协作（SSE：`token` / `tool_call` / `final` / `done`）；`mode=chat\|manual` 决定指令与参数谁是权威 |
| POST | `/api/runs/{id}/cancel` | **取消运行**：协作式取消，会终止对接进程池并停止后续步骤 |
| GET | `/api/runs` `/api/runs/{id}` | 运行列表 / 运行详情（结果、报告、产物清单；大库自动截断内联数据） |
| GET | `/api/runs/{id}/ranking` | **结果分页**：`offset/limit/sort/order/q/hits_only` + 聚合统计 |
| GET | `/api/runs/{id}/export.csv` | 导出完整排序 CSV（产物缺失时现场生成） |
| GET | `/api/runs/{id}/artifacts/{name}` | 单项产物下载（`?inline=1` 用于图片） |
| GET | `/api/runs/{id}/download.zip` `/poses.zip` | 整包 / 位姿打包下载 |
| GET | `/api/molecule/depict` `/properties` | 二维结构图（PNG）/ 单分子理化性质 |
| POST | `/api/uploads` | **上传小分子/蛋白质文件**：校验解析后返回 `path` / `receptor_file`（含位点盒） |
| POST | `/run` `/stream_run` `/pipeline` `/cancel/{id}` | 兼容脚本调用的旧接口 |
| POST | `/v1/chat/completions` | OpenAI 兼容接口 |

```bash
# 确定性流水线（无需 LLM）
curl -N -X POST localhost:5000/api/pipeline/stream -H 'Content-Type: application/json' -d '{
  "receptor":"thrombin","ligands_text":"阿司匹林:CC(=O)Oc1ccccc1C(=O)O",
  "positive_control":"NC(=N)c1ccccc1","exhaustiveness":6,"engine":"vina"}'

# 多 Agent（需要 LLM）
curl -N -X POST localhost:5000/api/agent/stream -H 'Content-Type: application/json' -d '{
  "message":"用示例分子库完成一次完整筛选并给出排序结论",
  "receptor":"thrombin","site_center":[31.5,13.74,24.36],"site_size":[22,22,22],
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

- 内置：`thrombin`(1DWC)、`trypsin`(1PTU)，位点盒已标定；
- 自定义：传 `.pdbqt` 直接使用；传 `.pdb/.ent/.pdb1/.cif/.mmcif`（大小写不敏感，`.ent` 是 RCSB
  坐标文件的常见后缀）会调用 meeko 的 `mk_prepare_receptor.py` 现场准备，
  盒中心优先取共晶配体质心，其次蛋白质心；也可用 `site_center/site_size` 显式覆盖；
  文件不存在或内容不是结构时**回退默认凝血酶**，并在结果 `notes` 里写明原因与
  「这不是你指定的受体」（绝不静默回退）；
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
| `RUN_TIMEOUT_SECONDS` / `RECURSION_LIMIT` | 900 / 60 | 运行超时 / 图递归上限 |
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
| `list_pocket_engines` / `available_receptors` | 查引擎可用性与受体已知位点 |

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

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q              # 545 项：核心/API/在线解析/受理/协作/口袋与姿态分析/盒与参数规划/特殊化学/多轮会话/并发隔离/文档一致性/源码结构
PYTHONPATH=src .venv/bin/python scripts/smoke_test.py     # 46 项：Fake LLM 驱动多 Agent + 真实对接 + 运行目录
PYTHONPATH=src .venv/bin/python scripts/verify_docking.py # 52 项：对接引擎真实性取证（按盒分组独立复算，含分组反证）
.venv/bin/python scripts/check_web.py                      # 118 项：前端静态校验（语法/离线/id 对应/结构配平/布局健壮性/设计令牌/设置页/导出条与动效/对话附件/多轮会话/工具提示与高级设置栅格）
.venv/bin/python scripts/snapshot_ids.py diff               # 重构前后 DOM id 清单比对（防止误删动态引用的 id）
bash start.sh --check                                      # 一键自检
.venv/bin/python scripts/lint_local.py                      # 24 项静态规范门禁（零依赖；新增违规必须为 0）
bash scripts/kill_leftovers.sh --dry-run                    # 残留对接进程排查（--dry-run 只看；去掉即清理）
.venv/bin/python scripts/intake_eval.py                     # 14 项：任务受理层用例集（确定性，零模型）
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


## 14. 修复记录

以下均为原项目既有问题（原始压缩包 `project_20260912_151606.tar.gz` 中同样存在），已修复并被测试看护：

1. **排序 CSV 被二次 JSON 编码**（`reporting/tables.py` 的前身）——
   `to_json_bytes(_build_csv(...))` 把 CSV 字符串又 `json.dumps` 了一次，落盘内容是一行带引号的
   JSON（零个真实换行），Excel/pandas 无法解析。
2. **报告产物不排序**——排序责任全压在 LLM 上；现由 `rank_molecules()` 在报告入口防御性排序，
   图表与 CSV 顺序一致。
3. **子 Agent 会话串扰**——原实现用固定 `thread_id`，同进程内多次筛选会共享历史上下文；
   现每次调用使用独立线程（子 Agent 本就是无状态执行器）。
4. **多 Agent 结果落盘丢失**——协调 Agent 常把候选分子与阳性对照拆成两次对接调用，
   落盘时只保留最后一次会丢数据；现对同名工具的多次返回做合并。
5. **中文名称误报 SMILES 解析错误**——原用 `Chem.MolFromSmiles(name)` 试探名称，
   中文名会打出一串 RDKit 报错；现改为字符集启发式判断。
6. **中文字体缺失**——图表中文渲染成方块；现在自动挑选系统 CJK 字体并修正负号显示。
7. **结合模式检测静默降级**（重构时引入的回归）——`binding_mode_analysis` 因 `core` 未导出
   `_fingerprint` 而报错，子 Agent 于是回退到轻量的 `positive_control_similarity`：
   数值仍真实，但丢失了结构一致性与结合模式提示。现已修复，并把方法学补全为
   **Morgan + MACCS 双指纹 + SMARTS 药效团锚定基团匹配 + 性质差异**，
   两个工具共用同一数据源、口径完全一致。已加入两条永久回归测试
   （工具输出完整性 + **全量内部导入审计**，后者可防止同类「漏导出」再次发生）。
8. **逐分子位姿链接在批量事件中丢失**（本轮引入并修复）——把逐分子事件改为批量 `molecules` 后，
   服务端的位姿 URL 补全只处理单条 `molecule`，批量条目拿不到 `pose_url`。现两者都处理。
9. **产物清单与接口响应随库增长膨胀**——原先每个位姿都进清单、`/api/runs/{id}` 内联全量分子/性质/对接明细；
   现已限流 + 截断，并加入「按命名规则直取位姿」的回退解析。
10. *（与第 7 条同一问题：`_fingerprint` 漏导出导致结合模式静默降级；此处保留编号以免后续引用错位，
    内容已并入第 7 条，不再重复记录。）*
11. **在线数据库工具遇到 400 就整体失败**——`fetch_protein_structure` 只接受 UniProt accession，
    用户输入**基因名/蛋白名**（如 `EGFR`）时被拼进 accession 路径，UniProt 直接返回 `HTTP 400`，
    而错误信息对用户毫无可操作性。现已支持三种输入并做检索式回退；同时修掉三处连带问题：
    RCSB 无命中返回 204 空体被当成 JSON 解析错误、候选结构准备失败不换下一个、
    **交替构象（altloc）导致 meeko 准备失败**（缺 `--default_altloc`）。
12. **「停止」按钮无法真正中断**——流水线跑在工作线程 + 子进程里，`asyncio.Task.cancel()`
    只能取消协程，Vina 仍在跑。现引入**协作式取消标志**：置位后父线程终止进程池并停止后续步骤，
    实测取消后 1.5s 内结束（否则需 30s+），运行状态标记为 `cancelled` 且保留已完成数据。
13. **阳性对照总是使用默认值**——原先未提供对照时也会用内置苯甲脒跑一遍对照，
    与「对照是可选」的预期不符。现改为**只有用户提供阳性对照才执行对照分析**，
    未提供则在结果与报告中如实标注并跳过。
14. **报告里没有图、格式不固定**——原先报告由模型自由撰写（流水线模式为简易文本），
    图片不在报告里。现改为**两种模式共用同一固定模板**（9 个固定章节，含「方法与局限」「失败与跳过」），
    把图以内嵌 Markdown 图片写入报告（相对路径、图/表编号题注）；多 Agent 的文字被放进固定的「结论与建议」一节（原文另存为产物）。
15. **上传受体后位点盒丢失**——上传时按共晶配体质心标定的活性位点盒，
    在只用 `.pdbqt` 路径对接时会退化成「全蛋白质心」（实测盒心从 31.5/13.74/24.36
    漂到 41.9/22.4/19.1，等于把对接盒子放到了错误的位点）。现把位点写入 `.site.json` sidecar，
    下游只拿 PDBQT 也能复原。
16. **子 Agent 静默失败与吞异常**（本轮自查发现）——一处分发工具的字符串替换静默失效
    （未断言锚点存在），导致对接结果从未写入共享黑板，交叉核验拿到空数据却"看起来正常"。
    现关键改动均带断言，并对子 Agent 返回做 JSON 校验；另外修掉 `write_json` 参数误用导致复核产物
    未落盘、以及交叉核验把「阳性对照与自己比较」误判为不一致两个缺陷。
17. **阳性对照可能被漏掉**——新提示词让 Docking 子 Agent 自主选受体后，它会专注候选分子而跳过
    对照对接，使对照分析失去基线。**当时的独立复核当场抓到了这个问题**（报 `fail` 并指出
    「阳性对照对接结果为 None」）。现已在子 Agent 提示词、协调 Agent 执行纪律、分发工具消息
    三处明确要求「阳性对照必须一并对接」——这条纪律在复核流程移除后仍然保留。
18. **聊天指令与运行参数冲突**——原先无论何种模式都把表单参数强行注入指令，用户在下拉里选了
   thrombin、又在对话里说用 trypsin 时，行为不可预期。现引入 `mode` / `advanced` 语义（见上表），
   并在真实运行中双向验证：chat 模式下指令生效（trypsin / 4），manual 模式下参数生效
   （thrombin / 6，且 Agent 主动说明「已按权威参数执行，未按 trypsin / 4」）。
19. **对接盒「放错地方」**——原实现只有注册表人工位点、共晶配体质心、**整个蛋白质质心**三个来源，
    最后这个兜底等于把盒子放在蛋白中心（实测盒心会从 31.5/13.74/24.36 漂到 41.9/22.4/19.1）。
    现引入**工具定盒**：新增「口袋分析 Agent」与 `core/pockets.py`——优先 P2Rank（成熟工具），
    未安装时用内置几何法（表面层网格 + 埋藏/包封/疏水三特征 + 贪心球聚类），
    实验位点优先并用工具做独立验证，盒子来源全程写入 `box_source`、报告与网页；
    确定性核验新增「对接盒来源可追溯」「口袋预测与实验位点一致」两项，
    运行目录新增 `pockets.json`。内置几何法在凝血酶上与共晶配体中心相距 **2.6 Å**
    （top-1，附近残基 `TRP215/CYS191/GLU192` 正是 S1 口袋），并作为永久质量门写进 `tests/test_pockets.py`。
20. **「理解用户要什么」与「怎么做到」挤在同一个提示词里**——协调 Agent 的提示词同时写着
    「若输入含糊不清…先礼貌询问用户」与「只要候选分子库非空…就**不得**在中途停下来询问用户」，
    在「库有但意图含糊」时两条规则直接冲突；受理逻辑还散在三处（API 拼消息 / 提示词两节 / 工具说明）。
    现抽出**任务受理层**（`intake.py`）：确定性规则产出结构化**任务规约**（任务类型、权威来源、
    `decision=run/ask/reject`、假设、缺失项、指令中提到的分子与受体），编排 Agent 只读规约、只管编排；
    提示词从 6079 字符降到 4129 字符（**−32%**），并删掉与工具 docstring 重复的约 2000 字符工具说明。
    受理模型只在「对话模式 + 自然语言」时调用一次（参数模式**零额外调用**，实测 intake 实例根本不构建），
    且只能补白名单字段：**不能给任何参数、不能编造分子**（提取的分子名必须逐字出现在用户原文里）。
    构建过程被自己的评估脚本抓到两个真问题：① 模型把「没给分子/受体」写进 `missing` 就让 5 个本该执行的
    用例变成 `ask`，直接违反产品底线——现改为「缺东西默认不阻断，只有模型明确要求补充时才 ask」并有回归用例；
    ② 运行记录里的 `calls` 是**进程累计**而非单次运行，无法判断本次调用量——现每次运行开始时重置计数。
21. **三个子 Agent 共用一个模型实例**——`init_workers()` 原先只构建**一个** LLM 对象，
    分发给属性评估 / 对接执行 / 结合模式三个子 Agent（只有提示词与工具集不同），
    因此既不能给不同角色配不同模型，也无法分辨各 Agent 的真实调用与开销。
    现改为**每角色一个独立实例**（`build_chat_llm(role=...)`）：可分别配置模型 / 端点 / 温度 /
    超时（`LLM_<字段>_<角色>` 或 `config/agent_llm_config.json` 的 `roles` 段），
    每个子 Agent 另有独立 checkpointer；同时加入**服务端回执确认**的模型记录
    （`response_metadata.model_name` → 运行记录 `agent_models` + 报告首表「各 Agent 模型」一行）。
    实测一次真实运行：对接 Agent 走 `deepseek-v4-pro`、其余走 `deepseek-flash`，
    四个角色实例号互不相同、调用次数分别为 3 / 2 / 2 / 4，对接结果仍全部来自真实 Vina。
    顺带修掉一个隐蔽的优先级陷阱：`.env` 里的全局 `LLM_TEMPERATURE` 原先会**压掉**配置文件里
    按角色的显式设置，现按「角色环境变量 > 角色文件项 > 全局环境变量 > 全局文件项」排序。
22. **界面无法配置全局参数**——原先所有参数只能手改 `.env` / `config/agent_llm_config.json`，
    改完还要重启；也没有任何地方能看出「某个字段当前的值到底来自哪儿」。
    现新增**导航栏 + 设置页面**（`#/settings`）：字段表由 `GET /api/settings` 下发，
    覆盖 API 接入、模型管理（拉取端点模型列表 + 每角色连通性测试）、5 个 Agent 的模型与调用参数、
    对接默认值、运行与性能参数；每个字段标注**生效值来源**，保存写入 `config/local_settings.json`
    （已 gitignore / 打包排除），模型类改动**自动重载 Agent**无需重启服务；
    部署保护项（`PORT` / `DOCKING_MAX_LIGANDS` / `UPLOAD_MAX_MB`）保持 `.env` 优先，界面只读。
    该功能经过一轮对抗性复核，并修掉了复核发现的真实问题：密钥在「上游错误回显」与
    `额外请求头` 两条路径上可能明文外泄（现分别做**错误脱敏**与**值掩码**）、
    手改成结构错误的设置文件会让**服务起不来**（现做结构净化并退化为内置默认）、
    清空某项后环境变量**残留旧值**且 `reset` 会误删 shell/CI 注入的键（现记录注入基线并只回滚自己注入的键）、
    点一次「测试连通性」会**清空模型登记表**从而破坏运行记录里的「各 Agent 模型」溯源（现只临时接管该角色并还原）、
    服务原先监听 `0.0.0.0` 而设置接口无鉴权（现默认只听 `127.0.0.1`，并新增 `--host` 与告警）。
    构建过程中被测试抓到两个真实缺陷：① 角色字段规格表漏写 `group` 位置参数，导致 `kind` 全部退化成
    `str`（**API Key 会被当成普通字符串回显**）——被 `tests/test_settings.py` 的「密钥只写不读」用例抓到；
    ② 布尔字段（如 `save_poses`）的初始快照与控件显示状态不一致，未改动的字段也会被判定为已修改并写入
    设置文件，破坏「继承」语义——被 `scripts/ui_e2e.js` 的 DOM 级用例抓到。两者均已修复并留下回归用例。

23. **受体/配体的「特殊化学」被静默处理**——标准对接流程默认「纯蛋白 + 中性单片段配体」，
    但真实输入常有金属酶、血红素/NAD 辅因子、盐/反离子、含金属配体。原实现的问题不是「没处理」，
    而是**处理得无声**：受体准备直接丢掉全部 HETATM（金属离子、辅因子一起没），
    配体侧把 `CC(=O)[O-].[Na+]` 整个丢给 Vina（Na⁺ 参与打分、污染分数），
    并且共晶配体识别对**所有** HETATM 求质心（硫酸根、甘油、离子会把盒子带偏）。
    现按「**工具只报事实、化学判断交给 Agent**」改造：
    - 受体准备逐条统计 `dropped_hetatm` / `dropped_waters` / `kept_hetatm`（以**最终 PDBQT** 为准），
      新增 `keep_hetatm` 白名单参数；要求保留但缺 meeko 模板的残基（HEM/GOL/NAD…）会被**逐个剔除**
      并在 `unsupported_hetatm` 里点名，而不是让整次对接崩掉或假装保留成功（实测 `ZN/MG/CA/FE/MN/CL`
      可直接保留，`HEM` 需要模板）。这些字段贯穿上传响应 → `.site.json` sidecar → 对接结果块 →
      `notes` → 运行详情 → 页面告警卡片，**只拿 PDBQT 也不会丢**。补救办法（提供模板 SDF /
      直接给已备好的 PDBQT / 去掉 keep_hetatm 重跑）随错误信息一起给出。
    - 配体侧新增确定性化学体检 `describe_ligand`：盐/反离子按最大有机片段对接并写明移除了谁
      （`removed_fragments` + `dock_smiles`，原 SMILES 仍作合并主键），金属/净电荷/未定义手性/
      偏大分子给出 `ligand_warnings`，解析失败返回原因而非静默跳过。这些告警同样汇总进 `notes`
      与运行详情（`result.notes` 此前根本没进 `GET /api/runs/{id}` 的裁剪结果，用户看不到）。
    - 顺带修掉两个真实缺陷：① 共晶配体识别改为复用 `pockets.cocrystal_ligand`
      （按残基分组取最大团、排除离子与结晶添加剂），位点来源会写清是哪个配体
      （`共晶配体(HEM)质心`）；② 受体准备缓存原先按时间戳比较，而 `prot.pdb` 每次都重写，
      导致**缓存恒失效、每次重跑 meeko**，且 `keep_hetatm` 变化时还可能误用旧结果 ——
      现按准备后结构的内容哈希（`.sha1` sidecar）判定。
    新增 17 条回归用例（`tests/test_ligand_receptor_chemistry.py`）看护上述行为，
    其中「要求保留却无模板必须点名而不是崩掉」「盐必须拆分并说明」「sidecar 必须带化学溯源」
    都是客观断言。

24. **用户写了分子、系统却对接了示例库**——聊天/说明里用「名称 + 空格 + SMILES」这种最自然的写法
    （如「华法林 CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O；布洛芬 CCC(C)Cc1ccc(cc1)C(C)C(=O)O」）时，
    旧的分词只按 `,;
` 切分，整句被当成一个 SMILES 解析失败 → 受理层判定「未提供候选分子」→
    **静默退回内置示例分子库**，用户最后看到的是别人的分子（benzamidine…）被对接打分，
    而日志里只有一句「跳过无法解析的片段」。现改为：
    ① `core.ligands.extract_smiles` 按「含空白」的词法切分并回填名称（`名称 SMILES`、
    一句话里夹带的分子、`名称:SMILES` 三种写法都支持，散文会被忽略）；
    ② 受理层在表单没给分子时先做这次确定性抽取，抽到就用（`ligands.source="message"`，
    指令里直接给出可导入的 `名称:SMILES` 清单），**只有确实没有任何分子信息时**才允许示例库兜底；
    ③ `_resolvable_ligands` 把 `message` 计入「用户已给出分子」。
    实测一次真实 agent 运行（1A6M 血红素蛋白 + 上述两个分子）：正确对接了华法林 −6.97 /
    布洛芬 −7.02 kcal/mol，报告里还主动说明了「HEM×43 被剔除，本次结果是脱辅基(apo)条件下的结论，
    若要研究血红素口袋请提供原始 PDB」 —— 这正是「特殊体系交给 Agent 判断」的预期行为。

25. **文档与代码漂移**（2026-09-14 文档审计，30 处不一致）——最危险的一类是「会让后续 AI 按错误契约
    改代码」：`docs/api.md` 仍写着已删除的 `critic` 角色与「3 个子 Agent」、承诺服务端会「自动补齐缺失环节」
    （实际只记录、不接管）、称阳性对照留空会用「系统默认对照」（实际留空即跳过）、
    chat 折叠模式「不注入任何参数」（实际会注入系统默认参数块）；README 还有「5 个角色」「check_web 48 项」
    「ui_e2e 38 项」等过期数字。**更正来源是代码**，并顺手把「`molecules` 只回前 5 条预览」「SSE stage 含
    `done`」「可排序字段 14 个」「黑板 site/pockets 由口袋 Agent 写入」等缺失契约补齐。
    新增 `tests/test_docs_consistency.py`（6 条机器可判定的一致性断言：已删角色不许复活、角色名必须来自
    `ROLES`、代码读到的环境变量必须在 `.env.example` 登记、README 必须指向开发手册、承诺的门禁脚本必须
    存在、不得再承诺已移除的自动补齐），让文档漂移在 CI 层就被拦住。
26. **工程规范只存在于口头约定**——源码里已有 116 处 ruff `noqa`（说明按 ruff 规则写码），
    但仓库**没有** ruff/mypy 配置与依赖、**没有**任何 CI 文件，新接手者与 AI 无法复现这套约定。
    现新增零依赖静态门禁 `scripts/lint_local.py`：静默 `except: pass`（必须打日志或写明
    `# 允许静默：<原因>`）、`src/` 非 CLI 的 `print`、裸 `os.getenv`（白名单：config/settings/paths/
    logging_setup/load_env）、缺 `from __future__ import annotations`、公共函数缺返回类型、
    超长文件（>700 行需登记豁免）、函数内 import 未注明原因（`--lazy-imports`）。
    采用**基线式增量门禁**（`scripts/lint_baseline.json`，只允许收窄）：存量 251 处缺类型等已登记，
    新增违规必须为 0。首批清理即清零静默 except（15 处，其中 9 处补日志、6 处标注合法静默原因）、
    `print`、缺失的未来注解；并把 `DEPLOY_PROTECTED`、`POSITIVE_CONTROL_NAME`、`_parse_json_object`、
    `_as_text`、`_env_int`、相对路径换算、亲和力排序（6 处）等重复实现各收敛为**唯一实现**，
    删掉 18 组零引用符号与 `api/app.py` 中整块复制 `intake.py` 的死副本。

27. **前端重做（代码风 · 统一设计 · 动效）与"导出/报告"能力补齐**——本轮把界面从"能用"推到"统一、耐看、可验证"，
    同时补上用户点名要的报告与下载能力，并修掉一个真实并发缺陷：
    - **设计系统**：`web/styles.css` 从零重写（3096 行、106 个设计令牌、0 处 `!important`）。
      基调 = 深色 IDE / 可观测性控制台：等宽字体即语义（数值/ID/参数/文件名/日志一律 `tabular-nums`，刷新不跳宽）、
      小节标题统一 `//` 注释符、微标签大写化、四层底色（页面 < 面板 < 卡片 < 悬浮）+ 1px 边框 + 小圆角。
      面板/按钮/输入框/表格各只有一个基类，变体只改颜色与尺寸 —— 这才叫"所有元素统一设计"。
    - **布局**：工作台两栏（左「任务配置」随整行等高，把原先面板下方的空白收进面板里；右「编排 + 执行」），
      **「结果与产物」跨两列全宽**（表格与图表需要横向空间，3840 宽屏终于用得上）。窄屏 ≤1100px 自动单列。
    - **动效**：统一 140/180/220ms + `cubic-bezier(.4,0,.2,1)`；面板入场、按钮 hover 上浮/按下回弹、
      编排节点按 `data-state` 呼吸脉冲、实测时间轴时间条从 0 平滑展开、进度条动画条纹、日志行一次性淡入、
      KPI 数字滚动（等宽防抖）、标签页滑动下划线、设置页/报告目录平滑滚动；
      **`prefers-reduced-motion: reduce` 下全部降到 0**（可访问性）。
    - **导出与报告**：结果面板右上新增统一导出条（报告 PDF / 报告 MD / 排序 CSV / 位姿 ZIP / 打包下载）并显示
      本次运行的**规范文件名前缀**；报告页新增**章节目录**（由 Markdown 标题生成、点击平滑跳转）。
      新增 `report.pdf`（matplotlib PdfPages，零新增运行时依赖）：封面 = 标题 + 一行摘要 + **工具与版本**
      （vina/rdkit/meeko/p2rank/python/matplotlib，取不到写"未知"）+ 生成时间；正文按章节分页、页脚带 run id 与页码、
      内嵌本次运行的真实图表；两条落盘路径（流水线 / 多 Agent）都会生成，失败只 warning 不影响主流程。
      报告"任务与参数"新增 `随机种子 seed / seed_policy`，排序 CSV 同步新增这两列（可复算）。
      **下载命名规范**：`dock_{run_id}_{受体}_{N}mols_{kind}.{ext}`（如
      `dock_20260914-164720-6383_thrombin_1DWC_2mols_report.pdf`），受体名只保留 `[A-Za-z0-9._-]` 并截断，
      分子数未知时省略该段；规则**只有一处实现**（`runs.download_name()`），经 `GET /api/runs/{id}` 的
      `downloads` 下发给界面，避免前后端两套命名逻辑漂移。
    - **修掉一个真实并发缺陷**：受体准备产物原先只按源文件 basename 命名（`<base>.pdbqt` 等），而缓存目录全局共享 ——
      两个并发运行（或两个都叫 `receptor.pdb` 的上传）会写同一组文件互相覆盖，出现"我请求保留 ZN，拿回别人的
      HEM+ZN 结果"的静默串数据；它也正是全量测试里那条偶发失败的根因（两个 pytest 进程同时准备同名受体）。
      现改为**内容寻址**（文件名带源文件内容哈希 + keep 集哈希，展示名仍是原始 basename），
      并新增 `tests/test_receptor_race.py`（3 线程并发准备同名受体，断言各自结果与请求一致、产物文件名互不相同）。
    - **过程事故与加固**：搬移 `index.html` 的大块 DOM 时，用字符串替换但**没有先断言插入锚点存在**，
      导致"块被删掉但没插回去"、67 个 id 静默消失；靠 Firefox `cache2` 里缓存的响应恢复了原始文件
      （与 213 基线逐 id 对齐），随后改成"先切块校验标签配平 → 先插入再删除 → 断言 id 数与无重复"的流程，
      并把这条写进 `docs/architecture.md` §13 陷阱清单。视觉验证也从"只能靠人眼"变成可门禁：
      `scripts/ui_shot.py check`（6 页：控制台错误/横向溢出/图片失败）与 `baseline`/`diff`（逐像素回归，
      动态内容自动冻结，自检 0.000%）。

28. **「上传的文件用不上 / 气泡被长清单挤没」两个真实缺陷，以及随之而来的异构输入归一化**（2026-09-17）：
    - **上传的 `.sdf` 读不到**（run `20260917-112206-5017`）：协调 Agent 拿到的是**裸显示名**
      `PGR.sdf`（日志 `SDF 读取失败: File error: Bad input file PGR.sdf`），而落盘名是
      `<时间戳>-<哈希>-原名`。根因是受理层在「对话 + 折叠高级设置」时把附件的 `molecule_file`
      置空，渲染给编排层的话里只剩文件名。修法：受理层带上附件字段；`tools/dispatch.py` 新增
      `looks_like_molecule_path()` / `resolve_molecule_file()`（绝对/相对 → uploads/cache/assets
      精确名 → 后缀匹配），**命中多个候选时返回 `candidates` 不猜**，解析失败逐条回传 `attempted`；
      分发消息一律携带**解析后的绝对路径**。
    - **气泡被「引用文件」清单撑爆**：显示与传输分离 —— 气泡只显示用户输入 + 紧凑 chip
      （文件名 + R/L，完整路径在悬停提示），机器拼接的清单只留在发给服务端的指令里；助手长回执
      （>24 行 / 1800 字）自动折叠。`scripts/ui_e2e.js` 新增 14 条断言同时看护两侧。
    - **统一输入归一化层（v0.15）**：见上方能力清单与 `docs/api.md` §9.5；新增
      `tests/test_input_normalization.py`（23 条）与 `tests/test_molecule_path_resolution.py`（14 条）。
    - **同一批 bug 的收尾修复（v0.16）**：
      ① **附件路径里的哈希被当成受体名**（run `20260917-122453-0404`：模型把
      `.../20260917-122453-c6b872-...-PGR_120.sdf` 里的 `c6b872` 读成「用户点名的受体 C6B872」，
      在线解析失败后整个 120 分子运行被问句阻断、零计算）。现在受理模型看到的是**不含绝对路径**的
      指令视图，确定性规则与 LLM 合并都只在**用户自己写的正文**里找受体名，丢弃时写进 `llm_notes`；
      ② **金属配合物的 3D 嵌入失败**（代森锰 `S=C([S-])NCCN/C1[S-]->[Mn+2]/[SH]=1` 直接判失败）——
      增加随机坐标重试（不改任何化学信息），该分子现在能生成 PDBQT；真失败时错误文本会点名金属并给出
      可操作建议；③ **空异常消息**导致的「对接失败 XXX:」没有原因，改为回落到异常类名；
      ④ InChIKey 结构若来自**内置映射表**，归一化 notes 会点名列出，不让用户误以为结构直接来自其文件。
      新增回归测试 8 条（`tests/test_unresolved_receptor.py` 5 条、`tests/test_ligand_receptor_chemistry.py` 3 条）。
29. **输入解析「保证解析」+ Vina 效率与机器自适应（v0.17）**：
    - **InChIKey 不再是单向死路**：新增 `core/inchikey.py` —— 内置表 → 本地缓存
      （`assets/cache/inchikey/<KEY>.json`）→ **PubChem 在线反查**（`INCHIKEY_ONLINE=on` 默认开）。
      在线拿到的 SMILES **必须回算 InChIKey 与查询值逐字一致**才采用（防命中错结构），
      通过后写缓存、下次离线即用；失败原因（未收录/网络不可用/校验不一致）会写进跳过原因，
      不再只显示「无法解析」。溯源在 `input_normalization.json` 里点名区分
      「内置表 / 本地缓存 / 在线还原」三种来源。
    - **Excel（.xlsx）真读**：`openpyxl` 进运行时依赖（`requirements-local.txt` + `pyproject.toml`），
      解析改为**扫描所有工作表**并合并（说明页在前的常见排版不会再得到 0 个分子），逐表上报条数；
      单元格规格化（整数不再变 `2244.0`、日期不带 `00:00:00`）；坏行带工作表名与行号上报。
    - **Vina 效率：并发规划改为「线程优先 + 由部署机器推导」**（详见下图与 §4）：
      唯一的输入是 `machine_profile()` —— cgroup 配额 > CPU 亲和性 > `cpu_count`，结合物理核与
      `MemAvailable`；同一份代码在 4C/8T 笔记本自动得到 3×3、容器 `--cpus=4` 得到 3×1、
      64C/128T 得到 16×8。均匀小分子库实测快 **1.83×**（38.5s vs 70.4s）、总 CPU 少 29%、
      利用率 44.6% → 58.1%；重尾真实库（137 分子）主组 73s → 62s。**所有对接都在 worker 进程里跑**，
      因此单分子也能被「停止」秒级杀掉（旧实现 ≤7 分子是进程内多线程，杀不掉）。
      `GET /api/health` 新增 `machine` 与 `docking_plan`，部署后可一眼核对。
30. **修复报告里记录的两处执行异常（2026-09-17 实测，均为「静默错误」类）**：
    - **① 首轮误用默认受体 → 分数全 0.0**：Vina 在**盒子内没有任何受体原子**时不报错、不告警，
      直接返回**全 0 能量**（`affinity=0.0`）。0.0 不是分数而是「什么都没算」，却被当成合法结果
      一路流到排序/CSV/报告。修复：新增 `core/receptors.py::box_atom_stats()`，
      `dock_library` 在**调用引擎之前**数盒内原子，为 0 则**拒绝该受体**并在 notes 里写明
      「距最近受体原子 X Å」；行级再把全 0 能量（Vina 与 AutoDock 两条路径）判为 `error`。
      实测：盒子来自另一个蛋白时被拒（距最近原子 75.24 Å），正确受体的盒子照常出分（盒内 568 个原子）。
    - **② 属性评估 Agent 连续 3 次读到「黑板上无分子」**：真实原因是**没有任何地方往黑板写过分子** ——
      `import_molecule_library` 只落盘不写黑板，而它的返回 notice 与 `run_property_assessment` 的
      docstring 都承诺「留空参数即用共享黑板」。于是 147 条库只有前 20 条被评估（报告数据缺口），
      且以 `status=ok` 悄悄通过。修复：导入成功即 `board.add_molecules()`（并写一条 note）；
      大库时 `normalize_molecule_library` 只回前 N 条（完整清单留在黑板），避免把上万条 SMILES
      灌进子 Agent 上下文。实测：30 条库的 `properties.json` = **30 条**（修复前为 0/20）。
    - **顺带收紧：默认不再使用示例库/示例受体**（产品要求）——`AgentRequest.allow_example_fallback`
      与 `pipeline.run_pipeline` 默认值由 `True` 改为 `False`；受理层新增确定性识别
      `intake.user_requested_example_library()`（用户明确说「用示例库」时才渲染
      `allow_example_fallback=true`）；协调 Agent 提示词删掉「缺省用 thrombin/1DWC」这类默认受体点名，
      改为**受体缺省时以工具返回为准，不得自行假定受体名**。
    - 新增 `tests/test_run_anomalies.py`（7 条）并更新 4 处旧语义用例；全量 pytest **408 passed**。
31. **对话气泡不再堆工具调用信息（界面体验）**：原先 `appendToolItem()` 会在聊天页**额外**往对话流里
    镜像一条「调用工具：X／工具返回：X／节点更新：tools」简讯，与右侧「工具轨迹」「阶段日志」完全重复，
    长报告被挤在下面看不见。现在工具事件只进轨迹面板，对话流只保留用户/助手往来与运行级状态
    （「运行完成 · run_id …」「运行已取消」）；`scripts/ui_e2e.js` 新增 5 条断言看护
    （气泡无工具字样、工具返回原文不入对话流、轨迹面板照常保留、运行级状态仍在、报告正文正常渲染）。
    - **本轮排查中发现的另外两个问题（一并修掉）**：
      ① **口袋 Agent 给的盒子会绕过库级下限** —— 只有用户显式给的盒子才该「一动不动」，而口袋 Agent
      提交的是**工具产物**（中心要尊重、尺寸应受库级下限约束）。实测 120 个农药大分子库：P95 跨度
      40.8 Å，若沿用 Agent 顺手给的 22³，绝大多数分子只能落到互不可比的 large 组；现在主盒按下限
      抬到 30 Å（受 `POCKET_MAX_SIZE` 夹取），两组数量与尺寸都如实写进结果与报告。
      ② **跨度抽样串行太慢且期间无任何反馈** —— 单个大分子 ETKDG+MMFF 要 1–3 s，120 个串行 ~5 min；
      现在 ≥8 个样本走进程池（实测 5.0× 加速，进程池不可用时静默退回串行且结果一致），抽样前后
      各一条日志并把「正在确定对接盒」推给界面。
      ③ 顺带删掉 `core/docking.py` 里**被静默遮蔽的第二个 `dock_library`**（62 行死代码），
      并新增 `tests/test_source_structure.py`（AST 检查重复顶层定义 + `core/` 反向依赖）永久看护。
    - **「停止」按钮在对话模式下形同虚设（本轮实测发现并修复）**：用真实 147 分子库点击停止，
      流立刻显示「已取消」，但服务器上的 Vina 仍在满负荷跑，load 从 18 涨到 40，还有新分子
      不断算完。三个叠加原因，逐个修掉：
      ① **对接层根本没拿到取消标志** —— `cancel_event` 只在流水线里传递，多 Agent 模式的
      `molecular_docking` 从未传给 `dock_library`；现在按 run id 取同一个协作式标志传下去。
      ② **只在「有分子算完」时才查标志** —— 一整批都是超大柔性分子时（真实库里有 118 个可旋转
      键的分子），第一批要跑十几分钟，循环压根转不到；现在起一个守护线程盯标志，置位即终止进程池。
      ③ **执行器会「重新拉起」被杀的 worker** —— 147 个任务一次性提交，`terminate()` 后管理线程
      发现队列里还有一百多个任务，于是重开 worker 继续啃；现在**先** `shutdown(cancel_futures=True)`
      取消队列、**再**终止（并在 1 s 后对不响应 SIGTERM 的 worker 强杀 —— 实测 20 个 worker
      都「未响应 SIGTERM」）。
      另外补上「已取消的运行不得再开始新的对接」护栏：API 在流结束时清掉取消标志，图里在途的
      工具调用会拿一个**全新的、未置位**的标志重新开跑（实测取消 3 s 后又起了一个 147 分子的池）。
      现在实测：点击停止 **0.2 s** 内流结束、`status=cancelled`、pose 文件数**冻结**、
      load 从 20.6 回落到 10.3，且没有任何新对接。新增 6 条回归测试（`tests/test_resilience.py`）。

32. **对话只显示「一份规范报告」+ 模型叙述与结论；多处静默丢数据/默认资源收尾（2026-09-17 后续）**：
    - **气泡渲染规范化报告（report.md）**：`final` 事件里 `state.reportMarkdown` 是**模型自己写的**
      文本（章节/措辞/裸网址都不受控），运行结束时会被落盘的 `report.md`（9 节、零网址、图表内嵌）替换 ——
      现在**两者并存**：模型自己的叙述与结论（含过程异常说明）保留在气泡正文，规范报告作为独立小节
      「规范报告（report.md · 唯一权威版）」渲染在其下方；超长时沿用既有「展开/收起」折叠。
    - **顺带修掉一个真实渲染缺陷**：整体重渲染（choices/备注触发）会把助手气泡打回**纯文本**，
      于是报告在聊天里显示成 `#`/`**`/表格源码；现在已定稿的助手消息固定走 Markdown 渲染。
    - **正文不再出现裸网址**：`stripBareUrls()` 剥掉 `http(s)://…`（`[文字](url)` 只留文字），
      但**保留 Markdown 图片**（图表要能直接显示），并清掉因此只剩冒号的空行。
    - **多次对接调用不再丢数据**：`tool_io.write_json` 是覆盖写 ⇒ `docking_tool.json` 只有**最近一次**
      调用，而落盘层原先只信这个文件（文档却承诺「多次返回做合并」）。现在按 `receptor_key` 合并
      「产物文件 + 消息历史里的全部调用」，分批对接/蛋白质库场景不再丢掉早先受体块；
      来源标注只在真的多出数据时写 `tool_file+tool_message`。新增 2 条回归测试。
    - **默认受体名不再进入 Agent 视野**：受理层的假设与渲染不再点名 `thrombin`，改为
      「由系统内建默认受体兜底（**具体受体名以工具返回的 notes/结果块为准**，报告照抄该名称）」；
      工具运行时的 note 仍如实给出真实受体名，报告的可追溯性不变。

33. **修复「停止后同一会话续聊报 400」——跨轮次线程状态自愈**：
    用户实测报错 `400 - An assistant message with 'tool_calls' must be followed by tool messages
    responding to each 'tool_call_id'. (insufficient tool messages …)`。根因：点「停止」或运行失败时，
    模型**已经发出** `tool_calls`，而工具回执永远不会产生（工具被中止）；LangGraph 的 checkpointer
    把那条 AIMessage 记进 thread 历史，于是**同一会话的下一轮**把非法消息序列发给 OpenAI → 400，
    整段对话卡死。
    - **修法**：新增 `agents/threads.py`，**每轮开始前**检查该 thread 的历史，对每个没有回执的
      `tool_call_id` 在**所属 AIMessage 正后方**插入占位 `ToolMessage`（内容如实写明「该工具调用在上一轮
      被中断、**没有产生结果**、不要假设数据」），再用 `RemoveMessage` + 整体重写写回状态
      （`add_messages` 只能追加、不能插中间）；三个图入口（对话流 / `/run` / OpenAI 兼容）统一调用。
    - **两条纪律**：① 占位回执是**事实说明**，不是编造结果；② **只在下一轮开始前**修，不在取消那一刻修
      —— 取消时工具可能仍在跑，若它稍后补回真实回执，同一 `tool_call_id` 会出现两条回执（同样被拒）。
    - **真实验证**：第 1 轮跑到对接阶段点停止（`cancelled`）→ 日志
      `线程自愈：为 2 个中断的 tool_call 补了占位回执（call_00_SM9i…、call_01_HUsO…）` →
      同一 `conversation_id` 续聊 `status=ok`（修复前这里必然是 400）。新增 `tests/test_thread_healing.py`
      （5 条，含真实 LangGraph + MemorySaver 的写入验证与幂等性）。

34. **属性评估漏网路径修复 + Agent 间「按文件交接」（v0.19）**：
    - **属性评估仍然收不到分子的真实原因（已复现）**：协调 Agent 被提示词允许**跳过 import
      直接把文件交给 `run_docking`**；此时分子只进了对接工具，**没有任何人写共享黑板**，
      属性评估读到 0 条 —— 而且返回 `status=ok`（静默降级）。修复三处：
      ① `molecular_docking` 解析出分子后**发布到黑板**（谁解析到数据谁发布）；
      ② 属性侧加第二道兜底：黑板为空时退回**本次运行请求里的 `molecule_file`**（与对接同一口径）；
      ③ 读取统一走 `core.ligands.read_molecules_any()`，**运行产物 JSON 与用户上传文件同入口**。
      实测：黑板为空 + 请求带文件 → 属性评估从 0 条变为 **30/30 条**。
    - **Agent 间改成「文件优先」交接**（用户要求：很多数据本来就该按文件传，不必全靠黑板）：
      · `tool_io.record()` 把产物**绝对路径**记进 `run.data["tool_files"]`，
        `artifact_path()` / `artifact_refs()` 把它交给模型（`molecules_file` / `properties_file` /
        `docking_file` / `pockets_file` / `binding_file`）；
      · 下游工具新增文件参数（都可留空回退黑板）：`molecular_property_assessment(molecules_file=…)`、
        `normalize_molecule_library(molecules_file=…)`、`binding_mode_analysis(molecules_file=…)`、
        `check_binding_consistency(docking_file=…)`、`run_docking(molecules_file=…)`（`molecule_file` 别名）；
      · 协调 Agent 与四个子 Agent 的提示词都加了同一条纪律（`agents/prompts.py::DATA_HANDOFF_RULE`）：
        **大表走文件路径、黑板只放小状态（受体/位点/阳性对照/计数）**；
      · 真实验证：运行记录里出现
        `tool_files: {molecules: …/molecules_tool.json, properties: …, docking: …}`，
        且该轮属性评估 **30/30**、对接受体为上传的 8ZE2、0 个 0.0 分。
    - 新增 5 条回归测试（`tests/test_run_anomalies.py`）。

---

35. **配体质子化态：从「只告警」改为「运行级统一策略 + 逐分子溯源」（本轮）**：
    真实问题：库里大量盐/羧酸根/多质子化碱，同一分子的离子态与中性态对接行为差别很大；
    旧实现只在结果里提示「请确认质子化态」，**不提供统一口径**，于是同一批筛选里混着两种化学形式。
    现在：`docking.protonation`（环境变量 `LIGAND_PROTONATION`，界面**基础参数区**直接可见）——
    `neutralize`：只用 RDKit `Uncharger` 中和**带净电荷**的分子（v0.21 起不是默认）（季铵等永久电荷中和不了就
    如实说明"无法中和"，不假装处理过）；`keep`：完全保持输入形式、仅告警。
    策略是**运行级**的（`core.ligands._protonation_policy`：显式参数 → 本次运行请求 → 环境变量 → 默认），
    同一批分子同口径；逐分子记录 `{policy, applied, charge_before, charge_after, method}`，
    **原始 SMILES 始终保留**（对接行的 join key 仍是原始 SMILES，`dock_smiles` 是实际参与对接的形式）。
    **理化性质与对接同口径**（`compute_properties(smiles, protonation)`），排序 CSV 新增
    `protonation_policy` / `charge_input` / `charge_used` 三列，报告 1.3/4/6 节分别给出策略、
    被调整的分子数与"这是有意的化学处理而非误差"的说明。库级归一化只汇总一条 note（避免上百条重复）。
    **实测（确定性流水线 `20260917-215007-8219`，6 分子含 2 个阴离子 + 1 个季铵）**：
    乙酸根/己二酸根被中和后再对接（`dock_smiles` 为中性形式），季铵如实记为「无法中和」，
    原始 SMILES 全部保留；排序 CSV 的 `protonation_policy`/`charge_input`/`charge_used` 取值正确。
    **同时发现并修复一个真实缺陷**：性质计算若把返回的 `smiles` 换成中和后的形式，
    带电分子就与对接行（主键是原始 SMILES）对不上，排序表里分子量/logP/Lipinski **整列为空** ——
    现规定 `smiles` 恒为原始输入（合并主键），中和后的形式放 `protonated_smiles`（有回归测试看护）。
36. **报告不再重复内容 + 新增「推荐化合物排行」+ PDF 内嵌 2D 结构（本轮）**：
    - **重复内容（用户指出）**：第 8 节过去把协调 Agent 的**整份报告**贴进来，同一批数字在报告里出现两遍。
      现在 `reporting.content.extract_agent_conclusions()` 只摘录**结论/建议类小节**
      （参数摘要/分子列表/属性评估/可视化/对照数据/数据可用性等"复读"小节丢弃，
      但其中的「一句话结论」会摘出来）；完整原文仍在产物 `agent_report.md`。
      实测：5.7 KB 的 Agent 报告 → 报告内只保留 0.9 KB 结论。
    - **推荐化合物排行（新第 3.1 节）**：综合分 = `0.45×亲和力 + 0.20×配体效率 + 0.20×类药性 + 0.15×理化窗口`
      （绝对尺度、不按本批库归一化，跨运行可比；权重可在设置页改 `runtime.rank_weights`）。
      等级 A/B/C，且**亲和力弱于 −6 kcal/mol 的分子等级封顶为 C**（没有结合强度的分子不该因"小而类药"
      被推荐）；排行按等级优先、同级按综合分降序（避免"排行第 2 名是 C 级"的自相矛盾）。
      缺少数据的分量按 0 计入并标注，无对接分数的分子不进排行（如实计数）。
      **Agent 写理由**：新增两个工具 `recommend_compounds`（算真实排行）与
      `submit_recommendations`（写逐分子理由，按 name/SMILES 核对是否在榜，不在榜的回传 `unmatched`
      拒绝写入）——数值一律来自真实计算，Agent 只补"为什么推荐/如何推进"。
    - **PDF 内嵌 2D 结构**：新增 `charts/recommend_structure_grid.png`（按综合分降序的 RDKit 2D 结构网格，
      格内标注排名/综合分/等级/亲和力），随 Markdown 报告一起嵌入 PDF。
    - **理化性质空间图只画 top**（用户要求：整库散点太乱）：`property_scatter_chart(top_only=True)`
      只画与第 3.1 节同一集合的前 N 个分子（`runtime.recommend_top_n`，默认 10）。
    **实测（真实 LLM 多 Agent 运行 `20260917-215513-5225`，53 s）**：协调 Agent 自动完成
    `recommend_compounds` → `submit_recommendations`（**6 条逐分子理由全部写入**），
    报告第 3.1 节含排行表 + 2D 结构图 + Agent 理由 + 筛选建议，裸 URL 为 0；
    对话里的数值与工具输出逐项一致（未发现编造）。

37. **质子化态支持「目标 pH」+ 对新手关键的参数移出折叠（本轮）**：
    - **目标 pH（`docking.protonation_ph` / `LIGAND_PROTONATION_PH`，默认 7.4）**：
      `docking.protonation` 新增第三个取值 `ph` —— 先中和到中性形式，再按**内置官能团 pKa 规则表**
      把羧酸（4.5）/ 胺（10）/ 脒（12.5）/ 胍（13）/ 咪唑（7.0）/ 吡啶（5.2）/ 四氮唑（4.9）/
      磷酸（2.1）/ 磺酸（−1）/ 硫醇（8.5）/ 酚（10）等加到目标 pH 的状态。
      行为已按方向逐项验证：乙酸在 pH 1.5 保持中性、pH 7.4 为阴离子；乙胺 pH 7.4 为阳离子、
      pH 10 中性；甘氨酸 pH 7.4 得到**两性离子**（净电荷仍为 0 但化学形式变了 → 以规范 SMILES
      是否变化判定 `applied`，不看净电荷）；胍只 +1（不会被"脂肪胺"规则二次质子化）；
      咖啡因（N-取代咪唑）在任何 pH 都保持中性；季铵的永久电荷如实保留。
      **逐分子记录命中的规则**（`rules: [{name, pka, action, rule}]`，如 `pH 7.4 > pKa 4.5`），
      规则表版本 `PKA_TABLE_VERSION` 与免责说明（**规则近似，不是 pKa 预测**）同时写进报告 1.3/6 节。
      非法 pH（非数字/越界）一律回退 7.4，绝不静默改口径。
    - **界面（用户要求"对对接特别重要的参数放在折叠外，新手也容易调"）**：
      「目标 pH」与「质子化态策略」并排放在**基础参数区**，带常用值预设
      （胃酸 1.5 / 溶酶体 4.5 / 生理 7.4 / 弱碱 8.0，`<datalist id="ph-presets">`）；
      策略不是 `ph` 时该项**置灰**并给出气泡说明（避免"填了却不生效"的静默无效）。
      同时把**阳性对照 SMILES**（结果可靠性的关键基线）与**最大分子数**（控成本护栏）从「更多参数」
      上移到基础区；「更多参数」只留需要专业知识的项（结合位点盒坐标、口袋引擎、位姿保存、上传受体），
      摘要行写清里面有什么。`check_web` 门禁同步改为校验新布局（119 项）。
    - **默认值选择**：`docking.protonation` 默认改为 **`ph`（目标 pH 7.4）** —— 对接的通行假设，
      比"一律中和"更接近真实条件；也让界面上的「目标 pH」默认就是可编辑的（不必先改策略）。
      纯中和/原样保留仍是一键可切。实测（确定性流水线，同一批 6 分子）：
      pH 7.4 下乙酸根保持 −1、乙胺 → +1、甘氨酸 → 两性离子（净电荷仍 0 但形式已变，`applied=True`）、
      苯甲脒 → 脒阳离子（+1，凝血酶体系的关键）；pH 1.5 下乙酸根 → 中性酸、甘氨酸 → 阳离子。
      理化性质与对接逐分子同形式（主键仍是原始 SMILES）。

38. **上传不再立刻处理文件：只有「校验」或「开始运行」才解析/准备（本轮）**：
    用户要求「不要一上传文件就开始处理文件，要在用户开始后才能开始处理文件」。此前 `POST /api/uploads`
    在**上传瞬间**就做重活：受体现场准备 PDBQT（meeko，含位点推断，~1.5 s 起）、分子库完整解析
    （大 SDF 更久）—— 用户只是选了个文件就先被处理一遍，且这些结果**运行时还会重做一次**。
    现在：上传接口只做「落盘 + 低成本内容嗅探判断类型」，立刻返回 `path` 与 `pending: true`；
    新增 `POST /api/uploads/inspect` 供用户**主动**校验（界面文件卡片里的「校验文件（解析预览）」），
    与运行阶段**共用同一套实现**（`_receptor_upload_payload` / `_ligand_upload_payload`），
    因此预览与真正运行不会出现「预览 30 个、实跑 29 个」的偏差。实测：上传 1.6 s → **0.01 s**；
    校验/运行才花 1.6 s。界面同时说明「已保存，待运行」，不再显示假的解析结果。
39. **受体质子化与配体对齐到同一目标 pH（本轮）**：
    用户提问「检查一下受体有没有做与配体相同 pH 情况的质子化」——**此前没有**：受体走 meeko 残基模板，
    等价于固定的 ≈pH 7 标准态，与配体按目标 pH 分配的状态不同口径；而 HIS 互变异构（HID/HIE/HIP）
    与 ASP/GLU 质子化直接决定氢键/静电互补（凝血酶 S1 的 ASP189，PROPKA pKa ≈ 6.6 是典型例子）。
    现在（`core/receptor_ph.py`）：策略为 `ph`（默认）时用
    `pdb2pqr --ph-calc-method=propka --with-ph=<pH>` 生成 PQR → meeko `--read_pqr` 写受体 PDBQT，
    **与配体同一目标 pH**；逐残基留痕：实际应用的 HIS 状态（从 PQR 氢原子读出，不是猜）、
    各可滴定残基在目标 pH 下的质子化计数（PROPKA 摘要）、边界残基（|pKa−pH|<0.5）、
    剔除的主链不全残基（1DWC 的 GLY H 246 会让 pdb2pqr 直接失败）。
    实测（1DWC，300 残基）：冷启动 1.5 s、缓存命中 0.03 s；pH 4.0 → 5 个 HIS 全为 HIP、
    ASP 4/18 与 GLU 12/22 质子化；pH 7.4 → HIS 为 HID×3/HIE×2、ASP/GLU 均未质子化。
    **失败绝不假装**：找不到 pdb2pqr（`PDB2PQR_BIN` 可指定）、工具报错、或要求保留 HEM/NAD 等
    有机辅因子（pdb2pqr 无法参数化）时，回退原标准流程并回传 `receptor_protonation.applied=false`
    + 原因；报告 §1.2 会明确写出「受体未按目标 pH 准备、两侧口径不一致」并给出三种补救方案。
    顺带修掉一处重复劳动：口袋分析原本**又准备了一遍受体**（且用模板态），现在复用同一份
    内容寻址缓存（同一 pH + 同一 keep 集），既省时间又消除口径分叉。
    另修一处**溯源丢失**：Agent 模式里口袋分析先把受体准备成 PDBQT、对接阶段只拿到这个 PDBQT 路径，
    原来会丢掉「按 pH 准备过」的记录，报告把**做过** pH 处理的受体误报成「未按 pH 准备」。
    现在 pH 产物带同名溯源侧车（`<base>_ph7.4.site.json`），只拿 PDBQT 也能恢复
    HIS 状态/可滴定残基/剔除残基；外来 PDBQT 则如实标注「无法确认是否与目标 pH 一致」。

40. **受体 pH 准备的分级重试 + 「模板不匹配被丢弃的残基」不再静默（本轮，用户实测 8ZE2 触发）**：
    用户在 **8ZE2**（Gr64a 蔗糖结合态，四条链）上看到「受体未按目标 pH 准备：meeko 读取 PQR 失败」。
    **根因（逐层查清）**：
    ① 该结构里 ILE402 的羰基 O 与 THR406 的羟基 O 只相距 **2.15 Å**（正常氢键 2.6–3.0 Å，属几何异常）；
    ② `pdb2pqr` 默认会做**氢键网络优化**，于是把 THR406 的羟基氢 HG1 摆到距 ILE402 羰基 O **1.15 Å** 处；
    ③ meeko 读 PQR 时用**共价半径距离**推断残基间键，把这一对当成**残基间共价键**，而任何残基模板都不允许
       这种连接 → `Template matching failed for: A/B/C/D:402, A/B/C/D:406`；
    ④ 我的 pH 路径当时**没带**标准路径那套容错开关（`-a/--allow_bad_res`、altloc 重试），于是整条 pH 准备失败，
       按设计回退到标准准备（meeko 模板默认态 ≈pH 7）——所以报告如实写了「未按目标 pH 准备」。
    **修复**：
    - `core/receptor_ph.py` 改为**分级阶梯**，每档都记进 `info["attempts"]`：
      `opt`（pdb2pqr 默认氢键优化 + meeko 严格模板匹配，His 互变异构最接近真实）→
      `noopt`（几何摆氢，**保住全部残基**，只是 His 的 HID/HIE 由几何而非氢键环境决定）→
      `delete`（才允许删掉模板不匹配的残基）→ `noopt+delete` → 仍失败才回退标准准备。
      **明确不先删残基**：8 个被删残基（每条链 2 个）的代价远大于"氢摆放不最优"。
    - meeko 的输出现在会被解析：**被丢弃的残基逐个上报**（`dropped_bad_residues`）并写进报告 §1.2；
      标准（模板态）准备路径此前**完全静默**地丢残基（`-a` 的副作用），现在同样上报。
    - 溯源新增 `variant` / `pdb2pqr_noopt` / `attempts` / `best_variant_failed` / `dropped_bad_residues`；
      缓存只在「上次就是最优档」或「已记录最优档确实失败」时复用，避免一次偶然失败被永久缓存。
    **实测（用户那份文件，300+ 残基 × 4 链）**：`opt` 失败 → `noopt` 成功，
    `applied=true`、`variant=noopt`、**丢弃残基 0 个**、HIS HID×20、盒心取自共晶配体 Z9N 质心；
    冷启动 18.6 s，缓存命中 0.08 s。新增 4 项回归测试（含真实 8ZE2 用例与两档降级的行为断言）。
    **同时修掉一个相邻问题（同一轮实测暴露）**：上传受体的运行里，子 Agent 传来的注册表受体名
    （默认 `thrombin`）被当成"多受体蛋白质库"，于是**对默认凝血酶又白跑了一遍**（6 个分子 × 1 轮），
    报告里多出一个受体块，用户会以为跑了两个靶点。现在 `tools/dispatch.run_docking` 有一条硬护栏：
    本次运行**有上传受体**时，忽略子 Agent 传来的注册表受体名（URL/结构文件路径不受影响），
    并在分发给对接子 Agent 的指令里写明「只对接上传的那个受体」。
    实测（run `20260918-094946-0829`）：单受体块、`variant=noopt`、丢弃残基 0、盒内 556 原子。

41. **口袋说明 + 姿态–口袋结合分析（2D/3D 图）（本轮，用户要求）**：
    用户要求「让 agent 把口袋也分析说明一下，写报告与推荐小分子时加上当前对接姿态与口袋的
    结合情况分析，2D 与 3D 的结合分析图」。新增三块，全部基于**真实坐标**：
    - **相互作用引擎**（`core/interactions.py`）：读真实位姿（`poses/pose_*.pdbqt`，只取第一个 MODEL
      = 最优位姿）与受体 PDBQT，逐残基/逐原子对判定 —— 氢键（配体 N/O ⋯ 受体 N/O ≤ 3.5 Å）、
      盐桥（双方部分电荷 |q| ≥ 0.3 且异号、≤ 4.0 Å）、疏水接触（双方碳 ≤ 4.2 Å）、
      π–π 堆叠（芳香碳对数 ≥ 3 且 ≤ 5.5 Å，**单对靠近不算**）、金属配位（金属 ⋯ 配体 N/O ≤ 3.0 Å）；
      同时给出盒内残基清单（不依赖口袋引擎，实验位点/手填盒也能说清口袋组成）。
      口径是**几何启发式**（未做氢键角度/质子化方向校正与能量分解），阈值随结果一起交出去。
    - **2D / 3D 图**（`reporting/charts.py`）：2D 用**位姿文件里记录的 SMILES**（= 对接实际化学形式）
      画结构式，再用 `REMARK SMILES IDX` 把相互作用原子映射到 2D 图（不靠猜名字；meeko 的映射会
      **折成多行 REMARK**，必须逐行累加 —— 只读第一行会漏原子，已修），按相互作用类型着色画虚线到残基标签；
      3D 画口袋内受体原子（按元素着色）+ 配体骨架（键长判据连键）+ 相互作用虚线 + 对接盒线框，等比例。
    - **Agent 工具 + 报告**：新增 `analyze_pose_pocket`（口袋说明 + 逐分子结合情况，供协调 Agent
      写进叙述与推荐理由；**不许编造工具没给出的残基**）；报告新增 `### 5.1 结合口袋分析`
      （盒子来源/依据、盒内残基与组成特征、与实验位点一致性、口袋告警）与
      `### 5.2 推荐分子的姿态–口袋相互作用（2D / 3D）`（逐残基表 + 两张图），
      推荐排行 §3.1 每个分子也加一行「姿态–口袋（几何分析）」；关闭「保存位姿」时**如实说明**
      没有位姿可分析（不臆测结合方式）。口袋子 Agent 的提示词也要求返回 `pocket_explanation`。
    **实测**（run `20260918-094946-0829`，8ZE2 + 6 分子）：P2Rank pocket1（score 45.58）盒内 24 个残基、
    芳香 10 / 带电 4；咖啡因与 7 个残基接触（氢键×3、疏水×25，最近 THR257 3.06 Å，
    并与 TYR200/PHE134 有 π–π 堆积），2D/3D 图均正常生成并内嵌 PDF。
    新增 11 项回归测试（`tests/test_pose_pocket.py`）。

42. **运行参数常驻在对话框上 + 默认搜索强度 16（本轮，用户要求）**：
    用户要求把「对接引擎 / 质子化态策略 / 目标 pH / 输出位姿数 n_poses / 搜索强度 exhaustiveness /
    阳性对照 SMILES」**直接放在对话框上，不要折叠到设置里**，并把默认搜索强度设为 **16**。
    - **布局**：把共享参数 DOM 拆成两块 —— `#param-common`（运行参数：一键预设 + 上述 6 项 + 最大分子数）
      与 `#params-rest`（配体来源 / 位点盒 / 上传受体）。`app.js::mountParams()` 按模式分别挂载：
      对话模式把 `#param-common` 挂到对话框正上方的 `#chat-quick-params`（**不在任何 `<details>` 里**），
      其余挂进「其他设置」折叠；参数模式两块按顺序放回 `#manual-params-mount`。
      同一份 DOM 两种模式复用，不会出现两套同名字段。
    - **语义（v0.26 修正后）**：对话模式**只下发用户真正改动过的字段**，未改动的一律留空、由服务端按需自动决定：
      - 什么都不改 → `advanced=false`，请求体里**没有任何运行参数字段**；展开再收起「其他设置」**不算改动**（不注入）。
      - 只把搜索强度拖到 24 → `advanced=true` 且请求体只多一个 `exhaustiveness`。
      - 点「恢复系统默认」→ 回到自动，又不带任何字段。
      `exhaustiveness` 默认勾着「自动」（滑块禁用、标签显示**自动**）：滑块位置（16）只是"取消自动后"的起点，
      自动时不下发，服务端 `core/params.py` 按库柔性 / 盒体积规划（`AUTO_PARAM_BASE_SCREENING=16`，即基准同为 16）；
      取消自动则为显式值，`param_plan.source=user`，不再被规划改写。
      界面上「自动」的实际表现：intake 横幅显示 `搜索强度 exhaustiveness=自动（按库柔性/盒体积规划，基准 16）`、
      `输出位姿数 n_poses=默认（筛选 1）`、`引擎=auto`。默认值三方一致：界面滑块起点 = 设置页默认
      （`docking.exhaustiveness=16`）= 自动规划基准（16），因此"不改也能落在 16 档"。
    - **修复的用户投诉**：早期实现把「打开过高级设置」当成"用户改过参数"（`advancedTouched`），
      于是"打开又关闭高级设置"就把所有默认值塞进请求体 —— 用户明确指出"我没有指定高级参数啊"。
      现改为**逐字段 `state.touchedFields`**：只有真正触发过 `input`/`change`（或点过预设 / 填入上传位点）的字段才下发；
      也不再无条件下发 `skip_positive_control`（留空本来就是"不做对照分析"，无需声明）。
    - **默认 16 的连带更新**：`settings.py`（表单默认）、`api/schemas.py`、`intake._system_default_params()`、
      `tools/dispatch.py` / `tools/docking.py` 的工具签名与说明、`cli.py`、
      `core/params.py` 自动规划基准（`AUTO_PARAM_BASE_SCREENING` 12→16，保证"自动"与"默认"同档）、
      Docking 子 Agent 提示词与协调 Agent 提示词；一键预设改为 **快速 4 / 平衡 16 / 高精度 32**。
    - **顺带修**：`#param-common` 改挂到 `#chat-quick-params` 后，`.chat-mode` 隐藏受体下拉的 CSS 选择器失配
      （受体下拉在对话模式又冒出来了）→ 选择器同时支持 `#param-common.chat-mode`，并在 `#params-panel` 上也保留类名。
    门禁更新：`check_web` 122 项（新增「运行参数常驻容器 / 两处挂载 / 默认 16」断言）、`ui_e2e` 186 项
    （新增运行参数条可见性 + 6 个字段齐全 + 不在折叠里 + 预设 32 + **逐字段注入场景**：
    什么都不改 → 不带任何参数；只改搜索强度 → 只带该字段；展开又收起高级设置 → 仍然不带参数）。

43. **LangGraph Studio / LangGraph CLI 部署层（新增 `langgraph-deploy/`，用户要求）**：
    把整个项目**原样**加载进 LangGraph Studio 与 CLI，`projects/` 一行未改（独立 venv + editable 安装）。
    - **7 个图**：`coordinator`（多 Agent 主入口）/ `pipeline`（确定性流水线，无 LLM）/ `intake`（受理层）/
      `property`·`pocket`·`docking`·`binding`（4 个子 Agent）；`docking_graphs/runtime.py` 把项目的
      ContextVar 运行上下文（`current_run` / `current_blackboard` / `request_context`）适配成平台可用的
      「运行作用域」，跑完调用项目自己的 `persist_agent_run()` + `run.finish()`，产物与网页端**完全一致**
      （`docking.json` / `ranking.csv` / `report.md` / `report.pdf` / `report_agent.md` / 各角色调用次数）。
    - **图不带 checkpointer**（Platform 自己托管）：用 `mock.patch` 在构建瞬间把 `get_memory_saver` /
      `workers._checkpointer` 换成「返回 None」，工具列表、提示词、per-role LLM 全部复用项目源码，零漂移。
    - **测试**：`langgraph-deploy/tests/` 63 项（离线 55 + 真实服务集成 8）。集成用例自起隔离 dev server
      （端口 2033 + 临时工作区，assets/config/web 软链、var 独立），覆盖真实 Vina 对接、3 路并发隔离、
      无分子错误路径、未知 assistant、多轮记忆、服务日志无 `BlockingError`。
    - **修掉两个部署缺陷**：① 内层 Agent 图原先在**事件循环里**构建（每请求重建 5 个 LLM，且在
      「不豁免 `os.stat`」的 blockbuster 配置下必抛 `BlockingError`）→ 改为图构建阶段建好并缓存；
      ② pipeline 节点不自行收尾运行状态（内层只返回字典时 `run.json` 永远 `status=running`）→ 现在
      失败落 `error` 再抛、成功兜底 finish。
    - **使用陷阱（已写进测试）**：多轮记忆必须用线程级端点 `POST /threads/{id}/runs/wait`；把
      `thread_id` 放进 `POST /runs/wait` 的 body 会被当成无状态运行，第二轮看不到第一轮历史。

44. **LangChain 1.x / LangGraph 1.x 规范改造 P0（本轮，用户要求「按规范设计」）**：
    先做了一份 45 条差距的规范审计（`var/reports/lc_conventions_audit.md`，含逐条 `file:line` 证据与
    P0/P1/P2 分期），然后落地 P0（零行为变更、纯增益）：
    - **`state_schema` 基类改为 `langchain.agents.AgentState`**（原为 `langgraph.graph.MessagesState`）：
      只有前者带 `structured_response` / `jump_to` 通道，否则 `create_agent(response_format=...)`
      的结构化输出**永远无法终止**（实测 `GraphRecursionError`）。新增断言：编译图必须含这两个通道。
    - **5 个 Agent 图全部加 `name=`**（默认名会退化成 `'LangGraph'`，Studio / 回调归属无法区分）：
      `coordinator` 与 4 个子 Agent（`property`/`pocket`/`docking`/`binding`）。
    - **checkpointer 用规范名 `InMemorySaver`**（`MemorySaver` 只是兼容别名）。
    - **提示词显式组合**：去掉 `globals()[name] = ...` 改写，改为 `PROPERTY_SP = _PROPERTY_SP_BASE + DATA_HANDOFF_RULE`；
      新增兜底 `COORDINATOR_SP`（配置 `sp` 为空时使用，`system_prompt=cfg.get("sp") or COORDINATOR_SP`）。
    - **修文档漂移**：`molecular_property_assessment` 的 docstring 曾写了并不存在的 `protonation_ph`；
      `runtime/llm.py` 的死引用 `src/main.py` → `-m docking_agent`；`architecture.md` 工具数 9→12、路由数
      「~22」→「实测 33（其中 `/api/*` 23）」、`MemorySaver`→`InMemorySaver`。
    - **打包修复**：项目此前**没装进自己的 venv**（`import docking_agent` 依赖 `PYTHONPATH=src`，
      `[project.scripts]` 完全失效）→ `scripts/setup.sh` 改为 `-e .` 安装，并新增 `tests/test_packaging.py`
      守住「依赖清单与 `pyproject.toml` 集合一致」「包已安装且 `project_root()` 指向仓库内 projects/」
      「命令行入口可导入」。
    - **测试补齐**（审计指出 5 处零覆盖）：新增 `tests/test_agent_conventions.py`（8 项）、
      `tests/test_session_memory.py`（9 项：滑动窗口 / ContextVar 语义与任务隔离 / SSE 编解码与
      `stream_agent_sse` 帧序列）、`tests/test_packaging.py`（3 项）；并把原先用
      `inspect.getsource` + 正则数工具列表的脆弱用例（`test_core.py`）换成**真正构建图并读 ToolNode
      绑定工具名**的版本。
    门禁：`pytest` **490 项全绿**（原 466 + 24）、`lint_local` 新增 0、`check_web` 122/122、
    `ui_e2e` 186/186、`snapshot_ids` 无移除、`smoke_test.py` 46/46、部署层 63 项（含 8 项真实服务集成）、
    真实网页端运行（真实 LLM + Vina，46 秒 / 25 项产物）与真实 Studio coordinator 运行（33 秒）均通过。
    **P1（中间件/记忆/限额/重试/context_schema）与 P2（结构化输出/subgraph/state reducer）待续**。

45. **LangChain 规范改造 P1（本轮，用户已确认三项口径）**：把「状态/记忆/限额/重试/工具入参」从手写
    实现换成 LangChain 官方设施，**行为与对外契约不变**。用户确认的口径：
    **限额只做安全网级**（阈值取高，不改变长任务行为）、**子 Agent 保持无状态**、**P2 只类化小参数**。
    - **上下文窗口改用官方 `trim_messages`**（新增 `agents/state.py`，`coordinator` 再导出保持 import 路径）：
      旧写法 `add_messages(old,new)[-40:]` 会**切断 `AI(tool_calls)+ToolMessage` 配对**，留下悬空
      ToolMessage → OpenAI 兼容端点直接 400（`agents/threads.py` 要自愈的就是这类状态）。
      现在 `token_counter=len + start_on="human" + include_system=True`：窗口从人类消息开始、
      配对被完整保留，且任何异常都退化为旧的下标滑动（绝不因裁剪失败让整轮对话崩掉）。
    - **中间件四件套**（新增 `agents/middleware.py`，协调 Agent 与 4 个子 Agent 共用）：
      `ModelRetryMiddleware`（此前**零重试**，一次 429 就整轮失败）、`ModelCallLimitMiddleware` /
      `ToolCallLimitMiddleware`（安全网级：默认 200/400，`exit_behavior="end"` → 优雅结束，
      已算出的对接结果不会因触发安全网而丢掉）、`SummarizationMiddleware`（长对话摘要，
      默认 >30 条触发、保留最近 20 条；短任务零额外模型调用）。
      阈值/env 见 `.env.example`（`AGENT_RETRY_MAX` / `AGENT_MODEL_CALL_LIMIT` / `AGENT_TOOL_CALL_LIMIT` /
      `AGENT_SUMMARY_*`）。**不引入 `ToolRetryMiddleware`**：工具是真实计算，重试=重复烧算力。
    - **工具小参数类型化**（新增 `tools/schemas.py`）：`site_center` / `site_size` / `center` / `size`
      改为 **number 数组**（`run_docking` / `molecular_docking` / `set_docking_site`），`engine` 改 Literal；
      `floats_to_text()` 让旧调用方的字符串形态仍然可用，并且**非法坐标会得到如实提示**
      （不再被静默当成"没给位点"而悄悄换盒子）。下发子 Agent 的消息也改为数组形态（否则子 Agent 按旧
      提示词传字符串会被 schema 拒）。`molecules_json` / `*_file` 的「路径即总线」契约**保持不变**。
    - **子 Agent 无状态语义测试化**：`invoke_worker` 每次独立 thread（防串扰）现在有回归用例
      （同运行两次调用必须拿到不同 thread）并写进 `architecture.md`。
    - **流式新增 `custom` 通道**：`stream_mode += "custom"`，节点/工具可用 `get_stream_writer()`
      上报真实进度（新事件类型 `{"type":"custom","payload":...}`）；**既有 12 种事件类型一个都没改**
      （前端不认新类型也不会坏）。
    - **`context_schema` + `ToolRuntime` 已实测可用**（下一步的机制验证）：`create_agent(context_schema=Ctx)`
      + `graph.invoke(..., context=Ctx(...))` + 工具签名 `runtime: ToolRuntime[Ctx]` → 工具内
      `runtime.context` 拿到的就是传入对象，且 `runtime` 自动从模型可见 schema 中排除。
      注意：这类工具的 `tool.get_input_jsonschema()` 会因 dataclass 里的可调用字段而报
      `PydanticInvalidForJsonSchema`，断言 schema 要用 `tool.args` / `tool.tool_call_schema`。
    门禁：`pytest` **514 项全绿**（P0 后 490 → +24）、`lint_local` 新增 0、`check_web` 122/122、
    `snapshot_ids` 无移除、`ui_e2e` 186/186。**P1 剩余：`context_schema`+`Runtime` 的"删 ContextVar"阶段（P2）。**
46. **LangChain 规范改造 P1 ④：`context_schema` + `Runtime`（双读落地，本轮）**：
    - **新增 `runtime.context.AgentContext`**（`run` / `blackboard` / `request`）与读取器
      `context_of(runtime)` / `active_run(runtime)` / `active_blackboard(runtime)` /
      `active_request(runtime)` / `current_agent_context()`。口径：**有 runtime 用它（权威），
      没有才回退 ContextVar**，因此"走图"与"直接调用"两条路径行为一致，迁移期不留半吊子状态。
    - **`coordinator` 与 4 个子 Agent 全部声明 `context_schema=AgentContext`**；图调用方全部传入 context：
      网页 SSE（`stream_agent_sse(..., context=)`）、`/run` 非流式、`/v1/chat/completions`
      （这个兼容端点此前**没设 ContextVar**，现在显式传 `AgentContext(run=run)`，工具层终于拿得到运行目录）、
      CLI 的 `flow` / `agent` 两条路径、子 Agent 调用（`invoke_worker`）以及部署层包装节点。
    - **23 个 `@tool` 全部改为 runtime 优先**：签名末尾追加 `runtime: ToolRuntime[AgentContext] = None`
      （LangGraph 自动注入、**不出现在模型可见 schema**，旧直调 `.func(...)` 仍可用），函数体内
      `current_run.get()` / `request_context.get()` / `get_blackboard()` → `active_*(runtime)`（35 处）。
      **残留**：15 处**内部辅助函数**（`_invoke_checked` / `_analyze` / `_from_blackboard` /
      `_coerce_molecule_list` 等）仍读 ContextVar —— 它们与工具同一条调用栈、行为等同；
      P2 删 ContextVar 时一并处理（代码注释已标注）。
    - 实测证据：`graph.invoke(..., context=AgentContext(run=..., blackboard=...))` 能把上下文一路送进
      **真实工具**内部（`tests/test_context_schema.py` 用假模型发 tool_call 验证，不是纸面配置）。
    - ⚠️ 已知限制（写进测试注释）：带注入 `ToolRuntime` 的工具，`tool.get_input_jsonschema()` 会因
      dataclass 里的可调用字段报 `PydanticInvalidForJsonSchema`；**模型侧真正的 schema 是
      `tool.tool_call_schema`**，相关断言已全部改用它。

47. **LangChain 规范改造 P2-a：子 Agent 结构化输出（`response_format=ToolStrategy`，本轮）**：
    - **新增 `agents/reports.py`**：4 个 pydantic 报告模型（`PropertyReport` / `PocketReport` /
      `DockingReport` / `BindingReport`），关键字段与既有 `tools/dispatch._REQUIRED_KEYS` 对齐，
      且 `extra="allow"` —— 子 Agent 真实报告里的 `artifacts` / `top` / `summary` / `concurrency`
      等字段**必须原样透传**，不能被 schema 吃掉。
    - **子 Agent 图挂 `response_format`**：由框架**强制模型调用结构化输出工具**，返回值进
      `structured_response` 通道（pydantic 已校验）；`invoke_worker` 优先把它序列化成 JSON 字符串返回，
      因此**协调 Agent / persistence / 报告链路的对外契约完全不变**（仍是 JSON 原文），
      `_invoke_checked` 的「解析失败重试一次」保留为文本路径的安全网（既有语义不丢）。
    - **实测发现并解决一个供应商兼容问题**：DeepSeek 的 **thinking mode 不支持强制 `tool_choice`**
      （结构化输出必然报 `400 Thinking mode does not support this tool_choice`），真实运行当场失败。
      因此做成**「能上就上、被拒绝就优雅降级」**：`AGENT_STRUCTURED_OUTPUT=auto`（默认）时，只有
      **供应商拒绝类**错误（关键词匹配 `tool_choice` / `thinking mode` / `response_format` …）才降级到
      「不带 `response_format` 的同款图」（同一 LLM 实例，懒构建并缓存），并在运行日志里**明确告警**；
      限流/超时等无关错误照常抛出（绝不掩盖真实故障）。`on` = 不降级（被拒就报错）、`off` = 完全不用。
      **同一端点两种机制都实测不可用**：`ToolStrategy`（强制 `tool_choice`）报 `Thinking mode does not
      support this tool_choice`；`ProviderStrategy`（原生 `json_schema`）报 `This response_format type is
      unavailable now`（关掉 `LLM_THINKING_<ROLE>` 也一样）。因此本部署下结构化输出会走「一次尝试 → 记一笔
      说明 → 文本契约」的路子；换到支持结构化输出的端点/模型时**无需改代码**即自动启用。
    - 回归：`tests/test_structured_worker_output.py` 13 项（模型字段覆盖 / 额外字段保留 / 4 个角色都挂对
      ToolStrategy / 结构化优先与文本回退 / `_invoke_checked` 一次通过且**不**重试 / 文本路径仍会重试并
      返回 `agent_output_invalid` / 拒绝检测的**窄匹配** / 无关错误必须上抛 / `off` 时完全不挂
      `response_format` / **同一角色被拒后记住该状态**，后续调用直接走文本契约图，不再重复付一次 400 的代价 /

48. **LangChain 规范改造 P2-b：工具链路彻底去 ContextVar 依赖 + run.json 原子写（本轮）**：
    - **工具侧内部辅助函数全部改为 runtime 优先**（P1 只做了 `@tool` 本身）：`_analyze` /
      `_invoke_checked` / `_coerce_molecule_list` / `_live_molecule_row` / `_receptor_blocks` /
      `_ranked_rows` / `_from_blackboard` / `current_ranking` / `_default_receptor_from_run` /
      `_resolve_specs` / `mark_receptor_unresolved` / `publish_choices` / `clear_choices` 都接受
      `runtime`（或 `run`），**所有工具调用点显式传入**（`runtime=runtime` / `run=active_run(runtime)`）；
    - **数据总线 `agents/tool_io.py` 支持显式 `run=`**（`record` / `artifact_path` / `load` /
      `artifact_refs`），工具侧一律传 `run=active_run(runtime)`，落盘层 `persistence` 传它自己的 `run`；
    - **实时进度回调用 `partial(_live_progress, runtime=runtime)` 绑定上下文**（否则 `dock_library`
      以 `(done, total, result)` 调用闭包时拿不到 runtime）；
    - **新增回归守卫 `tests/test_context_independence.py`（4 项）**：`tools/` 下**任何裸读
      ContextVar 都会让测试失败**；允许的兜底必须写成 `x if x is not None else current_run.get()`
      的显式形式；残余依赖集中在**带理由的显式白名单**里（兜底层本身、黑板访问器、日志前缀、
      质子化/归一化的运行回溯、两个非工具辅助函数、落盘层）。白名单里的函数若被删掉也会报错，
      防止"永久豁免"腐烂。
    - **`run.json` 改为原子写**（同目录临时文件 + `os.replace`）：此前并发读取可能读到写了一半/空文件
      （外部脚本真实报过 `json.loads: Expecting value: line 1 column 1`）。
    - 门禁：`pytest` **545 项全绿**、`lint_local` 新增 0（`tools/dispatch.py` 因显式透传超 700 行，
      按既有 `BIG_FILE_EXEMPT` 机制登记并写明理由）、部署层 63 项 + `smoke.py --with-coordinator` 通过；
      **真实网页端运行** 54 s / status ok / 27 项产物（含 2D+3D 姿态图与 1MB PDF）/ 逐分子实时事件正常
      （证明 `tool_io` 显式 run、进度回调、姿态与排行链路都没坏）。
    - **P2-c（本轮，用户决策：迁到 LangGraph store + 保留 ContextVar 兜底）**见记录 49。

49. **LangChain 规范改造 P2-c：黑板接入 LangGraph `store`（用户决策）**：
    - **`Blackboard` 支持两种后端**：`store=None`（默认，进程内对象 + `RLock`，CLI/单测不变）与
      `store=<BaseStore>`（图调用链路：父图与 4 个子 Agent **共享同一个 store 实例**）。
      store 后端**按字段写**（`("blackboard", run_id)` 命名空间下每字段一个 key），
      避免整档覆写丢更新；构造时 `_hydrate()` 从 store 读回已有状态，因此
      **同一 run 的第二个视图（另一进程/另一个子图）能看到前一个视图写的内容** ——
      这正是接入 store 的意义（与 checkpointer 对齐、平台可见、跨进程可读）。
    - **父图与子 Agent 共享 store**：`coordinator.build_agent` 与 `workers._make_agent` 都传
      `store=shared_store()`（`agents/blackboard.py` 的进程内 `InMemoryStore` 单例）——
      子 Agent 是分别编译的图，只有共享同一 store 实例，`set_docking_site` 写下的盒子才能被
      Docking 子 Agent 读到。
    - **`active_blackboard(runtime)` 优先级**：`runtime.context.blackboard` → `runtime.store` + run
      → ContextVar 兜底（按用户决策保留）。API 层与部署适配层同样改用 `store_blackboard(...)` 取黑板，
      避免「进程内对象 / store」两份状态。
    - **真实端到端验证（关键协作路径）**：**不给任何位点坐标**跑完整筛选 —— pocket 子 Agent 用
      P2Rank 预测口袋 → `set_docking_site` 写入 **store 黑板** → Docking 子 Agent 从 store 读到盒子并完成对接
      （实测 `box_source=口袋分析 Agent 指定坐标：引擎=p2rank(auto)，采纳口袋=pocket1(rank=1,…)`，
      50 s / status ok / 26 项产物 / 报告含 §5.1 口袋分析）。
    - 回归：`tests/test_context_independence.py` 新增 4 项（跨视图共享、run 隔离、按字段写、
      `active_blackboard` 优先 store）。门禁：`pytest` **545 项全绿**、`lint_local` 新增 0、
      `check_web` 122/122、`snapshot_ids` 无移除、`ui_e2e` 186/186、部署层 63 项 + `smoke.py --with-coordinator` 通过。
    - 注：审计里另外几项 P2（`Send` map-reduce 并行、`HumanInTheLoopMiddleware`、把
      「工具内调子图」改成 subgraph 节点）按审计建议**保留现状**（与 `architecture.md` 的
      「单机 32 核、不引入分布式、进程池吃满核」定位冲突，需先有运营口径），不在本轮范围。
      LangGraph `store` / graph state？审计把它列为**需要作者定夺**的开放问题：若"进程内、单次运行隔离"
      就是既定不变量，则**只需补文档**；若要"同一 conversation 多轮共享受体/分子库"，就该迁到
      `store`（按 run/thread 键）。现状：API/部署层仍注入黑板 ContextVar，工具侧已优先读 runtime。
      降级动作**如实写进运行记录**（run.log），报告与运行详情里能看到本次为什么没用结构化输出）。

50. **标准 Agent Protocol 服务面（阶段 1：后端，用户要求「按标准 agent 服务规范」）**：
    网页端原本只有自定义的 `POST /api/agent/stream`；外部客户端（LangGraph SDK / Studio / 其它平台）
    期望标准协议。新增 `api/agent_service.py`，把这套标准面**薄适配**到既有执行链路上——
    同一个 Run、同一套产物与报告、同一份 store 黑板、同一个受理层，**功能与产物完全不变**。
    - **标准子集（用户确认「最小可用集」，实测 18 个端点）**：
      System（`GET /ok`、`GET /info`）、Assistants（`POST /assistants/search`、`GET /assistants/{id}`、
      `GET /assistants/{id}/schemas`）、Threads（`POST /threads`、`GET/DELETE /threads/{id}`、
      `GET/POST /threads/{id}/state`、`GET /threads/{id}/history`）、
      Thread Runs（`POST /threads/{id}/runs/stream|wait`、`GET /threads/{id}/runs`、
      `GET /threads/{id}/runs/{run_id}`、`POST /threads/{id}/runs/{run_id}/cancel`）、
      Stateless Runs（`POST /runs/stream|wait`）。
    - **7 个助手**（= 7 个图）：`coordinator` / `pipeline` / `intake` / `property` / `pocket` / `docking` / `binding`，
      `assistant_id` 是**名字派生的确定性 uuid5**（重启不变）；`/assistants/{id}/schemas` 直接由
      `AgentRequest` / `PipelineRequest` 生成，**schema 与真实校验同一处定义**。
    - **标准 SSE 帧**（按本机 `langgraph dev` 实测格式）：`metadata` → `messages/partial`（增量 token）/
      `updates`（节点推进）/`custom`（领域事件：阶段、逐分子、候选选择、工具调用）/`messages/complete`
      → `values` → `end`；错误走 `error` 帧。实测 coordinator 流：metadata 1 + updates 11 +
      **messages/partial 570** + messages/complete 1 + custom 2 + values 1 + end 1。
    - **run_id 语义（用户决策：双 id，业务 id 为准）**：`metadata.run_id` 是平台风格 uuid（进程内句柄，
      用于取消/查询），真正的业务运行 id（`var/runs/<id>/`，产物与报告按它落盘）在
      `metadata.business_run_id`；`/runs/{id}/cancel` 与 `GET .../runs/{id}` **两种 id 都接受**。
      踩坑修复：`/runs/wait` 原先写成 `{"run_id": platform_id, **values}` → 把 values 里的业务 run id
      覆盖掉了（有回归测试 `test_wait_payload_never_clobbers_business_run_id`）。
    - **装配方式**：`create_app()` 末尾 `register_agent_service(app)`；标准面复用既有端点的方式是把
      `api_agent_stream` / `api_pipeline_stream` 挂到 `app.state`（同一进程内直调，不再发一次 HTTP），
      因此**既有端点行为零改动**（`/api/agent/stream` 仍是同一个实现）。
    - **已知边界（如实记录）**：`/threads/{id}/state|history` 只对**被 checkpointer 记录的图**
      （`coordinator`）有内容；`pipeline`/`intake`/子 Agent 不写图状态，返回空 state（实测 coordinator
      线程 state=4 条消息 / history=5 个检查点）；thread 登记落在 `var/threads/<id>.json`（原子写）；
      平台 run id 是**进程内**句柄，跨重启请用业务 run id。
    - 门禁：`pytest` **557 项全绿**（+12 `tests/test_agent_service.py`）、`lint_local` 新增 0、
      `check_web` 122/122、`snapshot_ids` 无移除、`ui_e2e` 186/186（前端未改，仍走既有端点）、
      部署层 63 项 + `smoke.py` 通过；真实运行实测：`pipeline` 标准流（custom 12 帧 + values + end）、
      `coordinator` 标准流（570 个 token 帧）、`/runs/wait`、cancel、state、history 全部符合预期。
    - **待续（阶段 2）**：前端 `web/app.js` 切到标准 API（`/threads/{tid}/runs/stream` + 标准帧解析），
      并把 `ui_e2e` / `check_web` 的桩与断言同步更新；旧端点保留为 deprecated 兼容层。

51. **标准 Agent Protocol 阶段 2：前端切到标准面 + 旧端点标废弃（本轮，记录 50 的「待续」已兑现）**：
    - **前端成为标准客户端**（`web/app.js`）：对话模式 `assistant_id=coordinator`、参数模式（流水线）
      `assistant_id=pipeline`，两者都先 `POST /threads`（`if_exists=do_nothing`，幂等）再
      `POST /threads/{tid}/runs/stream`；请求体是标准信封
      `{assistant_id, stream_mode:["messages","updates","custom"], input:{…业务参数, conversation_id, messages:[{type:"human",content}]}}`。
      **`/api/agent/stream`、`/api/pipeline/stream` 在前端代码里已无调用点**（`check_web.py` 有静态断言守住）。
    - **标准帧 → 内部事件的适配层 `standardFrameEvents()`**：`metadata` → 平台 run id
      （`state.standardRunId`，取消时优先用它）；`messages/partial` → `token`；`updates` → `update`
      （**必须剔除 `ts`**：服务端每个帧的 data 都带它，按 `Object.keys(data)[0]` 取节点名会误判成 `ts`）；
      `messages/complete` → `final`；`custom` → 领域事件原样透传（**含 `start`**）；`values` → `done`；
      `error`/`end` 各自处理。渲染、折叠、取消、结果载入、历史与改造前**行为一致**（`ui_e2e` 189 项证明）。
    - **后端配套**：`start` 事件同时进 `custom` 通道 —— 前端必须拿到 `task_spec` / `request`
      才能如实写「开始运行 · 受体文件 …」运行日志（少了它日志会退化成「用户指定受体（未给出名称）」）。
    - **旧端点标注废弃（8 个）**：`POST /api/agent/stream`、`POST /api/pipeline/stream`、`POST /run`、
      `POST /stream_run`、`POST /pipeline`、`POST /cancel/{run_id}`、`GET /health`、`GET /graph_parameter`
      在 OpenAPI 里 `deprecated=True`，`summary` 以 `[已废弃]` 开头并写明替代端点；标准面 18 个 op
      全部有 `tags` + `summary`（`/docs` 里按 System / Assistants / Threads / Thread Runs / Stateless Runs 分组）。
      `PUT /api/settings` 的错误体补上标准 `detail`（保留 `error_message` 供未升级的前端读同一句话）。
    - **门禁**：`pytest` **559 项全绿**（新增 2 项：废弃标注 / tag+summary；修订 1 项 custom 帧计数）、
      `lint_local` 新增 0、`check_web` **124/124**、`snapshot_ids` 无移除（基线刷新到 248 个 id）、
      `ui_e2e` **189/189**、部署层 63 项 + `check.sh` + `smoke.py --with-coordinator` 全部通过。
    - **真实浏览器验证（新增 `scripts/browser_check.py`，Playwright + Chromium）**：桩模式 **11/11**
      （对话与参数模式都必须打标准端点、必须声明 `stream_mode`、长回执必须出现「展开」折叠且可展开、
      不得出现老端点、无 JS 运行时错误，并留截图 `var/tmp/browser_std/*.png`）；
      `--live` 模式 **5/5** —— 真实浏览器 + 真实后端 + 真实 LLM 跑完一次完整筛选
      （`run_id 20260918-183435-5292`，拿到业务 run_id、报告与 2D/3D 姿态图渲染进气泡、未打任何老端点）。
    - **踩坑（写下来免得再犯）**：`ui_e2e.js` 的桩把数组帧写成 `{...data, ts}`，数组被摊成对象
      `{0:…,ts:…}`，前端 `Array.isArray(data)` 判假 → `messages/partial|complete` 全部当成空内容，
      长回执折叠用例当场暴露（已在 `stdFramePairs` 里改成数组不塞 `ts`）；
      另外 jsdom 关窗后未完成的 `loadRun → refreshHistory` 续体会访问失效 `document` 把 e2e 进程带崩，
      现在只忽略这一种已知脚手架噪声（其余异常照旧失败），并在场景收尾等运行真正结束再关窗。

52. **气泡小票与请求体同源（用户实测反馈：什么都没写，却显示「本次下发参数：受体 thrombin / 位点盒 …」）**：
    - **现象**：对话模式只输入一句普通指令、什么都不改，用户气泡仍写着
      「本次下发参数：受体 thrombin / 位点中心 [31.50, 13.74, 24.36] / 位点尺寸 [22.00, 22.00, 22.00]
      / 质子化 ph=7.4 / 引擎 vina / n_poses=1 / 保存位姿」，看起来像前端擅自指定了受体与位点盒。
    - **真实情况（真实浏览器抓包）**：请求体 `input` 只有 `mode/message/advanced/conversation_id/messages`，
      `receptor` / `site_center` / `site_size` **根本没发**。问题出在**显示层**：`paramChips(form)`
      读的是**原始表单**，而对话模式下受体下拉与位点盒是 `display:none` 的隐藏项
      （`.receptor-manual-block` + `.chat-mode`），未改动的运行参数也不下发 —— 界面默认值被当成了"已下发"。
    - **顺带修掉一个反向缺陷**：在「其他设置」里手填的 SMILES（`ligands_text`）**看得见、填得进**，
      却因为 `advanced` 只由运行参数条上的字段触发，被 `payloadToBody` 的 `if (payload.advanced)` 静默丢掉
      （用户填了分子库，等于没填）。现在 `ligands_text` / `allow_example_fallback` 也算显式意图。
    - **修法（显示与传输同源）**：`paramChips(form, params)` 改为从**真正进请求体的那份 `params`**
      生成小票，并按模式分派：
      * 对话模式 `chatParamChips`：只列真的会发出去的字段（附件 / 改动过的运行参数）；
        一条都没有就如实写「**纯指令：不注入任何运行参数（全部按系统默认/指令执行）**」，
        **受体与位点盒既不发送、也不显示**（由指令或口袋分析决定）；
      * 参数模式 `manualParamChips`：表单是权威参数，列出的就是真正下发的值（受体/位点盒照常显示）。
      同时把 `#chat-hint` 的提示语从「始终作为默认值下发」改成「**只把你改动过的项作为默认值下发**；
      受体与位点盒在对话模式不发送」，别再误导。
    - **回归守卫**：`ui_e2e` 新增 4 项（193/193）——什么都没改时小票必须含「纯指令」且不含受体/位点/引擎、
      只改搜索强度时小票只列 `exhaustiveness=24`、手填 SMILES 必须真的进请求体且小票如实显示；
      `check_web` 新增 2 项静态断言（126/126）；`browser_check.py` 真实浏览器新增 5 项（17/17，
      含"改动过的项真的下发、未改动项连小票都不出现"）。
    - 证据截图：`var/tmp/browser_std/chips_untouched.png`（纯指令）与 `chips_touched.png`（只列 exhaustiveness=24）。

53. **「没算任何东西」的运行不再产出空报告（用户实测：「你好」→ 一份 11 KB 的空报告，这是什么鬼）**：
    - **现象**：用户只发了一句「你好」。受理层判定 `decision=reject`、**零工具调用**，但落盘层照样写出
      11 KB 全是空表格的报告 + 4 张空图 + **328 KB PDF**，网页把这份 no-op 运行当作
      「规范报告（report.md · 唯一权威版）」挂在对话气泡上 —— 报告里出现
      「本次输入 0 个分子全部成功对接」「图 1 图片加载失败」这类噪声，历史里还是一条绿色的 `[ OK ]`。
    - **根因**：`agents/persistence.py` 的报告/图表/PDF 产出**没有任何"是否真的算过"的门禁**，
      只要图跑到收尾就无条件生成；前端又无条件把它挂到气泡、并把用户甩到空的结果总览。
    - **修法（三层一起收敛）**：
      1. **后端门禁**：只有真的算过东西（分子库 / 理化性质 / 对接行 / 口袋分析 / 排序）才写
         排序 CSV、图表、`report.md`、PDF；否则只留运行记录 + 协调 Agent 的对话原文
         （`agent_report.md`）+ `result.json`，并在 `run.json` 写
         `no_report_reason: 任务未受理（decision=reject），未执行任何计算`。
      2. **状态如实**：这类运行的 `status` 由 `ok` 改为 **`no_op`**（`result.no_op=True` → `run.finish("no_op")`），
         历史列表显示 **[ SKIP ]**；前端 `done` 事件也写「本次未执行计算」而不是「运行完成」。
      3. **前端不装作有结果**：载入后若「没有报告且没有任何排序行」，不挂规范报告小节、不切到结果总览、
         不写「结果已载入」，改为气泡提示「本次未执行计算（未生成报告）· run_id …：
         受理层判定本次指令不是可执行的筛选任务，因此没有调用任何工具、也没有生成报告」，
         运行提示同步为「本次未执行计算（未生成报告）：<id>　如需开始筛选，请给出候选分子与受体目标」。
    - **实测对照**（真实后端 + 真实 LLM，「你好」）：
      修复前 run 目录 = `report.md 11 KB + report.pdf 328 KB + charts/ 4 张空图 + ranking.csv`；
      修复后 = `request.json / result.json / run.json / blackboard.json / agent_report.md`，
      `status=no_op`、`has_report=false`、`report_markdown` 长度 0、产物 4 项。
    - **回归守卫**：新增 `tests/test_report_skip.py` 6 项（greeting 不产报告、受理通过但零工具产出同样不产、
      **真实对接必须照旧产报告**（防止门禁做过头）、`no_report_reason` 文案）；`ui_e2e` 新增 no_op 场景
      4 项（197/197）；`browser_check.py` 真实浏览器新增 no_op 阶段 3 项（20/20，
      且普通运行的桩记录补上报告与排序行，避免把"桩太假"测成"产品缺陷"）；`check_web` 新增 2 项静态断言。
    - 证据截图：`var/tmp/browser_std/greeting_noop.png`（真实浏览器 + 真实 LLM）。
    - **顺带修的测试脚手架脆弱点**：「导出条 / KPI / 报告目录」原来挑历史里**第一条 `[ OK ]`** 记录，
      现在历史里会出现 `no_op`（分子数 0、无产物）的完成记录，于是要按「已完成 **且分子数 > 0**」挑，
      否则会把"导出条正确禁用"误判成缺陷。

54. **前端重排（用户要求）：对话居中放大 · 组件重排 · 去噪 · 设置页整理 · 补返回按钮**：
    - **对话成为页面中心**：工作台从「左栏配置 + 右栏执行/结果」两栏改为**单列居中**
      （`.layout` flex 列 + `max-width: 1240px`，左右留白对称）；对话区 `.chat-stream`
      `min-height: 46vh / max-height: 60vh`（实测 451px / 980px 视口 ≈ 46% 首屏），
      气泡宽度放宽到 820px，输入框加高到 92px 并把「发送」按钮做成主操作。
    - **重新布局其它组件**：模式切换从两张卡片收成一条分段控件；参数条由「一列一字段」压成
      自适应多列栅格（高度 408px → 214px），每条只留「标签 + 控件」（说明统一进 `?` 气泡）；
      结果与产物提到运行详情之前；**多 Agent 编排 / 阶段日志 / 工具轨迹 / 实时逐分子表**整体
      收进默认折叠的「运行详情」（开跑自动展开，跑完保留可回看）。
    - **去除不必要的内容与提醒**：删除重复提示（参数说明与 `?` 气泡重复的长 hint、模式说明、
      空态里指向已隐藏按钮的文案）；未运行时收起排序工具条 / 结果表 / 分页 / 图例 / 图表
      （首屏只剩一句引导）；对话模式隐藏与「发送」重复的「开始运行」按钮；
      运行日志、进度、工具轨迹等只在实际运行时出现。
    - **设置页整理**：头部收成一条工具带（**← 返回工作台** / 标题 / 配置文件路径 /
      保存·重新加载 Agent·拉取模型·清除界面设置 / 状态）；6 个分组**默认只展开第一组**
      （页面高度 4773px → 1000px 一屏），其余以两栏卡片列出并标注「N 项」；左侧锚点导航保留
      并吸顶；字段仍是「标签 + 控件 + 生效来源 chip」的紧凑两/三列栅格。
    - **返回按钮**：新增 `#btn-settings-back`（设置页头部）+ **Esc** 快捷键；两者都真正切回工作台
      视图（视图、导航高亮、URL hash 三者同步）。此前设置页只能靠顶部导航切回去。
    - **回归守卫**：`check_web` 新增 10 项静态断言（单列居中、对话 ≥46vh、运行详情默认收起、
      首屏 no-run、设置页默认只展开第一组、返回按钮与 Esc、分组项数徽标…）→ **138/138**；
      `ui_e2e` 新增 9 项（返回按钮切换视图与高亮、Esc 返回、首屏无结果状态、运行详情默认收起、
      对话区结构完整）→ **206/206**；`browser_check.py` 新增**真实浏览器布局验收**（对话区占首屏
      ≥40%、左右留白对称且内容宽度收敛、首屏工具栏收起、开跑自动展开运行详情、返回按钮可用）
      → **26/26**。
    - 证据截图：`var/tmp/ui_final/live_chat_new_ui.png`（真实 LLM 对话）、`settings_roles.png`
      （各 Agent 角色三列卡片）、`var/tmp/ui_after3/{chat,manual,settings}.png`（重排后三页整页）。

55. **受体结构入包（用户实测提问：「你提供的打包文件里，有用于对接的受体结构文件吗？」）**：
    - **当时的答案是没有**：`download.zip` 打包的是运行目录，而运行目录里只有配体位姿
      （`poses/pose_*.pdbqt`）与 JSON/图表/报告 —— 受体结构只在 `docking.json` 里留了一个
      **绝对路径**（`assets/receptors/registry/*.pdbqt` 或 `assets/cache/<ts>-<hash>*.pdbqt`）。
      实测证据（run `20260918-195319-3249`）：ZIP 26 个条目、扩展名只有
      `json/csv/md/pdf/png/pdbqt`，其中 `.pdbqt` 全是 `pose_ligand_*.pdbqt`（配体）。
      换台机器/换个人拿到的包**无法复现**这次对接。
    - **修法（用户选择：对接用的 PDBQT + 原始结构）**：运行目录新增 `receptor/`，复制三类文件
      （取得到才放，尽力而为，失败只记 warning）：
      * `receptor/<受体>.pdbqt` —— 本次对接**实际使用**的受体（AutoDock 输入，权威）；
      * `receptor/<受体>_prepared.pdb` —— 准备阶段去水/去杂原子后的蛋白（按 pH 准备时能透过
        `_ph7.4` 后缀找到同一批的 `*_prot.pdb`）；
      * `receptor/<受体>_source.<ext>` —— **原始结构**：用户上传的原文件 / URL 下载的原文件
        （注册表预置受体只有 PDBQT，故通常没有这一类）。
      三者都登记为产物（`receptor_pdbqt_*` / `receptor_prepared_*` / `receptor_source_*`），
      因此同时出现在「中间数据」页签、报告第 9 节产物清单与整包 ZIP 里。
    - **两条路径共用同一实现**（`reporting/artifacts.py::copy_receptor_files`）：
      多 Agent 落盘（`agents/persistence.py`）与确定性流水线（`pipeline.py`）都在写报告**之前**调用，
      保证报告 §9 能列出这些产物。
    - **实测**（真实运行，两条路径都跑过）：
      * 注册表受体 chat 运行 `20260920-211846-8552`：ZIP 29 条目，含 `receptor/thrombin.pdbqt`；
      * 上传受体流水线 `20260920-212015-0184`：`receptor/` 三个文件齐全
        （`*.pdbqt` 230 KB / `*_prepared.pdb` 186 KB / `*_source.pdb` 240 KB），全部进了 ZIP。
    - **回归**：新增 `tests/test_receptor_pack.py` 6 项（三类文件入包并登记、缺文件/URL 只警告不抛错、
      整包 ZIP 同时含受体与位姿、流水线路径同样入包、报告列出受体产物、`_ph7.4` 变体能找到 `_prot.pdb`）；
      `docs/api.md` 新增「3.0 整包里包含哪些结构文件」一节。

56. **报告版式与备注精简（用户要求：推荐排行旁边附 2D 结构、数据用表格、备注别太长）**：
    - **每个推荐分子一张「结构 + 数据」卡片**：新增 `charts/recommend_card_NN.png`
      （左 RDKit 2D 结构、右指标表：综合分/等级/亲和力/LE/分子量/logP/TPSA/Lipinski 违例/
      相似度/对接盒子），报告 3.1 节逐分子内嵌（`##### #N 分子名` → 卡片图）。
      为什么做成一张图：markdown 与 PDF 都没有「把图片放进表格单元格」的可靠画法，
      卡片图让**网页 / PDF / 整包 ZIP 三处长得完全一样**，用户要的「结构就在数据旁边」才成立。
      卡片登记为产物（`recommend_card_01…`），中间数据页签可单独下载。
    - **表格瘦身**：排行表从 11 列收到 6 列（# / 分子 / 综合分 / 等级 / 亲和力 / LE）——
      PDF 渲染器对 >8 列的宽表会降级成「逐行竖排」（`PDF_WIDE_TABLE_COLS = 8`），
      那正是用户说的「不整洁」；其余逐分子指标挪进卡片，完整字段仍在 `ranking.csv`。
      冗余的整张推荐网格图（`recommend_chart`）不再在 3.1 节重复内嵌。
    - **备注精简（网页 + PDF 同一份源）**：新增 `report._brief()`（去加粗 → 取第一个分句 → 超长截断），
      运行笔记压缩成一句话且最多 6 条，推荐理由/推进建议 ≤120 字，规则提示最多 2 条 ≤80 字；
      逐分子的姿态–口袋只留一行并指向第 5.2 节。完整原文仍在 `run.json` / `result.json`。
    - **网页不再被备注拉长**：结果总览的汇总表原来把 `run.notes` **整段**塞进一个单元格
      （5 条 × 100+ 字），现在只显示「运行笔记 N 条（见下方）」，正文移到新的 `#run-notes`：
      每条默认 2 行（`-webkit-line-clamp: 2`）+ 「展开 / 收起」。
    - **顺带修掉一个真实缺陷**：`.md-img-fallback` 的 `display: inline-block` 覆盖了 `[hidden]`
      的 UA 规则，导致**图片明明加载成功也一直显示「图片加载失败：<图题>」**（用户上一轮截图里
      那行红字就是它，而不是图片真的坏了）。改为 `.md-img-fallback[hidden] { display: none; }`。
    - **门禁**：`check_web` **145/145**（+7：卡片/精简/笔记折叠/降级文案）；新增
      `tests/test_report_layout.py` 4 项（卡片逐分子内嵌且紧跟标题、宽表不再出现、笔记与理由被
      精简且原文不入报告、卡片登记产物并进 ZIP）；`ui_e2e` **210/210**（+4：笔记条目渲染、
      汇总表只报条数、展开/收起）；`browser_check.py` **31/31**（+5，新增「报告版式」真实渲染阶段：
      结构卡加载与位置、12/12 图片真的加载、无「图片加载失败」误报、笔记 ≤2 行）。
    - 证据：`var/tmp/ui_final/report_card_web.png`（网页卡片）、`var/tmp/pdf_new-05.png`
      （PDF 第 5 页：6 列排行表 + 结构卡）、`var/tmp/pdf_page_6-06.png`（PDF 卡片页）。
    - 实现位置：卡片渲染独立成 `reporting/cards.py`（图表模块已到 700 行门禁上限，拆开更清晰）；
      产物登记在 `reporting/artifacts.py::_write_recommend_cards`；报告措辞与精简在 `reporting/report.py`。

57. **协调 Agent 的主观能动性：按用户要求定制报告 + 分子 ID 全链路可追溯（用户实测反馈）**：
    - **用户原话**：「报告要按格式，但也不能死板……用户要求带上小分子的 ID 或者其它信息，
      要能正确从文件等获取，并在输出时按照要求来，主管 agent 到底在干什么，似乎什么都没干」。
      核查结论：**ID 确实被丢了**，而且协调 Agent 除了「写推荐理由 + 结尾一段话」之外，
      对报告**没有任何可操作的输出控制手段** —— 说它「什么都没干」并不冤。
    - **ID 全链路修复**（真实缺陷）：此前 `id` 只活在 `molecules.json` 里，
      对接/性质/排序/CSV/报告每一环都丢。现在：
      * `core.docking.carry_identity()` 把 `id / source_file / source_index` 从输入分子带进
        **对接结果行**（进程池与串行路径同一个收口点），性质评估同样带上；
      * 排序行透传报告字段白名单里的字段（`reporting/recommend.py::score_compound`），
        因此 `id` 能一路到推荐排行、结构卡与报告；
      * `ranking.csv` 开头新增 `id` 列、结尾新增 `source_index` / `source_file`；
      * **表格文件的独立 ID 列**（`id,name,smiles` / `编号,SMILES` / `catalog` / `ChEMBL` …）
        现在会被识别成分子 ID（`_ID_HEADER_RE`），名称列让给真正的名称列 ——
        以前 `id` 列会被启发式当成 name，ID 就只剩「和名称一样」这一个信息量。
      * SDF 标题行仍是 `id`（文件里就是这么标的）。
    - **协调 Agent 的新工具 `customize_report(spec_json)`**（第 13 个工具，已登记进
      `config/agent_llm_config.json` 与协调提示词）：在**固定骨架 §1–§9 不变**的前提下，
      让 Agent 声明「这次报告要什么」——`title` / `extra_columns`（白名单 26 个真实字段）/
      `highlights`（它自己的判断）/ `requirements`（用户要求 → 实际处理）/ `notes`。
      工具会核对**覆盖率**并回绝取不到的项（例如「文件里没有 ID 列」），
      Agent 必须如实转达，不许假装生效或编造字段值。
    - **报告随之可变**：自定义标题；第 3.1 节排行表按 `extra_columns` 追加列（数据自带且与名称
      不同的 ID 会**自动**带上）；**新增「0. 本次要求与响应（协调 Agent）」一节**：
      用户要求→实际处理表、Agent 要点、它的补充说明，以及**调度记录表**
      （各角色实际模型 + 调用次数）。「主管 Agent 到底干了什么」从此写在报告第一屏。
    - **真实端到端实测**（真实 LLM，上传 `id,name,smiles` 的 CSV，指令里明确要求「报告带上 ID」）：
      66 s 跑完，工具调用序列为
      `import_molecule_library → list_known_receptors → run_pocket_analysis(set_docking_site)
       → run_property_assessment → run_docking → recommend_compounds → analyze_pose_pocket
       → submit_recommendations → customize_report → generate_screening_report`
      （run `20260921-095530-0405`）；`run.json.report_customization` 记录了标题
      「凝血酶(thrombin)对接筛选报告（含小分子 ID）」、要求「报告中带上小分子 ID → 已启用 id 列，
      按上传文件 pgr_ids.csv 的 ID 列展示（PGR137 阿司匹林、PGR042 布洛芬，覆盖 2/2）」、
      两条基于真实数值的要点，以及未做对照分析的原因；报告里 **ID 列出现且两个 ID 都在**，
      §0 与调度记录表（协调 7 次 / 口袋分析 5 次 / 对接 3 次 / 属性 3 次 / 受理 1 次）齐备。
    - **门禁**：新增 `tests/test_report_customization.py` **11 项**（ID 带进结果行、`dock_batch` 收口、
      CSV 含 ID 列、报告按需加列、工具白名单/覆盖率/如实回绝/长度钳制、§0 内容、
      无定制时骨架保持原样、表格 ID 列解析、ID 与名称不同才自动加列）；
      `check_web` **149/149**（+4 静态守卫：工具/共享字段表/§0/carry_identity/CSV 列）；
      既有 `tests/test_api.py` 两处 CSV 表头断言同步为 `rank,id,name,smiles`。
      全量 `pytest` **586 passed**。
    - 证据截图：`var/tmp/ui_final/report_custom_section0.png`、`report_id_column.png`。

58. **开箱即用 P0：打包修复 + 能力自检 + 标准面定位（ADR-0001，路线 A）**：
    - **背景**：可移植性评估暴露三件事 —— ① `pack.sh` 的排除项写的是 `assets/receptor/cache`
      （只 1.3 MB），tar 实测把 `assets/cache`（786 MB）与 `assets/uploads`（245 MB，**含用户上传**）
      打进"源码包"，体积 **1.4 GB**；② "缺 P2Rank/pdb2pqr/Java 会怎样"只写在 `.env.example`
      注释里，用户要跑到某一步才知道降级；③ 三处 Agent 面（`/api/*`、手写标准子集、
      `langgraph-deploy/`）没有文字规定谁为准。
    - **① 标准面定位（路线 A）**：新增 `docs/adr/0001-agent-surface.md` —— ①+② 合并为
      **产品面**（本机网页/脚本；标准子集自称"兼容子集，非平台官方实现"），③ 为**平台面**
      （Studio/SDK/未来 durable）；写死"**能力只加一处**"表（Agent 编排 → `agents/`+`tools/`
      两面共用；产品形态 → `/api/*`；协议形状 → 一个共享模块；平台特性 → `langgraph-deploy/`），
      并规定产品面标准子集**冻结**、要加协议端点先改 ADR；备选路线 B/C 记录为演进路径。
      `architecture.md §1.3` 加摘要、`docs/api.md` 顶部加"这个面是什么/平台面在哪"。
    - **② 打包修复**：`scripts/pack.sh` 重写 —— 排除项对齐真实目录（`assets/cache`、
      `assets/uploads`、`assets/tools`、`assets/receptor`、`var`、`.env`、`config/local_settings.json`…），
      新增 `--list` 预演（不写文件、列出会包含的条目与体积）、`--with-cache/--with-tools`、
      `--max-size`（默认 80 MB，超限失败）与**包内硬校验**（出现 `assets/uploads|cache` 直接失败）。
      实测：**1.4 GB → 5 MB**，受体注册表（thrombin/trypsin PDBQT）仍在包内，离线可用。
    - **③ 能力自检**：新增 `scripts/doctor_probe.py` + `scripts/doctor.sh`（findings 17 项：
      必需=Python/7 个依赖/目录可写；可选=Java、P2Rank、pdb2pqr、AutoDock4、中文字体、LLM、
      端口），每项缺了都给「影响什么 + 怎么补」；退出码 **0 全能力 / 2 仅降级 / 1 缺必需**，
      支持 `--json`（CI/测试）与 `--online`（探测 LLM 端点）。**复用项目自己的探测函数**
      （`core.pockets.p2rank_command`、`core.receptor_ph.pdb2pqr_bin`、`core.docking._autodock_bin`），
      不另写一套判断。另加 `scripts/fetch_tools.sh`（P2Rank 按需获取，290 MB 工具不进交付包）。
      `start.sh --check` 现在先跑 doctor 再跑功能冒烟（doctor 返回 1 时直接停）。
    - **④ 文档**：README 顶部改为"新机器三条命令"（解包 → `doctor.sh` → `start.sh`）+
      **环境能力矩阵表**（缺 X → 降级成 Y 的完整口径，含离线可用性说明）；项目结构树补
      `docs/adr/`、`scripts/{doctor,pack,fetch_tools}.sh`。
    - **门禁**：新增 `tests/test_ootb.py` **7 项**（排除项不得指向不存在的路径、必须覆盖
      重目录/私密路径、`--list` 体积 <80 MB 且不含用户数据但要含受体、doctor 矩阵与退出码、
      probe 可复用且端口语义正确、ADR 说清两个面与"能力只加一处"、三份文档互相链接）；
      全量 `pytest` **593 passed**、`lint_local` 新增 0、`check_web` **149/149**、
      `ui_e2e` **210/210**、`browser_check` **31/31**、deploy `check.sh` 全部通过、
      `snapshot_ids` 无移除；`bash start.sh --check` 端到端实跑（doctor → 真实 1 分子对接）通过。
    - **留待 P1/P2（未做，需你批准）**：`uv.lock` 固化（当前 `setup.sh` 仍是 `uv pip install -e .`）、
      离线断言、端点/SSE 帧快照、`prune_runs.sh`、`run.json` schema 版本、Docker 构建验证。

## 15. 注意事项

- **`/node_run`**：Agent 图由 `create_agent` 内部构建，节点通常无法直接寻址；请用网页、`-m flow`、
  `-m agent` 或 `/api/pipeline/stream`。
- **会话持久化**：默认内存 checkpointer，重启后多轮会话丢失；需要持久化请设 `CHECKPOINT_BACKEND=sqlite`。
- **并发**：子 Agent 在工具内同步调用，单进程串行；多用户高并发建议多进程部署（每进程独立 checkpointer）。
- **长任务**：大分子库会显著耗时，可用 `--max-ligands` 或界面上的「最大分子数」先做小样本验证。
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
