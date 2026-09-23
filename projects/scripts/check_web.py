#!/usr/bin/env python
"""前端静态校验（无需浏览器）。

检查项：
  1. `node --check web/app.js` 语法正确；
  2. 不引用任何外部 CDN / 外部字体（离线可用）；
  3. `index.html` 引用了 /static/app.js 与 /static/styles.css；
  4. app.js 中所有 getElementById / querySelector('#id') 的 id 都存在于 index.html；
  5. 关键交互存在：对话模式 / 参数模式子页、高级设置折叠、两种模式的接口与 mode 字段；
  6. 业务字段渲染：结合模式相关字段（maccs/一致性/锚定/提示）在 app.js 中被使用。

用法： .venv/bin/python scripts/check_web.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB = PROJECT_ROOT / "web"
RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  →  {detail}" if detail else ""))
    return bool(ok)


class _Ids(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "id" and v:
                self.ids.append(v)


def main() -> int:
    print("=" * 74)
    print("前端静态校验")
    print("=" * 74)

    index = WEB / "index.html"
    app = WEB / "app.js"
    css = WEB / "styles.css"
    simple = WEB / "simple.html"
    simple_js = WEB / "simple.js"
    simple_css = WEB / "simple.css"
    md_js = WEB / "markdown.js"  # 两套界面共用的 Markdown 渲染器（唯一实现）
    for f in (index, app, css, simple, simple_js, simple_css, md_js):
        if not check(f.is_file(), f"文件存在：{f.name}"):
            return 1

    html = index.read_text(encoding="utf-8")
    js = app.read_text(encoding="utf-8")
    styles = css.read_text(encoding="utf-8")
    s_html = simple.read_text(encoding="utf-8")
    s_js = simple_js.read_text(encoding="utf-8")
    s_styles = simple_css.read_text(encoding="utf-8")
    mdjs = md_js.read_text(encoding="utf-8")

    # 1) 语法
    node = shutil.which("node")
    if node:
        for target in (app, simple_js, md_js):
            proc = subprocess.run([node, "--check", str(target)], capture_output=True, text=True)
            check(proc.returncode == 0, f"node --check web/{target.name}", (proc.stderr or "").strip()[:200] or "语法正确")
    else:
        print("  [SKIP] 未找到 node，跳过 JS 语法检查")

    # 2) 无外部资源
    def external_refs(text: str) -> list[str]:
        hits = []
        for m in re.finditer(r"""["'(](https?://[^"')\s]+)""", text):
            url = m.group(1)
            if any(x in url for x in ("127.0.0.1", "localhost")):
                continue  # 本机地址示例不算外部依赖
            hits.append(url)
        return hits

    ext = (external_refs(html) + external_refs(styles) + external_refs(js)
           + external_refs(s_html) + external_refs(s_styles) + external_refs(s_js)
           + external_refs(mdjs))
    check(ext == [], "无外部 CDN / 外部字体引用", ", ".join(sorted(set(ext))[:5]) or "无")
    check("@import" not in styles and "url(http" not in styles, "CSS 内无外部 @import / url()")
    check("@import" not in s_styles and "url(http" not in s_styles,
          "简易模式 CSS 内无外部 @import / url()")

    # 3) 静态资源引用
    check("/static/app.js" in html, "index.html 引用 /static/app.js")
    check("/static/styles.css" in html, "index.html 引用 /static/styles.css")

    # 4) id 对应（覆盖 getElementById / $(id) 辅助函数 / querySelector('#id ...') 三种写法）
    parser = _Ids()
    parser.feed(html)
    html_ids = set(parser.ids)
    used: set[str] = set()
    used |= set(re.findall(r"""getElementById\(\s*['"]([^'"]+)['"]""", js))
    used |= set(re.findall(r"""\$\(\s*['"]([^'"]+)['"]\s*\)""", js))
    used |= set(re.findall(r"""querySelector(?:All)?\(\s*['"]#([A-Za-z0-9_-]+)""", js))
    missing = sorted(i for i in used if i not in html_ids)
    check(len(used) >= 30, f"识别到足量 DOM id 引用（{len(used)} 个）", "数量异常偏少说明校验正则需要更新，避免出现假通过")
    check(missing == [], f"JS 引用的 {len(used)} 个 id 均存在于 HTML", f"缺失：{missing}" if missing else "全部命中")

    # 注：动态拼接的 id（$('center-' + axis)、'chat-' + seq）无法用静态正则可靠区分
    #「运行时生成的 id」与「写错的 id」，因此不在此处猜测；改用 scripts/snapshot_ids.py 做前后清单比对。

    # 5) 关键交互
    check(bool(re.search(r'id="mode-(tabs|switch)"', html)), "存在「对话 / 参数」模式切换控件")
    check("advanced" in js, "JS 处理 advanced（高级设置）字段", f"出现 {js.count('advanced')} 次")
    check(bool(re.search(r"<details|advanced-body|adv-body", html + js)), "高级设置使用可折叠容器（默认收起）")
    check("mode" in js and "'chat'" in js.replace('"', "'") and "'manual'" in js.replace('"', "'"),
          "提交时区分 chat / manual 模式")
    # 阶段 2：前端改走标准 Agent Protocol（线程级 stream）；旧端点仍在后端保留为兼容层
    check("/runs/stream" in js and "/threads/" in js, "调用标准 Agent Protocol 线程级流式端点（/threads/{tid}/runs/stream）")
    check("assistant_id" in js and "STANDARD_ASSISTANTS" in js, "请求体带 assistant_id（coordinator / pipeline）")
    check("standardFrameEvents" in js and "messages/partial" in js,
          "标准 SSE 帧（metadata/messages.partial/updates/custom/values）→ 内部事件适配层")
    check("/api/agent/stream" not in js, "前端不再直接调用旧端点 /api/agent/stream（后端仍保留为 deprecated 兼容层）")
    check("/api/runs" in js, "调用 /api/runs（历史与中间数据）")
    for field in ("maccs_tanimoto", "structural_consistency", "anchor_match", "binding_mode_hint"):
        check(field in js, f"界面展示结合模式字段：{field}")

    # 6) 必须的产物下载能力
    check("download.zip" in js, "提供整包下载（download.zip）")
    check("poses.zip" in js, "提供位姿下载（poses.zip）")

    # 6.5) 结构与语法（可靠守卫：标签配平 / 重复 id / CSS 括号配平）
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}
    stack: list[str] = []
    problems: list[str] = []

    class _Balance(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in VOID:
                stack.append(tag)

        def handle_endtag(self, tag):
            if tag in VOID:
                return
            if not stack:
                problems.append(f"多余的 </{tag}>")
            elif stack[-1] != tag:
                problems.append(f"标签不匹配：期望 </{stack[-1]}>，实际 </{tag}>")
                if tag in stack:
                    while stack and stack.pop() != tag:
                        pass
            else:
                stack.pop()

    bal = _Balance()
    bal.feed(html)
    if stack:
        problems.append(f"未闭合：{stack[:5]}")
    check(problems == [], "HTML 标签配平", "；".join(problems[:3]) if problems else "结构正确")

    all_ids = re.findall(r"""\sid=["']([^"']+)["']""", html)
    dups = sorted({i for i in all_ids if all_ids.count(i) > 1})
    check(dups == [], "HTML 无重复 id", f"重复：{dups}" if dups else f"{len(all_ids)} 个 id 唯一")

    css_clean = re.sub(r"/\*.*?\*/", "", styles, flags=re.S)
    opens, closes = css_clean.count("{"), css_clean.count("}")
    parens = css_clean.count("(") - css_clean.count(")")
    check(opens == closes, "CSS 花括号配平", f"{{ {opens} }} {closes}")
    check(parens == 0, "CSS 括号配平", f"差值 {parens}")
    check("undefined" not in styles.lower() and "TODO" not in styles, "CSS 中无占位/未完成内容")

    # 6.6) 导航栏与设置页面（v0.8 新增）
    check(bool(re.search(r'id="topnav"', html)), "存在主导航栏")
    nav_views = re.findall(r'data-view="([a-z]+)"', html)
    check(nav_views == ["workbench", "settings"], "导航栏含「工作台 / 设置」两个页面", "/".join(nav_views))
    check('id="view-workbench"' in html and 'id="view-settings"' in html, "两个顶层视图容器存在")
    check(".view.is-active" in styles, "视图通过 .is-active 切换（含淡入过渡）")
    for needed in ("btn-settings-save", "btn-settings-reset", "btn-settings-reload",
                   "btn-fetch-models", "settings-groups", "settings-status",
                   "settings-model-list"):
        check(f'id="{needed}"' in html, f"设置页元素存在：{needed}")
    for api in ("/api/settings", "/api/settings/reload", "/api/settings/reset",
                "/api/settings/test", "/api/models"):
        check(api in js, f"前端调用设置接口 {api}")
    check("settings-model-list" in js and "datalist" in html.lower(), "模型名提供自动补全（datalist）")
    check("src-chip" in js and "src-chip" in styles, "设置项带来源标注（界面/环境变量/默认）")
    check("viewFromHash" in js and "#/settings" in js, "设置页支持 URL hash 深链接（#/settings）")
    check("applySettingsToWorkbench" in js and "settingsState.formTouched" in js, "工作台表单用设置默认值预填（且不覆盖用户已改字段）")

    # 6.65) 运行图按「真实并行情况」展示（v0.11）
    check('id="orch-lanes"' in html and "renderOrchLanes" in js, "编排图含实测时间轴（按真实起止时间画条）")
    check("orchParallelism" in js or "parallel" in js.lower(), "前端计算实测并行度")
    check("'verify'" not in js or "orch-node-verify" not in html, "独立复核节点已移除（流程控制权在主管 Agent）")

    # 6.7) 结合口袋 / 口袋分析 Agent（v0.9 新增）
    check('id="orch-node-pocket"' in html, "编排图含「口袋分析」节点")
    check("'pocket'" in js or '"pocket"' in js, "前端识别 pocket 角色/节点")
    check("run_pocket_analysis" in js and "predict_binding_pockets" in js, "前端跟踪口袋分析工具调用")
    check('id="pocket-engine-select"' in html, "工作台提供「结合位点来源（口袋引擎）」选择")
    for api in ("pocket_engine",):
        check(api in js, f"请求体下发 {api}")
    check('id="pocket-tbody"' in html and "renderPocketAnalysis" in js, "结果区展示口袋预测表（含被采用的口袋）")
    check("renderBoxLine" in js and 'id="orch-box"' in html, "展示实际使用的对接盒与来源")

    # 6.75) 多轮会话（conversation_id，v0.13 新增）
    check('id="btn-chat-new"' in html and "startNewConversation" in js, "对话区提供「新对话」按钮（清空气泡 + 换新会话 id）")
    check("conversation_id" in js and "dsh_conversation_id" in js, "每次提交携带 conversation_id，并持久化到 localStorage（刷新后继续）")
    check('id="exec-conversation-id"' in html and "setConversationIdLabel" in js, "执行区显示当前会话短 id（便于排查）")

    # 7) 布局健壮性与设计一致性（防止长文本/长日志「拉扯」布局）
    root_block = ""
    m = re.search(r":root\s*\{(.*?)\}", styles, re.S)
    if m:
        root_block = m.group(1)
    tokens = re.findall(r"--[a-z0-9-]+\s*:", root_block)
    check(len(tokens) >= 12, "设计令牌（:root CSS 变量）≥12 个", f"实际 {len(tokens)} 个")
    for token in ("--bg", "--panel", "--border", "--text", "--accent"):
        check(token in root_block, f"定义了核心令牌 {token}")

    check("tabular-nums" in styles, "数值使用 tabular-nums（等宽对齐、不跳动）")
    check(re.search(r"--font-mono|ui-monospace|Menlo|Consolas", styles) is not None, "定义了等宽字体（代码/数值/日志风格）")

    # 防「拉扯」：flex/grid 子项必须允许收缩 + 长文本必须能断行/截断
    check(styles.count("min-width: 0") >= 3,
          "flex/grid 子项设置 min-width:0（防止长内容撑破布局）",
          f"出现 {styles.count('min-width: 0')} 次")
    check(re.search(r"overflow-wrap|word-break|text-overflow", styles) is not None,
          "长文本可断行/截断（overflow-wrap / word-break / text-overflow）")
    check(re.search(r"overflow-x:\s*auto", styles) is not None, "宽表格有横向滚动容器")
    check(re.search(r"max-height:\s*\d+", styles) is not None and
          re.search(r"overflow-y:\s*auto|overflow:\s*auto", styles) is not None,
          "日志/流式区域有 max-height + 纵向滚动（不随内容无限增长）")
    check(re.search(r"table-layout:\s*fixed", styles) is not None, "表格使用 table-layout:fixed（列宽稳定，不被内容拉扯）")

    # 模式切换平滑
    mode_transition = re.search(r"transition[^;]*(opacity|transform)", styles) is not None
    check(mode_transition, "模式面板使用 transition（opacity/transform）实现平滑切换")
    check(re.search(r"\.mode-panel|\.is-active|\.panel-enter|data-active", styles + js) is not None,
          "模式面板通过 class/属性切换（而非 display:none 硬切）")
    check("prefers-reduced-motion" in styles, "尊重 prefers-reduced-motion（无障碍）")

    # 深链接/可测试性
    check("location.hash" in js and ("#chat" in js or "'chat'" in js), "模式支持 URL hash（#chat / #manual）深链接")
    check(re.search(r"@media[^{]*max-width", styles) is not None, "包含响应式断点")
    check(styles.count("!important") <= 2, "无 !important 滥用", f"{styles.count('!important')} 处")
    check(len(re.findall(r"#[0-9a-fA-F]{6}\b", styles)) <= 60,
          "颜色以令牌集中管理（裸十六进制颜色数量受控）",
          f"{len(re.findall(r'#[0-9a-fA-F]{6}', styles))} 处")

    # 导出条（本次运行：报告 PDF / 报告 MD / 排序 CSV / 位姿 ZIP / 打包下载）
    download_ids = ["btn-download-pdf", "btn-download-report", "btn-download-csv",
                    "btn-download-poses", "btn-download-zip"]
    check('id="download-bar"' in html and all(f'id="{i}"' in html for i in download_ids),
          "结果面板含统一导出条（PDF / MD / CSV / 位姿 / 打包）",
          "缺失：" + ", ".join(i for i in download_ids if f'id="{i}"' not in html)
          if not all(f'id="{i}"' in html for i in download_ids) else "5 个入口齐备")
    check("updateExportLink" in js and "DOWNLOADSET" not in js and "downloads" in js,
          "导出条文件名由后端 downloads 下发（前端不自己拼命名规则）")
    check('id="run-kpis"' in html and "function renderKpis" in js, "结果总览有 KPI 一行（聚合统计：分子数/命中/最佳/失败/耗时）")
    check('id="settings-anchor"' in html and "renderSettingsAnchor" in js, "设置页有左侧锚点导航（长页面可跳转）")
    check(".kpi" in styles and ".download-bar" in styles and ".panel-title" in styles, "新增组件（KPI / 导出条 / 面板标题）都有样式")
    check("motion-in" in styles and "num-tween" in styles and "@keyframes" in styles,
          "统一动效（入场动画 / 数字滚动 / keyframes）已定义")

    # v0.31 三栏：对话与结果在中栏，参数在左栏，运行详情在右栏
    center = re.search(r'id="workbench-center".*?(?=<section class="main-col)', html, re.S)
    check(bool(center) and 'id="chat-form"' in center.group(0)
          and 'id="chat-input"' in center.group(0),
          "对话框（输入框 + 发送）位于中栏「对话与结果」内")
    check(bool(center) and 'panel panel-result' in center.group(0), "结果与产物面板与对话同处中栏")
    params_side = re.search(r'id="config-panel".*?id="workbench-center"', html, re.S)
    check(bool(params_side) and 'id="chat-form"' not in params_side.group(0)
          and 'id="manual-params-mount"' in params_side.group(0),
          "左栏只放参数设置（对话输入不再留在左栏）")
    # v0.33 布局：右栏只留「多 Agent 编排」；运行详情（阶段日志/工具轨迹/实时逐分子结果）
    # 紧邻对话下方（中栏），保证主阅读动线是「对话 → 运行详情 → 结果」
    live_side = html[html.index('<section class="main-col'):]
    check('id="orchestration-panel"' in live_side
          and 'id="run-details"' not in live_side,
          "右栏保留多 Agent 编排（运行详情不再占用右栏）")
    center_side = html[html.index('id="workbench-center"'):html.index('<section class="main-col')]
    check('id="run-details"' in center_side and 'id="molecules-tbody"' in center_side
          and 'id="tool-trace"' in center_side and 'id="log-box"' in center_side,
          "运行详情（阶段日志/工具轨迹/实时逐分子结果）在对话下方的中栏")
    check('id="chat-file-input"' in html and 'id="chat-attachments"' in html
          and 'id="chat-mention"' in html,
          "对话框支持上传附件与 @ 引用（文件输入 / chips / 引用选择器）")
    check(all(k in js for k in ("chatAttachFiles", "chatAttachmentPayload", "appendFileRefs",
                                "mentionedAttachments", "renderMention")),
          "附件与引用逻辑齐备（上传 / 请求构造 / 引用清单 / @ 匹配 / 选择器）")
    check("dragenter" in js and "drop" in js and "chat-mention-item" in js, "支持拖拽上传与引用选择器交互")

    # 8) 精简设计（v0.14）：统一工具提示 / 高级设置栅格与预设（只增不减的守卫）
    tip_triggers = len(re.findall(r"data-tip(?:-html)?=", html))
    check(tip_triggers >= 15, f"长解释搬进气泡提示（data-tip / data-tip-html 共 {tip_triggers} 处）")
    check('data-tip-html=' in html and 'data-tip=' in html, "同时支持「一句话」data-tip 与「结构化」data-tip-html")
    for name in ("bindTooltips", "showTip", "hideTip", "placeTip", "tipNodesFromHtml", "TIP_TAGS"):
        check(name in js, f"工具提示组件含 {name}（挂 body / 自适应定位 / 白名单 HTML）")
    check("tip-box" in styles and "tip-above" in styles and "tip-arrow-x" in styles,
          "气泡样式含外框 + 自适应箭头（.tip-box / .tip-above）")
    check(re.search(r"prefers-reduced-motion[^{]*\{[^@]*\.tip-box", styles, re.S) is not None,
          "工具提示尊重 prefers-reduced-motion")
    check(re.search(r"\.tip-box\s*\{[^}]*z-index:\s*\d+", styles, re.S) is not None, "气泡 z-index 高于面板层")
    check("attr(data-tip)" in styles and "has-tip-js" in styles and "has-tip-js" in js,
          "无 JS 时用纯 CSS ::after 气泡兜底（有 JS 时切 body 级气泡防裁切）")

    # 8.1 高级设置：一键预设 / 恢复系统默认
    for pid, exh, poses in (("preset-fast", "4", "1"),
                            ("preset-balanced", "16", "1"),
                            ("preset-accuracy", "32", "3")):
        block = re.search(r'id="' + pid + r'"(.{0,220}?)>', html, re.S)
        body = block.group(1) if block else ""
        check(f'data-exhaustiveness="{exh}"' in body and f'data-n-poses="{poses}"' in body,
              f"预设 {pid} = exhaustiveness {exh} / n_poses {poses}")
    check("PARAM_PRESETS" in js and "applyPreset" in js and "setPresetNote" in js, "预设点击填入并给出「已应用预设」轻提示")
    check('id="preset-note"' in html and 'role="status"' in html, "轻提示使用 role=status（读屏可播报）")
    check('id="btn-restore-defaults"' in html and "restoreParamDefaults" in js, "提供「恢复系统默认」按钮（只清界面值，不写配置文件）")
    check("settingsState.formTouched.clear()" in js and "/api/settings" in js, "恢复默认不删除设置页已保存配置（仅清界面值）")

    # 8.2 高级设置：常用参数栅格 + 「更多参数」二级折叠 + 折叠摘要
    check('id="param-common"' in html and ".param-common" in styles
          and re.search(r"\.param-common\s*\{[^}]*grid-template-columns", styles, re.S) is not None,
          "常用参数使用紧凑栅格（.param-common）")
    check('id="more-params"' in html and ".more-body" in styles
          and re.search(r"\.more-body\s*\{[^}]*grid-template-columns", styles, re.S) is not None,
          "次要参数收进「更多参数」二级折叠（.more-body 栅格）")
    more_tag = re.search(r"<details[^>]*id=\"more-params\"", html)
    check(bool(more_tag) and "open" not in more_tag.group(0), "「更多参数」默认收起（二级折叠）")
    check('id="chat-advanced-summary"' in html and 'id="more-params-summary"' in html
          and "updateAdvancedSummary" in js and "updateMoreSummary" in js,
          "折叠摘要行显示当前生效的关键值（位点盒 / 口袋引擎 / 保存位姿）")
    # v0.24：运行参数（引擎/质子化+目标pH/位姿数/搜索强度/阳性对照）常驻在**对话框上方**，
    # 由 app.js 把 #param-common 挂到 #chat-quick-params；其余设置才收进「其他设置」折叠。
    check('id="chat-quick-params"' in html and 'id="params-rest"' in html,
          "运行参数有专用常驻容器（#chat-quick-params），其余设置单独成块（#params-rest）")
    check("chat-quick-params" in js and "params-rest" in js, "app.js 按模式分别挂载：运行参数→对话框上方，其余→其他设置")
    check('id="exhaustiveness"' in html and 'value="16"' in
          (re.search(r'id="exhaustiveness"[^>]*>', html).group(0) if re.search(r'id="exhaustiveness"[^>]*>', html) else ""),
          "搜索强度默认值 = 16（用户要求）")
    # v0.27：气泡小票必须与请求体同源。旧实现直接读表单，把界面默认值（受体 thrombin /
    # 位点中心 / 引擎 vina）写成「本次下发参数」，而请求体里根本没有这些字段 —— 用户实测反馈。
    check("chatParamChips" in js and "manualParamChips" in js
          and "纯指令：不注入任何运行参数" in js,
          "小票由请求体参数生成（未改动 = 纯指令），不再罗列界面默认值")
    check("对话模式的受体与位点盒由指令或口袋分析决定" in js, "小票明确不显示对话模式不发送的受体/位点盒")
    # v0.27：受理层未受理的运行（例如只说了句「你好」）不得产/挂空报告，状态如实标 no_op
    check("本次未执行计算（未生成报告）" in js and "noWork" in js, "未执行计算的运行：前端不挂空报告，如实提示（不再假装「运行完成 · 结果已载入」）")
    check("status === 'no_op' ? 'skip'" in js, "历史列表把 no_op 显示成 [ SKIP ]（而不是 [ FAIL ] 或 [ OK ]）")
    # 6.9) v0.28 布局重排：对话居中放大 / 运行详情默认收起 / 设置页返回入口
    check('id="btn-settings-back"' in html and "bindSettingsBack" in js, "设置页有「返回工作台」按钮（并支持 Esc）")
    # 三栏：某条 grid-template-columns 必须声明三段轨道（左参数 / 中主内容 / 右运行详情）
    three_cols = re.search(
        r"\.layout\s*\{[^}]*grid-template-columns:\s*minmax\([^;]*minmax\([^;]*minmax\(",
        styles, re.S)
    check(three_cols is not None, "工作台三栏网格（参数 / 对话与结果 / 运行详情）")
    check(".center-col" in styles
          and "body.cols-params-collapsed .layout" in styles
          and "body.cols-live-collapsed .layout" in styles,
          "左右侧栏可折叠（body.cols-*-collapsed 控制列宽与隐藏）")
    check("@media (max-width: 1180px)" in styles and "@media (max-width: 900px)" in styles, "窄屏降为两栏/单栏（不出现横向滚动）")
    # 历史任务查询：关掉页面后仍能按关键词/状态/受体/时间找回旧运行
    check('id="history-q"' in html and 'id="history-search"' in html and 'id="history-reset"' in html,
          "历史面板有关键词输入、查询与重置")
    check('id="history-status"' in html and 'id="history-receptor"' in html
          and 'id="history-since"' in html and 'id="history-until"' in html,
          "历史面板有状态/受体/时间范围过滤")
    check('id="history-prev"' in html and 'id="history-next"' in html and 'id="history-count"' in html, "历史面板有分页与命中计数")
    check("function historyQueryString(" in js and "'/api/runs?'" in js
          and "state.historyTotal" in js and "docking.history.query" in js,
          "历史检索走服务端 /api/runs（关键词/过滤/分页）并记住上次检索条件")
    check(re.search(r"\.chat-dock \.chat-stream\s*\{[^}]*min-height:\s*46vh", styles, re.S) is not None,
          "对话区放大（min-height 46vh，成为页面视觉中心）")
    check('id="run-details"' in html and "setRunDetailsOpen" in js, "运行详情（编排 / 日志 / 工具轨迹 / 实时表）默认收起、开跑自动展开")
    run_details_tag = re.search(r'<details[^>]*id="run-details"', html)
    check(bool(run_details_tag) and "open" not in run_details_tag.group(0), "运行详情默认收起（首屏不再被空面板占满）")
    check("setWorkbenchHasRun" in js and "#view-workbench.no-run" in styles, "未运行时收起空控件（排序工具条 / 表格 / 图例 / 图表）")
    check(re.search(r'<div class="[^"]*no-run[^"]*" id="view-workbench"', html) is not None, "工作台首屏即处于「无结果」状态")
    check("details.open = group.id === (data.groups || [])[0]?.id" in js, "设置页默认只展开第一组（6 组 60+ 字段不再一次铺开）")
    check("settings-group-count" in js and "settings-group-count" in styles, "设置分组标题带「N 项」徽标（先看规模再展开）")
    check(".settings-head" in styles and "settings-back" in styles, "设置页头部是一条工具带（返回 / 标题 / 动作），不再是散落的按钮行")
    # 6.10) v0.29 报告与备注：推荐卡片（结构挨着数据）+ 运行笔记折叠
    check('id="run-notes"' in html and "renderRunNotes" in js, "运行笔记有独立区块（不再把整段备注塞进汇总表格）")
    check(re.search(r"\.run-note-text\s*\{[^}]*-webkit-line-clamp:\s*2", styles, re.S) is not None
          and ".run-note-text.is-open" in styles,
          "运行笔记默认只显示 2 行，可「展开」看全文（超长备注不再拉长页面）")
    check("运行笔记', run.notes.length" in js or "运行笔记" in js and "条（见下方）" in js, "汇总表只显示备注条数，正文交给折叠区块")

    reporting_dir = PROJECT_ROOT / "src" / "docking_agent" / "reporting"
    # 报告模板第 2 波已按章节拆分：静态核对必须覆盖**整个模板**（report.py 现在只剩编排）
    report_py = "\n".join(
        (reporting_dir / name).read_text(encoding="utf-8")
        for name in ("report.py", "report_context.py",
                     "report_sections_setup.py", "report_sections_results.py"))
    fields_py = (PROJECT_ROOT / "src" / "docking_agent" / "reporting" / "fields.py").read_text(encoding="utf-8")
    rec_py = (PROJECT_ROOT / "src" / "docking_agent" / "tools" / "recommend.py").read_text(encoding="utf-8")
    coord_py = (PROJECT_ROOT / "src" / "docking_agent" / "agents" / "coordinator.py").read_text(encoding="utf-8")
    dock_py = (PROJECT_ROOT / "src" / "docking_agent" / "core" / "docking.py").read_text(encoding="utf-8")
    cards_py = (PROJECT_ROOT / "src" / "docking_agent" / "reporting" / "cards.py").read_text(encoding="utf-8")
    arts_py = (PROJECT_ROOT / "src" / "docking_agent" / "reporting" / "artifacts.py").read_text(encoding="utf-8")
    check("recommend_card_" in report_py and "recommendation_card" in cards_py, "推荐排行逐个分子附「2D 结构 + 关键指标」卡片图（结构挨着数据）")
    check("def _brief" in report_py and "_brief(n, 110)" in report_py, "报告里的运行笔记/理由先精简成一句话再写入（PDF 不再整页备注）")
    check('_write_recommend_cards' in arts_py and 'charts/{name}.png' in arts_py, "推荐卡片登记为产物（网页 / PDF / 整包 ZIP 三处一致）")
    # 6.11) 协调 Agent 的自主性：按用户要求定制报告 + 用户的数据字段一路带得下去
    check("def customize_report" in rec_py and "customize_report" in coord_py
          and "REPORT_FIELD_WHITELIST" in rec_py,
          "协调 Agent 有 customize_report 工具（按用户要求改报告内容，白名单校验）")
    check("REPORT_FIELD_LABELS" in fields_py and "report_customization" in report_py
          and "本次要求与响应" in report_py,
          "报告按定制输出：自定义标题 + 附加列 + 「0. 本次要求与响应」一节")
    check("def carry_identity" in dock_py and "IDENTITY_FIELDS" in dock_py,
          "分子 ID / 来源文件等字段一路带进对接、性质、排序与报告（不再只活在 molecules.json）")
    csv_fields = (PROJECT_ROOT / "src" / "docking_agent" / "reporting" / "tables.py").read_text(
        encoding="utf-8")
    check('"id"' in fields_py and '"id"' in csv_fields and '"cas"' in csv_fields
          and '"remark"' in csv_fields,
          "ranking.csv 含 id / cas / 备注 列（用户要 ID/CAS/备注 时可离线核对）")
    check(".md-img-fallback[hidden]" in styles, "图片降级文案只在加载失败时出现")

    # 6.12) 前端与接口的安全姿态：URL 方案白名单 + 无内联 onerror + CSP + 同源校验 + id 白名单
    # （审计发现：这三套前端门禁里原本**只有一条**安全断言，改坏 scheme 过滤不会变红）
    # app.py 第 2 波已拆成 routers/* + support.py + agent_flow.py，因此安全断言扫**整个 api 包**。
    api_dir = PROJECT_ROOT / "src" / "docking_agent" / "api"
    app_py = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted([api_dir / "app.py", api_dir / "support.py", api_dir / "agent_flow.py"]
                        + list((api_dir / "routers").glob("*.py"))))
    # 渲染器已抽到 markdown.js（唯一实现），以下安全断言改扫它
    check("function safeUrl" in mdjs and "escapeHtml(safeUrl(url))" in mdjs,
          "报告图片 URL 先过方案白名单再转义（不是只挡小写 javascript: 的黑名单）")
    check("safeUrl(url)" in mdjs and "href === '#'" in mdjs, "链接过方案白名单，非法方案降级为纯文本")
    check("onerror=" not in mdjs.split("function imageHtml")[1].split("function installImageFallback")[0],
          "图片降级不用内联 onerror（会被 CSP script-src 'self' 拦掉）")
    check("addEventListener('error'" in mdjs, "图片加载失败降级走捕获阶段委托")
    check(all("window.DockingMarkdown" in t for t in (mdjs, js, s_js)), "Markdown 渲染只有一份实现（markdown.js），两套客户端都调用它")
    check("Content-Security-Policy" in app_py and "script-src 'self'" in app_py,
          "服务端下发 CSP（script-src 'self'，与「无内联脚本」的现状一致）")
    check("_same_origin" in app_py and "_MUTATING_METHODS" in app_py, "写操作有同源校验（无鉴权服务必须挡住跨站简单请求）")
    check("safe_run_component" in app_py, "外部传入的 run/会话 id 先过白名单（防路径穿越）")
    check("ensure_inside(uploads_dir()" in app_py,
          "「校验文件」端点限定在 assets/uploads 之下（防任意文件读取）")

    # 6.13) 可访问性：可交互控件要有可访问名；tab 状态要与 aria 同步；错误面要被朗读
    check(html.count('role="tab"') == html.count("aria-selected="),
          "每个 role=tab 都有 aria-selected",
          f'role=tab {html.count("role=\"tab\"")} / aria-selected {html.count("aria-selected=")}')
    for cid in ("chat-input", "chat-file-input", "history-q", "history-status",
                "history-kind", "history-receptor"):
        segment = html.split(f'id="{cid}"')[1][:220] if f'id="{cid}"' in html else ""
        check("aria-label" in segment, f"控件 #{cid} 有可访问名（aria-label）")
    check('id="exec-error" role="alert"' in html or "role=\"alert\"" in html, "错误面 role=alert（失败必须被朗读）")
    check('aria-live=' in html, "存在 aria-live 实时区域（运行提示会被朗读）")
    check("setAttribute('aria-selected'" in js,
          "切换 tab / 配体来源时同步 aria-selected（视觉与语义不脱节）")

    # 6.14) HTML → JS 孤儿 id：HTML 里的 id 必须要么被 JS 引用，要么是 HTML 内部的
    # a11y/表单指向目标，要么在下面白名单里写明用途。
    # 审计 5.3：此前只校验 JS→HTML 方向（"JS 引用的 id 都在 HTML 里"），
    # HTML→JS 的孤儿方向完全无守卫，于是 51 个死 id（死 *-hint、死容器）就这样攒了出来。
    html_ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', html))
    # HTML 内部的指向目标：aria-labelledby/aria-controls/for/list/href="#id" 等
    html_id_targets: set = set()
    for attr in ("aria-labelledby", "aria-controls", "aria-describedby", "for", "list", "form"):
        for value in re.findall(attr + r'="([^"]+)"', html):
            html_id_targets.update(value.split())
    html_id_targets |= set(re.findall(r'href="#([A-Za-z0-9_-]+)"', html))
    # 动态拼接出来的 id 家族：JS 用前缀拼（静态扫描看不到字面量），逐个列举见 app.js
    dynamic_id_prefixes = ("orch-node-",   # $('orch-node-' + node)  → setOrchNode
                           "orch-tag-",    # $('orch-tag-' + node)
                           "tab-",         # pane.id === 'tab-' + name → switchTab
                           "view-",        # id === 'view-' + name     → viewFromHash/setView
                           "settings-group-",   # SETTINGS_GROUP_ID_PREFIX + group.id
                           "lang-",        # 'lang-' + lang（代码高亮）
                           "report-section-")   # 'report-section-' + n（JS 创建，不在 HTML）
    # CSS 专用 id：只在 styles.css 里作 `#id` 选择器（改成 class 是独立一轮的事，先登记）
    css_only_ids = {"agent-stream-title", "history-table", "ranking-pager",
                    "ranking-toolbar", "receptor-meta", "pocket-table"}
    # 前端门禁脚本的选择器锚点（scripts/ui_e2e.js / browser_check.py / 本文件的切片断言）
    gate_anchor_ids = {"more-params", "download-bar", "nav-settings", "nav-workbench", "nav-simple",
                       "orch-flow", "orch-lanes-hint", "orchestration-panel",
                       "workbench-center", "workbench-layout"}
    orphan_ids = []
    for cid in sorted(html_ids):
        if cid in css_only_ids or cid in gate_anchor_ids or cid in html_id_targets:
            continue
        if cid.startswith(dynamic_id_prefixes):
            continue
        if f"'{cid}'" in js or f'"{cid}"' in js or f"#{cid}" in js:
            continue
        orphan_ids.append(cid)
    check(not orphan_ids,
          "HTML 里的 id 都被 JS / a11y / 门禁脚本用到（无孤儿死 id；白名单见 check_web 6.14）",
          "孤儿 id：" + ", ".join(orphan_ids))

    common_start = html.find('id="param-common"')
    more_start = html.find('id="more-params"')
    common_block = html[common_start:more_start] if 0 <= common_start < more_start else ""
    more_block = html[more_start:html.find("</body>")] if more_start > 0 else ""
    # 基础区必须含「新手也能直接调」的关键项：受体 / 引擎 / 搜索强度 / 位姿数 /
    # 质子化态策略 + 目标 pH / 阳性对照 / 最大分子数（后三者 2026-09-17 从「更多参数」上移，
    # 用户要求"对对接特别重要的参数放在折叠外，新手也容易调"）。
    common_ids = ["receptor-source", "engine-select", "exhaustiveness", "n-poses",
                  "protonation-select", "protonation-ph", "positive-control", "max-ligands"]
    # 专家项（需要坐标/化学模板知识）留在「更多参数」折叠里
    more_ids = ["center-x", "center-y", "center-z", "size-x", "size-y", "size-z",
                "pocket-engine-select", "save-poses"]
    check(all(f'id="{i}"' in common_block for i in common_ids),
          "基础参数区含受体来源 / 引擎 / 搜索强度 / 位姿数 / 质子化态+目标pH / 阳性对照 / 最大分子数",
          "缺失：" + ", ".join(i for i in common_ids if f'id="{i}"' not in common_block))
    check("receptor-select" not in html and "receptor-select" not in js
          and "/api/receptors" not in js,
          "用户界面不再暴露预置受体下拉（受体只能由 PDB/UniProt/上传指定）")
    check(all(f'id="{i}"' in more_block for i in more_ids),
          "更多参数含结合位点盒 / 口袋引擎 / 保存位姿",
          "缺失：" + ", ".join(i for i in more_ids if f'id="{i}"' not in more_block))
    check('id="ph-presets"' in common_block, "目标 pH 提供常用取值预设（胃酸/溶酶体/生理），新手不用查文献")

    # 8.3 精简：主流程不再堆长段解释（长 hint 文案已进气泡）
    long_hint = re.compile(r'<p class="hint"[^>]*>\s*[^<]{0,40}<strong>[^<]{40,}', re.S)
    check(not long_hint.search(html), "主流程不再保留多行长段 .hint（已移入气泡提示）")
    check("自动规划" in html and "clamp(round(base" in html,
          "搜索强度气泡说明「自动规划」公式与「显式给值不自动改」")

    # 9) 简易模式页面（/simple）—— 同一份后端契约的轻量视图
    #    用户要求：只要「对话 + 结果」两块、去掉非必要内容、参数走默认/自动规划、
    #    顶栏能在两套界面间切换、背景做成现代简约的动态背景。
    check("/static/simple.js" in s_html and "/static/simple.css" in s_html, "simple.html 引用 js/css")
    check(s_html.index("/static/markdown.js") < s_html.index("/static/simple.js")
          and html.index("/static/markdown.js") < html.index("/static/app.js"), "两套页面先加载共用渲染器")
    check("/static/styles.css" in s_html, "simple.html 复用 styles.css 的设计令牌与基础组件")
    check(bool(re.search(r'<a[^>]*id="nav-simple"[^>]*href="/"', html)), "高级模式顶栏的「简易模式」入口指向默认首页 /")
    check('href="/advanced"' in s_html and "高级模式" in s_html, "简易模式顶栏能切到 /advanced")
    # 简约：简易模式不得出现任何参数表单控件/字段（去除非必要内容）
    param_tokens = ("exhaustiveness", "protonation", "n-poses", "site_center", "site_size",
                    "engine-select", "max-ligands", "pocket-engine", "positive-control-input")
    leaked = [t for t in param_tokens if t in s_html]
    check(leaked == [], "简易模式不暴露任何参数控件", "出现：" + ", ".join(leaked))
    check("advanced: false" in s_js and "mode: 'chat'" in s_js, "简易模式请求固定 chat + advanced=false")
    check("/threads/" in s_js and "/runs/stream" in s_js and "assistant_id" in s_js, "简易模式走标准端点")
    check("/api/agent/stream" not in s_js, "简易模式不调用旧端点 /api/agent/stream")
    for api in ("/api/health", "/api/uploads", "/api/runs"):
        check(api + "'" in s_js or api + '/' in s_js or api + '?' in s_js, f"简易模式调用 {api}")
    for path in ("/report.pdf", "/export.csv", "/download.zip"):
        check(path in s_js, f"简易模式提供产物下载：{path}")
    check("positive_control" in s_js and "positive_control_decision" in s_js, "对照决定作为请求字段下发")
    for field in ("molecule_choice", "molecule_choice_decision", "molecule_choice_label"):
        check(field in s_js, f"简易模式把「代表结构」选择作为请求字段下发：{field}")
    for field in ("molecule_choice", "molecule_choice_decision", "molecule_choice_label"):
        check(field in js, f"高级模式把「代表结构」选择作为请求字段下发：{field}")
    check("=== 'molecule'" in s_js and "=== 'molecule'" in js, "两套客户端都按 kind=molecule 识别「代表结构」选项")
    check("messages/partial" in s_js and "messages/complete" in s_js, "简易模式解析标准 SSE 帧")
    # 思考/推理必须折叠成可展开的「思考」气泡，绝不混进正文（避免刷屏）
    check("event.type === 'thinking'" in s_js and ".s-think" in s_styles, "简易模式思考进折叠块")
    check("finishThinking" in s_js and "思考中" in s_js and "'hidden'" in s_js, "简易模式思考：推理中展开可见，正文到达即自动收起（可再点开）")
    check("s-body markdown" in s_js and "renderMarkdown" in s_js, "简易模式回执按 Markdown 渲染")
    check("s-fold-toggle" not in s_js and ".s-folded" not in s_styles and "chat-fold" not in js,
          "对话气泡不再默认折叠（长回执全文直接显示）")
    # 候选选择：同一问只替换自己那一类，**别的待答问题必须留着**；运行中点不动（不再静默丢弃）
    check("function clearChoices(kind)" in js and "data-choice-group" in js, "高级模式候选按 kind 分组")
    # 候选**等本轮模型输出结束再挂出**：否则模型还在输出时用户就点了，点选与运行抢跑
    check(all(t in js for t in ("deferredChoices", "function flushDeferredChoices"))
          and "showChoices(choices, (data && data.note)" not in js
          and "deferredChoices" in s_js and "function flushDeferredChoices" in s_js
          and "renderChoices(event.choices" not in s_js,
          "两套界面的候选都先缓冲、本轮模型输出结束才挂出")
    check("仍然在运行" in js or "本轮仍在运行" in js, "高级模式运行中点选给出明确提示")
    check("setRunning" in js and "button.disabled = Boolean(running)" in js, "高级模式运行中按钮点不动")
    check("本轮仍在运行" in s_js and "s-choice-btn" in s_js, "简易模式运行中点不动且给提示")
    check("needs_user_input" in js and "tag-ask" in styles, "「停下等用户选择」有独立状态（非 no_op/SKIP）")
    # 步数预算：跑满上限由系统自动放宽/收尾，前端只显示状态文案，**不产生需要回答的问题**
    check("case 'limit'" in js and "event.type === 'limit'" in s_js,
          "两套界面都只把 limit 事件显示成状态文案（不弹问题）")
    check("loadedRun.choices" in s_js or "loadedRun.choices" in js, "刷新/载入运行记录时补挂候选（选项不会「加载不出来」）")
    check("await loadRun(last)" not in s_js and "不自动载入上一次运行" in s_js, "刷新后不自动载入上一次结果（结果区保持空态）")
    check("'thinking'" in js.replace('"', "'") and ".chat-think" in styles, "高级模式思考进可折叠块")
    check("chat-think-body" in js and "思考中" in js and "finishThinking" in js, "高级模式思考：推理中展开可见，正文到达即自动收起（可再点开）")
    check("renderMarkdown(message.text)" in js, "高级模式流式阶段也按 Markdown 渲染（不再是源码形态）")
    # 动态背景：纯 CSS、只动 transform/opacity、尊重 reduced-motion
    check("bg-anim" in s_html and "bg-blob" in s_html, "简易模式含动态背景容器")
    check("bg-drift" in s_styles and "bg-grid-pan" in s_styles, "动态背景由 CSS 关键帧驱动")
    check("prefers-reduced-motion: reduce" in s_styles and "animation: none" in s_styles, "背景尊重 reduced-motion")
    check("@media (max-width: 980px)" in s_styles, "简易模式窄屏自动堆叠（单列布局）")
    # 无障碍：对话区可朗读、输入有可访问名
    check('role="log"' in s_html and "aria-live" in s_html, "对话区 role=log + aria-live")
    check(bool(re.search(r'<textarea[^>]*id="s-input"[^>]*aria-label="[^"]+"', s_html, re.S))
          and 'aria-label="模式切换"' in s_html,
          "输入框与导航都有可访问名")
    # 结构与孤儿 id（与高级模式同一套规则，但只认 simple.js 的引用）
    s_stack: list[str] = []
    s_problems: list[str] = []

    class _SBalance(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in VOID:
                s_stack.append(tag)

        def handle_endtag(self, tag):
            if tag in VOID:
                return
            if not s_stack:
                s_problems.append(f"多余的 </{tag}>")
            elif s_stack[-1] != tag:
                s_problems.append(f"标签不匹配：期望 </{s_stack[-1]}>，实际 </{tag}>")
                if tag in s_stack:
                    while s_stack and s_stack.pop() != tag:
                        pass
            else:
                s_stack.pop()

    _SBalance().feed(s_html)
    if s_stack:
        s_problems.append(f"未闭合：{s_stack[:5]}")
    check(s_problems == [], "简易模式 HTML 标签配平", "；".join(s_problems[:3]) if s_problems else "结构正确")
    s_ids = re.findall(r"""\sid=["']([^"']+)["']""", s_html)
    s_dups = sorted({i for i in s_ids if s_ids.count(i) > 1})
    check(s_dups == [], "简易模式 HTML 无重复 id", f"重复：{s_dups}" if s_dups else f"{len(s_ids)} 个 id 唯一")
    s_used = set(re.findall(r"""\$\(\s*['"]([A-Za-z0-9_-]+)['"]\s*\)""", s_js))
    s_used |= set(re.findall(r"""getElementById\(\s*['"]([A-Za-z0-9_-]+)['"]""", s_js))
    s_orphans = sorted(set(s_ids) - s_used)
    check(s_orphans == [], "简易模式的 id 都被 simple.js 用到（无孤儿死 id）", "孤儿 id：" + ", ".join(s_orphans))
    s_missing = sorted(i for i in s_used if i not in set(s_ids))
    check(s_missing == [], "simple.js 引用的 id 都存在于 simple.html",
          f"缺失：{s_missing}" if s_missing else f"{len(s_used)} 个引用全部命中")
    s_clean = re.sub(r"/\*.*?\*/", "", s_styles, flags=re.S)
    check(s_clean.count("{") == s_clean.count("}"), "简易模式 CSS 花括号配平")
    check('id="s-input"' in s_html and 'id="s-log"' in s_html and 'id="s-hits"' in s_html
          and 'id="s-send"' in s_html,
          "简易模式保留最小必需元素（输入 / 对话 / 结果榜 / 发送）")

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [(l, d) for ok, l, d in RESULTS if not ok]
    print("\n" + "=" * 74)
    print(f"结果：{passed}/{len(RESULTS)} 通过")
    for label, detail in failed:
        print(f"  - 未通过：{label}  {detail}")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
