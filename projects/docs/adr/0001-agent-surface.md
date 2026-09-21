# ADR-0001：Agent 面的定位与边界（产品面 / 平台面）

- 状态：**已采纳（路线 A）**
- 日期：2026-09-21
- 决策者：项目作者
- 影响范围：`src/docking_agent/api/`、`web/`、`langgraph-deploy/`、`docs/api.md`、README

## 背景

系统里同时存在 **三处**与"Agent 协议"有关的东西，此前没有文字规定各自是什么、谁为准：

| # | 位置 | 是什么 | 现状使用者 |
| --- | --- | --- | --- |
| ① | `src/docking_agent/api/app.py`（产品 REST，`/api/*`） | 网页真正依赖的业务接口：产物下载、报告/PDF、上传、设置、历史、图表、分子结构图 | 网页（全部非运行类请求）、外部脚本 |
| ② | `src/docking_agent/api/agent_service.py`（手写 Agent Protocol 子集，18 个 op） | **我们自己写的**平台风格兼容层：`/threads`、`/threads/{id}/runs/stream\|wait`、`/assistants/*`、`/ok`、`/info` 等，把同一张图按平台帧重新包装 | **网页的运行路径**（阶段 2 已从旧端点切过来） |
| ③ | `langgraph-deploy/`（`langgraph.json` + 7 张图） | **真正的 LangGraph Platform**：`langgraph-cli dev` 起的 Agent Server（含 Studio、SDK、未来的 durable runs） | Studio 调试、外部 SDK 接入 |

问题：没有文档能回答"外部集成应该调哪一个""新增一个 Agent 能力该加在哪一层"。结果是
两套面都在长，且协议形状（SSE 帧映射、run_id 双语义、取消、幂等）**存在两处各写一份**的风险。

同时确认过两个约束（见 `docs/architecture.md §1.2` 非目标）：
**单机多进程把 32 核吃满、不做分布式、不做多用户 SaaS**。因此"把主链路押在平台运行时上"
与项目定位冲突——LangGraph 本地运行时是同进程的，而对接要起进程池、跑分钟~小时级、产物 GB 级。

## 决策（路线 A）

1. **① + ② 合并称为「产品面（Product API）」**：
   - 对外名称：本地 Agent API（**Agent Protocol 兼容子集**，不是平台官方实现）；
   - 服务对象：本机网页、本机脚本、同机集成方；
   - 承载形态：单进程 FastAPI（`bash start.sh` 起的那个端口）。
2. **③ 称为「平台面（Platform API）」**：
   - 对外名称：LangGraph Platform（Studio / SDK / 未来 durable 能力）；
   - 服务对象：需要 Studio 可视化调试、LangGraph SDK、或未来计划化/重试能力的场景；
   - 承载形态：`langgraph-deploy/`（独立 venv、独立端口、复用同一份 `src/docking_agent`）。
3. **能力只加一处**（硬规则）：
   | 能力类型 | 唯一落点 |
   | --- | --- |
   | Agent 编排（图、工具、提示词、编排策略） | `src/docking_agent/agents/`、`tools/`（**两个面共用**，禁止复制） |
   | 产品形态能力（产物、报告/PDF、上传、设置、历史、图表） | 产品面 ① |
   | 协议形状（SSE 帧映射、run_id 双语义、取消、幂等） | 共享模块（`agents/protocol.py`），②与③都从这里取 |
   | 平台特性（Studio、SDK、cron/重试/队列） | 平台面 ③ |
4. **② 冻结为新能力的禁区**：不再往 `agent_service.py` 增加端点；需要新能力时，先判定它属于
   Agent 编排（→ 图/工具，两面共享）还是产品形态（→ ①）。若确实需要新的协议端点，必须
   先改本 ADR。
5. **平台面不复制业务代码**：`langgraph-deploy/docking_graphs/graphs.py` 继续
   `from docking_agent... import ...` 复用项目源码；构建期只做两件平台必须的适配
   （把 checkpointer 交给平台托管、把 Agent 包成单节点壳以保住 ContextVar 传播）。

## 后果

**好处**

- "调哪个面"有唯一答案：本机产品集成 → 产品面；Studio/SDK/计划化 → 平台面。
- 协议形状只有一份实现，帧映射与 run_id 语义不会两边漂移。
- 与"单机重算力 + 重产物"的定位一致：网页与产物服务不需要额外进程，平台面按需启动。
- 迁移成本可增量：未来若要多租户/durable，只需把算力外置成服务，平台面已经就位。

**代价 / 已知限制**

- 产品面的协议兼容性由我们自己维护（不是平台官方实现），新增协议能力要人工补齐；
- 平台面的 Docker 构建路径**至今未验证**（`prepare_build.sh`/`build.sh`），且 `langgraph-api` 仍是
  0.14.x 早期版本——这也是不让主链路依赖它的理由之一；
- 两个面各自有独立 venv 与端口，用户需要知道"哪个是哪个"（README 与 `doctor.sh` 会说明）。

## 落实与守卫

| 动作 | 位置 | 守卫 |
| --- | --- | --- |
| 文档写明两个面的定位与唯一契约 | 本文件 + `docs/architecture.md §1.4` 摘要 + `docs/api.md` 顶部说明 | `tests/test_ootb.py::test_adr_documents_surfaces` |
| 冻结 ② 的端点集合 | `docs/api.md` 的端点表 | 端点集合快照测试（P1） |
| 协议形状抽成共享模块 | `src/docking_agent/agents/protocol.py`（P1） | 帧类型快照（P1） |
| 平台图与项目工具/提示词不漂移 | `langgraph-deploy/tests/` | `tests/test_core.py::test_coordinator_tool_list_matches_config`（已有） |

## 备选方案（未采纳）

- **B：平台面为唯一对外 Agent API**。需要网页改调 `:2024` 或本地做代理，引入第二进程与
  平台版本耦合，离线交付变重；与"单机本地产品"定位不符。若将来明确要做多租户/外部集成，
  再回到这条路线（本 ADR 记录它作为演进路径）。
- **C：删掉平台面**。实现最简单，但失去 Studio 调试与未来 durable 能力，且已投入的
  7 张图与回归测试作废。
