# 修复记录（CHANGELOG）

本文件是原 `projects/README.md` §14「修复记录」的归档 —— 2026-09 文档收敛时迁出：
该节占 README 全文 **52%**（1140 行 / 62 条），且含对**已删除代码**的现在时引用，
混在上手文档里既难检索也容易误导。

**这是历史日志**：条目按当时的时间顺序编号、保留当时的结论（含「当时 631 passed」这类
历史门禁数字），不再随代码更新；**当前**行为与门禁口径以 `README.md`、
`docs/architecture.md`、`docs/api.md` 为准。

新增修复记录请追加到本文件末尾（新条目写明日期与验证方式），不要写回 README。

---

## 版本 0.8.0（2026-09-23）

发布版本号从 `0.7.0` 升到 `0.8.0`（`projects/pyproject.toml`、`src/docking_agent/__init__.py`、
`CITATION.cff`、`docs/api.md`、`docs/技术报告.md` 同步；`/api/health` 与设置页显示的版本随之变化）。

本版的主要内容：

- **外部 GPU 引擎接入执行**：登记 `EXTERNAL_DOCKING_BIN` 后，`engine=external` 会把对接真的交给
  外部二进制。已接入执行适配的是 **AutoDock-GPU**（`autogrid4` 按会话生成格点图并缓存、解析 DLG）
  与 **CPU 版 Vina CLI**（`--out` + `--score_only`）；`unidock` / `vina-gpu` 仍只登记不执行（不猜参数）。
  结果行与内置引擎同形，并记录 `engine`（如 `autodock-gpu`）与 `engine_version`。
  本机实测（RTX 5090，AutoDock-GPU v1.6 CUDA 构建）：凝血酶 1DWC × 苯甲脒 → −6.15 kcal/mol，
  乙醇 → −2.80 kcal/mol，单配体 < 1 s。
- **默认引擎可配且真的生效**：设置页新增「对接引擎（默认）」（`auto`/`vina`/`autodock`/`external`，
  出厂 `auto` = 优先 Vina、不可用回退 AutoDock4 CPU）；工具参数 `engine` 默认改为**留空**，
  解析优先级为「工具参数 > 运行请求（表单）> 设置页默认 > auto」—— 否则用户在设置页做的选择会被
  工具层硬编码默认值静默盖掉。报告「1.5 参数自动规划」里的引擎同步显示本次真正要用的引擎。
- **外部工具登记项补全**：新增 `AUTODOCK4_BIN` / `AUTOGRID4_BIN`（conda 独立前缀不在 PATH 上时必需），
  `.env.example`、设置页与 `doctor.sh` 一并同步。
- **健康面板如实反映本机引擎**：`/api/health` 的 `engine_available` 增加 `external` 与
  `external_flavor`；AutoDock4 的可用性判定改走项目自己的定位逻辑（不再用 `shutil.which` 误报）。
- **修复（均为实测踩到的真实故障）**：
  1. `GPU_DEVICE=0` 下发 `--devnum 0` 被 AutoDock-GPU 拒绝（它要求 1 基）→ 登记了 GPU 却 0.15 s 失败；
  2. 配体带极性氢（meeko 的 `HD` 类型）而格点图没有 `HD` → 引擎报「任务未成功」；
  3. `--nrun N` 时 DLG 里有多组结果，旧解析取第一组而非最优组 → 改为取最低结合能并与
     `CLUSTERING HISTOGRAM` 交叉校验；
  4. 外部引擎登记的播报绕过了 `_note()` 的异常保护，`note_cb` 抛错会打断对接本身。

> 说明：文档里出现的 `(v0.28)`、`(v0.33)` 这类括号是**特性迭代号**（历史沿用的写作习惯），
> 发行版本以本节的 `0.8.0` 为准。

---

## 版本 0.7.0（2026-09-23）

发布版本号从 `0.6.0` 升到 `0.7.0`（`projects/pyproject.toml`、`src/docking_agent/__init__.py`、
`CITATION.cff`、`docs/api.md`、`docs/技术报告.md` 同步；`/api/health` 与设置页显示的版本随之变化）。

本版包含的主要能力与修复（细节见下方按日期分节的条目）：

- **两套界面**：新增简易模式并设为默认首页（`/`，别名 `/simple`），原工作台迁到 `/advanced`；
  对话呈现重做 —— Markdown 渲染（`web/markdown.js` 单一实现）、思考折叠、候选点选时机、
  刷新后结果区置空。
- **候选选择生命周期**：同一问题只下发一次、按 `kind` 分组不互相覆盖、等本轮输出结束才挂出、
  运行中点不动；停下等选择记为 `needs_user_input`（`[ ASK ]`），与 `no_op`（`[ SKIP ]`）区分。
- **执行自愈**：工具调用序列自愈（悬空 `tool_calls` / 孤儿 `ToolMessage` 在每次模型调用前修好）、
  步数预算自动放宽（120 → 240 → 480）并从检查点续跑、到顶由协调 Agent 用现有结果收尾。
- **计算正确性**：分子身份键统一（规范 SMILES），修「一个分子对接出两行」。
- **报告与文档**：交付文案精简、受体溯源、golden 版式回归；README / 技术文档 / API 契约同步更新，
  架构图由脚本重绘。
- **工程保障**：接入 GitHub Actions（静态门禁 + 离线用例 + 部署自检）；部署自检在无密钥环境
  （干净检出 / CI）按 `[SKIP]` 分级，不再假失败。

> 说明：文档里出现的 `(v0.28)`、`(v0.33)` 这类括号是**特性迭代号**（历史沿用的写作习惯）；
> 本节记录 0.7.0 发布当时的状态，当前发行版本见上一节。

---

# 2026-09-23 · 坐标入参接受文档承诺的字符串形态；对接验证脚本区分「跳过」与「失败」

起因：按用户要求核对环境里的 AutoDock Vina（CLI + 项目绑定）时，跑了仓库自带的
`scripts/verify_docking.py`，暴露出两个真实缺陷。

**① `site_center` / `site_size` 的文档承诺与真实校验不一致（真缺陷）**
字段描述与工具 docstring 都写着「也接受 `"22,22,22"`」，`floats_to_text()` 也早就支持字符串，
但字段类型是 `List[float]` —— 字符串在 **pydantic 校验阶段**就被拒，函数体永远拿不到。
表现：仓库自带脚本按文档传 `site_center="31.5,13.74,24.36"` 直接抛 `ValidationError`；
模型照描述传字符串同样会被拒（提示词里承诺的写法不可用）。
修法：`CoordArray` 改为 `Annotated[Optional[List[float]], BeforeValidator(normalize_coord)]`，
校验前把逗号/空格/分号分隔的字符串折成数组；空串按「未提供」处理；无法解析的字符串原样交给
pydantic 报错。回归：`tests/test_coord_schema.py`（5 项：三种分隔符 / 数组与 None 透传 /
垃圾输入仍报错 / 工具 schema 两种形态等价）。

**② `verify_docking.py` 把「前置条件不满足」当成验证失败（假失败）**
`--quick` 分子集里没有超出主盒的分子 → `large` 组不变量无从验证，脚本却记 FAIL；
`[E]` 环节拿**旧归档**（C 方案之前、无 box_group 与版本戳）做「同代码逐位复现」，差异 0.02–0.33
kcal/mol 也记 FAIL。两者都会让验证结论看起来比实际差。
修法：新增 `skip()` 与 `[SKIP]` 分级（与 FAIL 分离，汇总里单列）：
- `large` 组：本次分子集无超限分子 → SKIP（并说明该不变量由 [D] 反证控制或含大配体库覆盖）；
- 旧归档重放：完全一致 → PASS；差异 ≤ 0.5 kcal/mol → SKIP 并报出最大差异与排序是否变化；
  超过该量级 → FAIL（可能是真回归）。同代码的逐位一致由 [B] 独立复算与 [C] 交叉验证覆盖。

顺带拆分：`scripts/verify_docking.py` 已顶到 700 行上限（`lint_local.py` 的文件规模门禁），
把「独立实现的对接探针」（`make_ligand_pdbqt` / `vina_dock` / `vina_rescore` 与它们硬编码的常量）
拆到 `scripts/vina_probe.py` —— 这一组本来就是「用另一条实现路径重算」的地基，拆开后两个文件都远低于上限。

验证：`scripts/verify_docking.py --quick` → **31/33 通过（2 项跳过）✓**；
`doctor_probe.py` 报告 AutoDock Vina Python 绑定 1.2.7 可用；`check.sh --static` 全绿；
`check.sh --fast` → 568 passed / 248 skipped。

---

# 2026-09-23 · 登记本机 AutoDock Vina CLI（不改默认执行）

用户要求把环境里新装的命令行 Vina 配置进项目，并选择「**只登记路径、不改变对接由谁执行**」。

- **识别**：外部引擎注册表此前只认 Uni-Dock / Vina-GPU / AutoDock-GPU 三类 GPU 工具 ——
  本机装了官方 CPU CLI（`vina 1.2.7`）也会被判「无法识别」而**拒绝启动**。现在新增
  `vina-cpu` 类型（`--version` 出现 "AutoDock Vina" 即命中，不会抢走 vina-gpu 的识别），
  并补齐它的 argv 形状；`GPU_FLAVORS` 之外的引擎不再校验 GPU 可见性（CPU 引擎不该因“没有显卡”不可用）。
- **登记**：新增设置项 `external.vina_bin`（环境变量 `VINA_BIN`）与 `vina_cli_bin()`：
  `VINA_BIN` → PATH 自动查找。它**只进探测报告** —— `configured_bin()` 仍为空、
  `require_engine()` 仍返回 `{}`，因此对接照旧由内置绑定（同版本 1.2.7）执行，
  每分子盒子 / 分批 / 取消等能力不变。
- **可见**：`collect()` 在 `not_configured` 时附带 `detected`（路径 / 类型 / 版本），
  `doctor.sh`、`/api/tools/probe` 与设置页「检测」三处同源显示；
  本机 `.env` 已写入 `VINA_BIN=/home/biolab/Autodock_Vina/vina-env/.pixi/envs/default/bin/vina`。
- 回归：`tests/test_external_engine.py` +3 项（CPU CLI 识别与 argv / 登记不改执行路径 /
  `VINA_BIN` 无效时回退 PATH）。

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
      置空，渲染给编排层的话里只剩文件名。修法：受理层带上附件字段；`agents/dispatch.py` 新增
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
      `agents/dispatch.py` / `tools/docking.py` 的工具签名与说明、`cli.py`、
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
    - **数据总线 `runtime/tool_io.py` 支持显式 `run=`**（`record` / `artifact_path` / `load` /
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
    - 门禁：`pytest` **545 项全绿**、`lint_local` 新增 0（`agents/dispatch.py` 因显式透传超 700 行，
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
      `store=shared_store()`（`runtime/blackboard.py` 的进程内 `InMemoryStore` 单例）——
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

59. **外部对接引擎（用户自行安装）：登记 / 探测 / 拒绝启动（P0）**：
    - **背景**：GPU 版对接工具（Uni-Dock / Vina-GPU / AutoDock-GPU）编译与驱动依赖复杂，
      随项目分发不现实。约定：**二进制由用户在设置页提供**，本项目**不新增引擎身份** ——
      外部工具是 `vina` 引擎的另一种执行器（同一打分函数族、不同运行后端）。
    - **唯一探测实现** `src/docking_agent/core/external_tools.py`：路径存在 / 可执行 /
      `--version|--help` 能跑通 / GPU 可见（`nvidia-smi` → `clinfo`）/ **引擎类型识别**
      （按版本输出特征串区分 Uni-Dock、Vina-GPU、AutoDock-GPU 三类 CLI）；并提供
      `build_argv()` 固定三类调用形状（避免执行期再猜参数）。设置页「检测」按钮、
      `POST /api/tools/probe`、`scripts/doctor.sh` **共用这一份**，三处结论必然一致。
    - **三条边界行为（不静默）**：未提供路径 → 内置 CPU Vina（行为与之前完全一致）；
      提供但探测不通过 → `require_engine()` 抛错，`dock_library` 在进入任何计算前
      **拒绝启动**并在 `hint` 里给出补齐方法；提供且通过 → 记录类型/版本/设备/单批大小，
      并以运行笔记如实播报"执行适配启用前本次仍由内置引擎完成计算"（P1 去掉这句话）。
    - **设置页**：新增「外部工具（自行安装）」分组 —— `external.docking_bin`（`EXTERNAL_DOCKING_BIN`）、
      `external.gpu_device`（`GPU_DEVICE`）、`external.gpu_batch_size`（`GPU_BATCH_SIZE`），
      并把原先只能改 `.env` 的 `P2RANK_HOME`、`PDB2PQR_BIN` 一并搬进界面；字段经
      `apply_runtime_env()` 注入环境变量，`core/` 侧读取逻辑不变。分组内提供「检测外部工具」按钮，
      逐项显示就绪状态与补齐方法。
    - **兼容性增强**：三类引擎的 argv 形状集中在一个适配器里（`FLAVOR_ARGV_STYLE` + `build_argv`），
      输出解析按类型分支（P1 实现），**不新增引擎枚举值**。
    - **门禁**：新增 `tests/test_external_engine.py` **15 项**（未配置即用内置、路径缺失/目录/无执行权限、
      三类引擎识别、未知类型拒绝、`require_engine` 拒绝启动、对接入口在计算前即拒绝、
      三类 argv 形状、设备与批量来自环境变量、设置页字段与环境变量映射、探测接口三块结果、
      doctor 能力行）；`tests/test_settings.py` 分组清单同步为 7 组。全量 `pytest`、`lint_local`、
      `check_web` 149/149、`ui_e2e` 210/210、`browser_check` 全部通过。
    - **待办（P1，需你确认后开工）**：外部引擎的真实执行与输出解析（三类引擎各自的结果文件）、
      GPU 批量调度（`plan_gpu_batch`：单设备单进程 + 分批）、对照基准
      （CPU vs GPU 分数差 / 排序相关性 / top-1 位姿 RMSD）与报告引擎标注回归。

60. **移除"不经过 Agent 的确定性流水线"（对接统一由 Agent 驱动）**：
    - **决定**：对接只保留一条执行路径 —— 协调 Agent 分发工具执行。删除理由是该模式是**第二条对接
      代码路径**（与 `tools/` 重复实现定盒/对接/排序/落盘），与 ADR-0001「能力只加一处」冲突，
      且两条路径都要验、都要修。代价是**对接必须有 LLM**（本记录生效起，README 不再声明"不配 LLM
      也能跑通对接"）。历史记录 50–59 里提到的 pipeline 均为当时事实，保留不改。
    - **删除范围**：`src/docking_agent/pipeline.py`（812 行，含 `run_pipeline`）；
      `POST /api/pipeline/stream` 与 `POST /pipeline`；`api/schemas.py::PipelineRequest`；
      CLI `-m pipeline`；标准 Agent Protocol 面的 `pipeline` 助手与 `/assistants/{id}/schemas` 分支；
      前端参数模式的「运行方式」选择器（参数模式恒为多 Agent，表单仍是权威参数）；
      部署层 `langgraph-deploy` 的 `pipeline` 图（**7 张图 → 6 张**）。
    - **保留与迁出**：`default_library_path`、`positive_control_info`、`POSITIVE_CONTROL_LABEL`、
      `DEFAULT_POSITIVE_CONTROL`、`load_positive_control`、`resolve_molecules` 迁到新模块
      `src/docking_agent/core/library.py`（`agents/dispatch.py` 与 `api/app.py` 仍在使用）。
    - **自检替代**：`bash start.sh --check` 改为跑 `scripts/smoke_test.py --fast`
      （内置假 LLM 驱动真实多 Agent 编排：协调 Agent → 工具 → 子 Agent → 真实计算），
      仍然是**不消耗真实额度**的离线冒烟；`smoke_test.py` 里的流水线用例已删除。
    - **文档同步**：根 README（能力矩阵 LLM 行改为必需、删除"不配 LLM 也能用"与流水线小节、
      Studio 图数 7→6）、`projects/README.md`（能力矩阵、CLI 参数表、结构树、状态映射）、
      `docs/architecture.md`（模块表、依赖方向、模式对照表）、`docs/api.md`（端点表、
      废弃端点说明、前端切换表）。
    - **测试改造**：新增 `tests/support/fake_llm.py`（从 `scripts/smoke_test.py` 抽出的假 LLM 脚手架：
      脚本化协调 Agent → 工具 → 子 Agent，真实计算、不联网、不消耗额度，含
      `run_agent()` / `agent_input_from_form()` / `business_run_id_from_sse()`）；
      **17 个测试模块**改为走标准 Agent Protocol 路径或 Agent 侧等价实现，端到端断言强度不变
      （上传受体是实际使用的那一个、报告固定章节与内嵌图、PDF 与规范下载名、受体入包、
      漏斗合并取精度更高的一条、产物闭环与分页导出等）。
    - **删除流水线后暴露并已修复的两个真实缺口**：
      ① `normalize_ligand_text` 把「名称:SMILES,名称:SMILES」因逗号判成 CSV → 返回 0 条
      （删掉流水线后 Agent 路径成了唯一入口，用户这样写就什么都跑不了）→ 现在**单行**文本
      解析不到时回退行内 SMILES 解析；多行仍按 CSV/SDF 处理（多行回退会改写跳过原因、
      掩盖 InChIKey 在线反查失败这类真实原因，这一点有回归守着）；
      ② 表单里的「保存对接位姿」「最大分子数」在 Agent 路径被忽略（恒存位姿、不套上限）→
      `molecular_docking` 新增 `save_poses` / `max_ligands` 入参并透传 `dock_library`，
      协调 Agent 提示词同步要求把任务规约里的这两项原样传下去。
      两者都有回归（`tests/test_report_skip.py` 新增 2 项）。
    - **行为变化（如实记录）**：`result.notes` 不再包含「未提供阳性对照」—— Agent 路径把它写在
      `task_spec.assumptions` 与报告正文里（不静默原则不变，但依赖该 note 的下游需改读这两处）。
    - **待办**：部署层 `test_live_server.py` 中三个依赖流水线图的用例（真实对接产物 / 并发隔离 /
      无分子诚实返回）已删除 —— 这三个行为现在需要真实 LLM 才能覆盖，若要在 CI 里保住，
      应补一个"用假 LLM 驱动 coordinator 的生产并发与产物"的部署层用例（P1 待办）。

61. **三栏布局 + 历史任务检索 + 结构化输出不再每次撞 400（v0.31）**：
    - **三栏布局**（用户要求：参数放左、阶段日志与实时分子结果放右）：把工作台从 v0.28 的单列
      改回并升级为三栏 grid —— 左=参数设置（`#config-panel`）、中=对话与结果（新的
      `#workbench-center`，含原先的对话面板与「结果与产物」面板）、右=运行详情
      （`section.main-col`：编排时间轴 / 阶段日志 / 工具轨迹 / 实时逐分子结果）。
      左右栏各有「收起/展开」按钮（`body.cols-params-collapsed` / `cols-live-collapsed`，
      状态存 localStorage）；≤1180px 右栏下沉、≤900px 单栏，实测无横向滚动。
      DOM 只做位置重排，**所有既有 id 保持不变**。
    - **历史任务检索**（用户要求：关掉页面后也能查之前的运行）：`RunStore.search()` 在
      `run.json` + 排序表前 64 KB 上建**进程内索引**（含 run_id / 受体 / 状态 / 类型 /
      任务描述 / 分子名与 ID；实测 3.4k 条首次建索引约 0.4 s，之后毫秒级），支持
      `q`（空格分词为 AND）、`status`、`kind`、`receptor`、`since`/`until`（只给日期含当天）
      与 `offset`/`limit` 分页；`GET /api/runs` 带任一检索参数时返回
      `{runs,total,offset,limit,query}`，**不带参数时保持旧结构**（老调用方不受影响）。
      界面：历史页签新增关键词/状态/类型/受体/时间范围 + 查询/重置 + 上一页/下一页/命中计数，
      每行「载入」复用现有 `loadRun()`；上次检索条件存 localStorage（键 `docking.history.query`）。
    - **结构化输出降级问题**（用户反馈的运行日志：pocket/property/docking 每轮都报
      「供应商拒绝结构化输出（thinking 模式不支持强制 tool_choice），已降级为文本 JSON 契约」）：
      实测根因是该端点在 thinking 语义下**只拒绝"强制" tool_choice**（`ToolStrategy` 正靠它工作），
      自动 tool_choice 正常、**JSON 模式正常**（`json_schema` 不可用）。因此不再"每次先撞一次 400"：
      新增 `agents/capabilities.py` 把「该模型不支持强制 tool_choice」按 `base_url+model+thinking`
      记到 `var/state/llm_capabilities.json`（30 天后过期重探），构建子 Agent 时直接跳过
      `ToolStrategy`，降级优先用 **JSON 模式**（`response_format={"type":"json_object"}`，
      供应商保证返回合法 JSON，仍经必需字段校验），拿不到才退回纯文本契约。
      运行日志相应改为「本模型不支持强制结构化输出（已记录该能力，后续运行不再重试）」。
      设置页每个角色新增 `structured_output`（auto/on/off），`on` 可无视记忆强制试一次。
    - **门禁**：新增 `tests/test_llm_capabilities.py`（7 项：未知先试 / 被拒后记住并转 JSON 模式 /
      记忆过期重探 / 环境变量与按角色设置优先级 / 构建期真的不再挂 ToolStrategy / 有支持时仍挂）
      与 `tests/test_run_search.py`（10 项：关键词、过滤器、分页、结构、可载入、HTTP 兼容）；
      `check_web` 的三栏与历史检索检查按新设计改写（158 项）、`browser_check` 把「单列居中」
      断言换成「三栏并排 + 无横向滚动 + 右栏内容」（28 项）、`ui_e2e` 设置分组期望改 7 组（211 项）。

62. **结构化选项（choices）在参数模式下不可见 + 被中断的运行不收尾（v0.32）**：
    - **缺陷 1（用户报障）**：运行日志出现「已生成 4 个可选项（kind=molecule）：等待用户在界面上选择」，
      但网页上没有任何按钮。原因有两处：`handleChoicesEvent` 里 `if (state.page !== 'chat') return;`
      把参数模式的候选直接丢弃；`loadRun()` 也不回填 `run.json` 里的 `choices`，因此刷新页面或换设备后
      候选彻底丢失（后端一直是对的：`choices` 与 `choices_note` 已落盘并由 SSE 下发）。
      修复：中栏新增「需要你确认的选项」面板（`#choice-box` / `#choice-list`），**与页面模式无关**地渲染；
      载入历史运行时从 `run.json` 回填；候选项的结构化 `detail`（cid/mode/formula/note）经
      `choiceDetailText()` 格式化成可读文本（此前会显示成 `[object Object]`）。
    - **缺陷 2（顺带发现）**：点选后的追问原先通过回填输入框再 `startRun()` 传递，遇到面板/表单重渲染
      就会写丢（用户表现为"点了没反应"）。现在 `startRun(messageOverride)` / `buildPayload(messageOverride)`
      支持把追问作为**显式参数**下发，同时仍写进当前输入框供用户编辑。
    - **缺陷 3（进程被杀留下的僵尸状态）**：运行只存在于本进程内，进程重启后不可能还有运行在执行，
      但残留的 `running` 会让历史列表永久显示「运行中」且 `finished_at` 为空（实测一次重启后收尾了
      **39 个**这样的运行）。新增 `RunStore.reconcile_interrupted()`，在应用启动钩子里执行：
      标记为 `interrupted`、补 `finished_at`/`duration_sec`、追加一条日志说明，并**保留 choices**
      （用户仍可点选，点选会以同一会话发起新运行）；前端把 `interrupted` 归入取消类标签并显示
      「已中断（进程重启）」。
    - **门禁**：`tests/test_run_search.py` 新增 2 项（收尾幂等 + 启动钩子真的调用），
      `scripts/ui_e2e.js` 新增参数模式场景（面板可见、详情可读、载入历史回填；点选续跑链路由既有
      对话模式场景覆盖）；全量 `pytest` 627 passed、`check_web` 158/158、`ui_e2e` 217/217。

63. **共晶配体可作为阳性对照：未指定对照时询问用户（v0.33）**：
    - **动机**：受体结构自带的共晶配体是**天然的方法学基线**——它本来就结合在这个位点上，
      拿它做阳性对照比随便指定一个分子更有说服力。此前系统只会把它用于定位位点，
      用户若不主动提供 `positive_control`，报告里就写「本次未提供阳性对照」。
    - **实现**：`core/pockets.cocrystal_ligand_smiles()` 按 `chain:resid:resname` 取出该配体的
      HETATM 与相关 CONECT 记录，拼成最小 PDB 块交给 RDKit 解析为 SMILES（实测凝血酶的
      共晶配体 MIT 可正常解出）；`tools/choices.offer_cocrystal_positive_control()` 在
      **未指定阳性对照**且受体带共晶配体时，通过既有 choices 通道下发两个选项：
      「把共晶配体 X 作为阳性对照，做结合模式对比」与「不使用阳性对照，只做候选筛选」，
      并给出 resname/残基号/原子数/SMILES 作为选择依据。对接结果块新增 `receptor_pdb`
      字段供上层解析（仅内部字段，摘要视图不变）。
    - **不阻塞**：阳性对照只是基线，缺它不影响候选分子结果，因此这里只"询问"——
      本次筛选照常出结果，用户的点选会以同一会话发起新一轮并带上对照。这一点写在选项的
      note 与运行日志里，避免用户以为必须选择才能继续。
    - **不猜测**：解不出 SMILES（结构缺键级、缺原子、文件不可读）时**不询问**，
      而是在运行日志里说明原因 —— 绝不拿不确定的结构当阳性对照。
    - **门禁**：`tests/test_run_search.py` 新增 4 项（从结构解出配体 SMILES / 未指定对照时发布
      两个选项 / 已指定对照不再询问 / 无配体或解不出 SMILES 时不询问）；全量 `pytest` 631 passed、
      `lint_local` 新增 0、`check_web` 158/158。

---

# 2026-09-22 · 审计残余收尾（第 3 波续）

> 本轮为**维护性收尾**：清死代码与进程内泄漏、补运行目录保留策略、把打包口径写成明确声明、
> 补齐贡献/安全文档、让技术报告的配图可从仓库复现。全部改动都有回归用例，门禁数字见文末。

## 1. 死代码与进程内泄漏（审计 §2.1）

- **删除 `core/docking.py::save_poses_for`**：零调用者，却被 `core/__init__` 导出、被
  `docs/architecture.md` 文档化（"死 API"比没有 API 更误导）。同步删除导出与文档条目。
- **修 `runtime/blackboard.py` 的 store 视图缓存泄漏**：`_store_boards` 以
  `(id(store), run_id)` 为键缓存黑板视图，`reset_store_blackboards()` 却从无调用者 ——
  长驻服务里每个 run 都会留下一条（视图持有分子/性质/对接等运行级状态）。
  现在：
  - 新增 `forget_store_blackboard(run_id)`，在**每条运行收尾路径**调用
    （`api/routers/agent.py` 的 SSE finally、`api/routers/legacy.py` 的 `/run`、
    `api/agent_service.py` 的 intake 与子 Agent 两条路径）；
  - 另加 `_STORE_BOARDS_MAX=32` 的 FIFO 上限护栏，任何漏掉的路径也不会无限增长。
  - 回归：`tests/test_collaboration.py`（复用/幂等/上限）+ `tests/test_agent_service.py`
    （走**离线 intake** 的标准面运行，结束后不留视图）。

## 2. 运行目录保留策略（审计：2.2 GB / 4900+ 目录，启动要遍历全部）

- **`RunStore.prune(keep_last=200, max_age_days=30, dry_run=True)`**：删除条件为
  「不属于最近 N 个」**且**「超过 M 天」两条同时满足（保守）；`status == "running"` 永不删除；
  返回候选/已删/释放字节数/错误；真删后显式失效检索索引。
- **`scripts/prune_runs.py`**：默认 dry-run（必须 `--apply`），`--threads` 顺带清理
  `var/threads/*.json`，`--json` 供定时任务；阈值可用
  `RUNS_KEEP_LAST` / `RUNS_MAX_AGE_DAYS` 覆盖。
- **启动清理为 opt-in**：`RUNS_AUTO_PRUNE=1` 时 lifespan 才按阈值清理；三个变量已登记 `.env.example`。
- 回归：`tests/test_run_retention.py`（dry-run 不动盘 / 两阈值同时满足才删 / 数量阈值独立生效 /
  running 永不删 / 清理后检索不再列出）。

## 3. 打包口径：明确 `-e .`-only（审计 3.8 方案 B）

- **声明**：`web/`、`config/`、`assets/`、`var/` 都是运行期读写的目录，**不随 wheel 分发**；
  只支持源码检出 + `pip install -e .`。写进 `README` §1、`pyproject.toml` 的 wheel 段注释、
  `start.sh` 的 `PYTHONPATH` 注释（该 export 降级为"旧 venv 兜底"）。
- **执行**：非 editable 安装会被服务入口 `cli -m http` 的
  `paths.assert_runtime_layout(strict=True)` 当场拒绝（不是等前端 404）。
- 回归：`tests/test_packaging.py::test_runtime_layout_is_intentionally_outside_the_wheel`
  把"声明"与"执行"绑在一起（禁用 force-include + 入口严格自检 + README 写明 editable）。
- **README 不再文档化 `PYTHONPATH=src`**：`pytest` 已配 `pythonpath=["src"]`，脚本自带
  `sys.path.insert`，该前缀纯属历史残留。

## 4. 文档补齐

- 新增 [`CONTRIBUTING.md`](../../CONTRIBUTING.md)：环境、**改完必须跑的门禁**、硬性约定
  （引擎测试名单 / 测试返回类型 / 环境变量登记 / golden 重出 / 文档产物重生成 / 分层依赖 / 安全取向）、
  提交与 CHANGELOG 约定。
- 新增 [`SECURITY.md`](../../SECURITY.md)：安全模型（无鉴权 + 仅本机）、已实现的浏览器侧防护表、
  已知限制、漏洞报告方式（GitHub Security Advisories）、自建部署加固清单。
- 新增 [`docs/adr/README.md`](adr/README.md)：ADR 索引 + 何时该写 + 模板 + 与其它文档的分工；
  `README.md`（根）与 `architecture.md` §14 文档地图同步补链。
- `architecture.md` §15 的差距表补状态：4.1 取消链路 ✅、4.4 失败清单进报告 ✅、
  4.2 部分修复（`reconcile_interrupted` + `prune`；reaper/resume 仍缺）；lint 计数 251 → 224。

## 5. 技术报告配图可复现（审计 3.5）

- 原先 13/13 张配图都指向被 gitignore 的 `var/` 运行产物：干净克隆上重跑生成器只会得到
  13 个红色「［缺图］」占位符，而提交的 DOCX 里却有真图。
- 现在配图**入库**在 `docs/report-media/`（13 张，约 1.8 MB），`FIGURE_MAP` 改为指向它们，
  并删掉写死的 `LATEST_RUN` 运行目录。
- 生成器新增 `missing_figure_files()`：缺图时**默认拒绝出文档**（退出码 2，
  `--allow-missing-figures` 才能强行继续），不再静默产出"假成品"。
- 回归：`tests/test_docs_consistency.py`（配图入库 + 无 f-string 路径 + 生成器缺图拒绝）；
  `docs/技术报告.{docx,pdf}` 已按新路径重出（13 图 / 103 页）。

## 门禁实测（2026-09-22）

```
pytest -q（全量，含真实引擎）          711 passed
bash scripts/check.sh --fast           464 passed, 247 skipped（~20 s）
bash scripts/check.sh --static         lint 通过 + check_web 179/179 + ruff 通过 + mypy 通过（21 模块）
node scripts/ui_e2e.js <url>           231/231
python scripts/browser_check.py <url>  34/34
langgraph-deploy check.sh / test.sh    全部通过 / 46 passed, 4 skipped
```

---

# 2026-09-22 · 修复「同一轮被问两次小分子」与迟到的阳性对照询问

> 现场：run `20260922-140827-6679`（提问轮）+ `20260922-140903-6542`（回答轮）。
> 用户反馈「为什么会让我确定两次小分子」。

## 1. `decision=run` 时不再把「缺少 / 待向用户确认」下发给编排层（真缺陷）

- **现象**：用户只发了一句「把代森锰锌与植物去甲基化酶 ROS1 做分子对接」。
  受理层规则判定 `decision=run`（名称会先在线查询，不需要用户补库），
  但受理**模型**把「用户点名了分子但没给 SMILES」记进 `missing=["ligands"]` 并附了一条
  「请提供候选分子库：① 输入名称或 SMILES…」的问题；`render_agent_message()`
  **无条件**把 `missing`/`questions` 渲染成「缺少：…」「待向用户确认：…」发给编排层，
  主管 Agent 于是当场把它转述成一次提问。同一轮里系统**又**发布了「代森锰锌是多组分结构、
  请选择代表结构」的 4 个选项 —— 用户因此被问了两遍同一件事（问库 + 选结构）。
- **修复**：`intake.render_agent_message()` 只在 `decision=ask`（受理层确实要停下来问用户）
  时才下发「缺少 / 待向用户确认」。`missing`/`questions` 仍原样保留在 `run.json`（可观测、可排查），
  只是不再进入给编排层的指令 —— 与「`decision=run` → 不得在中途停下来询问用户」的既有契约一致。
- **回归**：`test_intake.py::test_llm_missing_alone_does_not_block_the_run` 增加渲染断言
  （run 时不得出现「缺少：/待向用户确认」）；`test_llm_explicit_needs_user_input_becomes_ask`
  增加正向断言（ask 时必须下发，否则用户看不到问题）。

## 2. 共晶配体（阳性对照）询问：一次运行只判定一次，且必须在对接前

- **现象**（同一现场）：run `20260922-140903-6542` 的日志先写两次
  「检测到共晶配体 5CM，但在 assets/cache/7YHP.pdb 中都解不出 SMILES，**因此未询问**」，
  第三次调用却成功解出并发布 2 个 `positive_control` 选项 —— 此时对接已跑完、报告即将生成。
  原因：`run_docking` 在一次运行里被调用了多次（重试/分阶段），每次都会重新判定并询问；
  受体结构在前两次还没准备好，第三次才可读。
- **修复**：`tools/choices.offer_cocrystal_positive_control()` 用
  `run.data["cocrystal_check_done"]` 把判定**锁定为一次**（进入即置位），无论结果如何都不再复问、
  也不再重复写「解不出」日志；`specified_control` 非空时依旧直接跳过。
- **回归**：`test_run_search.py` 新增
  `test_cocrystal_offer_is_asked_at_most_once_per_run`、
  `test_no_late_cocrystal_ask_once_docking_started`、
  `test_cocrystal_smiles_failure_is_logged_once_per_run`。

## 门禁实测（2026-09-22，本轮）

```
pytest -q（全量，含真实引擎）          714 passed
bash scripts/check.sh --fast           467 passed, 247 skipped
bash scripts/check.sh --static         lint 通过 + check_web 179/179 + ruff 通过 + mypy 通过（21 模块）
```

---

# 2026-09-22 · 修受体质子化静默失败 + 接入专业 pKa 引擎

> 现场：run `20260922-140903-6542` 的报告写「受体质子化（7YHP）：未按目标 pH 重新准备…」，
> 错误里是 meeko 的 `int('128.990')`。

## 1. 真因：pdb2pqr 固定列下「4 位残基号把链号挤没空格」

- pdb2pqr 按 PDB **固定列**写 PQR。7YHP 的残基号到 1xxx，链号与残基号之间没有空格：

      ATOM   2841  N   GLU A1005     128.990 117.515 122.728 -0.5163 1.8240

  白空格切分只有 10 个 token，meeko 把 `A1005` 当链号、把 x 坐标当残基号 →
  `ValueError: invalid literal for int() with base 10: '128.990'`。
  原有的 `normalize_pqr_for_meeko()` 只处理了同类的**插入码**粘连（`36A`，11 token），
  4 位残基号这一半没覆盖 → 整条 pH 准备在 `opt → noopt → delete → noopt+delete` 四档全军覆没，
  最后**静默回退**标准准备流程，报告只写一句「未按目标 pH 重新准备」。
- **修复**（`core/receptor_ph.py`）：新增 `parse_pqr_atom_line()` —— 按「最后 5 个 token 一定是
  `x y z charge radius`」反推残基键，链号/残基号/插入码三者任意粘连都能拆开，不依赖列宽；
  已经是规范形状的行**逐字保留**（不做无谓数值重排）。`his_states_from_pqr()` 也改用同一解析
  （此前 4 位残基号会把链名读成 `A1005`、HID/HIE/HIP 统计全错）。
- **回归**：`tests/test_receptor_ph_parsing.py` 7 项（插入码 / 4 位残基号 / 链+4位+插入码 /
  规范行不动 / 非原子行保留 / HIS 分组 / **真实 7YHP PQR 全量校验**）。该文件不在 engine 组，CI 也跑。
- **端到端复验**：`prepare_pdbqt_at_ph(7YHP_prot.pdb, ph=7.4)` 现在**第一档 `opt` 就成功**、
  丢弃残基 0 个、产出 298 KB / 3732 行 PDBQT（此前四档全失败）。

## 2. 配体接入专业 pKa 引擎：Dimorphite-DL（新增 `core/ligand_pka.py`）

- 受体侧本就是专业工具（pdb2pqr + PROPKA），配体侧原先只有**内置官能团 pKa 规则表**（近似）。
  现在 `ph` 策略**默认优先专业引擎**：`LIGAND_PKA_ENGINE=auto|dimorphite|rules`
  （`auto`＝装了就用；`rules`＝复现旧口径）。
- 窗口内多微观态（Dimorphite 不给布居数）时按**可复现规则**取一个形式对接：
  「\|净电荷\| 最小 → 带电原子最少 → 字典序」，**全部候选与规则**写进
  `docking.json` 的 `ligand_facts.protonation.variants` / `variant_rule`；
  报告 §1.3 显示「质子化引擎：dimorphite-dl 2.0.2（N 个分子）」，§6 的方法学说明同步改写。
- 引擎缺失/异常一律**回退内置规则表**并如实记录 `engine_fallback_reason` + 安装提示，
  绝不让配体准备失败（CI 环境不装引擎也全绿）。
- **安装口径（重要）**：Dimorphite-DL 2.0.2 的元数据把 `rdkit` 钉在 `<2026`，而本项目要
  `rdkit>=2026.3.6`。实测二者兼容，因此**必须 `--no-deps`** 安装，否则会把 RDKit 降级：

      uv pip install loguru && uv pip install --no-deps dimorphite-dl

  （本次实施时如实踩到：直接安装把 rdkit 降到 2025.9.6，已立刻恢复 2026.3.6 并改用 `--no-deps`。）
- 新环境变量：`LIGAND_PKA_ENGINE` / `LIGAND_PKA_WINDOW`（默认 0.5）/ `LIGAND_PKA_PRECISION`（默认 0.1），
  已登记 `.env.example`；`doctor_probe.py`、`/api/tools/probe`（新增 `pka` 字段）都会报告引擎可用性与版本。
- **回归**：`tests/test_ligand_pka.py` 8 项（引擎状态与安装提示 / 微观态选择规则 / 强制 rules /
  正式入口溯源 / 专业引擎默认生效 / 多微观态可追溯 / 汇总统计 / **未装引擎的 CI 回退路径**）。

## 门禁实测（2026-09-22，本轮）

```
bash scripts/check.sh --fast           482 passed, 247 skipped
bash scripts/check.sh --static         lint 通过 + check_web 179/179 + ruff 通过 + mypy 通过（21 模块）
python scripts/doctor_probe.py         配体 pKa 引擎：dimorphite-dl 2.0.2（auto）
```

---

# 2026-09-22 · 修「同一轮被问两次配体」的另一半：选项明细不再回流给模型 + 指令补质子化口径

> 现场：run `20260922-150752-9080`（重启后的新代码）。日志显示**已按目标 pH 7.4 准备受体**
> （`pdb2pqr + PROPKA + meeko(propka3.0)`，HIS HID×7/HIE×5，边界残基 HIS581 留痕）——
> 受体质子化修复确实生效。但用户仍反馈「为什么还是出现了两次配体的选择」。

## 1. 真因：同一个问题走了**两条通道**

读取该 run 的产物可见两处同时存在：

- `run.choices` = 4 个 `kind=molecule` 可点选项（界面按钮，来自 `publish_choices`）；
- `agent_report.md` = 主管 Agent 的正文里**又抄了一遍 A/B/C/D 表格**，并写「请从以下四种真实
  可选形式中选择一种（界面已给出可点选项）」。

原因是 `fetch_molecule_record` 在命中多组分结构时，把**完整的选项明细**（label/SMILES/prompt）
作为 `choices` 放进了**给模型看的** JSON 返回里，模型于是自然地复述成正文表格。用户看到的就是
「同一批配体选择出现两次」。

**修复**（`tools/choices.py` + `tools/online.py` + `tools/docking.py`）：

- 新增 `CHOICES_PROSE_RULE` 与 `choices_payload()`：给模型的载荷只给
  `status=needs_user_input` + 原因 + `choices_published{kind,count}` + 「**不要在回复里重复列出
  选项/SMILES/参数**，只说明为什么必须由用户决定并请用户在界面点选」；
- 选项明细**只走界面**（`run.data.choices` → SSE `choices` → 前端按钮），不再回流给模型；
- 顺带修正语义：多组分命中时工具状态由 `ok` 改为 `needs_user_input`（记录查到了，但**没有导入
  任何分子**）—— 旧的 `ok` 会诱导模型直接拿某个片段去对接；
- 共晶配体（阳性对照）询问路径同样改为紧凑载荷。

## 2. 附带发现并修掉：指令没写质子化策略，主管 Agent 臆测成 `keep`

同一份产物里主管 Agent 写「当前策略为 `keep`，净电荷 −2 会原样保留」——**与事实相反**
（默认是 `ph` 7.4；该 run 的 `request.protonation` 为空串，会按 env → 默认 ph 解析）。
根因：`_render_params()` 渲染了受体/位点/引擎/搜索强度等默认值，却**没有渲染质子化态策略**，
模型只能猜。

**修复**：`intake._render_params()` 现在显式渲染一行
`质子化态策略：ph（按目标 pH 7.4 分配；配体与理化性质必须同一形式，不要自行更改）`
（`neutralize` / `keep` 同样各自成句；`0` 作为「未设置」哨兵不会与 pH 0 撞车）。

## 3. 回归用例

- `tests/test_online_resolution.py`：新增
  `test_mixture_tool_payload_hides_option_details_from_model`（模型侧不得含选项明细/id，界面侧
  必须仍有完整选项）；`test_fetch_molecule_record_maps_chinese_alias_and_reports_mixture` 断言
  状态为 `needs_user_input` 且带 `choices_published`；结构化选项测试改为直接测
  `mixture_choices()` 纯函数（不再依赖「明细回流给模型」）。
- 复刻该 run 的真实规约渲染，确认指令里出现质子化那行、且不再出现「待向用户确认」。

## 门禁实测（2026-09-22，本轮）

```
bash scripts/check.sh --fast           483 passed, 247 skipped
bash scripts/check.sh --static         lint 通过 + check_web 179/179 + ruff 通过 + mypy 通过（21 模块）
```

---

# 2026-09-22 · 提示词审计后的「风险修复」批次（不改变产品口径的部分）

按「提示词审计」逐条核对了协调 Agent 的 `sp`（`config/agent_llm_config.json`）、4 个子 Agent
提示词（`agents/prompts.py`）、受理层提示词（`intake.INTAKE_SYSTEM_PROMPT`）与工具说明，
把其中**客观错误/自相矛盾**的部分就地修掉；**属于产品取舍的分歧**另列清单交用户裁决（未改）。

## 1. 提示词点名了工具**并不存在**的参数（会白烧一轮模型往返）

- 兜底协调提示词原写 `run_property_assessment(molecules_file=...)`，但该 dispatch 工具签名是
  `run_property_assessment(molecules_json: str = "", runtime)` —— **没有** `molecules_file`；
  配置 `sp` 里也写着「`run_docking`、`run_property_assessment`、`run_binding_mode_analysis`
  都支持 `molecules_file`」。模型照传会拿到 `args_schema` 校验错误。
- 事实是：`run_property_assessment` **自己**把本次运行的产物路径（`artifact_path("molecules")`）
  塞进给子 Agent 的任务参数 JSON，所以协调层**留空调用**即可。两处都改成这个口径。
- 新增**参数存在性守卫**（`tests/test_agent_conventions.py`）：
  `test_prompt_tool_calls_only_mention_real_parameters` 扫描所有模型可见文本里
  `` `tool(param=…)` `` 的写法，用工具真实 `args_schema` 校验参数名；另有负向用例
  `test_prompt_param_guard_rejects_a_planted_phantom_parameter` 证明守卫真能抓到植入的假参数
  （否则它只是装饰）。

## 2. 同一份提示词里自相矛盾的「阳性对照要不要单独对接」

- 纪律 9 与 `dispatch.run_docking` 的实现都表明：给了阳性对照时**同一次** `run_docking`
  会把对照分子一并补入（结果里带 `__positive_control__` 标签），不需要再调一次；
- 但「任务分配」一节还写着「需要排序基线 → 对阳性对照**单独** `run_docking`」，
  纪律 7 也读起来像要单独跑一次。已按代码事实统一：删掉「单独调用」，纪律 7 补一句
  「与候选分子放在同一次对接调用里，工具会自动补入对照分子」。

## 3. 子 Agent「数据交接」块按角色拆分（原先一份块点名 4 个工具）

原来 4 个子 Agent 共用一份 `DATA_HANDOFF_RULE`，里面写死了 4 个子 Agent 工具名：
对口袋 Agent 而言 **4 个它一个都没有**（它的工具不接收文件参数），property/binding 也各只有
1/4、2/4 相关 —— 等于让模型去调不存在的工具。现改为
`DATA_HANDOFF_{COORDINATOR,PROPERTY,DOCKING,BINDING}` 按角色拼装，
`POCKET_SP = _POCKET_SP_BASE + PARAMS_CONTRACT`（不拼交接块），并加断言：
每个角色只能看到自己的工具名。

## 4. 受理层 JSON 契约缺了 `needs_user_input` 键

`INTAKE_SYSTEM_PROMPT` 的规则区 5 处提到 `needs_user_input`（含「仅当…才置 true」的判定），
但示例 schema 里**没有这个键**（`intake.py:853` 确实读它）。已在 schema 示例中补上。

## 5. Docking 子 Agent 的「默认 exhaustiveness=16」改为「不要自己编数字」

子 Agent 基础提示词原写「未指定用默认 `exhaustiveness=16`、`n_poses=1`」，
与协调层「有『本次建议参数（自动规划）』时用规划值」的口径打架（工具签名也无法区分
「未设置」与「16」）。子 Agent 侧改为「未指定时不要自己编数字；有规划值用规划值」。
（`exhaustiveness` 的 `int` 默认值本身是**产品取舍**，见交接给用户的清单。）

## 6. 文档同步

`docs/api.md`、`docs/architecture.md` 里对已删除常量 `DATA_HANDOFF_RULE` 的现在时引用
改为按角色拼装的实际结构（并写明 `run_property_assessment` 没有 `molecules_file`）。

## 门禁实测（2026-09-22，本轮）

```
.venv/bin/python -m pytest -q                     732 passed
bash scripts/check.sh --fast                      483 passed, 247 skipped
bash scripts/check.sh --static                    lint 通过 + check_web 179/179 + ruff 通过 + mypy 通过（21 模块）
```

---

# 2026-09-22 · 提示词审计后的「取舍落地」批次（用户裁决 5 项，全部按推荐项执行）

上一批修的是**客观错误**（点名不存在的参数、自相矛盾的对照对接、按角色拆分交接块…）；
这一批是**产品取舍**——先列清单交用户裁决，再逐项实现。

## 1. 对话回复改为「简短总结」（删掉 7 节聊天报告）

`sp` 的「# 输出格式」原列了 7 节聊天报告，而产物报告是 §0–§9（`customize_report` 说
「§1–§9 骨架不变」、`analyze_pose_pocket` 引用「第 5.2 节」）—— 两套编号并存，
模型很容易把整份报告复述进对话（用户反馈过「同一批内容出现两次」）。
现在对话只给 5 条：理解与分配 / 实际执行了什么 / 关键数值与推荐前 3 名 / 风险与局限 / 产物链接，
明细一律在报告产物里。

## 2. `exhaustiveness` 的「未指定」哨兵（修静默降级）

`run_docking(exhaustiveness=16)` / `molecular_docking(exhaustiveness=16)` 无法区分
「用户显式设了 16」与「没人给值」，而 `plan_docking_params` 算的是
`clamp(round(base × f_rot × f_box), 2, 32)`（常为 20–32）：
协调层一旦漏传规划值（措辞/渲染任一环失效），运行就**静默退回 16**、采样偏低，且报告里看不出来。

- `core/params.py` 新增 `UNSET_EXHAUSTIVENESS = 0` 与 `resolve_exhaustiveness(explicit, plan)`；
- 两个工具的签名默认值 16 → **0**；调度层 `run_docking` 从 `run.data["param_plan"]` 取规划值
  （取不到就**删掉这个键**，而不是下发 `null` —— 子 Agent 的参数契约是「没出现的键 = 未指定」，
  且 `null` 会被 `int` 形态的 `args_schema` 拒掉）；
- 实际用到的强度与**来源**写进该次对接的 `notes`
  （`搜索强度：exhaustiveness=24（受理层自动规划；运行级）`），没有规划值时明说
  「本次没有搜索强度规划值」；
- 回归：`tests/test_param_resolution.py`（哨兵优先级、两个签名默认值必须是 0、
  规划值一路传到 `dock_library`、显式值优先、无规划值时键缺失）。

## 3. 位点不再有「注册已知位点」兜底

纪律 3 原写「结合位点未指定时…或该受体注册的已知位点」，与纪律 3b「位点盒必须由工具确定」
直接冲突。按 3b 统一：未给坐标 → 先 `run_pocket_analysis`；只有用户**明确要求**注册位点、
或口袋工具不可用时才退回（这一句现在是唯一出处）。

## 4.「# 能力」段精简为「何时调哪个工具」

原段 2,228 字符（≈1,268 tokens/次调用）与 12 个工具的描述（8,959 字符 ≈4,576 tokens，
本来就随每次请求发送）逐条重复。现在只保留**路由触发条件**（如「点名受体 → 第一步调
`fetch_protein_structure`」「对接之后的固定动作按顺序」），参数与返回字段交给工具说明。

## 5. 条件纪律段：按运行事实只注入用得上的那段（省固定开销）

系统提示词在**每次模型调用**上重发；「执行纪律」6,181 字符（≈3,960 tokens，占 `sp` 55%），
但单次运行通常只用得到几条。现在 `sp` 用 `<!-- block:KEY -->` 标记出四段，
协调 Agent 的 `dynamic_prompt` 中间件按运行事实抽掉不相关的段：

| 条件段 | 抽掉的条件 | 依据 |
| --- | --- | --- |
| `upload_files` | 确认没有上传 | `task_spec.ligands/receptor.file`、请求里的 `molecule_file`/`receptor_file` |
| `receptor_discipline` | 受体已落实（`source` 非 `default`/`named`） | `task_spec.receptor.source` |
| `funnel_two_stage` | 指令里已带规划建议，或库 < `AGENT_FUNNEL_MIN` | `result.param_plan` / `task_spec.ligands.count` |
| `special_systems` | 已看过一份**干净**的对接结果 | `run.data["prompt_facts"]`（对接工具记账） |

- 新增 `agents/prompt_blocks.py`（标记解析 + 选择规则 + 组装/记录）与
  `runtime/run_facts.py`（工具侧只记布尔：`docking_seen/hetero_atoms/special_chemistry`，
  负面信号**粘住**；放在 `runtime/` 是因为 `tools/` 不得 import `agents/`，有分层回归守护）；
- **安全方向写死**：配置里没有标记 → 全文；条件事实未知 → 注入；组装异常 → 全文
  （宁可多花 token，不可丢纪律）；
- 实测：全文 10,015 字符 ≈6,327 tokens；常见小库运行 5,763 字符 ≈3,778 tokens
  （省 ≈**2,549 tokens / 次模型调用**）；
- 提示词不再是常量 → 注入清单随 `run.data["prompt_blocks"]` 写进 `result.json`；
- 回归：`tests/test_prompt_blocks.py`（配置标记齐全、成对性、失败方向、
  选择矩阵、中间件记录、事实粘性、落盘）+ 端到端确认 `create_agent` 真的用上了组装后的提示词。

## 6. 附带发现：两处静默漂移

**① 文档里的规划基准不一致**：`docs/api.md` 与 `README.md` 都写着「筛选基准 12」，
而 `settings.py::AUTO_PARAM_BASE_SCREENING` 的默认值是 **16**（`docs/技术报告.md`、
`core/params.py` 也是 16）。已按代码改齐。

**② 受理层取证脚本静默失效**：`scripts/intake_eval.py` 的「对话折叠·表单被忽略」用例
一直期望 `params.exhaustiveness=6`，而系统默认自 v0.24 起已是 16（`tests/test_intake.py`
里写着这条），于是该脚本只报「未命中 1 项」而**没有任何门禁发现**（它不在自动门禁里）。
已把期望值改为 16 并加注原因，同时补一条回归
`tests/test_intake.py::test_intake_eval_script_passes_all_its_cases`（子进程跑脚本、
断言「全部用例符合预期」）—— 这类「人工取证脚本」的期望值必须被看护，否则会再次悄悄过期。

## 门禁实测（2026-09-22，本轮）

```
.venv/bin/python -m pytest -q                                   755 passed
DOCKING_ENGINE_TESTS=0 .venv/bin/python -m pytest -q            508 passed, 247 skipped
bash scripts/check.sh --static                                  lint 通过 + check_web 179/179 + ruff 通过 + mypy 通过（21 模块）
```

---

# 2026-09-22 · 报告文案专业化：从「过程日志」到「交付文档」

用户要求「报告专业精简，不要一堆口语化和冗余的描述」。这一轮**只改措辞与重复，不删事实与溯源**：
实跑同一运行（`var/runs/20260922-171953-2502`）由 **17,935 → 11,678 字符（−35%）**。

## 1. 去过程性元话与自我声明

- 删掉「本节是协调 Agent 按本次任务的具体要求组织的部分：§1–§9 仍是系统固定骨架…」；
- 删掉第 1 节「本章列出…供复现与核对；结论只在本章条件下成立」这类填充句；
- 尾部「由系统按固定模板生成，未使用任何模拟或编造数据」→「数值均由真实计算产生…完整溯源见
  `result.json` 与上表产物」；
- 「运行笔记（核心，最多 6 条；完整原文见…）」→「运行笔记（最多 6 条；完整记录见 `run.json` 的 `notes`）」；
- PDF 里的「提示：」→「说明：」。

## 2. 同一件事只写一次（真冗余）

- **质子化口径**原先在 1.3 表格、1.3 引用块、第 4 节、第 6 节（5）各写一遍 → 现在 1.3 一句话 + 第 6 节方法学；
- **同阶段参数一致**原先在 1.3 与 1.5 各一段 → 只在 1.3 一行（1.5 不再重复「原则」与 CSV 留痕段）；
- **等级分布**原先在第 3.1 的提示块与「筛选建议」各写一遍 → 只在筛选建议；封顶数为 0 时不再提；
- **口袋候选表**在多受体块里完全重复 → 按签名只印一次；
- **运行笔记**按签名（屏蔽受体哈希/长标识）去重，同一受体多种准备产生的重复条目不再重复列出；
- 多受体块（同一受体的多种准备）在最前面加一行说明，读者不会把重复的盒子/残基当成排版错误；
- 第 8 节：协调 Agent 文本进报告前**先剥掉复读小节**（需求理解 / 任务分配 / 实际执行 / 报告与产物）
  与过程性句子（「已完成全部流程（decision=run）」「以下是简短总结」「明细见报告产物」），
  只保留**结论 / 建议 / 风险 / 关键数值**类内容；识别加粗编号小标题（`**3. 关键数值与结论**`）。
  配套在协调 Agent 提示词里加一句：这段总结会被报告第 8 节原样收录，用第三人称、不写过程性说明。

## 3. 顺带修正

- `_RECEPTOR_SOURCE_ZH["default"]` 原写「系统默认受体」——与产品底线（**系统没有默认受体**）
  自相矛盾，改为「未指定（系统无默认受体）」；
- 「盒子效应对排序是共模的」→「盒子效应对相对排序无影响」（去掉行话）；
- 「另有 0 个带净电荷但无法中和…」在 0 时不再输出；
- `core/receptors.py` 的运行笔记去掉「**注意**：…（原因见上一条）」这种顺序错乱的指令式措辞；
- 第 3.1 节逐分子提示由 2 条减到 1 条，前缀由「提示：」改「说明：」。

措辞压缩时**刻意保留**的部分（风险与溯源，不能为了短而删）：「**未按目标 pH 重新准备**」与
「两侧质子化条件**不完全一致**」的加粗强调、排序 CSV 的 `charge_input`/`charge_used` 列指针、
盒边长 18/22/28/34 Å 差 1.34 kcal/mol 的实测证据、第 6 节全部 8 项局限、
不判定命中/不跨组比较/失败分子不代表无活性等口径声明。

## 4. 顺带补上：受体溯源改由报告自己给出（精简带来的真缺口）

把第 8 节的「实际执行」小节剥掉之后，**受体溯源**（UniProt accession / 物种 / PDB 号 /
实验方法与分辨率）就从报告里消失了 —— 它原先只存在于 `fetch_protein_structure` 的工具返回与
协调 Agent 那段叙述里。这属于「为了短而丢事实」，必须补：

- `fetch_protein_structure` 成功时把 `provenance` 记为**运行事实**
  （`runtime/run_facts.note_receptor_provenance()`，白名单字段，URL/路径等明细不进）；
  同时补上选中结构的实验方法与分辨率；
- `persistence` 把它写进 `result.json`（`receptor_provenance`），`GET /api/runs/{id}` 的
  `result` 也带上（界面可独立追溯）；
- 报告 §1.2 新增一行「受体来源：UniProt `Q9SJQ6`（DNA glycosylase/AP lyase ROS1，
  Arabidopsis thaliana）；结构 RCSB PDB `7YHP`（EM，3.1 A）；检索词「植物去甲基化酶ROS1」」，
  没有溯源（旧数据 / 直接给 PDB 号）时不出现该行；
- 回归：`tests/test_online_resolution.py`（溯源必须落到 `run.data`，且不带 URL）、
  `tests/test_report_layout.py::test_report_states_receptor_provenance_itself`、
  `tests/test_prompt_blocks.py::test_receptor_provenance_recording_is_whitelisted_and_defensive`。

## 5. 回归

- `tests/golden/report_min.md` 重出（逐字节快照，`REGEN_REPORT_GOLDEN=1`）；
- `tests/test_report_customization.py` 的两处断言随文案更新（调度记录表题、共晶配体行）；
  `tests/test_resilience.py`（「未提供阳性对照」措辞，并改为同时断言「不判定命中」）与
  `tests/test_receptor_ph.py`（笔记断言改成语义更准的「口径不一致」）；
- 报告骨架、表/图编号、9 个章节与全部数值字段未变（另有 `tests/test_report_layout.py`
  与 `test_protonation_ranking.py::test_extract_agent_conclusions_keeps_one_line_summary` 看护）。

## 门禁实测（2026-09-22，本轮）

```
.venv/bin/python -m pytest -q            全量通过
bash scripts/check.sh --fast             全部门禁通过 ✅
```

---

# 2026-09-22 · Agent 之间的信息传递：受众分离 + 输出经济性

用户要求「每一个 agent 的输出信息也要精简高效、专业，准确高效的信息传递」。这一轮把
**同一条信息要服务多个受众**这件事理清：工具产物面向报告与审计（全量），回给模型的载荷
只带决策与转述所需字段；子 Agent 的回执由契约强制「一句话」。

## 1. 受众分离：给模型的视图 ≠ 产物

`tool_io.record()` 一直把**完整**结果写进 `*_tool.json`，报告/排序读的是那份产物
（`persistence` 以产物优先、消息只作回填）。既然如此，回给模型的载荷不必再背一遍溯源长文：

| 工具 | 变化 | 实测（3 分子载荷） |
| --- | --- | --- |
| `molecular_property_assessment` | 逐分子 `protonation` 压成 `{policy, applied}`（`method`/`note`/`variants`/`variant_rule`/`engine_window`/`rules` 留在产物） | 2,085 → 1,146 字符（−45%） |
| `molecular_docking` | 逐分子 `ligand_facts.protonation` 压成聚合口径（`policy/applied/engine/engine_version/ph/charge_*`）、`ligand_warnings` 最多 2 条；受体块去掉**只有报告用**的 `box_atom_stats` | 4,059 → 2,071 字符（−49%） |
| `binding_mode_analysis` | 逐行不再重复对照药效团（顶层已给）、对照属性压成数据字段 | 3,067 → 2,462 字符（−20%） |

- 新增 `core.protonation.compact_protonation()` 与 `PROTONATION_AGGREGATE_FIELDS`：**报告聚合
  需要的字段一个不少**（口径/电荷/引擎），被压掉的只是同一件事的散文描述
  （`method` = "dimorphite-dl 2.0.2（专业 pKa 引擎；pH 7.4 ± 0.5）" 完全等价于
  `engine`+`engine_version`+`ph`+`engine_window`，逐分子重复一遍能占整行 80% 的字节）；
- 受体块保留 `box_center/box_size/box_source/box_validation/pockets`（子 Agent 要原样转述，
  也是多受体运行里早先调用的唯一回填来源），只删报告侧才用的统计；
- 回归：`tests/test_agent_payloads.py`（**非引擎文件**，CI 也跑）逐条断言「视图更小、
  产物更全」，并断言被封顶的字段确实不在视图里。

## 2. 输出经济性：由契约强制，不只靠提示词

- 新增共享块 `prompts.OUTPUT_ECONOMY`（四个子 Agent 都拼上）：只输出契约 JSON、不复述明细数值、
  `agent_note` 至多一句（≤60 字）、明细一律按文件交接；
- `reports.py` 新增 `_NoteContract`：`agent_note` 由 **pydantic 校验器**压成「一句、≤80 字」
  （取第一个句子，超长截断加省略号）。提示词写规矩、契约兜底 —— 这条对
  `PropertyReport`/`PocketReport`/`DockingReport`/`BindingReport` 全部生效，且 `extra="allow"`
  的透传契约不变；
- 四个子 Agent 的「输出格式」段改成**逐行契约**（小库/大库两种形态各一行），删掉与
  `OUTPUT_ECONOMY` 重复的告诫。

## 3. 协调 Agent 每次调用都要付的工具说明也瘦了

`run_docking` / `fetch_protein_structure` / `customize_report` 三个最长的 docstring 重写为
「参数分组 + 留空语义 + 返回形态」，实测 5 个最长工具合计 **2,781 → 1,922 tokens/次调用**
（−859）；参数语义、留空约定、`needs_user_input` 护栏与返回字段一个没少。

## 4. 回归与门禁

```
DOCKING_ENGINE_TESTS=0 pytest -q        516 passed, 247 skipped
pytest -q（全量含真实引擎）              见下（本轮末次全量）
check.sh --static                        lint + check_web 179/179 + ruff + mypy(21) ✅
tests/test_agent_payloads.py             3 项（视图/产物分离）
tests/test_agent_conventions.py          新增「每个子 Agent 都带输出经济性」断言
tests/test_structured_worker_output.py   新增 agent_note 一句/截断/透传用例
```

---

# 2026-09-22 · 修「共晶配体是否用作阳性对照」的询问从不触发（用户实测反馈）

用户反馈：早就规划好的「受体自带共晶配体 → 问一句要不要当阳性对照做结合模式分析」**没有实现**。
排查后确认不是没写，而是**在真实路径上永远走不到询问分支** —— 运行日志已经写明了：

```
[17:20:20] 检测到共晶配体 5CM，但在 （无可读结构） 中都解不出 SMILES，因此未询问是否用作阳性对照
```

## 三个原因（叠加才导致「永不触发」）

1. **原始结构路径没带出来**：受体先被口袋 Agent 准备成
   `7YHP_f7f8da9b58da39a3_ph7.4.pdbqt`，`_pdbqt_spec` 从 sidecar 只读了共晶配体的**残基名**，
   `source_pdb`（原始 `.pdb`）没进 spec —— 而 PDBQT 是**去配体**的，配体原子只存在于原始结构。
2. **缺 `key` 解不出 SMILES**：旧 sidecar 里 `"cocrystal_ligand": "5CM"` 只是字符串，
   没有 `chain:resid:resname`；`cocrystal_ligand_smiles()` 按 key 取原子 → 即便给对 PDB 也返回空。
3. **PDB 号提取在哈希后缀上失效**：`\b([0-9][A-Za-z0-9]{3})\b` 对 `7YHP_f7f8da9b…_ph7.4`
   匹配不到（`_` 是词字符 → 没有词边界），于是「按 PDB 号找回缓存结构」这条兜底也断了。

结果：候选结构列表为空 → 解不出 SMILES → 命中「不拿不确定结构当对照」的保守分支 →
**用户永远不会被问**。讽刺的是，这一轮前后所有测试都是绿的：它们直接喂一个完整的
`cocrystal_ligand` dict，绕过了「从 PDBQT 侧车恢复」这段真实链路。

## 修法

- `prepare_user_receptor`：sidecar 里 `cocrystal_ligand` 改存**完整配体 dict**
  （`resname`/`key`/`n_atoms`/`center`），并新增 `source_pdb`（原始结构绝对路径）；spec 同步返回。
- `_pdbqt_spec`：读回 `source_pdb`；遇到**旧格式** sidecar（只有残基名字符串）时，回到
  `origin_path` 用 `guess_cocrystal_ligand()` 把 `key`/原子数补全 —— 老运行目录也能救回。
- `_ligand_candidate_paths`：候选顺序改为「原始结构 → 本次对接结构 → 请求里的受体文件 →
  `assets/cache` / `assets/receptor/cache`」；PDB 号提取改用前后不接字母数字的断言
  （容忍 `_<hash>_ph7.4`）；候选**只保留真实存在的文件**（否则日志会写「试过 a.pdb」而它并不存在）。
- `tools/docking.py` 的对接前预览块、`core/docking.py` 的受体块都带上了 `source_pdb`/`receptor_key`，
  避免这一环再被谁漏掉。

## 实测（用出问题的那份真实受体）

```
修复前：候选结构路径 [] → SMILES '' → 询问选项 0 个（永不触发）
修复后：source_pdb = assets/cache/7YHP.pdb
        cocrystal_ligand = {'key': 'C:26:5CM', 'resname': '5CM', 'n_atoms': 20, 'center': [...]}
        SMILES = CC1CN([C@H]2C[C@H](O)[C@@H](COP(O)O)O2)C(O)NC1N
        界面侧选项 = [positive_control:C:26:5CM, positive_control:none]
        molecular_docking 返回 status=needs_user_input（**任何引擎都没启动**），
        模型侧载荷只有 choices_published{kind,count}（不重复列出 SMILES/选项）
```

回归：`tests/test_cocrystal_control_offer.py` 四项 —— 旧 sidecar 兼容、新 sidecar 直用、
没有原始结构时**不询问**（保守分支不能被这次修复放松）、PDB 号提取容忍哈希后缀。

---

# 2026-09-22 · 受体不可用时改为硬错误（用户裁决：让用户抉择，不再静默回退预置受体）

上一轮把这条列为「产品底线隐患」交用户裁决，用户决定：**直接报错、提醒用户、让用户抉择**。

## 删除的两处静默兜底（`core/receptors.py`）

| 情形 | 旧行为 | 新行为 |
| --- | --- | --- |
| 上传的受体文件准备失败（不存在 / 内容不是结构 / 缺 meeko 模板） | 回退 **凝血酶(thrombin, 1DWC)** 继续对接，只在小字笔记里写「这不是你指定的受体」 | `ReceptorInputError(reason=prepare_failed)` → `needs_user_input` |
| 受体名既不是 PDB 号 / UniProt / 基因或蛋白名，也不是可读结构文件 | 同上（笔记写「未识别受体，已回退默认」） | `ReceptorInputError(reason=unrecognized)` → `needs_user_input` |

两种情形用户会拿到一份**以凝血酶为受体的答非所问报告**——这是产品底线问题，不是措辞问题。

## 统一回答链路（`tools/choices.py`）

`receptor_input_problem()`（新增）与 `receptor_input_guard()`（把异常转成回答）：

1. 返回 `{"status":"needs_user_input","missing":["receptor"],"reason":…,"options":[…],"message":…}`，
   消息里写清**失败原因**与**三选一**（换文件 / 给 PDB 号或名称由系统在线解析 / 按原因修正后重试）；
2. 把 `task_spec.receptor.source` 写成 `unresolved`（`resolution.status="input_invalid"`）→
   `run_docking` 的护栏在**代码层**拦住后续调用（不靠模型自觉）；
3. 对接工具与口袋工具两条路径共用它；输入归一化失败（`normalize_failed`）也走同一回答
   （旧行为只回一个 `file_error`，主管 Agent 还可能换个受体继续跑）。

`agents/dispatch.py::unresolved_receptor_message` 为 `input_invalid` 增加了专门措辞（不能套用
「你没给受体」那套说法），报告的「受体来源」映射补上 `unresolved → 不可用（待用户确认）`。
协调 Agent 提示词的受体纪律 3c 也补了一条：文件不可用同样是拦截条件。

## 实测（三种不可用输入）

```
① 文件内容不是结构（归一化失败） status=needs_user_input reason=normalize_failed 引擎调用=0 护栏=True
② 受体名无法识别                 status=needs_user_input reason=unrecognized     引擎调用=0 护栏=True
③ 文件不存在                     status=needs_user_input reason=normalize_failed 引擎调用=0 护栏=True
```

## 回归

`tests/test_receptor_ext_upload.py` 相应改写（它原先把回退当预期锁住）：

- 缺失 / 不可用的结构文件 → 断言**抛 `ReceptorInputError`**、原因里带文件名、带三选一；
- 新增 `test_prepare_failure_never_falls_back_to_a_preset_receptor`：三种不可用输入合并断言
  **不产出任何受体 spec**，且注册表里的预置受体一个都不许出现；
- 小分子文件当受体 → `unrecognized`；
- 原先依赖「随便编个受体名也能跑」的载荷用例改用内部测试受体 `thrombin`。

---

# 2026-09-22 · 新增「简易模式」独立页面（保留原页面为高级模式）

用户要求：原页面保留为**高级模式**，另建一个**简易模式**页面，两者靠顶栏导航切换；
简易模式只要「对话 + 结果」两块、去除非必要内容，新手可快速上手（质子化等参数走默认或 AI 决策）；
背景做成现代简约的动态背景；所有改动按 API 标准开发。

## 1. 两套界面 = 同一份后端契约的两个视图

| | 简易模式 `/simple` | 高级模式 `/` |
| --- | --- | --- |
| 内容 | 对话区 + 结果区（前 5 名 + 关键指标 + 报告/CSV/整包链接）+ 最近运行下拉 | 参数表单、历史检索、报告全文、中间数据、设置页 |
| 参数 | **零参数表单**：`mode=chat`、`advanced=false`，质子化态/搜索强度/位点盒全由默认值与受理层自动规划决定 | 表单/高级设置可显式指定 |
| 运行 | `POST /threads/{tid}/runs/stream`（`assistant_id=coordinator`，标准 Agent Protocol） | 同一端点 |
| 结果 | `GET /api/runs/{id}`（服务端唯一权威）+ `/report.pdf` · `/export.csv` · `/download.zip` | 同一批接口 |

后端只加了一条静态路由 `GET /simple`（与 `GET /` 共用 `_serve_page()`）；**没有新增私有接口**，
两套界面走的是同一份标准面与产物接口。顶栏互切：高级模式导航新增 `nav-simple` 链接，
简易模式顶栏有「高级模式」链接。

## 2. 新手可用性（都写成了回归）

- 不会写指令也能用：只写分子名（「阿司匹林、布洛芬」）或只上传文件都能跑（附件走 `POST /api/uploads`，
  按返回的 `kind` 自动归到 `receptor_file` / `molecule_file`）；
- **结构化选项直接变按钮**：受体歧义、共晶配体是否用作阳性对照等，点一下即续跑；
  阳性对照类选择会作为**请求字段**（`positive_control` + `positive_control_decision`）下发 ——
  否则后端读不到、跑完会再问一次（这条在高级模式踩过，简易模式一并守住）；
- 刷新不丢：会话 id 与最近一次运行存 localStorage，重开页面自动载回结果；
- 跑动时可「停止」（标准面 `POST /threads/{tid}/runs/{rid}/cancel`）。

## 3. 动态背景

纯 CSS：三团径向渐变光斑（`mix-blend-mode: screen`）+ 遮罩网格，只用 `transform/opacity` 关键帧，
无外部资源、无 JS 定时器；`@media (prefers-reduced-motion: reduce)` 下完全停止；
`pointer-events: none` 保证背景不吃点击（门禁用 `elementFromPoint` 断言发送按钮在最上层）。
卡片改为半透明 + `backdrop-filter: blur(8px)`，让光斑在内容后面缓慢移动。

## 4. 门禁补齐

- `scripts/check_web.py` 新增第 9 节（**+33 项，179 → 212 全通过**）：资源本地化、双向导航、
  **简易模式不得出现任何参数控件**、请求体固定 `chat + advanced=false`、标准协议与产物链接、
  标准 SSE 帧、背景动效可关闭、窄屏堆叠、`role=log`/aria、HTML 配平与**孤儿 id** 双向校验；
- `scripts/browser_check.py` 新增简易模式用例（**+10 项，34 → 44 全通过**）：真浏览器打开 `/simple`、
  断言背景不挡点击、发送走标准端点且不注入参数、回执与结果区渲染（另存截图 `simple.png`）；
- `scripts/ui_e2e.js`：顶栏入口断言改为「两个视图按钮 + 一个独立页面链接」（**232/232**）；
- 修一处级联缺陷：`.hidden` 在 `styles.css` 里是 `display:none`，而 `simple.css` 在其后加载、
  又给 `.s-choices` 等设了 `display:flex` —— 结果是**空选项面板可见**。现在 `simple.css` 显式重申
  `.hidden { display: none !important; }`，浏览器截图复核确认已消失。

## 5. 提示词一处措辞

「提示他到设置页把策略改为 keep」→「提示他到**「高级模式 → 设置」**把策略改为 keep 后重跑
（简易模式下先让他切到高级模式）」—— 简易模式里没有设置页，不能把用户指向不存在的入口。

## 6. 简易模式文案与尺度复检（用户第二轮反馈）

用户反馈：简易模式也不要用口语化描述、文字尽量精简、字体与布局要更大更大气。改动：

**文案（去掉口语化，全部改成陈述句）**

| 位置 | 原 | 现 |
| --- | --- | --- |
| 欢迎气泡 | 「你好，我是这个系统的助手。告诉我要筛什么分子…我就把整条流程跑完」 | 「输入任务后系统自动完成：分子导入 → 性质评估 → 对接 → 结合模式 → 报告。」 |
| 列表引导 | 「只写分子名称也行（如「阿司匹林、布洛芬」），系统会自己查结构」 | 「可直接写分子名称或 SMILES；」 |
| 参数说明 | 「参数不用管：质子化态、搜索强度、位点盒都由系统按默认与自动规划决定」 | 「参数由默认值与自动规划决定，无需配置。」 |
| 空状态 | 「还没有结果。在上面描述任务并点「开始筛选」，跑完后这里会给出推荐分子。」 | 「暂无结果。提交任务后，此处显示推荐分子与关键指标。」 |
| 脚注 | 「数值由真实计算产生（AutoDock Vina 对接 + RDKit 理化性质）；方法与局限见报告。需要手动调参、看历史与中间数据，请切到高级模式。」 | 「数值来自真实计算（AutoDock Vina / RDKit），方法与局限见报告。手动调参与完整产物：高级模式。」 |
| 运行提示 | 「正在理解任务…」「进行中：已完成 3/20」「已停止（已完成的部分仍会落盘）」 | 「解析任务」「已完成 3 / 20」「已停止，已完成部分仍会落盘」 |
| 底部按钮 | 「下载排序 CSV」「打包下载全部」 | 「排序 CSV」「全部产物」 |
| 对话区标题 | 「1 · 说清要筛什么」/「2 · 看结果」 | 步骤徽标 `01 任务描述` / `02 筛选结果` |
| 附件 | 「＋ 上传文件」「未选择文件」 | 「上传文件」「未选择附件」 |

**尺度（更大更大气，全部只作用于本页）**

- 本页自己的尺度变量：`--s-max: 1320px`（原 1180）、正文 15.5px（原 13）、卡标题 22px、
  指标数字 28px、榜单名称 17px、亲和力 22px、元信息 13.5px；
- 卡片内边距 32px（原 20）、圆角 18px、栏间距 28px；按钮放大一号，主按钮 `12px 32px / 15.5px / 600`；
- 输入框 96px 高、16–18px 内边距、1.75 行距；消息气泡 16/20 内边距、1.8 行距；
- 结果区指标从「一排小 chip」改为**三个等宽大格**（候选分子 / 成功对接 / 最优亲和力），
  次级信息（亲和力单位 / 搜索强度 / 引擎 / 用时 / 状态）收进一行低调的元信息；
- 顶栏加高到 14px 内边距、导航项 15px / 8×20 内边距。

`check_web.py` 的可访问名断言改为正则匹配 `<textarea id="s-input" … aria-label>`，
不再绑死具体文案（文案会继续迭代）。门禁复跑：`check_web` 212/212、`browser_check` 44/44、
`ui_e2e` 232/232。

## 7. 默认首页改为简易模式（用户裁决）

- `GET /` → `web/simple.html`（简易模式，**默认打开就是这个**）；
- `GET /advanced` → `web/index.html`（高级模式，原页面内容不变）；
- `GET /simple` 保留为简易模式**别名**（兼容刚上线这一小时里可能被收藏/引用的链接）；
- 导航指向同步：简易模式顶栏「高级模式」→ `/advanced`；高级模式顶栏「简易模式」→ `/`。

门禁同步（否则它们仍在断言旧语义）：

- `tests/test_api.py::test_index_page_served`：断言 `/` 直出简易模式、`/advanced` 直出高级模式、
  `/simple` 别名仍可用；
- `scripts/browser_check.py`：高级模式用例统一改走 `/advanced#chat` / `/advanced#/settings` /
  `/advanced#manual`；简易模式用例改从**默认首页** `/` 进，并额外断言 `/simple` 别名与
  `/advanced` 都可用（44 → **46/46**）；
- `scripts/ui_e2e.js`：它驱动的是高级模式 → 新增 `ADVANCED = '/advanced'` 常量；
- `scripts/check_web.py`：两面导航的指向断言同步（高级模式的 `nav-simple` 必须指向 `/`，
  简易模式必须提供 `/advanced` 入口）。

## 8. 顶栏标题改中英双语 + 提示去掉文件格式 + 增加「自动在线取数」提示（用户反馈）

- **品牌标题**：中文 `多Agent协作分子对接筛选工作台`，其下为**英文全称**
  `Multi-Agent Collaborative Molecular Docking Screening Workbench`（等宽 + `$ ` 前缀，沿用站点终端风格）；
  两套界面一致，浏览器 `<title>` 分别为 `… · 简易模式` / `… · 高级模式`；
  高级模式导航把「简易模式」排到最前（编号 `00`），与「默认首页是简易模式」一致；
  窄屏（≤1120px）顶栏自动分两行（品牌一行、导航与状态一行），不再把导航压成竖排文字。
- **提示不再列文件格式**：简易模式的引导与附件按钮说明、高级模式的受体/配体/上传/位点提示、
  以及 app.js 里唯一一处面向用户的格式清单（「未指定受体」的提问）都改成不含扩展名的表述
  （如「支持常见结构文件 · 单文件上限 200MB」）；`accept="…"` 这类**功能性**过滤属性保留不动。
- **新增「自动在线取数」提示**：简易模式引导首条改为「只写名称即可：受体与分子会自动从在线数据库检索
  （UniProt / RCSB / AlphaFold / PubChem）」；高级模式的受体提示写明「结构由系统自动到在线数据库检索
  （UniProt → RCSB 实验结构 / AlphaFold 预测结构）」，配体来源提示写明「只写名称或 PubChem CID 也可以：
  分子结构会自动从在线数据库解析」。
- 文档同步：README 标题与产品名对齐（含页面标题说明）、`docs/技术报告.md` 同步并**重新生成带哈希戳的
  DOCX/PDF**。

---

# 2026-09-22 · 修「界面点选了代表结构却没有继续跑」（用户实测反馈）

用户实测：让「拟南芥ROS1 × 代森锰锌」对接 → 系统正确指出代森锰锌是多组分配位聚合物、下发 4 个
可点选项 → **点选之后没有任何对接发生**，且助手仍然在问同一件事。

## 两个叠加的原因（都已修）

**① 点选只把选项文案当普通消息发回**（前端）
`applyChoice` 只对 `kind='positive_control'` 写请求字段，`kind='molecule'` 只发 `choice.prompt`。
于是后端按名称重新查询 → 又是多组分 → 把同一个问题再问一次。
现在新增三个请求字段并**两套界面都下发**：`molecule_choice`（所选 SMILES）、
`molecule_choice_decision`（`raw-mixture`/`metal-monomer`/`organic-fragment`）、
`molecule_choice_label`（展示名，写进报告追溯），受理层据此把配体来源定为
`ligands.source="choice"`：**不再查询、不再询问**，指令里明确写「用户在界面选定的化学形式：…」。

**② 追问丢了受体 → 被判 `ask`**（受理层）
追问消息里通常只有分子。受体继承原先要求「上一轮用户写出带『酶/蛋白/受体』后缀的名字」，
而「把拟南芥ROS1和代森锰锌对接」是**基因符号**写法 → 不被识别 → 上一轮已解析的
`Q9SJQ6 / 7YHP` 被丢掉 → `receptor.source="default"` → 只提问、零计算。
现在继承额外识别基因符号式点名（`ROS1`/`EGFR`/`TP53`…，带 ADMET/PDB/SMILES 等非受体缩写停用表），
并优先继承上一轮已解析出的 accession / PDB 编号。

顺带修掉一个潜伏问题：`pendingPositiveControl` **从不清空** —— 点选「用共晶配体作对照」之后，
同一页面里后续每条消息都会重复下发该对照；现在两类点选决定都在写进请求体后立即清空（一次性）。

## 验证

```
tests/test_intake.py                  +4 项：点选即 run / 无受体仍 ask / 基因符号可继承 /
                                             ADMET·PDB 之类缩写不得被当成受体
scripts/browser_check.py              46 → 52/52：真浏览器点选后，续跑请求必须带
                                             molecule_choice*（SMILES/取法/展示名）且仍在同一会话
scripts/check_web.py                  212 → 219/219：两套客户端都必须按 kind=molecule 处理并下发这三个字段
docs/api.md                           新增 §10.4a「界面点选「代表结构」的续跑契约」（含缺陷复盘）
```

---

# 2026-09-22 · 思维链与长回执一律折叠（用户反馈「thinking 刷屏」）

用户反馈：模型把推理过程整段打进正文，页面被推理刷屏，真正结论看不见。
要求：**所有 thinking 输出都折叠到气泡里的「思考」块**。

## 三处改动

**① 服务端把推理从正文里抽出来**（`runtime/streaming.py`）
新增 `reasoning_of(msg)` 与 `split_thinking()`：DeepSeek 的 `additional_kwargs.reasoning_content`、
Responses 风格的 `summary`、正文里的 `<thinking>/<think>/<reasoning>/<analysis>` 标签、
content blocks 里的 `thinking`/`reasoning` 块，全部抽出成独立的 `thinking` 领域事件；
`token` 只承载正文。`api/agent_service.py` 的 `_DOMAIN_TYPES` 加入 `thinking`
→ 走 `custom` 帧，**绝不混进 `messages/partial`**（否则前端无法区分）。

**② 两套界面都是「默认收起」的思考块**（`web/simple.js` + `web/app.js`）
气泡最前面插一个 `.s-think`（高级模式为 `.chat-think`），默认 `hidden`，
按钮文案 `▸ 思考 · N 字`（展开为 `▾ 思考`），点一下看推理全文；正文始终可见。
简易模式另有长回执折叠：超过 **24 行 / 1800 字符**给「展开全文」（`.s-folded` + `aria-expanded`），
与思考块各自独立。

**③ 门禁看护**
`tests/test_streaming_thinking.py`（4 项：抽取 / 不污染 token / 标签切分 / 无推理时不产生事件）；
`scripts/check_web.py` 219 → 224/224（两套客户端都要有思考块与长回执折叠、且不得把
thinking 渲染成普通消息）；`scripts/browser_check.py` 52 → 61/61（真浏览器：思考块默认收起、
标注「思考」、可展开、推理不得出现在正文里；长回执默认折叠、点开完整显示）。

## 一个断言坑（值得记住）

「长回答默认折叠」最初断言 `#s-log .s-msg-bot p` 的第一个 `<p>` 是否带折叠类 —— 但
`#s-log` 里**第一条助手气泡是页面自带的用法说明**（短、不折叠），于是断言的是说明气泡，
功能其实是对的。现在按**折叠类**定位（`#s-log .s-msg-bot p.s-folded`）并额外断言被收起的
确实是长回执（> 24 行）。

---

# 2026-09-22 · 对话交互对齐市面主流（Markdown 渲染 + 思考「推理中展开、正文到达收起」）

用户实测反馈：「AI 对话框的**交互逻辑**不行 —— Markdown 没渲染，正文直接显示 `##`、`**`、`-`；
思考折叠块的位置/样式也不对。就用市面上差不多的设计就可以了。」

## 两个真实缺陷

**① 简易模式（默认首页）根本不渲染 Markdown。** `simple.js` 把助手回执写进
`<p>.textContent`，于是用户看到的是 `## 对接结果`、`- MOL1：…`、`**加粗**` 这种**源码**。
高级模式虽然早有一份可用的渲染器，但只在**运行结束定稿时**才渲染（`setChatMarkdown`），
流式阶段仍是纯文本 —— 中途看过去同样是源码形态。

**② 思考块的交互不是主流做法。** 两套客户端都是「一出现就收起、只显示 `思考 · N 字`」：
推理正在生成时用户看不到它在想什么，推理结束也没有用时；层次感差。

## 改法

**渲染只有一份实现**：把高级模式的 Markdown→DOM 渲染器（含先转义再生成标签、
`safeUrl` 方案白名单、图片失败降级）整段抽到新文件 `web/markdown.js`，
`index.html` 与 `simple.html` 都在各自客户端之前加载；`app.js` 只留同名薄封装
（`renderMarkdown` / `renderInline` / `safeUrl` / `escapeHtml` / `splitTableRow` / …），
调用点与 `swin.*` 外部契约不变。两套界面的渲染规则与 XSS 姿态从此不会发散。

- **简易模式**：气泡正文改成 `<div class="s-body markdown">` + 渲染器输出，`pre-wrap`
  只留给用户气泡；长回答折叠落在 `.s-body` 上（仍是 24 行 / 1800 字符阈值）。
- **高级模式**：流式阶段（`setChatText`）与整段重渲染都按 Markdown 渲染，
  定稿前后观感一致，不再回退成源码。

**思考块改成主流交互**：推理流式期间**展开可见**（标题 `▾ 思考中 · N 字`），
**正文第一个 token 到达就自动收起**（`finishThinking()`）成
`▸ 思考 · N 字 · 用时 X 秒`，随时可点开看推理全文；位置仍在气泡内、正文之前。推理永不进正文。

## 验证

```
scripts/check_web.py       224 → 230/230：markdown.js 存在且两套页面都在客户端之前加载 /
                                       渲染器只有一份实现 / 思考「思考中 + 自动收起」/ 流式 Markdown
scripts/browser_check.py    61 → 67/67：真浏览器断言 ## → <h2>、列表 → <ul><li>、
                                       对话区不再出现 Markdown 源码符号（两套界面各一组）、
                                       思考块收起后带「用时 N 秒」且推理不进正文
scripts/ui_e2e.js          232/232：高级模式 jsdom 全量回归（渲染器迁移未破契约）
```

踩坑记录：折叠断言原先按 `textContent.split('\n').length` 数行 —— Markdown 渲染后
`textContent` 里已经没有换行（列表项直接拼接），改用**去空白后的字符数**度量长回执。

---

# 2026-09-22 · 修「配体选择问了两次 / 选了前一个，后面的选项不出来」（用户实测反馈）

用户实测：「配体选择出现了两次：一个很快很短，直接让选配体；另一个慢，会说受体解析完成、
配体需要选择，并且会覆盖之前的输出。如果点了先出现的那个选项，后面的选项就出不来了。」

## 四个叠加的原因

**① 服务端：同一问题可以发布两次，第二次会改写已下发的候选。**
`publish_choices` 的幂等只按 `kind + 选项 id` 判重，命中后**仍然**用新的 label/prompt 覆写
`run.data["choices"]`。同一工具被两种写法各调一次（「代森锰锌」/「Mancozeb」解析到同一 CID）
时 id 相同、prompt 不同 → `_tick` 按内容比较判定「变了」→ **又发一条 `choices` 事件** →
界面上的选项被覆盖，用户点了先出现的那份就与后端当前候选错位。
现在：同一问题**只认第一次**（不改写、不重复记日志、不再发事件）；`clear_choices(kind)`
同时撤销该类的幂等标记，保证「先问 → 已答 → 又问」仍能下发。

**② 客户端：清空候选时把**所有**问题一起清掉。** `clearChoicesEverywhere()` 遍历所有消息
把 `choices` 清空，`startRun()` 新一轮开始也会调用它 —— 于是新问题一到，用户还没回答的
旧问题按钮凭空消失（另一个 agent 的问题被覆盖）。
现在按 `kind` 分组：`clearChoices(kind)` 只替换同类问题，`showChoices` 合并而不是取代，
气泡内可同时并存「分子代表结构」与「阳性对照」两组按钮（`data-choice-group`）；
运行记录载入（刷新页面/换设备）时按 `run.choices` 补挂候选。

**③ 客户端：运行中点选被静默丢弃。** `applyChoice` 先清空所有按钮、再被 `startRun` 的
`state.running` 判定 `return` 丢掉 —— 用户看到「点了没反应，后面的选项也不出来了」，
而且 `pendingMoleculeChoice` 已经写进状态，可能污染下一轮。
现在运行中候选按钮**点不动**（`setRunning`/`setBusy` 统一同步 `disabled`），程序路径被调用时
给出明确提示（「本轮仍在运行：请等结束后再点选，或先点「停止」」）且**不清空**按钮。

**④ 服务端：等用户选择被记成 `no_op`。** 受体已解析成功、工具确实跑过，只因配体侧要用户
确认就落成 `no_op` → 历史列表显示 `[ SKIP ]`、日志说「未执行计算（未调用任何工具）」。
现在状态记为 **`needs_user_input`**（界面标签 `[ ASK ]`，`statusLabel` 显示「等待用户选择」），
`no_report_reason` 写「已向用户提出确认问题（…），等待在界面上选择」。

## 验证

```
tests/test_choices_lifecycle.py     新增 5 项：首次为准 / clear 后可再问 / 清一类不动另一类 /
                                              等选择状态 ≠ no_op / 对照组仍是 no_op
scripts/browser_check.py            67 → 78/78：真浏览器「两问共存、回答其一不清另一、
                                              运行中点不动、运行中点选不先清空、跑完恢复可点」
scripts/check_web.py                230 → 236/236：按 kind 分组 / 运行中提示 / disabled 同步 /
                                              [ ASK ] 状态 / 载入记录补挂候选
scripts/browser_choice_checks.py    新增：候选选择交互场景独立成文件（browser_check.py 已被
                                     lint_local.py 的 700 行上限约束，见 SCRIPT_BIG_FILE_EXEMPT）
```

顺带记一条诊断经验：`no_op` 与「等用户选择」在**产物层面**很像（都没有 report/ranking），
但语义完全不同 —— 前者是「没受理」，后者是「受理了、跑了一部分、卡在人的决定上」，
混用会让界面给出与事实相反的说明。

---

# 2026-09-22 · 修「一个分子对接出两行」+ 对话气泡去掉默认折叠

用户实测两条：①「就一个分子，怎么对接出来两个」；②「把对话气泡默认折叠去除」。

## ① 一个分子 → 两行结果（运行 `20260922-203256-6431`）

先纠正对象：用户点名的 `20260922-203229-5062` 是 `decision=ask`（缺受体）的**零计算**运行；
真正出现两行的是同一会话里紧随其后的 `20260922-203256-6431`：`molecules.json` 只有 **1** 个分子，
`ranking.json` 却有 **2** 行同名同分（−4.13）的结果。

复盘（两条 SMILES 是同一物质的两种写法）：

```
用户点选：C(CNC(=S)[S-])NC(=S)[S-].C(CNC(=S)[S-])NC(=S)[S-].[Mn+2].[Zn+2]
在线查询：S=C([S-])NCCNC(=S)[S-].S=C([S-])NCCNC(=S)[S-].[Mn+2].[Zn+2]
canonical SMILES 与 InChIKey 完全相同（CHNQZRKUZPNOOH-UHFFFAOYSA-J）
```

黑板里 `add_molecules` 用**规范** SMILES 做键，而 `set_properties` / `set_docking` / `set_binding`
用**原始** SMILES 做键 —— 同一物质于是各占一个键：`molecules` 变成 2 条，对接工具的输入
（`board_molecules_json`）就是 2 个配体，`library_n=2`、`box_group_counts.main=2`，
最终排行两行、分数一模一样。

**修复**：
- 新增 `runtime/blackboard.canonical_key()`，四张分子键控表（`molecules` / `properties` /
  `docking` / `binding`）与 `get_property()` 查询统一用它；同一物质只保留一条（值仍是原始行，可追溯）；
- 新增 `dedupe_molecules()`（与 `add_molecules` 同一套身份规则），`molecular_docking` 在对接前
  对配体清单做**身份兜底去重**并记日志 —— 即使上游仍塞进两种写法，也不会重复对接。

## ② 对话气泡不再默认折叠

两套界面的助手回执曾按 **24 行 / 1800 字符**阈值默认折叠（按钮「展开全文」/「展开」），
报告块也一样。用户要求去掉：现在助手正文与报告块**一律全文直接显示**；
`chatNeedsFold` / `makeChatFoldToggle` / `syncChatFold` 与 `.chat-fold` / `.s-folded` /
两个折叠按钮样式全部删除。默认收起的**只剩「思考」块**（推理不该刷屏，结论不该被藏）。

## ③ 刷新网页后结果区不再残留上一次运行

用户实测：「刷新了网页，怎么右边还是显示上一次的结果。」
原因是简易模式启动时读 `localStorage['dsa.simple.lastRun']` 并**自动 `loadRun(last)`** ——
聊天气泡是空的（前端不持久化对话），右侧却挂着上一次的榜单与报告链接，两边对不上。
现在刷新 = 干净一页：结果区保持空态，下拉默认停在占位项「历史运行」；
要回看往次结果必须**显式选择**（空态文案会提示上一次的 run id）。
运行结束后仍然自动载入本次结果（这条不变）。

## 验证

```
tests/test_blackboard_identity.py   新增 5 项：两种写法 canonical 相同 / 属性不再多出一个分子 /
                                              对接与结合模式按身份落键 / dedupe_molecules 报数 /
                                              不可解析写法退回原文
scripts/check_web.py                230 → 236/236：候选分组与运行中禁点 + 「不再默认折叠」
scripts/browser_check.py            76/76：高级模式长回执无折叠按钮且超 800 字符完整显示；
                                    简易模式长回执同样不折叠；候选选择四条交互回归
scripts/ui_e2e.js                   230/230：长助手正文默认全文显示（无 .chat-fold）
```

顺带说明「为什么两个 agent 会问同一件事」：那条已在上一个条目修掉（候选按 `kind` 分组 +
服务端同一问题只认第一次）。本次两行结果与提问重复无关，是**分子身份键**的口径不一致。

---

# 2026-09-22 · 修模型端 400「insufficient tool messages following tool_calls」

用户报的错：

```
Error code: 400 - An assistant message with 'tool_calls' must be followed by tool messages
responding to each 'tool_call_id'. (insufficient tool messages following tool_calls message)
```

## 为什么原来的自愈没兜住

`agents/threads.py` 早就处理这个故障（取消/失败时模型已发出 `tool_calls` 而工具没回执），
但它只覆盖**协调 Agent 的 thread**、且只在**每轮开始前**跑。两个漏洞：

1. **子 Agent 的固定角色线程完全没修**：`dispatch.py` 给四个子 Agent 用的是固定 thread
   （`thread_id="property"/"pocket"/"docking"/"binding"`）+ 各自的 `InMemorySaver`。
   一次取消 / 触发限额（`ToolCallLimitMiddleware(exit_behavior="end")`）/ 工具异常，
   都会把「AI(tool_calls) 而没有回执」留在子 Agent 线程里 —— 下一次调用同一个子 Agent
   直接把非法历史发给模型 → 400，整轮卡死。
2. **运行过程中新产生的悬空没人管**：只在下一轮开头修，本轮内后续的模型调用仍会撞上。

## 修法（以及踩到的一个坑）

新增 `ToolCallPairingMiddleware`，挂在**所有** Agent（协调 + 4 子 Agent，见
`agents/middleware.py::build_agent_middleware` 的第一件）的 `before_model`：每次模型调用前
检查 `state["messages"]`，把非法序列修成合法。

坑：第一版沿用「在 AIMessage 正后方插一条占位 `ToolMessage`」，实测**不生效** ——
LangGraph 的 `add_messages` 只把**新 id** 追加到**末尾**（同 id 才原地替换），于是占位回执
落到了最新一条人类消息之后，模型实际收到
`[Human, AI(tool_calls), Human, ToolMessage]`，顺序依旧非法（已用真实 `create_agent` 图复现）。
现在改成**同 id 原地改写那条 AIMessage**：去掉没有回执的 `tool_calls`（有回执的保留，
支持并行工具只回一半的情况），并在正文追加一句事实说明（哪个工具被中断、没有结果、
如仍需要请重新调用）；孤儿 `ToolMessage`（配对的 AIMessage 被摘要/裁剪切掉）用
`RemoveMessage` 移除。顺序天然合法，不需要插入任何消息。

`repair_thread_state()`（每轮开始前修协调 Agent 的 thread）改用同一套 `pairing_updates()`，
因此中间位置的悬空也能修好（旧实现同样只对"悬空在尾部"有效）。

## 验证

```
tests/test_tool_call_pairing.py    新增 10 项：合法历史不动 / 尾部与中间悬空原地改写 /
                                             并行只回一半保留已回执的 / 孤儿回执移除 /
                                             异步钩子一致 / 四个角色都挂了中间件 /
                                             真实 create_agent 图：模型**实际收到**的序列合法
tests/test_thread_healing.py       改为断言「同 id 原地改写」与真实 checkpointer 落盘结果
tests/test_agent_middleware.py     中间件清单加入 ToolCallPairingMiddleware；并断言它**关不掉**
                                   （防的是模型端 400，属于正确性，不是预算开关）
```

---

# 2026-09-22 · 候选选择改到「模型输出结束」之后再出现

用户实测：「运行任务时，模型一开始输出『我先并行解析受体（植物去甲基化酶 ROS1）与配体（丙森锌）』，
后续输出还没出现，配体选择就出现了；如果提前选择，后续就取不到这次选择。
选择请放到模型完成输出后再出现。」

成因：服务端在**工具返回时**就下发 `choices`（可能远早于本轮结束），前端收到即渲染 ——
于是按钮出现在模型仍在输出（还在调别的工具）的时候。用户此刻点选会与进行中的运行抢跑：
要么被运行态拦下、要么这次选择与后续那批候选错位，最终「取不到选择」。

改法：**两套界面都先缓冲、等本轮结束再挂出**。

- 简易模式：`choices` 事件只写 `state.deferredChoices`；流结束的 `finally` 里
  **先 `setBusy(false)` 再 `flushDeferredChoices()`**（顺序要紧：按钮的可点性按运行态决定，
  先渲染会得到一排 disabled 按钮）。
- 高级模式：`handleChoicesEvent` 只入队 `state.deferredChoices`（按 `kind` 去重）；
  `setRunning(false)`（正常结束 / 取消 / 出错都走它）统一 `flushDeferredChoices()`。
- 运行记录里本来就带着这次候选（`run.choices`），所以刷新或换设备仍能补挂。

回归：

```
scripts/browser_check.py        78 → 83/83：真浏览器断言「缓冲期间界面上没有按钮、
                                           本轮结束后才出现」（两套界面各一条 + 缓冲不丢）
scripts/check_web.py            236 → 238/238：两套界面的 choices 处理必须走缓冲
                                           （`renderChoices(event.choices` / `showChoices(choices,`
                                           直接渲染的写法不允许再出现）
```

---

# 2026-09-22 · 修 `GRAPH_RECURSION_LIMIT`（递归上限被三重原因撞穿）

用户报错：

```
Recursion limit of 60 reached without hitting a stop condition.
```

三个叠加原因，全部修掉：

**① 子 Agent 调用根本没给 `recursion_limit`。** `workers.py::invoke_worker` 只传了
`configurable.thread_id` → 用它的是 LangGraph 的**默认值 25**。子 Agent 要连着调
「解析受体 → 口袋分析 → 对接（分批）→ 结合模式」多轮工具，很快就顶到上限，而报错信息里
只会说"60"（协调 Agent 的值），根本看不出是子 Agent。
现在子 Agent 调用显式带上 `recursion_limit`（与协调 Agent 同一个 `RECURSION_LIMIT`）。

**② 默认 60 对长任务偏紧。** 每个模型调用至少消耗 2 个 super-step（model + tools），
60 步只够 ~30 轮。默认提到 **120**（`config.DEFAULT_RECURSION_LIMIT`，`settings.py` 的
Spec 默认值同步；settings 只依赖 `envs.py`，不反向导入 config，因此写字面量并由
`tests/test_recursion_limit.py` 看护两者不发散）。

**③ 工具调用配对自愈不能多占步数。** 上一节新加的 `ToolCallPairingMiddleware` 原先挂
`before_model` —— LangChain 会把它编译成图里的一个**节点**（实测节点名
`ToolCallPairingMiddleware.before_model`），于是**每次模型调用多一个 super-step**，
等于把可用轮数砍掉三分之一，正是这次撞墙的放大器。
现在改成 `wrap_model_call` / `awrap_model_call`（包裹模型调用）：直接改**这一次请求**的消息，
图结构不变。代价是落盘状态保持原样 —— 由每轮开始前的 `repair_thread_state()` 兜住，
两条路径互补（回归里同时断言这两件事）。

## 验证

```
tests/test_recursion_limit.py      新增 5 项：默认 120 / settings 与 config 不发散 /
                                           子 Agent 显式带额度 / 环境变量覆盖生效 /
                                           配对中间件**不得**在图里多出一个节点
tests/test_tool_call_pairing.py    改为断言包裹式钩子（请求被改写、状态不被改写、
                                           真实 create_agent 图：模型收到的序列合法）
```

---

# 2026-09-22 · 跑满步数不再报错：自动放宽 → 主管 Agent 收尾（用户定的产品准则）

用户要求：「不单是额度的问题，我希望主管的 agent 可以根据情况让其继续跑，而不是报错误。
整个项目要尽可能的少打扰用户，自动处理，**除非是涉及到影响对接的问题**。」

据此把「步数上限」从**报错**改成**执行细节**（新增 `runtime/limits.py` 统一策略）：

1. **自动放宽并继续**：命中 `GRAPH_RECURSION_LIMIT` 时不再上抛 ——
   `RECURSION_LIMIT`（120）→ 240 → 480（天花板 `RECURSION_LIMIT_MAX`，默认 4 倍），
   从 checkpoint 继续（`payload=None`，不重放用户消息）。协调 Agent（`runtime/streaming.py`）
   与**子 Agent**（`agents/workers.py`）同一套阶梯；子 Agent 到顶时如实返回
   `{"status":"agent_step_limit"}` 交主管 Agent 决策，而不是抛异常。
2. **让主管 Agent 收尾**：放宽到最后一档时注入一条 **SystemMessage** 收尾提示
   （用已有结果给结论、不要再起长流程工具、未完成部分如实说明、不得臆造数据），
   让它用剩余步数收尾。用 SystemMessage 是有意的：不进用户可见对话，也不污染受理层的
   历史继承（`_recent_prior_turns` 只读 human/ai）。
3. **到顶也正常结束**：发一条 `limit` 领域事件如实告知（走 `custom` 帧，前端只显示状态文案，
   **不产生任何需要用户回答的问题**），并用现有结果落盘 —— 排行/报告照常产出。
4. **顺带**：`.env` 里写死的 `RECURSION_LIMIT=60` 也同步到 120（默认值改了但 .env 会覆盖，
   不改就等于没改）；`.env.example`/README/技术报告同口径。

## 验证

```
tests/test_step_budget.py          新增 9 项：放宽阶梯与天花板（含环境变量覆盖）、错误识别、
                                           真实循环图「起始 4 步 → 自动放宽后跑完且无 error」、
                                           到顶时发「收尾」事件并正常 done、子 Agent 自动重试、
                                           子 Agent 到顶返回 agent_step_limit、收尾提示内容
scripts/check_web.py / browser_check.py  前端只多了一行状态文案的显示分支（limit 事件）
```

回归口径：**任何执行类失败都不该变成用户要回答的问题**；只有「影响对接本身」的歧义
（受体不可用/歧义、代表结构、阳性对照）才中断并请用户决定。

---

# 2026-09-23 · 修 CI：部署自检在无密钥的 runner 上假失败

GitHub Actions 的 `deploy` job 首次运行即红：`coordinator/property/pocket/docking/binding`
五个图构建失败（`LLMConfigError: 未配置 LLM API Key`）、`var/runs` 不存在、LLM 配置项失败。

根因：`langgraph-deploy/scripts/verify_graphs.py` 是给**本机**写的自检 —— 把「本机没有模型端点」
当成部署不可用；而干净检出既没有 `projects/.env`（已 gitignore），也没有 `var/`（运行时才创建）。

改法（不放松真正该拦的东西）：
- **分级**：新增 `[SKIP]`，与 `[FAIL]`（退出码 1）/ `[WARN]` 区分。缺少前置条件（没有 key）时
  图构建与模型实例检查标 `[SKIP]`，**不计失败**，并在结尾逐条列出跳过了什么 —— 不把「没检查」说成「通过」。
- **CI 用占位 key 真跑**：`gate.yml` 的 deploy job 设 `LLM_API_KEY=ci-graph-build-only`
  （端点指向不可达地址），LangChain 实例构建不发请求，因此「图加载 + 工具集核对」在 CI 里仍然真跑。
- `var/runs` 缺失时按运行时行为**创建**（干净检出也能过），权限问题才判失败。
- 另一处 CI 会红的用例：`tests/test_security_boundary.py::test_legacy_run_accepts_wellformed_x_run_id`
  断言 200，但请求会构建 Agent 图 → 无 key 时 500。改为把图替换成**最小假图**（并断言请求确实走到图），
  用例只验证 id 白名单与运行目录落盘，不再依赖本机是否配置密钥。

验证（本机模拟 CI）：
```
LLM_API_KEY= OPENAI_API_KEY= DOCKING_ENGINE_TESTS=0 pytest -q      # 563 passed, 248 skipped
bash langgraph-deploy/scripts/check.sh                              # 无 key：全部通过，7 项跳过
LLM_API_KEY=ci-graph-build-only ... bash langgraph-deploy/scripts/check.sh   # 全部通过（图真加载）
LLM_API_KEY=ci-graph-build-only ... bash langgraph-deploy/scripts/test.sh    # 46 passed, 4 skipped
```
