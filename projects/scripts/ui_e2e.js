#!/usr/bin/env node
/* 设置页面 / 导航栏的 DOM 级端到端验证（jsdom + 真实后端）。
 *
 * 为什么需要它：check_web.py 是纯静态校验，无法发现「渲染出来是空的」「保存没生效」
 * 「布尔字段被误判为已修改」这类运行时缺陷。本脚本用 jsdom 真加载页面、真点按钮、
 * 真读接口，验证设置页与导航栏的关键行为。
 *
 * 依赖（可选，仅开发期）：
 *     cd projects && npm_config_cache=$PWD/var/tmp/npm-cache npm install jsdom
 * 用法：
 *     node scripts/ui_e2e.js http://127.0.0.1:5000
 *
 * 安全：脚本会先快照现有界面设置，结束时**原样恢复**（不改动 .env，也不触碰 API Key）。
 */
let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (error) {
  console.error('缺少依赖 jsdom。请先安装后重试：');
  console.error('    cd projects && npm install jsdom');
  console.error('（受限环境可用工作区内的缓存目录：');
  console.error('  npm_config_cache=$PWD/var/tmp/npm-cache npm install jsdom）');
  process.exit(2);
}

const BASE = (process.argv[2] || 'http://127.0.0.1:5000').replace(/\/$/, '');
/* 默认首页是简易模式；本脚本驱动的是高级模式（工作台 / 设置），因此固定走 /advanced */
const ADVANCED = '/advanced';

/* 场景结束会 `window.close()`，但页面里仍可能有未完成的异步续体（startRun 收尾的
 * loadRun → refreshHistory）。jsdom 关窗后 document 失效，这类续体会抛
 * "Cannot read properties of undefined (reading 'getElementById')" 把整个进程带崩——
 * 那是脚手架时序噪声，不是产品缺陷：只忽略这一种，其余异常照旧让进程失败。 */
process.on('uncaughtException', (error) => {
  const text = String((error && error.message) || '');
  if (/Cannot read properties of undefined \(reading 'getElementById'\)/.test(text)) return;
  console.error(error);
  process.exit(1);
});

/* --------------------------------------------------------------------------
 * 标准 Agent Protocol（阶段 2）：前端现在请求
 *   POST /threads                       → 线程 id
 *   POST /threads/{tid}/runs/stream     → 标准 SSE 帧
 * 这里把各场景原本的「内部事件」序列**翻译成标准帧**，场景本身不用改。
 * ------------------------------------------------------------------------ */
function isStdRunsStream(url) {
  return !!url && url.indexOf('/runs/stream') >= 0;
}
function isStdThreadsCreate(url, init) {
  return !!url && /\/threads$/.test(url) && !!init && String(init.method || 'POST').toUpperCase() === 'POST';
}
function stdThreadResponse() {
  return Promise.resolve(new Response(JSON.stringify({ thread_id: 'e2e-thread-1' }), {
    status: 200, headers: { 'Content-Type': 'application/json' }
  }));
}
/** 标准请求体 {assistant_id, stream_mode, input} → 展平后的业务参数。
 *  这样各场景的旧断言（直接读业务字段）不用改，只把信封换个位置校验。 */
function flattenRunRequest(raw, envelopes) {
  const body = raw && typeof raw === 'object' ? raw : {};
  if (!body.assistant_id && !body.input) return body;
  if (envelopes) {
    envelopes.push({
      assistant_id: body.assistant_id,
      stream_mode: body.stream_mode,
      input_keys: Object.keys(body.input || {}).sort()
    });
  }
  return { ...(body.input || {}) };
}
/** 内部事件 → 标准帧（event 名 + data）。 */
function stdFramePairs(ev) {
  const type = ev && ev.type ? ev.type : '';
  const ts = ev && ev.ts !== undefined ? ev.ts : undefined;
  /* messages/partial 与 messages/complete 的 data 是**数组**（消息块列表）：
     不能为了塞 ts 把数组摊成对象，否则前端收到 {0:…,ts:…} 会当成「没有内容」。 */
  const withTs = (data) => (ts === undefined || Array.isArray(data) ? data : { ...data, ts });
  if (type === 'token') {
    return [['messages/partial', withTs([{ type: 'AIMessageChunk', content: ev.content || '', id: 'e2e-chunk' }])]];
  }
  if (type === 'update') {
    return [['updates', withTs({ [ev.node || 'node']: { keys: ev.keys || [] } })]];
  }
  if (type === 'final') return [['messages/complete', withTs([{ type: 'AIMessage', content: ev.content || '' }])]];
  if (type === 'done') {
    return [['values', withTs({ run_id: ev.run_id || 'e2e-run', summary: ev.summary || {} })]];
  }
  if (type === 'error') {
    return [['error', withTs({ error: ev.error_code || 'error', message: ev.error_message || '失败' })]];
  }
  if (type === 'start') {
    const meta = { run_id: 'run_e2e', attempt: 1, thread_id: 'e2e-thread-1', graph_id: 'coordinator' };
    return [['metadata', withTs(meta)], ['custom', withTs(ev)]];
  }
  return [['custom', withTs(ev)]];
}
/** 单个内部事件 → 标准 SSE 报文（可能含多帧）。 */
function stdSSE(ev) {
  return stdFramePairs(ev)
    .map(([name, data]) => 'event: ' + name + '\ndata: ' + JSON.stringify(data) + '\n\n')
    .join('');
}
const results = [];

function check(ok, label, detail) {
  results.push([!!ok, label, detail || '']);
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label}${detail ? '  →  ' + detail : ''}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, options) {
  const res = await fetch(BASE + path, options);
  const text = await res.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) { data = null; }
  return { status: res.status, data, text };
}

function putSettings(settings) {
  return api('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ settings })
  });
}

async function openPage(hash, storage) {
  const dom = await JSDOM.fromURL(BASE + ADVANCED + (hash || ''), {
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url);
        return fetch(new URL(url, BASE).toString(), init);
      };
      window.confirm = () => true;
      window.alert = () => {};
      // 模拟「浏览器刷新」：把上一次页面的 localStorage 预置进新窗口
      if (storage) {
        Object.keys(storage).forEach((key) => {
          try { window.localStorage.setItem(key, storage[key]); } catch (error) { /* 忽略 */ }
        });
      }
    }
  });
  const { window } = dom;
  for (let i = 0; i < 100; i += 1) {
    if (window.document.querySelectorAll('.role-card').length > 0) break;
    await sleep(200);
  }
  /* 再等 init() 的异步续体跑完（设置 / 受体 / 历史都加载完）：
     否则在页面启动未完成时关窗，会让它后续的 document 访问抛未处理拒绝并让 Node 直接退出。 */
  for (let i = 0; i < 100; i += 1) {
    if (window.__dshReady === true) break;
    await sleep(100);
  }
  return dom;
}

/* 本次会被改写的路径（结束时按快照恢复） */
const TOUCHED = {
  'llm.model': '',
  'docking.exhaustiveness': '',
  'docking.save_poses': '',
  'roles.docking.model': '',
  'roles.docking.temperature': '',
  'roles.binding.model': ''
};

/* 测试前置状态：清掉角色级覆盖（才能验证「继承」语义），并给一个已知的对接默认值 */
const SETUP = {
  docking: { exhaustiveness: 11 },
  roles: {
    docking: { model: '', temperature: '' },
    binding: { model: '' }
  }
};

/* 用合成 SSE 事件流驱动真实前端逻辑：验证「运行图按实测并行情况展示」。
   事件带服务端风格的时间戳 ts，前端据此算真实起止与重叠。 */
function stubAgentStream(window, events, baseMs) {
  const encoder = new TextEncoder();
  const original = window.fetch;
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url);
    if (isStdThreadsCreate(url, init)) return stdThreadResponse();
    if (!isStdRunsStream(url)) {
      return original(input, init);
    }
    const stream = new ReadableStream({
      start(controller) {
        let i = 0;
        const push = () => {
          if (i >= events.length) {
            controller.close();
            return;
          }
          const event = events[i];
          const payload = { ...event, ts: baseMs + event.at };
          controller.enqueue(encoder.encode(stdSSE(payload)));
          i += 1;
          setTimeout(push, 120);      // 真实分帧下发，客户端时间戳随之推进
        };
        push();
      }
    });
    return Promise.resolve(new Response(stream, {
      status: 200, headers: { 'Content-Type': 'text/event-stream' }
    }));
  };
  return original;
}

/* 场景：口袋分析串行 → 属性评估与对接**并行**（时间重叠）→ 结合模式 → 报告 */
function parallelEvents() {
  return [
    { type: 'start', run_id: 'E2E-PAR', task_spec: { task_type: 'screening', authority: 'manual',
                                                     decision: 'run', source: 'rules' }, at: 0 },
    { type: 'tool_call', tool: 'run_pocket_analysis', at: 100 },
    { type: 'tool_call', tool: 'predict_binding_pockets', at: 200 },
    { type: 'tool_result', tool: 'predict_binding_pockets', at: 900 },
    { type: 'tool_result', tool: 'run_pocket_analysis', at: 1000 },
    { type: 'tool_call', tool: 'run_property_assessment', at: 1100 },
    { type: 'tool_call', tool: 'run_docking', at: 1150 },          // 与属性评估重叠 → 并行
    { type: 'tool_call', tool: 'molecular_property_assessment', at: 1200 },
    { type: 'tool_result', tool: 'molecular_property_assessment', at: 2000 },
    { type: 'tool_result', tool: 'run_property_assessment', at: 2100 },
    { type: 'tool_call', tool: 'molecular_docking', at: 2150 },
    { type: 'tool_result', tool: 'molecular_docking', at: 3600 },
    { type: 'tool_result', tool: 'run_docking', at: 3700 },
    { type: 'tool_call', tool: 'run_binding_mode_analysis', at: 3800 },
    { type: 'tool_result', tool: 'run_binding_mode_analysis', at: 4500 },
    { type: 'tool_call', tool: 'generate_screening_report', at: 4600 },
    { type: 'tool_result', tool: 'generate_screening_report', at: 5000 },
    { type: 'stage', stage: 'verify', at: 5050 },
    { type: 'done', run_id: 'E2E-PAR', summary: { status: 'ok' }, at: 5100 }
  ];
}

async function runParallelScenario(dom, check, sleep) {
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  stubAgentStream(window, parallelEvents(), Date.now());
  // 参数模式恒为多 Agent（运行方式选择器已随流水线移除）；给受体来源与一个有效分子库后点「开始运行」
  if ($('#receptor-source')) {
    $('#receptor-source').value = '1DWC';
    $('#receptor-source').dispatchEvent(new window.Event('input', { bubbles: true }));
  }
  const ligands = $('#ligands-text');
  ligands.value = '乙醇:CCO';
  ligands.dispatchEvent(new window.Event('input', { bubbles: true }));
  await sleep(100);
  $('#btn-start').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  // 等这次合成运行**收敛**（done → [ OK ]）再看时间轴，避免读到中间态
  for (let i = 0; i < 80; i += 1) {
    const state = $('#orch-run-state').textContent || '';
    if (/OK|FAIL|CANCEL/.test(state) && doc.querySelectorAll('.orch-lane-bar').length >= 4) break;
    await sleep(200);
  }
  check((doc.querySelector('#exec-run-id').textContent || '').indexOf('E2E-PAR') >= 0,
    '合成事件流确实由前端自己的 SSE 处理链路消费', doc.querySelector('#exec-run-id').textContent);
  const bars = Array.from(doc.querySelectorAll('.orch-lane-bar'));
  check(bars.length >= 3, `运行图按节点画出了实测时间条（${bars.length} 根）`);
  const badge = $('#orch-parallel');
  check(/并行/.test(badge.textContent), '并行度徽标显示实测并行（属性×对接重叠）', badge.textContent);
  // 属性评估与对接两根条在时间轴上必须重叠（左侧位置 + 宽度有交集）
  const laneOf = (node) => Array.from(doc.querySelectorAll('.orch-lane'))
    .find((row) => row.dataset.node === node);
  const rect = (row) => {
    if (!row) return null;
    const bar = row.querySelector('.orch-lane-bar');
    if (!bar) return null;
    const left = parseFloat(bar.style.left) || 0;
    const width = parseFloat(bar.style.width) || 0;
    return { left, right: left + width };
  };
  const props = rect(laneOf('properties'));
  const dock = rect(laneOf('docking'));
  check(!!props && !!dock, '属性评估与对接都有时间条');
  if (props && dock) {
    const overlap = Math.min(props.right, dock.right) - Math.max(props.left, dock.left);
    check(overlap > 0, '两根条在时间轴上真实重叠（= 实测并行）',
      `重叠 ${overlap.toFixed(2)}%`);
  }
  const pocket = rect(laneOf('pocket'));
  if (pocket && props) {
    check(pocket.right <= props.left + 0.001, '口袋分析与属性评估不重叠（= 实测串行）',
      `pocket 止于 ${pocket.right.toFixed(2)}%，properties 起于 ${props.left.toFixed(2)}%`);
  }
}

/* 场景：实时逐分子结果 —— 合成 `molecules` 批量事件必须真的渲染成表格行并隐藏空状态。
   这是真实缺陷回归：多 Agent（对话）运行时右侧「实时分子结果」一直空着。 */
function liveMoleculeEvents() {
  return [
    { type: 'start', run_id: 'E2E-LIVE', task_spec: { task_type: 'screening', authority: 'manual',
                                                      decision: 'run', source: 'rules' }, at: 0 },
    { type: 'molecules', items: [
      { index: 0, total: 3, name: '乙醇', smiles: 'CCO', affinity_kcal_mol: -4.2,
        engine: 'vina', exhaustiveness: 1,
        properties: { molecular_weight: 46.07, logP: -0.001, tpsa: 20.23 } },
      { index: 1, total: 3, name: '甲醇', smiles: 'CO', affinity_kcal_mol: -3.1,
        engine: 'vina', exhaustiveness: 1,
        properties: { molecular_weight: 32.04, logP: -0.001, tpsa: 20.23 } },
      { index: 2, total: 3, name: '苯酚', smiles: 'Oc1ccccc1', affinity_kcal_mol: -5.6,
        engine: 'vina', exhaustiveness: 1,
        properties: { molecular_weight: 94.11, logP: 1.39, tpsa: 20.23 } }
    ], at: 300 },
    { type: 'done', run_id: 'E2E-LIVE', summary: { status: 'ok', molecule_count: 3 }, at: 600 }
  ];
}

async function runLiveMoleculesScenario(dom, check, sleep) {
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  stubAgentStream(window, liveMoleculeEvents(), Date.now());
  // 受体是必需项：先给出受体来源（没有预置受体下拉了），再给分子库
  if ($('#receptor-source')) {
    $('#receptor-source').value = '1DWC';
    $('#receptor-source').dispatchEvent(new window.Event('input', { bubbles: true }));
  }
  const ligands = $('#ligands-text');
  ligands.value = '乙醇:CCO';
  ligands.dispatchEvent(new window.Event('input', { bubbles: true }));
  await sleep(120);
  $('#btn-start').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  const tbody = $('#molecules-tbody');
  for (let i = 0; i < 60; i += 1) {
    if (tbody && tbody.childElementCount >= 3) break;
    await sleep(100);
  }
  const rows = tbody ? tbody.childElementCount : 0;
  check(rows >= 3, `合成 molecules 事件驱动实时表出现数据行（${rows} 行）`);
  const empty = $('#molecules-empty');
  check(!!empty && empty.classList.contains('hidden'),
    '实时表出现行后空状态被隐藏', empty ? empty.className : '缺失');
  check((($('#live-count') || {}).textContent || '') === '3',
    '实时计数随逐分子事件累加', ($('#live-count') || {}).textContent);
  check(!!tbody && /-4\.20/.test(tbody.textContent || ''), '数据行渲染出亲和力数值',
    tbody ? (tbody.textContent || '').replace(/\s+/g, ' ').slice(0, 80) : '缺失');
}

/* 场景：多轮会话 —— 同一 conversation_id 连续发送、点「新对话」换 id 并清空气泡、
   刷新后从 localStorage 恢复同一个 id。stub 掉 /api/agent/stream 并记录请求体。 */
async function runConversationScenario(check, sleep) {
  const dom = await openPage('#chat');
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  const bodies = [];
  const originalFetch = window.fetch;
  const encoder = new TextEncoder();
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url);
    if (isStdThreadsCreate(url, init)) return stdThreadResponse();
    if (!isStdRunsStream(url)) return originalFetch(input, init);
    let body = {};
    try { body = JSON.parse((init && init.body) || '{}'); } catch (error) { body = {}; }
    body = flattenRunRequest(body);
    bodies.push(body);
    const runId = 'E2E-CONV-' + bodies.length;
    const events = [
      { type: 'start', run_id: runId, conversation_id: body.conversation_id },
      { type: 'final', content: '第 ' + bodies.length + ' 轮回复' },
      { type: 'done', run_id: runId, summary: { status: 'ok' } }
    ];
    const stream = new ReadableStream({
      start(controller) {
        let i = 0;
        const push = () => {
          if (i >= events.length) { controller.close(); return; }
          const payload = { ...events[i], ts: Date.now() };
          controller.enqueue(encoder.encode(stdSSE(payload)));
          i += 1;
          setTimeout(push, 20);
        };
        push();
      }
    });
    return Promise.resolve(new Response(stream, {
      status: 200, headers: { 'Content-Type': 'text/event-stream' }
    }));
  };

  const readId = () => {
    try { return window.localStorage.getItem('dsh_conversation_id') || ''; } catch (error) { return ''; }
  };
  const bubbleCount = () => doc.querySelectorAll('#chat-history .chat-msg').length;
  const isRunning = () => !!($('#btn-start') && $('#btn-start').disabled);

  const send = async (text) => {
    const input = $('#chat-input');
    input.value = text;
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    $('#chat-send').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    const before = bodies.length;
    for (let i = 0; i < 100; i += 1) {
      if (!isRunning() && bodies.length > before) break;
      await sleep(100);
    }
    await sleep(150);
  };

  const startedWith = readId();
  check(!!startedWith, '进入页面即生成会话 id 并写入 localStorage', String(startedWith));
  check((($('#exec-conversation-id') || {}).textContent || '').indexOf(startedWith.slice(0, 8)) >= 0,
    '顶部显示当前会话短 id', ($('#exec-conversation-id') || {}).textContent);

  await send('帮我筛这两个分子 CCO');
  await send('用 trypsin');
  check(bodies.length === 2, `连续两次发送都带上了请求体（${bodies.length} 次）`,
    JSON.stringify(bodies.map((b) => b.conversation_id)));
  check(!!bodies[0] && bodies[0].conversation_id === bodies[1].conversation_id,
    '两次提交使用同一个 conversation_id',
    String(bodies[0] && bodies[0].conversation_id));
  check(bodies[1] && bodies[1].conversation_id === startedWith,
    '请求体里的 conversation_id 与页面当前会话一致');
  check(bubbleCount() >= 4,
    `同一会话内气泡持续累积（不因新一轮而清空上一轮）`, `${bubbleCount()} 条`);

  const sameId = bodies[1] && bodies[1].conversation_id;
  $('#btn-chat-new').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await sleep(80);
  const newId = readId();
  check(!!newId && newId !== sameId, '点「新对话」后 conversation_id 变化', String(newId));
  check(bubbleCount() === 0, '点「新对话」后气泡被清空', `${bubbleCount()} 条`);
  check((($('#exec-conversation-id') || {}).textContent || '').indexOf(newId.slice(0, 8)) >= 0,
    '顶部会话短 id 同步为新会话', ($('#exec-conversation-id') || {}).textContent);
  check(/已开始新对话/.test($('#run-hint').textContent || ''), '提示已开始新对话',
    ($('#run-hint').textContent || '').slice(0, 40));

  // 模拟浏览器刷新：把 localStorage 里的 id 带进新窗口，应恢复到同一会话
  const refreshed = await openPage('#chat', { dsh_conversation_id: newId });
  await sleep(150);
  const rdoc = refreshed.window.document;
  const rlabel = (rdoc.querySelector('#exec-conversation-id') || {}).textContent || '';
  let rId = '';
  try { rId = refreshed.window.localStorage.getItem('dsh_conversation_id') || ''; }
  catch (error) { rId = ''; }
  check(rId === newId && rlabel.indexOf(newId.slice(0, 8)) >= 0,
    '刷新（重新打开页面）后仍从 localStorage 读到同一个会话 id', `${rId} / ${rlabel}`);
  dom.window.close();
  refreshed.window.close();
}

/* 场景：工具调用轨迹只进「工具轨迹」面板，**不得**镜像到对话气泡。
   真实体验问题：气泡里堆满「调用工具：X / 工具返回：X / 节点更新：tools」，
   与右侧面板重复且淹没报告正文。 */
async function runChatTraceSeparationScenario(check, sleep) {
  const dom = await openPage('#chat');
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  const originalFetch = window.fetch;
  const encoder = new TextEncoder();
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url);
    /* 收尾会 loadRun() 拉一次运行详情（成功后才写「运行完成」简讯）→ 桩掉它，
       否则 404 会让收尾走 catch 分支，测不到气泡里的运行级状态。 */
    if (url && url.indexOf('/api/runs/') >= 0) {
      return Promise.resolve(new Response(JSON.stringify({
        run: { run_id: 'E2E-TRACE', status: 'ok', molecule_count: 1, kind: 'agent' },
        result: { aggregates: {}, ranking: [], ranking_total: 0, molecules: [] },
        artifacts: [],
        /* 规范化报告（落盘 report.md）——必须出现在气泡里，且与模型叙述并存 */
        report_markdown: '## 规范报告 · 测试节\n\n| 排名 | 分子 | 亲和力 |\n| --- | --- | --- |\n'
          + '| 1 | A | -9.1 |\n',
        downloads: {}, log: [], collaboration: {}
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    }
    if (isStdThreadsCreate(url, init)) return stdThreadResponse();
    if (!isStdRunsStream(url)) return originalFetch(input, init);
    const events = [
      { type: 'start', run_id: 'E2E-TRACE' },
      { type: 'tool_call', name: 'run_pocket_analysis', node: 'model' },
      { type: 'tool_result', name: 'run_pocket_analysis', node: 'tools',
        content: '{"status":"ok","pockets":[]}' },
      { type: 'update', node: 'tools', keys: ['messages'] },
      { type: 'final', content: '**叙述与结论**：优先推进 A。\n\n'
        + '亲和力排序图：http://127.0.0.1:9999/affinity.png\n'
        + '参考 https://example.com/paper 与 [文献](https://example.org/x)。' },
      { type: 'done', run_id: 'E2E-TRACE', summary: { status: 'ok' } }
    ];
    const stream = new ReadableStream({
      start(controller) {
        let i = 0;
        const push = () => {
          if (i >= events.length) { controller.close(); return; }
          controller.enqueue(encoder.encode(
            'data: ' + JSON.stringify({ ...events[i], ts: Date.now() }) + '\n\n'));
          i += 1;
          setTimeout(push, 15);
        };
        push();
      }
    });
    return Promise.resolve(new Response(stream,
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
  };
  const input = $('#chat-input');
  input.value = '做一次筛选';
  input.dispatchEvent(new window.Event('input', { bubbles: true }));
  $('#chat-send').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  for (let i = 0; i < 60; i += 1) {
    if (/运行完成/.test(($('#chat-history').textContent || ''))) break;
    await sleep(100);
  }
  const chatText = $('#chat-history').textContent || '';
  const noteText = Array.from(doc.querySelectorAll('#chat-history .chat-note'))
    .map((n) => n.textContent).join(' | ');
  check(!/调用工具|工具返回|节点更新/.test(noteText),
    '对话气泡不再显示工具调用/返回/节点更新（只保留运行状态）', noteText.slice(0, 120));
  check(!/pockets/.test(chatText) && !/run_pocket_analysis/.test(chatText),
    '工具返回原文不得出现在对话流里', chatText.slice(-160));
  check(/运行完成/.test(noteText), '运行级状态（运行完成 · run_id）仍保留在气泡里',
    noteText.slice(0, 120));
  const trace = $('#tool-trace').textContent || '';
  check(/run_pocket_analysis/.test(trace) && /pockets/.test(trace),
    '工具轨迹面板照常保留调用与返回', trace.slice(0, 120));
  /* B：规范报告（report.md）渲染进气泡，且模型叙述与结论仍保留、裸 URL 被剥掉 */
  check(/规范报告 · 测试节/.test(chatText) && !!doc.querySelector('[data-chat-report-block]'),
    '规范化报告（report.md）作为独立小节渲染进气泡', chatText.slice(-200));
  check(/叙述与结论/.test(chatText) && /优先推进 A/.test(chatText),
    '模型自己的叙述与结论仍保留在气泡正文里', chatText.slice(0, 160));
  check(!/127\.0\.0\.1:9999|example\.com|example\.org/.test(chatText),
    '气泡里不再出现任何裸网址（报告规范）', chatText.slice(-160));
  check(!!doc.querySelector('#chat-history strong') || !!doc.querySelector('#chat-history h2'),
    '助手正文按 Markdown 渲染（不再显示 # / ** 源码）');
  /* 不在这里关窗：startRun 的收尾（refreshHistory）可能还在 await 中，
     提前 close() 会让续体访问 document 抛错把脚本带崩（见文件末尾的统一收尾约定）。 */
}

/* 场景：结构化选项（choices）—— 服务端把「多个候选受体」下发成 choices 事件，
   前端必须在助手气泡下方渲染成可点按钮；点选后以**同一 conversation_id** 追问一句
   等价的话继续跑（多轮会话机制），并且点选后清空按钮避免重复提交。 */

/* 场景：**参数模式**下的结构化选项（真实缺陷回归）。
   原先 choices 只在对话模式的助手气泡里渲染，参数模式被直接丢弃 —— 用户只看到日志
   「已生成 N 个可选项」却没有任何按钮；刷新页面后也无法恢复。
   现在统一由中栏 #choice-box 承载，且载入历史运行时会从 run.json 回填。 */
async function runManualChoicesScenario(check, sleep) {
  const dom = await openPage('#manual');
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  const pending = [
    { id: 'molecule:3034368:raw', kind: 'molecule',
      label: 'Mancozeb（CID 3034368） · PubChem 原始多组分结构',
      value: 'C(CNC(=S)[S-])NC(=S)[S-].[Mn+2]',
      detail: { cid: 3034368, mode: 'raw-mixture', formula: 'C8H12MnN4S8Zn' },
      prompt: '代森锰锌 按 PubChem 原始多组分结构对接' },
    { id: 'molecule:3034368:zn', kind: 'molecule',
      label: 'Mancozeb · Zn-EBDC 单体', value: 'C(CNC(=S)[S-])NC(=S)[S-].[Zn+2]',
      detail: { mode: 'metal-monomer' }, prompt: '代森锰锌 按 Zn-EBDC 单体对接' },
  ];
  const submitted = [];
  const encoder = new TextEncoder();
  const original = window.fetch;
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url);
    if (isStdThreadsCreate(url, init)) return stdThreadResponse();
    if (url && url.indexOf('/api/runs/') >= 0 && (!init || !init.method || init.method === 'GET')) {
      // 载入历史运行：run.json 里带着当时未点选的候选（刷新/换设备也能恢复）
      return Promise.resolve(new Response(JSON.stringify({
        run: { run_id: 'E2E-MANUAL-CHOICE', status: 'interrupted',
               choices: pending, choices_note: '该名称是多组分/聚合物：代表结构需用户确认' },
        result: {}, artifacts: [], log: [], report_markdown: '', downloads: {},
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    }
    if (!isStdRunsStream(url)) return original(input, init);
    let body = {};
    try { body = flattenRunRequest(JSON.parse((init && init.body) || '{}')); } catch (error) { body = {}; }
    submitted.push(body);
    const call = submitted.length;
    // 只有**首轮**下发 choices：续跑那轮不再发，否则面板会被重新填满（桩与真实行为一致）
    const frames = call === 1
      ? [{ type: 'start', run_id: 'E2E-MANUAL-1' },
         { type: 'choices', note: '该名称是多组分/聚合物：代表结构需用户确认', choices: pending },
         { type: 'done', run_id: 'E2E-MANUAL-1' }]
      : [{ type: 'start', run_id: 'E2E-MANUAL-2' }, { type: 'done', run_id: 'E2E-MANUAL-2' }];
    const stream = new ReadableStream({
      start(controller) {
        frames.forEach((frame) => {
          controller.enqueue(encoder.encode(stdSSE({ ...frame, ts: Date.now() })));
        });
        controller.close();
      },
    });
    return Promise.resolve(new Response(stream, {
      status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
  };

  // 参数模式要求指定受体来源（**没有**预置受体下拉了）：填一个 PDB 编号即可
  const receptorSource = $('#receptor-source');
  if (receptorSource) {
    receptorSource.value = '1DWC';
    receptorSource.dispatchEvent(new window.Event('input', { bubbles: true }));
  }
  $('#ligands-text').value = '代森锰锌';
  $('#ligands-text').dispatchEvent(new window.Event('input', { bubbles: true }));
  $('#btn-start').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  for (let i = 0; i < 100; i += 1) {
    if (doc.querySelectorAll('#choice-list .chat-choice').length > 0) break;
    await sleep(100);
  }
  const panel = doc.querySelector('#choice-box');
  const buttons = Array.from(doc.querySelectorAll('#choice-list .chat-choice'));
  check(!!panel && !panel.classList.contains('hidden'),
    '参数模式下 choices 渲染成可见的「需要你确认的选项」面板', $('#run-hint').textContent);
  check(buttons.length === pending.length,
    `面板里是全部候选（${buttons.length}/${pending.length}）`);
  check(buttons.every((b) => b.tagName === 'BUTTON'), '候选渲染为真正的 button');
  check(!!buttons[0] && /CID：3034368/.test(buttons[0].textContent || ''),
    '候选详情按可读文本渲染（结构化 detail 不再显示成 [object Object]）',
    buttons[0] ? buttons[0].textContent.slice(0, 70) : '缺失');

  /* 点选后的完整链路（清空面板 + 同一会话续跑 + 追问带上候选）由对话模式场景
     runChatChoicesScenario 端到端覆盖且稳定；本场景专注"参数模式下必须看得见"这一缺陷，
     只做可见性与回填断言，避免与桩/重渲染的时序纠缠。 */

  // 载入历史运行：从 run.json 回填候选（刷新/换浏览器也能看到）
  await window.loadRun('E2E-MANUAL-CHOICE', { silent: true });
  await sleep(250);
  const restored = Array.from(doc.querySelectorAll('#choice-list .chat-choice'));
  check(restored.length === pending.length, `载入历史运行后候选被回填（${restored.length} 个）`);
  check(!$('#choice-box').classList.contains('hidden'), '回填后面板可见（刷新不再丢失可选项）');
  window.close();
  await sleep(50);
}

async function runChatChoicesScenario(check, sleep) {
  const dom = await openPage('#chat');
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  const bodies = [];
  const originalFetch = window.fetch;
  const encoder = new TextEncoder();
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url);
    if (isStdThreadsCreate(url, init)) return stdThreadResponse();
    if (!isStdRunsStream(url)) return originalFetch(input, init);
    let body = {};
    try { body = JSON.parse((init && init.body) || '{}'); } catch (error) { body = {}; }
    body = flattenRunRequest(body);
    bodies.push(body);
    const runId = 'E2E-CHOICE-' + bodies.length;
    const events = bodies.length === 1
      ? [
        { type: 'start', run_id: runId, conversation_id: body.conversation_id },
        { type: 'choices', note: '检索到多个同样合理的候选：请选择要使用的受体',
          choices: [
            { id: 'receptor:P08922', kind: 'receptor',
              label: 'Homo sapiens 人源 ROS1 激酶 · P08922 · RCSB 3ZBF · 打分 64',
              value: 'P08922',
              prompt: '用 P08922（Homo sapiens，Proto-oncogene tyrosine-protein kinase ROS）作为受体继续对接筛选' },
            { id: 'receptor:Q9SJQ6', kind: 'receptor',
              label: 'Arabidopsis thaliana ROS1 去甲基化酶 · Q9SJQ6 · RCSB 7YHP · 打分 64',
              value: 'Q9SJQ6',
              prompt: '用 Q9SJQ6（Arabidopsis thaliana，DNA glycosylase/AP lyase ROS1）作为受体继续对接筛选' }
          ] },
        { type: 'final', content: '检索到多个候选，请在上方点选后继续。' },
        { type: 'done', run_id: runId, summary: { status: 'ok' } }
      ]
      : [
        { type: 'start', run_id: runId, conversation_id: body.conversation_id },
        { type: 'final', content: '已按 Q9SJQ6 继续对接。' },
        { type: 'done', run_id: runId, summary: { status: 'ok' } }
      ];
    const stream = new ReadableStream({
      start(controller) {
        let i = 0;
        const push = () => {
          if (i >= events.length) { controller.close(); return; }
          const payload = { ...events[i], ts: Date.now() };
          controller.enqueue(encoder.encode(stdSSE(payload)));
          i += 1;
          setTimeout(push, 20);
        };
        push();
      }
    });
    return Promise.resolve(new Response(stream, {
      status: 200, headers: { 'Content-Type': 'text/event-stream' }
    }));
  };

  const input = $('#chat-input');
  input.value = '从在线数据库中获取植物去甲基化酶ROS1，与小分子代森锰锌进行对接筛选';
  input.dispatchEvent(new window.Event('input', { bubbles: true }));
  $('#chat-send').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  for (let i = 0; i < 100; i += 1) {
    if (doc.querySelectorAll('.chat-choice').length > 0) break;
    await sleep(100);
  }
  const buttons = Array.from(doc.querySelectorAll('.chat-choice'));
  check(buttons.length >= 2, `choices 事件渲染成可选按钮（${buttons.length} 个）`);
  // 回归：同一批候选只能出现在一个承载面 —— 对话模式用气泡，面板必须为空
  check(doc.querySelectorAll('#choice-list .chat-choice').length === 0,
    '对话模式下候选只出现在助手气泡里（面板不同时显示，避免两份）',
    `面板按钮 ${doc.querySelectorAll('#choice-list .chat-choice').length} 个`);
  check(buttons.every((b) => b.tagName === 'BUTTON'), '选项渲染为真正的 button 元素');
  check(!!buttons[0] && buttons[0].getAttribute('data-choice-value') === 'P08922',
    '按钮带机器可用的 data-choice-value（accession）',
    buttons[0] ? buttons[0].getAttribute('data-choice-value') : '缺失');
  check(!!buttons[0] && /P08922/.test(buttons[0].textContent || ''),
    '按钮文案含 accession 与物种/结构来源（人看的信息）',
    buttons[0] ? buttons[0].textContent.slice(0, 60) : '缺失');

  if (buttons.length >= 2) {
    buttons[1].dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    for (let i = 0; i < 100; i += 1) {
      if (bodies.length >= 2 && !$('#btn-start').disabled) break;
      await sleep(100);
    }
    await sleep(150);
    check(bodies.length === 2, `点选后确实又提交了一轮（${bodies.length} 次请求）`);
    check(!!bodies[1] && bodies[1].conversation_id === bodies[0].conversation_id,
      '点选追问沿用同一 conversation_id（延续同一段对话）',
      String(bodies[1] && bodies[1].conversation_id));
    check(!!bodies[1] && String(bodies[1].message || '').indexOf('Q9SJQ6') >= 0,
      '点选追问里带上了所选候选（Q9SJQ6）', String(bodies[1] && bodies[1].message).slice(0, 70));
    check(doc.querySelectorAll('.chat-choice').length === 0, '点选后按钮被清空，避免重复提交');
    check(String($('#chat-input').value || '') === '',
      '点选后输入框保持为空（选项内容不残留在对话框里）',
      JSON.stringify(String($('#chat-input').value || '').slice(0, 40)));
  }
  /* 等第二轮真正跑完再关窗：否则收尾的异步续体（loadRun → refreshHistory）
     会在 document 失效后抛错，把 e2e 进程带崩。 */
  for (let i = 0; i < 40; i += 1) {
    const startBtn = doc.querySelector('#btn-start');
    if (startBtn && !startBtn.disabled) break;
    await sleep(50);
  }
  await sleep(150);
  dom.window.close();
}

/* 场景（v0.26）：只改动一个运行参数 → 只下发那一个字段；
   「打开过其他设置再关掉」不得下发任何参数（用户实测反馈）。 */
/** 最后一个用户气泡的文本（小票「本次下发参数：…」就在这个气泡里）。 */
function lastUserBubbleText(doc) {
  const bubbles = doc.querySelectorAll('.chat-msg.user .chat-bubble');
  return bubbles.length ? bubbles[bubbles.length - 1].textContent : '';
}

async function runQuickParamInjectionScenario(check, sleep) {
  const dom = await openPage('#chat');
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  const bodies = [];
  const envelopes = [];
  const originalFetch = window.fetch;

  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url);
    if (isStdThreadsCreate(url, init)) return stdThreadResponse();
    if (isStdRunsStream(url)) {
      bodies.push(flattenRunRequest(JSON.parse(String(init && init.body || '{}')), envelopes));
      return Promise.resolve(new Response(
        'data: ' + JSON.stringify({ type: 'start', run_id: 'E2E-QUICK' }) + '\n\n'
        + 'data: ' + JSON.stringify({ type: 'final', content: 'ok' }) + '\n\n'
        + 'data: ' + JSON.stringify({ type: 'done', summary: {} }) + '\n\n',
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
    }
    if (url && url.indexOf('/api/runs/') >= 0) {
      return Promise.resolve(new Response(JSON.stringify({
        run: { run_id: 'E2E-QUICK', status: 'ok', molecule_count: 1, kind: 'agent' },
        result: { aggregates: {}, ranking: [], ranking_total: 0, molecules: [] },
        artifacts: [], report_markdown: '', downloads: {}, log: [], collaboration: {} }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }));
    }
    return originalFetch(input, init);
  };

  const send = async (text) => {
    const input = $('#chat-input');
    input.value = text;
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    $('#chat-send').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    for (let i = 0; i < 60; i += 1) {
      if (bodies.length > 0 && !$('#btn-start').disabled) break;
      await sleep(80);
    }
    await sleep(120);
  };

  /* ① 什么都不改（含"打开其他设置再关掉"） */
  const adv = $('#chat-advanced');
  adv.open = true;
  adv.dispatchEvent(new window.Event('toggle', { bubbles: false }));
  adv.open = false;
  adv.dispatchEvent(new window.Event('toggle', { bubbles: false }));
  await sleep(60);
  await send('什么都不改，直接跑');
  const first = bodies.shift() || {};
  check(first.advanced === false && first.exhaustiveness === undefined
    && first.engine === undefined && first.site_center === undefined,
    '打开过「其他设置」再关掉 → 仍然不下发任何参数', JSON.stringify(first));
  /* 真实缺陷（用户实测反馈）：气泡写「本次下发参数：受体 thrombin / 位点中心 … / 引擎 vina」，
     而请求体里根本没有这些字段（对话模式的受体下拉与位点盒是隐藏项）。
     小票必须与请求体同源：什么都没改 = 纯指令，不得出现受体/位点。 */
  const firstChips = lastUserBubbleText(doc);
  check(/纯指令/.test(firstChips) && firstChips.indexOf('位点') < 0
    && firstChips.indexOf('受体') < 0 && firstChips.indexOf('引擎') < 0,
    '什么都没改 → 气泡不谎报「受体 / 位点盒 / 引擎」已下发',
    firstChips.replace(/\s+/g, ' ').slice(0, 120));

  /* ② 取消「自动」并把搜索强度拖到 24 → 只下发 exhaustiveness */
  const auto = $('#exhaustiveness-auto');
  auto.checked = false;
  auto.dispatchEvent(new window.Event('change', { bubbles: true }));
  await sleep(40);
  check($('#exhaustiveness').disabled === false, '取消「自动」后滑块可编辑');
  $('#exhaustiveness').value = '24';
  $('#exhaustiveness').dispatchEvent(new window.Event('input', { bubbles: true }));
  await send('搜索强度用 24');
  const second = bodies.shift() || {};
  check(second.advanced === true && String(second.exhaustiveness) === '24',
    '只改动搜索强度 → 只下发 exhaustiveness=24', JSON.stringify(second));
  check(second.engine === undefined && second.n_poses === undefined
    && second.site_center === undefined && second.positive_control === undefined,
    '其余未改动的参数仍然不下发', JSON.stringify(second));
  const secondChips = lastUserBubbleText(doc);
  check(/exhaustiveness=24/.test(secondChips) && secondChips.indexOf('位点') < 0
    && secondChips.indexOf('受体') < 0 && secondChips.indexOf('质子化') < 0
    && secondChips.indexOf('引擎') < 0,
    '小票只列真的下发的项（改动过的 exhaustiveness 在、未改动的都不在）',
    secondChips.replace(/\s+/g, ' ').slice(0, 140));

  /* ③ 恢复默认 → 回到"自动"，且不再下发 */
  $('#btn-restore-defaults').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await sleep(60);
  check($('#exhaustiveness-auto').checked === true && $('#exhaustiveness').disabled === true,
    '恢复默认回到「自动」并禁用滑块');
  await send('再跑一次');
  const third = bodies.shift() || {};
  check(third.advanced === false && third.exhaustiveness === undefined,
    '恢复默认后不再下发参数', JSON.stringify(third));

  /* ④ 「其他设置」里手填的 SMILES 是**看得见、填得进的输入** → 必须真的下发。
     真实缺陷：它只在 advanced=true 时才进请求体，改动它本身不触发 advanced，
     于是用户填了分子库却"什么都没发出去"（请求体里没有 ligands_text）。 */
  adv.open = true;
  adv.dispatchEvent(new window.Event('toggle', { bubbles: false }));
  await sleep(60);
  const lig = $('#ligands-text');
  lig.value = 'CCO\nCCN';
  lig.dispatchEvent(new window.Event('input', { bubbles: true }));
  await sleep(40);
  await send('换成这两个分子');
  const fourth = bodies.shift() || {};
  check(String(fourth.ligands_text || '') === 'CCO\nCCN' && fourth.advanced === true,
    '「其他设置」里手填的 SMILES 会真的下发（不再被 advanced=false 静默丢掉）',
    JSON.stringify({ligands_text: fourth.ligands_text, advanced: fourth.advanced}));
  const fourthChips = lastUserBubbleText(doc);
  check(/SMILES 文本 2 个分子/.test(fourthChips) && fourthChips.indexOf('位点') < 0
    && fourthChips.indexOf('受体 ') < 0,
    '小票如实显示「SMILES 文本 2 个分子」且不含未下发的受体/位点',
    fourthChips.replace(/\s+/g, ' ').slice(0, 140));

  /* ⑤ 请求信封必须是标准 Agent Protocol 形状（阶段 2 前端已切到 /runs/stream） */
  check(envelopes.length === 4 && envelopes.every((e) => e.assistant_id === 'coordinator'),
    '四轮都以 assistant_id=coordinator 走标准运行接口', JSON.stringify(envelopes));
  check(envelopes.every((e) => Array.isArray(e.stream_mode) && e.stream_mode.indexOf('messages') >= 0),
    '四轮都声明了标准 stream_mode（含 messages）', JSON.stringify(envelopes));
  check(envelopes.every((e) => e.input_keys.indexOf('message') >= 0 || e.input_keys.indexOf('messages') >= 0),
    '业务内容放在标准请求体的 input 字段内', JSON.stringify(envelopes));
}

/* 场景：设置页的返回入口（用户反馈「还有没有返回按钮」）。
 * 点「← 返回工作台」或按 Esc 都必须真的回到工作台视图，而不是只改 hash。 */
async function runSettingsBackScenario(check, sleep) {
  const dom = await openPage('#/settings');
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  const btn = $('#btn-settings-back');
  check(!!btn, '设置页头部有「返回工作台」按钮');
  check(doc.body.dataset.view === 'settings', '进入设置页时视图为 settings', doc.body.dataset.view);
  if (btn) {
    btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await sleep(150);
    check(doc.body.dataset.view === 'workbench'
      && $('#view-workbench').classList.contains('is-active')
      && !$('#view-settings').classList.contains('is-active'),
      '点「返回工作台」切回工作台（视图与高亮同步）', doc.body.dataset.view);
    check($('#nav-workbench').classList.contains('active')
      && $('#nav-workbench').getAttribute('aria-selected') === 'true',
      '返回后导航栏高亮回到工作台');
  }
  // Esc 也要能返回（焦点不在输入框时）
  doc.body.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  window.location.hash = '#/settings';
  await sleep(200);
  const esc = new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true });
  doc.body.dispatchEvent(esc);
  await sleep(150);
  check(doc.body.dataset.view === 'workbench', 'Esc 也能从设置页返回工作台', doc.body.dataset.view);
  dom.window.close();
}

/* 场景：首屏「无结果」状态 + 运行详情默认收起（v0.28 布局重排）。 */
async function runLayoutStateScenario(check, sleep) {
  const dom = await openPage('#chat');
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  const view = $('#view-workbench');
  check(!!view && view.classList.contains('no-run'),
    '首屏处于「无结果」状态（空排序工具条 / 表格 / 图例收起）', view ? view.className : '缺失');
  const details = $('#run-details');
  check(!!details && details.tagName.toLowerCase() === 'details' && !details.open,
    '运行详情默认收起（编排 / 日志 / 轨迹 / 实时表不占首屏）', details ? String(details.open) : '缺失');
  check(!!$('#chat-quick-params') && !!$('#chat-quick-params').querySelector('#exhaustiveness'),
    '参数条仍在对话上方且不折叠（用户要求：不折进设置里）');
  check(!!$('#chat-history') && !!$('#chat-form') && !!$('#chat-input'),
    '对话区（历史 + 输入）结构完整');
  dom.window.close();
}

/* 场景：「不是可执行任务」的运行（例如用户只说了句「你好」）。
 *
 * 真实缺陷（用户实测）：受理层 decision=reject、零工具调用，后端照样产出 11 KB 全空表格的
 * 「规范报告」+ 4 张空图 + 328 KB PDF，前端把它当作「规范报告（唯一权威版）」挂到气泡上，
 * 并把用户甩到空的结果总览，还写「运行完成 · 结果已载入」。现在：无报告、状态 no_op、
 * 气泡与运行提示都必须如实说「本次未执行计算」。 */
async function runNoOpRunScenario(check, sleep) {
  const dom = await openPage('#chat');
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  const originalFetch = window.fetch;
  const encoder = new TextEncoder();
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url);
    if (isStdThreadsCreate(url, init)) return stdThreadResponse();
    if (isStdRunsStream(url)) {
      const events = [
        { type: 'start', run_id: 'E2E-NOOP-1' },
        { type: 'final', content: '你好！请告诉我候选分子与受体目标。' },
        { type: 'done', run_id: 'E2E-NOOP-1', summary: { status: 'no_op' } }
      ];
      const stream = new ReadableStream({
        start(controller) {
          let i = 0;
          const push = () => {
            if (i >= events.length) { controller.close(); return; }
            controller.enqueue(encoder.encode(stdSSE({ ...events[i], ts: Date.now() })));
            i += 1;
            setTimeout(push, 20);
          };
          push();
        }
      });
      return Promise.resolve(new Response(stream,
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
    }
    if (url && url.indexOf('/api/runs/E2E-NOOP-1') >= 0) {
      return Promise.resolve(new Response(JSON.stringify({
        run: { run_id: 'E2E-NOOP-1', status: 'no_op', molecule_count: 0, kind: 'agent' },
        result: { ranking: [], ranking_total: 0, aggregates: {}, molecules: [] },
        artifacts: [], report_markdown: '', downloads: {}, log: [], collaboration: {}
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    }
    return originalFetch(input, init);
  };

  const input = $('#chat-input');
  input.value = '你好';
  input.dispatchEvent(new window.Event('input', { bubbles: true }));
  $('#chat-send').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  for (let i = 0; i < 80; i += 1) {
    if (!($('#btn-start') && $('#btn-start').disabled)) break;
    await sleep(80);
  }
  await sleep(250);

  const bubble = doc.querySelector('.chat-msg.assistant .chat-bubble');
  const text = bubble ? bubble.textContent : '';
  const hint = String(($('#run-hint') || {}).textContent || '');
  const log = String(($('#log-box') || {}).textContent || '');
  check(!doc.querySelector('.chat-msg.assistant .chat-report'),
    '未执行计算的运行不挂「规范报告」小节（页面不再出现空报告）', text.replace(/\s+/g, ' ').slice(0, 90));
  check(/本次未执行计算（未生成报告）/.test(text),
    '气泡如实说明「本次未执行计算（未生成报告）」', text.replace(/\s+/g, ' ').slice(0, 160));
  check(/未执行计算/.test(hint), '运行提示如实说明未执行计算', hint.slice(0, 90));
  check(/未执行计算/.test(log) && log.indexOf('运行完成') < 0,
    '运行日志写「本次未执行计算」而不是「运行完成」', log.replace(/\s+/g, ' ').slice(0, 140));
  dom.window.close();
}

/* 场景：对话模式下「折叠高级设置 + 有附件」的请求体必须带 receptor_file。
 *
 * 真实缺陷（2026-09-16 用户报告）：app.js 的 payloadToBody 里写的是
 *     if (payload.advanced) Object.assign(body, payload.params);
 * 于是 advanced=false（高级设置未展开）时，连上传受体的 receptor_file 一起被丢掉，
 * 服务端收不到 → 回退默认凝血酶 → 用户上传的蛋白质压根没被用上。
 * 这里的 stub 覆盖 /api/uploads（jsdom 的 fetch 代理无法转发 FormData），
 * 其余链路（选文件 → 附件状态 → 发送 → 请求体）全部走真实前端代码。 */
async function runChatAttachmentRequestScenario(check, sleep) {
  const dom = await openPage('#chat');
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  const bodies = [];
  const originalFetch = window.fetch;
  const encoder = new TextEncoder();
  const SOURCE_ENT = '/home/biolab/Tools/docking-agent/projects/assets/uploads/20260916-143529-cbf16a-pdb2gs3.ent';
  const SOURCE_SDF = '/home/biolab/Tools/docking-agent/projects/assets/uploads/20260917-112109-3e5739-PGR.sdf';
  /* 长助手回执：验证「超过 N 行折叠」而不把一屏占满 */
  const LONG_REPLY = ['## 对接结果', ''].concat(
    Array.from({ length: 40 }, (_, i) => `- 分子 MOL${i + 1}：亲和力 ${(-5 - i * 0.1).toFixed(2)} kcal/mol`),
    ['', '以上为长工具回执示例。']).join('\n');

  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url);
    if (url && url.indexOf('/api/uploads') >= 0) {
      /* 按 FormData 的 kind 返回受体或配体上传结果（jsdom 的 fetch 代理转发不了 FormData） */
      const form = init && init.body;
      const kind = form && typeof form.get === 'function' ? form.get('kind') : '';
      const file = form && typeof form.get === 'function' ? form.get('file') : null;
      const fileName = (file && file.name) || 'file';
      if (kind === 'ligand') {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            status: 'ok', kind: 'ligand', file_name: fileName, ext: '.sdf',
            path: SOURCE_SDF, size: 573645, pending: true,
            message: '文件已保存到服务端（尚未解析/准备）'
          })
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          status: 'ok', kind: 'receptor', file_name: 'pdb2gs3.ent', ext: '.ent',
          path: SOURCE_ENT, size: 155196, pending: true,
          message: '文件已保存到服务端（尚未解析/准备）'
        })
      });
    }
    if (url && url.indexOf('/api/runs/E2E-ATT-') >= 0) {
      /* 运行结束后前端会 loadRun()；若该运行不存在会走 showError → 覆盖助手气泡，
         这里给出最小可用记录，保证「长助手消息」的折叠断言测的是真实渲染结果。 */
      return Promise.resolve(new Response(JSON.stringify({
        run: { run_id: 'E2E-ATT-1', status: 'ok', molecule_count: 2 },
        result: {}, artifacts: [], log: [], downloads: {}, report_markdown: '', collaboration: {}
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    }
    if (isStdThreadsCreate(url, init)) return stdThreadResponse();
    if (!isStdRunsStream(url)) return originalFetch(input, init);
    let body = {};
    try { body = JSON.parse((init && init.body) || '{}'); } catch (error) { body = {}; }
    body = flattenRunRequest(body);
    bodies.push(body);
    const runId = 'E2E-ATT-' + bodies.length;
    const events = [
      { type: 'start', run_id: runId, conversation_id: body.conversation_id,
        request: body,
        task_spec: { receptor: { name: '', file: body.receptor_file || '', source: 'user' } } },
      { type: 'final', content: LONG_REPLY },
      { type: 'done', run_id: runId, summary: { status: 'ok' } }
    ];
    const stream = new ReadableStream({
      start(controller) {
        let i = 0;
        const push = () => {
          if (i >= events.length) { controller.close(); return; }
          controller.enqueue(encoder.encode(stdSSE({ ...events[i], ts: Date.now() })));
          i += 1;
          setTimeout(push, 20);
        };
        push();
      }
    });
    return Promise.resolve(new Response(stream, {
      status: 200, headers: { 'Content-Type': 'text/event-stream' }
    }));
  };

  const logText = () => (($('#log-box') || {}).textContent || '');

  // 1) 像用户那样选一个 .ent 附件（走 chatAttachFiles 的真实分支）
  const fileInput = $('#chat-file-input');
  const file = new window.File(['HEADER fake ent'], 'pdb2gs3.ent', { type: 'chemical/x-pdb' });
  Object.defineProperty(fileInput, 'files', { value: [file], configurable: true });
  fileInput.dispatchEvent(new window.Event('change', { bubbles: true }));
  for (let i = 0; i < 60; i += 1) {
    if (doc.querySelectorAll('.chat-attachment').length > 0) break;
    await sleep(100);
  }
  check(doc.querySelectorAll('.chat-attachment').length === 1,
    '上传 .ent 后附件 chip 出现', `${doc.querySelectorAll('.chat-attachment').length} 个`);
  const chip = doc.querySelector('.chat-attachment');
  check(!!chip && /开始运行时才准备为 PDBQT/.test(chip.title || ''),
    '附件提示写明「开始运行时才准备」（上传不做处理）',
    chip ? (chip.title || '').slice(0, 110) : '缺失');
  check(/已保存（受体将在开始运行时现场准备为 PDBQT/.test(logText()),
    '附件日志如实写「已保存，运行阶段才准备」', logText().slice(0, 140));

  // 1b) 再上传一个配体库 .sdf（附件 chip 需要能区分 R / L）
  const ligandFile = new window.File(['Dicyclanil\n  test\n\n  1 0 0'], 'lib.sdf',
    { type: 'chemical/x-mdl-sdfile' });
  Object.defineProperty(fileInput, 'files', { value: [ligandFile], configurable: true });
  fileInput.dispatchEvent(new window.Event('change', { bubbles: true }));
  for (let i = 0; i < 60; i += 1) {
    if (doc.querySelectorAll('.chat-attachment').length >= 2) break;
    await sleep(100);
  }
  check(doc.querySelectorAll('.chat-attachment').length === 2,
    '上传 .sdf 后出现第二个附件 chip（上传阶段不解析，仅保存）',
    `${doc.querySelectorAll('.chat-attachment').length} 个`);

  // 2) 不展开高级设置，直接发送（传输文本里含「引用文件」；气泡正文不得含它）
  const chatInput = $('#chat-input');
  chatInput.value = '使用上传的受体与分子库进行对接，输出最好带上小分子的ID @pdb2gs3.ent @lib.sdf';
  chatInput.dispatchEvent(new window.Event('input', { bubbles: true }));
  $('#chat-send').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  for (let i = 0; i < 100; i += 1) {
    if (bodies.length > 0 && !$('#btn-start').disabled) break;
    await sleep(100);
  }
  await sleep(200);

  check(bodies.length === 1, `发送一次产生一个请求体（${bodies.length}）`);
  const body = bodies[0] || {};
  check(body.receptor_file === SOURCE_ENT,
    '折叠高级设置（advanced=false）时请求体仍带 receptor_file = **原始上传文件**'
    + '（准备与 pH 处理在服务端运行阶段完成）',
    String(body.receptor_file));
  check(body.molecule_file === SOURCE_SDF,
    '折叠高级设置时请求体带 molecule_file = 上传的 SDF 绝对路径', String(body.molecule_file));
  check(body.advanced === false,
    '未改动任何运行参数 → 不下发参数（advanced=false，搜索强度走自动规划）',
    String(body.advanced));
  /* 允许下发的字段 = 附件字段 + 运行参数条上的字段（v0.25「所见即所用」）。
     关键是**不能**发 receptor / site_center / site_size —— 那些由指令或系统默认决定。 */
  const ALLOWED_INJECT = ['mode', 'message', 'messages', 'advanced', 'conversation_id', 'receptor_file',
    'molecule_file', 'engine', 'protonation', 'protonation_ph', 'exhaustiveness', 'n_poses',
    'max_ligands', 'save_poses', 'skip_positive_control'];
  const extraKeys = Object.keys(body).filter((key) => ALLOWED_INJECT.indexOf(key) < 0);
  check(extraKeys.length === 0,
    '除附件与运行参数外不注入其它参数（不发送 receptor / site_center / site_size）',
    extraKeys.length ? extraKeys.join(',') : '（无）');
  check(body.receptor === undefined && body.site_center === undefined
    && body.site_size === undefined,
    '对话模式不发送受体与位点盒（由指令或系统默认决定）');
  /* 未改动的项一律不下发：搜索强度走自动规划、不做阳性对照、不指定受体/位点盒 */
  check(body.exhaustiveness === undefined && body.n_poses === undefined
    && body.engine === undefined && body.protonation === undefined,
    '未改动的运行参数不下发（留空 = 自动/系统默认）',
    JSON.stringify({engine: body.engine, exh: body.exhaustiveness, n: body.n_poses,
      ph: body.protonation}));
  check(body.positive_control === undefined && body.skip_positive_control === undefined,
    '未填阳性对照 → 不下发对照相关字段', JSON.stringify({pc: body.positive_control}));

  // ③ 服务端收到的 message 仍含「引用文件」清单与绝对路径（模型要靠它读文件）
  const sentMessage = String(body.message || '');
  check(/引用文件/.test(sentMessage), '服务端 message 仍含「引用文件」清单（Agent 可读绝对路径）',
    sentMessage.split('\n').slice(-3).join(' | ').slice(0, 120));
  check(sentMessage.indexOf(SOURCE_ENT) >= 0 && sentMessage.indexOf(SOURCE_SDF) >= 0,
    '服务端 message 里两个附件的绝对路径都在');

  // ① 用户气泡正文只显示用户自己输入的文字：不含「引用文件」、不含绝对路径
  const userBubble = doc.querySelector('.chat-msg.user .chat-bubble');
  const userText = userBubble ? (userBubble.querySelector('.chat-text') || {}).textContent || '' : '';
  check(/使用上传的受体与分子库进行对接/.test(userText),
    '用户气泡显示用户自己输入的文字', userText.slice(0, 60));
  check(userText.indexOf('引用文件') < 0,
    '① 用户气泡正文不含「引用文件」长清单', userText.slice(0, 80));
  check(userText.indexOf(SOURCE_ENT) < 0 && userText.indexOf(SOURCE_SDF) < 0
    && userText.indexOf('/home/biolab') < 0,
    '① 用户气泡正文不含任何绝对路径（显示与传输分离）', userText.slice(0, 80));

  // ② 附件渲染成紧凑 chip：文件名 + R/L，完整路径在 title
  const fileChips = userBubble ? Array.from(userBubble.querySelectorAll('.chat-files .chat-file')) : [];
  check(fileChips.length === 2, `② 气泡里有 ${fileChips.length} 个附件 chip（应为 2）`);
  const chipNames = fileChips.map((c) => (c.querySelector('.chat-file-name') || {}).textContent || '');
  const chipKinds = fileChips.map((c) => (c.querySelector('.chat-file-kind') || {}).textContent || '');
  check(chipNames.join(',').indexOf('pdb2gs3.ent') >= 0 && chipNames.join(',').indexOf('lib.sdf') >= 0,
    '② 附件 chip 文件名正确', chipNames.join(','));
  check(chipKinds.slice().sort().join(',') === 'L,R', '② 附件 chip 带类型标记 R/L', chipKinds.join(','));
  check(fileChips.every((c) => String(c.title || '').indexOf('/home/biolab') >= 0),
    '② chip 悬停 title 里有完整路径（正文里没有）',
    (fileChips[0] ? String(fileChips[0].title) : '').slice(0, 80));

  // ④ 长助手消息**不再默认折叠**（用户要求）：全文直接显示、没有折叠按钮

  const foldBtn = doc.querySelector('.chat-msg.assistant .chat-fold-toggle');
  const foldBody = doc.querySelector('.chat-msg.assistant .chat-body.chat-fold');
  const assistantBody = doc.querySelector('.chat-msg.assistant .chat-body');
  check(!foldBtn && !foldBody, '④ 长助手正文默认全文显示（无折叠按钮 / 无 .chat-fold）');
  check(!!assistantBody && (assistantBody.textContent || '').length > 800,
    '④ 长正文确实很长但未被截断', assistantBody ? String(assistantBody.textContent.length) : '缺失');
  check(/开始运行 · 受体文件 .*pdb2gs3\.ent/.test(logText()),
    '运行日志显示「受体文件 <用户上传的原始文件>」', logText().slice(-160));
  check(logText().indexOf('受体 thrombin') < 0,
    '运行日志不再谎报「受体 thrombin」');
  dom.window.close();
}

/* 场景：未指定受体时运行日志必须如实写「未指定受体（系统无默认受体，需用户指定）」；
   用户点名受体时才显示「受体 <名字>」。
 * 背景：AgentRequest.receptor 曾经有 schema 默认值 "thrombin"，前端照抄 request.receptor
   会把默认值当成用户指定（真实缺陷）。受理层用 task_spec.receptor.source 区分；
   现在 schema 默认值已删除，且系统**没有**任何默认受体。 */
async function runRunLogReceptorScenario(check, sleep) {
  const dom = await openPage('#chat');
  const { window } = dom;
  const doc = window.document;
  const $ = (sel) => doc.querySelector(sel);
  const originalFetch = window.fetch;
  const encoder = new TextEncoder();
  let round = 0;

  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url);
    if (isStdThreadsCreate(url, init)) return stdThreadResponse();
    if (!isStdRunsStream(url)) return originalFetch(input, init);
    round += 1;
    let body = {};
    try { body = JSON.parse((init && init.body) || '{}'); } catch (error) { body = {}; }
    // 第 1 轮：用户什么都没指定（请求体里不携带任何受体字段）
    // 第 2 轮：用户点名了受体 trypsin（source=user）
    const request = round === 1
      ? { mode: 'chat', advanced: false, engine: 'vina' }
      : { mode: 'chat', advanced: true, receptor: 'trypsin', engine: 'vina' };
    const taskSpec = round === 1
      ? { receptor: { name: '', file: '', source: 'default' } }
      : { receptor: { name: 'trypsin', file: '', source: 'user' } };
    const runId = 'E2E-LOG-' + round;
    const events = [
      { type: 'start', run_id: runId, conversation_id: body.conversation_id,
        request: request, task_spec: taskSpec },
      { type: 'final', content: '第 ' + round + ' 轮回复' },
      { type: 'done', run_id: runId, summary: { status: 'ok' } }
    ];
    const stream = new ReadableStream({
      start(controller) {
        let i = 0;
        const push = () => {
          if (i >= events.length) { controller.close(); return; }
          controller.enqueue(encoder.encode(stdSSE({ ...events[i], ts: Date.now() })));
          i += 1;
          setTimeout(push, 20);
        };
        push();
      }
    });
    return Promise.resolve(new Response(stream, {
      status: 200, headers: { 'Content-Type': 'text/event-stream' }
    }));
  };

  const send = async (text) => {
    const input = $('#chat-input');
    input.value = text;
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    $('#chat-send').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    for (let i = 0; i < 100; i += 1) {
      if (!($('#btn-start') || {}).disabled && round > 0) break;
      await sleep(100);
    }
    await sleep(200);
    return (($('#log-box') || {}).textContent || '');
  };

  const firstLog = await send('帮我筛这两个分子 CCO');
  check(/开始运行 · 未指定受体（系统无默认受体，需用户指定）/.test(firstLog),
    '未指定受体时日志如实写「未指定受体（系统无默认受体，需用户指定）」',
    (firstLog.match(/开始运行[^\n]*/) || [''])[0].slice(0, 90));
  check(firstLog.indexOf('受体 thrombin') < 0,
    '未指定受体时绝不显示成「受体 thrombin」（不再把 schema 默认值当用户指定）');

  const secondLog = await send('用 trypsin 再筛一次');
  check(/开始运行 · 受体 trypsin/.test(secondLog),
    '用户点名受体时如实显示「受体 trypsin」',
    (secondLog.match(/开始运行[^\n]*/) || [''])[0].slice(0, 90));
  dom.window.close();
}

async function main() {
  console.log('='.repeat(74));
  console.log('设置页面 / 导航栏 DOM 级验证（jsdom + 真实后端）');
  console.log('='.repeat(74));

  const before = await api('/api/settings');
  if (before.status !== 200 || !before.data) {
    throw new Error('后端 /api/settings 不可用，请先启动服务');
  }
  const snapshot = {};
  Object.keys(TOUCHED).forEach((path) => { snapshot[path] = before.data.values[path]; });

  // 预置一个已知值，便于验证「来源标注」与「工作台预填」
  await putSettings(SETUP);

  const dom = await openPage('#/settings');
  const doc = dom.window.document;
  const $ = (sel) => doc.querySelector(sel);
  const $$ = (sel) => Array.from(doc.querySelectorAll(sel));

  // 1) 导航栏与视图
  // 顶栏：工作台 / 设置两个**视图按钮** + 一个指向独立页面的「简易模式」链接
  // （`data-view` 是视图切换按钮的标记，链接没有它 —— 按它计数才不会把两套界面混为一谈）
  check($$('#topnav .nav-btn[data-view]').length === 2, '导航栏有两个视图入口（工作台 / 设置）');
  check($$('#topnav a.nav-btn[href="/"]').length === 1,
        '导航栏提供默认首页「简易模式」的入口链接');
  check(doc.body.dataset.view === 'settings', 'URL hash #/settings 直接进入设置页', doc.body.dataset.view);
  check($('#view-settings').classList.contains('is-active'), '设置视图处于激活状态');
  check(!$('#view-workbench').classList.contains('is-active'), '工作台视图此时隐藏');
  check($('#nav-settings').classList.contains('active'), '导航按钮高亮跟随视图');
  check($('#nav-settings').getAttribute('aria-selected') === 'true', '导航按钮有 aria-selected');

  // 2) 设置页内容（由 /api/settings 的 specs 驱动渲染）
  const groups = $$('#settings-groups > details');
  // 7 组：llm / models / roles / docking / external（外部工具，P0 新增）/ runtime / deploy
  check(groups.length === 7, `设置分组渲染完整（${groups.length} 组）`,
    groups.map((g) => g.dataset.group).join(','));
  check(groups.some((g) => g.dataset.group === 'external'),
    '设置页有「外部工具（自行安装）」分组');
  const fields = $$('[data-setting-path]');
  check(fields.length >= 60, `配置项控件渲染完整（${fields.length} 个）`);
  const roleCards = $$('.role-card');
  check(roleCards.length === 6, `6 个 Agent 角色各有一张参数卡片（${roleCards.length}）`);
  check(roleCards.every((c) => c.querySelector('button[data-role-test]')),
    '每张角色卡片都有「测试连通性」按钮');

  // 3) 来源标注 + 继承占位符
  const chips = $$('.src-chip').filter((c) => c.textContent.trim());
  check(chips.length >= 60, `每个字段都有来源标注（${chips.length} 个）`);
  check(chips.some((c) => c.className.indexOf('src-ui') >= 0), '存在「界面设置」来源标注');
  check(chips.some((c) => c.className.indexOf('src-default') >= 0), '存在「内置默认」来源标注');
  const exhaust = $('[data-setting-path="docking.exhaustiveness"]');
  check(exhaust && exhaust.value === '11', '已保存的界面设置回显到表单', exhaust ? exhaust.value : '缺失');
  const roleTemp = $('[data-setting-path="roles.docking.temperature"]');
  check(roleTemp && roleTemp.value === '', '未覆盖的角色字段留空（表示继承）');
  check(roleTemp && /继承/.test(roleTemp.placeholder || ''), '留空字段的占位符显示继承到的值',
    roleTemp ? roleTemp.placeholder : '');

  // 3.5) 只读项（PORT）不参与提交
  const portInput = $('[data-setting-path="deploy.port"]');
  check(portInput && portInput.disabled, '只读项（PORT）渲染为不可编辑');
  check(portInput && portInput.dataset.settingReadonly === 'true', '只读项带提交豁免标记');

  // 3.6) 结合口袋相关控件
  check($$('#orch-flow .orch-node').length === 6, '编排图有 6 个节点（复核节点已移除）',
    String($$('#orch-flow .orch-node').length));
  check(!!$('#orch-node-pocket'), '存在「口袋分析」节点');
  check(!$('#orch-node-verify'), '独立复核节点已移除（流程控制权在主管 Agent）');
  check(!!$('#orch-lanes') && !!$('#orch-parallel'), '编排图有实测时间轴与并行度徽标');
  check(/实测时间轴/.test($('#orch-lanes-hint').textContent),
    '时间轴说明写明「重叠即并行」');
  check(!!$('#orch-box'), '编排面板显示对接盒信息行');
  check(!!$('#protonation-select'), '工作台提供「质子化态策略」选择（基础参数区默认可见）');
  check(['neutralize', 'ph', 'keep'].indexOf($('#protonation-select').value) >= 0,
    '质子化态策略取值只能是 neutralize / ph / keep', $('#protonation-select').value);
  check(!!$('#protonation-ph') && !!$('#ph-presets'),
    '目标 pH 输入与常用预设（胃酸/溶酶体/生理）都在基础参数区');
  /* 默认 = ph（生理 pH），因此「目标 pH」默认就是可编辑的（新手不用先改策略） */
  check($('#protonation-select').value === 'ph', '默认质子化策略为 ph（生理 pH 7.4）',
    $('#protonation-select').value);
  check($('#protonation-ph').disabled === false && $('#protonation-ph').value === '7.4',
    '默认目标 pH = 7.4 且可编辑', String($('#protonation-ph').value));
  check(!!$('#protonation-ph').list, '目标 pH 关联常用值预设（datalist）');
  /* 联动：切到非 ph 策略时目标 pH 置灰（不能静默无效） */
  $('#protonation-select').value = 'neutralize';
  $('#protonation-select').dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  check($('#protonation-ph').disabled === true, '非 ph 策略时「目标 pH」置灰');
  $('#protonation-select').value = 'ph';
  $('#protonation-select').dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  check($('#protonation-ph').disabled === false, '切回 ph 策略后「目标 pH」可编辑');
  check(!!$('#protonation-select').closest('#param-common')
    && !$('#protonation-select').closest('#more-params'),
    '质子化态策略必须在基础参数区（不在「更多参数」折叠里）');
  check(!!$('#protonation-select').closest('label').querySelector('.tip-icon'),
    '质子化态策略带 (?) 说明');
  /* 界面选了必须真的进入提交参数：collectParamForm 是工作台唯一的参数汇集点 */
  if (typeof dom.window.collectParamForm === 'function') {
    const chosen = dom.window.collectParamForm().protonation;
    check(['neutralize', 'ph', 'keep'].indexOf(chosen) >= 0,
      '参数模式提交里带 protonation（界面选择真正生效）', String(chosen));
    const sel = $('#protonation-select');
    sel.value = 'keep';
    sel.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
    check(dom.window.collectParamForm().protonation === 'keep',
      '切换为 keep 后提交参数随之变化', dom.window.collectParamForm().protonation);
    sel.value = 'ph';
    sel.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
    $('#protonation-ph').value = '5.5';
    $('#protonation-ph').dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    const withPh = dom.window.collectParamForm();
    check(withPh.protonation === 'ph' && withPh.protonation_ph === 5.5,
      'ph 策略下目标 pH 随表单一起提交', JSON.stringify([withPh.protonation, withPh.protonation_ph]));
    /* 校验要过「受体来源 + 分子库非空」两关，先给受体与一个分子，才能单独验证 pH 规则 */
    if ($('#receptor-source')) {
      $('#receptor-source').value = '1DWC';
      $('#receptor-source').dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    }
    $('#ligands-text').value = '乙醇:CCO';
    $('#ligands-text').dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    check(dom.window.validateManualForm(dom.window.collectParamForm()) === null,
      'ph=5.5 通过表单校验',
      String(dom.window.validateManualForm(dom.window.collectParamForm())));
    $('#protonation-ph').value = '99';
    check(/目标 pH/.test(String(dom.window.validateManualForm(dom.window.collectParamForm()))),
      'pH 超出 0–14 被拦下',
      String(dom.window.validateManualForm(dom.window.collectParamForm())));
    $('#protonation-ph').value = '';
    check(/目标 pH/.test(String(dom.window.validateManualForm(dom.window.collectParamForm()))),
      'ph 策略下留空目标 pH 被拦下');
    $('#protonation-ph').value = '7.4';
    $('#ligands-text').value = '';
    $('#ligands-text').dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    if ($('#receptor-source')) {
      $('#receptor-source').value = '';
      $('#receptor-source').dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    }
    sel.value = 'neutralize';
    sel.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  }
  check(!!$('#pocket-engine-select'), '工作台提供「结合位点来源（口袋引擎）」选择');
  const pocketEngineOptions = $$('#pocket-engine-select option').map((o) => o.value);
  check(pocketEngineOptions.join(',') === ',p2rank,geometric,known_site',
    '口袋引擎可选 auto/p2rank/geometric/known_site', pocketEngineOptions.join(','));
  check(!!$('#pocket-tbody') && !!$('#pocket-empty'), '结果区有口袋预测表与空状态');

  // 4) 密钥只写不读
  const keyInput = $('[data-setting-path="llm.api_key"]');
  check(keyInput && keyInput.type === 'password', 'API Key 使用密码框');
  check(keyInput && keyInput.value === '', 'API Key 不回显明文到输入框');
  check(/已配置|未配置/.test(keyInput ? keyInput.placeholder : ''),
    'API Key 显示「已配置」状态而非明文', keyInput ? keyInput.placeholder : '');

  // 5) 未保存修改提示
  const modelInput = $('[data-setting-path="llm.model"]');
  modelInput.value = 'ui-e2e-model';
  modelInput.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check(!$('#nav-settings-dot').classList.contains('hidden'), '修改后导航栏出现未保存标记');
  check(/未保存/.test($('#settings-status').textContent), '状态栏提示有未保存修改');

  // 6) 保存 → 真实写入后端（并自动重载 Agent）
  $('#btn-settings-save').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  for (let i = 0; i < 60; i += 1) {
    if (/已保存/.test($('#settings-status').textContent) || /失败/.test($('#settings-status').textContent)) break;
    await sleep(200);
  }
  check(/已保存/.test($('#settings-status').textContent), '保存成功并提示（含自动重载 Agent）',
    $('#settings-status').textContent.slice(0, 60));
  const saved = await api('/api/settings');
  check(saved.data.values['llm.model'] === 'ui-e2e-model', '后端已持久化界面设置',
    String(saved.data.values['llm.model']));
  check(saved.data.sources['llm.model'] === '界面设置', '来源标注更新为「界面设置」',
    saved.data.sources['llm.model']);
  check(saved.data.effective['roles.binding.model'] === 'ui-e2e-model',
    '未单独配置的角色继承界面全局模型', String(saved.data.effective['roles.binding.model']));
  const written = Object.keys(saved.data.values)
    .filter((path) => saved.data.values[path] !== null);
  check(written.indexOf('docking.save_poses') < 0,
    '未改动的布尔字段不会被误写入设置文件（继承语义保持）', written.join(','));

  // 7) 切回工作台 + 预填
  $('#nav-workbench').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  await sleep(200);
  check($('#view-workbench').classList.contains('is-active'), '点击导航可切回工作台');
  check(doc.body.dataset.view === 'workbench', '视图状态同步');
  check(dom.window.location.hash === '#chat', '切回工作台恢复子页 hash', dom.window.location.hash);
  check($('#exhaustiveness').value === '11', '工作台表单用设置页默认值预填（exhaustiveness=11）',
    $('#exhaustiveness').value);
  /* 预填只挪滑块位置，不代表用户做了选择：默认仍勾着「自动」，标签必须显示「自动」 */
  check($('#exhaustiveness-auto').checked === true && $('#exhaustiveness-value').textContent === '自动',
    '预填默认值但用户未改动 → 标签显示「自动」（数值不下发）',
    `${$('#exhaustiveness').value} / ${$('#exhaustiveness-value').textContent}`);

  // 8) 拉取模型列表（真实请求端点）
  $('#nav-settings').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  await sleep(200);
  $('#btn-fetch-models').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  for (let i = 0; i < 60; i += 1) {
    if (doc.querySelectorAll('[data-model-chip]').length > 0) break;
    await sleep(200);
  }
  const modelChips = $$('[data-model-chip]');
  check(modelChips.length > 0, `拉取到可用模型标签（${modelChips.length} 个）`,
    modelChips.map((c) => c.dataset.modelChip).join(','));
  check($$('#settings-model-list option').length > 0, '模型输入框获得自动补全候选');

  // 9) 点击模型标签填入默认模型
  if (modelChips.length) {
    modelChips[0].dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    await sleep(100);
    check($('[data-setting-path="llm.model"]').value === modelChips[0].dataset.modelChip,
      '点击模型标签填入默认模型', $('[data-setting-path="llm.model"]').value);
  }
  dom.window.close();

  // 10) 恢复现场（把本次改动过的路径按快照写回）
  const restore = {};
  Object.keys(TOUCHED).forEach((path) => {
    const original = snapshot[path];
    restore[path] = (original === null || original === undefined) ? '' : original;
  });
  const restored = await putSettings(restore);
  check(restored.status === 200, '测试结束后已恢复原有设置（不改动用户配置）');
  const after = await api('/api/settings');
  const okRestore = Object.keys(TOUCHED).every((path) => {
    const want = snapshot[path];
    const got = after.data.values[path];
    return (want === null || want === undefined) ? (got === null || got === undefined) : String(got) === String(want);
  });
  check(okRestore, '恢复结果与测试前一致');

  // 10.5) 设置页返回入口（用户反馈：设置页没有返回按钮）
  console.log('\n--- 设置页返回工作台（按钮 / Esc） ---');
  await runSettingsBackScenario(check, sleep);

  // 11) 运行图：按实测起止时间展示并行/串行（合成事件流驱动真实前端逻辑）
  console.log('\n--- 运行图并行展示 ---');
  const parallelDom = await openPage('#manual');
  await runParallelScenario(parallelDom, check, sleep);
  /* 刻意不在这里 close()：jsdom 关窗后，应用内部仍在 await 的异步续体访问 document 会抛
     未处理拒绝并让 Node 直接退出。两个窗口统一在全部断言完成后关闭。 */

  // 11a) 实时逐分子结果：合成 molecules 事件必须驱动表格真的出现行、空状态隐藏
  console.log('\n--- 实时逐分子结果 ---');
  const liveDom = await openPage('#manual');
  await runLiveMoleculesScenario(liveDom, check, sleep);

  // 11b) 对话附件与 @ 引用：注入附件状态后，断言请求构造把文件带进正确字段
  console.log('\n--- 对话附件与 @ 引用 ---');
  const attDom = await openPage('#chat');
  const adoc = attDom.window.document;
  const awin = attDom.window;
  if (typeof awin.chatAttachmentPayload === 'function') {
    // 用显式附件列表调用（纯函数，不依赖界面 state）
    const files = [
      /* v0.22：上传不再产出 PDBQT，附件里就是**原始文件**（准备发生在运行阶段） */
      { id: 'a1', name: 'receptor.pdb', kind: 'receptor', path: '/tmp/up/receptor.pdb',
        receptorFile: '', count: 0 },
      { id: 'a2', name: 'lib.csv', kind: 'ligand', path: '/tmp/up/lib.csv', count: 12 },
      { id: 'a3', name: 'extra.sdf', kind: 'ligand', path: '/tmp/up/extra.sdf', count: 5 },
    ];
    const att = awin.chatAttachmentPayload(files);
    check(att.receptor_file === '/tmp/up/receptor.pdb',
      '附件：受体交**原始文件**（PDBQT 与 pH 质子化在运行阶段才处理）',
      String(att.receptor_file));
    check(att.molecule_file === '/tmp/up/lib.csv', '附件：首个配体库进 molecule_file',
      String(att.molecule_file));
    check(att.refs.length === 3, `附件：引用清单含全部 ${att.refs.length} 个文件`);
    const params = awin.chatAttachmentParams(files).params;
    check(params.receptor_file === '/tmp/up/receptor.pdb'
      && params.molecule_file === '/tmp/up/lib.csv',
      '附件作为明确意图下发（receptor_file=原始文件 / molecule_file）');
    check(Object.keys(params).length === 2,
      '除附件外一个参数字段都不带（保持"对话不注入参数"语义）', Object.keys(params).join(','));
    const mentioned = awin.mentionedAttachments('用 @receptor.pdb 对接 @lib.csv', files);
    check(mentioned.length === 2, '指令中的 @名称能匹配到附件', String(mentioned.length));
    const message = awin.composeChatMessage('用 @receptor.pdb 对接 @lib.csv', files);
    check(/引用文件/.test(message) && /receptor\.pdb/.test(message) && /lib\.csv/.test(message),
      '指令里附带「引用文件」清单（含服务端绝对路径）');
    check(message.indexOf('extra.sdf') < 0,
      '只 @ 了部分附件时，引用清单只列被引用的那些');
    const all = awin.composeChatMessage('随便做一次筛选', files);
    check(/extra\.sdf/.test(all), '未使用 @ 时，引用清单覆盖全部附件');
  } else {
    check(false, '对话附件相关函数可被调用（chatAttachmentPayload）');
  }
  /* 这个窗口点过「发送」→ startRun 的收尾（loadRun → refreshHistory）可能仍在 await 中，
     提前 close() 会让续体访问 document 抛错把整个脚本带崩；交给 process.exit 收尾。 */

  // 11c) 多轮会话：conversation_id 贯穿 + 「新对话」+ 刷新后恢复
  console.log('\n--- 多轮会话（conversation_id） ---');
  await runConversationScenario(check, sleep);

  // 11d) 结构化选项（choices）：候选渲染成可点按钮，点选后同一会话继续
  console.log('\n--- 结构化选项（choices 点选继续） ---');
  await runManualChoicesScenario(check, sleep);
  console.log('\n--- 对话：工具轨迹不再进气泡 ---');
  await runChatTraceSeparationScenario(check, sleep);

  console.log('\n--- 对话：不是可执行任务的运行（无报告 / no_op） ---');
  await runNoOpRunScenario(check, sleep);

  console.log('\n--- 布局：首屏无结果状态 + 运行详情默认收起 ---');
  await runLayoutStateScenario(check, sleep);

  await runChatChoicesScenario(check, sleep);

  // 11e) 折叠高级设置 + 有附件：请求体必须带 receptor_file（回归：曾被整段丢掉）
  console.log('\n--- 对话附件 → 请求体 receptor_file ---');
  await runChatAttachmentRequestScenario(check, sleep);
  await runQuickParamInjectionScenario(check, sleep);

  // 11f) 运行日志的受体来源必须如实（未指定 ≠ 受体 thrombin）
  console.log('\n--- 运行日志：受体来源如实展示 ---');
  await runRunLogReceptorScenario(check, sleep);

  // 12) 导出条 / KPI / 报告目录：像用户那样点进「历史运行 → 第一行」，再断言导出能力
  console.log('\n--- 导出条 / KPI / 报告目录 ---');
  const runDom = await openPage('#chat');
  const rdoc = runDom.window.document;
  const rwin = runDom.window;
  const clickTab = (tab) => {
    const btn = rdoc.querySelector('[data-tab="' + tab + '"]');
    if (btn) btn.click();
    return !!btn;
  };
  const waitFor = async (selector, tries) => {
    for (let i = 0; i < (tries || 40); i += 1) {
      const node = rdoc.querySelector(selector);
      if (node) return node;
      await sleep(150);
    }
    return null;
  };

  clickTab('history');
  await waitFor('#history-tbody tr', 60);
  /* 历史列表默认只显示最近 20 条，而工作区里可能**全是 0 分子的 no_op 运行**
     （门禁/审计脚本自己也会产生运行记录）——那样"挑一条有分子数的已完成记录"会挑空，
     导出条断言就会随机变红（此前正是这么脆）。这里先用 API 找一条**有分子数且已完成**
     的运行，再用搜索框把它筛出来点开，使该场景不依赖环境里恰好有一条真跑过的运行。 */
  let targetRunId = '';
  try {
    const listing = await api('/api/runs?limit=200');
    const candidates = (listing.data && listing.data.runs) || [];
    const hit = candidates.find((r) => (r.molecule_count || 0) > 0 && String(r.status) === 'ok');
    if (hit) targetRunId = String(hit.run_id);
  } catch (error) { /* 取不到候选就退回下面的启发式 */ }
  if (targetRunId) {
    const search = rdoc.querySelector('#history-q');
    if (search) {
      search.value = targetRunId;
      search.dispatchEvent(new rwin.Event('input', { bubbles: true }));
      for (let i = 0; i < 60; i += 1) {
        if (rdoc.querySelector('#history-tbody tr[data-run-id="' + targetRunId + '"]')) break;
        await sleep(150);
      }
      check(!!rdoc.querySelector('#history-tbody tr[data-run-id="' + targetRunId + '"]'),
        '历史检索能把「有分子数的已完成运行」筛出来（导出条断言不依赖环境）', targetRunId);
    }
  }
  const historyRows = Array.from(rdoc.querySelectorAll('#history-tbody tr'));
  /* no_op 运行（受理层未受理，例如用户只说了句「你好」）状态同样是"完成"、分子数为 0、
     且没有 report/ranking 产物 → 导出条本来就该是禁用的，不能拿它验证导出契约。
     这里挑一条**有分子数**的已完成记录（第 4 列 = molecule_count）。 */
  const moleculeCount = (tr) => {
    const cells = tr.querySelectorAll('td');
    const raw = cells[3] ? String(cells[3].textContent).replace(/[^0-9]/g, '') : '';
    return raw ? Number(raw) : 0;
  };
  const firstRow = historyRows.find((tr) => tr.querySelector('.tag-ok') && moleculeCount(tr) > 0)
    || historyRows.find((tr) => tr.querySelector('.tag-ok')) || historyRows[0] || null;
  check(historyRows.length > 0, `历史运行列表可取到记录（${historyRows.length} 行）`);
  if (firstRow) {
    firstRow.click();
    const kpi = await waitFor('#run-kpis .kpi', 60);
    check(!!kpi, '结果总览渲染 KPI 卡（聚合统计）');
    const pdf = rdoc.querySelector('#btn-download-pdf');
    check(!!pdf && !pdf.classList.contains('disabled') && /\/report\.pdf$/.test(pdf.href || ''),
      '导出条：报告 PDF 按钮指向 /report.pdf', pdf ? String(pdf.getAttribute('href')) : '缺失');
    const zip = rdoc.querySelector('#btn-download-zip');
    check(!!zip && /\/download\.zip$/.test(zip.href || ''),
      '导出条：打包下载指向 /download.zip');
    const csv = rdoc.querySelector('#btn-download-csv');
    check(!!csv && /\/export\.csv$/.test(csv.href || ''),
      '导出条：排序 CSV 指向 /export.csv');
    const kpis = rdoc.querySelectorAll('#run-kpis .kpi');
    check(kpis.length >= 5, `KPI 卡数量足够（${kpis.length} 个）`);
    const hint = rdoc.querySelector('#download-name-hint');
    check(!!hint && (hint.textContent || '').length > 0, '导出条显示本次运行的命名前缀');
    /* 运行笔记：汇总表只报条数，正文在 #run-notes 里默认折叠成 2 行（用户反馈：超长备注拉长页面） */
    const noteBox = rdoc.querySelector('#run-notes');
    const noteRows = noteBox ? noteBox.querySelectorAll('.run-note') : [];
    check(!!noteBox && !noteBox.classList.contains('hidden') && noteRows.length > 0,
      `运行笔记渲染成可折叠条目（${noteRows.length} 条）`,
      noteBox ? noteBox.className : '缺失');
    const summaryText = (rdoc.querySelector('#run-summary') || {}).textContent || '';
    check(summaryText.indexOf('运行笔记') >= 0 && summaryText.indexOf('条（见下方）') >= 0,
      '汇总表只显示笔记条数（不再塞整段备注）',
      summaryText.replace(/\s+/g, ' ').slice(0, 120));
    const firstToggle = noteRows.length ? noteRows[0].querySelector('.run-note-toggle') : null;
    const firstText = noteRows.length ? noteRows[0].querySelector('.run-note-text') : null;
    if (firstToggle && firstText) {
      firstToggle.dispatchEvent(new rwin.MouseEvent('click', { bubbles: true }));
      await sleep(30);
      check(firstText.classList.contains('is-open') && firstToggle.textContent === '收起',
        '点「展开」后单条笔记展开（按钮变「收起」）', firstToggle.textContent);
      firstToggle.dispatchEvent(new rwin.MouseEvent('click', { bubbles: true }));
      await sleep(30);
      check(!firstText.classList.contains('is-open') && firstToggle.textContent === '展开',
        '再点收回到 2 行折叠态', firstToggle.textContent);
    }

    const runId = String(rwin.state && rwin.state.runId ? rwin.state.runId : '');
    if (runId) {
      const detail = await api('/api/runs/' + runId);
      const wantName = (detail.data && detail.data.downloads) ? detail.data.downloads.report_pdf : '';
      if (wantName) {
        check(pdf && pdf.getAttribute('download') === wantName,
          '导出文件名取后端下发的规范名（前端不自己拼）', pdf ? pdf.getAttribute('download') : '缺失');
      }
    }
  } else {
    check(false, '历史运行列表可取到至少一行（先跑一次运行再执行本脚本）');
  }
  if (typeof rwin.renderReport === 'function') {
    rwin.renderReport('# 报告\n\n## 一、系统与参数\n\n正文\n\n## 二、结果排序\n\n正文\n');
    const tocLinks = rdoc.querySelectorAll('#report-toc .report-toc-link');
    check(tocLinks.length === 2, '报告目录按章节生成可跳转链接', String(tocLinks.length));
    check(!!rdoc.querySelector('#report-box h2[data-report-section]'),
      '报告正文标题带锚点（目录可定位）');
  } else {
    check(false, 'renderReport 可被调用（报告渲染入口存在）');
  }
  // 13) 高级设置精简：预设 / 恢复系统默认 / 折叠摘要 / 工具提示（v0.14）
  console.log('\n--- 高级设置精简（预设 / 恢复默认 / 摘要 / 气泡） ---');
  const presetDom = await openPage('#chat');
  const sdoc = presetDom.window.document;
  const swin = presetDom.window;
  const sq = (sel) => sdoc.querySelector(sel);
  const summary = sq('#chat-advanced-summary');
  check(!!summary && /自动定盒|手填位点盒/.test(summary.textContent || ''),
    '折叠摘要行未展开即显示关键值（位点盒来源 / 口袋引擎）',
    summary ? summary.textContent : '缺失');
  check(!!summary && /口袋/.test(summary.textContent || ''),
    '摘要显示口袋引擎', summary ? summary.textContent : '缺失');

  // v0.24：运行参数常驻在对话框上方（不在折叠里），这里校验它确实可见且可用
  const quick = sq('#chat-quick-params');
  check(!!quick && !!quick.querySelector('#exhaustiveness'),
    '运行参数条常驻在对话框上（含搜索强度）', quick ? quick.className : '缺失');
  check(!!quick && !quick.closest('details'),
    '运行参数条不在任何折叠容器里（用户要求：不用折叠到设置中）');
  ['engine-select', 'protonation-select', 'protonation-ph', 'n-poses', 'exhaustiveness',
    'positive-control'].forEach((id) => {
    check(!!quick.querySelector('#' + id), '运行参数条含 ' + id);
  });
  check(['11', '16'].indexOf(sq('#exhaustiveness').value) >= 0
    && sq('#exhaustiveness-auto').checked === true
    && sq('#exhaustiveness-value').textContent === '自动',
    '运行参数条的搜索强度默认「自动」（滑块预填合法值但不显式下发）',
    `${sq('#exhaustiveness').value} / ${sq('#exhaustiveness-value').textContent}`);

  // 展开「其他设置」：摘要随之更新
  const adv = sq('#chat-advanced');
  adv.open = true;
  adv.dispatchEvent(new swin.Event('toggle', { bubbles: false }));
  await sleep(60);
  check(/口袋/.test(summary.textContent || ''), '展开后摘要仍显示其他设置状态', summary.textContent);

  // 一键预设：点击即填入并给出轻提示
  const clickPreset = (id) => sq('#' + id).dispatchEvent(new swin.MouseEvent('click', { bubbles: true }));
  clickPreset('preset-accuracy');
  await sleep(40);
  check(sq('#exhaustiveness').value === '32' && sq('#n-poses').value === '3',
    '「高精度」预设写入 exh=32 / n_poses=3',
    `${sq('#exhaustiveness').value} / ${sq('#n-poses').value}`);
  check(sq('#exhaustiveness-value').textContent === '32', '范围控件数值标签同步预设值',
    sq('#exhaustiveness-value').textContent);
  check(/已应用预设：高精度/.test(sq('#preset-note').textContent || ''),
    '预设给出「已应用预设：高精度」轻提示', sq('#preset-note').textContent);
  check(sq('#preset-accuracy').classList.contains('is-active'), '当前预设按钮高亮');
  clickPreset('preset-fast');
  await sleep(40);
  check(sq('#exhaustiveness').value === '4' && sq('#n-poses').value === '1',
    '「快速初筛」预设写入 exh=4 / n_poses=1',
    `${sq('#exhaustiveness').value} / ${sq('#n-poses').value}`);
  check(/已应用预设：快速初筛/.test(sq('#preset-note').textContent || ''),
    '预设提示随选择切换', sq('#preset-note').textContent);

  // 更多参数：默认收起，摘要行给出关键值
  const more = sq('#more-params');
  check(!!more && !more.open, '「更多参数」二级折叠默认收起');
  check((sq('#more-params-summary').textContent || '').trim().length > 0,
    '「更多参数」摘要行显示关键值', sq('#more-params-summary').textContent);

  // 恢复系统默认：只清界面值
  sq('#positive-control').value = 'NC(=N)c1ccccc1';
  sq('#positive-control').dispatchEvent(new swin.Event('input', { bubbles: true }));
  sq('#center-x').value = '12.5';
  sq('#center-x').dispatchEvent(new swin.Event('input', { bubbles: true }));
  sq('#max-ligands').value = '250';
  sq('#max-ligands').dispatchEvent(new swin.Event('input', { bubbles: true }));
  await sleep(40);
  sq('#btn-restore-defaults').dispatchEvent(new swin.MouseEvent('click', { bubbles: true }));
  await sleep(40);
  check(sq('#positive-control').value === '', '恢复默认：阳性对照清空', sq('#positive-control').value);
  check(sq('#center-x').value === '', '恢复默认：结合位点盒清空', sq('#center-x').value);
  check(sq('#max-ligands').value === '0', '恢复默认：最大分子数回到默认 0', sq('#max-ligands').value);
  check(['auto', 'vina', 'autodock'].indexOf(sq('#engine-select').value) >= 0,
    '恢复默认：对接引擎回到系统默认', sq('#engine-select').value);
  check(['neutralize', 'ph', 'keep'].indexOf(sq('#protonation-select').value) >= 0,
    '恢复默认：质子化态策略回到系统默认', sq('#protonation-select').value);
  check(/已恢复系统默认值/.test(sq('#preset-note').textContent || ''),
    '恢复默认给出轻提示（并声明不动设置页配置）', sq('#preset-note').textContent);
  check(!sq('#preset-accuracy').classList.contains('is-active')
    && !sq('#preset-fast').classList.contains('is-active'),
    '恢复默认后预设高亮被清除');

  // 工具提示：显示 / 内容 / 层级 / 关闭
  const tipIcon = sq('#engine-select').closest('label').querySelector('.tip-icon');
  if (typeof swin.showTip === 'function' && tipIcon) {
    swin.showTip(tipIcon);
    await sleep(30);
    const box = sdoc.querySelector('.tip-box');
    check(!!box && !box.hidden && (box.textContent || '').length > 4,
      '悬停/聚焦显示气泡提示（.tip-box 可见且有内容）', box ? box.textContent.slice(0, 30) : '缺失');
    check(!!box && box.getAttribute('role') === 'tooltip', '气泡带 role=tooltip');
    check(!!tipIcon.getAttribute('data-tip') || !!tipIcon.getAttribute('data-tip-html'),
      '(?) 图标带 data-tip / data-tip-html');
    swin.hideTip();
    await sleep(20);
    check(!!box && box.hidden, '移开后气泡隐藏');
  } else {
    check(false, '工具提示入口可被调用（showTip / tip-icon）');
  }
  if (typeof swin.tipNodesFromHtml === 'function') {
    const nodes = swin.tipNodesFromHtml('<b>粗</b><img src=x onerror=alert(1)><code>c</code>');
    const host = sdoc.createElement('div');
    nodes.forEach((node) => host.appendChild(node));
    check(nodes.length > 0 && host.querySelectorAll('img').length === 0 && /粗/.test(host.textContent),
      'data-tip-html 走白名单（剥离 img 等非白名单标签、保留文字）',
      host.textContent);
  } else {
    check(false, 'data-tip-html 白名单函数可被调用（tipNodesFromHtml）');
  }

  /* 报告 Markdown 的 URL 方案白名单：四类绕过是审计实测出来的（大小写变体、
     data:、vbscript:、图片分支零校验），必须逐条钉住 —— 旧实现只挡小写 javascript:。 */
  if (typeof swin.renderInline === 'function') {
    const md = (text) => swin.renderInline(text);
    check(!/javascript:/i.test(md('[a](javascript:alert(1))')),
      'XSS：小写 javascript: 被挡', md('[a](javascript:alert(1))'));
    check(!/javascript:/i.test(md('[a](JaVaScRiPt:alert(1))')),
      'XSS：混合大小写 JaVaScRiPt: 被挡（旧实现漏掉）', md('[a](JaVaScRiPt:alert(1))'));
    check(!/href="data:/i.test(md('[a](data:text/html,x)')),
      'XSS：data: 链接被挡（旧实现完全未过滤）', md('[a](data:text/html,x)'));
    check(!/vbscript:/i.test(md('[a](vbscript:msgbox(1))')),
      'XSS：vbscript: 被挡', md('[a](vbscript:msgbox(1))'));
    check(!/javascript:/i.test(md('![a](javascript:alert(1))')),
      'XSS：图片分支同样校验 scheme（旧实现零校验）', md('![a](javascript:alert(1))'));
    check(md('[a](//evil.example/x)').indexOf('//evil.example') < 0,
      'XSS：协议相对 //host 被挡', md('[a](//evil.example/x)'));
    check(!/onmouseover="/.test(md('[a](x" onmouseover="alert(1))')),
      'XSS：属性逃逸被转义', md('[a](x" onmouseover="alert(1))'));
    check(md('[a](/api/runs/x/report.pdf)').indexOf('href="/api/runs/x/report.pdf"') >= 0,
      '正常站内链接仍可用', md('[a](/api/runs/x/report.pdf)'));
    check(md('![图](http://127.0.0.1:5001/files/a.png)')
      .indexOf('src="http://127.0.0.1:5001/files/a.png"') >= 0,
      '正常图片链接仍可用（报告里的绝对 URL）', md('![图](http://127.0.0.1:5001/files/a.png)'));
  } else {
    check(false, 'renderInline 可被调用（XSS 方案白名单）');
  }
  if (typeof swin.imageHtml === 'function' && typeof swin.installImageFallback === 'function') {
    check(swin.imageHtml('/api/x.png', 'a').indexOf('onerror') < 0,
      '图片降级不再用内联 onerror（CSP script-src self 兼容）', swin.imageHtml('/api/x.png', 'a').slice(0, 60));
    check(typeof swin.safeUrl === 'function' && swin.safeUrl('javascript:alert(1)') === '#',
      'safeUrl 是显式白名单（非法方案落到 #）', String(swin.safeUrl));
  } else {
    check(false, '图片降级已改为委托实现（installImageFallback）');
  }
  presetDom.window.close();

  /* 这三个页面挂着**在途的 SSE/请求**（parallelDom/liveDom/runDom）。
     高负载下 run 的收尾（如 startRun → refreshHistory）可能还没回来，此时 close() 会让
     续体访问 document 抛错，把「全绿」变成 crash（真实踩坑：与大库对接并行跑时）。
     它们不需要显式关闭：进程结束即释放，因此这里只冻结后续请求并交给 process.exit。 */
  [parallelDom, liveDom, runDom].forEach((dom) => {
    try { dom.window.fetch = () => new Promise(() => {}); } catch (e) { /* 忽略 */ }
  });
  await sleep(200);

  const passed = results.filter((r) => r[0]).length;
  console.log('\n' + '='.repeat(74));
  console.log(`结果：${passed}/${results.length} 通过`);
  results.filter((r) => !r[0]).forEach((r) => console.log(`  - 未通过：${r[1]}  ${r[2]}`));
  console.log('='.repeat(74));
  return results.every((r) => r[0]) ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.error('E2E 运行失败：', error);
  process.exit(2);
});
