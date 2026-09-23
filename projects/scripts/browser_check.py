#!/usr/bin/env python
"""真实浏览器（Playwright + Chromium）验证：前端确实在走标准 Agent Protocol。

为什么需要它：`scripts/ui_e2e.js` 用 jsdom，能验证 DOM 逻辑，但不跑真实浏览器的
渲染/布局与真实网络栈。本脚本用真实 Chromium 打开页面对 :端口 的服务：
  1. 对话模式发送 → 必须打 `POST /threads` + `POST /threads/{tid}/runs/stream`
     （assistant_id=coordinator），而不是老的 `/api/agent/stream`；
  2. 标准 SSE 帧（metadata / custom / updates / messages/partial /
     messages/complete / values / end）能被正确渲染；长回执出现「展开」折叠按钮且可展开；
  3. 参数模式（#manual）点「开始运行」→ 同一个标准端点，assistant_id=pipeline；
  4. `--live` 时**不打任何桩**，真跑一次后端（含真实 LLM），确认端到端可用。

用法：
    PLAYWRIGHT_BROWSERS_PATH=var/cache/ms-playwright \\
        .venv/bin/python scripts/browser_check.py http://127.0.0.1:5106
    # 真跑一次（消耗 LLM 额度）
    PLAYWRIGHT_BROWSERS_PATH=var/cache/ms-playwright \\
        .venv/bin/python scripts/browser_check.py http://127.0.0.1:5106 --live

依赖：`pip install playwright` + `python -m playwright install chromium`
（本机浏览器缓存已固定在 projects/var/cache/ms-playwright，用上面的环境变量指向它）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import gate_cleanup  # 同目录助手：python scripts/xxx.py 时脚本目录就是 sys.path[0]
BASE = "http://127.0.0.1:5106"
SHOT_DIR = Path(__file__).resolve().parent.parent / "var" / "tmp" / "browser_std"

LONG_REPLY = "\n".join(
    ["## 对接结果", ""]
    + [f"- MOL{i + 1}：亲和力 {-5.0 - i * 0.1:.2f} kcal/mol" for i in range(40)]
    + ["", "以上为长工具回执示例（超过 24 行默认折叠）。"]
)


def sse(events: List[Dict[str, Any]]) -> str:
    """内部事件 → 标准 SSE 报文（event 名 + data）。"""
    blocks: List[str] = []
    for ev in events:
        kind = str(ev.get("type") or "")
        if kind == "final":
            blocks.append(_frame("messages/complete", [{"type": "AIMessage", "content": ev.get("content", "")}]))
        elif kind == "token":
            blocks.append(_frame("messages/partial", [{"type": "AIMessageChunk", "content": ev.get("content", "")}]))
        elif kind == "update":
            blocks.append(_frame("updates", {str(ev.get("node") or "node"): {"keys": ev.get("keys") or []}}))
        elif kind == "done":
            blocks.append(_frame("values", {"run_id": ev.get("run_id"), "summary": ev.get("summary") or {}}))
        elif kind == "end":
            blocks.append(_frame("end", {}))
        else:
            blocks.append(_frame("custom", ev))
    return "".join(blocks)


def _frame(name: str, data: Any) -> str:
    return "event: " + name + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def _chat_frames(run_id: str, reply: str) -> str:
    return sse([
        {"type": "start", "run_id": run_id,
         "task_spec": {"receptor": {"name": "thrombin", "source": "default"}},
         "request": {"mode": "chat"}},
        {"type": "update", "node": "tools", "keys": ["messages"]},
        {"type": "token", "content": reply[:12]},
        {"type": "final", "content": reply},
        {"type": "done", "run_id": run_id, "summary": {"status": "ok", "molecule_count": 2}},
        {"type": "end"},
    ])


def _run_record(run_id: str) -> str:
    """普通运行的记录：必须带真实报告与排序行，否则前端会按「未执行计算」处理。"""
    row = {"rank": 1, "name": "MOL1", "smiles": "CCO", "affinity_kcal_mol": -2.8,
           "engine": "vina", "exhaustiveness": 1}
    return json.dumps({
        "run": {"run_id": run_id, "status": "ok", "molecule_count": 1, "kind": "agent"},
        "result": {"aggregates": {"total": 1}, "ranking": [row], "ranking_total": 1,
                   "molecules": [row]},
        "artifacts": [{"name": "report.md", "label": "分析报告（Markdown）"}],
        "report_markdown": "# 分子对接筛选报告\n\n表 1 报告信息\n",
        "downloads": {}, "log": [], "collaboration": {},
    }, ensure_ascii=False)


class Report:
    """极简的检查记录器（与 ui_e2e.js 的 check() 同款输出）。"""

    def __init__(self) -> None:
        self.rows: List[tuple[bool, str, str]] = []

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        self.rows.append((bool(ok), label, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f"  →  {detail}" if detail else ""))

    def summary(self) -> int:
        failed = [r for r in self.rows if not r[0]]
        print("=" * 74)
        print(f"结果：{len(self.rows) - len(failed)}/{len(self.rows)} 通过")
        for _ok, label, detail in failed:
            print(f"  - 未通过：{label}  {detail}")
        print("=" * 74)
        return 1 if failed else 0


def _install_stubs(page: Any, calls: List[Dict[str, Any]], reply: str, run_id: str,
                   no_op: Optional[List[bool]] = None) -> None:
    """只桩住运行相关端点，其余 /api/* 一律透传真实服务。

    `no_op[0] = True` 时，coordinator 的流返回 no_op 运行（受理层未受理的指令）。
    """
    no_op = no_op if no_op is not None else [False]
    def handle_threads(route: Any) -> None:
        calls.append({"method": route.request.method, "url": route.request.url, "body": {}})
        route.fulfill(status=200, headers={"Content-Type": "application/json"},
                      body=json.dumps({"thread_id": "browser-thread-1"}))

    def handle_stream(route: Any) -> None:
        try:
            body = json.loads(route.request.post_data or "{}")
        except json.JSONDecodeError:
            body = {}
        calls.append({"method": route.request.method, "url": route.request.url, "body": body})
        if body.get("assistant_id") == "coordinator" and no_op[0]:
            route.fulfill(status=200, headers={"Content-Type": "text/event-stream"},
                          body=sse([{"type": "start", "run_id": "E2E-BROWSER-NOOP"},
                                    {"type": "final", "content": "你好！请告诉我候选分子与受体目标。"},
                                    {"type": "done", "run_id": "E2E-BROWSER-NOOP",
                                     "summary": {"status": "no_op"}},
                                    {"type": "end"}]))
            return
        frames = _chat_frames(run_id, reply) if body.get("assistant_id") == "coordinator" else sse([
            {"type": "start", "run_id": run_id, "task_spec": {"task_type": "screening"}},
            {"type": "update", "node": "docking", "keys": ["messages"]},
            {"type": "done", "run_id": run_id, "summary": {"status": "ok", "molecule_count": 2}},
            {"type": "end"},
        ])
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=frames)

    def handle_run_record(route: Any) -> None:
        route.fulfill(status=200, headers={"Content-Type": "application/json"},
                      body=_run_record(run_id))

    page.route("**/threads", handle_threads)
    page.route("**/runs/stream", handle_stream)
    page.route("**/api/runs/E2E-BROWSER*", handle_run_record)


def _standard_calls(calls: List[Dict[str, Any]], assistant: str) -> List[Dict[str, Any]]:
    return [c for c in calls
            if "/runs/stream" in c["url"] and c["body"].get("assistant_id") == assistant]


def _standard_call(calls: List[Dict[str, Any]], assistant: str) -> Optional[Dict[str, Any]]:
    found = _standard_calls(calls, assistant)
    return found[-1] if found else None


def run_report(page: Any, rep: Report, run_id: str) -> None:
    """报告版式验收（真实渲染）：结构卡在数据旁、备注折叠、失败占位不误报。"""
    page.goto(BASE + "/advanced#chat", wait_until="domcontentloaded")
    page.wait_for_selector("#chat-input")
    page.evaluate(f"loadRun('{run_id}', {{silent: true}})")
    page.wait_for_timeout(1500)
    page.evaluate("switchTab('report')")
    page.wait_for_timeout(1200)
    # 触发懒加载：滚到底再回顶，等图片全部就绪
    page.evaluate("""async () => {
        const box = document.getElementById('report-box');
        for (let y = 0; y <= box.scrollHeight; y += 600) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 60)); }
        window.scrollTo(0, 0);
    }""")
    page.wait_for_timeout(1500)

    cards = page.eval_on_selector_all(
        "#report-box img[src*='recommend_card']",
        "els => els.map(e => ({ src: e.getAttribute('src'), ok: e.complete && e.naturalWidth > 0 }))")
    rep.check(len(cards) >= 1 and all(c["ok"] for c in cards),
              "推荐分子 2D 结构卡内嵌并成功加载（结构就在数据旁边）",
              f"{len(cards)} 张 | 失败 {[c['src'].split('/')[-1] for c in cards if not c['ok']]}")

    # 结构图必须紧跟在对应分子的标题之后（顺序即「挨着」），而不是集中在末尾的网格图
    order = page.evaluate("""() => {
        const sel = "#report-box h3, #report-box h4, #report-box h5, #report-box h6,"
                  + " #report-box img[src*=recommend_card], #report-box img[src*=pose_3d]";
        return Array.from(document.querySelectorAll(sel)).map(n => n.tagName === 'IMG'
            ? 'IMG:' + (n.getAttribute('src') || '').split('/').pop()
            : 'H:' + (n.textContent || '').trim().slice(0, 24));
    }""")
    card_idx = [i for i, item in enumerate(order) if item.startswith("IMG:recommend_card")]
    paired = all(i > 0 and order[i - 1].startswith("H:") for i in card_idx)
    first_pose = next((i for i, item in enumerate(order) if item.startswith("IMG:pose_3d")), len(order))
    rep.check(bool(card_idx) and paired and max(card_idx) < first_pose,
              "每张结构卡都紧跟在对应分子标题之后（不是集中在末尾的网格图）",
              " → ".join(order[:5])[:180])

    broken = page.eval_on_selector_all(
        "#report-box .md-img-fallback",
        "els => els.filter(e => e.offsetParent !== null || e.clientHeight > 0).length")
    rep.check(broken == 0, "报告里没有「图片加载失败」误报（降级文案仅在真失败时出现）",
              f"可见降级文案 {broken} 处")
    images = page.eval_on_selector_all(
        "#report-box img", "els => els.map(e => ({ok: e.complete && e.naturalWidth > 0}))")
    rep.check(all(i["ok"] for i in images), "报告内所有图片都真的加载出来了",
              f"{sum(1 for i in images if i['ok'])}/{len(images)}")
    page.screenshot(path=str(SHOT_DIR / "report_cards.png"))

    page.evaluate("switchTab('overview')")
    page.wait_for_timeout(500)
    heights = page.eval_on_selector_all(
        "#run-notes .run-note-text", "els => els.map(e => Math.round(e.getBoundingClientRect().height))")
    rep.check(bool(heights) and max(heights) <= 48,
              "运行笔记折叠成 ≤2 行（超长备注不再拉长页面）",
              f"{len(heights)} 条，最高 {max(heights) if heights else 0}px")
    page.screenshot(path=str(SHOT_DIR / "overview_notes.png"))


def run_layout(page: Any, rep: Report) -> None:
    """布局验收（只有真实浏览器能测）：对话居中放大、首屏无空控件、设置页能返回。"""
    page.goto(BASE + "/advanced#chat", wait_until="domcontentloaded")
    page.wait_for_selector("#chat-input")
    page.wait_for_timeout(600)
    metrics = page.evaluate(
        """() => {
            const box = (sel) => { const e = document.querySelector(sel);
                if (!e) return null; const r = e.getBoundingClientRect();
                return { top: r.top, left: r.left, right: r.right, width: r.width, height: r.height }; };
            const layout = box('#workbench-layout');
            const stream = box('.chat-stream');
            const details = document.querySelector('#run-details');
            const view = document.querySelector('#view-workbench');
            return { vw: window.innerWidth, vh: window.innerHeight, layout, stream,
                     left: box('#config-panel'), center: box('#workbench-center'),
                     right: box('.main-col'), log: box('#log-box'), molecules: box('#molecules-tbody'),
                     orch: box('#orchestration-panel'),
                     scrollW: document.documentElement.scrollWidth,
                     detailsOpen: details ? details.open : null,
                     noRun: view ? view.classList.contains('no-run') : null,
                     toolbarDisplay: getComputedStyle(document.querySelector('#ranking-toolbar')).display };
        }""")
    vh = metrics["vh"]
    stream = metrics["stream"] or {"height": 0}
    rep.check(stream["height"] >= vh * 0.4,
              "对话区占首屏 ≥40%（对话框大一些）",
              f"{round(stream['height'])}px / 视口 {vh}px")
    left_col = metrics.get("left") or {}
    center_col = metrics.get("center") or {}
    right_col = metrics.get("right") or {}
    rep.check(bool(left_col) and bool(center_col) and bool(right_col)
              and left_col["right"] <= center_col["left"] + 1
              and center_col["right"] <= right_col["left"] + 1,
              "三栏并排：左=参数设置 / 中=对话与结果 / 右=运行详情（互不重叠）",
              f"左 {round(left_col.get('right', 0))}px ≤ 中 {round(center_col.get('left', 0))}px ≤ "
              f"右 {round(right_col.get('left', 0))}px")
    rep.check(float(metrics.get("scrollW") or 0) <= float(metrics["vw"]) + 1,
              "三栏布局无横向滚动",
              f"scrollWidth {metrics.get('scrollW')} / 视口 {metrics['vw']}")
    rep.check(bool(metrics.get("log")) and (metrics["log"]["width"] or 0) > 120,
              "运行详情（阶段日志 / 实时逐分子结果）在对话下方可见",
              f"log 宽 {round((metrics.get('log') or {}).get('width', 0))}px")
    rep.check(metrics.get("orch") is not None and right_col.get("width", 0) > 100,
              "右栏保留多 Agent 编排",
              f"右栏宽 {round(right_col.get('width', 0))}px")
    rep.check(metrics["detailsOpen"] is False, "运行详情默认收起")
    rep.check(metrics["noRun"] is True and metrics["toolbarDisplay"] == "none",
              "首屏收起空排序工具条（无结果时不铺开空控件）",
              f"no-run={metrics['noRun']} toolbar={metrics['toolbarDisplay']}")
    page.screenshot(path=str(SHOT_DIR / "layout_chat.png"), full_page=False)

    page.goto(BASE + "/advanced#/settings", wait_until="domcontentloaded")
    page.wait_for_selector("#btn-settings-back")
    page.click("#btn-settings-back")
    page.wait_for_timeout(500)
    rep.check(page.evaluate("document.body.dataset.view") == "workbench"
              and page.evaluate("document.getElementById('view-workbench').classList.contains('is-active')"),
              "设置页「← 返回工作台」真的切回工作台")
    page.screenshot(path=str(SHOT_DIR / "layout_settings_back.png"), full_page=False)


def run_chat(page: Any, rep: Report, calls: List[Dict[str, Any]]) -> None:
    page.goto(BASE + "/advanced#chat", wait_until="domcontentloaded")
    page.wait_for_selector("#chat-input")
    page.fill("#chat-input", "用上传的受体跑一次对接筛选")
    page.click("#chat-send")
    page.wait_for_function(
        "() => { const b = document.querySelector('.chat-msg.assistant .chat-body');"
        " return b && b.textContent.indexOf('对接结果') >= 0; }",
        timeout=20000)
    rep.check(page.evaluate("document.getElementById('run-details').open") is True,
              "开跑后「运行详情」自动展开（日志 / 轨迹可见）")
    body_text = page.inner_text(".chat-msg.assistant .chat-body")
    rep.check("对接结果" in body_text, "标准 messages/complete 正文渲染进助手气泡",
              body_text[:60].replace("\n", " "))
    # 用户要求：对话气泡**不再默认折叠** → 长回执必须全文直接显示，且没有折叠按钮
    rep.check(not page.query_selector(".chat-fold-toggle") and not page.query_selector(".chat-body.chat-fold"),
              "长回执默认全文显示（不再有「展开」折叠按钮）")
    rep.check(len(body_text) > 800, f"正文确实很长但未被截断（{len(body_text)} 字符）")
    rep.check(any(c["method"] == "POST" and c["url"].rstrip("/").endswith("/threads") for c in calls),
              "先建线程（POST /threads）", _urls(calls))
    call = _standard_call(calls, "coordinator")
    rep.check(call is not None, "对话走标准运行端点（assistant_id=coordinator）", _urls(calls))
    if call:
        rep.check("messages" in (call["body"].get("stream_mode") or []),
                  "标准 stream_mode 含 messages", json.dumps(call["body"].get("stream_mode")))
        rep.check(not any("/api/agent/stream" in c["url"] for c in calls),
                  "不再打老端点 /api/agent/stream")
        inp = (call["body"].get("input") or {})
        rep.check(all(inp.get(key) is None for key in ("receptor", "site_center", "site_size")),
                  "什么都没写 → 不发送 receptor / site_center / site_size",
                  json.dumps({key: inp.get(key) for key in ("receptor", "site_center", "site_size")},
                             ensure_ascii=False))
    # 气泡小票必须与请求体同源：没改动就不能写「已下发受体 / 位点盒」（用户实测反馈）
    user_text = page.inner_text(".chat-msg.user .chat-bubble").replace("\n", " ")
    rep.check("纯指令" in user_text and "位点" not in user_text and "受体 " not in user_text,
              "什么都没改 → 气泡不谎报「受体 / 位点盒」已下发", user_text[:120])

    # 第二阶段：改动「搜索强度」→ 只下发它，小票如实列出它（改动即下发 = 所见即所用）
    before = len(_standard_calls(calls, "coordinator"))
    page.uncheck("#exhaustiveness-auto")
    page.fill("#exhaustiveness", "24")
    page.dispatch_event("#exhaustiveness", "input")
    page.fill("#chat-input", "搜索强度用 24，再跑一次")
    page.click("#chat-send")
    page.wait_for_function(
        "() => { const b = document.querySelector('.chat-msg.assistant .chat-body');"
        " return b && b.textContent.indexOf('对接结果') >= 0; }",
        timeout=20000)
    page.wait_for_timeout(300)
    calls2 = _standard_calls(calls, "coordinator")
    rep.check(len(calls2) == before + 1, "改动参数后确实又提交了一轮", f"{before} → {len(calls2)}")
    last = calls2[-1]["body"].get("input") if calls2 else {}
    rep.check(str(last.get("exhaustiveness")) == "24" and last.get("advanced") is True,
              "改动过的 exhaustiveness=24 真的下发（advanced=true）",
              json.dumps({"exhaustiveness": last.get("exhaustiveness"),
                          "advanced": last.get("advanced")}, ensure_ascii=False))
    rep.check(all(last.get(key) is None for key in ("receptor", "site_center", "site_size")),
              "改动参数后仍然不发送 receptor / site_center / site_size")
    chips = page.eval_on_selector_all(
        ".chat-msg.user .chat-bubble",
        "els => els.length ? els[els.length - 1].textContent : ''").replace("\n", " ")
    rep.check("exhaustiveness=24" in chips and "位点" not in chips and "受体 " not in chips,
              "小票列出改动过的项、且不含未下发的受体/位点", chips[:140])
    assistant_text = page.eval_on_selector(".chat-msg.assistant .chat-body", "el => el.textContent")
    rep.check(page.locator(".chat-msg.assistant .md-root h2").count() >= 1, "高级模式回执按 Markdown 渲染")
    rep.check("##" not in assistant_text, "高级模式正文不再显示 Markdown 源码符号", assistant_text[:80])
    page.screenshot(path=str(SHOT_DIR / "chat.png"), full_page=False)


def run_no_op(page: Any, rep: Report, calls: List[Dict[str, Any]]) -> None:
    """「不是可执行任务」的运行（例如只说了句「你好」）：不得挂空报告、不得说「运行完成」。"""
    calls.clear()
    page.route("**/api/runs/E2E-BROWSER-NOOP", lambda route: route.fulfill(
        status=200, headers={"Content-Type": "application/json"},
        body=json.dumps({"run": {"run_id": "E2E-BROWSER-NOOP", "status": "no_op",
                                 "molecule_count": 0, "kind": "agent"},
                         "result": {"ranking": [], "ranking_total": 0, "aggregates": {},
                                    "molecules": []},
                         "artifacts": [], "report_markdown": "", "downloads": {},
                         "log": [], "collaboration": {}}, ensure_ascii=False)))
    page.goto(BASE + "/advanced#chat", wait_until="domcontentloaded")
    page.wait_for_selector("#chat-input")
    page.fill("#chat-input", "你好")
    page.click("#chat-send")
    page.wait_for_function(
        "() => (document.getElementById('run-hint') || {}).textContent"
        " && document.getElementById('run-hint').textContent.indexOf('未执行计算') >= 0",
        timeout=20000)
    page.wait_for_timeout(300)
    last = page.eval_on_selector_all(
        ".chat-msg.assistant",
        "els => { const n = els[els.length - 1]; return n ? { html: n.innerHTML,"
        " text: n.querySelector('.chat-bubble').textContent } : { html: '', text: '' }; }")
    rep.check("chat-report" not in last["html"] and "规范报告" not in last["text"],
              "未执行计算的运行不挂「规范报告」小节（只看本轮气泡）", last["text"][:120])
    bubble = last["text"].replace("\n", " ")
    rep.check("本次未执行计算（未生成报告）" in bubble,
              "气泡如实说明「本次未执行计算（未生成报告）」", bubble[:140])
    log_text = page.inner_text("#log-box").replace("\n", " ")
    rep.check("未执行计算" in log_text and "运行完成" not in log_text,
              "运行日志不写「运行完成」", log_text[:140])
    page.screenshot(path=str(SHOT_DIR / "no_op.png"), full_page=False)


def run_manual(page: Any, rep: Report, calls: List[Dict[str, Any]]) -> None:
    page.goto(BASE + "/advanced#manual", wait_until="domcontentloaded")
    page.wait_for_selector("#btn-start")
    # 用户面已经没有预置受体下拉：受体只能由 PDB/UniProt/名称/上传指定。
    # 这里填一个 PDB 编号（4HHB = 血红蛋白，非注册表预置受体）。
    if page.query_selector("#receptor-source") is not None:
        page.fill("#receptor-source", "4HHB")
        page.dispatch_event("#receptor-source", "input")
    # 参数模式至少要有一个分子来源：用 SMILES 文本框，避免依赖示例库解析
    page.fill("#ligands-text", "CCO,CCN,CCC")
    page.dispatch_event("#ligands-text", "input")
    page.click("#btn-start")
    page.wait_for_timeout(2500)
    call = _standard_call(calls, "coordinator")
    hint = ""
    for sel in ("#exec-error", "#run-hint"):
        node = page.query_selector(sel)
        if node is not None and (node.inner_text() or "").strip():
            hint += f" {sel}={node.inner_text().strip()[:120]}"
    rep.check(call is not None, "参数模式走标准运行端点（assistant_id=coordinator）",
              (_urls(calls) or "（无请求）") + hint)
    if call:
        rep.check("messages" in (call["body"].get("stream_mode") or []),
                  "参数模式同样声明 stream_mode", json.dumps(call["body"].get("stream_mode")))
    rep.check(not any("/api/pipeline" in c["url"] for c in calls),
              "不再打已删除的流水线端点")
    page.screenshot(path=str(SHOT_DIR / "manual.png"), full_page=False)


def run_simple(page: Any, rep: Report, calls: List[Dict[str, Any]]) -> None:
    """简易模式（/simple）：只有对话与结果，且必须走同一套标准 Agent Protocol。

    用户要求：简易模式要「新手也能直接用」——因此这里断言的是**可操作性**而不是外观：
    背景不挡点击、发送按钮能跑、选项能点、结果区能载入；外观另存截图供人工复核。
    """
    calls.clear()
    # **默认首页就是简易模式**（/ 直出 simple.html）；/advanced 才是高级模式
    page.goto(BASE + "/", wait_until="networkidle")
    rep.check(page.locator("#s-log").count() == 1, "默认首页是简易模式")
    # 刷新 = 干净一页：结果区不得残留上一次运行（用户反馈）
    page.evaluate("() => localStorage.setItem('dsa.simple.lastRun', '20260922-171953-2502')")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(600)
    rep.check(page.locator("#s-hits .s-hit").count() == 0 and page.locator("#s-actions").is_hidden(),
              "刷新后结果区保持空态（不自动载入上一次结果）")
    rep.check(page.locator("#s-runs").input_value() == "", "「历史运行」下拉默认停在占位项")
    page.goto(BASE + "/simple", wait_until="networkidle")
    rep.check(page.locator("#s-log").count() == 1, "别名 /simple 仍可用")
    page.goto(BASE + "/advanced", wait_until="networkidle")
    rep.check(page.locator("#view-workbench").count() == 1, "高级模式迁到 /advanced")
    page.goto(BASE + "/", wait_until="networkidle")
    rep.check(page.locator("#s-hits").count() == 1 and page.locator("#s-kpi").count() == 1, "简易模式有结果区")

    # 顶栏双向导航：简易 ⇄ 高级
    links = page.eval_on_selector_all(".s-nav-link", "els => els.map(e => e.getAttribute('href'))")
    rep.check("/" in links and "/advanced" in links, f"简易模式顶栏可切换两套界面：{links}")
    rep.check(page.locator('a[href="/advanced"][title]').count() >= 1, "高级模式入口带用途说明")

    # 动态背景：存在、不拦截点击（新手点不动按钮是最致命的可用性缺陷）
    rep.check(page.locator(".bg-anim .bg-blob").count() >= 3, "动态背景含多个光斑层")
    bg_pointer = page.eval_on_selector(".bg-anim", "el => getComputedStyle(el).pointerEvents")
    rep.check(bg_pointer == "none", f"动态背景不吃点击（pointer-events={bg_pointer}）")
    hit = page.evaluate("""() => {
        const b = document.getElementById('s-send');
        const r = b.getBoundingClientRect();
        const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return top ? (top.id || top.className) : '';
    }""")
    rep.check("s-send" in str(hit), f"发送按钮在最上层（实际命中 {hit}）")

    # 参数：简易模式不暴露任何参数控件，请求体必须是 chat + advanced=false
    rep.check(page.locator("#exhaustiveness, #protonation-select, #site-center").count() == 0, "简易模式没有参数表单")

    page.fill("#s-input", "筛选阿司匹林和布洛芬对 EGFR 的结合")
    page.click("#s-send")
    page.wait_for_timeout(900)
    stream_calls = _standard_calls(calls, "coordinator")
    rep.check(bool(stream_calls), "简易模式走标准端点 POST /threads/{tid}/runs/stream")
    rep.check(any(c["url"].rstrip("/").endswith("/threads") for c in calls), "首次运行前注册线程")
    if stream_calls:
        payload = stream_calls[0]["body"]
        fields = payload.get("input") or {}
        rep.check(fields.get("mode") == "chat" and fields.get("advanced") is False,
                  f"请求固定 chat + advanced=false（实际 mode={fields.get('mode')}, "
                  f"advanced={fields.get('advanced')}）")
        leaked = [k for k in ("exhaustiveness", "protonation", "site_center", "site_size",
                              "engine", "max_ligands")
                  if fields.get(k) not in (None, "", [])]
        rep.check(leaked == [], f"请求不注入任何参数（实际带了 {leaked}）")

    # 回执与结果：桩里给了长文本与一份运行记录 → 对话区有内容、结果区有推荐榜
    page.wait_for_timeout(1200)
    log_text = page.inner_text("#s-log")
    rep.check("MOL1" in log_text or "对接结果" in log_text,
              f"助手回执渲染到对话区（{len(log_text)} 字符）")
    # 交互逻辑（用户反馈「Markdown 没渲染」）：回执按 Markdown 渲染，正文里不得再出现源码符号
    rep.check(page.locator("#s-log .s-msg-bot .md-root h2").count() >= 1,
              "简易模式助手回执按 Markdown 渲染（## → <h2>）")
    rep.check(page.locator("#s-log .s-msg-bot .md-root ul li").count() >= 5, "Markdown 列表渲染成 <ul><li>")
    rep.check("##" not in log_text and "**" not in log_text, "对话区不再显示 Markdown 源码符号")
    page.wait_for_timeout(800)
    rep.check(page.locator("#s-hits .s-hit").count() >= 1, "结果区渲染出推荐分子")
    rep.check(page.locator("#s-report").get_attribute("href", timeout=2000)
              and "report.pdf" in (page.locator("#s-report").get_attribute("href") or ""),
              "结果区给出「查看完整报告」链接")
    page.screenshot(path=str(SHOT_DIR / "simple.png"), full_page=True)

    # ---- 回归：思考（thinking）必须**折叠**在气泡里，绝不混进正文（避免刷屏） ----
    calls.clear()
    def handle_think_stream(route: Any) -> None:
        calls.append({"method": route.request.method, "url": route.request.url, "body": {}})
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=sse([
            {"type": "start", "run_id": "E2E-THINK"},
            {"type": "thinking", "content": "先看靶点口袋，再决定盒子；" * 6},
            {"type": "token", "content": "结论：推荐 MOL1。"},
            {"type": "final", "content": "结论：推荐 MOL1。"},
            {"type": "done", "run_id": "E2E-THINK", "summary": {"status": "ok"}},
            {"type": "end"}]))
    page.route("**/runs/stream", handle_think_stream)
    page.fill("#s-input", "带思考再跑一次")
    page.click("#s-send")
    page.wait_for_timeout(1200)
    think = page.locator(".s-think")
    rep.check(think.count() >= 1, "思考内容渲染成独立的「思考」块")
    if think.count():
        body_hidden = page.eval_on_selector(".s-think .s-think-body", "el => el.classList.contains('hidden')")
        rep.check(body_hidden, "思考块收起成一行（不占屏）")
        toggle = page.locator(".s-think .s-think-toggle").first
        label = toggle.inner_text() or ''
        rep.check("思考" in label, f"折叠按钮标注「思考」：{label!r}")
        rep.check("用时" in label, f"正文到达后自动收起并记下用时：{label!r}")
        toggle.click()
        page.wait_for_timeout(200)
        rep.check(not page.eval_on_selector(".s-think .s-think-body",
                                           "el => el.classList.contains('hidden')"),
                  "点一下能展开看推理全文")
        answer = page.inner_text("#s-log")
        rep.check("先看靶点口袋" not in answer.split("结论：推荐 MOL1。")[0].replace(
            page.inner_text(".s-think .s-think-body"), ""), "推理不得出现在正文里")

    # ---- 回归：长回答**不再默认折叠**（用户要求：气泡直接全文显示） ----
    rep.check(page.locator(".s-fold-toggle, .s-body.s-folded").count() == 0,
              "简易模式长回答不再折叠（没有「展开全文」按钮 / 折叠类）")
    long_len = page.eval_on_selector_all(
        "#s-log .s-msg-bot .s-body",
        "els => els.map(e => e.textContent.replace(/\\s/g, '').length)")
    rep.check(long_len and max(long_len) > 800, f"长回执确实完整显示（最长 {max(long_len) if long_len else 0} 字符）")

    # ---- 回归：多组分分子的「代表结构」点选必须作为**请求字段**续跑 ----
    # 真实缺陷（用户反馈「我选择了，但没有正常工作」）：点选只把选项文案当普通消息发回，
    # 后端于是按名称重新查询、又是多组分 → 再问一次；追问若丢了受体还会被判 ask。
    calls.clear()
    state = {"streams": 0}

    def handle_choice_stream(route: Any) -> None:
        body = json.loads(route.request.post_data or "{}")
        calls.append({"method": route.request.method, "url": route.request.url, "body": body})
        state["streams"] += 1
        if state["streams"] == 1:
            route.fulfill(status=200, headers={"Content-Type": "text/event-stream"},
                          body=sse([{"type": "start", "run_id": "E2E-CHOICE"},
                                    {"type": "choices",
                                     "choices": [{"id": "molecule:3034368:raw", "kind": "molecule",
                                                  "label": "Mancozeb（CID 3034368）· 原始多组分结构",
                                                  "value": "CCO.[Zn+2]", "prompt": "按原始多组分继续",
                                                  "detail": {"mode": "raw-mixture"}}],
                                     "note": "需要确认："},
                                    {"type": "final", "content": "请在下方选项中点选代表结构。"},
                                    {"type": "done", "run_id": "E2E-CHOICE",
                                     "summary": {"status": "needs_user_input"}},
                                    {"type": "end"}]))
            return
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"},
                      body=_chat_frames("E2E-CHOICE", "已按你选定的代表结构继续。"))

    page.route("**/runs/stream", handle_choice_stream)
    page.fill("#s-input", "代森锰锌")
    page.click("#s-send")
    page.wait_for_timeout(900)
    rep.check(page.locator(".s-choice-btn").count() >= 1, "多组分选项渲染成按钮")
    page.locator(".s-choice-btn").first.click()
    page.wait_for_timeout(1200)
    followups = [c["body"].get("input") or {} for c in calls if "/runs/stream" in c["url"]]
    rep.check(len(followups) >= 2, f"点选后确实发起了续跑（{len(followups)} 次）")
    if len(followups) >= 2:
        last = followups[-1]
        rep.check(last.get("molecule_choice") == "CCO.[Zn+2]",
                  f"续跑带上所选代表结构的 SMILES：{last.get('molecule_choice')!r}")
        rep.check(last.get("molecule_choice_decision") == "raw-mixture",
                  f"续跑带上取法（raw-mixture）：{last.get('molecule_choice_decision')!r}")
        rep.check(bool(last.get("molecule_choice_label")),
                  "续跑带上展示名（报告可追溯）")
        rep.check(not last.get("molecule_choice") == ""
                  and last.get("conversation_id") is not None, "续跑仍在同一会话里")


def run_live(page: Any, rep: Report, calls: List[Dict[str, Any]]) -> None:
    """不打桩：真跑一次后端（含真实 LLM），只验证链路与最终渲染。"""
    page.goto(BASE + "/advanced#chat", wait_until="domcontentloaded")
    page.wait_for_selector("#chat-input")
    page.fill("#chat-input", "用预置受体 thrombin、对接盒 center=31.5,13.74,24.36 / size=22,22,22，对乙醇 CCO 做一次完整筛选：导入 → 属性评估 → 对接 → 推荐排行 → 生成报告。exhaustiveness=1、n_poses=1，直接执行，不要问我。")
    page.click("#chat-send")
    try:
        # 等**事件流真正结束**（而不是只等气泡里出现占位文案）
        page.wait_for_function(
            "() => { const log = document.getElementById('log-box');"
            " const t = log ? log.textContent : '';"
            " const hint = document.getElementById('run-hint');"
            " const h = hint ? hint.textContent : '';"
            " return t.indexOf('事件流已结束') >= 0 || t.indexOf('运行失败') >= 0"
            " || t.indexOf('请求失败') >= 0 || h.indexOf('运行完成') >= 0"
            " || h.indexOf('载入结果失败') >= 0; }",
            timeout=600000)
    except Exception as exc:  # noqa: BLE001 - 需要把超时当失败记录而不是崩溃
        rep.check(False, "真实 LLM 运行跑完（事件流结束）", f"{type(exc).__name__}: {exc}")
        return
    page.wait_for_timeout(500)
    text = page.inner_text(".chat-msg.assistant .chat-body")
    rep.check(len(text.strip()) > 1 and "本次运行没有产生文本输出" not in text,
              "真实 LLM 运行产生文本输出", text[:80].replace("\n", " "))
    run_label = page.inner_text("#exec-run-id") if page.query_selector("#exec-run-id") else ""
    rep.check(bool(run_label) and "—" not in run_label, "真实运行拿到业务 run_id", run_label)
    call = _standard_call(calls, "coordinator")
    rep.check(call is not None, "真实运行同样走标准端点", _urls(calls))
    rep.check(not any("/api/agent/stream" in c["url"] for c in calls), "真实运行未打老端点")
    page.screenshot(path=str(SHOT_DIR / "live.png"), full_page=False)


def _urls(calls: List[Dict[str, Any]]) -> str:
    return " | ".join(f"{c['method']} {c['url'].split('127.0.0.1')[-1]}" for c in calls[-6:])


def _json_or_empty(raw: Optional[str]) -> Dict[str, Any]:
    """请求体可能不是 JSON（FormData / 空）——解析失败就当成空对象，绝不能因此崩掉检查。"""
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main(argv: List[str]) -> int:
    global BASE
    args = [a for a in argv[1:] if not a.startswith("--")]
    live = "--live" in argv
    if args:
        BASE = args[0].rstrip("/")
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH",
                          str(Path(__file__).resolve().parent.parent / "var" / "cache" / "ms-playwright"))
    try:
        # 允许延迟：playwright 是可选开发依赖，未安装时给出可操作的提示而不是 ImportError 崩栈
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：.venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium")
        return 2

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    gate_runs_before = gate_cleanup.run_ids(BASE)
    rep = Report()
    calls: List[Dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 980})
        errors: List[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        if live:
            # 真跑模式没有桩，必须在网络层记录请求才能断言「走的是标准端点」
            page.on("request", lambda req: calls.append({
                "method": req.method, "url": req.url, "body": _json_or_empty(req.post_data)}))
        if not live:
            no_op = [False]
            from browser_choice_checks import (choices_are_deferred, limit_event_is_informational,
                                               simple_choice_guard, two_kinds_coexist)
            _install_stubs(page, calls, LONG_REPLY, "E2E-BROWSER-CHAT", no_op)
            print("--- 真实浏览器 · 布局（居中 / 放大 / 首屏无空控件 / 返回） ---")
            run_layout(page, rep)
            print("--- 真实浏览器 · 对话模式（标准帧桩） ---")
            run_chat(page, rep, calls)
            print("--- 真实浏览器 · 不是可执行任务（no_op） ---")
            no_op[0] = True
            run_no_op(page, rep, calls)
            no_op[0] = False
            calls.clear()
            print("--- 真实浏览器 · 简易模式（标准帧桩） ---")
            run_simple(page, rep, calls)
            calls.clear()
            print("--- 真实浏览器 · 参数模式（标准帧桩） ---")
            run_manual(page, rep, calls)
            print("--- 真实浏览器 · 候选选择交互（两问互不覆盖 / 运行中点不动） ---")
            two_kinds_coexist(page, rep, calls, BASE)
            simple_choice_guard(page, rep, BASE)
            choices_are_deferred(page, rep, BASE)
            limit_event_is_informational(page, rep, BASE)
            report_run = os.environ.get("BROWSER_CHECK_REPORT_RUN") or ""
            if report_run:
                print("--- 真实浏览器 · 报告版式（结构卡 / 备注 / 图片） ---")
                run_report(page, rep, report_run)
        else:
            print("--- 真实浏览器 · 对话模式（真跑后端 + 真实 LLM） ---")
            run_live(page, rep, calls)
        rep.check(not errors, "浏览器无 JS 运行时错误", "; ".join(errors[:3]))
        browser.close()
    deleted, created = gate_cleanup.cleanup(BASE, gate_runs_before)
    rep.check(deleted == created, f"门禁自清理：删除本次创建的运行记录（{deleted}/{created} 条）")
    return rep.summary()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
