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
    page.goto(BASE + "/#chat", wait_until="domcontentloaded")
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
    page.goto(BASE + "/#chat", wait_until="domcontentloaded")
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
                     detailsOpen: details ? details.open : null,
                     noRun: view ? view.classList.contains('no-run') : null,
                     toolbarDisplay: getComputedStyle(document.querySelector('#ranking-toolbar')).display };
        }""")
    vh = metrics["vh"]
    stream = metrics["stream"] or {"height": 0}
    layout = metrics["layout"] or {"left": 0, "right": 0, "width": 0}
    rep.check(stream["height"] >= vh * 0.4,
              "对话区占首屏 ≥40%（对话框大一些）",
              f"{round(stream['height'])}px / 视口 {vh}px")
    left_gap = layout["left"]
    right_gap = metrics["vw"] - layout["right"]
    rep.check(abs(left_gap - right_gap) <= 24 and layout["width"] <= 1300,
              "工作台单列居中（左右留白对称、内容宽度收敛）",
              f"左 {round(left_gap)}px / 右 {round(right_gap)}px / 宽 {round(layout['width'])}px")
    rep.check(metrics["detailsOpen"] is False, "运行详情默认收起")
    rep.check(metrics["noRun"] is True and metrics["toolbarDisplay"] == "none",
              "首屏收起空排序工具条（无结果时不铺开空控件）",
              f"no-run={metrics['noRun']} toolbar={metrics['toolbarDisplay']}")
    page.screenshot(path=str(SHOT_DIR / "layout_chat.png"), full_page=False)

    page.goto(BASE + "/#/settings", wait_until="domcontentloaded")
    page.wait_for_selector("#btn-settings-back")
    page.click("#btn-settings-back")
    page.wait_for_timeout(500)
    rep.check(page.evaluate("document.body.dataset.view") == "workbench"
              and page.evaluate("document.getElementById('view-workbench').classList.contains('is-active')"),
              "设置页「← 返回工作台」真的切回工作台")
    page.screenshot(path=str(SHOT_DIR / "layout_settings_back.png"), full_page=False)


def run_chat(page: Any, rep: Report, calls: List[Dict[str, Any]]) -> None:
    page.goto(BASE + "/#chat", wait_until="domcontentloaded")
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
    rep.check(bool(page.query_selector(".chat-fold-toggle")),
              "长回执出现「展开」折叠按钮")
    if page.query_selector(".chat-fold-toggle"):
        page.click(".chat-fold-toggle")
        page.wait_for_timeout(120)
        rep.check(page.inner_text(".chat-fold-toggle") == "收起" and
                  not page.query_selector(".chat-body.chat-fold"),
                  "点「展开」后折叠类移除、按钮变「收起」")
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
    page.goto(BASE + "/#chat", wait_until="domcontentloaded")
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


def run_pipeline(page: Any, rep: Report, calls: List[Dict[str, Any]]) -> None:
    page.goto(BASE + "/#manual", wait_until="domcontentloaded")
    page.wait_for_selector("#btn-start")
    select = page.query_selector("#receptor-select")
    if select is not None:
        options = [o.get_attribute("value") for o in select.query_selector_all("option")]
        values = [v for v in options if v]
        if values:
            page.select_option("#receptor-select", values[0])
    # 参数模式至少要有一个分子来源：用 SMILES 文本框，避免依赖示例库解析
    page.fill("#ligands-text", "CCO,CCN,CCC")
    page.dispatch_event("#ligands-text", "input")
    page.click("#btn-start")
    page.wait_for_timeout(2500)
    call = _standard_call(calls, "pipeline")
    hint = ""
    for sel in ("#exec-error", "#run-hint"):
        node = page.query_selector(sel)
        if node is not None and (node.inner_text() or "").strip():
            hint += f" {sel}={node.inner_text().strip()[:120]}"
    rep.check(call is not None, "参数模式走标准运行端点（assistant_id=pipeline）",
              (_urls(calls) or "（无请求）") + hint)
    if call:
        rep.check("messages" in (call["body"].get("stream_mode") or []),
                  "参数模式同样声明 stream_mode", json.dumps(call["body"].get("stream_mode")))
    rep.check(not any("/api/pipeline/stream" in c["url"] for c in calls),
              "不再打老端点 /api/pipeline/stream")
    page.screenshot(path=str(SHOT_DIR / "pipeline.png"), full_page=False)


def run_live(page: Any, rep: Report, calls: List[Dict[str, Any]]) -> None:
    """不打桩：真跑一次后端（含真实 LLM），只验证链路与最终渲染。"""
    page.goto(BASE + "/#chat", wait_until="domcontentloaded")
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
            print("--- 真实浏览器 · 参数模式（标准帧桩） ---")
            run_pipeline(page, rep, calls)
            report_run = os.environ.get("BROWSER_CHECK_REPORT_RUN") or ""
            if report_run:
                print("--- 真实浏览器 · 报告版式（结构卡 / 备注 / 图片） ---")
                run_report(page, rep, report_run)
        else:
            print("--- 真实浏览器 · 对话模式（真跑后端 + 真实 LLM） ---")
            run_live(page, rep, calls)
        rep.check(not errors, "浏览器无 JS 运行时错误", "; ".join(errors[:3]))
        browser.close()
    return rep.summary()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
