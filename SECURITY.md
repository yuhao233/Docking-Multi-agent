# 安全策略（SECURITY）

## 1. 设计前提

本系统是**单机本地工具**：默认只监听 `127.0.0.1`，**没有账号体系、没有鉴权**。
安全模型建立在「只有本机可信用户能访问」这一条前提上。

因此，把它暴露到不可信网络（`--host 0.0.0.0`、反向代理到公网、容器端口映射）属于
**明确不支持**的用法：任何能访问该端口的人都能改 LLM 端点/密钥、上传文件、发起运行。

已经实现的浏览器侧防护（针对「用户在本机用浏览器访问、但打开了恶意网页」这一现实威胁）：

| 防护 | 位置 | 说明 |
| --- | --- | --- |
| 同源中间件 | `projects/src/docking_agent/api/support.py`（`_MUTATING_METHODS` / `_same_origin`） | 带 `Origin`/`Sec-Fetch-Site` 且与 Host 不同源的**写请求**一律 403；缺 Origin（curl/测试/同源导航）放行 |
| 反向代理白名单 | `DOCKING_ALLOWED_ORIGINS`（逗号分隔） | 代理换 Host 时显式声明允许的来源 |
| CSP 与安全响应头 | 同一中间件 | `script-src 'self'`（前端无内联脚本）、`nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy` |
| 路径白名单 | `runs.safe_run_component` / `runs.ensure_inside` | 外部传入的 run/线程 id 与上传路径必须落在工作区允许目录内（防路径穿越与任意文件读取） |
| 上传校验范围 | `POST /api/uploads/inspect` | 只允许校验 `assets/uploads` 之下的文件 |
| 密钥脱敏 | `_mask_secret` / `_redact` | 界面只回显密钥首尾 4 位；上游错误体中的 `sk-*`/Bearer/`api_key=` 等一律打码 |
| URL 方案白名单 | `projects/web/app.js`（`safeUrl`） | 报告中的链接/图片只允许 `http(s)`/站内路径，非法方案降级为纯文本 |
| 生成物溯源 | `scripts/doc_stamp.py` | PDF/Word 与 Markdown 源的内容哈希 sidecar，防止提交陈旧产物 |

**已知限制**（部署前必须知道）：

- 无速率限制、无审计日志防篡改；LLM 连通性测试会真实消耗额度。
- 上传的受体/分子库与运行产物会落盘到 `projects/var/runs/`、`assets/uploads/`，属于**明文**；
  共享机器上请注意目录权限，并用 `scripts/prune_runs.py` 做保留策略。
- `assets/cache/` 可能包含从公网下载的结构文件。

## 2. 支持的版本

安全修复只提供给最新的 minor 版本（当前 `0.6.x`，见 `projects/pyproject.toml` 的 `version`）。

## 3. 报告漏洞

- 请用 GitHub 的 **Security Advisories**（仓库页面 → Security → Report a vulnerability）私下报告；
  也欢迎直接开 issue 描述**非敏感**问题。
- 不要在公开 issue 里粘贴 API Key、`.env` 内容或可直接利用的完整 PoC。
- 请在报告里给出：受影响版本、复现步骤、影响面（例如「任意文件读取」「XSS」）、以及你建议的修复方向。
- 维护者会在确认后修复并在 `projects/CHANGELOG.md` 记录；如你希望署名请一并说明。

## 4. 加固清单（自建部署）

```bash
# 1) 只监听本机（默认；不要改成 0.0.0.0）
grep -n '^HOST=' projects/.env          # 期望 127.0.0.1
# 2) 必须经反向代理暴露时，显式声明来源
echo 'DOCKING_ALLOWED_ORIGINS=https://your.example' >> projects/.env
# 3) 定期清理历史运行（含上传结构与产物）
.venv/bin/python projects/scripts/prune_runs.py            # 先看
.venv/bin/python projects/scripts/prune_runs.py --apply    # 再删
# 4) 检查密钥是否只存在于被 gitignore 的文件里
git check-ignore -v projects/.env projects/config/local_settings.json
```

相关回归用例：`projects/tests/test_security_boundary.py`、`test_packaging.py`、
`test_docs_consistency.py`；浏览器侧见 `projects/scripts/browser_check.py` 与 `ui_e2e.js`。
