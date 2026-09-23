/* ============================================================================
 * 简易模式（/simple）客户端 —— 只用「对话 + 结果」两件事，参数一律走默认/自动规划。
 *
 * 设计原则（与高级模式同一套后端契约，不新增私有接口）：
 *   · 运行走**标准 Agent Protocol**：POST /threads/{tid}/runs/stream
 *     （assistant_id=coordinator，stream_mode=[messages,updates,custom]），
 *     与高级模式共用同一条服务端链路与同一份产物；
 *   · 参数：请求体只带「附件派生字段 + 指令」，`mode=chat`、`advanced=false`
 *     —— 质子化态、搜索强度、位点盒等全部由受理层默认值与自动规划决定；
 *   · 结构化选项（受体歧义 / 共晶配体是否用作阳性对照）直接渲染成按钮，
 *     点选即续跑；阳性对照类选择会作为**请求字段**下发（否则后端读不到，跑完还会再问一次）；
 *   · 结果只读 `GET /api/runs/{id}`（与服务端唯一权威一致），榜单只取前几名。
 *
 * 本文件不依赖 app.js：高级模式有 7000+ 行、且绑定它自己的 DOM 与状态机，
 * 简易模式复用它只会把两边的改动互相牵制。
 * ========================================================================== */
'use strict';

/* ------------------------------------------------------------------ 小工具 */
const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function show(node, visible) {
  if (node) node.classList.toggle('hidden', !visible);
}

function fmtNum(value, digits) {
  if (typeof value !== 'number' || !isFinite(value)) return '—';
  return value.toFixed(digits === undefined ? 2 : digits);
}

function fmtInt(value) {
  const n = Number(value);
  return isFinite(n) ? String(Math.round(n)) : '—';
}

/* 会话与最近一次运行：刷新页面后仍能继续同一段对话 / 看回上次结果 */
const LS_THREAD = 'dsa.simple.thread';
const LS_LAST_RUN = 'dsa.simple.lastRun';

const state = {
  threadId: '',
  runId: '',
  standardRunId: '',
  controller: null,
  busy: false,
  files: { receptor_file: '', molecule_file: '' },
  fileNames: [],
  pendingPositiveControl: '',
  pendingDecision: '',
  pendingMoleculeChoice: null,
  thinking: '',
  thinkingLive: false,
  // 候选选择：**先缓冲、等本轮输出结束再渲染**（用户反馈：模型还在输出时按钮就出现，
  // 提前点选会与进行中的运行抢跑，后续就取不到这次选择）
  deferredChoices: null,
  lastText: '',
};

/* ------------------------------------------------------------------ 健康状态 */
async function refreshHealth() {
  const dot = $('s-health-dot');
  const text = $('s-health-text');
  try {
    const res = await fetch('/api/health', { headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    dot.className = 'dot dot-ok';
    const engines = data.engine_available || {};
    const ready = Object.keys(engines)
      .filter((key) => key !== 'external_flavor' && engines[key])
      .map((key) => (key === 'external' && engines.external_flavor ? engines.external_flavor : key));
    const engine = ready.length ? ('引擎 ' + ready.join(' / ')) : '未检测到引擎';
    text.textContent = '服务正常 · ' + engine + ' · v' + (data.version || '—');
  } catch (error) {
    dot.className = 'dot dot-bad';
    text.textContent = '服务不可用 · ' + (error && error.message ? error.message : error);
  }
}

/* ------------------------------------------------------------------ 对话渲染 */
function pushMessage(role, text) {
  const node = el('div', 's-msg s-msg-' + role);
  node.appendChild(el('p', '', text || ''));
  $('s-log').appendChild(node);
  $('s-log').scrollTop = $('s-log').scrollHeight;
  return node;
}

/** Markdown → DOM。唯一实现在 `/static/markdown.js`（与高级模式共用）；缺失时退化为纯文本，
 *  绝不出现白屏或 `[object Object]`。 */
function mdBlock(text) {
  const source = String(text || '');
  const md = window.DockingMarkdown;
  if (md && typeof md.renderMarkdown === 'function') return md.renderMarkdown(source);
  const fallback = el('div', 'md-root');
  fallback.textContent = source;
  return fallback;
}

function setAssistant(node, text, { typing } = {}) {
  if (!node) return;
  const thinkBlock = node.querySelector('.s-think');
  const value = String(text || '');
  node.replaceChildren();
  const body = el('div', 's-body markdown');
  if (value) body.appendChild(mdBlock(value));
  node.classList.toggle('s-typing', Boolean(typing) && !value);
  node.appendChild(body);
  if (thinkBlock) node.insertBefore(thinkBlock, node.firstChild);
  // 助手气泡**不再默认折叠**（用户要求）：长回执直接全文显示，不做 24 行/1800 字截断
  scrollLog();
}

/**
 * 思考/推理：收进气泡里的「思考」块，永不进正文。
 * 交互照市面通行做法：**推理流式期间展开可见**（`live`），**正文一开始就自动收起**成一行
 * 「思考 · N 字 · 用时 X 秒」，任何时候点一下都能展开看推理全文。
 */
function renderThinking(bubble, text, options) {
  if (!bubble) return;
  const live = Boolean(options && options.live);
  const source = String(text || '');
  let block = bubble.querySelector('.s-think');
  if (!source) {
    if (block) block.remove();
    return;
  }
  if (!block) {
    block = el('div', 's-think');
    block.dataset.started = String(Date.now());
    const toggle = el('button', 's-think-toggle');
    toggle.type = 'button';
    const body = el('div', 's-think-body');
    if (!live) body.classList.add('hidden');
    toggle.addEventListener('click', () => {
      body.classList.toggle('hidden');
      paintThinking(block, toggle);
    });
    block.appendChild(toggle);
    block.appendChild(body);
    bubble.insertBefore(block, bubble.firstChild);
  }
  const body = block.querySelector('.s-think-body');
  const toggle = block.querySelector('.s-think-toggle');
  const wasLive = block.dataset.live === '1';
  block.dataset.live = live ? '1' : '0';
  if (live) body.classList.remove('hidden');
  else if (wasLive) body.classList.add('hidden');  // 正文开始 → 自动收起
  body.textContent = source;
  paintThinking(block, toggle);
  scrollLog();
}

/** 只更新思考块头部（展开态 / 文案），不动正文内容 */
function paintThinking(block, toggle) {
  const body = block.querySelector('.s-think-body');
  const expanded = !body.classList.contains('hidden');
  const live = block.dataset.live === '1';
  const chars = (body.textContent || '').replace(/\s/g, '').length;
  const count = chars ? (chars > 999 ? Math.round(chars / 1000) + 'k' : chars) + ' 字' : '';
  const started = Number(block.dataset.started || 0);
  const seconds = live || !started ? 0 : Math.max(1, Math.round((Date.now() - started) / 1000));
  const meta = [count, live ? '' : (seconds ? '用时 ' + seconds + ' 秒' : '')].filter(Boolean).join(' · ');
  toggle.textContent = (expanded ? '▾ ' : '▸ ') + (live ? '思考中' : '思考') + (meta ? ' · ' + meta : '');
  toggle.setAttribute('aria-expanded', String(expanded));
}

/** 正文开始到达：把还处于「思考中」的块收起（只做一次状态迁移） */
function finishThinking(bubble) {
  if (!state.thinking || !state.thinkingLive) return;
  state.thinkingLive = false;
  renderThinking(bubble, state.thinking, { live: false });
}

function scrollLog() {
  const log = $('s-log');
  if (log) log.scrollTop = log.scrollHeight;
}

function setStatus(text, kind) {
  const hint = $('s-chat-hint');
  if (hint) hint.textContent = text || '';
  if (kind === 'error') pushMessage('bot', text || '运行失败');
}

function setBusy(busy) {
  state.busy = busy;
  $('s-send').disabled = busy;
  $('s-send').textContent = busy ? '运行中…' : '开始筛选';
  show($('s-stop'), busy);
  /* 运行中的点选必须点不动：历史缺陷是「先清空按钮、再被 busy 判定丢掉」，
     用户看到的是「点了没反应，后面的选项也不出来了」（真实反馈）。 */
  document.querySelectorAll('.s-choice-btn').forEach((button) => {
    button.disabled = Boolean(busy);
  });
}

/* ------------------------------------------------------------------ 附件 */
async function uploadFile(file) {
  const body = new FormData();
  body.append('file', file);
  body.append('kind', 'auto');
  const res = await fetch('/api/uploads', { method: 'POST', body });
  if (!res.ok) {
    let detail = 'HTTP ' + res.status;
    try { detail = (await res.json()).detail || detail; } catch (error) { /* 允许静默：错误体不是 JSON 时用状态码 */ }
    throw new Error(detail);
  }
  return res.json();
}

async function onPickFiles(event) {
  const files = Array.from(event.target.files || []);
  if (!files.length) return;
  for (const file of files) {
    try {
      const info = await uploadFile(file);
      if (info.kind === 'receptor') {
        state.files.receptor_file = info.path;
      } else {
        state.files.molecule_file = info.path;
      }
      state.fileNames.push((info.kind === 'receptor' ? '受体 ' : '分子库 ') + info.file_name);
    } catch (error) {
      setStatus('上传失败：' + (error && error.message ? error.message : error), 'error');
    }
  }
  $('s-file').value = '';
  $('s-files').textContent = state.fileNames.join('、') || '未选择附件';
}

/* ------------------------------------------------------------------ 结构化选项 */
function renderChoices(choices, note) {
  const box = $('s-choices');
  box.textContent = '';
  if (!choices || !choices.length) {
    show(box, false);
    return;
  }
  box.appendChild(el('p', 's-choices-note', note || '需要确认：'));
  choices.forEach((choice) => {
    const btn = el('button', 's-choice-btn', choice.label || choice.prompt || '选择');
    btn.type = 'button';
    btn.disabled = Boolean(state.busy);
    btn.addEventListener('click', () => applyChoice(choice));
    box.appendChild(btn);
  });
  show(box, true);
}

/** 把本轮缓冲的候选渲染出来（只在模型输出结束后调用一次） */
function flushDeferredChoices() {
  const pending = state.deferredChoices;
  state.deferredChoices = null;
  if (!pending || !pending.choices || !pending.choices.length) return;
  renderChoices(pending.choices, pending.note || '');
}

function applyChoice(choice) {
  if (!choice) return;
  if (state.busy) {
    setStatus('本轮仍在运行：请等结束后再点选，或先点「停止」。');
    return;
  }
  const text = String(choice.prompt || choice.value || choice.label || '');
  if (!text) return;
  if (String(choice.kind || '') === 'positive_control') {
    state.pendingPositiveControl = String(choice.value || '');
    state.pendingDecision = choice.value ? 'use' : 'skip';
  }
  // 多组分/配位聚合物的「代表结构怎么取」：把选中的 SMILES 与取法作为请求字段下发 ——
  // 只发散文时后端会按名称重新查询、又是多组分，于是把同一个问题再问一次（真实反馈）。
  if (String(choice.kind || '') === 'molecule') {
    state.pendingMoleculeChoice = {
      smiles: String(choice.value || ''),
      decision: String((choice.detail && choice.detail.mode) || ''),
      label: String(choice.label || ''),
    };
  }
  renderChoices([], '');
  pushMessage('user', choice.label || text);
  startRun(text, { fromChoice: true });
}

/* ------------------------------------------------------------------ SSE（标准帧） */
async function* sseFrames(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index = buffer.indexOf('\n\n');
    while (index >= 0) {
      const chunk = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      let name = 'message';
      const dataLines = [];
      chunk.split('\n').forEach((line) => {
        if (line.startsWith('event:')) name = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
      });
      if (dataLines.length) {
        let data = null;
        try { data = JSON.parse(dataLines.join('\n')); } catch (error) { data = null; }
        if (data !== null) yield { event: name, data };
      }
      index = buffer.indexOf('\n\n');
    }
  }
}

/* 标准帧 → 简易模式关心的少量事件（与高级模式的适配层同契约，只取需要的） */
function frameEvents(frame) {
  const name = frame && frame.event ? frame.event : 'message';
  const data = frame ? frame.data : null;
  if (name === 'metadata') {
    state.standardRunId = (data && data.run_id) || state.standardRunId;
    return [];
  }
  if (name === 'messages/partial') {
    const items = Array.isArray(data) ? data : [data];
    return items.filter((item) => item && typeof item.content === 'string' && item.content)
      .map((item) => ({ type: 'token', content: item.content }));
  }
  if (name === 'messages/complete') {
    const first = Array.isArray(data) ? data[0] : data;
    return (first && first.content) ? [{ type: 'final', content: first.content }] : [];
  }
  if (name === 'custom') return (data && data.type) ? [data] : [];
  if (name === 'values') {
    const values = data || {};
    const out = [];
    if (values.run_id) out.push({ type: 'done', run_id: values.run_id, summary: values.summary || {} });
    return out;
  }
  if (name === 'error') {
    return [{ type: 'error', error_message: (data && data.message) || '运行失败' }];
  }
  if (name === 'end') return [];
  return (data && data.type) ? [data] : [];
}

/* ------------------------------------------------------------------ 运行 */
async function startRun(text, options) {
  const opts = options || {};
  const message = String(text || '').trim();
  if (!message && !state.files.receptor_file && !state.files.molecule_file) {
    setStatus('请填写任务描述或上传文件', 'error');
    return;
  }
  if (state.busy) return;

  state.threadId = state.threadId || localStorage.getItem(LS_THREAD) || newThreadId();
  localStorage.setItem(LS_THREAD, state.threadId);
  state.runId = '';
  state.standardRunId = '';
  state.lastText = '';
  state.thinking = '';
  state.thinkingLive = false;
  state.deferredChoices = null;
  renderChoices([], '');

  if (!opts.fromChoice) pushMessage('user', message || '（仅附件）');
  const assistant = pushMessage('bot', '');
  setAssistant(assistant, '', { typing: true });
  setBusy(true);
  setStatus('解析任务');
  $('s-empty').textContent = '运行中：进度与结论显示在左侧，完成后此处给出推荐分子。';
  show($('s-empty'), true);

  const input = {
    mode: 'chat',
    advanced: false,
    message: message || '使用上传的文件执行一次筛选，受体与参数按任务规约处理',
    conversation_id: state.threadId,
    messages: [{ type: 'human', content: message || '使用上传的文件执行一次筛选' }],
  };
  if (state.files.receptor_file) input.receptor_file = state.files.receptor_file;
  if (state.files.molecule_file) input.molecule_file = state.files.molecule_file;
  if (state.pendingPositiveControl) {
    input.positive_control = state.pendingPositiveControl;
    input.positive_control_decision = state.pendingDecision || 'use';
  }
  const molecule = state.pendingMoleculeChoice;
  if (molecule && molecule.smiles) {
    input.molecule_choice = molecule.smiles;
    input.molecule_choice_decision = molecule.decision || '';
    input.molecule_choice_label = molecule.label || '';
  }
  const body = {
    assistant_id: 'coordinator',
    stream_mode: ['messages', 'updates', 'custom'],
    input,
  };

  /* 标准面：线程 = 会话 id（首次使用时注册；失败不阻塞，服务端会自动建线程） */
  try {
    await fetch('/threads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ thread_id: state.threadId, if_exists: 'do_nothing' }),
    });
  } catch (error) { /* 允许静默：旧后端没有标准面时运行时会自动建线程 */ }

  state.controller = new AbortController();
  try {
    const res = await fetch('/threads/' + encodeURIComponent(state.threadId) + '/runs/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal: state.controller.signal,
    });
    if (!res.ok) {
      let detail = 'HTTP ' + res.status;
      try { detail = (await res.json()).error_message || detail; } catch (error) { /* 允许静默：非 JSON 错误体 */ }
      throw new Error(detail);
    }
    let text = '';
    for await (const frame of sseFrames(res)) {
      for (const event of frameEvents(frame)) {
        if (event.type === 'thinking') {
          state.thinking += String(event.content || '');
          state.thinkingLive = true;
          renderThinking(assistant, state.thinking, { live: true });
        } else if (event.type === 'token') {
          finishThinking(assistant);
          text += event.content || '';
          setAssistant(assistant, text, { typing: false });
        } else if (event.type === 'final') {
          finishThinking(assistant);
          text = event.content || text;
          setAssistant(assistant, text, { typing: false });
        } else if (event.type === 'run_id' && event.run_id) {
          state.runId = event.run_id;
        } else if (event.type === 'stage') {
          setStatus(event.message || '运行中');
          // 阶段变化 = 当前回合结束：把缓冲的候选挂出来（运行中按钮仍是禁用的）
          flushDeferredChoices();
        } else if (event.type === 'progress') {
          if (event.total) setStatus('已完成 ' + fmtInt(event.done) + ' / ' + fmtInt(event.total));
        } else if (event.type === 'choices') {
                  // 先缓冲，回合边界（stage）或本轮结束时再挂出；运行中按钮禁用
          state.deferredChoices = { choices: event.choices || [], note: event.note || '' };
        } else if (event.type === 'limit') {
          // 步数预算：系统自己放宽/收尾，只是如实告知，不需要用户做任何事
          setStatus(event.message || '已达步数上限，自动处理中');
        } else if (event.type === 'error') {
          setStatus(event.error_message || '运行失败', 'error');
        } else if (event.type === 'cancelled') {
          setStatus('已停止，已完成部分仍会落盘');
        } else if (event.type === 'done') {
          if (event.run_id) state.runId = event.run_id;
        }
      }
    }
    state.lastText = text;
    finishThinking(assistant);
    setAssistant(assistant, text || '（本次无文本输出，结果见右侧）', { typing: false });
    setStatus('');
    if (state.runId) {
      localStorage.setItem(LS_LAST_RUN, state.runId);
      await loadRun(state.runId);
    }
  } catch (error) {
    if (error && error.name === 'AbortError') {
      setStatus('已停止');
    } else {
      setStatus('运行失败：' + (error && error.message ? error.message : error), 'error');
    }
  } finally {
    // 模型输出结束（正常结束 / 出错 / 取消都算）→ 这时才把候选渲染出来。
    // 必须**先**解除 busy：renderChoices 会按当前运行态决定按钮是否可点。
    setBusy(false);
    flushDeferredChoices();
    state.controller = null;
    state.pendingPositiveControl = '';
    state.pendingDecision = '';
    state.pendingMoleculeChoice = null;
  }
}

async function stopRun() {
  if (!state.controller) return;
  const runId = state.standardRunId || state.runId;
  if (runId) {
    try {
      await fetch('/threads/' + encodeURIComponent(state.threadId) + '/runs/'
        + encodeURIComponent(runId) + '/cancel', { method: 'POST' });
    } catch (error) { /* 允许静默：取消请求失败时仍会中断本地数据流 */ }
  }
  state.controller.abort();
}

/* ------------------------------------------------------------------ 结果 */
function kpi(label, value, kind) {
  const box = el('div', 's-kpi-item' + (kind ? ' is-' + kind : ''));
  box.appendChild(el('b', '', value));
  box.appendChild(el('span', '', label));
  return box;
}

function renderResult(detail) {
  const meta = (detail && detail.run) || {};
  const result = (detail && detail.result) || {};
  const rows = Array.isArray(result.ranking) ? result.ranking.slice() : [];
  const kpis = $('s-kpi');
  kpis.textContent = '';

  const molecules = Number(meta.molecule_count || rows.length || 0);
  // 大库时 `result.ranking` 只是内联的前 N 条 → 成功数以 ranking_total 为准
  const scored = Number(result.ranking_total || rows.length || 0);
  const best = rows.filter((row) => typeof row.affinity_kcal_mol === 'number')
    .sort((a, b) => a.affinity_kcal_mol - b.affinity_kcal_mol)[0];
  kpis.appendChild(kpi('候选分子', fmtInt(molecules)));
  kpis.appendChild(kpi('成功对接', fmtInt(scored), scored ? 'ok' : 'warn'));
  kpis.appendChild(kpi('最优亲和力', best ? fmtNum(best.affinity_kcal_mol) : '—',
    best ? 'ok' : 'warn'));
  const metaBits = ['亲和力单位 kcal/mol'];
  metaBits.push('搜索强度 ' + (meta.exhaustiveness === undefined || meta.exhaustiveness === null
    ? '自动' : fmtInt(meta.exhaustiveness)));
  if (meta.engine) metaBits.push('引擎 ' + meta.engine);
  if (meta.duration_sec) metaBits.push('用时 ' + fmtInt(meta.duration_sec) + ' s');
  if (meta.status && meta.status !== 'ok') metaBits.push('状态 ' + meta.status);
  $('s-meta').textContent = metaBits.join(' · ');

  const list = $('s-hits');
  list.textContent = '';
  const top = rows.filter((row) => typeof row.affinity_kcal_mol === 'number')
    .sort((a, b) => a.affinity_kcal_mol - b.affinity_kcal_mol).slice(0, 5);
  top.forEach((row, index) => {
    const item = el('li', 's-hit');
    item.appendChild(el('span', 's-hit-rank', '#' + (row.rank || index + 1)));
    item.appendChild(el('span', 's-hit-name', row.name || row.smiles || '（无名称）'));
    const aff = el('span', 's-hit-aff', fmtNum(row.affinity_kcal_mol));
    aff.appendChild(el('small', '', 'kcal/mol'));
    item.appendChild(aff);
    const bits = [];
    if (typeof row.molecular_weight === 'number') bits.push('分子量 ' + fmtNum(row.molecular_weight, 1));
    if (typeof row.logP === 'number') bits.push('logP ' + fmtNum(row.logP));
    if (typeof row.tpsa === 'number') bits.push('TPSA ' + fmtNum(row.tpsa, 1));
    if (typeof row.lipinski_violations === 'number') {
      bits.push(row.lipinski_violations ? 'Lipinski 违例 ' + fmtInt(row.lipinski_violations) : '类药性通过');
    }
    if (typeof row.similarity_to_positive_control === 'number') {
      bits.push('与对照相似度 ' + fmtNum(row.similarity_to_positive_control, 2));
    }
    item.appendChild(el('span', 's-hit-meta', bits.join(' · ')));
    list.appendChild(item);
  });

  const hasRows = top.length > 0;
  show($('s-empty'), !hasRows);
  if (!hasRows) {
    $('s-empty').textContent = '本次无有效对接分数：分子解析或受体准备未完成。'
      + '原因见左侧对话，运行笔记见高级模式。';
  }
  show($('s-actions'), Boolean(state.runId));
  if (state.runId) {
    $('s-report').href = '/api/runs/' + encodeURIComponent(state.runId) + '/report.pdf';
    $('s-csv').href = '/api/runs/' + encodeURIComponent(state.runId) + '/export.csv';
    $('s-zip').href = '/api/runs/' + encodeURIComponent(state.runId) + '/download.zip';
  }
}

async function loadRun(runId) {
  if (!runId) return;
  try {
    const res = await fetch('/api/runs/' + encodeURIComponent(runId), { headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const detail = await res.json();
    state.runId = runId;
    localStorage.setItem(LS_LAST_RUN, runId);
    if (detail.run && detail.run.conversation_id) {
      state.threadId = String(detail.run.conversation_id);
      localStorage.setItem(LS_THREAD, state.threadId);
    }
    renderResult(detail);
    /* 运行记录里带着「当时停在等你选择」的候选：SSE 那一条若错过/被覆盖，这里补挂一次，
       刷新页面或换设备后也能继续点选（真实反馈：「后面的选项不出来」）。 */
    const loadedRun = (detail && detail.run) || {};
    const pending = Array.isArray(loadedRun.choices) ? loadedRun.choices : [];
    if (pending.length) renderChoices(pending, loadedRun.choices_note || '');
  } catch (error) {
    $('s-empty').textContent = '结果载入失败：' + (error && error.message ? error.message : error);
  }
}

async function loadRecentRuns() {
  const select = $('s-runs');
  try {
    const res = await fetch('/api/runs?limit=8', { headers: { Accept: 'application/json' } });
    if (!res.ok) return;
    const data = await res.json();
    (data.runs || []).forEach((run) => {
      const option = el('option', '', (run.run_id || '') + ' · ' + fmtInt(run.molecule_count || 0) + ' 分子');
      option.value = run.run_id || '';
      select.appendChild(option);
    });
    /* **不自动载入上一次运行**（用户反馈：「刷新了网页，右边还是显示上一次的结果」）：
       刷新 = 干净的一页，结果区保持空态；要回看往次结果请显式从「历史运行」里选。
       下拉默认停在占位项，避免"看着已选中、内容却是上一轮"。 */
    select.value = '';
    const last = localStorage.getItem(LS_LAST_RUN);
    if (last && (data.runs || []).some((run) => run.run_id === last)) {
      const empty = $('s-empty');
      if (empty) empty.textContent = '暂无结果。提交任务后，此处显示推荐分子与关键指标；'
        + '要看上一次运行，请在上方「历史运行」里选择 ' + last + '。';
    }
  } catch (error) { /* 允许静默：历史列表只是便利功能，失败不影响主流程 */ }
}

/* ------------------------------------------------------------------ 启动 */
function newThreadId() {
  const stamp = Date.now().toString(36);
  const rand = Math.random().toString(36).slice(2, 8);
  return 'simple-' + stamp + '-' + rand;
}

function init() {
  $('s-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const text = $('s-input').value.trim();
    $('s-input').value = '';
    startRun(text);
  });
  $('s-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      $('s-form').requestSubmit();
    }
  });
  $('s-attach').addEventListener('click', () => $('s-file').click());
  $('s-file').addEventListener('change', onPickFiles);
  $('s-stop').addEventListener('click', stopRun);
  $('s-runs').addEventListener('change', (event) => loadRun(event.target.value));

  const saved = localStorage.getItem(LS_THREAD);
  state.threadId = saved || newThreadId();
  localStorage.setItem(LS_THREAD, state.threadId);

  refreshHealth();
  loadRecentRuns();
  setInterval(refreshHealth, 30000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
