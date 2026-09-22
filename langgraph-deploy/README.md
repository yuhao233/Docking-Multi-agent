# docking-agent → LangGraph Studio / LangGraph CLI 部署层

把 `../projects`（多 Agent 分子对接筛选系统）**原样加载**进 LangGraph Studio 与 LangGraph CLI，
不改动项目本身一行代码。

```
langgraph-deploy/
├── langgraph.json          # LangGraph CLI / Studio 的部署清单（图入口、env、Python 版本）
├── pyproject.toml          # 部署包（依赖与 ../projects 逐条一致，check.sh 会比对）
├── pip.conf                # Docker 构建时的 pip 源（阿里云镜像）
├── docking_graphs/
│   ├── graphs.py           # 6 个图入口（协调 / 受理 / 4 个子 Agent）
│   └── runtime.py          # 运行上下文适配（run 目录 + ContextVar，与网页端一致）
├── scripts/
│   ├── install.sh          # 建独立 .venv + 安装（editable 安装项目源码）
│   ├── check.sh            # 自检：依赖一致性 + 每个图真加载
│   ├── verify_graphs.py    # 自检实现（图 / 工具集 / 工作区 / LLM / 计算引擎）
│   ├── dev.sh              # 启动 langgraph dev（连 Studio 的入口）
│   ├── smoke.py            # 冒烟：对已启动的服务做真实调用（coordinator / 子 Agent）
│   ├── prepare_build.sh    # 第二阶段：把源码同步进 Docker 构建上下文
│   ├── build.sh            # 第二阶段：langgraph build 镜像
│   └── up.sh               # 第二阶段：langgraph up 全栈（api+postgres+redis）
├── docs/
│   └── studio-connected.png   # Studio 连上本机 Agent Server 的实拍（本轮验证）
└── .env                    # 由 install.sh 从 ../projects/.env 复制（600，已 gitignore）
```

## 1. 一次性安装

```bash
cd /home/biolab/Tools/docking-agent/langgraph-deploy
bash scripts/install.sh              # 建 .venv 并安装（约 800MB、几分钟）
bash scripts/check.sh                # 自检，必须全绿
```

- **不动 `../projects/.venv`**：部署环境是独立 venv；项目源码以 `pip install -e ../projects`
  方式装入，因此**改项目源码即时生效**，且 `paths.project_root()` 仍指向 `projects/`
  → 资产（受体/示例库）、`config/`、`var/runs/` 全部沿用现有项目，单一事实来源。
- 缺 `.env` 时自动从 `../projects/.env` 复制一份（600 权限）。要换模型/端点，改这个
  `.env`（或直接用环境变量覆盖，项目内 `.env` 只作兜底，不会被覆盖）。

## 2. 启动并连 Studio

```bash
bash scripts/dev.sh                  # 默认 127.0.0.1:2024
# 或：PORT=8123 HOST=127.0.0.1 bash scripts/dev.sh
```

启动后：

| 入口 | 地址 |
| --- | --- |
| Agent Server API | `http://127.0.0.1:2024`（`/ok`、`/assistants`、`/threads`、`/runs/stream` …） |
| **LangGraph Studio** | `https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024` |

Studio 是浏览器里的托管前端，通过 `baseUrl` 直连你本机 2024 端口的 Agent Server；
图、线程、状态、逐节点中间结果都在 Studio 里可视化。不用 Studio 也可以纯 REST/SDK 调用。

> **首次打开 Studio 必须允许「访问本地网络」**：Chrome / Edge 的
> *Local Network Access* 保护会拦截「公网页面（smith.langchain.com）→ 本机 127.0.0.1」的请求，
> 浏览器会弹一个权限询问，**必须点「允许」**；否则 Studio 顶部显示
> `Failed to initialize Studio / Failed to fetch`（控制台会写
> `Permission was denied for this request to access the loopback address space`）。
> 实测：同一页面授予 `local-network-access` 权限后立刻 `Connected`（见
> `docs/studio-connected.png`）。不想被这个权限拦，可用 `langgraph dev --tunnel`
> 换成 https 的临时地址（需要 LangSmith 账号）。
>
> **想让 Studio 里也能看到 trace**（可选）：在部署 `.env` 里加 `LANGSMITH_API_KEY=...` 与
> `LANGSMITH_TRACING=true`；不加也能正常跑图，Studio 只提示「Not seeing LangSmith runs?」，
> 运行产物照样落在 `../projects/var/runs/<run_id>/`。

## 3. 暴露了哪些图

| 图名 | LangGraph Studio 里做什么 | 需要 LLM |
| --- | --- | --- |
| `coordinator` | **主入口**：整体协调 Agent，把任务拆给 4 个子 Agent（属性→口袋→对接→结合模式→推荐→报告） | 是 |
| `intake` | 任务受理层：自然语言 + 表单 → 结构化任务规约（task_type / authority / 缺失项 / 假设） | 可选（`use_llm=false` 走纯规则） |
| `property` / `pocket` / `docking` / `binding` | 4 个子 Agent 本身，便于逐步调试单个环节 | 是 |

调用示例（`coordinator`，Studio 里给 `messages` 即可）：

```json
{"messages": [{"role": "user", "content": "用凝血酶对 CCO、c1ccccc1 做筛选，出报告"}]}
```

`coordinator` 的输入是标准对话输入（Studio 的 Input 面板可直接编辑 messages）：

```json
{"ligands_text": "乙醇,CCO\n苯酚,Oc1ccccc1", "receptor": "thrombin",
 "exhaustiveness": 1, "save_poses": true, "pocket_engine": "known_site",
 "site_center": [31.5, 13.74, 24.36], "site_size": [22, 22, 22]}
```

## 4. 运行上下文（为什么 Studio 里跑出来的产物和网页端一样）

项目的工具层通过 **ContextVar** 拿「当前运行」（`var/runs/<run_id>/` 落盘、子 Agent 黑板交接）。
网页端由 `api/app.py` 在每个 SSE 请求里注入；Studio/Platform 没有这层，因此
`docking_graphs/runtime.py` 提供等价的运行作用域：

- 进入节点 → 建 `var/runs/<run_id>/`（`kind=studio`）、设置
  `current_run` / `current_blackboard` / `request_context`；
- **在同一协程内**调用内层 Agent（ContextVar 才能可靠传播到工具与子 Agent）；
- 跑完 → 调用项目自己的 `agents.persistence.persist_agent_run()` 与 `run.finish("ok")`
  —— 这一步保证 Studio 里的产物和网页端**完全一致**（`docking.json` / `ranking.csv` /
  `report.md` / `report.pdf` / `agent_report.md` / `charts/` / 各角色模型与调用次数），
  而不是只剩 `*_tool.json` 中间文件；
- 失败 → 如实 `run.finish("error", ...)` 后再抛出，运行目录里能看到原因；
- 退出 → 复位 ContextVar，并把 `run_id` / `run_dir` 作为图输出返回（Studio 里一眼看到产物在哪）。

**两个必须踩过的坑（已在代码里解决）**：

1. `langgraph dev` 默认开启 blockbuster：事件循环里出现同步阻塞调用（`os.mkdir`、
   首次 `import matplotlib`）就会让整次运行以 `BlockingError` 失败。因此建 run / 写盘
   一律走 `asyncio.to_thread`，重量级导入放在**模块顶层**（图加载发生在事件循环之外），
   并把 `MPLCONFIGDIR` 固定到 `projects/var/cache/matplotlib`（install.sh 自动写入 `.env`）。
2. 图**不能自带 checkpointer**——Platform 自己托管持久化。协调 Agent 用项目的
   `build_agent()` 构建，只在构建瞬间把 `get_memory_saver` / `workers._checkpointer`
   替换成「返回 None」；工具列表、提示词、按角色的 LLM 实例全部复用项目源码 ——
   不存在「部署层抄一份、和项目漂移」的问题。

## 5. 常用命令

```bash
# 自检（依赖一致性 + 图加载 + 工具集 + 工作区 + LLM + 计算引擎）
bash scripts/check.sh

# 冒烟：对**已启动**的服务真实调一次 coordinator（需要可用的 LLM）
.venv/bin/python scripts/smoke.py
# 再带上 coordinator（真实 LLM 多 Agent + 真实对接，约 30–60 秒）
.venv/bin/python scripts/smoke.py --with-coordinator

# REST 冒烟：列出图（注意 /assistants 只接受 POST search）
curl -s -X POST http://127.0.0.1:2024/assistants/search \
  -H 'Content-Type: application/json' -d '{"limit":20}' | python3 -m json.tool | head -40

# 跑一次流水线图（真实对接）
curl -s -X POST http://127.0.0.1:2024/runs/wait \
  -H 'Content-Type: application/json' \
  -d '{"assistant_id":"coordinator","input":{"message":"用示例库对接 thrombin 前 2 个分子","
       "receptor":"thrombin","exhaustiveness":1,"save_poses":true}}' | python3 -m json.tool | head -60

# 看这次运行写了什么
ls -la ../projects/var/runs/<run_id>/
```

## 6. 测试与验证记录（真实跑过，不是纸面配置）

### 6.1 自动化测试

```bash
bash scripts/test.sh          # 离线：46 项通过 + 4 项跳过（live，不联网 / 不调 LLM / 不真对接，约 2 秒）
bash scripts/test.sh --live   # 离线 + 真实服务集成：8 项（自起隔离 dev server + 真实 Vina + 真实 LLM）
```

| 套件 | 覆盖 | 结果 |
| --- | --- | --- |
| `tests/test_config.py` | `langgraph.json` 解析、7 个入口、依赖与项目一致、`env`/`pip.conf` 存在、无 checkpointer | ✅ |
| `tests/test_runtime.py` | `create_run` / `bind_run`（三个 ContextVar 的置位与复位，含异常路径）/ `detached_messages` / `last_user_text` / `call_meta` | ✅ |
| `tests/test_agent_graph.py` | 假 `create_agent` 图：`persist_agent_run` 被调用、消息只增不重、多轮 id 序列正确 | ✅ |
| `tests/test_schemas.py` | coordinator 的 `messages`、intake 字段、子 Agent schema | ✅ |
| `tests/test_blockbuster.py` | 在 blockbuster 下跑两种节点，断言无 `BlockingError`（另含自证用例：同 harness 下 `os.mkdir` 必抛） | ✅ |
| `tests/test_live_server.py` | 真实服务：图清单、schema、未知 assistant 4xx、多轮记忆、服务日志无 BlockingError | ✅ |

### 6.2 真实跑过的证据

| 验证项 | 结果 |
| --- | --- |
| `scripts/check.sh` | 依赖清单与 `../projects` 完全一致；6 个图全部加载成功且**无 checkpointer** |
| 工具集一致性 | 协调 Agent 12 个工具、property 2 / pocket 5 / docking 3 / binding 3 个工具，全部来自项目源码 |
| 运行环境 | 工作区解析到 `projects/`，可对接受体 2 个（thrombin_1DWC / trypsin_1PTU），LLM 6 个角色实例可构建 |
| ~~`pipeline` 图~~（该图已移除，见 ADR-0001 与 projects/README 记录 60） | 历史实测：2 分子 + Vina，4 秒完成，22 项产物 |
| `coordinator` 图（真实 LLM 多 Agent） | **34 秒**完成，14 条消息，产出 `docking.json` / `report.md` / `report.pdf` / `agent_report.md`，并记录各角色模型与调用次数 |
| 并发 3 个 run | 3 个 `run_id` / 3 个运行目录互不串台，各自都有 `docking.json` |
| `property` 子 Agent 图 | 5 秒，产出 `properties.json` 与报告 |
| `intake` 图 | 0.5 秒（`use_llm=false` 纯规则），任务规约 `task_type=screening / authority=chat / decision=run`，指令里渲染「搜索强度=自动（基准 16）」 |
| Studio 前端连本机服务 | 已 `Connected` 并渲染出图的输入表单（截图 `docs/studio-connected.png`，为 `pipeline` 图存在时的记录） |

### 6.3 本轮修掉的两个真实缺陷（都由上面的测试守住）

1. **内层 Agent 图在事件循环里构建**（`_wrap_agent` 原先是「每个请求都 build 一次」）。
   构建会读 `config/agent_llm_config.json`（`Path.exists()` → `os.stat`）。
   `langgraph dev` 的 inmem 运行时**显式把 `os.stat` 从拦截列表里摘掉**了
   （`langgraph_runtime_inmem/queue.py` 的 `to_disable = ["os.stat", ...]`），所以在本机 dev 下
   「恰好」没炸；但用默认 `BlockBuster`（不摘 `os.stat`）的 harness 里必然抛
   `BlockingError: Blocking call to os.stat`（`tests/test_blockbuster.py` 就是按更严格的配置守的），
   而且每请求重建 5 个 LLM 实例纯属浪费。
   → 现在内层图在**图构建阶段**建好并缓存（事件循环之外），节点里直接用缓存对象。
2. **（历史）流水线节点不自行收尾运行状态**：内层异常被吞或只返回字典时，`run.json` 会永远停在
   `status="running"`。→ 现在节点自己 `try/except`：失败落 `error` 再抛，成功但内层没 finish 时兜底 finish。

### 6.4 一个使用陷阱（写进测试）

多轮记忆必须用**线程级**端点：`POST /threads/{thread_id}/runs/wait`。
把 `thread_id` 放进 `POST /runs/wait` 的 body 会被当成**无状态**运行（每轮新建线程，
第二轮看不到第一轮历史）——`tests/test_live_server.py::test_thread_history_accumulates_without_duplicates`
就是按正确用法写的，`GET /threads/{id}/state` 可观累积历史。

## 7. 第二阶段：Docker（`build` / `up`）

`langgraph build/up` 的构建上下文是**本目录**，而依赖里的 `../projects` 是目录外路径，
Docker 不会带进镜像，所以构建前要先把源码同步进来（副本是生成物，已 gitignore）：

```bash
bash scripts/prepare_build.sh        # rsync ../projects → build-src/projects + 生成 langgraph.build.json
bash scripts/build.sh                # 只有镜像
bash scripts/up.sh                   # 全栈：api + postgres + redis
```

> 状态：`prepare_build.sh` / `build.sh` / `up.sh` **尚未实测**（本轮按你的选择先跑通 dev + Studio）。
> 首次使用请预留时间（基础镜像 + RDKit/Vina 依赖，镜像数 GB）。
> 另外 Docker 路径下 `paths.project_root()` 会指向镜像内的源码副本，需要在镜像里设
> `DOCKING_WORKSPACE=/<源码副本路径>`（`prepare_build.sh` 会把源码放到构建上下文的
> `build-src/projects`），这一点要在第一次 build 时一并验证。

## 8. 排错

| 现象 | 处理 |
| --- | --- |
| `langgraph: command not found` | 用 `bash scripts/dev.sh`（内部用 `.venv/bin/langgraph`），或先 `install.sh` |
| 图加载报 `ModuleNotFoundError` | `bash scripts/install.sh` 重新安装（editable 装的是 `../projects`） |
| Studio 显示 "Failed to connect" | 确认 `dev.sh` 在跑、`baseUrl` 端口一致；Studio 需要浏览器能访问 `127.0.0.1:2024` |
| 图里报「未配置 API Key」 | 检查部署目录 `.env` 的 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` |
| 对接报找不到受体结构 | `python scripts/verify_graphs.py` 会打印工作区与 `assets/receptors/registry` 内容 |
| 产物去哪了 | 图输出里的 `run_dir`；默认落在 `../projects/var/runs/<run_id>/`（与网页端同目录） |
| Studio 报 `Failed to initialize Studio` | 90% 是浏览器 Local Network Access 权限被拒：地址栏左侧/页面提示里点「允许访问本地网络」后刷新；或改用 `--tunnel` |
| `langgraph dev` 打印 `Port ... already in use, using port NNNNN` | 旧实例没停干净。`dev.sh` 现在会**预检端口并直接报错**（不让端口漂移）；确认后 `pkill -f '[l]anggraph dev'` 或换 `PORT=` |
| 图里报 `BlockingError: Blocking call to os.mkdir` | 说明有人在事件循环里做了同步 I/O。本部署已把建 run / 写盘 / matplotlib 首次导入都移出事件循环（`to_thread` / 模块顶层导入）；若你自己加了节点，请照此处理，或临时用 `langgraph dev --allow-blocking` |
