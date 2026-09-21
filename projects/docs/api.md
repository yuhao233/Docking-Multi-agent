# 本地 HTTP API 契约（v0.6.0）

本文件是**前端与后端的接口契约**。任何一方改动都必须同步更新本文件。
服务默认监听 `http://127.0.0.1:5000`，无鉴权（本地单机使用）。

约定：
- 除特别标注外，请求与响应均为 `application/json; charset=utf-8`。
- 时间戳为 ISO 8601 本地时间字符串，如 `2026-09-12T16:37:12`。
- 所有 `run_id` 形如 `20260912-163712-3404`。
- 错误统一为 `{"error_code": "...", "error_message": "...", "stack_trace": "...", "where": {...}}`。

---

## 1. 状态与配置

### `GET /api/health`
```json
{
  "status": "ok",
  "version": "0.6.0",
  "llm_configured": true,
  "model": "deepseek-flash",
  "base_url": "https://api.deepseek.com",
  "engine_available": {"vina": true, "autodock": false},
  "roles": {
    "intake":      {"model": "deepseek-flash", "temperature": 0.0, "...": "..."},
    "coordinator": {"model": "deepseek-flash", "temperature": 0.2, "top_p": 0.9,
                    "base_url": "https://api.deepseek.com", "timeout": 600.0,
                    "thinking": "disabled", "max_tokens": null},
    "property":    {"model": "deepseek-flash", "temperature": 0.0, "...": "..."},
    "pocket":      {"model": "deepseek-flash", "temperature": 0.0, "...": "..."},
    "docking":     {"model": "deepseek-v4-pro", "temperature": 0.1, "...": "..."},
    "binding":     {"model": "deepseek-flash", "temperature": 0.0, "...": "..."}
  }
}
```

`roles` 为**每个 Agent 角色最终生效**的模型配置（不构建实例），共 6 个角色：
`intake`（任务受理）/ `coordinator`（整体协调/主管）/ `property`（分子属性评估）/
`pocket`（口袋分析）/ `docking`（Docking 执行）/ `binding`（结合模式检测）。
各角色持有独立 LLM 实例，可用环境变量 `LLM_<字段>_<角色>` 或
`config/agent_llm_config.json` 的 `roles` 段分别指定模型（见 §11）。

### `GET /api/receptors` —— 受体与「已知结合位点」
```json
{
  "default": "thrombin",
  "aliases": {"1dwc": "thrombin", "1ptu": "trypsin"},
  "receptors": [
    {
      "key": "thrombin",
      "name": "thrombin",
      "pdb": "1DWC",
      "protein": "人α-凝血酶 (human alpha-thrombin, PDB 1DWC)",
      "pdbqt": "assets/receptors/registry/thrombin_1DWC.pdbqt",
      "available": true,
      "site": {
        "center": [31.5, 13.74, 24.36],
        "size": [22.0, 22.0, 22.0],
        "source": "共晶配体质心",
        "description": "凝血酶 S1 口袋 / 催化位点",
        "residues": ["HIS57", "ASP102", "SER195"]
      }
    }
  ]
}
```

### `GET /api/libraries` —— 示例分子库与默认阳性对照
```json
{
  "libraries": [
    {"id": "example", "name": "示例分子库", "path": "assets/libraries/mol_library.csv",
     "count": 8, "molecules": [{"name": "benzamidine", "smiles": "NC(=N)c1ccccc1"}]}
  ],
  "positive_control": {"name": "benzamidine", "smiles": "NC(=N)c1ccccc1",
                       "source": "assets/libraries/positive_control.csv"}
}
```

---

## 2. 执行（SSE 流式）

SSE 统一报文：`event: message\ndata: {JSON}\n\n`；最后一条一定是 `type=done` 或 `type=error`。

### `GET /api/runs` —— 历史运行列表 / 检索

不带检索参数时返回最近若干条（`{"runs":[...]}`，与旧版一致）：

```bash
curl -s "localhost:5000/api/runs?limit=20"
```

带任一检索参数时返回分页结构，用于"关掉页面后仍能找回之前的运行"：

```bash
curl -s "localhost:5000/api/runs?q=阿司匹林&status=ok&since=2026-09-01&offset=0&limit=20"
```

```json
{ "runs": [{"run_id": "20260921-095530-0405", "created_at": "2026-09-21T09:55:30",
            "status": "ok", "kind": "agent", "receptor": "thrombin(1DWC)",
            "molecule_count": 2}],
  "total": 39, "offset": 0, "limit": 20,
  "query": {"q": "阿司匹林", "status": "ok", "kind": "", "receptor": "", "since": "2026-09-01", "until": ""} }
```

| 参数 | 说明 |
| --- | --- |
| `q` | 关键词，匹配 run_id、受体、状态、任务描述与**排序表里的分子名/ID**；空格分隔多个词表示 AND |
| `status` | `ok` / `no_op` / `error` |
| `kind` | `agent` / `studio`（历史记录里可能有旧值） |
| `receptor` | 受体名模糊匹配 |
| `since` / `until` | `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`；只给日期时含当天 |
| `offset` / `limit` | 分页（`limit` 上限 200） |

索引在进程内惰性建立（读 `run.json` 与排序表前 64 KB），3.4k 条运行首次约 0.4 s，之后毫秒级。

### 历史端点（已删除）

> `POST /api/pipeline/stream` 与 `POST /pipeline` 承载的"不经过 Agent 的确定性流水线"已下线。
> 对接一律走标准 Agent Protocol（`POST /threads/{tid}/runs/stream`，`assistant_id=coordinator`）。
请求体：
```json
{
  "receptor": "thrombin",
  "site_center": [31.5, 13.74, 24.36],
  "site_size": [22.0, 22.0, 22.0],
  "ligands_text": "阿司匹林:CC(=O)Oc1ccccc1C(=O)O",
  "molecule_file": "",
  "allow_example_fallback": true,
  "positive_control": "",
  "exhaustiveness": null,
  "n_poses": null,
  "engine": "vina",
  "save_poses": true,
  "max_ligands": 0
}
```
字段说明：`site_center`/`site_size` 为空则用该受体注册位点；`molecule_file` 为服务端可读路径或 URL；
`max_ligands=0` 表示不限制；`engine` ∈ `auto|vina|autodock`。
`exhaustiveness` / `n_poses` 为 `null`（或省略）时按任务/库柔性/盒体积/预算**自动规划**，
并在报告「参数自动规划」一节给出理由链；给出数值即用户参数，规划不改写（见 §7.6）。

事件序列：
```json
{"type":"start","run_id":"20260912-163712-3404","request":{...}}
{"type":"stage","stage":"import","message":"解析分子库","index":0,"total":8}
{"type":"molecule","index":0,"total":8,"name":"nafamostat","smiles":"...","affinity_kcal_mol":-9.41,
 "engine":"vina","exhaustiveness":6,"pose_url":"/api/runs/<id>/artifacts/pose_nafamostat",
 "properties":{"molecular_weight":347.38,"logP":2.65,"tpsa":138.07,"hbd":5,"hba":4,
               "rotatable_bonds":4,"aromatic_rings":3,"formula":"C19H17N5O2",
               "lipinski_violations":0,"drug_likeness_pass":true},
 "similarity_to_positive_control":0.262}
{"type":"stage","stage":"report","message":"生成报告产物","index":8,"total":8}
{"type":"done","run_id":"20260912-163712-3404","summary":{ ...同 /api/runs/{id} 的 run 对象... }}
```
失败：`{"type":"error","error_code":"DOCKING_FAILED","error_message":"...","stack_trace":"..."}`

### `POST /api/agent/stream` —— 多 Agent 协作（需要 LLM）

请求体：
```json
{"mode":"chat|manual",
 "message":"请用示例分子库完成筛选并给出排序结论",
 "advanced": false,
 "conversation_id":"同一段对话的稳定 id（留空 = 每次独立）",
 "receptor":"thrombin","ligands_text":"","molecule_file":"",
 "positive_control":"","exhaustiveness":6,"engine":"vina",
 "site_center":null,"site_size":null}

// 对话模式（chat）下只有**被改动过**的运行参数才出现在请求体里；
// 什么都没改时（advanced=false）上述 receptor / positive_control / exhaustiveness /
// engine / site_center / site_size **一个都不会出现**，服务端按系统默认与自动规划执行。
```

**`mode` 决定「自然语言指令」与「表单参数」谁是权威，这是避免二者冲突的关键：**

| mode | 语义 | 服务端行为 |
| --- | --- | --- |
| `manual`（默认） | 表单参数是本次任务的**权威参数**；`message` 只作为目标描述/额外要求 | 始终在指令末尾附带「本次运行参数（权威）」块，并声明：若与描述冲突以参数块为准 |
| `chat` + `advanced=false` | 纯对话：完全按自然语言执行，**没有任何运行参数字段下发**，缺省用**系统默认参数** | 仍附带「默认运行参数（系统默认；指令已指定则以指令为准）」块，块内渲染成「自动（按库柔性/盒体积规划，基准 16）」「未指定坐标」「未提供」 |
| `chat` + `advanced=true` | 对话为主，**用户改动过的**运行参数作为**默认值** | 附带「默认参数（仅当指令未指定时生效；指令已指定则以指令为准）」块 |

前端建议（v0.26）：`advanced` 由**逐字段**判定 —— 只有真正触发过 `input`/`change` 的字段
（`state.touchedFields`：引擎 / 质子化 / 目标 pH / n_poses / exhaustiveness / 阳性对照 /
口袋引擎 / 最大分子数 / 保存位姿 / 位点盒；或点过一键预设、「填入上传位点」）才会进入请求体，
只要有一个这样的字段就把 `advanced` 置为 `true`。

> **「打开过高级设置」不算改动**：早期实现用「展开过折叠」当成"用户改过参数"，于是"打开又关掉"
> 就把所有默认值塞进请求体（用户反馈：「我没有指定高级参数啊」）。现在展开/收起不产生任何字段，
> `exhaustiveness` 默认勾着「自动」（滑块禁用、标签显示**自动**）：不下发数值，服务端按库柔性 /
> 盒体积规划（基准 16）；取消「自动」才作为显式值下发（`param_plan.source=user`）。
> 阳性对照留空也不再无条件下发 `skip_positive_control`（留空本来就是"不做对照分析"）。

> **附件例外**：`advanced=false` 表示「不注入**表单参数**」，但 `receptor_file` /
> `molecule_file` 是**附件派生字段**（用户上传的文件 = 明确意图），前端在任何情况下都会带上，
> 有值才带。真实缺陷：早期实现用 `if (payload.advanced) Object.assign(body, payload.params)`，
> 折叠高级设置时连 `receptor_file` 一起丢掉 → 服务端收不到上传受体 → 回退默认凝血酶。
事件序列：
```json
{"type":"start","run_id":"..."}
{"type":"token","content":"增量文本"}
{"type":"tool_call","name":"run_docking"}
{"type":"tool_result","name":"run_docking","content":"（截断后的工具返回）"}
{"type":"update","node":"model"}
{"type":"choices","choices":[{"id","kind","label","value","prompt","detail"}],"note":"..."}
{"type":"final","content":"完整报告（Markdown）"}
{"type":"done","run_id":"...","summary":{...}}
{"type":"error","error_code":"...","error_message":"..."}
```

### 多轮会话（`conversation_id`）—— 追问 → 回答要延续上一轮

`AgentRequest` 新增可选字段 `conversation_id`（默认 `""`）：

- **同一 `conversation_id` = 同一段对话**：服务端把它当作 LangGraph 的 `thread_id`，
  checkpointer 因此能命中上一轮的消息历史；受理层还会从历史里**确定性继承**上一轮已给出的
  分子（`core.ligands.extract_smiles`）与受体（`_mentioned_receptor`）。于是
  「系统问 → 用户答」不会再另起一段对话，也不会把同一个问题原样再问一遍。
- **留空 = 每次独立**：`thread_id` 回落到本次运行的 `run_id`，与修复前行为完全一致（向后兼容）。
- 可观测：`GET /api/runs/{id}` 的 `run` 对象带上 `conversation_id`（调用方传入值）与
  `thread_id`（实际使用的 thread，留空时等于 `run_id`）；SSE `start` 事件同样带这两个字段。
  多轮继承的结果会写进 `task_spec.prior_turn_count` 与 `assumptions`（例如
  「候选分子继承自上一轮对话」「受体继承自上一轮对话 → trypsin」）。

**持久化边界（重要）**：这段记忆在 LangGraph checkpointer 里。默认 `CHECKPOINT_BACKEND=memory`
时它是**进程内内存**——**服务一重启，多轮上下文即失效**（页面上还留着 id，但服务端已无历史，
下一轮相当于新对话）；要跨重启保留对话，设 `CHECKPOINT_BACKEND=sqlite`（写入
`var/checkpoints.sqlite`，需安装 `langgraph-checkpoint-sqlite`）。这与运行记录无关：
`var/runs/<run_id>/` 始终落盘，重启后仍能在「历史运行」里查看。

两轮 curl 示例（第二轮是回答第一轮的追问，服务端会带着第一轮历史受理）：

```bash
BASE=http://127.0.0.1:5000
CONV=$(python -c "import uuid;print(uuid.uuid4())")

# 第一轮：受理层可能只提问（decision=ask，不调用任何工具）
curl -N -s "$BASE/api/agent/stream" -H 'Content-Type: application/json' \
  -d "{\"mode\":\"chat\",\"message\":\"帮我看看那个化合物\",\"conversation_id\":\"$CONV\"}"

# 第二轮：同一个 conversation_id → 命中上一轮 thread；本轮只是回答，不再重复追问
curl -N -s "$BASE/api/agent/stream" -H 'Content-Type: application/json' \
  -d "{\"mode\":\"chat\",\"message\":\"用 trypsin\",\"conversation_id\":\"$CONV\"}"

# 排查：两次运行的 conversation_id / thread_id 应一致
curl -s "$BASE/api/runs/<run_id>" | python -m json.tool | grep -E 'conversation_id|thread_id'
```

> 兼容接口 `/run`、`/v1/chat/completions` 也识别请求体里的 `conversation_id`（缺省同样回落 `run_id`）；
> 该字段在标准 Agent Protocol 请求里表示会话线程；线程级端点本身就是多轮记忆的载体。

---

## 3. 运行记录与中间数据

### `GET /api/runs?limit=20`
```json
{"runs":[{
  "run_id":"20260912-163712-3404","kind":"pipeline","status":"ok",
  "created_at":"2026-09-12T16:37:12","finished_at":"2026-09-12T16:37:40",
  "receptor":"thrombin","receptor_label":"thrombin(1DWC)",
  "site":{"center":[31.5,13.74,24.36],"size":[22.0,22.0,22.0]},
  "engine":"vina","exhaustiveness":6,"molecule_count":8,
  "top":[{"name":"nafamostat","affinity_kcal_mol":-9.41}],
  "artifact_count":12,"has_report":false,"error":null
}]}
```

### `GET /api/runs/{run_id}`
```json
{
  "run": { ...列表中的 run 对象 + "request":{...}, "notes":["..."], "duration_sec":28.4,
           "agent_models": { "docking": {"model":"deepseek-v4-pro","actual_model":"deepseek-v4-pro",
                                         "temperature":0.1,"calls":2,"instance_id":132917840948080} } },
  "result": {
    "properties": [{"name":"...","smiles":"...","molecular_weight":...}],
    "docking": {"status":"ok","receptors":[{"receptor_key":"thrombin","results":[...]}]},
    "binding": {"positive_control":"NC(=N)c1ccccc1","rows":[{"name":"...","similarity_to_positive_control":0.262}]},
    "ranking": [{"rank":1,"name":"nafamostat","smiles":"...","affinity_kcal_mol":-9.41,
                 "engine":"vina","exhaustiveness":6,"box_group":"main",
                 "box_size":[22.0,22.0,22.0],"molecular_weight":347.38,"logP":2.65,
                 "similarity_to_positive_control":0.262,"drug_likeness_pass":true}],
    "positive_control": {"name":"benzamidine","smiles":"NC(=N)c1ccccc1","affinity_kcal_mol":-5.78},
    "pockets": [{"rank":1,"name":"pocket1","score":10.58,"probability":0.569,
                 "center":[32.8846,11.8534,19.3657],"extent":[12.7,13.8,14.4],
                 "residues":["H_191","H_195","H_215"]}],
    "pocket_analysis": {"engine":"p2rank","source":"p2rank 预测口袋 pocket1，score=10.58",
                        "validation":{"status":"no_reference"},"warnings":[]}
  },
  "artifacts": [
    {"name":"ranking_csv","label":"排序结果 CSV","path":"ranking.csv","size":967,
     "content_type":"text/csv; charset=utf-8","download_url":"/api/runs/<id>/artifacts/ranking_csv"},
    {"name":"docking_chart","label":"对接亲和力对比图","path":"charts/docking_chart.png","size":53629,
     "content_type":"image/png","download_url":"/api/runs/<id>/artifacts/docking_chart","inline_url":"/api/runs/<id>/artifacts/docking_chart?inline=1"}
  ],
  "report_markdown": "## 筛选报告\n...",
  "downloads": {
    "report_pdf": "dock_20260912-163712-3404_thrombin_8mols_report.pdf",
    "report_md": "dock_20260912-163712-3404_thrombin_8mols_report.md",
    "ranking_csv": "dock_20260912-163712-3404_thrombin_8mols_ranking.csv",
    "data_zip": "dock_20260912-163712-3404_thrombin_8mols_data.zip",
    "poses_zip": "dock_20260912-163712-3404_thrombin_8mols_poses.zip",
    "prefix": "dock_20260912-163712-3404_thrombin_8mols"
  },
  "log": ["[16:37:12] 解析分子库", "[16:37:14] 对接 nafamostat ..."]
}
```

> `downloads` 是前端下载按钮的**唯一事实源**：所有值都由 `runs.download_name()` 产出，
> 前端不要再自己拼文件名，避免两处规则漂移。键始终返回；某类产物不存在时值仍是规范名，
> 由前端按 `artifacts` 是否存在决定按钮可用性。

### 其他
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/runs/{run_id}/artifacts/{name}` | 下载单个产物；`?inline=1` 用于图片内联显示 |
| GET | `/api/runs/{run_id}/report.pdf` | 下载 PDF 版报告（已有 `report_pdf` 产物则直接返回，缺失时现场生成） |
| GET | `/api/runs/{run_id}/download.zip` | 打包下载该次运行的全部中间数据 |
| GET | `/api/runs/{run_id}/poses.zip` | 仅打包全部位姿文件（无位姿时 404） |

### 3.0 整包里包含哪些结构文件

`download.zip` 打包的是**运行目录的全部内容**，其中与结构有关的文件分两处：

| 路径 | 内容 | 说明 |
| --- | --- | --- |
| `poses/pose_*.pdbqt` | 对接得到的**配体**位姿 | 每个分子一个（`save_poses=true` 时） |
| `receptor/<受体>.pdbqt` | **受体**结构：本次对接实际使用的 AutoDock 输入 | 权威文件，换台机器也能复现打分 |
| `receptor/<受体>_prepared.pdb` | 受体的准备后蛋白（去水 / 去杂原子，必要时 mmCIF→PDB） | 没有该中间产物时不生成 |
| `receptor/<受体>_source.<ext>` | 受体的**原始结构**：用户上传的原文件 / URL 下载的原文件 | 注册表预置受体只有 PDBQT，故通常没有这一类 |

命名规则：`<受体>` 取 `receptor_key`（注册表 key、上传文件名 slug 或受体名），多受体时每个受体一组文件。
三者都会登记成产物（`receptor_pdbqt_*` / `receptor_prepared_*` / `receptor_source_*`），
因此会出现在「中间数据」页签、报告第 9 节产物清单与整包 ZIP 里。
**复制是尽力而为**：文件取不到只记 warning，不影响运行与报告。

| GET | `/api/molecule/depict?smiles=...&width=320&height=240` | RDKit 二维结构图（PNG） |
| GET | `/api/molecule/properties?smiles=...` | 单分子理化性质（JSON） |

### 3.1 下载文件命名规范

所有下载接口统一用 `Content-Disposition: attachment; filename="..."` 返回下述 ASCII 安全名
（物理落盘路径不变，只是对外下载名规范化）：

```
dock_{run_id}_{receptor}[_{N}mols]_{kind}.{ext}
例（分子数已知）：dock_20260914-151116-3404_thrombin_8mols_report.pdf
例（分子数未知）：dock_20260914-151116-3404_thrombin_report.pdf
```

- `receptor` 取自运行的受体标签，只保留 `[A-Za-z0-9._-]`（中文/空格/斜杠等降级为 `_`），
  截断到 24 字符，为空或不可用时用 `receptor`；
- 分子数未知时**整段省略** `_{N}mols`（不写 `namols`）；`kind.ext` 依次为 `report.pdf` /
  `report.md` / `ranking.csv` / `data.zip` / `poses.zip`。

---

## 4. 兼容接口（保留，供脚本调用）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/run` | 同步多 Agent，返回 `{messages:[...]}` |
| POST | `/stream_run` | 多 Agent SSE（旧事件格式） |
| GET | `/files/{key}` | 下载 `var/outputs/` 下的产物 |
| POST | `/cancel/{run_id}` | 取消运行 |
| GET | `/health` | 旧版健康检查 |
| GET | `/graph_parameter` | 输入输出 schema |
| POST | `/v1/chat/completions` | OpenAI 兼容 |

---

## 5. 前端约束

- 静态资源：`GET /` 返回 `web/index.html`；`GET /static/{path}` 返回 `web/` 下资源。
- **禁止外部 CDN**（离线可用）：不得引用任何外部 JS/CSS/字体。图表直接用服务端产出的 PNG。
- 原生 JS（无构建步骤），入口 `web/app.js`，样式 `web/styles.css`。
- 中文界面，配色专业（深色顶栏 + 浅色内容区），响应式（窄屏单列）。
- 关键交互：
  1. 任务配置表单（受体下拉并显示已知位点、位点可编辑、配体来源三种：文本框/文件路径/示例库、阳性对照、引擎、exhaustiveness、模式切换）。
  2. 执行区：阶段进度、实时逐分子结果表、Agent 文本流。
  3. 结果总览：可排序的排序表 + 图表 + 阳性对照对比。
  4. 分子详情：每个分子一张卡片，含 RDKit 二维结构图、理化性质表、对接能量、与阳性对照相似度、位姿下载。
  5. 报告：渲染 Markdown（标题/粗体/列表/表格/代码块）。
  6. 中间数据：产物清单 + 单项下载 + 整体 zip 下载。
  7. 历史运行：列表 + 点击载入。

---

## 6. 实现说明与变更（v0.3 冻结后补充）

1. **`molecule` 事件只推送候选分子**：阳性对照仅作为对比基准，不出现在逐分子实时流中；
   它的对接结果通过 `done.summary` 与 `GET /api/runs/{id}` 的 `result.positive_control` 提供。
2. **结构化字段随指令下发，但「默认值」与「用户意图」必须区分**：`POST /api/agent/stream`
   会把 `receptor / site_center / site_size / ligands_text / molecule_file / positive_control /
   exhaustiveness / engine` 追加到指令里，避免用户填了表单却被忽略；其中 `chat` 折叠时
   受理层**未识别到**的受体只以弱表述出现（`source="default"`，见 §7.1.1），不会被当成用户指定。
3. **`done` 事件只会出现一次**：Agent 流内部的 `start`/`done` 被外层统一接管。
4. **产物清单**除契约列出的项外，还包含 `property_chart`（理化性质空间图）与 `result`（完整结果 JSON）。
5. **报告由协调 Agent 出具**：骨架固定、数值来自工具真实计算，Agent 只写文字判断与结论。

6. **`mode` / `advanced` 字段**（见第 2 节）：彻底避免「聊天指令」与「运行参数」互相冲突。
   `manual` = 参数权威；`chat`+`advanced=false` = 纯指令，参数块用系统默认值；
   `chat`+`advanced=true` = 参数仅作默认值，指令优先。
7. **结合模式分析已修复并升级**：`binding_mode_analysis` 与 `positive_control_similarity`
   现在共用同一套真实计算（Morgan + MACCS 双指纹、SMARTS 药效团锚定基团、性质差异），
   两个工具口径一致，`structural_consistency` / `binding_mode_hint` / `pharmacophore` /
   `anchor_match` 等字段在两者中都可获得。

---

## 7. 大库承载（上千～上万分子）与完整性保证（v0.4）

### 7.1 参数语义修正

| 模式 | 语义 |
| --- | --- |
| `manual` | 表单参数**权威**；`message`（目标描述）**可留空**，服务端会补全完整任务描述并跑完整流程 |
| `chat` + `advanced=false` | 使用**系统默认参数**（受体默认位点、`exhaustiveness=16`、`n_poses=1`、`engine=vina`、`protonation=ph@7.4`；**默认不使用示例库**，仅当用户明确要求时才用），指令中已指定的以指令为准。界面上的「运行参数」条**常驻在对话框上方**（不在折叠里）：**只有改动过的字段**才按 `advanced=true` 作为默认值下发；未改动就是本行的系统默认/自动（打开过折叠又关掉不会注入任何参数） |
| `chat` + `advanced=true`（动过运行参数） | 使用**被改动过的那些参数**作为默认值，指令中已指定的以指令为准；未改动的字段仍然缺省 |

- **阳性对照为可选项**：`positive_control` **留空即跳过**对照分子对接与结合模式比较
  （`result.positive_control` 为 `{}`、`result.binding` 为空、相似度字段为 `null`，并在 `run.notes` 说明）；
  传 `skip_positive_control=true` 可显式跳过（即使提供了对照）。界面默认不预填阳性对照。

#### 7.1.1 未指定受体时：默认受体是**假设**，不是用户意图

`chat` 折叠模式下，受理层若没从指令/表单识别到受体，会把 `task_spec.receptor.source` 标为
`"default"`，渲染给主管 Agent 的指令用**弱表述**（「指令未指定；`thrombin` 只是系统默认」），
并要求调用 `run_pocket_analysis` / `run_docking` 时把 `receptor_sources` 与 `receptor_file` **留空**。

- 主管 Agent 即便习惯性显式传入默认受体名（`thrombin`/`1DWC`），`molecular_docking` 也会把它
  还原为空（`_drop_unspecified_default_receptor`），交由 `resolve_receptor_specs` 走空值回退，
  于是 `docking.notes` 里必有「未指定受体，已默认使用 凝血酶(thrombin, 1DWC)…」；
- `resolve_receptor_specs` 对 `None` / `""` / 纯空白 / 字面量 `"default"` 一律按「未指定」回退默认并写 note；
- 用户在指令里明确提到受体（`trypsin`/`1PTU`、PDB 号、文件路径）或表单显式选择时，
  `source="user"`，**原样使用**，绝不被护栏改写；
- 最终回复与报告必须写明「未指定受体，已使用系统默认 …」，把它当作**假设**而不是用户要求。

### 7.2 完整性保证

只要**输入合法且可对接**（能解析出至少一个分子），主管 Agent 会按任务规约把流程走完（见 §10.3）。
若多 Agent 未跑完（例如模型提前收尾），服务端**只记录、不接管控制**：不会自动补齐缺失环节，也不会写「系统自动补齐」。
`run.status` 仍为 `ok`，`run.completeness` 只如实说明本次实际完成了什么（信息展示，不驱动行为）：
`{"docking":"agent|partial|missing|auto","docked":N,"total":M,"ranking":"ok|missing","report":"ok|missing","positive_control":"ok|skipped"}`（无分子时为 `not_applicable`）。

### 7.3 SSE 新增事件（运行流帧，标准 Agent Protocol 与兼容端点通用）

```json
{"type":"progress","stage":"docking","done":1200,"total":10000,"percent":12.0,
 "elapsed_sec":34.5,"eta_sec":251.0,"message":"已完成 1200/10000"}
{"type":"molecules","items":[{"index":0,"total":10000,"name":"...","smiles":"...",
 "affinity_kcal_mol":-8.1,"engine":"vina","exhaustiveness":6,
 "properties":{...},"similarity_to_positive_control":0.21,"pose_url":"..."}, ...]}
{"type":"choices","note":"检索到多个同样合理的候选：请选择要使用的受体",
 "choices":[{"id":"receptor:Q9SJQ6","kind":"receptor",
             "label":"Arabidopsis thaliana DNA glycosylase/AP lyase ROS1 · Q9SJQ6 · RCSB 7YHP · 打分 114",
             "value":"Q9SJQ6",
             "prompt":"用 Q9SJQ6（Arabidopsis thaliana，DNA glycosylase/AP lyase ROS1）作为受体继续对接筛选",
             "detail":{"accession":"Q9SJQ6","organism":"Arabidopsis thaliana",
                       "protein":"DNA glycosylase/AP lyase ROS1","structure_source":"RCSB 7YHP",
                       "pdb_ids":["7YHP"],"score":114.0,"reasons":[...]}}]}
```

- 库较大时逐分子结果以 `molecules`（**批量数组**）推送，服务端按 ~200ms / 每 25 条合并一次；
  库较小时仍可能用单条 `molecule` 事件。**前端两种都要处理**。
- `progress` 每 ~1s 或每批推送一次，用于进度条与 ETA。
- 事件条目结构与原 `molecule` 完全同构。
- **`choices`（结构化选择通道）**：受体/分子自动解析出**多个候选**或**置信度不足**时下发，
  每项 `{id, kind, label, value, prompt, detail}`（`kind ∈ receptor|molecule`）：
  `label` 给人看、`value` 是机器可用值（accession / CID / SMILES）、`prompt` 是点选后原样发出的追问、
  `detail` 带物种/蛋白名/结构来源/打分。附带 `note` 说明为什么需要用户选。
  前端把它渲染成助手气泡下方的可点按钮；点选后以**同一 `conversation_id`** 把 `prompt` 作为
  下一轮指令发出，从而延续同一段对话继续跑。同一份 `choices` 只发一次（心跳路径与 `final` 前
  各有一条触发路径，已去重）；`run.data.choices` / `GET /api/runs/{id}` 的 `run.choices` 可查。
  高置信解析成功时**不会**出现 `choices`（直接继续，不打扰用户）。
- **多 Agent（`/api/agent/stream`）同样发 `molecules`**：子 Agent 执行对接时父流程阻塞、
  没有 `token`/`tool_call` 事件，`run_docking` 工具会逐条把对接结果写进运行级缓冲
  （`live_molecules`），API 层每秒心跳（`_interleave` 的 `_tick`）把它转成 `molecules`
  事件下发，并在图结束前补齐最后一个窗口的结果。实时缓冲**不写入**运行元数据。
  阳性对照只作基线，不进逐分子实时流。

### 7.4 结果分页与聚合

`GET /api/runs/{id}` 的 `result.ranking` **只内联前 N 条**（默认 200，可用 `RESULT_INLINE_LIMIT` 调整），
避免上万分子时单次响应过大；并新增：

```json
"result": {
  "ranking": [ ...最多 200 条... ],
  "ranking_total": 10000,
  "aggregates": {
    "total": 10000, "hits": 37,
    "affinity": {"min": -11.2, "max": -3.1, "mean": -6.4, "median": -6.3},
    "positive_control_affinity": -5.78,
    "engine": "vina", "exhaustiveness": 6, "with_errors": 0
  }
}
```

新增分页接口（结果表与分子详情卡片都用它，**不要**再一次性拉全量）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/runs/{id}/ranking?offset=0&limit=100&sort=affinity_kcal_mol&order=asc&q=&hits_only=false` | 分页 + 排序 + 搜索 + 「只看优于对照」；返回 `{total, offset, limit, rows, aggregates}` |
| GET | `/api/runs/{id}/export.csv` | 直接导出完整排序 CSV（不受分页限制），下载名为 `dock_{run_id}_{receptor}_{N}mols_ranking.csv` |
| GET | `/api/runs/{id}/report.pdf` | PDF 版报告，下载名为 `dock_{run_id}_{receptor}_{N}mols_report.pdf` |

`sort` 允许：`rank`、`name`、`affinity_kcal_mol`、`molecular_weight`、`logP`、`tpsa`、`hbd`、`hba`、
`rotatable_bonds`、`lipinski_violations`、`similarity_to_positive_control`、`maccs_tanimoto`、
`combined_similarity`、`exhaustiveness`；`order` 为 `asc|desc`。
`q` 为名称/SMILES 子串过滤（大小写不敏感）。

### 7.5 大规模下的产物策略

- **图表**：分子数 > 50 时自动改为「Top-20 对比图 + 亲和力分布直方图」，避免上万个柱子不可读；
  `>50` 时产物名仍为 `docking_chart` / `similarity_chart`（内容自动切换），并新增 `affinity_histogram`。
- **报告**：Markdown 中只列 **Top-N**（默认 50，`REPORT_TOP_N` 可调），完整结果以 CSV 交付。
- **位姿**：默认保存；超过 `POSE_SAVE_MAX`（默认 5000）时只保存 **亲和力最优的前 N 个**，
  并在 `notes` 说明，避免单个目录写入过多文件。
- **并发**：对接使用多进程并行，`DOCKING_WORKERS`（默认 `min(CPU//2, 16)`）控制并行度；
  Vina 网格图按受体**只计算一次**并在同一批配体间复用。

### 7.6 对接参数自动规划（v0.12）

`exhaustiveness` / `n_poses` **留空（null）即自动规划**；给出数值视为用户显式指定（`source="user"`，
自动规划不改写）。规划实现见 `core/params.py::plan_docking_params`（纯函数 + 可选 pilot 回调）。

| 规则 | 公式（阈值均来自 `AUTO_PARAM_*`，见 `.env.example`） |
| --- | --- |
| 基准强度 | `screening` → `AUTO_PARAM_BASE_SCREENING`(12)；`binding_only`/姿态分析 → `AUTO_PARAM_BASE_BINDING`(16)；`properties_only` 不规划 |
| 柔性系数 | `f_rot = clamp(P90(库内可旋转键)/5, 0.75, 2.5)`（2D 描述符；大库抽样最重的 500 个） |
| 盒体积系数 | `f_box = clamp((V_box/22³)^(1/3), 1.0, 2.0)`（保持单位体积采样密度） |
| 搜索强度 | `exhaustiveness = clamp(round(base × f_rot × f_box), 2, 32)` |
| 两阶段漏斗 | `N ≥ AGENT_FUNNEL_MIN(500)`：粗筛 `max(1, round(exh/4))` 全库 → 精算 `exh` 前 `AGENT_REFINE_TOP_N(200)`；`N ≥ 5000` 且预算允许上调到 300（`AUTO_PARAM_REFINE_TOP_LARGE`） |
| pilot 预算护栏 | `N ≥ 50`：取最贵的 3 个分子以 `exhaustiveness=1` **真实试跑**，外推总耗时与 `0.6 × RUN_TIMEOUT_SECONDS` 比较；超预算先降精算头部到 100，仍超才降强度且不低于 `base/2` |

**不变量（有回归测试守护 `tests/test_param_plan.py`）**：

- 参数是**运行级 / 漏斗阶段级**的：同一 `pass`（coarse/fine）内所有分子的 `exhaustiveness`、
  `box_size`、`box_center` 完全一致，**绝不逐分子变化**（换盒子实测就能差 1.34 kcal/mol，采样强度更大）。
- pilot **只测量**：不写位姿、不进 ranking/黑板，失败即退回静态规划（只 `logger.warning`）。
- 每条决策（含每次降级）都写入 `result.param_plan.decisions`，报告新增「参数自动规划」一节展示。

运行在 `N ≥ AGENT_FUNNEL_MIN` 时自动执行两阶段漏斗：
粗筛复用现有 `dock_library`，精算只跑头部并**沿用粗筛盒子**；同一分子的精算值参与排序，
粗筛值保留在 `affinity_coarse`。多 Agent 路径由受理层把同一规划作为「建议参数」写进指令
（`intake._planned_params_advice`），主管据此用 `run_docking(top_from_previous=...)` 走同一口径。

`GET /api/runs/{id}` 的 `result.param_plan` 与报告「参数自动规划」一节给出完整参数与理由链。

---

## 8. 取消运行、阳性对照可选（v0.5）

### 8.1 取消运行（真正中断对接）

```
POST /api/runs/{run_id}/cancel
→ {"status":"cancelling"|"already_cancelling"|"already_finished","run_id":"...","message":"..."}
```

- 对接跑在**工作线程 + 多个子进程**里，`asyncio.Task.cancel()` 无法让 Vina 停下来；
  服务端使用**协作式取消标志**：置位后父线程在收集结果时**终止进程池**并停止后续步骤。
- 运行中的流会推送：

```json
{"type":"cancelled","run_id":"...","message":"运行已被用户取消","summary":{...}}
{"type":"done","run_id":"...","summary":{...}}
```

- 之后 `run.status == "cancelled"`，`run.error` 记录进度（如「对接已取消（已完成 12/126）」），
  已完成的中间数据（分子库/性质/部分对接）仍会落盘。
- 运行已结束时返回 `already_finished`；记录不存在时 HTTP 404。

### 8.2 阳性对照为**可选**

- `positive_control` **留空 → 跳过对照分子对接与结合模式比较**：
  `result.positive_control` 为 `{}`、`result.binding` 为空、相似度字段为 `null`、图表不画对照参考线，
  并在 `run.notes` 说明「未提供阳性对照，已跳过…」。
- 提供 `positive_control`（SMILES）→ 正常执行对照对接与结合模式比较。
- `skip_positive_control=true` 可显式跳过（即使提供了对照）。

> 界面默认**不预填**阳性对照；需要对照分析时由用户填写。

### 8.3 多 Agent 模式下的实时进度心跳

子 Agent 执行对接时，父流程处于阻塞状态（不会产生 `token`/`tool_call` 事件）。
服务端为此增加**每秒心跳**：把子 Agent 上报的实时对接进度转成 `progress` 事件推送：

```json
{"type":"progress","stage":"docking","done":23,"total":40,"percent":57.5,
 "elapsed_sec":14.0,"message":"已完成 23/40"}
```

因此**两种模式都会持续输出 `progress`**，前端可用它显示进度条与 ETA、并驱动编排示意图的「当前任务」。

---

## 9. 文件上传与固定报告（v0.6）

### 9.1 上传文件

```
POST /api/uploads        Content-Type: multipart/form-data
  file: <文件>           # 必填
  kind: auto|ligand|receptor   # 可选，默认 auto（按扩展名判断）
```

支持：小分子 `.sdf/.sd/.smi/.smiles/.txt/.csv/.mol2/.mol`，蛋白质
`.pdb/.ent/.pdb1/.cif/.mmcif/.pdbqt`（大小写不敏感；`.ent` 是 RCSB 下载坐标文件的常见后缀，
与 `.pdb` 同样处理；`.cif/.mmcif` 先用 gemmi 转成 PDB 再现场准备）。
扩展名集合在 `core/receptors.py::RECEPTOR_EXTS` **只定义一份**，上传端点与受体解析链共用。

**上传只保存文件，不解析、不准备**（v0.22，用户要求「不要一上传就开始处理文件」）。
返回里只有落盘路径与 `pending: true`；解析/准备发生在**用户主动校验**或**开始运行**时：

```json
{"status":"ok","kind":"ligand","file_name":"lib.sdf",
 "path":"/abs/path/assets/uploads/....sdf","relative_path":"assets/uploads/....sdf",
 "size":12345,"ext":".sdf","pending":true,
 "message":"文件已保存到服务端（尚未解析/准备）。分子库会在**开始运行时**解析；…"}
```

`kind` 仍由扩展名/内容嗅探**低成本**判定（只读文件头），用于界面提示与校验时的分支。

### 9.1b 校验已上传文件（用户主动触发）

```
POST /api/uploads/inspect      Content-Type: application/json
  {"path": "/abs/path/assets/uploads/....sdf", "kind": "auto|ligand|receptor"}
```

这一步才做重活（完整解析 / 现场准备受体），返回与「运行阶段」**同一套实现**的结果，
因此预览与真正运行不会出现偏差：

小分子返回（`pending:false`）：`count` 为去重后的数量，`molecules` **仅前 5 条预览**；
完整数量以 `count` 为准，前端不要据此认为库中只有这些分子。

**蛋白质**返回（已现场准备为 PDBQT，并标定已知位点盒 + 受体质子化溯源）：
```json
{"status":"ok","kind":"receptor","file_name":"rec.pdb",
 "path":"...","receptor_file":"/abs/path/assets/cache/xxx.pdbqt",
 "box_center":[14.96,28.73,4.74],"box_size":[22.0,22.0,22.0],
 "site_source":"共晶配体(HEM)质心",
 "protein":"用户自定义受体 (xxx)","message":"受体已现场准备为 PDBQT（...pdbqt）",
 "dropped_hetatm":{"HEM":43,"SO4":10,"OXY":2},
 "kept_hetatm":{},
 "dropped_waters":193,
 "unsupported_hetatm":[],
 "cocrystal_ligand":{"key":"A:154:HEM","resname":"HEM","n_atoms":43,"center":[14.96,28.73,4.74]},
 "receptor_protonation":{"applied":true,"policy":"ph","ph":7.4,
   "tool":"pdb2pqr + PROPKA + meeko","his_states":{"HID":3,"HIE":2,"HIP":0},
   "titratable":{"by_residue":{"ASP":{"total":18,"protonated":0},"GLU":{"total":22,"protonated":0},
                               "HIS":{"total":5,"protonated":0},"LYS":{"total":22,"protonated":22},
                               "CYS":{"total":8,"protonated":8},"TYR":{"total":12,"protonated":12}},
                 "boundary":[{"resname":"ASP","resnum":189,"chain":"H","pka":6.6}]}},
 "chemistry_warning":"受体准备按标准流程剔除了非水杂原子：HEM×43、SO4×10、OXY×2（水 193 个）…"}
```

- **受体质子化**（v0.22）：策略为 `ph`（默认）时受体与配体用**同一目标 pH** ——
  `pdb2pqr --ph-calc-method=propka --with-ph=<pH>` 生成 PQR，再交 meeko `--read_pqr` 写 PDBQT；
  `receptor_protonation` 给出实际应用的 HIS 状态（HID/HIE/HIP）、各可滴定残基在目标 pH 下的质子化计数、
  边界残基（|pKa−pH|<0.5）与剔除的主链不全残基。找不到 pdb2pqr / 需要保留有机辅因子 /
  工具失败时**回退**标准流程，并把 `applied:false` 与原因如实回传（报告 §1.2 会给出「两侧口径不一致」提示）。
  `PDB2PQR_BIN` 可指定工具路径。
- **准备阶梯**（v0.23）：`pdb2pqr` 的氢键优化 + meeko 严格匹配（`opt`）→ 几何摆氢（`noopt`，保住全部残基；
  某些结构里氢键优化会把羟基氢摆到受体羰基氧 ~1.1 Å，meeko 距离法会误判成残基间共价键）→
  才允许 `delete`（丢弃模板不匹配的残基）→ `noopt+delete` → 仍失败才回退模板态。
  用的是哪一档记 `receptor_protonation.variant`，每档成败记 `attempts`；
  **被丢弃的残基**逐个记 `dropped_bad_residues`（报告 §1.2 会单列提示）。

- 校验失败返回 400（并删除已写入的临时文件）；超过 `UPLOAD_MAX_MB`（默认 200）返回 413。
- 返回的 `path` 直接作为 `molecule_file`，`receptor_file` 直接作为 `receptor_file` 传给运行接口。
- 位点信息会写入 `.site.json` sidecar，因此**只拿 PDBQT 路径**也能复原活性位点盒（否则会退化成全蛋白质心）。
- **化学溯源**（`dropped_hetatm` / `kept_hetatm` / `dropped_waters` / `unsupported_hetatm` /
  `cocrystal_ligand` / `chemistry_warning`）同样写入 sidecar，因此后续**只拿 PDBQT** 对接时，
  「剔除了哪些金属/辅因子」依然会出现在结果 `notes` 里，不会随上传步骤一起丢失。
  标准流程默认剔除杂原子（金属酶/含辅因子体系请见 README §11.4c：可用 `keep_hetatm` 保留后重跑）。

### 9.2 运行参数新增 `receptor_file`

`/threads/{tid}/runs/stream` 与 `/api/agent/stream` 均支持 `receptor_file`：
**优先于 `receptor`**（上传的受体文件 > 注册表受体）。若上传时给定了 `box_center`，
建议同时按 `site_center`/`site_size` 传回，或在界面中自动填入。

对话模式（`mode=chat`）下 `receptor_file` / `molecule_file` 是**附件派生字段**：
`advanced=false`（高级设置未展开）时前端仍会带上它们（附件是明确意图），
其余参数字段则一律不带。`receptor` 的 schema 默认值是 `"thrombin"`，**不代表用户指定**；
「用户到底指没指定受体」以服务端受理层 `task_spec.receptor.source` 为准
（`user` / `named` / `default` / `unresolved`），运行日志与结果 `notes` 都按它如实展示。

`receptor` / `receptor_sources` 里的**结构文件路径**同样接受 `.pdb/.ent/.pdb1/.cif/.mmcif`
与 `.pdbqt`；无法作为受体结构使用时（文件不存在、内容不是结构）会**回退默认凝血酶**，
并在 `notes` 里写明原因与「这不是你指定的受体」——绝不静默回退。

### 9.3 固定报告格式

`report.md` 由系统按**固定模板**生成（两种模式一致），章节顺序与编号固定：

```
表 1 报告信息（run id/模式/时间/受体/对接盒/引擎/搜索强度/种子/候选数/对照/受理/各 Agent 模型）
## 1. 任务与参数   1.1 任务来源与受理  1.2 受体、位点与对接盒  1.3 对接参数（同一阶段内一致）
                  1.4 工具与版本      1.5 参数自动规划       1.6 结合口袋预测
## 2. 结果排序     2.1 主组排序（含命中判据）  2.2 大配体组（盒子不同，不跨组比较）
## 3. 推荐分子
      3.1 推荐化合物排行（综合分 = 亲和力×0.45 + 配体效率×0.20 + 类药性×0.20 + 理化窗口×0.15；
                            等级 A/B/C，亲和力弱于 −6 kcal/mol 封顶 C；排行按等级优先、
                            同级按综合分降序；含推荐化合物 2D 结构图 + Agent 逐分子理由 + 筛选建议）
      3.2 优于阳性对照的分子
## 4. 理化性质   （性质按与对接**相同的质子化态**计算）
## 5. 结合模式与阳性对照比较
      5.1 结合口袋分析（盒子来源与依据 / 盒内残基清单与组成特征 / 与实验位点一致性）
      5.2 推荐分子的姿态–口袋相互作用（逐残基氢键/盐桥/疏水/π/金属配位 + 2D 相互作用图 + 3D 姿态图）
## 6. 方法与局限   （打分函数边界 / 单盒 vs 分组可比性 / 未做重打分与共识 /
                    质子化态按运行级策略统一处理但未枚举互变异构 / 受体状态 / 覆盖范围 / 结论口径）
## 7. 失败与跳过   （按原因分组列出失败分子数、占比与示例；结论只覆盖成功对接的分子）
## 8. 结论与建议   （只摘录协调 Agent 的结论/建议类小节，不重复正文数据；原文见 agent_report.md）
## 9. 数据与产物
```

- **质子化态策略**（运行级，界面基础参数区可见 / `docking.protonation` / `LIGAND_PROTONATION`）：
  `ph`（**默认**，按目标 pH 重新分配质子化态，配套 `docking.protonation_ph` /
  `LIGAND_PROTONATION_PH`，默认 7.4）/ `neutralize`（只中和带净电荷的分子）/
  `keep`（保持输入形式）；排序 CSV 带 `protonation_policy` / `charge_input` / `charge_used` 列，
  原始 SMILES 始终保留。
- **pH 处理口径**：先中和到中性形式，再按**内置官能团 pKa 规则表**（`core/protonation.PKA_RULES`，
  版本 `PKA_TABLE_VERSION`）把羧酸/胺/脒/胍/咪唑/吡啶/四氮唑/磷酸/磺酸/硫醇/酚加到目标 pH 的状态；
  逐分子记录命中的规则与判定式（如 `pH 7.4 > pKa 4.5`）。**这是规则近似，不是 pKa 预测**：
  需要微观态分布时请用专业工具生成质子化态后以 SDF 提供并选择 `keep`。
- **工作台布局约定**：基础参数区放「新手也容易调」的关键项（受体 / 引擎 / 搜索强度 / 位姿数 /
  质子化态策略 + 目标 pH / 阳性对照 / 最大分子数）；「更多参数」只放需要专业知识的项
  （结合位点盒坐标 / 口袋引擎 / 位姿保存 / 上传受体）。`scripts/check_web.py` 看护这一分层。
- **推荐排行产物**：`charts/recommend_structure_grid.png`（2D 结构网格，PDF 内嵌）；
  Agent 理由由 `submit_recommendations` 写入，报告与运行数据都会随产物一起交付。

- **统一数字格式**：亲和力 2 位小数 + 单位 `kcal/mol`、分子量 2 位、logP/TPSA 2 位、百分比 1 位；
  亲和力、分子量等数字列在 Markdown 表格里用 `:---`/`---:` 声明右对齐。
- **图与表都带连续编号与题注**：表题在表格上方（`**表 N …**`），图题在图下方（`**图 N …**`）；
  正文**不出现裸 URL**，产物引用统一写成「文件名 + 章节位置」。
- 「参数自动规划」一节的表列出本次实际采用的 `exhaustiveness`/`n_poses`/漏斗参数/pilot 结果，
  并逐条给出 `decisions` 理由链；同时声明「同一阶段（pass）内参数完全一致」。
- 「失败与跳过」从 `docking.receptors[*].results[*].error` 归并原因分组；同时 `ranking.csv` 含
  `status`/`error` 两列，失败行以 `rank=FAIL` 追加，避免「全成功」的假象。
- 多 Agent 模式下，协调 Agent 的输出被放入**第 8 节**（原文另存为产物 `agent_report_md`）；
- 图片以**相对路径**内嵌：`![图 N …](charts/<name>.png)`，任何 Markdown 阅读器都能直接显示；
  网页报告页把相对路径解析为产物接口的真实 `<img src>`（界面不显示地址文本），
  PDF 则把同名 PNG 真正嵌入页面（不是只写路径）。图包括：对接对比图、亲和力分布、
  相似度图、性质空间图、**结合模式散点图**、**分子结构对比网格**；
- 无阳性对照时，第 5 节写「未做对照分析」，对照相关图自动省略。

### 9.4 上传的健壮性细节（v0.6）

- **`.smi` 两种列顺序都支持**：标准 `SMILES 名称` 与中文用户常见的 `名称 SMILES` 均可解析。
- **位点来源会如实回报**：`site_source` 字段说明位点盒是怎么来的
  （`共晶配体质心` / `注册表已知位点（1DWC）` / `蛋白质质心`）。
- **位点不确定时给出 `warning`**：上传 `.pdbqt` 且无法定位活性位点时，
  返回里会带 `warning`，提示「盒子很可能不在活性位点，建议改传 .pdb 或手工填写位点」。
  界面应把这个 warning 显著展示并引导用户填写位点盒。
- 上传失败会删除已写入的临时文件，不留垃圾。

### 9.5 统一输入归一化层（v0.15）

所有入口（`/api/uploads`、对话附件、`receptor_file` / `molecule_file` / `ligands_text` 参数、
`import_molecule_library`、`molecular_docking`、`read_receptor_file`）共用同一套归一化实现：

| 模块 | 职责 |
| --- | --- |
| `core/normalize_io.py` | 容器解包（gzip/zip 单成员）、**内容嗅探**（优先于扩展名）、编码/分隔符/表头识别 |
| `core/normalize.py` | 配体/受体解析、规范 SMILES + 原始 ID、去重与别名、逐行容错、溯源落盘 |
| `core/ligands.py::read_molecule_file` | 旧契约入口，内部走归一化层（异常时回退旧解析器） |

**配体侧**：SDF/MOL2/MOL、SMILES/`.smi`/`.txt`、CSV/TSV、gzip/zip；
编码自动判定（`utf-8-sig`/`utf-8`/`gbk`/`utf-16`/`latin-1`）；分隔符自动判定（`,`/`\t`/`;`/空白）；
表头别名（`name/nickname/title/compound/id/编号/名称/分子名/配体`，
`smiles/canonical_smiles/structure/结构`，`inchi/inchikey`）；坏行不中断整库。
标准表示：

```json
{"id":"PGR-001","name":"PGR-001","smiles":"<canonical>",
 "source_file":"/abs/.../lib.sdf","source_index":1,"raw":"<原始记录>"}
```

SDF 优先取 `_Name`，其次 `ID/Name/Title/编号`，无名称则 `MOL<i>`；按规范 SMILES 去重并保留首个 ID 与全部别名；
盐/反离子沿用 `describe_ligand` 的保守拆分，**不静默改变化学**。`.xlsx` 在未安装 `openpyxl` 时
返回空清单 + `notes` 提示转 CSV（不报错）。名称-only 库走在线解析链，失败时列出候选让用户选择。

**受体侧**：`.pdb/.ent/.pdb1/.cif/.mmcif/.pdbqt` + gzip/zip；内容不像结构时抛可操作的 `ValueError`
（说明「看起来是什么、缺什么、怎么办」），**不静默回退默认受体**。

**溯源落盘**：每次归一化都并入运行产物 `input_normalization.json`（前端「中间数据」可下载）：

```json
{"inputs":[{"kind":"ligand","source_file":"/abs/.../lib.csv","format":"csv",
  "encoding":"gbk","delimiter":",","header":{"name":"名称","smiles":"SMILES"},
  "records_total":9,"records_ok":6,"records_skipped":3,
  "skipped":[{"line":2,"reason":"SMILES 无法解析"},{"line":5,"reason":"缺少结构列"}],
  "duplicates_removed":1,"duplicates":[{"name":"ASA","smiles":"...","kept_name":"阿司匹林","aliases":["ASA"]}],
  "aliases":{"阿司匹林":["ASA"]},"notes":["共 9 条，成功 6；跳过 3 条：第 2 行 …"]}]}
```

`POST /api/uploads` 的返回里同时带 `input_normalization`（同一结构，单条）；此处 `count`
即归一化后的 `records_ok`，与后续 `import_molecule_library` 读到的分子数**一致**。

**路径解析兜底**：上传端点的落盘名带时间戳前缀（`<时间戳>-<哈希>-<原名>`），
`import_molecule_library` / `molecular_docking` 的 `molecule_file` 允许只给显示名
（`PGR.sdf`），系统会在 `uploads/cache/assets` 中按「精确名 → 后缀匹配」解析成绝对路径；
**命中多个候选时返回 `candidates` 与 `needs_user_input: true`，绝不随便取一个**。
解析失败时返回 `attempted`：逐条列出实际尝试过的路径与失败原因。

---

## 10. 多 Agent 协作机制（v0.7）

### 10.1 共享黑板

多 Agent 运行会创建一块**运行级共享黑板**，子 Agent 通过它横向协作（而不是靠消息字符串中转）：

| 区块 | 写入者 | 读取者 |
| --- | --- | --- |
| `molecules`（规范化去重后） | 分子属性评估 Agent | Docking 执行 Agent（对接时自动取用，无需再传 JSON） |
| `properties` | 分子属性评估 Agent | 报告与复核 |
| `pockets` / `site` | 口袋分析 Agent（`predict_binding_pockets` / `set_docking_site`） | Docking 执行 Agent（对接盒交接） |
| `docking` / `receptor` | Docking 执行 Agent | 结合模式检测 Agent（交叉核验） |
| `binding` / `positive_control` | 结合模式检测 Agent | 复核 |
| `notes` | 全部 Agent | 界面展示（协作痕迹） |

产物 `blackboard`（JSON）保存协作快照；`GET /api/runs/{id}` 的 `collaboration`
字段返回 `{"stats": {...}, "notes": [...]}`。

### 10.2 子 Agent 的决策空间与输出契约

四个子 Agent 不再「只有一个工具 + 原样复述」，而是各自拥有可自主选择的工具：

| 子 Agent | 工具 | 输出契约 |
| --- | --- | --- |
| 分子属性评估 | `normalize_molecule_library`、`molecular_property_assessment` | 一个 JSON，含 `assessment`，可带 `agent_note` |
| 口袋分析 | `predict_binding_pockets`、`compare_pocket_with_experiment`、`set_docking_site`、`list_pocket_engines`、`available_receptors` | 一个 JSON，含 `pockets` / `selected` / `validation`，可带 `agent_note` |
| Docking 执行 | `available_receptors`、`molecular_docking`、`fetch_protein_structure` | 一个 JSON，含 `receptors`，可带 `agent_note` |
| 结合模式检测 | `binding_mode_analysis`、`positive_control_similarity`、`check_binding_consistency` | 一个 JSON，含 `results` 与可选 `consistency` |

`molecular_docking` 的关键参数（完整说明见工具 docstring）：

| 参数 | 作用 |
| --- | --- |
| `receptor_file` / `receptor_sources` | 上传受体文件（.pdb/.ent/.pdb1/.cif/.mmcif/.pdbqt）或预置/多个受体（蛋白质库，全组合对接） |
| `molecule_file` / `molecules_json` | 小分子库文件或直接 JSON；`top_from_previous=N` 表示从共享黑板取上一轮最好的 N 个精算 |
| `keep_hetatm` | 逗号分隔的**要保留的非水杂原子残基名**（如 `HEM,ZN,NAD`）。默认空 = 标准流程剔除杂原子，但被剔除的残基会逐条计数返回 `dropped_hetatm` 并在 `notes` 中提示 |

每个对接结果行除能量项外还带化学事实字段：
`ligand_facts`（`num_fragments`/`formal_charge`/`has_metal`/`mw`/`heavy_atoms`/`rotatable_bonds`/
`undefined_stereocenters`…）、`ligand_warnings`、`dock_smiles` + `removed_fragments`（发生盐/反离子
拆分时）、`pose_count`、`seed`/`seed_policy`、`box_fit_warning`（配体跨度接近或超出搜索盒）。
C 方案（v0.12 起）每个对接结果行还带 `box_group`（`main`/`large`）、`box_size`（该分子实际使用的
盒子）与 `ligand_span`（其 3D 跨度）；`large` 组行另有 `main_box_size` 与改写过的
`box_fit_warning`（「已单独分组重跑（盒子 X Å）」）。详见 §13.5。
受体块另带 `dropped_hetatm` / `kept_hetatm` / `dropped_waters` / `unsupported_hetatm` /
`cocrystal_ligand`。这些字段都**由工具原始输出回填**，模型转述时漏掉也会被补回（见 §10.1）。

**分发边界校验**：协调 Agent 侧对子 Agent 返回做 JSON 解析与关键字段校验，
失败会**带纠正提示自动重试一次**；两次都失败则返回 `{"status":"agent_output_invalid", ...}`
（而不是把垃圾文本交给协调 Agent）。

### 10.3 流程控制权（v0.11 起）

整条流程由**主管 Agent** 决策：何时分发、发给哪个子 Agent、并行还是串行、失败是否重试、
最后是否出报告。服务端**不再**提供独立复核流程，也不再事后接管「自动补齐」。

服务端只保留两类职责：

1. **记录**：`run.completeness` 记录本次实际完成了什么（`docking: agent|partial|missing|auto`、
   `ranking`、`report`、`positive_control`），**仅作信息展示**，不驱动任何行为；
2. **接收**：解析并落盘 Agent 真实调用工具产出的数据（见 §3）。

因此报告固定为 **9 个章节**（不再有「独立复核」节），`GET /api/runs/{id}` 也不再返回
`verification` 字段；`stage=verify` 事件已移除。

### 10.4 SSE 阶段事件

```json
{"type":"stage","stage":"pocket","message":"分析结合口袋并确定对接盒子（引擎=auto）"}
```

`stage` 取值：`import` / `properties` / `pocket` / `docking` / `binding` / `report` / `done`
（`stage=verify` 已随独立复核流程一并移除）。前端按 stage→节点映射推进运行图，
并用事件里的 `ts` 计算每段的真实时长（见 §15）。

---

## 11. 每个 Agent 独立的模型实例（v0.8）

四个子 Agent 之前**共用同一个 LLM 对象**（只有提示词与工具集不同），现已改为**每角色一个独立实例**。

**角色**（`docking_agent.runtime.llm.ROLES`）：`intake` / `coordinator` / `property` / `pocket` / `docking` / `binding`。

**配置优先级**（越具体越优先）：

```
角色环境变量 LLM_<字段>_<角色>  >  配置文件 roles.<角色>  >  全局环境变量 LLM_<字段>  >  配置文件 config
```

可覆盖字段：`model` `base_url` `api_key` `temperature` `top_p` `timeout` `thinking`
`max_tokens` `extra_body`（以及 `extra_headers`）。

```dotenv
LLM_MODEL_DOCKING=deepseek-v4-pro
LLM_MODEL_PROPERTY=deepseek-flash
LLM_TEMPERATURE_PROPERTY=0
LLM_BASE_URL_BINDING=
LLM_API_KEY_POCKET=
```

> 注意：全局环境变量**不会**压掉配置文件里按角色的显式设置（例如 `.env` 里的
> `LLM_TEMPERATURE=0.2` 不会覆盖 `roles.property.temperature`）。

**可观测性**：

- `GET /api/health` → `roles`：各角色最终生效配置；
- `GET /api/runs/{id}` → `agent_models`：本次运行各角色**实际使用**的模型，每项含
  `model`（构建时请求）、`actual_model` / `calls`（**服务端响应回执确认**的模型与调用次数）、
  `temperature` / `base_url` / `instance_id`（`instance_id` 各不相同即证明实例独立）；
- 固定报告首表新增「各 Agent 模型」一行（优先展示服务端确认的模型）。

**记忆隔离**：每个子 Agent 另有独立 checkpointer（各自一个 `MemorySaver`），
因此四者的短期记忆互不干扰；子 Agent 每次调用仍使用新 `thread_id`。

---

## 12. 设置接口（v0.8 设置页面）

设置页面（`#/settings`）的所有字段由 `GET /api/settings` 下发，前端不硬编码字段列表。

### `GET /api/settings`

```json
{
  "specs": [
    {"path": "llm.model", "label": "默认模型", "group": "llm", "kind": "str",
     "default": null, "choices": [], "min": null, "max": null, "step": null,
     "env": null, "env_priority": false, "needs_restart": false,
     "role": "", "role_label": "", "field": "", "help": "", "placeholder": "deepseek-flash"}
  ],
  "groups": [{"id": "llm", "label": "LLM 接入（全局）", "help": "..."}],
  "roles": ["intake", "coordinator", "property", "pocket", "docking", "binding"],
  "role_labels": {"coordinator": "整体协调 Agent"},
  "values": {"llm.model": null, "roles.docking.model": "deepseek-v4-pro"},
  "effective": {"llm.model": "deepseek-flash", "docking.exhaustiveness": 6},
  "sources": {"llm.model": "环境变量", "roles.docking.model": "界面设置"},
  "secret": {"api_key_set": true, "api_key_hint": "sk-1…7e97", "docking_api_key_set": false},
  "meta": {"path": ".../config/local_settings.json", "relative": "config/local_settings.json",
           "exists": false, "env_file": true, "needs_restart": ["CHECKPOINT_BACKEND"]},
  "agent_models": {"docking": {"model": "deepseek-v4-pro", "actual_model": "deepseek-v4-pro",
                               "calls": 2, "instance_id": 132917840948080}}
}
```

- `values`：**界面设置**里显式保存的值（未保存为 `null`）；
- `effective`：该字段最终生效的值；
- `sources`：生效值的来源（`界面设置` / `环境变量 …` / `内置默认` / `内置角色默认 …`）；
- `secret`：密钥只回显「是否配置 + 首尾掩码」，**任何接口都不返回明文**；
- `llm.extra_headers` 中名字含 `key`/`token`/`auth`/`secret` 的项，`values` 与 `effective`
  都只给 `***`（提交时原样回传 `***` 表示保持原值）；
- `specs[].readonly = true` 的项（如 `deploy.port`）由启动参数决定，`PUT` 会被拒绝。

### `PUT /api/settings`

请求体支持嵌套或扁平两种写法（都只写**提交的字段**，未提交的保持原样）：

```json
{"settings": {"llm": {"model": "deepseek-flash"}, "roles": {"docking": {"model": "deepseek-v4-pro"}}}}
{"settings": {"llm.model": "deepseek-flash"}}
```

- 空字符串 `""` 或 `null` = **清除该项**（回到继承 / 内置默认）；
- 校验失败返回 `400` + `{"error_message": "...", "errors": [{"path": "...", "message": "..."}]}`，
  **不会写入文件**；
- 成功返回 `{"status":"ok","path":"...","applied_env":["SSE_BATCH_SIZE"],"updated":[...],"settings":{...}}`；
- 写入 `config/local_settings.json`（已加入 `.gitignore` 与 `scripts/pack.sh` 排除），
  运行类字段会立即写入进程环境变量，无需重启。

### 其他

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/settings/reset` | 删除 `config/local_settings.json`，回到 `.env` + 内置默认（返回 `{"removed":true}`） |
| POST | `/api/settings/reload` | 丢弃已构建的 Agent 与模型实例，使模型/端点改动在下一次运行生效 |
| GET | `/api/models` | 用当前端点 `GET /models` 拉取可用模型：`{"status":"ok","base_url":"...","models":["..."]}`；失败时 `{"status":"error","error":"..."}` |
| POST | `/api/settings/test` | `{"role":"docking"}` → 用该角色自己的配置发一次最小请求，返回 `{"status":"ok","model":"...","actual_model":"...","latency_ms":3700,"sample":"就绪"}` |

> 上游失败时返回的 `error` 文本会做**脱敏**（`sk-…` / `Bearer …` / `api_key=…` 被替换为 `xxx***`），
> 避免上游把请求头回显在错误体里造成密钥泄露。

> `POST /api/settings/test` 只**临时**构建该角色的模型实例，测试结束后恢复该角色的登记条目，
> 不会影响运行记录 / 报告里的「各 Agent 模型」溯源。

### 优先级（越具体越优先）

```
角色环境变量 LLM_<字段>_<角色>
  > 界面设置（该 Agent）  local_settings.json → roles.<角色>
  > 内置角色默认         agent_llm_config.json → roles.<角色>
  > 界面设置（全局）      local_settings.json → llm
  > .env 全局            LLM_<字段>
  > 内置默认             agent_llm_config.json → config
```

运行类字段：**界面设置 > .env > 内置默认**。`deploy.*` 例外：`PORT` 恒为只读（由启动命令决定），
`DOCKING_MAX_LIGANDS` / `UPLOAD_MAX_MB` 在 `.env` 已显式设置时以 `.env` 为准（防止界面放宽上限），
未设置时可由界面调整。被界面清除的项会**恢复到注入前的环境变量基线**，`reset` 也只回滚界面注入过的键。

### 服务暴露

服务默认只监听 `127.0.0.1`；`python -m docking_agent -m http --host 0.0.0.0` 可改为对外监听，
但**本服务没有鉴权**（设置接口可改端点、连通性测试会携带当前密钥发请求），请勿暴露到不可信网络。

---

## 13. 结合口袋与对接盒（v0.9）

对接盒不再由模型猜测，而是由**真实工具**确定并全程记录来源。

### 13.1 请求参数新增 `pocket_engine`

`/threads/{tid}/runs/stream`、`/api/agent/stream`、`/run` 均新增：

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `pocket_engine` | `""`(默认=auto) / `auto` / `p2rank` / `geometric` / `known_site` | 未显式给 `site_center`/`site_size` 时，用该引擎确定盒子；`known_site` 表示只用受体已知位点、不做预测 |

多 Agent 模式下该参数会渲染进协调 Agent 的指令，要求它**先调用 `run_pocket_analysis`**（口袋分析 Agent）
再对接；显式给了 `site_center` 的请求则跳过口袋分析。

### 13.2 结果新增字段（每个受体块）

```json
{
  "receptor_key": "thrombin",
  "box_center": [31.5, 13.74, 24.36],
  "box_size": [22.0, 22.0, 22.0],
  "box_source": "实验位点（共晶配体质心）· geometric 预测一致（相距 2.6 Å）",
  "box_chosen_by": "experimental_site",
  "box_validation": {"status": "consistent", "distance_angstrom": 2.6, "agree_radius": 8.0,
                     "shared_residues": ["TRP215", "CYS191", "SER195"]},
  "box_library_floor": {"enabled": true, "bound": 25.2, "p95_span": 15.2,
                        "sample_n": 200, "library_n": 4312, "k": 200, "cached": true,
                        "raised": true, "size_before": [22.0, 22.0, 22.0]},
  "box_atom_stats": {"atoms": 431, "nearest_angstrom": 3.32, "nearest_atom": "GLY216"},
  "box_group_sizes": {"main": [25.2, 25.2, 25.2], "large": [33.0, 33.0, 33.0]},
  "box_group_counts": {"main": 4311, "large": 1},
  "pockets": [{"rank": 1, "name": "pocket_1", "score": 4.545,
               "center": [29.099, 14.677, 24.0], "extent": [13.0, 11.0, 7.0],
               "volume": 215.0, "burial": 47.8, "enclosure": 23.4,
               "hydrophobic_contacts": 5.2, "residues": ["GLU192", "TRP215"], "source": "geometric"}],
  "box_warnings": []
}
```

`box_chosen_by` 取值：`request`（用户/Agent 显式指定）、`pocket_agent`（口袋分析 Agent 通过
`set_docking_site` 提交）、`experimental_site`（实验位点）、
`pocket_prediction`（工具预测口袋）、`known_site`（注册位点）、`centroid`（不可靠兜底）。

### 13.3 决策规则与优先级

```
用户显式指定（表单/指令/口袋 Agent 提交）
  > 实验位点（共晶配体 / 注册表标注）——工具预测做独立验证，不一致时记录警告
  > P2Rank 预测口袋
  > 内置几何法预测口袋
  > 蛋白质心（标注「不可靠」）
```

只有**低可信兜底位点**（蛋白质心）时，系统会改用工具预测的 top 口袋。

### 13.4 运行产物

- 新产物 `pockets.json`：`{engine, source, chosen_by, center, size, validation, warnings, pockets}`；
- 固定报告首表新增「对接盒」行；第 1 节附口袋预测表（标出被采用的口袋）；
- 对接盒溯源：`box_source` / `box_chosen_by` / `box_validation` / `box_warnings` 随结果一起落盘，
  不一致时写入 `box_warnings`（由主管 Agent 在结论里说明）。

### 13.5 库级配体感知下限与超限分子分组（C 方案，v0.12）

问题：Vina 分数对盒子大小**敏感且非单调**（同一配体在 18³/22³/28³/34³ 间最大差 1.34 kcal/mol，
见 `docs/architecture.md` §15.7b 的 B0 表），所以**绝不能逐分子自适应盒子**；但固定小盒子又会让
大配体（肽类/大环）被欠采样。C 方案在「一致性第一」的前提下解决这件事：

1. **主组盒子唯一**：每个受体只定一次盒子，同一运行内所有 main 组分子共用它（不变量，有回归测试）。
2. **库级配体感知下限**（默认开，`BOX_SPAN_ENABLED=on`）：
   `size_i = clamp(pocket_extent_i + 2×padding, lib_lower_bound, max_size)`，
   其中 `lib_lower_bound = max(POCKET_MIN_SIZE, round(P95(库内配体 3D 最大跨度) + 10, 1))`。
   成本控制：先用 2D 重原子数降序取前 `BOX_SPAN_SAMPLE`（默认 200，20–1000）个生成 3D 算跨度，
   取 P95（最近秩，抗单个异常值）+ 10 Å；结果按「库 SMILES 的 sha1 前 12 位」缓存到
   `assets/cache/box_span/`，命中直接返回。抽样/3D 失败一律降级为 `POCKET_MIN_SIZE` 并
   `logger.warning`，**不让对接因为算跨度而失败**。抬高时写入 `box_warnings` 并把
   ` · 库级下限 Y Å` 追加到 `box_source`。
3. **超限分子分组**：某分子「任一维跨度 + `BOX_GROUP_MARGIN`（默认 10 Å）> 主盒对应边」时划入
   `box_group="large"`。实现上是**两遍对接**：先用主盒跑一遍（超限分子的结果先扣住不上报，
   主组结果即最终主组结果），再让 large 组用**同一中心**的
   `clamp(组内最大跨度 + BOX_LARGE_PADDING(默认 12 Å), 主盒, 大配体组上限)` 单独重跑。
   large 组为空时完全保持旧行为（零额外开销）；不为全库预生成 3D（跨度取自对接时本来就要生成的
   PDBQT），代价是超限分子会被对接两次（默认下限下通常只有 ~5% 的尾巴）。
4. **报告与数据**：
   - 每个结果行带 `box_group` / `box_size` / `ligand_span`；large 组行另有 `main_box_size`，
     且 `box_fit_warning` 写成「已单独分组重跑（盒子 X Å）」；
   - `ranking.csv` 在 `exhaustiveness` 之后新增 `box_group`、`box_size`（`22.0x22.0x22.0` 紧凑串）；
   - `ranking.csv` 开头新增 `id` 列（分子 ID，来自输入文件的 ID 列 / SDF 标题行），
     结尾新增 `source_index` / `source_file`（该分子在输入文件里的位置与来源）——
     用户要求「报告带上小分子 ID」时，报告与 CSV 都能给出文件里那个编号；
   - 默认排序榜（`merge_and_rank`）**只含 main 组**；large 组单独在报告「大配体组（盒子不同，不跨组
     比较）」小节列出，完整两组都在 `docking.json` / `result.json` 里（行上带标记，不丢数据）；
   - 报告元信息「对接盒」行写明中心、尺寸、来源，以及**库级下限值与是否因此扩大**。

显式指定盒子（表单 `site_center/site_size`、口袋 Agent 提交的盒子）**不受库级下限影响**（显式优先
是不变量）；但超限分子仍会被单独分组，因为那不改动主盒。新的四个环境变量：
`BOX_SPAN_ENABLED` / `BOX_SPAN_SAMPLE` / `BOX_GROUP_MARGIN` / `BOX_LARGE_PADDING`。

### 13.6 设置项（设置页面「运行与性能参数」）

| 路径 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `runtime.pocket_engine` | `POCKET_ENGINE` | `auto` | 口袋预测引擎 |
| `runtime.pocket_top_n` | `POCKET_TOP_N` | 10 | 保留的候选口袋数 |
| `runtime.pocket_padding` | `POCKET_PADDING` | 4.0 | 口袋范围 → 盒子的外扩量（Å） |
| `runtime.pocket_min_size` | `POCKET_MIN_SIZE` | 18.0 | 盒边长下限（Å） |
| `runtime.pocket_max_size` | `POCKET_MAX_SIZE` | 30.0 | 盒边长上限（Å） |
| `runtime.box_span_enabled` | `BOX_SPAN_ENABLED` | `true` | 库级配体感知下限开关（off=旧行为） |
| `runtime.box_span_sample` | `BOX_SPAN_SAMPLE` | 200 | 库级下限抽样的分子数 K（20–1000） |
| `runtime.box_group_margin` | `BOX_GROUP_MARGIN` | 10.0 | 超限判据余量（Å） |
| `runtime.box_large_padding` | `BOX_LARGE_PADDING` | 12.0 | 大配体组盒子余量（Å） |

### 13.7 本地部署 P2Rank（可选）

解压发行包到 `projects/assets/tools/`（或设 `P2RANK_HOME`）即可被自动识别，需要系统有 `java`；
未安装时自动回退到内置几何法，并在 `attempts` / `warnings` 里说明，不静默降级。

`GET /api/health` 之外，可用 `list_pocket_engines` 工具或
`docking_agent.core.pockets.available_engines()` 查询可用引擎。

---

## 14. 任务受理层（intake，v0.10）

「理解用户要什么」与「怎么做到」分成两层：受理层（`src/docking_agent/intake.py`）产出**任务规约**，
协调 Agent 只吃规约做编排。这样 `run / ask / reject` 由受理层显式判定，
不再出现「含糊就先问用户」与「输入合法就必须跑完、不得询问」两条规则互相打架。

### 14.1 调用链

```
POST /api/agent/stream
  → resolve 计数清零
  → thread_id = conversation_id or run_id（见「多轮会话」）
  → graph.aget_state(thread)              读最近 8 条 human/ai 作为 prior_turns（只读）
  → intake.build_task_spec(req, prior_turns=...)  确定性规则（零模型；继承上一轮分子/受体）
  → [需要时] intake.refine_task_spec()   受理模型（角色 intake，温度 0；上下文里带 prior_turns）
  → intake.render_agent_message(spec)    渲染成协调 Agent 的消息
  → run.data["task_spec"] = spec         写入运行记录（可观测）
  → graph.astream(..., config={"configurable":{"thread_id": thread_id}})  协调 Agent 按规约编排
```

多轮守卫：存在 `prior_turns` 且本轮 `message` 非空时，受理层**不得**重复判 `ask`
（用户正是在回答上一轮），除非受理模型仍明确要求补充且**上一轮与本轮都拿不到任何可识别分子来源**。

### 14.1b 点名了具体受体 → 先自动解析；不确定才让用户选（v0.11，产品底线不变）

用户点名受体时说的是口语化名称（「植物去甲基化酶ROS1」「代森猛锌」），而数据库要的是
accession/基因名/英文名。受理层**不做网络请求**（保持零模型、零延迟、可单测），只把这种受体标成
`named`（**待解析**）并 `decision=run`；真正的解析由协调 Agent 第一步调用 `fetch_protein_structure`
完成（多策略检索 + 结构获取 + 溯源，见 §14.1c）。

| 指令 | `receptor.source` | 判定 |
| --- | --- | --- |
| 完全没提受体 | `default` | `run`：用系统默认受体，并在回复/报告里写明「未指定受体，已使用系统默认 …」 |
| thrombin/trypsin/1DWC/PDB 号/UniProt accession/上传受体文件 | `user` | `run`：按用户指定的受体执行 |
| 点名了具体受体（如「植物去甲基化酶ROS1」「ROS1」「EGFR」）但非注册表/PDB/accession 形状 | `named` | `run`：**第一步自动在线解析**；解析成功即继续完整流程 |
| 自动解析**真失败**（一个都没查到）或**歧义/低置信**（工具写回） | `unresolved` | 不计算：把候选列成 `choices` 让用户选，或列出已尝试检索请用户补 PDB ID/accession/文件 |

受理层的「点名」发现来自两处：受理模型逐字提取的 `mentioned_receptor`，或指令里
「…酶/…蛋白/…受体/kinase/receptor」（可带尾随基因 token，如「植物去甲基化酶ROS1」）式表述
（`_named_receptor_candidate`）；另外显式出现的 UniProt accession 优先按 accession 处理
（用户点选候选后的追问「用 Q9SJQ6 …继续」因此是**可解析**的 `user`）。候选名会先剥掉
「请把/这个/该」等动词与指示代词，再排除「未指定/默认/某个/其它/任意受体」这类**泛指**表述，
因此「帮我看看这个受体的成药性」「未指定受体就用系统默认」都仍然 `run`。

**`named → unresolved` 的关键**：不是受理层先判死，而是 `fetch_protein_structure` 在**真正检索过**
之后写回（`tools/choices.py::mark_receptor_unresolved`）：解析失败/歧义时把本条规约的
`receptor.source` 改成 `unresolved`，并附上**真实的已尝试检索**与**候选清单**。

分发层护栏（代码保证，不靠模型自觉）：`run_docking` 发现 `receptor.source == "unresolved"` 时，
在调用任何对接引擎**之前**返回
`{"status":"needs_user_input","receptor":"…","attempts":[...],"candidates":[...],"message":"…"}`，
**不调用** `molecular_docking`/`dock_library` —— 即使主管 Agent 没照提示停下来问，也绝不可能
用默认受体把结果算出来。

### 14.1c 受体自动解析链（`core/resolve.py` + `tools/online.py`）

```
名称（含中文）
  → core.resolve.normalize_query   基因 token / 物种线索（植物→拟南芥 3702 等）/ 中文家族词→英文
  → 多策略检索 UniProt             gene_exact+organism_id → gene_exact+reviewed → gene_exact
                                   → (family) AND (organism_id) → (family) AND (plant OR Arabidopsis)
                                   → 自由文本（英文）
  → 打分（reviewed +40 / 物种精确 +30 / 同为植物 +18 / 物种不符 −25 / 家族词 +20 / 基因精确 +15 / 注释 +9）
  → 三态判定：
      resolved       唯一且所有线索强匹配 → 自动继续
      ambiguous      多个分数接近的候选（跨物种无法消歧）→ choices 让用户选
      low_confidence 单一候选但未 reviewed / 物种不符 / 名称只部分匹配 → choices 让用户确认
      not_found      一个都没查到 → 回执逐条列出已尝试检索
  → 结构链          UniProt PDB 交叉引用 / RCSB Search 反查的实验结构优先；
                     没有实验结构、或候选结构都准备失败 → AlphaFold DB（用接口返回的 pdbUrl，
                     模型版本不写死），再走现有 core.receptors.prepare_user_receptor 生成 PDBQT
  → 溯源            accession / entry_id / 物种 / 蛋白名 / structure_source(rcsb|alphafold) /
                     structure_url / 下载文件与大小 / AlphaFold 模型版本 / 打分理由 / 已尝试检索
```

返回 JSON（成功）关键字段：`receptor_file`、`box_center`、`box_size`、`structure_source`、
`pdb_id`（RCSB）或 `alphafold`（预测模型）、`accession`、`organism`、`protein`、`score`、
`score_reasons`、`why_selected`、`structure_url`、`structure_file`、`file_size`、
`receptor_resolution.attempt_summary`、`candidates`、`provenance`。
报告与最终回复应引用 `provenance`（"可追溯"要求）。

**分子侧同理**：`fetch_molecule_record` 支持中文别名映射（代森猛锌/代森锰锌 → Mancozeb）；
命中多组分/配位聚合物（如 PubChem CID 3034368 的 Zn/Mn-EBDC）时**不臆造单一结构**，
返回原始 SMILES、`is_mixture`、`components`、`representative_smiles`（最大有机片段）与
`mixture_note`，并给出 `choices`（原始多组分 / Mn-EBDC 单体 / Zn-EBDC 单体 / 最大有机片段）
让用户确认代表结构的取法。

### 14.2 任务规约（`task_spec`）

```json
{
  "task_type": "screening|properties_only|docking_only|binding_only|report_only|unknown",
  "goal": "用户目标（或系统默认任务）",
  "authority": "manual|chat|chat+advanced",
  "decision": "run|ask|reject",
  "receptor": {"name": "thrombin", "file": "", "source": "user|default|named|unresolved"},
  "site": {"center": [], "size": [], "source": "user|tool"},
  "ligands": {"source": "text|file|mentioned|library", "count": 1, "text": "...", "file": ""},
  "positive_control": {"smiles": "", "provided_by": "user|none|skipped"},
  "params": {"engine": "vina", "exhaustiveness": 6, "n_poses": 1, "pocket_engine": "auto",
             "save_poses": true, "max_ligands": 0, "skip_positive_control": false},
  "missing": [], "questions": [], "assumptions": ["未指定受体 → 使用默认受体 thrombin"],
  "out_of_scope": false, "raw_request": "用户原文", "current_message": "本轮指令",
  "prior_turn_count": 0, "multi_turn_answer": false,
  "needs_llm": true, "source": "rules|rules+llm", "llm_status": "ok|invalid_json|error:...",
  "confidence": 0.6
}
```

解析失败/歧义时，`receptor.resolution` 会带上 `{status, requested, attempts, candidates}`；
SSE `choices` 事件与 `GET /api/runs/{id}` 的 `run.choices` 给出结构化可选项。

关键语义：

- **`decision`**：`run`=必须完整跑完（不得中途询问）；`ask`=只说明缺什么并提问、不调工具；
  `reject`=超出系统能力、不调工具；`receptor.source=="named"` 是 `run` 的**唯一例外**：
  必须先自动解析，只有解析不确定时才可让用户选（见 §14.1b）；
- **`missing` 不是阻断**：系统对受体/阳性对照/参数有默认值、位点由口袋工具确定；
  分子缺失不再自动用示例库（见 §14.3e），改为向用户索取；
  `ask` 成立有两种情形：① 受理模型明确置 `needs_user_input=true` 且用户未给出任何可识别分子来源；
  ② **自动解析真失败（`receptor.source=="unresolved"`）**（见 §14.1b，多轮也不豁免）；
- **`receptor.source`**：`user`（指令/表单显式指定，原样使用）、`default`（受理层未识别到受体，
  只是系统/高级设置默认值）、`named`（点名了但待在线解析 → 仍 `run`，第一步自动解析）与
  `unresolved`（自动解析真失败/歧义 → 不计算，列 `choices`/已尝试检索让用户决定）语义不同；
  `default` 必须弱表述并让工具走空值回退（见 §7.1.1），报告与回复要写明「未指定受体，已使用系统默认」；
- **模型只能补白名单字段**（`task_type`/`goal`/`mentioned_molecules`/`mentioned_receptor`/
  `needs_user_input`/`missing`/`questions`/`assumptions`/`out_of_scope`/`confidence`）；
  `params`/`site`/`positive_control`/`decision` 等受限字段一律忽略并记入 `llm_notes`；
- **不得编造分子**：`mentioned_molecules` 里每一项都必须**逐字**出现在 `raw_request` 中，否则丢弃；
- 受理模型失败（无 key / 超时 / JSON 不合法）→ `llm_status` 记录原因并**回退确定性规约**，不影响运行。


### 14.3 调用次数与开销（实测）

| 模式 | 受理层调用模型 | 编排 Agent 调用次数 |
| --- | --- | --- |
| `manual` | 否（intake 实例不构建） | 4（与拆分前一致） |
| `chat` / `chat+advanced` | 是，1 次 | 4（与拆分前一致） |
| 确定性流水线 | 否（零模型） | 不适用 |

### 14.3b 指令里直接写了分子（确定性抽取，零模型）

用户在对话/表单说明里写「华法林 CC(=O)CC(...)；布洛芬 CCC(...)」（名称与 SMILES 空格分隔、
或夹在整句话里）时，受理层会用 `core.ligands.extract_smiles` **确定性抽取**这些分子并直接使用
（`ligands.source = "message"`，指令渲染成可直接导入的 `名称:SMILES` 清单）。

这条修复针对一个真实踩过的坑：抽取失败时旧逻辑会静默退回**内置示例分子库**，
用户看到的却是别人的分子（benzamidine 等）被对接 —— 即「擅自用示例库」。
**v0.18 起进一步收紧：默认完全不使用示例库**（见 §14.3e），只有用户明确要求时才允许。

### 14.3f Agent 间按文件交接（v0.19）

子 Agent 之间**优先传文件绝对路径**，共享黑板只承载小状态（受体/位点/阳性对照/计数）：

- 工具返回里带 `molecules_file` / `properties_file` / `docking_file`（`tool_io.artifact_path()` /
  `artifact_refs()`），都是运行产物（`var/runs/<id>/<name>_tool.json`）的**绝对路径**；
- 下游工具新增同名参数：`molecular_property_assessment(molecules_file=…)`、
  `binding_mode_analysis(molecules_file=…)`、`check_binding_consistency(docking_file=…)`、
  `run_docking(molecules_file=…)`（`molecule_file` 的别名）—— 全部可留空回退黑板；
- 读取统一走归一化层：产物 JSON（`[{…}]` 或 `{"molecules":[…]}`）与用户上传的
  SDF/CSV/SMI/MOL2 同一入口（`core.ligands.read_molecules_any()`）；
- 子 Agent 提示词与协调 Agent 提示词都写明了这条纪律（`agents/prompts.py::DATA_HANDOFF_RULE`）。

### 14.3e 默认不使用示例受体/示例分子库（v0.18，产品要求）

内置示例分子库与注册表里的预置受体**只在明确调用时使用**：

| 场景 | 行为 |
| --- | --- |
| 用户上传/给出了分子 | 用用户的分子（`ligands.source` = `file`/`message`/`text`） |
| 用户**什么分子都没给** | **不使用**示例库：指令里明确写「不要擅自使用内置示例库…请向用户索取候选分子库」，`import_molecule_library` 返回 `no_molecules` → 由受理层/协调 Agent 向用户要分子 |
| 用户**明确**说「用示例库」 | 受理层确定性识别（`intake.user_requested_example_library`）→ 指令里给出 `allow_example_fallback=true`，才加载示例库 |
| 参数模式表单里选中「示例库」来源 | 界面显式传 `allow_example_fallback=true`（用户自己的选择） |

服务端默认值同步收紧：`AgentRequest.allow_example_fallback` 的默认值是
**False**（此前是 True，导致 API/CLI 调用方在没给分子时被静默换成别人的分子）。
协调 Agent 提示词里也删掉了「缺省用 thrombin/1DWC」这类默认受体点名，改为
**受体缺省时以工具返回的 notes/结果块为准，不得自行假定受体名**。

### 14.3c 附件清单不是名称来源（v0.16，真实缺陷修复）

前端发送前会把上传文件拼成

```
引用文件（本次对话已上传，可直接作为工具输入）：
- <落盘名>（小分子库，120 个分子）→ /abs/.../20260917-122453-c6b872-...-PGR_120.sdf
```

这段是**机器生成**的，落盘名形如 `<时间戳>-<哈希>-<原名>`。真实缺陷 `20260917-122453-0404`：
受理模型把路径里的哈希片段当成了「用户点名的受体 **C6B872**」，随后在线解析 not_found →
`choices` 事件 → 120 个分子一个都没对接，整个运行被问句阻断。修法分三层：

| 层 | 做法 |
| --- | --- |
| 喂给模型 | `intake._llm_instruction_view()`：附件清单只保留**文件显示名**，绝对路径整段不喂（路径本来就走结构化字段 `ligands.file` / `receptor.file`） |
| 确定性规则 | `intake.user_instruction_text()` 剥掉清单后再抽受体名（`_mentioned_receptor` / `_mentioned_accession` / `_named_receptor_candidate`） |
| LLM 合并 | `mentioned_receptor` 的逐字校验只对**用户自己写的正文**做；丢弃时写进 `llm_notes`（如「忽略受体名「C6B872」（只出现在附件路径里…）：按未点名受体处理」），**不静默** |

`INTAKE_SYSTEM_PROMPT` 第 9 条同时明确：附件清单里的文件名/路径片段不是分子名、更不是受体名。
用户真在正文里点名（`请用 EGFR 和上传的库做对接`）时行为不变，仍走 §14.1b 的自动解析链。

### 14.3d 「停止」在对话模式下也必须真停（v0.16，实测修复）

`POST /api/runs/{id}/cancel` 会置位按 run id 的协作式取消标志（`cancellation.cancel_flag`）。
多 Agent 模式原先有三个缺口，导致「界面显示已取消、服务器仍在满负荷对接」：

| 缺口 | 现象（真实取证） | 修法 |
| --- | --- | --- |
| 标志没传到对接层 | 147 分子库点停止后 load 18 → 40，分子继续算完 | `molecular_docking` 按 run id 取同一个标志传给 `dock_library(cancel_event=...)` |
| 只在 future 完成时检查 | 首批全是 118 可旋转键的大分子，十几分钟转不到检查点 | `dock_batch` 起守护线程 `_start_cancel_watcher`，置位即终止进程池 |
| 执行器重拉 worker | `terminate()` 后队列里 100+ 任务让管理线程重开 worker 继续啃 | **先** `shutdown(cancel_futures=True)` 取消队列，**再** terminate，1 s 后强杀不响应者 |
| 取消后又被重新开跑 | API 在流结束时 `clear_cancel()`，在途工具调用拿到全新未置位标志，3 s 后又起一个 147 分子池 | `molecular_docking` 见 `run.data["status"] in ("cancelled","error")` 直接返回 `status=cancelled`，零计算 |

实测结果：点击停止 **0.2 s** 内流结束、`status=cancelled`、`poses/` 文件数冻结、load 20.6 → 10.3。
单进程多线程路径（≤7 个分子）无法从外部杀掉正在跑的 Vina，日志会如实写
「当前分子在进程内对接，将在本分子结束后停止」。

### 14.4 可观测性与评估

- `GET /api/runs/{id}` → `run.task_spec`、`result.task_spec`；
- SSE `start` 事件 → `task_spec`；
- 固定报告首表 → 「任务受理」一行（任务类型 / 权威 / 决策 / 来源 / 假设）；
- 评估脚本（14 个用例，可加 `--live` 跑真实模型）：

```bash
.venv/bin/python scripts/intake_eval.py          # 确定性规则（零模型）
.venv/bin/python scripts/intake_eval.py --live   # 额外打印受理模型判定与延迟
```

### 14.5 环境变量与设置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `INTAKE_LLM` | `on` | `off` 时受理层完全走确定性规则（测试环境默认关闭，避免联网） |
| `LLM_MODEL_INTAKE` 等 | 继承全局 | 受理模型也支持按角色配置（设置页面「各 Agent 模型与调用参数」里的 `intake` 角色） |

---

## 15. SSE 事件时间戳与运行图（v0.11）

### 15.1 `ts` 字段

所有 SSE 事件都带服务端毫秒时间戳 `ts`（由 `runtime/streaming.sse_event` 统一附加）：

```json
{"type":"tool_call","tool":"run_docking","ts":1789241981216}
```

前端用它计算每个节点的**真实起止时间**，因此「并行 / 串行」是实测结果，不是画死的流程。

### 15.2 运行图如何体现并行

- 前端按节点记录每次执行的 `[start, end]`（相对本次运行起点，来源即 `ts`）；
- **条带重叠即并行**：同层（口袋分析 / 属性评估 / 对接执行 / 结合模式）两根条只要有交集，
  就是并行执行；依次排列则为串行；
- 顶栏徽标给出实测并行度：`[ 并行 ×N ]`（N = 同时执行的最大个数）或 `[ 串行 ]`；
- 时间轴每行 = 一个节点，条宽 = 真实时长，重复执行会画出多根条（同色条纹标记）。

实测样例（真实运行，`chat+advanced`）：

| 节点 | 起止（相对起点） | 与相邻节点 |
| --- | --- | --- |
| 口袋分析 | 2.2 → 11.7 s | 串行 |
| 属性评估 | 12.9 → 17.4 s | 与对接**重叠 4.29 s（并行）** |
| 对接执行 | 13.1 → 23.1 s | 同上 |
| 结合模式 | 23.9 → 29.2 s | 串行 |
| 报告生成 | 30.7 → 31.6 s | 串行 |

并行来自协调 Agent 在**同一轮**里发出多个分发工具调用（LangGraph `ToolNode` 并发执行）；
如果它选择逐个调用，时间轴自然显示为串行 —— 图只反映实际发生的事。

---

## 16. 特殊化学的处理契约（v0.11）

设计原则：**工具只报事实，化学判断交给 Agent**。工具既不能静默丢弃信息，也不能悄悄改变化学。

### 16.1 受体侧（金属 / 辅因子 / 水）

| 字段 | 含义 |
| --- | --- |
| `dropped_hetatm` | 按标准流程剔除的非水杂原子计数，如 `{"HEM":43,"SO4":10,"OXY":2}` |
| `dropped_waters` | 剔除的水分子数 |
| `kept_hetatm` | **最终受体 PDBQT 里实际存在**的白名单残基计数（以文件内容为准，不是「打算保留」） |
| `unsupported_hetatm` | 要求保留但因缺少 meeko 化学模板未能进入受体的残基（如 HEM/NAD） |
| `cocrystal_ligand` | 用于定位位点盒的共晶配体（`resname` / `n_atoms` / `center`） |

- `keep_hetatm` 保留请求若整体失败，服务端会**逐个剔除**无法参数化的残基直到准备成功，
  并把剔除名单放进 `unsupported_hetatm`；对接不会因此中断，但**不会**假装保留成功。
- 若连剔除后仍失败，接口/工具返回明确的 `error`，并给出三条补救路径
  （去掉 `keep_hetatm` 重跑 / 提供模板 SDF / 直接提供已准备的 PDBQT）。
- 实测：`ZN/MG/CA/FE/MN/CL` 等金属离子可直接保留；`HEM`、`NAD`、`GOL` 等有机残基
  需要模板（否则记为 `unsupported_hetatm`）。
- 受体是**已准备好的 `.pdbqt`**（上传时杂原子已被剔除）时，`keep_hetatm` 依然生效：
  `.site.json` 里记录了来源 PDB 的绝对路径，系统会回到原始结构重新准备。
- 位点盒来源会写清具体配体名（`共晶配体(HEM)质心`），并用「最大的一团非水非添加剂 HETATM」计算，
  离子与结晶添加剂（SO4/GOL/EDO/PEG…）不会把盒子带偏。
- 受体准备缓存以**准备后结构的内容哈希**为准（`.sha1` sidecar），既不重复跑 meeko，
  也不会在 `keep_hetatm` 变化时误用旧结果。

### 16.2 配体侧（盐 / 金属 / 电荷 / 手性）

- 每个分子对接前执行确定性化学体检，结果写进该行：`ligand_facts`、`ligand_warnings`；
  发生改写时另有 `dock_smiles` 与 `removed_fragments`（保留 `smiles` 为原始输入，作为合并主键）。
- 多片段输入（如 `CC(=O)[O-].[Na+]`）按**最大有机片段**对接，并写明移除内容与判定：
  `移除：[Na+](反离子)`；含金属配体、净电荷、未定义手性中心、MW>800 均给出告警。
- SMILES 无法解析时返回 `error` + `ligand_warnings`（原因），不静默跳过、不编造数值。
- 受体/配体两类的**化学告警都会汇总进结果 `notes`**（并内联在 `GET /api/runs/{id}` 的
  `result.notes` 中），上传受体的响应还会额外给出 `chemistry_warning`，页面以上传卡片告警样式展示。

---

## 外部工具探测

`POST /api/tools/probe`（无需请求体）

探测用户自行安装的外部工具并返回结论，与 `bash scripts/doctor.sh`、设置页「检测」按钮**同源**
（共用 `core.external_tools`）：

```json
{
  "engine": {
    "configured": true, "ok": true, "state": "ready",
    "bin": "/opt/unidock", "flavor": "unidock", "flavor_label": "Uni-Dock",
    "version_line": "Uni-Dock v1.2.0 (CUDA 12.2)", "device": 0, "batch_size": 100,
    "gpu": {"ok": true, "tool": "nvidia-smi", "devices": ["GPU 0: NVIDIA GeForce RTX 5090"]}
  },
  "p2rank": {"ok": true, "detail": "/path/prank", "hint": ""},
  "pdb2pqr": {"ok": false, "detail": "", "hint": "设置 PDB2PQR_BIN 指向可用的 pdb2pqr"}
}
```

`engine.state` 取值：`not_configured`（未提供，用内置 CPU Vina）、`ready`、`no_gpu`、
`invalid`（路径/权限问题）、`probe_failed`、`unrecognized`（无法识别引擎类型）。
除 `not_configured` 与 `ready` 之外的状态都会让对接任务**拒绝启动**并返回 `hint` 中的补齐方法。

## 标准 Agent Protocol 面（兼容子集，阶段 1）

> **这个面是什么**：它是**产品面**的一部分 —— 我们自己实现的 Agent Protocol 兼容子集
> （不是 LangGraph Platform 官方实现），服务本机网页与同机集成方；产品面的其余接口
> （产物/报告/上传/设置/历史）见本文件其它章节。
> **平台面在哪**：需要 Studio / LangGraph SDK / 未来的 durable 运行，请用
> `langgraph-deploy/`（真正的 LangGraph Platform，独立端口）。
> **两条规则**：Agent 编排能力只写在 `src/docking_agent/agents|tools`（两个面共用）；
> 协议形状只实现一份。完整决策见
> [ADR-0001](adr/0001-agent-surface.md)。

项目服务在既有 `/api/*` 之外**额外**暴露一套标准 Agent 协议面（`api/agent_service.py`），
供 LangGraph SDK / LangGraph Studio / 其它 Agent 平台直接调用。**同一份执行链路**：
同一个运行记录（`var/runs/<run_id>/`）、同一套产物与报告、同一份 store 黑板、同一个受理层。

| 分组 | 端点 |
| --- | --- |
| System | `GET /ok`、`GET /info` |
| Assistants | `POST /assistants/search`、`GET /assistants/{assistant_id}`、`GET /assistants/{assistant_id}/schemas` |
| Threads | `POST /threads`、`GET /threads/{thread_id}`、`DELETE /threads/{thread_id}`、`GET/POST /threads/{thread_id}/state`、`GET /threads/{thread_id}/history` |
| Thread Runs | `POST /threads/{thread_id}/runs/stream`、`POST /threads/{thread_id}/runs/wait`、`GET /threads/{thread_id}/runs`、`GET /threads/{thread_id}/runs/{run_id}`、`POST /threads/{thread_id}/runs/{run_id}/cancel` |
| Stateless Runs | `POST /runs/stream`、`POST /runs/wait` |

**7 个助手**（`assistant_id` 为名字派生的确定性 uuid5，`graph_id` 与图名一致）：
`coordinator`、`intake`、`property`、`pocket`、`docking`、`binding`。
入参 schema 见 `/assistants/{id}/schemas`（由 `AgentRequest` / `PipelineRequest` 直接生成，
与真实校验同一处定义）；`coordinator` 额外接受标准 `input.messages`。

### 标准 SSE 帧（`/runs/stream`、`/threads/{id}/runs/stream`）

```
event: metadata          data: {"run_id": "run_<uuid>", "attempt": 1, "thread_id": "...",
                                "assistant_id": "...", "graph_id": "..."}
event: messages/partial  data: [{"type": "AIMessageChunk", "content": "增量文本", "id": "..."}]
event: updates           data: {"<节点名>": {...}}
event: custom            data: {...}     # 领域事件：stage / molecules / progress / choices / tool_call / tool_result
event: messages/complete data: [{"type": "AIMessage", "content": "最终文本", ...}]
event: values            data: {...}     # 最终状态（含业务 run id）
event: end               data: null
event: error             data: {"error": "<code>", "message": "...", "where": {...}}
```

### run_id 语义（双 id）

- `metadata.run_id` / 返回体的 `platform_run_id`：**平台风格 uuid**（`run_<32hex>`），
  进程内句柄，用于 `cancel` 与 `GET .../runs/{id}`；
- `metadata.business_run_id` / `values.run_id`：**业务运行 id**（`var/runs/<id>/`，时间戳），
  产物、报告、`/api/runs/{id}/*` 全都按它定位；
- 取消与查询**两种 id 都接受**。

### 已知边界

- `GET /threads/{id}/state|history` 只对**被 checkpointer 记录的图**（`coordinator`）有内容；
  `intake` / 4 个子 Agent 不写图状态，返回空 state（实测 coordinator 线程：
  state 4 条消息、history 5 个检查点）。
- 线程登记落盘在 `var/threads/<thread_id>.json`（临时文件 + `os.replace` 原子写）。
- 平台 run id 是**进程内**句柄（重启后消失）；跨重启请用业务 run id。
- 旧端点 `POST /api/agent/stream` **继续可用**，但已在 OpenAPI 里标注 `deprecated=True`
  （`summary` 以 `[已废弃]` 开头），不再新增能力；流水线端点已随功能一并删除。

### 前端已切换（阶段 2）

网页端 `web/app.js` **不再直接调用** `/api/agent/stream`：

| 模式 | assistant_id | 请求 |
| --- | --- | --- |
| 对话模式 | `coordinator` | `POST /threads` → `POST /threads/{thread_id}/runs/stream` |
| 参数模式（表单权威） | `coordinator` | 同上（同一个线程登记；`input.mode=manual`） |

请求体为标准信封，业务参数全部在 `input` 内：

```json
{
  "assistant_id": "coordinator",
  "stream_mode": ["messages", "updates", "custom"],
  "input": { "mode": "chat", "message": "…", "conversation_id": "<thread_id>",
             "messages": [{"type": "human", "content": "…"}] }
}
```

前端把标准帧还原成内部事件（`metadata` → 平台 run id；`messages/partial` → `token`；
`updates` → `update`（剔除 `ts`，它不是节点名）；`messages/complete` → `final`；
`custom` → 领域事件透传（含 `start`，运行日志的「受体文件 …」就靠它）；
`values` → `done`；`error` / `end` 各自处理），因此渲染、折叠、取消、结果载入与历史
行为与改造前**完全一致**。取消先试标准端点 `POST /threads/{tid}/runs/{rid}/cancel`，
失败再退回 `/api/runs/{run_id}/cancel`。

### 哪些旧端点被标成废弃

`POST /api/agent/stream`、`POST /run`、`POST /stream_run`、`POST /cancel/{run_id}`、
`GET /health`、`GET /graph_parameter`（替代关系见各自 `summary`）。已被删除的流水线端点
（`POST /api/pipeline/stream`、`POST /pipeline`）不再出现在 OpenAPI 里。`PUT /api/settings` 的错误体现在同时带标准 `detail`
与向后兼容的 `error_message`。

### 门禁

`pytest`（含 `tests/test_agent_service.py` 的废弃标注 / tag 断言）、`scripts/lint_local.py`、
`scripts/check_web.py`、`scripts/ui_e2e.js`（189 项，含标准信封与折叠断言）、
`scripts/browser_check.py`（真实 Chromium：对话 + 参数模式都必须打标准端点，`--live`
时真跑一次后端）全部通过。

