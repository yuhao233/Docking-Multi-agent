"""候选选择（choices）交互回归：两个问题互不覆盖 + 运行中点不动。

真实反馈（2026-09-22，用户实测）：
  「配体选择出现了两次：一个很快很短，另一个慢、会说受体解析完成，并且会覆盖之前的输出。
   如果点了先出现的那个选项，后面的选项就出不来了。」

服务端的幂等不变式由 `tests/test_choices_lifecycle.py` 看护；这里用**真实浏览器**看住
两条界面不变式：
  1. 同一轮里不同 kind 的问题（分子代表结构 / 阳性对照）必须**同时留着**，后到的不得覆盖先到的；
  2. 运行中的候选按钮必须点不动（历史上点了会被 busy 判定静默丢掉，用户以为「选了没反应」）。

单独成文件的原因：`scripts/browser_check.py` 已接近 700 行的文件上限（见
`scripts/lint_local.py` 的 BIG_FILE_EXEMPT 说明），新增场景按主题独立。
"""
from __future__ import annotations

from typing import Any, Dict, List

MOLECULE = {
    "id": "molecule:3034368:raw", "kind": "molecule",
    "label": "Mancozeb（CID 3034368） · PubChem 原始多组分结构",
    "value": "CCO.[Zn+2]", "prompt": "代森锰锌 CCO.[Zn+2] （按原始多组分继续对接）",
    "detail": {"cid": 3034368, "mode": "raw-mixture"},
}
POSITIVE = {
    "id": "positive_control:use", "kind": "positive_control",
    "label": "用共晶配体作阳性对照", "value": "5CM",
    "prompt": "用共晶配体 5CM 作阳性对照",
    "detail": {"ligand": "5CM"},
}


def two_kinds_coexist(page: Any, rep: Any, calls: List[Dict[str, Any]], base: str) -> None:
    """高级模式：同一轮下发两类问题 → 两份按钮都要在，且运行结束后都可点。"""
    import browser_check as bc  # 延迟导入：本模块由它调用，避免导入环

    calls.clear()

    def handle(route: Any) -> None:
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=bc.sse([
            {"type": "start", "run_id": "E2E-TWO"},
            {"type": "choices", "choices": [dict(MOLECULE)], "note": "该名称是多组分：请选择代表结构"},
            {"type": "choices", "choices": [dict(POSITIVE)], "note": "是否用共晶配体作阳性对照"},
            {"type": "final", "content": "两个问题都请确认。"},
            {"type": "done", "run_id": "E2E-TWO", "summary": {"status": "needs_user_input"}},
            {"type": "end"}]))

    page.route("**/runs/stream", handle)
    page.goto(base + "/advanced#chat", wait_until="networkidle")
    page.fill("#chat-input", "代森锰锌")
    page.click("#chat-send")
    page.wait_for_timeout(1500)
    mol = page.locator('#chat-history [data-choice-group="molecule"] .chat-choice')
    pos = page.locator('#chat-history [data-choice-group="positive_control"] .chat-choice')
    rep.check(mol.count() >= 1, "分子「代表结构」问题渲染成可点按钮")
    rep.check(pos.count() >= 1, "阳性对照问题未被后到的问题覆盖（两问共存于同一气泡）")
    if mol.count():
        rep.check(page.eval_on_selector(
            '#chat-history [data-choice-group="molecule"] .chat-choice',
            "el => el.disabled === false"), "运行结束后候选按钮可点")
    # 运行中必须点不动：直接驱动真实运行态，再断言按钮 disabled（点了也不会被静默丢弃）
    page.evaluate("() => setRunning(true)")
    rep.check(page.eval_on_selector('#chat-history [data-choice-group="molecule"] .chat-choice',
                                    "el => el.disabled === true"), "运行中候选按钮点不动")
    page.evaluate("() => setRunning(false)")
    rep.check(page.eval_on_selector('#chat-history [data-choice-group="molecule"] .chat-choice',
                                    "el => el.disabled === false"), "运行结束后恢复可点")
    # 点掉「分子」这一问：阳性对照那一问必须还在（旧的 clearChoicesEverywhere 会把它一起抹掉）
    page.locator('#chat-history [data-choice-group="molecule"] .chat-choice').first.click()
    page.wait_for_timeout(600)
    rep.check(page.locator('#chat-history [data-choice-group="positive_control"] .chat-choice').count() >= 1,
              "回答其中一问不会清掉另一问的按钮")
    page.unroute("**/runs/stream", handle)   # 还原桩，别影响后续场景


def simple_choice_guard(page: Any, rep: Any, base: str) -> None:
    """简易模式：候选按钮在运行中必须点不动，且不再「先清空再丢弃」。"""
    page.goto(base + "/", wait_until="networkidle")
    page.evaluate("""() => {
        window.__probe = {id: 'molecule:1', kind: 'molecule', label: '按原始多组分继续',
                          value: 'CCO.[Zn+2]', prompt: '代森锰锌 CCO.[Zn+2]（按原始多组分继续对接）',
                          detail: {mode: 'raw-mixture'}};
        renderChoices([window.__probe,
          {id: 'molecule:2', kind: 'molecule', label: '按最大有机片段继续', value: 'CCO',
           prompt: '代森锰锌 CCO（按最大有机片段继续对接）', detail: {mode: 'organic-fragment'}}
        ], '该名称是多组分：请选择代表结构');
    }""")
    page.wait_for_timeout(200)
    rep.check(page.locator(".s-choice-btn").count() == 2, "简易模式渲染出候选按钮")
    page.evaluate("() => setBusy(true)")
    rep.check(page.eval_on_selector(".s-choice-btn", "el => el.disabled === true"),
              "简易模式运行中候选按钮点不动")
    # 按钮已点不动；这里直接驱动 applyChoice，验证「程序路径」也不会先清空按钮再静默丢弃
    page.evaluate("() => applyChoice(window.__probe)")
    page.wait_for_timeout(300)
    rep.check(page.locator(".s-choice-btn").count() == 2, "运行中点选不会先把按钮清空")
    hint = page.inner_text("#s-chat-hint")
    rep.check("仍在运行" in hint, f"运行中点选给出明确提示：{hint!r}")
    page.evaluate("() => setBusy(false)")
    rep.check(page.eval_on_selector(".s-choice-btn", "el => el.disabled === false"),
              "简易模式运行结束后候选按钮恢复可点")


def choices_are_deferred(page: Any, rep: Any, base: str) -> None:
    """候选必须**等本轮模型输出结束**才挂出（用户反馈）。

    真实反馈：「模型一开始输出『我先并行解析受体与配体』，后续输出还没出现，配体选择就出现了；
    如果提前选择，后续就取不到这次选择。选择请放到模型完成输出后再出现。」

    所以两套界面收到 `choices` 时都只**缓冲**，本轮结束（`setRunning(false)` /
    `setBusy(false)`）才渲染。这里直接驱动客户端函数，断言"缓冲期间界面上没有按钮"。
    """
    molecule = {"id": "molecule:1", "kind": "molecule", "label": "按原始多组分继续",
                "value": "CCO.[Zn+2]", "prompt": "代森锰锌 CCO.[Zn+2]（按原始多组分继续对接）"}

    # ---- 简易模式 ----
    page.goto(base + "/", wait_until="networkidle")
    page.evaluate("""(choice) => {
        state.deferredChoices = {choices: [choice], note: '该名称是多组分：请选择代表结构'};
    }""", molecule)
    page.wait_for_timeout(200)
    rep.check(page.locator(".s-choice-btn").count() == 0,
              "简易模式：候选缓冲期间界面上没有按钮（模型还在输出）")
    page.evaluate("() => flushDeferredChoices()")
    page.wait_for_timeout(200)
    rep.check(page.locator(".s-choice-btn").count() == 1, "简易模式：本轮结束后候选才出现")

    # ---- 高级模式 ----
    page.goto(base + "/advanced#chat", wait_until="networkidle")
    page.evaluate("""(choice) => { handleChoicesEvent({choices: [choice], note: '需要确认'}); }""", molecule)
    page.wait_for_timeout(300)
    rep.check(page.locator("#chat-history .chat-choice").count() == 0,
              "高级模式：候选缓冲期间界面上没有按钮（模型还在输出）")
    rep.check(page.evaluate("() => (state.deferredChoices || []).length") == 1,
              "高级模式：候选已进入缓冲（不会丢）")
    page.evaluate("() => flushDeferredChoices()")
    page.wait_for_timeout(300)
    rep.check(page.locator("#chat-history .chat-choice").count() >= 1, "高级模式：本轮结束后候选才出现")


def limit_event_is_informational(page: Any, rep: Any, base: str) -> None:
    """步数预算事件只更新状态文案：**不得**产生任何需要用户回答的问题/按钮。

    用户要求：跑满步数由系统自动处理（自动放宽 → 主管 Agent 收尾），别打扰用户。
    """
    page.goto(base + "/advanced#chat", wait_until="networkidle")
    # 与「有没有历史候选」无关：只断言这个事件**不新增**任何可点选项
    before = page.locator("#chat-history .chat-choice").count()
    page.evaluate("""() => handleEvent({type: 'limit', limit: 120, next_limit: 240,
        message: '已达步数上限（120 步）：已自动放宽到 240 步继续，无需用户操作'})""")
    page.wait_for_timeout(300)
    hint = page.inner_text("#run-hint") if page.locator("#run-hint").count() else ""
    rep.check("自动放宽" in hint, f"高级模式：limit 事件只更新状态文案：{hint!r}")
    rep.check(page.locator("#chat-history .chat-choice").count() == before,
              "limit 事件不新增任何可点选项（不打扰用户）")
