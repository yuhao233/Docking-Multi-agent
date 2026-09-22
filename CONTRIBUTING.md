# 贡献指南（CONTRIBUTING）

本仓库是一个**单机运行的分子对接多 Agent 系统**。改动前请先读
[`projects/docs/architecture.md`](projects/docs/architecture.md)（尤其 §2 架构、§5 不变量、§10 开发规范），
它规定了本项目的硬性约束；本文只讲「怎么改、改完怎么验、怎么提交」。

## 1. 环境

```bash
cd projects
bash scripts/setup.sh        # 建 .venv 并 `pip install -e .`（依赖单一事实来源是 pyproject.toml）
bash scripts/doctor.sh       # 能力自检：有什么 / 缺什么 / 缺了降级成什么
```

**只支持源码检出 + editable 安装**（`pip install -e .`）。`web/`、`config/`、`assets/`、`var/`
都是运行期读写的目录，不随 wheel 分发；非 editable 安装会在服务入口被
`paths.assert_runtime_layout(strict=True)` 当场拒绝。回归见
[`projects/tests/test_packaging.py`](projects/tests/test_packaging.py)。

## 2. 改完必须跑的门禁

```bash
cd projects
bash scripts/check.sh --static     # lint + check_web + ruff + mypy（约 1 s，提交前随手跑）
bash scripts/check.sh --fast       # 上面的静态门禁 + 离线用例（跳过需要引擎的 engine 组，约 20 s）
bash scripts/check.sh              # 全量：含真实 Vina / P2Rank / pdb2pqr（约 6 min，改了计算层必须跑）
```

前端与部署侧（需要先起服务）：

```bash
node scripts/ui_e2e.js http://127.0.0.1:5001            # DOM 级交互
PLAYWRIGHT_BROWSERS_PATH=$PWD/var/cache/ms-playwright \
  .venv/bin/python scripts/browser_check.py http://127.0.0.1:5001
cd ../langgraph-deploy && bash scripts/check.sh && bash scripts/test.sh
```

CI（`.github/workflows/gate.yml`）跑的是 `check.sh --fast` 等价的离线组合；**全量引擎用例在本机跑**。

## 3. 硬性约定

- **新增/移动测试文件**：需要本机引擎或外部二进制的用例，必须同时登记到
  `projects/tests/conftest.py` 的 `ENGINE_TEST_FILES`（有防锈用例看护），否则 CI 会误跑/漏跑。
- **测试函数要有返回类型标注**（`-> None`）：lint 门禁对手写 AST 检查「公共函数缺返回类型」，
  测试里的 `def test_x():` 会被判为新增违规。
- **新增环境变量**：必须在 `projects/.env.example` 登记（`test_docs_consistency.py` 会红）。
- **报告版式改动**：用 `REGEN_REPORT_GOLDEN=1 .venv/bin/python -m pytest -q tests/test_report_golden.py`
  重出快照并**人工 review diff**；报告是逐字节快照兜底的。
- **改 `docs/技术文档.md` / `projects/docs/技术报告.md`**：必须重跑对应生成器
  （`scripts/build_tech_doc_pdf.py` / `scripts/build_report_docx.py`）刷新 PDF/Word；
  `doc_stamp` 的哈希 sidecar 会让「源改了产物没重出」在测试里变红。
- **分层依赖**：`core/` 不得依赖 `runs/reporting/tools/agents/api`；`runtime/` 不得依赖 `agents/tools/api`；
  `tools/` 不得依赖 `agents/`；模块级导入不得成环（见 `tests/test_dependency_layering.py`）。
  需要破环时用函数内延迟导入，并在行内写 `# noqa: PLC0415  # 原因`。
- **安全取向**：服务**没有鉴权**，默认只监听 `127.0.0.1`；任何新增写接口都必须经过
  `api/app.py` 的同源中间件（`_MUTATING_METHODS` + `_same_origin`），不得新增内联脚本/内联事件处理器
  （CSP 为 `script-src 'self'`）。详见 [`SECURITY.md`](SECURITY.md)。

## 4. 提交

- 一个提交只做一件事；先写能复现问题的失败用例，再修（架构手册 §10.3）。
- 提交信息写清**为什么**（真实缺陷/审计项编号），不要只写「fix」。
- 功能/修复记录追加到 [`projects/CHANGELOG.md`](projects/CHANGELOG.md)，
  **不要**再写回 README（README §14 只留指针）。
- 文档之间不重复叙述同一件事：分工见 [`README.md`](README.md) 的「文档分工」表。
