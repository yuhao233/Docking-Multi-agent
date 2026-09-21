# Docking-Multi-agent

一个多 Agent 协同的**分子对接工作流**：从自然语言指令或分子/受体文件出发，全自动完成
分子库导入 → 物化性质评估 → 结合口袋定盒 → 真实对接打分 → 结合模式分析 → 推荐排行 → 专家报告，
所有数值都来自真实计算（AutoDock Vina / RDKit / P2Rank），可逐条追溯到工具输出与运行产物。

- **对接引擎**：AutoDock Vina（主）/ AutoDock4（备用，可选）
- **性质与指纹**：RDKit；**配体/受体准备**：Meeko；**口袋预测**：P2Rank（可选，缺省退化为内置几何法）
- **Agent 编排**：LangChain `create_agent`（LangGraph）——「整体协调 Agent」统筹 4 个子 Agent
  （口袋分析 / 分子属性评估 / Docking 执行 / 结合模式检测）
- **两种使用方式**：网页对话 或 参数表单（确定性流水线，不需要 LLM）
- 也可作为 **LangGraph Platform** 项目运行（Studio / LangGraph SDK，见 `langgraph-deploy/`）

## 目录导航

| 路径 | 说明 |
| --- | --- |
| [`projects/README.md`](projects/README.md) | ★ **主文档**：快速开始 / 界面与能力 / 配置项 / 运行记录 / 改造记录（58 条） |
| [`projects/docs/architecture.md`](projects/docs/architecture.md) | 架构与设计决策（含非目标与硬约束） |
| [`projects/docs/adr/`](projects/docs/adr/) | 架构决策记录（`0001`：产品面 / 平台面定位） |
| [`projects/docs/api.md`](projects/docs/api.md) | HTTP API 契约（产品面） |
| [`projects/web/`](projects/web/) | 前端：原生 JS + CSS，无构建步骤、无 CDN（离线可用） |
| [`projects/src/docking_agent/`](projects/src/docking_agent/) | 后端源码（`core/` 真实计算核心、`agents/` 多 Agent 编排） |
| [`langgraph-deploy/`](langgraph-deploy/) | LangGraph Platform 侧的 7 张图与部署/自检脚本 |

## 快速开始

```bash
cd projects
bash scripts/doctor.sh     # ① 环境能力自检：有什么、缺什么、缺了会降级成什么
bash start.sh              # ② 装依赖（首次）→ 启动服务 → 打开 http://127.0.0.1:5000
```

多 Agent 模式需要 LLM：`cp .env.example .env` 后填 `LLM_API_KEY` / `LLM_BASE_URL`
（任意 OpenAI 兼容端点）；**不配 LLM 也能用** —— 参数模式的确定性流水线照常工作。

可选组件缺失时不会静默失败，而是**降级并在报告里如实说明**：

| 组件 | 缺失后的行为 |
| --- | --- |
| Java + P2Rank | 口袋分析改用内置几何法 |
| pdb2pqr | 受体不按目标 pH 重算质子化态（报告告警） |
| AutoDock4 / autogrid4 | 仅 Vina 可用 |
| 中文字体（Noto CJK 等） | 图表与 PDF 中的中文可能显示为方块 |
| LLM | 仅确定性流水线可用 |

## 质量门禁

`pytest`（590+ 项，含离线端到端）· `lint_local`（静态规范）· `check_web`（前端静态校验）·
`ui_e2e`（jsdom DOM 级）· `browser_check`（真实 Chromium：标准协议 / 报告版式 / 布局）·
部署层 `check.sh` + `smoke.py --with-coordinator`。

## 仓库不含什么

为保证可复现与安全，**不入库**（见 `.gitignore`）：运行历史 `var/`、受体/配体缓存、
用户上传、外部工具（P2Rank）、虚拟环境、`.env` 与本地设置、平台机器状态。
交付打包用 `projects/scripts/pack.sh`（默认约 5 MB，`--list` 可预演内容与体积）。
