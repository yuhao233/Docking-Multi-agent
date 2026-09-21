/* ==========================================================================
 * 分子对接多 Agent 协作系统 —— 前端交互逻辑
 * 纯原生 JavaScript（ES2020），无构建步骤、无任何外部依赖。
 * 所有接口路径均为同源相对路径（见 projects/docs/api.md v0.3 冻结契约）。
 * ========================================================================== */
'use strict';

/* --------------------------------------------------------------------------
 * 0. 全局状态
 * ------------------------------------------------------------------------ */
const POSITIVE_ROLE = '阳性对照';
/* 实时逐分子结果表最多保留的行数（超出即丢弃最旧行，完整结果见分页总览） */
const LIVE_ROW_LIMIT = 300;
const LARGE_LIBRARY_THRESHOLD = 500;
const DEFAULT_RANKING_SORT = 'affinity_kcal_mol';
const HISTORY_FETCH_LIMIT = 50;
const HISTORY_PAGE_SIZE = 20;
/* 图表展示顺序：大库模式下服务端会自动把 docking_chart / similarity_chart 切成 Top-20 + 直方图 */
const CHART_ORDER = ['docking_chart', 'similarity_chart', 'affinity_histogram', 'property_chart'];

/* 文件上传：与后端 UPLOAD_MAX_MB 默认值保持一致，仅用于前端「明显过大」提示，不做截断 */
const UPLOAD_MAX_MB = 200;
const LIGAND_UPLOAD_EXTS = ['sdf', 'sd', 'smi', 'smiles', 'txt', 'csv', 'mol2', 'mol'];
const RECEPTOR_UPLOAD_EXTS = ['pdb', 'ent', 'pdb1', 'cif', 'mmcif', 'pdbqt'];

/* 终端字符进度条宽度（等宽字符单元格数） */
const BAR_CELLS = 22;
/* 点击停止后，等待后端 cancelled/done 的最长时间；超时则兜底中断本地 fetch */
const CANCEL_ABORT_GRACE_MS = 2500;

/* ---- 多 Agent 编排示意图：节点、标签、事件映射 ---- */
const ORCH_NODE_ORDER = ['coordinator', 'properties', 'pocket', 'docking', 'binding', 'report'];
const ORCH_NODE_LABEL = {
  coordinator: '整体协调 Agent',
  properties: '分子属性评估 Agent',
  pocket: '口袋分析 Agent',
  docking: 'Docking 执行 Agent',
  binding: '结合模式检测 Agent',
  report: '报告生成 Agent'
};
/* 方括号状态标签文案（wait / run / ok / skip / fail / cancel） */
const ORCH_TAG_TEXT = {
  wait: '[ WAIT ]',
  run: '[ RUN ]',
  ok: '[ OK ]',
  skip: '[ SKIP ]',
  fail: '[ FAIL ]',
  cancel: '[ CANCEL ]'
};
/* 确定性流水线：stage → 节点（import→协调 Agent、properties→属性评估…） */
const ORCH_STAGE_NODE = {
  import: 'coordinator',
  properties: 'properties',
  pocket: 'pocket',
  docking: 'docking',
  binding: 'binding',
  report: 'report'
};
/* 多 Agent：tool_call / tool_result 名称 → 节点 */
const ORCH_TOOL_NODE = {
  run_property_assessment: 'properties',
  molecular_property_assessment: 'properties',
  run_pocket_analysis: 'pocket',
  predict_binding_pockets: 'pocket',
  compare_pocket_with_experiment: 'pocket',
  set_docking_site: 'pocket',
  run_docking: 'docking',
  molecular_docking: 'docking',
  run_binding_mode_analysis: 'binding',
  binding_mode_analysis: 'binding',
  positive_control_similarity: 'binding',
  generate_screening_report: 'report',
  import_molecule_library: 'coordinator',
  list_known_receptors: 'coordinator'
};

const state = {
  health: null,
  receptors: [],
  defaultReceptor: '',
  libraries: [],
  positiveControl: '',
  ligandSource: 'text',
  /* page：交互子页（chat = 对话模式 / manual = 参数模式），只决定配置区显示哪一页 */
  page: 'chat',
  /* view：顶层页面（workbench = 工作台 / settings = 设置），由导航栏与 #/settings 切换 */
  view: 'workbench',
  /* mode：参数模式下的运行方式（pipeline = 确定性流水线 / agent = 多 Agent 协作） */
  mode: 'pipeline',
  /* advancedTouched：对话模式的高级设置是否被用户展开过 */
  advancedTouched: false,
  /* touchedFields：用户**真正改动过**的字段 id 集合。只有这些字段才作为"默认值"下发；
     未改动 = 留空/自动（搜索强度自动规划、位点盒由口袋分析确定、不做阳性对照），
     因此"打开过高级设置又关掉"不会造成任何参数注入。 */
  touchedFields: new Set(),
  /* 对话附件：本次会话上传/拖入的文件（受体或小分子库），可被 @ 引用 */
  attachments: [],
  /* @ 引用选择器的状态 */
  mention: { open: false, query: '' },
  runId: null,
  /* conversationId：多轮会话 id（同一 id = 服务端同一段对话，LangGraph thread_id）。
     首次进入生成并写入 localStorage，刷新后继续聊；点「新对话」才换新 id。 */
  conversationId: '',
  running: false,
  /* stopping：已点击停止、正在等待后端取消确认（按钮显示「正在停止…」） */
  stopping: false,
  /* cancelled：本次运行已被取消（收到 cancelled 事件或 done.summary.status == cancelled） */
  cancelled: false,
  /* 本次运行是否提供了阳性对照（留空则后端跳过对照分析，结合模式检测节点标记 SKIP） */
  runPositiveControl: '',
  /* 示例阳性对照（来自 /api/libraries，仅在用户点击按钮时填入，绝不自动预填） */
  examplePositive: null,
  /* 上传状态机：ligand / receptor 各自 空(empty) → 上传中(uploading) → 成功(ok) / 失败(error) */
  upload: {
    ligand: { phase: 'empty', fileName: '', size: 0, progress: 0, error: '', data: null, seq: 0 },
    receptor: { phase: 'empty', fileName: '', size: 0, progress: 0, error: '', data: null, seq: 0 }
  },
  /* 位点字段是否被用户手动改过（手动改过时，上传受体不再自动覆盖） */
  siteTouched: false,
  /* 当前位点字段是否来自「上传受体的位点盒」自动填入 */
  uploadSiteFilled: false,
  controller: null,
  stopTimer: null,
  startedAt: 0,
  elapsedTimer: null,
  /* 服务端 progress 事件给出的权威已用时间（用于校正本地计时器） */
  serverElapsed: null,
  /* progress 事件给出的剩余时间（秒），供终端进度条与编排指标条复用 */
  etaSec: null,
  progress: { index: 0, total: 0, percent: 0, stage: '', lastDecile: -1 },
  /* 多 Agent 编排示意图状态 */
  orch: {
    /* timing：{node: [{name, start, end}]}，start/end 为相对本次运行起点(ms)的真实时间
       数据来自服务端事件时间戳(ts)，因此「并行/串行」是实测而不是画死的流程 */
    timing: {},
    t0: null,
    parallel: 0,
    coordinator: 'wait',
    properties: 'wait',
    docking: 'wait',
    binding: 'wait',
    report: 'wait',
    current: '',
    overall: 'wait',
    coord: '待机'
  },
  liveCount: 0,
  artifacts: [],
  /* 后端下发的规范下载文件名（report_pdf/report_md/ranking_csv/data_zip/poses_zip/prefix） */
  downloads: {},
  reportMarkdown: '',
  tokenBuffer: '',
  tokenRendered: '',
  lastEventAt: 0,
  /* 结果总览：服务端分页查询状态（offset/limit/sort/order/q/hits_only） */
  ranking: {
    offset: 0,
    limit: 50,
    sort: DEFAULT_RANKING_SORT,
    order: 'asc',
    query: '',
    hitsOnly: false,
    total: 0,
    aggregates: null,
    rows: [],
    loading: false,
    error: '',
    seq: 0
  },
  /* 分子详情：复用同一个 /ranking 分页接口，只渲染当前页卡片 */
  cards: {
    offset: 0,
    limit: 12,
    total: 0,
    rows: [],
    loading: false,
    error: '',
    seq: 0
  },
  positiveRow: null,
  currentRun: null,
  /* 多 Agent 协作痕迹（共享黑板 → {"stats","notes"}） */
  collaboration: null,
  estimatedCount: 0,
  /* 历史运行：一次最多取 50 条，界面按 20 条逐段展开，避免一次性铺开过多 DOM */
  historyRuns: [],
  historyShown: 0,
  /* 对话历史 */
  chatMessages: [],
  chatSeq: 0,
  chatActiveId: null
};

/* 对话模式高级设置里各字段的说明（留空语义） */
const ADV_EMPTY_HINT = '留空则按系统默认 / 由指令决定。';

/* --------------------------------------------------------------------------
 * 1. DOM 与通用工具
 * ------------------------------------------------------------------------ */
function $(id) {
  return document.getElementById(id);
}

function el(tag, cls, text, title) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  /* 被 CSS 截断/省略的内容补一个 title，鼠标悬停可看全（布局不受影响） */
  if (title !== undefined && title !== null && String(title) !== '') node.title = String(title);
  return node;
}

function clear(node) {
  if (node) node.replaceChildren();
}

function hide(node) { if (node) node.classList.add('hidden'); }
function show(node) { if (node) node.classList.remove('hidden'); }

function escapeHtml(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function isBlank(value) {
  return value === undefined || value === null || value === '';
}

/** 第一个既存在又非空的值 */
function pick(obj, keys, fallback) {
  if (obj && typeof obj === 'object') {
    for (const key of keys) {
      const value = obj[key];
      if (value !== undefined && value !== null && value !== '') return value;
    }
  }
  return fallback === undefined ? undefined : fallback;
}

function toNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

function fmtNum(value, digits) {
  const number = toNumber(value);
  if (number === null) return '—';
  return number.toFixed(digits === undefined ? 2 : digits);
}

/** 带符号的差值展示（正数补 +，缺失显示 —） */
function fmtSigned(value, digits) {
  const number = toNumber(value);
  if (number === null) return '—';
  const text = number.toFixed(digits === undefined ? 2 : digits);
  return number > 0 ? '+' + text : text;
}

function fmtText(value) {
  if (isBlank(value)) return '—';
  return String(value);
}

function fmtInt(value) {
  const number = toNumber(value);
  if (number === null) return '—';
  return String(Math.round(number));
}

function fmtSize(bytes) {
  const number = toNumber(bytes);
  if (number === null || number < 0) return '—';
  if (number < 1024) return number + ' B';
  if (number < 1024 * 1024) return (number / 1024).toFixed(1) + ' KB';
  return (number / 1024 / 1024).toFixed(2) + ' MB';
}

function fmtTime(value) {
  if (isBlank(value)) return '—';
  const raw = String(value).replace('T', ' ');
  return raw.length > 19 ? raw.slice(0, 19) : raw;
}

function nowStamp() {
  const date = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds());
}

function fmtDuration(seconds) {
  const total = Math.max(0, Math.round(toNumber(seconds) || 0));
  if (total < 60) return total + ' 秒';
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return minutes + ' 分 ' + rest + ' 秒';
}

/** 与 pick 类似，但把 false / 0 也视为有效取值 */
function pickDefined(obj, keys) {
  if (obj && typeof obj === 'object') {
    for (const key of keys) {
      const value = obj[key];
      if (value !== undefined && value !== null) return value;
    }
  }
  return undefined;
}

/** 与 pick 类似，但可串联多个来源对象（顶层 / properties / binding / docking） */
function pickFrom(sources, keys) {
  for (const source of sources) {
    const value = pick(source, keys, undefined);
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return undefined;
}

/** 布尔字段：容忍 true/"true"/1/"yes"/"pass" 等写法；缺失返回 undefined */
function toBool(value) {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  const text = String(value).trim().toLowerCase();
  if (['true', 'yes', 'y', '1', 'pass', 'ok', '是', '通过', '合格'].includes(text)) return true;
  if (['false', 'no', 'n', '0', 'fail', '否', '未通过', '不合格'].includes(text)) return false;
  return undefined;
}

/** 任意结构（对象 / 数组 / 字符串）转成可读文本，供药效团、锚定匹配等字段展示 */
function fmtStruct(value) {
  if (value === undefined || value === null || value === '') return '—';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) {
    if (!value.length) return '—';
    return value.map((item) => (typeof item === 'object' ? fmtStruct(item) : String(item))).join('、');
  }
  try {
    return Object.keys(value)
      .map((key) => key + ': ' + fmtStruct(value[key]))
      .join('；');
  } catch (error) {
    return String(value);
  }
}

function shortError(error) {
  if (!error) return '未知错误';
  let text = error.message || String(error);
  if (error.name === 'AbortError' || /aborted|abort/i.test(text)) text = '请求已被取消（用户停止）';
  return text;
}

/* --------------------------------------------------------------------------
 * 1.5 终端风格工具：等宽时钟 / 字符进度条 / 多 Agent 编排状态
 * ------------------------------------------------------------------------ */

/** 秒 → 等宽时钟：mm:ss 或 hh:mm:ss（无法计算时返回 --:--） */
function clockOf(seconds) {
  const value = toNumber(seconds);
  if (value === null || value < 0) return '--:--';
  const total = Math.round(value);
  const pad = (n) => String(n).padStart(2, '0');
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  return (hours > 0 ? pad(hours) + ':' : '') + pad(minutes) + ':' + pad(rest);
}

/** 已用秒数：本地计时器优先，服务端 elapsed_sec 到达后用较大值校正 */
function elapsedSeconds() {
  let seconds = state.startedAt ? (Date.now() - state.startedAt) / 1000 : 0;
  if (state.serverElapsed) {
    seconds = Math.max(seconds,
      state.serverElapsed.sec + (Date.now() - state.serverElapsed.at) / 1000);
  }
  return seconds;
}

/**
 * 终端字符进度条：[████████░░░░░░░░░░░░░░] 42%  120/126  ETA 00:18
 * 纯 monospace 字符，不使用任何图片；数值位宽固定，刷新时不抖动。
 */
function renderProgressBarText() {
  const node = $('progress-bar-text');
  if (!node) return;
  const percent = Math.max(0, Math.min(100, Math.round(toNumber(state.progress.percent) || 0)));
  const filled = Math.round((percent / 100) * BAR_CELLS);
  const bar = '█'.repeat(filled) + '░'.repeat(Math.max(0, BAR_CELLS - filled));
  const total = state.progress.total;
  const counter = total > 0 ? (fmtInt(state.progress.index) + '/' + fmtInt(total)) : '—/—';
  const eta = clockOf(state.etaSec);
  node.textContent = '[' + bar + '] ' + String(percent).padStart(3, ' ') + '%  ' +
    counter + '  ETA ' + eta;
  const etaNode = $('progress-eta-text');
  if (etaNode) etaNode.textContent = 'ETA ' + eta + ' · 用时 ' + clockOf(elapsedSeconds());
}

/** 编排面板底部紧凑指标条：DONE / PCT / ELAPSED / ETA */
function renderOrchMetrics() {
  const done = $('orch-metric-done');
  if (done) done.textContent = fmtInt(state.progress.index) + '/' + fmtInt(state.progress.total);
  const pct = $('orch-metric-percent');
  if (pct) pct.textContent = Math.round(toNumber(state.progress.percent) || 0) + '%';
  const elapsed = $('orch-metric-elapsed');
  if (elapsed) elapsed.textContent = clockOf(elapsedSeconds());
  const eta = $('orch-metric-eta');
  if (eta) eta.textContent = clockOf(state.etaSec);
}

/** 编排节点 → DOM（tag 文案 + data-state 供 CSS 上色 / 流光） */
function setOrchNode(node, status) {
  if (!node || !ORCH_TAG_TEXT[status]) return;
  state.orch[node] = status;
  const box = $('orch-node-' + node);
  if (box) box.dataset.state = status;
  const tag = $('orch-tag-' + node);
  if (tag) {
    tag.textContent = ORCH_TAG_TEXT[status];
    tag.className = 'tag tag-' + status;
  }
}

/** 顶部总体状态徽标 */
function setOrchOverall(status) {
  const key = ORCH_TAG_TEXT[status] ? status : 'wait';
  state.orch.overall = key;
  const tag = $('orch-run-state');
  if (!tag) return;
  tag.textContent = ORCH_TAG_TEXT[key];
  tag.className = 'tag tag-' + key;
}

/** 协调 Agent 当前动作（如「正在分发任务」「正在汇总生成报告」） */
function setOrchCoord(action) {
  state.orch.coord = action || '待机';
  const node = $('orch-coord');
  if (node) node.textContent = '协调动作：' + state.orch.coord;
}

/** 「当前任务」一行：节点名 + 已完成/总数（百分比） */
function updateOrchTask() {
  const node = $('orch-task');
  if (!node) return;
  const current = state.orch.current;
  const label = current ? ORCH_NODE_LABEL[current] : '待机';
  let text = '当前任务：' + label;
  if (current && state.progress.total > 0) {
    text += ' · 已完成 ' + fmtInt(state.progress.index) + '/' + fmtInt(state.progress.total) +
      '（' + Math.round(toNumber(state.progress.percent) || 0) + '%）';
  } else if (current) {
    text += ' · 进行中';
  }
  node.textContent = text;
}

/** 把焦点推进到某个节点：之前的运行中节点判定为完成 */
function orchAdvanceTo(node) {
  if (!node) return;
  const target = ORCH_NODE_ORDER.indexOf(node);
  ORCH_NODE_ORDER.forEach((item, index) => {
    if (index < target && state.orch[item] === 'run') setOrchNode(item, 'ok');
  });
  setOrchNode(node, 'run');
  state.orch.current = node;
  setOrchOverall('run');
  if (node === 'coordinator') setOrchCoord('正在分发任务');
  else if (node === 'report') setOrchCoord('正在汇总生成报告');
  else if (node === 'binding') setOrchCoord('正在检测结合模式');
  else if (node === 'docking') setOrchCoord('正在执行分子对接');
  else if (node === 'properties') setOrchCoord('正在评估分子属性');
  else setOrchCoord('正在分发任务');
  updateOrchTask();
  renderOrchMetrics();
}

/** 阶段推进：只在节点仍为 [ WAIT ] 时推进，避免迟到/重复事件把已完成节点拉回运行中 */
function orchStageAdvance(node, skipped) {
  if (!node) return;
  // 后端显式标记 skipped（如未提供阳性对照时的结合模式分析）→ 直接置 SKIP
  if (skipped) {
    if (state.orch[node] === 'wait') setOrchNode(node, 'skip');
    return;
  }
  if (state.orch[node] !== 'wait') return;
  const targetIndex = ORCH_NODE_ORDER.indexOf(node);
  const currentIndex = ORCH_NODE_ORDER.indexOf(state.orch.current);
  if (state.orch.current && state.orch[state.orch.current] === 'run' && currentIndex > targetIndex) return;
  orchAdvanceTo(node);
}

/** 收尾：final/done → 所有已运行节点 [ OK ]；无阳性对照时结合模式检测 [ SKIP ] */
function orchSettle(status) {
  // 运行结束：把所有仍未收尾的条带按「现在」收尾，时间轴与并行度随之定稿
  const spans = state.orch.timing || {};
  Object.keys(spans).forEach((node) => {
    (spans[node] || []).forEach((item) => {
      if (item.end === null) item.end = Math.max(orchNow(), item.start);
    });
  });
  state.running = false;
  renderOrchLanes();
  ORCH_NODE_ORDER.forEach((node) => {
    const current = state.orch[node];
    if (current === 'run') setOrchNode(node, status);
    else if (current === 'wait' && node === 'binding' && !state.runPositiveControl) {
      setOrchNode(node, 'skip');
    }
  });
  setOrchOverall(status);
  setOrchCoord(status === 'ok' ? '已完成' : '待机');
  updateOrchTask();
}

/** 出错：当前节点标记 [ FAIL ] */
function orchFail(node) {
  const target = node || state.orch.current || 'coordinator';
  setOrchNode(target, 'fail');
  state.orch.current = target;
  setOrchOverall('fail');
  setOrchCoord('执行失败');
  updateOrchTask();
}

/** 取消：当前节点标记 [ CANCEL ] */
function orchCancel() {
  const target = state.orch.current || 'coordinator';
  setOrchNode(target, 'cancel');
  state.orch.current = target;
  setOrchOverall('cancel');
  setOrchCoord('运行已被取消');
  updateOrchTask();
}

/** 复位到「待机」：所有节点 [ WAIT ]，指标清零 */
function resetOrchestration() {
  state.orch.timing = {};
  state.orch.t0 = null;
  state.orch.stageNode = '';
  state.orch.parallel = 0;
  ORCH_NODE_ORDER.forEach((node) => setOrchNode(node, 'wait'));
  state.orch.current = '';
  setOrchOverall('wait');
  setOrchCoord('待机');
  updateOrchTask();
  renderOrchMetrics();
}

/** 多 Agent 工具名 → 编排节点（未知工具返回 ''，不改变任何节点） */
function orchToolNode(name) {
  const key = String(name || '').trim();
  if (!key) return '';
  if (ORCH_TOOL_NODE[key]) return ORCH_TOOL_NODE[key];
  if (key.indexOf('fetch_') === 0 || key.indexOf('fetch') === 0) return 'coordinator';
  return '';
}

/* --------------------------------------------------------------------------
 * 1.6 运行图计时：按服务端事件时间戳记录每个节点的真实起止时间
 *     —— 同层节点的条带重叠=并行、依次排列=串行，运行图据此渲染
 * ------------------------------------------------------------------------ */
/** 事件时间戳（服务端 ts 优先，缺失时退回本地时钟） */
function eventTs(data) {
  const ts = toNumber(data && data.ts);
  return ts === null ? Date.now() : ts;
}

/** 记录某个节点的一次执行开始 */
function orchMarkStart(node, name, data) {
  if (!node) return;
  if (state.orch.t0 === null) state.orch.t0 = eventTs(data);
  const rel = Math.max(0, eventTs(data) - state.orch.t0);
  const list = state.orch.timing[node] || (state.orch.timing[node] = []);
  list.push({ name: name || node, start: rel, end: null });
  renderOrchLanes();
}

/** 记录某个节点的一次执行结束（把最近一个未结束的条目收尾） */
function orchMarkEnd(node, data) {
  if (!node) return;
  const list = state.orch.timing[node];
  if (!list || !list.length) return;
  const rel = state.orch.t0 === null ? 0 : Math.max(0, eventTs(data) - state.orch.t0);
  for (let i = list.length - 1; i >= 0; i -= 1) {
    if (list[i].end === null) {
      list[i].end = Math.max(rel, list[i].start);
      break;
    }
  }
  renderOrchLanes();
}

/** 每个节点的实测跨度（多个条目合并为 [最早开始, 最晚结束]） */
function orchSpans() {
  const spans = {};
  const timing = state.orch.timing || {};
  Object.keys(timing).forEach((node) => {
    const items = timing[node] || [];
    if (!items.length) return;
    const start = Math.min(...items.map((it) => it.start));
    const now = state.running ? orchNow() : 0;
    const ends = items.map((it) => (it.end === null ? Math.max(now, it.start) : it.end));
    spans[node] = { start, end: Math.max(...ends), items };
  });
  return spans;
}

/** 当前时刻相对运行起点的偏移（运行中用于把未结束的条带画到「现在」） */
function orchNow() {
  return state.startedAt ? Math.max(0, Date.now() - state.startedAt) : 0;
}

/**
 * 实测并行度：统计同层子 Agent 节点两两之间的时间重叠。
 * 返回 {level, max, pairs, sequential}；max>=2 表示确有并行执行。
 */
function orchParallelism(spans) {
  const children = ['properties', 'pocket', 'docking', 'binding'].filter((n) => spans[n]);
  const pairs = [];
  for (let i = 0; i < children.length; i += 1) {
    for (let j = i + 1; j < children.length; j += 1) {
      const a = spans[children[i]];
      const b = spans[children[j]];
      const overlap = Math.min(a.end, b.end) - Math.max(a.start, b.start);
      if (overlap > 50) pairs.push({ a: children[i], b: children[j], overlap });
    }
  }
  let max = children.length ? 1 : 0;
  const points = [];
  children.forEach((n) => { points.push([spans[n].start, 1], [spans[n].end, -1]); });
  points.sort((x, y) => (x[0] - y[0]) || (x[1] - y[1]));
  let cur = 0;
  points.forEach(([, delta]) => { cur += delta; max = Math.max(max, cur); });
  return { max, pairs, sequential: children.length > 1 && pairs.length === 0 };
}

/** 绘制实测时间轴：一根条 = 一个节点的一次真实执行 */
function renderOrchLanes() {
  const host = $('orch-lanes');
  if (!host) return;
  const spans = orchSpans();
  const nodes = ORCH_NODE_ORDER.filter((n) => spans[n]);
  clear(host);
  if (!nodes.length) {
    host.appendChild(el('p', 'empty', '尚未开始：运行后此处按真实起止时间绘制各 Agent 的时间条。'));
    setOrchParallelBadge(0, 0);
    return;
  }
  const total = Math.max(1, ...nodes.map((n) => spans[n].end));
  const parallel = orchParallelism(spans);
  nodes.forEach((node) => {
    const row = el('div', 'orch-lane');
    row.dataset.node = node;
    if (spans[node].items.length > 1) row.dataset.repeat = 'true';
    const label = el('span', 'orch-lane-label mono', (ORCH_NODE_LABEL[node] || node)
      .replace(' Agent', '').replace('分子属性评估', '属性评估'));
    row.appendChild(label);
    const track = el('div', 'orch-lane-track');
    spans[node].items.forEach((item) => {
      const bar = el('span', 'orch-lane-bar');
      const end = item.end === null ? Math.max(orchNow(), item.start) : item.end;
      const leftPct = ((item.start / total) * 100).toFixed(3) + '%';
      const widthPct = Math.max(0.6, (((end - item.start) / total) * 100)).toFixed(3) + '%';
      bar.style.left = leftPct;
      bar.style.width = prefersReducedMotion() ? widthPct : '0%';
      bar.dataset.state = state.orch[node] || 'run';
      bar.title = (item.name || node) + '：' + fmtDuration((end - item.start) / 1000);
      track.appendChild(bar);
      if (!prefersReducedMotion()) {
        /* 下一帧再给目标宽度 → 由 CSS transition 平滑展开（实测时间条"长出来"的观感） */
        window.requestAnimationFrame(() => { bar.style.width = widthPct; });
      }
    });
    row.appendChild(track);
    const meta = el('span', 'orch-lane-meta mono',
      fmtDuration((spans[node].end - spans[node].start) / 1000));
    row.appendChild(meta);
    host.appendChild(row);
  });
  setOrchParallelBadge(parallel.max, parallel.pairs.length);
}

/** 顶栏徽标：实测并行度 */
function setOrchParallelBadge(max, pairCount) {
  const badge = $('orch-parallel');
  if (!badge) return;
  state.orch.parallel = max || 0;
  if (!max || max < 2) {
    badge.textContent = max === 1 ? '[ 串行 ]' : '[ -- ]';
    badge.className = 'tag tag-wait';
    badge.title = max === 1 ? '实测同层子 Agent 依次执行（无时间重叠）' : '等待运行数据';
    return;
  }
  badge.textContent = '[ 并行 ×' + fmtInt(max) + ' ]';
  badge.className = 'tag tag-ok';
  badge.title = '实测最多 ' + max + ' 个子 Agent 同时执行'
    + (pairCount ? '（' + pairCount + ' 组时间重叠）' : '');
}

/* --------------------------------------------------------------------------
 * 2. HTTP 封装：所有 fetch 都做 try/catch 与中文错误提示
 * ------------------------------------------------------------------------ */
async function reqJson(url, options) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (error) {
      data = null;
    }
  }
  if (!response.ok) {
    const message = (data && (data.error_message || data.error || data.message)) ||
      (text ? text.slice(0, 300) : '') ||
      ('HTTP ' + response.status);
    const err = new Error(message);
    err.status = response.status;
    err.payload = data;
    throw err;
  }
  if (data === null) {
    const err = new Error('服务端返回了非 JSON 内容（HTTP ' + response.status + '）');
    err.status = response.status;
    throw err;
  }
  return data;
}

function getJson(path) {
  return reqJson(path, { method: 'GET', headers: { Accept: 'application/json' } });
}


/* --------------------------------------------------------------------------
 * 2b. 标准 Agent Protocol 客户端（阶段 2）
 *   网页端改走**标准面**：POST /threads/{thread_id}/runs/stream，帧为
 *     metadata / messages/partial / updates / custom / messages/complete / values / end / error
 *   这里把标准帧**翻译回既有内部事件**再交给 handleEvent，
 *   因此渲染、编排、报告、历史等逻辑一行都不用改（旧端点仍保留为 deprecated 兼容层）。
 * ------------------------------------------------------------------------ */
const STANDARD_ASSISTANTS = { chat: 'coordinator', agent: 'coordinator', pipeline: 'pipeline' };

function runsStreamPath(threadId) {
  return '/threads/' + encodeURIComponent(threadId) + '/runs/stream';
}

function runsCancelPath(threadId, runId) {
  return '/threads/' + encodeURIComponent(threadId) + '/runs/' + encodeURIComponent(runId) + '/cancel';
}

/** 首次使用时把本地会话 id 注册成服务端线程（失败不阻塞：运行时会自动建线程）。 */
async function ensureThreadRegistered(threadId) {
  try {
    await fetch('/threads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ thread_id: threadId, if_exists: 'do_nothing' })
    });
  } catch (error) { /* 允许静默：旧后端没有标准面 */ }
}

/** 标准帧 → 既有内部事件（handleEvent 的输入契约）。 */
function standardFrameEvents(frame) {
  const name = (frame && frame.event) ? frame.event : 'message';
  const data = frame ? frame.data : null;
  const out = [];
  if (name === 'metadata') {
    /* 平台风格 run uuid：取消时优先用它（标准面两种 id 都接受） */
    state.standardRunId = (data && data.run_id) ? data.run_id : null;
    return out;
  }
  const ts = (data && typeof data === 'object' && data.ts) ? data.ts : undefined;
  if (name === 'messages/partial') {
    const items = Array.isArray(data) ? data : [data];
    for (const item of items) {
      const text = item && typeof item.content === 'string' ? item.content : '';
      if (text) out.push({ type: 'token', content: text, ts: ts });
    }
    return out;
  }
  if (name === 'updates') {
    /* 服务端每个标准帧的 data 里都带 ts（前端据此算真实起止），它不是节点名，必须排除 */
    const nodes = (data && typeof data === 'object')
      ? Object.keys(data).filter((key) => key !== 'ts') : [];
    const node = nodes[0] || '';
    const payload = node ? data[node] : {};
    out.push({ type: 'update', node: node || 'node', keys: (payload && payload.keys) || [], ts: ts });
    return out;
  }
  if (name === 'messages/complete') {
    const first = Array.isArray(data) ? data[0] : data;
    if (first && typeof first.content === 'string' && first.content) {
      out.push({ type: 'final', content: first.content, ts: ts });
    }
    return out;
  }
  if (name === 'custom') {
    if (data && data.type) out.push(data);
    return out;
  }
  if (name === 'values') {
    const values = data || {};
    out.push({ type: 'values', values: values, ts: ts });
    if (values.run_id) {
      out.push({ type: 'done', run_id: values.run_id, summary: values.summary || {}, ts: ts });
    }
    return out;
  }
  if (name === 'error') {
    out.push({
      type: 'error',
      error_code: (data && data.error) || 'error',
      error_message: (data && data.message) || '运行失败'
    });
    return out;
  }
  if (name === 'end') return out;
  /* 兼容：服务端若仍是自定义帧（event: message + data.type），直接透传 */
  if (data && data.type) out.push(data);
  return out;
}

/* --------------------------------------------------------------------------
 * 3. SSE 解析（fetch + ReadableStream，因为需要 POST）
 *    - 按 SSE 规范以空行分隔事件，跨 chunk 断行由缓冲区兜底
 *    - 支持多行 data: 拼接；同时兼容后端 "event: message" 统一报文
 * ------------------------------------------------------------------------ */
async function* sseEvents(response, signal, withEventName = false) {
  if (!response.body || typeof response.body.getReader !== 'function') {
    throw new Error('当前浏览器不支持流式读取（ReadableStream），无法接收实时事件');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  const parseBlock = (block) => {
    let eventName = 'message';
    const dataLines = [];
    for (const rawLine of block.split(/\r?\n/)) {
      if (!rawLine || rawLine.startsWith(':')) continue;
      const colon = rawLine.indexOf(':');
      const field = colon === -1 ? rawLine : rawLine.slice(0, colon);
      let value = colon === -1 ? '' : rawLine.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'event') eventName = value;
      else if (field === 'data') dataLines.push(value);
    }
    if (!dataLines.length) return null;
    const data = dataLines.join('\n');
    if (data === '[DONE]') return { event: eventName, data: null, done: true };
    let parsed = null;
    try {
      parsed = JSON.parse(data);
    } catch (error) {
      parsed = { type: 'parse_error', raw: data };
    }
    return { event: eventName, data: parsed, done: false };
  };

  let finished = false;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep = buffer.search(/\r?\n\r?\n/);
    while (sep !== -1) {
      const match = /\r?\n\r?\n/.exec(buffer.slice(sep));
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + (match ? match[0].length : 2));
      if (block.trim()) {
        const parsed = parseBlock(block);
        if (parsed) {
          if (parsed.done) { finished = true; break; }
          yield (withEventName ? parsed : parsed.data);
        }
      }
      sep = buffer.search(/\r?\n\r?\n/);
    }
    if (finished) break;
  }
  if (finished) return;
  buffer += decoder.decode();
  if (buffer.trim()) {
    const parsed = parseBlock(buffer);
    if (parsed && !parsed.done) yield (withEventName ? parsed : parsed.data);
  }
  if (signal && signal.aborted) throw new DOMException('Aborted', 'AbortError');
}

/* --------------------------------------------------------------------------
 * 4. rAF 节流批量 DOM 更新
 * ------------------------------------------------------------------------ */
function createThrottled(handler, intervalMs) {
  let queued = [];
  let rafId = 0;
  let last = 0;

  const flush = () => {
    rafId = 0;
    last = performance.now();
    const batch = queued;
    queued = [];
    if (batch.length) handler(batch);
  };

  return (item) => {
    queued.push(item);
    if (rafId) return;
    const wait = Math.max(0, (intervalMs || 0) - (performance.now() - last));
    if (wait < 12) flush();
    else rafId = requestAnimationFrame(() => { rafId = 0; flush(); });
  };
}

/* --------------------------------------------------------------------------
 * 5. 精简 Markdown 渲染器（先转义 HTML，再做受限替换，防 XSS）
 * ------------------------------------------------------------------------ */

/**
 * 报告里的内嵌图片：![alt](url) → 图片容器 + 原图链接 + 失败占位。
 * - 容器 max-width:100% + overflow:hidden（见 styles.css），大图绝不撑破布局；
 * - 图片本体 display:block、max-width:100%、loading=lazy/decoding=async；
 * - 点击在新标签打开原图（target=_blank + rel=noopener）；
 * - onerror 时隐藏图片并显示「图片加载失败」占位文案，不出现破图。
 * 注意：只通过内联 onerror 做降级（不引入任何外部依赖）。
 */
function imageHtml(url, alt) {
  const safeUrl = escapeHtml(url);
  const safeAlt = escapeHtml(alt || '图片');
  const fallback = escapeHtml(alt ? ('图片加载失败：' + alt) : '图片加载失败（资源不可用）');
  const onError = "this.style.display='none';var f=this.parentNode.querySelector('.md-img-fallback');if(f){f.hidden=false;}";
  return '<span class="md-img-box">' +
    '<a class="md-img-link" href="' + safeUrl + '" target="_blank" rel="noopener">' +
    '<img class="md-img" src="' + safeUrl + '" alt="' + safeAlt + '" loading="lazy" decoding="async"' +
    ' onerror="' + onError + '">' +
    '<span class="md-img-fallback" hidden>' + fallback + '</span>' +
    '</a></span>';
}

function renderInline(text) {
  let out = escapeHtml(text);
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
  /* 图片必须先于链接处理，否则 ![alt](url) 会被链接规则截成 !<a> */
  out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)/g,
    (match, alt, url) => imageHtml(url, alt));
  /* 站内锚点链接：保持原有行为 */
  out = out.replace(/\[([^\]]+)\]\((#[^)\s]*)\)/g, '<a href="$2">$1</a>');
  /* 其它链接（报告里指向 run 产物等）：新标签打开 */
  out = out.replace(/\[([^\]]+)\]\(((?!javascript:)[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return out;
}

function splitTableRow(line) {
  let text = line.trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|')) text = text.slice(0, -1);
  const cells = [];
  let current = '';
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (char === '\\' && text[i + 1] === '|') {
      current += '|';
      i += 1;
    } else if (char === '|') {
      cells.push(current.trim());
      current = '';
    } else {
      current += char;
    }
  }
  cells.push(current.trim());
  return cells;
}

function isTableSeparator(line) {
  if (!line || line.indexOf('-') === -1) return false;
  return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
}

function buildMarkdownTable(header, aligns, bodyRows) {
  const wrap = el('div', 'table-wrap');
  const table = el('table', 'md-table');
  const thead = el('thead');
  const headRow = el('tr');
  header.forEach((cell, index) => {
    const th = el('th');
    th.innerHTML = renderInline(cell);
    if (aligns[index]) th.style.textAlign = aligns[index];
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = el('tbody');
  bodyRows.forEach((cells) => {
    const tr = el('tr');
    const numeric = aligns.every((a) => !a) && cells.length > 1 &&
      cells.slice(1).every((c) => c === '' || /^[-+]?[\d.,%eE\s]+$/.test(c));
    cells.forEach((cell, index) => {
      const td = el('td');
      td.innerHTML = renderInline(cell);
      if (aligns[index]) td.style.textAlign = aligns[index];
      else if (numeric && index > 0) td.classList.add('md-num');
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

function renderMarkdown(markdown) {
  const source = String(markdown || '');
  const lines = source.split(/\r?\n/);
  const root = el('div', 'md-root');
  let i = 0;

  const listStack = [];
  const openList = (ordered) => {
    const list = el(ordered ? 'ol' : 'ul');
    const top = listStack[listStack.length - 1];
    if (top) {
      const host = top.lastElementChild || top;
      host.appendChild(list);
    } else {
      root.appendChild(list);
    }
    listStack.push(list);
  };
  const closeList = () => {
    listStack.forEach((list) => {
      if (!list.childElementCount) list.remove();
    });
    listStack.length = 0;
  };

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // 代码块
    if (/^```/.test(trimmed)) {
      closeList();
      const lang = trimmed.slice(3).trim();
      const codeLines = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) {
        codeLines.push(lines[i]);
        i += 1;
      }
      i += 1;
      const pre = el('pre');
      const code = el('code');
      if (lang) code.className = 'lang-' + lang.replace(/[^a-zA-Z0-9_-]/g, '');
      code.textContent = codeLines.join('\n');
      pre.appendChild(code);
      root.appendChild(pre);
      continue;
    }

    // 分隔线
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
      closeList();
      root.appendChild(el('hr'));
      i += 1;
      continue;
    }

    // 空行
    if (!trimmed) {
      closeList();
      i += 1;
      continue;
    }

    // 标题
    const heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
    if (heading) {
      closeList();
      const level = Math.min(4, heading[1].length);
      const node = el('h' + level);
      node.innerHTML = renderInline(heading[2].replace(/\s*#+\s*$/, ''));
      root.appendChild(node);
      i += 1;
      continue;
    }

    // 表格（当前行含 |，下一行是分隔行）
    if (trimmed.indexOf('|') !== -1 && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      closeList();
      const header = splitTableRow(line);
      const aligns = splitTableRow(lines[i + 1]).map((cell) => {
        const left = cell.startsWith(':');
        const right = cell.endsWith(':');
        if (left && right) return 'center';
        if (right) return 'right';
        if (left) return 'left';
        return '';
      });
      i += 2;
      const bodyRows = [];
      while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') !== -1) {
        if (isTableSeparator(lines[i])) break;
        bodyRows.push(splitTableRow(lines[i]));
        i += 1;
      }
      // 统一列数，避免表格错位
      const width = Math.max(header.length, aligns.length);
      for (let r = 0; r < bodyRows.length; r += 1) {
        while (bodyRows[r].length < width) bodyRows[r].push('');
      }
      root.appendChild(buildMarkdownTable(header, aligns, bodyRows));
      continue;
    }

    // 引用
    if (/^>\s?/.test(trimmed)) {
      closeList();
      const quoteLines = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^\s*>\s?/, ''));
        i += 1;
      }
      const quote = el('blockquote');
      quote.innerHTML = quoteLines.map((text) => renderInline(text)).join('<br>');
      root.appendChild(quote);
      continue;
    }

    // 列表
    const orderedMatch = /^(\s*)(\d{1,9})[.)]\s+(.*)$/.exec(line);
    const bulletMatch = /^(\s*)[-*+]\s+(.*)$/.exec(line);
    if (orderedMatch || bulletMatch) {
      const ordered = Boolean(orderedMatch);
      const content = ordered ? orderedMatch[3] : bulletMatch[2];
      const top = listStack[listStack.length - 1];
      if (!top || (ordered && top.tagName !== 'OL') || (!ordered && top.tagName !== 'UL')) {
        if (listStack.length && top && top.childElementCount) {
          // 同级列表类型切换：先关闭当前层
          listStack.pop();
        }
        openList(ordered);
      }
      const current = listStack[listStack.length - 1];
      const li = el('li');
      li.innerHTML = renderInline(content);
      current.appendChild(li);
      i += 1;
      continue;
    }

    // 段落
    closeList();
    const paragraphLines = [];
    while (i < lines.length) {
      const candidate = lines[i].trim();
      if (!candidate) break;
      if (/^(#{1,6})\s+/.test(candidate)) break;
      if (/^```/.test(candidate)) break;
      if (/^>\s?/.test(candidate)) break;
      if (/^(\s*)(\d{1,9})[.)]\s+/.test(lines[i])) break;
      if (/^(\s*)[-*+]\s+/.test(lines[i])) break;
      if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(lines[i])) break;
      if (candidate.indexOf('|') !== -1 && isTableSeparator(candidate)) break;
      paragraphLines.push(candidate);
      i += 1;
      if (i < lines.length && lines[i].indexOf('|') !== -1 && isTableSeparator(lines[i])) {
        // 下一行可能是表格分隔行，结束段落以便表格解析
        if (paragraphLines.length) { i -= 1; }
        break;
      }
    }
    if (!paragraphLines.length) { i += 1; continue; }
    const p = el('p');
    p.innerHTML = renderInline(paragraphLines.join(' '));
    root.appendChild(p);
  }

  closeList();
  return root;
}

/* --------------------------------------------------------------------------
 * 6. 顶栏健康状态
 * ------------------------------------------------------------------------ */
async function loadHealth() {
  const dot = $('health-dot');
  const text = $('health-text');
  try {
    const data = await getJson('/api/health');
    state.health = data;
    const ok = (data.status || 'ok') === 'ok';
    dot.className = 'dot ' + (ok ? 'dot-ok' : 'dot-bad');
    text.textContent = ok ? '后端服务正常' : ('后端状态：' + fmtText(data.status));
    $('health-llm').textContent = 'LLM：' + (data.llm_configured
      ? ('已配置' + (data.model ? '（' + data.model + '）' : ''))
      : '未配置（多 Agent 模式不可用）');
    const engines = data.engine_available || {};
    const names = Object.keys(engines).filter((key) => engines[key]);
    $('health-engine').textContent = '引擎：' + (names.length ? names.join(' / ') : '无可用引擎');
    $('health-version').textContent = '版本 ' + fmtText(data.version);
    if (data.llm_configured === false) {
      setRunHint('提示：后端未配置 LLM，「对话模式」与「多 Agent 协作」不可用，请使用参数模式下的「确定性流水线」。', 'warn');
    }
  } catch (error) {
    state.health = null;
    dot.className = 'dot dot-bad';
    text.textContent = '无法连接后端服务：' + shortError(error);
    $('health-llm').textContent = 'LLM：未知';
    $('health-engine').textContent = '引擎：未知';
    $('health-version').textContent = '版本 —';
  }
}

function setRunHint(message, kind) {
  const node = $('run-hint');
  node.textContent = message;
  node.style.color = kind === 'err' ? 'var(--err)' : (kind === 'warn' ? 'var(--warn)' : '');
}

/* run_id 以单行省略号展示，同时把完整值放到 title，避免长 ID 撑破面板 */
function setRunIdLabel(text) {
  const node = $('exec-run-id');
  if (!node) return;
  node.textContent = text;
  node.title = text;
}

/* --------------------------------------------------------------------------
 * 7.0 会话 id（多轮对话）
 *   同一 conversation_id = 服务端同一段对话（前端每次提交都把它放进请求体，
 *   服务端据此当 LangGraph 的 thread_id，从而命中上一轮的 checkpointer 记忆）。
 *   - 首次进入生成并写入 localStorage → 刷新/重开页面仍接着上一段聊；
 *   - 点「新对话」才生成新 id 并清空气泡；
 *   - 顶部标签显示短 id，便于排查。
 * ------------------------------------------------------------------------ */
const CONVERSATION_KEY = 'dsh_conversation_id';

function newConversationId() {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
  } catch (error) { /* 允许静默：旧浏览器没有 crypto.randomUUID */ }
  return 'conv-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

function loadConversationId() {
  try {
    const saved = (window.localStorage.getItem(CONVERSATION_KEY) || '').trim();
    if (saved) return saved;
  } catch (error) { /* 允许静默：隐私模式下 localStorage 不可用 */ }
  const created = newConversationId();
  saveConversationId(created);
  return created;
}

function saveConversationId(id) {
  try { window.localStorage.setItem(CONVERSATION_KEY, String(id || '')); }
  catch (error) { /* 允许静默：localStorage 不可用不影响本次会话 */ }
}

function shortConversationId(id) {
  const text = String(id || '');
  return text ? text.slice(0, 8) : '—';
}

/* 顶部会话短 id（与 run_id 并排，便于对照排查） */
function setConversationIdLabel() {
  const node = $('exec-conversation-id');
  if (!node) return;
  const id = state.conversationId || '';
  node.textContent = 'conv ' + shortConversationId(id);
  node.title = id ? ('会话 id（同一 id = 同一段对话）：' + id) : '会话 id —';
}

/* 开始新对话：清空气泡 + 生成新 id + 写入 localStorage（服务端不再延续上一轮） */
function startNewConversation() {
  if (state.running) {
    setRunHint('运行中无法开始新对话，请先等待当前运行结束或点击「停止」。', 'warn');
    return;
  }
  state.conversationId = newConversationId();
  saveConversationId(state.conversationId);
  clearChatHistory();
  setConversationIdLabel();
  setRunHint('已开始新对话（conv ' + shortConversationId(state.conversationId) +
    '）：气泡已清空，服务端不会延续上一段对话的上下文。');
}

/* --------------------------------------------------------------------------
 * 7. 受体与结合位点
 * ------------------------------------------------------------------------ */
function siteValues(site) {
  const center = (site && Array.isArray(site.center)) ? site.center : [];
  const size = (site && Array.isArray(site.size)) ? site.size : [];
  return {
    cx: toNumber(center[0]), cy: toNumber(center[1]), cz: toNumber(center[2]),
    sx: toNumber(size[0]), sy: toNumber(size[1]), sz: toNumber(size[2])
  };
}

function fillSite(receptor) {
  const site = (receptor && receptor.site) || {};
  const values = siteValues(site);
  const pairs = [
    ['center-x', values.cx], ['center-y', values.cy], ['center-z', values.cz],
    ['size-x', values.sx], ['size-y', values.sy], ['size-z', values.sz]
  ];
  pairs.forEach(([id, value]) => { $(id).value = value === null ? '' : String(value); });
  /* 来自注册表受体的自动填充：不算用户手填 */
  state.siteTouched = false;
  state.uploadSiteFilled = false;
}

function clearSite() {
  /* 清空位点盒 = 回到"自动定盒"：不再下发坐标 */
  ['center-x', 'center-y', 'center-z', 'size-x', 'size-y', 'size-z']
    .forEach((id) => state.touchedFields && state.touchedFields.delete(id));
  ['center-x', 'center-y', 'center-z', 'size-x', 'size-y', 'size-z'].forEach((id) => { $(id).value = ''; });
  state.siteTouched = false;
  state.uploadSiteFilled = false;
}

function currentReceptor() {
  const key = $('receptor-select').value;
  return state.receptors.find((item) => item.key === key) || null;
}

function renderReceptorInfo(receptor) {
  const box = $('receptor-info');
  clear(box);
  if (!receptor) {
    box.appendChild(el('p', 'site-empty', '尚未选择受体。'));
    $('site-hint').textContent = '中心/尺寸留空时使用该受体注册的已知结合位点。';
    return;
  }
  const site = receptor.site || {};
  const values = siteValues(site);
  const title = el('p', 'site-title', fmtText(receptor.name || receptor.key) +
    (receptor.pdb ? '（PDB ' + receptor.pdb + '）' : ''));
  box.appendChild(title);
  if (receptor.protein) box.appendChild(el('p', 'site-desc', receptor.protein));
  if (site.description) box.appendChild(el('p', 'site-desc', '结合位点：' + site.description));
  if (site.source) box.appendChild(el('p', 'site-desc', '来源：' + site.source));
  const centerText = [values.cx, values.cy, values.cz].map((v) => fmtNum(v, 2)).join(', ');
  const sizeText = [values.sx, values.sy, values.sz].map((v) => fmtNum(v, 2)).join(', ');
  box.appendChild(el('p', 'site-kv', 'center = [' + centerText + ']'));
  box.appendChild(el('p', 'site-kv', 'size = [' + sizeText + ']'));
  if (Array.isArray(site.residues) && site.residues.length) {
    box.appendChild(el('p', 'site-kv', '关键残基：' + site.residues.join(', ')));
  }
  if (receptor.available === false) {
    box.appendChild(el('p', 'site-desc', '⚠ 该受体的对接文件当前不可用。'));
  }
  $('site-hint').textContent = '已填入注册位点，可按需修改；留空则使用默认位点。';
}

async function loadReceptors() {
  const select = $('receptor-select');
  try {
    const data = await getJson('/api/receptors');
    state.receptors = Array.isArray(data.receptors) ? data.receptors : [];
    state.defaultReceptor = data.default || (state.receptors[0] && state.receptors[0].key) || '';
    clear(select);
    if (!state.receptors.length) {
      const option = el('option', null, '未找到可用受体');
      option.value = '';
      select.appendChild(option);
      renderReceptorInfo(null);
      setRunHint('后端未返回任何受体，请检查资产目录配置。', 'warn');
      return;
    }
    state.receptors.forEach((receptor) => {
      const option = el('option', null, fmtText(receptor.name || receptor.key) +
        (receptor.pdb ? '  [' + receptor.pdb + ']' : '') +
        (receptor.available === false ? '（不可用）' : ''));
      option.value = receptor.key;
      select.appendChild(option);
    });
    select.value = state.defaultReceptor;
    const active = currentReceptor();
    renderReceptorInfo(active);
    fillSite(active);
  } catch (error) {
    clear(select);
    const option = el('option', null, '受体加载失败');
    option.value = '';
    select.appendChild(option);
    renderReceptorInfo(null);
    setRunHint('加载受体列表失败：' + shortError(error), 'err');
  }
}

/* --------------------------------------------------------------------------
 * 8. 示例分子库
 * ------------------------------------------------------------------------ */
function renderLibraryPreview(library) {
  const box = $('library-preview');
  clear(box);
  if (!library) {
    box.appendChild(el('p', 'preview-empty', '请选择示例分子库。'));
    return;
  }
  const molecules = Array.isArray(library.molecules) ? library.molecules : [];
  box.appendChild(el('p', 'preview-empty', '共 ' + fmtInt(library.count !== undefined ? library.count : molecules.length) +
    ' 个分子' + (library.path ? ' · ' + library.path : '')));
  const list = el('ul', 'file-list');
  molecules.slice(0, 8).forEach((molecule) => {
    const li = el('li');
    li.appendChild(el('span', 'file-name', fmtText(molecule.name || '未命名')));
    li.appendChild(el('span', 'file-smiles', fmtText(molecule.smiles), molecule.smiles));
    list.appendChild(li);
  });
  box.appendChild(list);
  if (molecules.length > 8) {
    box.appendChild(el('p', 'preview-empty', '仅预览前 8 个，实际将使用库中全部分子。'));
  }
}

async function loadLibraries() {
  const select = $('library-select');
  try {
    const data = await getJson('/api/libraries');
    state.libraries = Array.isArray(data.libraries) ? data.libraries : [];
    const positive = data.positive_control || null;
    if (positive && positive.smiles) {
      /* v0.5：阳性对照为可选项，**默认留空**。
         这里只把示例对照缓存起来，由用户点击「填入示例对照（苯甲脒）」主动填入，
         绝不在初始化时自动写进输入框。 */
      state.examplePositive = {
        name: positive.name || '示例对照',
        smiles: positive.smiles
      };
      const input = $('positive-control');
      const button = $('btn-fill-positive');
      if (button) button.textContent = '填入示例对照（' + state.examplePositive.name + '）';
      if (input && !input.value.trim()) {
        input.placeholder = '留空则不进行阳性对照分析（示例：' + positive.smiles + '）';
      }
    } else {
      state.examplePositive = null;
    }
    clear(select);
    if (!state.libraries.length) {
      const option = el('option', null, '未找到示例分子库');
      option.value = '';
      select.appendChild(option);
      renderLibraryPreview(null);
      return;
    }
    state.libraries.forEach((library) => {
      const count = library.count !== undefined ? library.count : (library.molecules || []).length;
      const option = el('option', null, fmtText(library.name || library.id) + '（' + fmtInt(count) + ' 个分子）');
      option.value = library.id || library.path || '';
      select.appendChild(option);
    });
    select.value = state.libraries[0].id || state.libraries[0].path || '';
    renderLibraryPreview(state.libraries[0]);
  } catch (error) {
    clear(select);
    const option = el('option', null, '示例分子库加载失败');
    option.value = '';
    select.appendChild(option);
    renderLibraryPreview(null);
    setRunHint('加载示例分子库失败：' + shortError(error), 'err');
  }
}

/* --------------------------------------------------------------------------
 * 8.5 文件上传（POST /api/uploads，见 docs/api.md 9.1）
 *   状态机：空(empty) → 上传中(uploading) → 成功(ok) / 失败(error)
 *   - 小分子：返回 path，提交时作为 molecule_file 下发；
 *   - 受体：后端现场准备为 PDBQT 并标定位点盒，提交时作为 receptor_file 下发（优先于 receptor）。
 *   仅用原生 XMLHttpRequest 以获取上传进度，不引入任何外部依赖。
 * ------------------------------------------------------------------------ */

/** 上传接口的错误文案：兼容 FastAPI 的 {"detail": "..."} 与 422 的数组 detail */
function uploadErrorText(error) {
  const payload = error && error.payload;
  let detail = payload && payload.detail;
  if (Array.isArray(detail)) {
    detail = detail.map((item) => (item && (item.msg || item.message)) || '').filter(Boolean).join('；');
  } else if (detail && typeof detail === 'object') {
    detail = detail.message || detail.error || JSON.stringify(detail);
  }
  if (!detail) detail = (payload && (payload.error_message || payload.message)) || (error && error.message);
  return String(detail || '上传失败');
}

function fileExt(name) {
  const text = String(name || '');
  const dot = text.lastIndexOf('.');
  return dot === -1 ? '' : text.slice(dot + 1).toLowerCase();
}

function fileBaseName(path) {
  const parts = String(path || '').split(/[\\/]/);
  return parts[parts.length - 1] || String(path || '');
}

function uploadExText(kind) {
  const exts = kind === 'ligand' ? LIGAND_UPLOAD_EXTS : RECEPTOR_UPLOAD_EXTS;
  return exts.map((ext) => '.' + ext).join(' / ');
}

/** 原生 XHR 上传（fetch 无法获得上传进度）；返回解析后的 JSON */
function postUpload(file, kind, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('file', file);
    form.append('kind', kind === 'ligand' ? 'ligand' : 'receptor');
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/uploads', true);
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
    });
    xhr.addEventListener('load', () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (error) { data = null; }
      if (xhr.status >= 200 && xhr.status < 300 && data) { resolve(data); return; }
      const err = new Error(uploadErrorText({ payload: data, message: 'HTTP ' + xhr.status }));
      err.status = xhr.status;
      err.payload = data;
      reject(err);
    });
    xhr.addEventListener('error', () => reject(new TypeError('网络错误：无法连接上传接口')));
    xhr.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    xhr.send(form);
  });
}

function openFilePicker(kind) {
  const input = kind === 'ligand' ? $('ligand-file-input') : $('receptor-file-input');
  if (input) input.click();
}

/** 把上传状态渲染成「代码风」文件卡片（方括号状态标签 + 等宽文件名 + 细边框） */
function renderUploadCard(kind) {
  const host = kind === 'ligand' ? $('ligand-upload-status') : $('receptor-upload-status');
  if (!host) return;
  const slot = state.upload[kind];
  clear(host);
  host.classList.toggle('is-empty', slot.phase === 'empty');
  if (slot.phase === 'empty') return;

  const card = el('div', 'upload-card');
  const head = el('div', 'upload-card-head');
  const phaseTag = slot.phase === 'ok' ? 'tag-ok' : (slot.phase === 'uploading' ? 'tag-run' : 'tag-fail');
  const phaseText = slot.phase === 'ok' ? '[ 已上传 ]' : (slot.phase === 'uploading' ? '[ 上传中 ]' : '[ 上传失败 ]');
  head.appendChild(el('span', 'tag ' + phaseTag, phaseText));
  head.appendChild(el('span', 'upload-name mono', slot.fileName || '未命名文件', slot.fileName || ''));
  card.appendChild(head);

  if (slot.phase === 'uploading') {
    card.appendChild(el('p', 'upload-meta mono', '上传中… ' + fmtSize(slot.size)));
    const track = el('div', 'upload-track');
    const fill = el('div', 'upload-fill');
    fill.style.width = Math.max(0, Math.min(100, Math.round((slot.progress || 0) * 100))) + '%';
    track.appendChild(fill);
    card.appendChild(track);
    card.appendChild(el('p', 'upload-meta mono', Math.round((slot.progress || 0) * 100) + '%'));
    host.appendChild(card);
    return;
  }

  if (slot.phase === 'error') {
    card.appendChild(el('p', 'upload-error', slot.error || '上传失败'));
    const errRow = el('div', 'btn-row');
    const retry = el('button', 'btn btn-ghost btn-sm', '重新选择文件');
    retry.type = 'button';
    retry.addEventListener('click', () => openFilePicker(kind));
    errRow.appendChild(retry);
    const removeErr = el('button', 'btn btn-ghost btn-sm', '移除');
    removeErr.type = 'button';
    removeErr.addEventListener('click', () => removeUpload(kind));
    errRow.appendChild(removeErr);
    card.appendChild(errRow);
    host.appendChild(card);
    return;
  }

  const data = slot.data || {};
  const pending = data.pending === true;
  const metaBits = [fmtSize(data.size !== undefined ? data.size : slot.size)];
  if (pending) {
    metaBits.push('待运行（开始运行时才解析/准备）');
  } else if (kind === 'ligand') {
    if (data.format) metaBits.push('格式 ' + fmtText(data.format));
    const count = toNumber(data.count);
    metaBits.push('解析到 ' + (count === null ? '—' : fmtInt(count)) + ' 个分子');
  } else if (data.receptor_file) {
    metaBits.push('PDBQT ' + fileBaseName(data.receptor_file));
    const prot = data.receptor_protonation || {};
    if (prot.applied) metaBits.push('受体质子化 pH ' + fmtNum(prot.ph, 1));
  }
  const metaText = metaBits.join(' · ');
  card.appendChild(el('p', 'upload-meta mono', metaText, metaText));

  /* 校验按钮：只有用户点了才去解析/准备（不做隐式处理）。
     放在卡片最前面，方便"先看一眼再决定跑不跑"。 */
  const inspectRow = el('div', 'btn-row');
  const busy = slot.busy === true;
  const inspect = el('button', 'btn btn-ghost btn-sm',
    busy ? '校验中…' : (pending ? '校验文件（解析预览）' : '重新校验'));
  inspect.type = 'button';
  inspect.disabled = busy;
  inspect.addEventListener('click', () => inspectUpload(kind));
  inspectRow.appendChild(inspect);
  card.appendChild(inspectRow);
  if (data.relative_path || data.path) {
    const pathText = String(data.relative_path || data.path);
    card.appendChild(el('p', 'upload-path mono', pathText, String(data.path || data.relative_path)));
  }

  if (pending) {
    card.appendChild(el('p', 'upload-meta',
      '已保存到服务端，尚未解析：不会在后台占用 CPU；开始运行或点「校验文件」时才处理。'));
    const rowPending = el('div', 'btn-row');
    const removePending = el('button', 'btn btn-ghost btn-sm', '移除');
    removePending.type = 'button';
    removePending.addEventListener('click', () => removeUpload(kind));
    rowPending.appendChild(removePending);
    card.appendChild(rowPending);
    host.appendChild(card);
    return;
  }

  if (kind === 'ligand') {
    const molecules = Array.isArray(data.molecules) ? data.molecules : [];
    if (molecules.length) {
      const list = el('ul', 'file-list');
      molecules.slice(0, 5).forEach((molecule) => {
        const li = el('li');
        li.appendChild(el('span', 'file-name', fmtText(molecule.name || '未命名')));
        li.appendChild(el('span', 'file-smiles', fmtText(molecule.smiles), molecule.smiles));
        list.appendChild(li);
      });
      card.appendChild(list);
      const count = toNumber(data.count);
      if (count !== null && count > molecules.length) {
        card.appendChild(el('p', 'upload-meta', '仅预览前 ' + molecules.length +
          ' 个分子名，实际使用全部 ' + fmtInt(count) + ' 个。'));
      }
    }
  } else {
    const center = Array.isArray(data.box_center) ? data.box_center : null;
    const size = Array.isArray(data.box_size) ? data.box_size : null;
    if (center) {
      card.appendChild(el('p', 'upload-meta mono', 'center = [' + center.map((v) => fmtNum(v, 2)).join(', ') + ']'));
    }
    if (size) {
      card.appendChild(el('p', 'upload-meta mono', 'size = [' + size.map((v) => fmtNum(v, 2)).join(', ') + ']'));
    }
    if (data.protein) card.appendChild(el('p', 'upload-meta', '蛋白：' + fmtText(data.protein)));
    // 化学溯源：被剔除的金属/辅因子必须在**上传时就摆在眼前**，
    // 否则用户看到对接分数时根本不知道体系里少了血红素/锌。
    const droppedHet = data.dropped_hetatm && typeof data.dropped_hetatm === 'object'
      ? Object.keys(data.dropped_hetatm) : [];
    if (droppedHet.length) {
      const detail = droppedHet
        .sort((a, b) => toNumber(data.dropped_hetatm[b]) - toNumber(data.dropped_hetatm[a]))
        .map((k) => k + '×' + fmtInt(data.dropped_hetatm[k])).join('、');
      const p = el('p', 'upload-warn',
        '已剔除非水杂原子：' + detail + '（水 ' + fmtInt(data.dropped_waters || 0) + ' 个）'
        + '。金属/辅因子若对结合重要，结果只代表「去辅因子」体系 —— '
        + '可在对接时用 keep_hetatm 指定残基名保留后重跑。');
      card.appendChild(p);
    }
    const keptHet = data.kept_hetatm && typeof data.kept_hetatm === 'object'
      ? Object.keys(data.kept_hetatm) : [];
    if (keptHet.length) {
      card.appendChild(el('p', 'upload-meta',
        '已保留杂原子：' + keptHet.map((k) => k + '×' + fmtInt(data.kept_hetatm[k])).join('、')));
    }
    const unsupported = Array.isArray(data.unsupported_hetatm) ? data.unsupported_hetatm : [];
    if (unsupported.length) {
      card.appendChild(el('p', 'upload-warn',
        '以下残基缺少对接所需化学模板，未能保留：' + unsupported.join('、')
        + '（金属离子通常可直接保留；HEM/NAD 等大辅因子需提供模板）。'));
    } else if (data.chemistry_warning) {
      card.appendChild(el('p', 'upload-warn', fmtText(data.chemistry_warning)));
    }
    if (center || size) {
      const applyRow = el('div', 'btn-row');
      const apply = el('button', 'btn btn-ghost btn-sm', '填入上传位点');
      apply.type = 'button';
      apply.addEventListener('click', () => {
        fillSiteBox(center, size);
        state.siteTouched = false;
        state.uploadSiteFilled = true;
        /* 这是**用户显式点击**，算作"已指定位点盒"（未点则不下发，由口袋分析自动定盒） */
        ['center-x', 'center-y', 'center-z', 'size-x', 'size-y', 'size-z']
          .forEach((id) => state.touchedFields.add(id));
        setRunHint('已把上传受体的位点盒填入「中心 / 尺寸」字段。');
      });
      applyRow.appendChild(apply);
      card.appendChild(applyRow);
    }
  }

  const row = el('div', 'btn-row');
  const remove = el('button', 'btn btn-ghost btn-sm', '移除');
  remove.type = 'button';
  remove.addEventListener('click', () => removeUpload(kind));
  row.appendChild(remove);
  card.appendChild(row);
  host.appendChild(card);
}

/**
 * 用户主动「校验文件」：调用 POST /api/uploads/inspect 做解析/受体准备。
 * 上传接口本身**不做**任何解析（用户要求：开始运行时才处理），
 * 因此这里必须显式触发；跑完把结果写回 slot.data，卡片与位点盒随之更新。
 */
async function inspectUpload(kind) {
  const slot = state.upload[kind];
  const data = slot.data || {};
  if (!data.path) return;
  slot.busy = true;
  renderUploadCard(kind);
  setRunHint('正在校验 ' + (data.file_name || '文件') + '…（受体准备 / 分子解析可能需要几秒到几十秒）');
  try {
    const response = await fetch('/api/uploads/inspect', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: data.path, kind }),
    });
    let payload = null;
    try { payload = await response.json(); } catch (error) { payload = null; }
    if (!response.ok) {
      throw new Error(uploadErrorText({ payload, message: 'HTTP ' + response.status }));
    }
    slot.data = payload;
    slot.phase = 'ok';
    slot.busy = false;
    renderUploadCard(kind);
    if (kind === 'ligand') {
      const count = toNumber(payload.count);
      setRunHint('校验完成：解析到 ' + (count === null ? '—' : fmtInt(count)) +
        ' 个分子（开始运行时仍会重新读取该文件，保证与校验一致）。');
      updateConfigInfoBar();
    } else {
      applyUploadedSiteBox(payload);
      const prot = payload.receptor_protonation || {};
      setRunHint('校验完成：受体已准备为 PDBQT'
        + (prot.applied
            ? '，并按目标 pH ' + fmtNum(prot.ph, 1) + ' 处理了质子化（HIS '
              + Object.entries(prot.his_states || {}).filter(([, v]) => v)
                .map(([k, v]) => k + '×' + v).join('、') + '）'
            : '（未按目标 pH 处理：' + fmtText(prot.reason || '未知原因') + '）')
        + '；开始运行时按同样口径重新准备。');
    }
  } catch (error) {
    slot.phase = 'error';
    slot.busy = false;
    slot.error = uploadErrorText(error);
    renderUploadCard(kind);
    setRunHint((kind === 'ligand' ? '小分子库' : '受体文件') + '校验失败：' + slot.error, 'err');
  }
}

/** 上传进度按 1% 节流刷新，避免大文件时频繁重建 DOM */
function updateUploadProgress(kind) {
  const slot = state.upload[kind];
  const percent = Math.round((slot.progress || 0) * 100);
  if (slot.shownPercent === percent) return;
  slot.shownPercent = percent;
  renderUploadCard(kind);
}

/** 选择 / 拖拽后的统一上传入口 */
async function handleUploadFile(kind, file) {
  if (!file) return;
  const slot = state.upload[kind];
  const input = kind === 'ligand' ? $('ligand-file-input') : $('receptor-file-input');
  const exts = kind === 'ligand' ? LIGAND_UPLOAD_EXTS : RECEPTOR_UPLOAD_EXTS;
  const ext = fileExt(file.name);
  if (ext && exts.indexOf(ext) === -1) {
    setRunHint('文件扩展名 .' + ext + ' 不在推荐列表（' + uploadExText(kind) + '）；仍会尝试上传，失败时按后端返回提示处理。', 'warn');
  }
  if (file.size > UPLOAD_MAX_MB * 1024 * 1024) {
    setRunHint('文件约 ' + fmtSize(file.size) + '，超过后端默认上限 ' + UPLOAD_MAX_MB +
      'MB（UPLOAD_MAX_MB 可调）；前端不做截断，仍会上传，后端可能返回 413。', 'warn');
  }

  slot.phase = 'uploading';
  slot.fileName = file.name;
  slot.size = file.size;
  slot.progress = 0;
  slot.shownPercent = -1;
  slot.error = '';
  slot.seq += 1;
  const seq = slot.seq;
  renderUploadCard(kind);
  setRunHint('正在上传 ' + file.name + '（' + fmtSize(file.size) + '）…');

  try {
    const data = await postUpload(file, kind, (ratio) => {
      if (slot.seq !== seq) return;
      slot.progress = Math.max(0, Math.min(1, ratio));
      updateUploadProgress(kind);
    });
    if (slot.seq !== seq) return;
    slot.data = data;
    slot.phase = 'ok';
    slot.progress = 1;
    renderUploadCard(kind);
    if (kind === 'ligand') {
      setRunHint('已保存分子库文件 ' + fmtText(data.file_name || file.name) +
        '（' + fmtSize(data.size || file.size) + '）。**开始运行时**才会解析分子；' +
        '想先看解析结果，点文件卡片里的「校验文件」。');
      updateConfigInfoBar();
    } else {
      setRunHint('已保存受体文件 ' + fmtText(data.file_name || file.name) +
        '（' + fmtSize(data.size || file.size) + '）。**开始运行时**才会现场准备为 PDBQT、' +
        '按目标 pH 处理受体质子化并推定位点盒；想先看一眼位点与化学溯源，点「校验文件」。');
    }
  } catch (error) {
    if (slot.seq !== seq) return;
    slot.phase = 'error';
    slot.error = uploadErrorText(error);
    slot.shownPercent = -1;
    renderUploadCard(kind);
    setRunHint((kind === 'ligand' ? '小分子库' : '受体文件') + '上传失败：' + slot.error, 'err');
  } finally {
    if (input) input.value = '';
  }
}

/** 移除已上传文件：小分子切回「SMILES 文本」；受体回退到注册表受体 */
function removeUpload(kind) {
  const slot = state.upload[kind];
  slot.seq += 1;
  slot.phase = 'empty';
  slot.fileName = '';
  slot.size = 0;
  slot.progress = 0;
  slot.shownPercent = -1;
  slot.error = '';
  slot.data = null;
  slot.busy = false;
  renderUploadCard(kind);
  if (kind === 'ligand') {
    if (state.ligandSource === 'upload') setLigandSource('text');
    setRunHint('已移除上传的小分子文件，配体来源已切回「SMILES 文本」。');
    updateConfigInfoBar();
    return;
  }
  if (state.uploadSiteFilled && !state.siteTouched) {
    clearSite();
    state.uploadSiteFilled = false;
    setRunHint('已移除上传的受体文件，并清空由它自动填入的位点；运行将使用注册表受体。');
  } else {
    setRunHint('已移除上传的受体文件；你手填的位点保持不变，运行将使用注册表受体。');
  }
}

/** 绑定拖拽 / 点击 / 键盘（Enter、空格）三种选择方式 */
function setupDropzone(zone, input, kind) {
  if (!zone || !input) return;
  const stop = (event) => { event.preventDefault(); event.stopPropagation(); };
  zone.addEventListener('click', () => openFilePicker(kind));
  zone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openFilePicker(kind);
    }
  });
  ['dragenter', 'dragover'].forEach((name) => {
    zone.addEventListener(name, (event) => { stop(event); zone.classList.add('is-dragover'); });
  });
  ['dragleave', 'dragend'].forEach((name) => {
    zone.addEventListener(name, (event) => { stop(event); zone.classList.remove('is-dragover'); });
  });
  zone.addEventListener('drop', (event) => {
    stop(event);
    zone.classList.remove('is-dragover');
    const files = event.dataTransfer && event.dataTransfer.files;
    if (files && files.length) handleUploadFile(kind, files[0]);
  });
  input.addEventListener('change', () => {
    if (input.files && input.files.length) handleUploadFile(kind, input.files[0]);
  });
}

/** 位点字段是否有任意一个非空值 */
function siteFieldsFilled() {
  return ['center-x', 'center-y', 'center-z', 'size-x', 'size-y', 'size-z'].some((id) => {
    const node = $(id);
    return Boolean(node) && String(node.value).trim() !== '';
  });
}

/** 只写入上传返回的非空分量，避免缺少 size 时误清空用户已有值 */
function fillSiteBox(center, size) {
  const values = siteValues({ center: center || [], size: size || [] });
  [['center-x', values.cx], ['center-y', values.cy], ['center-z', values.cz],
    ['size-x', values.sx], ['size-y', values.sy], ['size-z', values.sz]]
    .forEach(([id, value]) => { if (value !== null) $(id).value = String(value); });
}

/**
 * 上传受体后自动填入位点盒：
 * - 位点字段为空（或此前就是本上传自动填入的）→ 直接覆盖填入；
 * - 用户已手填 → 绝不覆盖，只提示并提供卡片里的「填入上传位点」按钮。
 */
function applyUploadedSiteBox(data) {
  const center = Array.isArray(data.box_center) ? data.box_center : null;
  const size = Array.isArray(data.box_size) ? data.box_size : null;
  if (!center && !size) return;
  const pristine = !state.siteTouched && !siteFieldsFilled();
  if (pristine || (state.uploadSiteFilled && !state.siteTouched)) {
    fillSiteBox(center, size);
    state.uploadSiteFilled = true;
    setRunHint('已自动填入上传受体的位点盒（center / size），可按需修改。');
    return;
  }
  state.uploadSiteFilled = false;
  setRunHint('位点字段已有手填值，未覆盖为上传受体的位点盒；如需使用，请点上传卡片里的「填入上传位点」。', 'warn');
}

/* --------------------------------------------------------------------------
 * 9. 配体文本解析
 * ------------------------------------------------------------------------ */
function parseLigandsText(text) {
  const items = [];
  if (!text) return items;
  // 名称允许的字符（不含 SMILES 常见符号）
  const nameRe = /^[A-Za-z0-9_\-.\u4e00-\u9fff()\[\] ]{1,60}$/;
  const smilesLike = (value) => /[=#$@\\()[\]0-9]/.test(value) || /^[A-Za-z]{1,3}$/.test(value);

  const pushEntry = (entry) => {
    const raw = String(entry).trim();
    if (!raw) return;
    // 1) 名称:SMILES （中英文冒号）
    const colon = /^([^:：]{1,60}?)\s*[:：]\s*(\S.*)$/.exec(raw);
    if (colon && nameRe.test(colon[1].trim()) && !/[=#$@+\\]/.test(colon[1])) {
      items.push({ name: colon[1].trim(), smiles: colon[2].trim() });
      return;
    }
    // 2) 名称,SMILES （英文/中文逗号，仅当前半段是合法名称时）
    const comma = /^([^,，]{1,60}?)\s*[,，]\s*(\S.*)$/.exec(raw);
    if (comma && nameRe.test(comma[1].trim()) && !/[=#$@+\\]/.test(comma[1]) && smilesLike(comma[2])) {
      items.push({ name: comma[1].trim(), smiles: comma[2].trim() });
      return;
    }
    // 3) 纯 SMILES；若含中文逗号则视为分隔符
    if (/[,，]/.test(raw) && !/[=#$@\\]/.test(raw)) {
      raw.split(/[,，]\s*/).forEach((piece) => pushEntry(piece));
      return;
    }
    items.push({ name: '', smiles: raw });
  };

  String(text).split(/\r?\n/).forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    pushEntry(trimmed);
  });
  return items;
}

/* --------------------------------------------------------------------------
 * 9.5 大库规模预估与信息条（>500 个分子时建议先做小样本验证，但不阻止运行）
 * ------------------------------------------------------------------------ */

/** 根据当前配体来源估算本次分子数（无法预估时返回 0） */
function estimatedMoleculeCount() {
  if (state.ligandSource === 'library') {
    const library = state.libraries.find((item) =>
      (item.id || item.path) === $('library-select').value);
    if (!library) return 0;
    const count = toNumber(library.count);
    if (count !== null) return Math.max(0, Math.round(count));
    return Array.isArray(library.molecules) ? library.molecules.length : 0;
  }
  if (state.ligandSource === 'upload') {
    const data = state.upload.ligand.data;
    const count = data ? toNumber(data.count) : null;
    return count === null ? 0 : Math.max(0, Math.round(count));
  }
  if (state.ligandSource === 'text') {
    return parseLigandsText($('ligands-text').value).length;
  }
  return 0;
}

/** 切换配体来源（文本 / 上传文件 / 服务端路径 / 示例库），并同步四个来源面板 */
function setLigandSource(source) {
  const allowed = ['text', 'upload', 'file', 'library'];
  const next = allowed.indexOf(source) === -1 ? 'text' : source;
  state.ligandSource = next;
  document.querySelectorAll('#ligand-source .seg-btn').forEach((button) => {
    button.classList.toggle('active', button.dataset.source === next);
  });
  $('pane-text').classList.toggle('hidden', next !== 'text');
  $('pane-upload').classList.toggle('hidden', next !== 'upload');
  $('pane-file').classList.toggle('hidden', next !== 'file');
  $('pane-library').classList.toggle('hidden', next !== 'library');
  updateConfigInfoBar();
}

/**
 * 更新配置区的信息条与 state.estimatedCount。
 * 分子库或粘贴文本超过 500 个分子时给出「先用最大分子数做小样本验证」的建议；
 * 仅提示，不阻止运行。
 */
function updateConfigInfoBar() {
  const bar = $('config-info-bar');
  const count = estimatedMoleculeCount();
  const maxLigands = toNumber($('max-ligands').value) || 0;
  const effective = maxLigands > 0 ? Math.min(count, maxLigands) : count;
  state.estimatedCount = effective;
  if (!bar) return;
  if (count > LARGE_LIBRARY_THRESHOLD) {
    const capped = maxLigands > 0
      ? '；当前「最大分子数」= ' + fmtInt(maxLigands) + '，本次最多对接 ' + fmtInt(effective) + ' 个'
      : '；当前未限制分子数';
    bar.textContent = '本次约 ' + fmtInt(count) + ' 个分子（超过 500，规模较大）：' +
      '建议先在「最大分子数」填一个较小值（如 200）做小样本验证，再放开全量运行' + capped +
      '。该提示不会阻止运行；运行中会持续显示「已完成 / 总数」与剩余时间。';
    show(bar);
  } else if (count > 0 && maxLigands > 0 && effective < count) {
    bar.textContent = '本次最多对接 ' + fmtInt(effective) + ' 个分子（共 ' + fmtInt(count) +
      ' 个，已由「最大分子数」限制）。';
    show(bar);
  } else {
    hide(bar);
    bar.textContent = '';
  }
}

/* --------------------------------------------------------------------------
 * 10. 事件（SSE）处理
 * ------------------------------------------------------------------------ */
/* 日志行：行首统一「[时间戳] 提示符 内容」，提示符 › 常规 / $ 指令 / ! 错误 */
function logLine(message, kind) {
  const box = $('log-box');
  const placeholder = box.querySelector('.empty');
  if (placeholder) placeholder.remove();
  const cls = kind === 'err' ? 'log-err'
    : (kind === 'stage' ? 'log-stage' : (kind === 'cmd' ? 'log-cmd' : 'log-info'));
  const line = el('p', cls);
  const prompt = kind === 'err' ? '!' : (kind === 'cmd' ? '$' : '›');
  line.appendChild(el('span', 'log-time', '[' + nowStamp() + ']'));
  line.appendChild(el('span', 'log-prompt', ' ' + prompt + ' '));
  line.appendChild(document.createTextNode(String(message)));
  if (!prefersReducedMotion()) line.classList.add('log-enter');
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
  while (box.childElementCount > 600) box.removeChild(box.firstChild);
  /* 动画类只保留一帧：避免长列表里每行都常驻 will-change，滚动手感变差 */
  if (line.classList.contains('log-enter')) {
    window.setTimeout(() => line.classList.remove('log-enter'), 260);
  }
}

/** 运行日志里的「受体」一行：必须如实反映用户到底指定了什么。
 *
 * 真实缺陷：AgentRequest.receptor 有 schema 默认值 "thrombin"，用户什么都没指定时
 * 请求体里也带着 receptor="thrombin"，前端照抄 request.receptor 就会打印
 * 「开始运行 · 受体 thrombin」，看起来像用户主动选了凝血酶。
 * 因此这里以受理层（task_spec.receptor）为准：
 *   - 有上传受体文件 → 打「受体文件 <文件名>」；
 *   - source=user    → 打用户指定的受体名；
 *   - source=named/unresolved → 如实标注「待在线解析」；
 *   - source=default（或没有 task_spec 的流水线模式，表单已强制选受体）→ 未指定就明说。
 */
function receptorLogText(request, taskSpec) {
  const req = request || {};
  const spec = (taskSpec && taskSpec.receptor) || {};
  const uploaded = req.receptor_file || spec.file || '';
  if (uploaded) return '受体文件 ' + fileBaseName(uploaded);
  const source = spec.source || '';
  const name = spec.name || req.receptor || '';
  if (source === 'user') return name ? ('受体 ' + name) : '用户指定受体（未给出名称）';
  if (source === 'named' || source === 'unresolved') {
    return name ? ('受体 ' + name + '（待在线解析，来源 ' + source + '）') : '受体来源待解析';
  }
  if (source === 'default') return '未指定受体（回退默认 ' + (name || 'thrombin') + '）';
  /* 没有 task_spec = 参数模式流水线：表单强制选了受体，request.receptor 即用户选择 */
  if (req.receptor) return '受体 ' + req.receptor;
  return '未指定受体（回退默认 thrombin）';
}

function handleStartEvent(data) {
  if (data.run_id) {
    state.runId = data.run_id;
    setRunIdLabel('run_id ' + data.run_id);
  }
  state.cancelled = false;
  const request = data.request || {};
  const receptorText = receptorLogText(request, data.task_spec);
  logLine('开始运行 · ' + receptorText +
    (request.engine ? (' · 引擎 ' + fmtText(request.engine)) : ''), 'stage');
  /* 编排：run_id 到手 = 协调 Agent 开始分发任务 */
  orchStageAdvance('coordinator');
  setRunHint('运行中…可随时点击「停止」。');
}

function handleStageEvent(data) {
  const stage = fmtText(data.stage);
  const message = fmtText(data.message);
  const index = toNumber(data.index);
  const total = toNumber(data.total);
  if (stage && stage !== state.progress.stage) {
    // 进入新阶段：上一阶段的 ETA 已失效，先清空避免误导
    state.progress.stage = stage;
    state.serverElapsed = null;
    $('progress-eta').textContent = '剩余时间 —';
  }
  $('stage-current').textContent = '阶段：' + stage + ' — ' + message;
  if (index !== null && total !== null) {
    $('stage-counter').textContent = fmtInt(index) + ' / ' + fmtInt(total);
    state.progress.index = index;
    state.progress.total = total;
    const percent = total > 0 ? Math.min(96, Math.round((index / total) * 100)) : 0;
    setProgress(percent);
    updateProgressDone();
  }
  logLine('【' + stage + '】' + message, 'stage');
  /* 编排：确定性流水线的 stage 事件 → 对应节点 */
  const mappedNode = ORCH_STAGE_NODE[String(data.stage || '').trim().toLowerCase()] || '';
  orchStageAdvance(mappedNode, Boolean(data.skipped));
  if (state.orch.stageNode && state.orch.stageNode !== mappedNode) {
    orchMarkEnd(state.orch.stageNode, data);   // 上一阶段到此结束（实测串行链）
  }
  if (mappedNode && data.skipped !== true) {
    orchMarkStart(mappedNode, 'stage:' + String(data.stage || ''), data);
    state.orch.stageNode = mappedNode;
  }
  renderProgressBarText();
}

function setProgress(percent) {
  const value = Math.max(0, Math.min(100, Math.round(percent)));
  state.progress.percent = value;
  $('progress-fill').style.width = value + '%';
  $('progress-percent').textContent = value + '%';
  renderProgressBarText();
  renderOrchMetrics();
}

/** 统一的「已完成 done/total（percent%）」显示 */
function updateProgressDone() {
  const done = state.progress.index;
  const total = state.progress.total;
  const node = $('progress-done');
  if (!node) return;
  node.textContent = '已完成 ' + fmtInt(done) + '/' + fmtInt(total) +
    '（' + state.progress.percent + '%）';
}

/** 已用时间：本地计时器优先，服务端 progress.elapsed_sec 到达后用较大值校正 */
function updateElapsed() {
  const node = $('exec-elapsed');
  if (node) node.textContent = '用时 ' + fmtDuration(elapsedSeconds());
  renderProgressBarText();
  renderOrchMetrics();
}

/**
 * v0.4 新增：progress 事件（≈1s 或每批一次）
 * {"type":"progress","stage":"docking","done":1200,"total":10000,"percent":12.0,
 *  "elapsed_sec":34.5,"eta_sec":251.0,"message":"已完成 1200/10000"}
 */
function handleProgressEvent(data) {
  const done = toNumber(pick(data, ['done', 'index', 'completed']));
  const total = toNumber(pick(data, ['total']));
  const stage = pick(data, ['stage'], '') || '';
  const message = pick(data, ['message'], '') || '';

  if (done !== null) state.progress.index = done;
  if (total !== null) state.progress.total = total;
  let percent = toNumber(pick(data, ['percent']));
  if (percent === null && state.progress.total > 0) {
    percent = (state.progress.index / state.progress.total) * 100;
  }
  if (percent !== null) setProgress(percent);

  const elapsed = toNumber(pick(data, ['elapsed_sec']));
  if (elapsed !== null) state.serverElapsed = { sec: elapsed, at: Date.now() };
  const eta = toNumber(pick(data, ['eta_sec']));
  state.etaSec = (eta !== null && eta >= 0) ? eta : null;

  if (stage) state.progress.stage = stage;
  $('stage-current').textContent = '阶段：' + fmtText(stage) +
    (message ? ' — ' + message : '');
  if (state.progress.total > 0) {
    $('stage-counter').textContent = fmtInt(state.progress.index) + ' / ' + fmtInt(state.progress.total);
  }
  updateProgressDone();
  $('progress-eta').textContent = (eta !== null && eta >= 0)
    ? ('剩余约 ' + fmtDuration(eta))
    : '剩余时间 —';
  /* 编排：progress.stage 同样映射到节点，并刷新「当前任务」计数 */
  orchStageAdvance(ORCH_STAGE_NODE[String(stage || '').trim().toLowerCase()] || '');
  updateElapsed();
  updateOrchTask();
  renderProgressBarText();
  renderOrchMetrics();

  // 进度日志按 10% 粒度记录，避免每秒一条刷屏
  const decile = Math.floor(state.progress.percent / 10);
  if (decile > state.progress.lastDecile) {
    state.progress.lastDecile = decile;
    logLine('进度：已完成 ' + fmtInt(state.progress.index) + '/' + fmtInt(state.progress.total) +
      '（' + state.progress.percent + '%）' +
      (eta !== null && eta >= 0 ? ' · 剩余约 ' + fmtDuration(eta) : ''), 'stage');
  }
}

function affinityClass(value) {
  const number = toNumber(value);
  if (number === null) return 'aff-none';
  if (number <= -9) return 'aff-strong';
  if (number <= -8) return 'aff-good';
  if (number <= -6) return 'aff-mid';
  return 'aff-weak';
}


function moleculeRowCells(molecule, index) {
  const cells = [];
  cells.push(el('td', 'num', String(index)));
  cells.push(el('td', 'name-cell', fmtText(molecule.name), molecule.name));

  const affinityCell = el('td', 'num aff-cell ' + affinityClass(molecule.affinity));
  affinityCell.textContent = fmtNum(molecule.affinity, 2);
  cells.push(affinityCell);

  cells.push(el('td', null, fmtText(molecule.engine)));
  cells.push(el('td', 'num', fmtText(molecule.exhaustiveness)));
  cells.push(el('td', 'num', fmtNum(molecule.mw, 2)));
  cells.push(el('td', 'num', fmtNum(molecule.logp, 2)));
  cells.push(el('td', 'num', fmtNum(molecule.tpsa, 2)));
  cells.push(el('td', 'num', fmtNum(molecule.similarity, 3)));
  return cells;
}

/**
 * 实时表批量插入：DocumentFragment 一次成型，且只保留最近 LIVE_ROW_LIMIT 行。
 * 事件回调里只做入队（createThrottled 以 rAF + 间隔做节流），绝不逐条改 DOM。
 */
const flushMolecules = createThrottled((batch) => {
  if (!batch.length) return;
  const tbody = $('molecules-tbody');
  const fragment = document.createDocumentFragment();
  batch.forEach((molecule, offset) => {
    const fallback = state.liveCount - batch.length + offset + 1;
    const displayIndex = (molecule.index !== null && molecule.index !== undefined)
      ? molecule.index + 1
      : fallback;
    const tr = el('tr');
    moleculeRowCells(molecule, displayIndex).forEach((cell) => tr.appendChild(cell));
    fragment.appendChild(tr);
  });
  tbody.appendChild(fragment);
  while (tbody.childElementCount > LIVE_ROW_LIMIT) tbody.removeChild(tbody.firstChild);
  $('live-count').textContent = String(state.liveCount);
  hide($('molecules-empty'));
}, 150);

/** 实时表表头提示：明确只保留最近 N 条，完整结果在结果总览分页查看 */
function setLiveNote(total) {
  const note = $('live-note');
  if (!note) return;
  const count = toNumber(total);
  const suffix = (count !== null && count > 0) ? '（本次共 ' + fmtInt(count) + ' 个分子）' : '';
  note.textContent = '实时视图仅显示最近 ' + LIVE_ROW_LIMIT + ' 条' + suffix +
    '，完整结果见「结果总览」';
}

function handleMoleculeEvent(data) {
  const molecule = extractMolecule(data);
  state.liveCount += 1;
  flushMolecules(molecule);
  /* 编排：收到逐分子结果说明 Docking 执行节点正在工作 */
  orchStageAdvance('docking');
}

/**
 * v0.4 新增：批量逐分子事件
 * {"type":"molecules","items":[{...与 molecule 完全同构...}, ...]}
 * 服务端大库时按 ~200ms / 每 25 条合并；同时兼容单条 {"type":"molecule",...}。
 */
function handleMoleculesEvent(data) {
  const items = Array.isArray(data.items) ? data.items
    : (Array.isArray(data.molecules) ? data.molecules
      : (Array.isArray(data.results) ? data.results : []));
  if (!items.length) return;
  items.forEach((item) => handleMoleculeEvent(item));
}

const flushToken = createThrottled(() => {
  const pending = state.tokenBuffer;
  state.tokenBuffer = '';
  if (!pending) return;
  const text = state.tokenRendered + pending;
  if (text.length > 40000) {
    state.tokenRendered = '…（早期输出已省略）\n' + text.slice(-30000);
  } else {
    state.tokenRendered = text;
  }
  // 对话模式：增量文本直接渲染进助手气泡；参数模式：渲染到「模型增量文本」面板
  if (state.page === 'chat' && state.chatActiveId) {
    setChatText(state.chatActiveId, state.tokenRendered);
    return;
  }
  const box = $('token-stream');
  box.textContent = state.tokenRendered;
  box.scrollTop = box.scrollHeight;
}, 120);

function handleTokenEvent(data) {
  const piece = pick(data, ['content', 'token', 'text', 'delta'], '');
  if (!piece) return;
  state.tokenBuffer += String(piece);
  flushToken();
}

function appendToolItem(kind, name, content) {
  const box = $('tool-trace');
  const placeholder = box.querySelector('.empty');
  if (placeholder) placeholder.remove();
  const item = el('div', 'tool-item kind-' + kind);
  const head = el('div', 'tool-head');
  head.appendChild(el('span', 'tool-name', fmtText(name)));
  head.appendChild(el('span', null, kind === 'call' ? '调用' : (kind === 'result' ? '返回' : '节点更新')));
  head.appendChild(el('span', null, nowStamp()));
  item.appendChild(head);
  if (content) {
    const body = el('div', 'tool-body');
    body.textContent = String(content).slice(0, 4000);
    body.title = body.textContent;
    item.appendChild(body);
  }
  box.appendChild(item);
  box.scrollTop = box.scrollHeight;
  while (box.childElementCount > 300) box.removeChild(box.firstChild);
  /* 这里**不再**往对话气泡镜像「调用工具 / 工具返回 / 节点更新」：
     工具调用轨迹看右侧「工具轨迹」面板、阶段进展看「阶段日志」，对话流只留用户与助手的往来
     （运行级状态如「运行完成 · run_id …」仍由 appendChatNote 单独写入，见 startRun 收尾）。*/
}

function handleToolCallEvent(data) {
  const name = pick(data, ['name', 'tool', 'tool_name'], '未知工具');
  const args = pick(data, ['arguments', 'args', 'input'], '');
  const rendered = typeof args === 'string' ? args : (args ? JSON.stringify(args) : '');
  appendToolItem('call', name, rendered);
  /* 编排：多 Agent 模式的 tool_call → 对应节点进入运行中 */
  const node = orchToolNode(name);
  orchStageAdvance(node);
  orchMarkStart(node, String(name), data);   // 记录真实开始时间（用于并行/串行判定）
}

function handleToolResultEvent(data) {
  const name = pick(data, ['name', 'tool', 'tool_name'], '未知工具');
  appendToolItem('result', name, pick(data, ['content', 'result', 'output'], ''));
  /* 编排：工具返回后该节点视为完成（后续若再次调用会重新进入运行中） */
  const node = orchToolNode(name);
  orchMarkEnd(node, data);                   // 记录真实结束时间
  if (node && state.orch[node] === 'run') setOrchNode(node, 'ok');
  updateOrchTask();
}

function handleUpdateEvent(data) {
  const node = pick(data, ['node', 'name', 'content'], '');
  appendToolItem('update', node || '节点', pick(data, ['message', 'content'], ''));
}

function showError(message) {
  const box = $('exec-error');
  box.textContent = message;
  if (state.page === 'chat') {
    showChatError(String(message));
    const bubbleId = state.chatActiveId || appendChatMessage('assistant', '', 'error');
    setChatText(bubbleId, String(message));
    setChatStatus(bubbleId, '失败');
  }
  show(box);
}

function hideError() {
  hide($('exec-error'));
  $('exec-error').textContent = '';
  const chatBox = $('chat-error');
  if (chatBox) {
    hide(chatBox);
    chatBox.textContent = '';
  }
}

async function handleEvent(data) {
  state.lastEventAt = Date.now();
  const type = data && data.type ? data.type : '';

  switch (type) {
    case 'start':
      handleStartEvent(data);
      break;
    case 'stage':
      handleStageEvent(data);
      break;
    case 'progress':
      handleProgressEvent(data);
      break;
    case 'molecule':
      handleMoleculeEvent(data);
      break;
    case 'molecules':
      handleMoleculesEvent(data);
      break;
    case 'token':
      handleTokenEvent(data);
      break;
    case 'tool_call':
      handleToolCallEvent(data);
      break;
    case 'tool_result':
      handleToolResultEvent(data);
      break;
    case 'update':
      handleUpdateEvent(data);
      break;
    case 'choices':
      handleChoicesEvent(data);
      break;
    case 'final': {
      const content = pick(data, ['content', 'report_markdown', 'markdown'], '');
      if (content) {
        state.reportMarkdown = String(content);
        renderReport(state.reportMarkdown);
        // 对话模式：助手气泡在收到 final 后由 Markdown 渲染器重排
        if (state.page === 'chat') {
          const bubbleId = state.chatActiveId || appendChatMessage('assistant', '', 'final');
          setChatMarkdown(bubbleId, state.reportMarkdown);
          setChatStatus(bubbleId, state.cancelled ? '已取消' : '已完成');
        }
        // 参数模式的多 Agent 报告仍在「报告」页签呈现
        if (state.page !== 'chat') switchTab('report');
        logLine('协调 Agent 已出具完整报告（Markdown）。', 'stage');
        // 编排：产出报告 = 所有已运行节点收敛为完成
        if (!state.cancelled) orchSettle('ok');
      }
      break;
    }
    case 'cancelled': {
      /* v0.5：后端已真正停止对接进程池 */
      state.cancelled = true;
      state.stopping = false;
      clearStopTimer();
      const message = data.message || '运行已被用户取消';
      logLine('已取消：' + message, 'cmd');
      setRunHint('运行已取消。已完成的部分中间数据仍会落盘，可在「历史运行 / 中间数据」查看。', 'warn');
      if (data.summary) state.summary = data.summary;
      orchCancel();
      renderProgressBarText();
      if (state.page === 'chat' && state.chatActiveId) setChatStatus(state.chatActiveId, '已取消');
      break;
    }
    case 'done': {
      state.summary = data.summary || null;
      const summary = data.summary || {};
      const status = String(summary.status || '');
      const cancelled = state.cancelled || status === 'cancelled';
      state.cancelled = cancelled;
      const summaryTotal = toNumber(pick(summary, ['molecule_count']));
      if (cancelled) {
        // 取消后的 done 绝不能显示为成功
        orchCancel();
        logLine('运行已取消' + (data.run_id ? ' · ' + data.run_id : '') +
          (summary.error ? '（' + summary.error + '）' : '') + '。', 'cmd');
        setRunHint('运行已取消。已完成的部分结果仍可通过「历史运行 / 中间数据」查看。', 'warn');
        renderProgressBarText();
      } else {
        orchSettle('ok');
        /* 状态 no_op = 受理层判定本次指令不是可执行任务、零工具调用：如实说「未执行计算」，
           不要说成「运行完成」（否则用户会以为跑了一次筛选）。 */
        if (status === 'no_op') {
          logLine('本次未执行计算' + (data.run_id ? ' · ' + data.run_id : '') +
            '（受理层未受理该指令，未调用任何工具）', 'cmd');
        } else {
          logLine('运行完成' + (data.run_id ? ' · ' + data.run_id : '') + '。', 'stage');
        }
        if (state.progress.total === 0 && summaryTotal !== null) {
          // 没有收到 progress/分子事件时，用 summary 的分子数兜底，保证进度显示完整
          state.progress.total = summaryTotal;
        }
        if (state.progress.total > 0) state.progress.index = state.progress.total;
        state.etaSec = 0;
        setProgress(100);
        $('progress-eta').textContent = '剩余 0 秒（已完成）';
      }
      if (state.progress.total === 0 && summaryTotal !== null) state.progress.total = summaryTotal;
      updateProgressDone();
      updateOrchTask();
      renderOrchMetrics();
      setLiveNote(summaryTotal !== null ? summaryTotal : state.progress.total);
      if (data.summary) renderSummary(data.summary, state.ranking.total || summaryTotal || state.liveCount);
      break;
    }
    case 'error': {
      const code = data.error_code ? '[' + data.error_code + '] ' : '';
      const message = data.error_message || data.message || '后端执行失败';
      showError('执行失败：' + code + message +
        (data.stack_trace ? '\n' + String(data.stack_trace).slice(0, 800) : ''));
      logLine('错误：' + code + message, 'err');
      if (!state.cancelled) orchFail(state.orch.current);
      break;
    }
    case 'parse_error':
      logLine('收到无法解析的事件数据（已忽略）：' + String(data.raw || '').slice(0, 200), 'err');
      break;
    default:
      if (data && (data.error_message || data.error)) {
        showError('后端返回错误：' + (data.error_message || data.error));
      }
      break;
  }
}

/* --------------------------------------------------------------------------
 * 10.5 对话历史区（用户气泡 + 助手气泡）
 *      - 助手气泡在流式阶段显示增量文本，收到 final 后改用 Markdown 渲染器重排
 *      - 参数模式下同样记录「参数 → 提交 → 工具调用 → 报告」链路，便于对照
 * ------------------------------------------------------------------------ */
function chatHistoryNode() {
  return $('chat-history');
}

function appendChatMessage(role, text, status, chips, advanced, attachments) {
  const id = 'chat-' + (++state.chatSeq);
  state.chatMessages.push({
    id: id,
    role: role,
    text: text || '',
    status: status || '',
    chips: Array.isArray(chips) ? chips.slice() : [],
    /* 附件 chip：只用于**展示**（文件名 + R/L + 悬停看完整路径）。
       传输用的 message 里仍保留带绝对路径的「引用文件」清单（显示与传输分离）。 */
    attachments: Array.isArray(attachments) ? attachments.slice() : [],
    advanced: Boolean(advanced),
    notes: [],
    choices: [],
    choiceNote: ''
  });
  renderChatHistory();
  return id;
}

/* --------------------------------------------------------------------------
 * 对话气泡展示：显示与传输分离
 *   - 发给服务端的 message 里必须保留「引用文件（…）：- 名称 → /abs/path」清单
 *     （模型要靠绝对路径调工具）；
 *   - 但用户气泡只能显示用户自己输入的文字 + 紧凑附件 chip，绝不能把那份长清单
 *     和绝对路径排进正文（否则气泡越来越长，对话被挤没）。
 * ------------------------------------------------------------------------ */

/** 从展示文本里剥掉机器拼接的「引用文件」清单（纯函数，便于单测） */
function stripFileRefs(text) {
  return String(text || '')
    .replace(/\n*引用文件（[^）]*）：[\s\S]*$/, '')
    .replace(/\s+$/, '');
}

/** 本次实际写进指令的附件清单（@ 命中的优先，否则全部）——与 composeChatMessage 同源 */
function chatMessageRefs(raw, attachments) {
  const list = Array.isArray(attachments) ? attachments : state.attachments;
  const mentioned = mentionedAttachments(raw, list);
  return (mentioned.length ? mentioned : list).map((a) => ({
    name: a.name, kind: a.kind, path: a.path, count: a.count
  }));
}

/**
 * 剥掉正文里的裸 URL（报告规范：不得出现网址）。
 * - **保留** Markdown 图片 `![alt](path)`：图表要能直接显示（这是唯一允许的链接形态）；
 * - `[文字](url)` → 只留文字；
 * - 其余 `http(s)://…` 一律删除，并清理因此变空/只剩冒号的行与多余空行。
 * 纯函数，便于单测（见 scripts/ui_e2e.js）。
 */
function stripBareUrls(markdown) {
  const images = [];
  let text = String(markdown || '').replace(/!\[[^\]]*\]\([^)\s]+\)/g, (match) => {
    images.push(match);
    return '\u0000IMGKEEP' + (images.length - 1) + '\u0000';
  });
  const lines = text.split('\n');
  const kept = [];
  lines.forEach((line) => {
    const hadUrl = /https?:\/\//.test(line);
    let next = line
      .replace(/\[([^\]]*)\]\([^)\s]+\)/g, '$1')
      .replace(/<?https?:\/\/[^\s)>,，。；;\]]+>?/g, '')
      .replace(/[ \t]+([：:，,、])/g, '$1')
      .replace(/[ \t]+$/g, '');
    // 整行只是为了给一个链接/网址做标注（例如「图表：」）→ 连标签一起删掉
    if (hadUrl && !/[\u4e00-\u9fffA-Za-z0-9]/.test(next.replace(/[|\s\-*：:：]/g, ''))) {
      return;
    }
    kept.push(next);
  });
  text = kept.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  return text.replace(/\u0000IMGKEEP(\d+)\u0000/g, (match, i) => images[Number(i)]);
}

/* 长内容折叠：超过阈值才显示「展开 / 收起」，避免一屏被长工具回执占满 */
const CHAT_FOLD_LINES = 24;
const CHAT_FOLD_CHARS = 1800;

/** 纯函数：文本是否需要折叠（便于单测） */
function chatNeedsFold(text) {
  const value = String(text || '');
  if (value.length > CHAT_FOLD_CHARS) return true;
  return value.split(/\r?\n/).length > CHAT_FOLD_LINES;
}

function makeChatFoldToggle(bubble, body) {
  const button = el('button', 'chat-fold-toggle', '展开');
  button.type = 'button';
  button.setAttribute('aria-expanded', 'false');
  button.addEventListener('click', () => {
    const collapsed = body.classList.toggle('chat-fold');
    button.textContent = collapsed ? '展开' : '收起';
    button.setAttribute('aria-expanded', String(!collapsed));
  });
  bubble.appendChild(button);
  return button;
}

/** 流式增长时同步折叠状态（正文在 setChatText/setChatMarkdown 里被就地替换） */
function syncChatFold(messageId) {
  const message = findChatMessage(messageId);
  const box = chatHistoryNode();
  if (!message || !box) return;
  const bubble = box.querySelector('[data-chat-bubble="' + messageId + '"]');
  const body = box.querySelector('[data-chat-body="' + messageId + '"]');
  if (!bubble || !body) return;
  const existing = bubble.querySelector('.chat-fold-toggle');
  const need = chatNeedsFold(message.text);
  if (need && !existing) {
    body.classList.add('chat-fold');
    makeChatFoldToggle(bubble, body);
  } else if (!need && existing) {
    existing.remove();
    body.classList.remove('chat-fold');
  }
}

function appendChatNote(note) {
  const message = state.chatActiveId ? findChatMessage(state.chatActiveId) : null;
  if (!message) return;
  message.notes.push(note);
  if (message.notes.length > 80) message.notes.shift();
  renderChatHistory();
}

function findChatMessage(id) {
  for (let i = state.chatMessages.length - 1; i >= 0; i -= 1) {
    if (state.chatMessages[i].id === id) return state.chatMessages[i];
  }
  return null;
}

/** 流式阶段：只更新助手气泡的纯文本，避免每个 token 都做整段 Markdown 重排 */
function setChatText(id, text) {
  const message = findChatMessage(id);
  if (!message) return;
  message.text = String(text || '');
  message.rendered = false;
  const body = chatHistoryNode().querySelector('[data-chat-text="' + id + '"]');
  if (body) {
    body.textContent = message.text;
    const stream = chatHistoryNode();
    if (stream) stream.scrollTop = stream.scrollHeight;
  }
  syncChatFold(id);
}

function setChatMarkdown(id, markdown) {
  const message = findChatMessage(id);
  if (!message) return;
  message.text = String(markdown || '');
  message.rendered = true;
  const host = chatHistoryNode().querySelector('[data-chat-body="' + id + '"]');
  if (!host) return;
  clear(host);
  host.classList.add('markdown');
  host.appendChild(renderMarkdown(message.text));
  syncChatFold(id);
}

function setChatStatus(id, status) {
  const message = findChatMessage(id);
  if (!message) return;
  message.status = status;
  const node = chatHistoryNode().querySelector('[data-chat-status="' + id + '"]');
  if (node) node.textContent = status;
}

function renderChatHistory() {
  const box = chatHistoryNode();
  if (!box) return;
  clear(box);
  const messages = state.chatMessages;
  $('chat-history-empty').classList.toggle('hidden', messages.length > 0);

  messages.forEach((message) => {
    const wrap = el('div', 'chat-msg ' + (message.role === 'user' ? 'user' : 'assistant') +
      (message.status === '失败' ? ' error' : ''));
    const bubble = el('div', 'chat-bubble');
    bubble.setAttribute('data-chat-bubble', message.id);
    const head = el('div', 'chat-who');
    const status = message.status ? ' · ' + message.status : '';
    head.textContent = (message.role === 'user' ? '我' : '协调 Agent') + status;
    head.setAttribute('data-chat-status', message.id);
    bubble.appendChild(head);

    const body = el('div', 'chat-body');
    body.setAttribute('data-chat-body', message.id);
    /* 显示与传输分离：用户气泡只显示用户自己输入的文字；
       机器拼接的「引用文件」清单（含绝对路径）只存在于发给服务端的 message 里。
       stripFileRefs 同时兜住历史里可能残留的旧格式文本。
       助手气泡：正文剥掉裸 URL（报告规范不许出现网址），并且**已定稿的消息按 Markdown 渲染** ——
       否则任何一次整体重渲染（choices/备注触发）都会把报告打回 `#`/`**`/表格源码。 */
    const displayText = message.role === 'user'
      ? stripFileRefs(message.text)
      : stripBareUrls(message.text || '正在等待模型输出…');
    if (message.role === 'assistant' && message.rendered) {
      body.classList.add('markdown');
      body.appendChild(renderMarkdown(displayText));
    } else {
      const text = el('p', 'chat-text');
      text.setAttribute('data-chat-text', message.id);
      text.textContent = displayText;
      body.appendChild(text);
    }
    bubble.appendChild(body);
    if (chatNeedsFold(displayText)) {
      body.classList.add('chat-fold');
      makeChatFoldToggle(bubble, body);
    }

    /* 规范化报告（report.md）：**唯一权威版**，运行结束后挂在气泡正文之后。
       模型自己的叙述与结论保留在上面（正文），两者不互相覆盖。 */
    if (message.role === 'assistant' && message.reportMarkdown) {
      const report = el('div', 'chat-report');
      report.setAttribute('data-chat-report-block', message.id);
      report.appendChild(el('p', 'chat-report-label', message.reportLabel || '规范报告（report.md）'));
      const rbody = el('div', 'chat-body markdown');
      rbody.setAttribute('data-chat-report', message.id);
      rbody.appendChild(renderMarkdown(resolveReportImages(message.reportMarkdown)));
      report.appendChild(rbody);
      if (chatNeedsFold(message.reportMarkdown)) {
        rbody.classList.add('chat-fold');
        makeChatFoldToggle(report, rbody);
      }
      bubble.appendChild(report);
    }

    /* 附件 chip：紧凑展示（文件名 + R/L），完整路径放 title，绝不排进正文 */
    if (message.role === 'user' && message.attachments && message.attachments.length) {
      const files = el('div', 'chat-files');
      message.attachments.forEach((file) => {
        const chip = el('span', 'chat-file');
        chip.setAttribute('data-file-kind', file.kind || 'ligand');
        const label = file.kind === 'receptor' ? 'R' : 'L';
        chip.title = file.name + '（' + (file.kind === 'receptor' ? '受体' : '小分子库')
          + (file.count ? '，' + fmtInt(file.count) + ' 个分子' : '')
          + '）→ ' + (file.path || '（无路径）');
        chip.appendChild(el('span', 'chat-file-name mono', file.name));
        chip.appendChild(el('span', 'chat-file-kind', label));
        files.appendChild(chip);
      });
      bubble.appendChild(files);
    }

    if (message.role === 'user' && message.chips && message.chips.length) {
      const meta = el('div', 'chat-meta');
      meta.appendChild(el('span', 'chat-meta-label', message.advanced
        ? '高级设置（默认值，可被指令覆盖）：'
        : '本次下发参数：'));
      message.chips.forEach((chip) => meta.appendChild(el('span', 'chat-chip', chip)));
      bubble.appendChild(meta);
    }

    if (message.notes && message.notes.length) {
      const notes = el('div', 'chat-notes');
      message.notes.slice(-12).forEach((note) => notes.appendChild(el('p', 'chat-note', note, note)));
      bubble.appendChild(notes);
    }

    /* 结构化选项：受体/分子解析不确定时，服务端把候选下发成 choices，
       这里渲染成可点按钮；点选后以同一 conversation_id 追问一句等价的话继续跑。 */
    if (message.role !== 'user' && message.choices && message.choices.length) {
      const choices = el('div', 'chat-choices');
      if (message.choiceNote) {
        choices.appendChild(el('p', 'chat-choice-note', message.choiceNote));
      }
      message.choices.forEach((choice, index) => {
        const button = el('button', 'chat-choice', choice.label || choice.value || '选项');
        button.type = 'button';
        button.setAttribute('data-choice-id', choice.id || ('choice-' + index));
        button.setAttribute('data-choice-kind', choice.kind || '');
        button.setAttribute('data-choice-value', choice.value || '');
        if (choice.prompt || choice.value) button.title = choice.prompt || choice.value;
        button.addEventListener('click', () => pickChatChoice(message.id, index));
        choices.appendChild(button);
      });
      bubble.appendChild(choices);
    }

    wrap.appendChild(bubble);
    box.appendChild(wrap);
  });

  box.scrollTop = box.scrollHeight;
}

/* 收到 choices 事件：挂到当前助手气泡上并重排（按钮在气泡下方） */
function handleChoicesEvent(data) {
  const choices = Array.isArray(data && data.choices) ? data.choices : [];
  if (!choices.length) return;
  if (state.page !== 'chat') return;
  if (!state.chatActiveId) state.chatActiveId = appendChatMessage('assistant', '', '请选择');
  const message = findChatMessage(state.chatActiveId);
  if (!message) return;
  message.choices = choices.slice();
  message.choiceNote = (data && data.note) || '';
  if (message.status === '运行中') message.status = '请选择';
  renderChatHistory();
  logLine('收到 ' + choices.length + ' 个候选可选项：可在助手气泡下方点选。', 'stage');
}

/* 点选某个候选：清空按钮 → 把等价追问写进输入框 → 以同一 conversation_id 继续运行 */
function pickChatChoice(messageId, index) {
  const message = findChatMessage(messageId);
  if (!message || !message.choices || !message.choices[index]) return;
  const choice = message.choices[index];
  const text = String(choice.prompt || choice.value || choice.label || '');
  if (!text) return;
  message.choices = [];
  message.choiceNote = '';
  renderChatHistory();
  const input = $('chat-input');
  if (input) {
    input.value = text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  logLine('已选择：' + (choice.label || text), 'cmd');
  startRun();
}

function finishAssistant(status) {
  if (state.page !== 'chat' || !state.chatActiveId) return;
  const message = findChatMessage(state.chatActiveId);
  if (!message) return;
  if (!message.rendered) {
    const fallback = state.tokenRendered.trim();
    setChatMarkdown(state.chatActiveId, fallback || '（本次运行没有产生文本输出，请查看下方工具调用轨迹与结果区。）');
  }
  setChatStatus(state.chatActiveId, status || '已完成');
}

function showChatError(message) {
  const box = $('chat-error');
  if (!box) return;
  box.textContent = message;
  show(box);
}

function clearChatHistory() {
  state.chatMessages = [];
  state.chatSeq = 0;
  state.chatActiveId = null;
  renderChatHistory();
}

/* --------------------------------------------------------------------------
 * 11. 运行控制
 *
 * 两种子页共用同一份参数表单，但下发语义完全不同：
 *   chat + advanced=false → mode/message/advanced + 附件派生字段（receptor_file /
 *                           molecule_file）；其余参数字段一个都不带
 *   chat + advanced=true  → 带上表单参数，服务端视为「可被指令覆盖的默认值」
 *   manual                → 表单参数是权威参数（mode=manual）
 * 这样对话指令与手动参数在协议层就不会互相冲突。
 * ------------------------------------------------------------------------ */

/* --------------------------------------------------------------------------
 * 对话附件：上传 / 拖拽 / @ 引用
 *   · 受体文件（.pdb/.ent/.pdb1/.cif/.mmcif/.pdbqt）→ 作为 receptor_file 下发
 *   · 小分子库（.sdf/.smi/.csv/.mol2…）→ 作为 molecule_file 下发
 *   · 其余附件写进指令的「引用文件」清单（带服务端绝对路径），供 Agent 在工具里使用
 * 设计原则：附件是**明确的用户意图**，所以即使高级设置没展开也会下发这两个字段；
 *          但除附件外的任何参数仍然一个都不带（保持"对话不注入参数"的语义）。
 * ------------------------------------------------------------------------ */
/* 与后端 core/receptors.py 的 RECEPTOR_EXTS 保持一致（后端那份是唯一权威定义） */
const RECEPTOR_EXTS = ['.pdb', '.ent', '.pdb1', '.cif', '.mmcif', '.pdbqt'];

function fileExt(name) {
  const s = String(name || '').toLowerCase();
  const i = s.lastIndexOf('.');
  return i >= 0 ? s.slice(i) : '';
}

function attachmentKind(name) {
  return RECEPTOR_EXTS.indexOf(fileExt(name)) >= 0 ? 'receptor' : 'ligand';
}

/** 上传一批文件（对话框附件 / 拖拽），成功后进入 state.attachments */
async function chatAttachFiles(fileList) {
  const files = Array.from(fileList || []).filter(Boolean);
  if (!files.length) return [];
  const hint = $('chat-attach-hint');
  const added = [];
  for (let i = 0; i < files.length; i += 1) {
    const file = files[i];
    if (hint) hint.textContent = '上传中 ' + (i + 1) + '/' + files.length + '：' + file.name;
    const form = new FormData();
    form.append('file', file);
    form.append('kind', attachmentKind(file.name) === 'receptor' ? 'receptor' : 'ligand');
    try {
      const response = await fetch('/api/uploads', { method: 'POST', body: form });
      const data = await response.json().catch(() => null);
      if (!response.ok || !data || data.status !== 'ok') {
        throw new Error((data && (data.detail || data.message)) || ('HTTP ' + response.status));
      }
      const item = {
        id: 'att-' + Date.now() + '-' + i,
        name: file.name,
        kind: attachmentKind(file.name),
        size: data.size || file.size || 0,
        path: data.path || data.receptor_file || '',
        /* 附件里的受体同样交原始文件：准备（PDBQT + pH 质子化）发生在运行阶段 */
        receptorFile: '',
        count: data.count || 0,
        molecules: Array.isArray(data.molecules) ? data.molecules.slice(0, 3) : [],
        chemistryWarning: data.chemistry_warning || '',
      };
      state.attachments.push(item);
      added.push(item);
      /* 上传接口只保存文件（v0.22）：准备 PDBQT、按目标 pH 处理受体质子化、解析分子数
         都发生在**开始运行时**，因此这里如实说「待运行」，不要显示还没发生的结果。 */
      const summary = item.kind === 'receptor'
        ? '已保存（受体将在开始运行时现场准备为 PDBQT，并按目标 pH 处理质子化）'
        : '已保存（分子库将在开始运行时解析）';
      logLine('已添加附件 ' + item.name + '（' + summary + '）', 'stage');
      if (item.chemistryWarning) logLine('附件提示：' + item.chemistryWarning, 'err');
    } catch (error) {
      logLine('附件上传失败 ' + file.name + '：' + shortError(error), 'err');
      if (hint) hint.textContent = '上传失败：' + shortError(error);
    }
  }
  renderChatAttachments();
  if (hint && !added.length) hint.textContent = '可拖拽文件到此，或输入 @ 引用';
  return added;
}

function removeAttachment(id) {
  state.attachments = state.attachments.filter((a) => a.id !== id);
  renderChatAttachments();
}

/** 渲染附件 chips + 同步 @ 引用按钮与提示文案 */
function renderChatAttachments() {
  const host = $('chat-attachments');
  const hint = $('chat-attach-hint');
  const mentionBtn = $('chat-mention-btn');
  if (!host) return;
  clear(host);
  state.attachments.forEach((item) => {
    const chip = el('span', 'chat-attachment');
    chip.title = (item.kind === 'receptor'
      ? ('受体文件：' + item.name + '（源文件 ' + item.path + '）'
         + '；开始运行时才准备为 PDBQT 并按目标 pH 处理受体质子化，提交时作为 receptor_file，'
         + '优先于注册表受体')
      : ('小分子库：' + item.path + '；开始运行时才解析'));
    chip.appendChild(el('span', 'chat-attachment-name mono', item.name));
    chip.appendChild(el('span', 'chat-attachment-kind', item.kind === 'receptor' ? 'R' : 'L'));
    const del = el('button', 'chat-attachment-remove', '×');
    del.type = 'button';
    del.setAttribute('aria-label', '移除附件 ' + item.name);
    del.addEventListener('click', () => removeAttachment(item.id));
    chip.appendChild(del);
    host.appendChild(chip);
  });
  host.classList.toggle('hidden', state.attachments.length === 0);
  if (mentionBtn) mentionBtn.classList.toggle('hidden', state.attachments.length === 0);
  if (hint) {
    hint.textContent = state.attachments.length
      ? (fmtInt(state.attachments.length) + ' 个附件：输入 @ 可在指令中引用')
      : '可拖拽文件到此，或输入 @ 引用';
  }
}

/**
 * 从附件推导请求字段：首个受体 + 首个配体库作为结构化字段，其余进引用清单。
 * 传 attachments 可脱离界面状态单测（见 scripts/ui_e2e.js）。
 */
function chatAttachmentPayload(attachments) {
  const list = Array.isArray(attachments) ? attachments : state.attachments;
  const receptor = list.find((a) => a.kind === 'receptor');
  const ligand = list.find((a) => a.kind === 'ligand');
  return {
    receptor_file: receptor ? (receptor.receptorFile || receptor.path || '') : '',
    molecule_file: ligand ? (ligand.path || '') : '',
    refs: list.map((a) => ({ name: a.name, kind: a.kind, path: a.path, count: a.count })),
  };
}

/** 附件 → 只含附件字段的 params（纯函数，便于单测；除附件外不带任何参数） */
function chatAttachmentParams(attachments) {
  const att = chatAttachmentPayload(attachments);
  const params = {};
  if (att.receptor_file) params.receptor_file = att.receptor_file;
  if (att.molecule_file) params.molecule_file = att.molecule_file;
  return { params: params, refs: att.refs };
}

/** 把「引用文件」清单拼进指令（纯函数）：@ 命中的附件优先，否则用全部附件 */
function composeChatMessage(raw, attachments) {
  const list = Array.isArray(attachments) ? attachments : state.attachments;
  const mentioned = mentionedAttachments(raw, list);
  const refs = (mentioned.length ? mentioned : list).map((a) => ({ name: a.name, kind: a.kind,
                                                                  path: a.path, count: a.count }));
  return appendFileRefs(raw, refs);
}

/** 把「引用文件」清单拼进指令：Agent 需要绝对路径才能用工具读它们 */
function appendFileRefs(message, refs) {
  if (!refs || !refs.length) return message;
  const lines = refs.map((r) => '- ' + r.name + '（'
    + (r.kind === 'receptor' ? '受体' : ('小分子库，' + fmtInt(r.count) + ' 个分子'))
    + '）→ ' + (r.path || '（无路径）'));
  return message + '\n\n引用文件（本次对话已上传，可直接作为工具输入）：\n' + lines.join('\n');
}

/** 指令里出现的 @名称 → 命中附件（用于"在对话中引用文件"；可传列表便于单测） */
function mentionedAttachments(text, attachments) {
  const list = Array.isArray(attachments) ? attachments : state.attachments;
  const names = new Set();
  String(text || '').replace(/@([^\s@，。；、]+)/g, (m, name) => {
    names.add(name.trim());
    return m;
  });
  return list.filter((a) => names.has(a.name)
    || Array.from(names).some((n) => a.name.indexOf(n) === 0 && n.length >= 2));
}

/* -------- @ 引用选择器（输入 @ 或点「@ 引用」按钮）-------- */
function openMention(query) {
  state.mention.open = true;
  state.mention.query = query || '';
  renderMention();
}

function closeMention() {
  state.mention.open = false;
  const box = $('chat-mention');
  if (box) box.classList.add('hidden');
}

function renderMention() {
  const box = $('chat-mention');
  const input = $('chat-input');
  if (!box) return;
  if (!state.mention.open || !state.attachments.length) {
    box.classList.add('hidden');
    return;
  }
  const q = state.mention.query.toLowerCase();
  const items = state.attachments.filter((a) => !q || a.name.toLowerCase().indexOf(q) >= 0);
  clear(box);
  if (!items.length) {
    box.appendChild(el('p', 'chat-mention-empty', '没有匹配的附件'));
  }
  items.forEach((item) => {
    const row = el('button', 'chat-mention-item');
    row.type = 'button';
    row.appendChild(el('span', 'chat-mention-name mono', item.name));
    row.appendChild(el('span', 'chat-mention-kind',
      item.kind === 'receptor' ? '受体' : (fmtInt(item.count) + ' 分子')));
    row.addEventListener('click', () => {
      if (input) {
        const value = input.value;
        const at = value.lastIndexOf('@');
        input.value = (at >= 0 ? value.slice(0, at) : value) + '@' + item.name + ' ';
        input.focus();
      }
      closeMention();
    });
    box.appendChild(row);
  });
  box.classList.remove('hidden');
}

/** 收集共享参数表单的原始值（未做必填校验） */
function collectParamForm() {
  const center = ['center-x', 'center-y', 'center-z'].map((id) => toNumber($(id).value));
  const size = ['size-x', 'size-y', 'size-z'].map((id) => toNumber($(id).value));
  const form = {
    receptor: $('receptor-select').value || '',
    /* 提交**原始上传文件路径**：受体在运行阶段才准备（含目标 pH 质子化），
       上传时后端不做任何处理，因此这里不能再用 receptor_file（那是预览产物）。 */
    receptor_file: (state.upload.receptor.data && state.upload.receptor.data.path) || '',
    site_center: center.every((value) => value === null) ? null : center,
    site_size: size.every((value) => value === null) ? null : size,
    ligands_text: '',
    molecule_file: '',
    allow_example_fallback: false,
    positive_control: $('positive-control').value.trim(),
    engine: $('engine-select').value || 'auto',
    protonation: ($('protonation-select') ? $('protonation-select').value : '') || '',
    protonation_ph: toNumber($('protonation-ph') ? $('protonation-ph').value : null),
    pocket_engine: ($('pocket-engine-select') ? $('pocket-engine-select').value : '') || '',
    /* 勾选「自动」→ 不下发数值（null = 后端按库柔性/盒体积自动规划） */
    exhaustiveness: ($('exhaustiveness-auto') && $('exhaustiveness-auto').checked)
      ? null : toNumber($('exhaustiveness').value),
    n_poses: toNumber($('n-poses').value),
    max_ligands: toNumber($('max-ligands').value) || 0,
    save_poses: $('save-poses').checked,
    ligandSource: state.ligandSource
  };

  if (state.ligandSource === 'text') {
    form.ligands_text = $('ligands-text').value.trim();
    form.allow_example_fallback = false;
  } else if (state.ligandSource === 'upload') {
    /* 上传成功后拿服务端返回的 path 当 molecule_file；上传本身优先，不做示例库回退 */
    form.molecule_file = (state.upload.ligand.data && state.upload.ligand.data.path) || '';
    form.allow_example_fallback = false;
  } else if (state.ligandSource === 'file') {
    form.molecule_file = $('molecule-file').value.trim();
    form.allow_example_fallback = $('allow-example-fallback').checked;
  } else {
    const library = state.libraries.find((item) =>
      (item.id || item.path) === $('library-select').value);
    form.molecule_file = library ? (library.path || '') : '';
    form.allow_example_fallback = true;
  }
  return form;
}

/** 构造请求体：决定 mode / advanced / message 与参数字段的组合方式 */
function buildPayload() {
  if (state.page === 'chat') {
    const raw = $('chat-input').value.trim();
    // 指令里 @ 了附件、或上传了附件：把它们作为「引用文件」一并写进指令，
    // 否则 Agent 拿到的只是文件名，无法在工具里读取。
    const message = composeChatMessage(raw);
    // 显示用：用户自己输入的文字 + 本次进入指令的附件清单（气泡里渲染成紧凑 chip）。
    // 注意：payload.message 仍是带绝对路径的完整传输文本 —— 显示与传输分离。
    /* 「运行参数」条常驻在对话框上（v0.25）→ **所见即所用**：
       **只把用户改动过的项**作为「默认值」下发（指令里明确写到的仍优先）。
       界面默认值与服务端默认值一致（exhaustiveness=16 / ph@7.4 / n_poses=1 / vina），
       因此不改动与下发默认值结果相同，不会出现"界面显示 16、实际跑 12"的错位。
       注意：受体下拉与位点盒在对话模式是**隐藏项**（见 mountParams 的 .chat-mode），
       由指令或口袋分析决定，既不发送也不能出现在"本次下发参数"里。 */
    const payload = {
      mode: 'chat', message: message, displayText: raw,
      attachments: chatMessageRefs(raw), advanced: false
    };
    const form = collectParamForm();
    /* 对话模式的高级设置只作为「默认值」下发：未改动的项一律省略，不覆盖指令。
       上传的受体/分子库文件是明确意图，无条件发送（receptor_file / molecule_file）。 */
    const att2 = chatAttachmentParams().params;
    const touched = (id) => state.touchedFields.has(id);
    const params = {
      receptor_file: form.receptor_file || att2.receptor_file || undefined,
      ligands_text: form.ligands_text || undefined,
      molecule_file: form.molecule_file || att2.molecule_file || undefined,
      positive_control: touched('positive-control') && form.positive_control
        ? form.positive_control : undefined,
      engine: touched('engine-select') && form.engine && form.engine !== 'auto'
        ? form.engine : undefined,
      protonation: touched('protonation-select') ? (form.protonation || undefined) : undefined,
      protonation_ph: touched('protonation-select') && form.protonation === 'ph'
        && form.protonation_ph !== null ? form.protonation_ph : undefined,
      pocket_engine: touched('pocket-engine-select') ? (form.pocket_engine || undefined) : undefined,
      exhaustiveness: touched('exhaustiveness') && form.exhaustiveness !== null
        ? form.exhaustiveness : undefined,
      n_poses: touched('n-poses') && form.n_poses !== null ? form.n_poses : undefined,
      max_ligands: touched('max-ligands') ? form.max_ligands : undefined,
      save_poses: touched('save-poses') ? form.save_poses : undefined
    };
    /* advanced=true 仅当确实有"运行参数/专家项"要作为默认值下发；否则保持纯对话语义。
       ligands_text / allow_example_fallback 也是用户在「其他设置」里**看得见、填得进**的输入，
       算显式意图 —— 否则会被 advanced=false 静默丢掉（真实缺陷，见 ui_e2e 回归）。 */
    payload.advanced = ['engine', 'protonation', 'protonation_ph', 'exhaustiveness', 'n_poses',
      'positive_control', 'pocket_engine', 'max_ligands', 'save_poses', 'ligands_text',
      'allow_example_fallback']
      .some((key) => params[key] !== undefined);
    if (form.allow_example_fallback && form.molecule_file) params.allow_example_fallback = true;
    /* 阳性对照：只有用户**动过**这个字段才下发 skip_positive_control；
       留空本来就是"不做对照分析"，不需要额外声明（否则"什么都没改"也会带一个字段）。 */
    if (touched('positive-control') && !form.positive_control) params.skip_positive_control = true;
    payload.params = params;
    payload.paramChips = paramChips(form, params);
    return payload;
  }

  // 参数模式：表单参数为权威参数
  const form = collectParamForm();
  const message = $('manual-description').value.trim();
  const params = {
    receptor: form.receptor,
    receptor_file: form.receptor_file,
    site_center: form.site_center || [],
    site_size: form.site_size || [],
    ligands_text: form.ligands_text,
    molecule_file: form.molecule_file,
    allow_example_fallback: form.allow_example_fallback,
    positive_control: form.positive_control,
    exhaustiveness: form.exhaustiveness,
    n_poses: form.n_poses,
    engine: form.engine,
    protonation: form.protonation || '',
    protonation_ph: form.protonation === 'ph' && form.protonation_ph !== null
      ? form.protonation_ph : 0,
    pocket_engine: form.pocket_engine || '',
    save_poses: form.save_poses,
    max_ligands: form.max_ligands
  };
  // 阳性对照留空 → 显式跳过对照分析（后端 v0.5：不填即跳过）
  if (!form.positive_control) params.skip_positive_control = true;
  const payload = { params: params, paramChips: paramChips(form, params) };
  if (state.mode === 'agent') {
    payload.mode = 'manual';
    if (message) payload.message = message;
  } else {
    payload.mode = 'pipeline';
  }
  payload.form = form;
  return payload;
}

/** 用户气泡里展示的「本次实际下发的关键参数」。
 *
 * **必须与请求体同源**：两张小票都从 `params`（真正进 body 的那份）生成，不再读原始表单。
 * 真实缺陷（用户实测反馈）：气泡写着「本次下发参数：受体 thrombin / 位点中心 […] / 位点尺寸 […]
 * / 引擎 vina / n_poses=1」，而请求体里根本没有这些字段 —— 对话模式的受体下拉与位点盒是
 * **隐藏项**（`.receptor-manual-block` 在 chat 模式 display:none），未改动的运行参数也不下发。
 * 把「界面默认值」写成「已下发」，用户会以为系统擅自指定了受体和位点盒。 */
function paramChips(form, params) {
  return state.page === 'chat' ? chatParamChips(form, params) : manualParamChips(form, params);
}

/** 对话模式小票：只列**真的在请求体里**的字段；一条都没有就如实说「纯指令」。 */
function chatParamChips(form, params) {
  const sent = params || {};
  const has = (key) => sent[key] !== undefined;
  const chips = [];
  if (has('receptor_file')) {
    const uploaded = state.upload.receptor.data || {};
    chips.push('受体文件 ' + fileBaseName(sent.receptor_file) +
      (uploaded.file_name ? '（' + uploaded.file_name + '）' : ''));
  }
  if (has('ligands_text')) {
    chips.push('SMILES 文本 ' + (parseLigandsText(sent.ligands_text).length || 1) + ' 个分子');
  }
  if (has('molecule_file')) {
    const uploaded = state.upload.ligand.data || {};
    const count = toNumber(uploaded.count);
    chips.push((uploaded.file_name ? '上传分子库 ' : '分子文件 ') +
      fileBaseName(uploaded.file_name || sent.molecule_file) +
      (count === null ? '' : '（' + fmtInt(count) + ' 个分子）'));
  }
  if (has('protonation')) {
    chips.push(sent.protonation === 'ph' && has('protonation_ph')
      ? '质子化 ph=' + fmtNum(sent.protonation_ph, 1)
      : '质子化 ' + sent.protonation);
  }
  if (has('positive_control')) chips.push('阳性对照 ' + sent.positive_control);
  if (sent.skip_positive_control) chips.push('不做阳性对照分析');
  if (has('engine')) chips.push('引擎 ' + sent.engine);
  if (has('pocket_engine')) chips.push('口袋引擎 ' + sent.pocket_engine);
  if (has('exhaustiveness')) chips.push('exhaustiveness=' + sent.exhaustiveness);
  if (has('n_poses')) chips.push('n_poses=' + sent.n_poses);
  if (has('max_ligands')) chips.push('最大分子数=' + sent.max_ligands);
  if (has('save_poses')) chips.push(sent.save_poses ? '保存位姿' : '不保存位姿');
  /* 对话模式的受体与位点盒由指令或口袋分析决定（界面里也是隐藏的）：既不发送，也不显示。 */
  if (!chips.length) chips.push('纯指令：不注入任何运行参数（全部按系统默认/指令执行）');
  return chips;
}

/** 参数模式小票：表单是权威参数，列出的就是真正下发的值。 */
function manualParamChips(form, params) {
  const sent = params || {};
  const chips = [];
  if (sent.receptor_file) {
    const uploaded = state.upload.receptor.data || {};
    chips.push('受体文件 ' + fileBaseName(sent.receptor_file) +
      (uploaded.file_name ? '（' + uploaded.file_name + '）' : ''));
  } else if (sent.receptor) {
    const receptor = state.receptors.find((item) => item.key === sent.receptor);
    chips.push('受体 ' + (receptor ? (receptor.name || receptor.key) : sent.receptor));
  }
  if (form.ligandSource === 'text' && sent.ligands_text) {
    const count = parseLigandsText(sent.ligands_text).length;
    chips.push('SMILES 文本 ' + (count || 1) + ' 个分子');
  } else if (form.ligandSource === 'upload' && sent.molecule_file) {
    const uploaded = state.upload.ligand.data || {};
    const count = toNumber(uploaded.count);
    chips.push('上传分子库 ' + fileBaseName(uploaded.file_name || sent.molecule_file) +
      (count === null ? '' : '（' + fmtInt(count) + ' 个分子）'));
  } else if (form.ligandSource === 'file' && sent.molecule_file) {
    chips.push('分子文件 ' + sent.molecule_file);
  } else if (form.ligandSource === 'library' && sent.molecule_file) {
    chips.push('示例分子库 ' + sent.molecule_file);
  }
  if (sent.site_center && sent.site_center.length) {
    chips.push('位点中心 [' + sent.site_center.map((v) => fmtNum(v, 2)).join(', ') + ']');
  }
  if (sent.site_size && sent.site_size.length) {
    chips.push('位点尺寸 [' + sent.site_size.map((v) => fmtNum(v, 2)).join(', ') + ']');
  }
  if (sent.protonation === 'ph' && sent.protonation_ph) {
    chips.push('质子化 ph=' + fmtNum(sent.protonation_ph, 1));
  } else if (sent.protonation) {
    chips.push('质子化 ' + sent.protonation);
  }
  if (sent.positive_control) chips.push('阳性对照 ' + sent.positive_control);
  else chips.push('不做阳性对照分析（留空）');
  if (sent.engine && sent.engine !== 'auto') chips.push('引擎 ' + sent.engine);
  if (sent.exhaustiveness !== null && sent.exhaustiveness !== undefined) {
    chips.push('exhaustiveness=' + sent.exhaustiveness);
  }
  if (sent.n_poses !== null && sent.n_poses !== undefined) chips.push('n_poses=' + sent.n_poses);
  if (sent.max_ligands) chips.push('最大分子数=' + sent.max_ligands);
  chips.push(sent.save_poses ? '保存位姿' : '不保存位姿');
  return chips;
}

/** 参数模式下的必填校验（表单为权威参数） */
function validateManualForm(form) {
  if (!form.receptor && !form.receptor_file) {
    return '请先选择受体，或上传受体文件（.pdb / .pdbqt）。';
  }
  if (state.ligandSource === 'text' && !form.ligands_text) {
    return '请至少输入一个 SMILES 分子，或切换到「使用示例库」/「上传文件」。';
  }
  if (state.ligandSource === 'upload' && !form.molecule_file) {
    return '已选择「上传文件」，请先上传成功一个小分子库文件（' + uploadExText('ligand') + '）。';
  }
  if (state.ligandSource === 'file' && !form.molecule_file && !form.allow_example_fallback) {
    return '请填写服务端文件路径或 URL，或勾选「回退使用示例分子库」。';
  }
  if (state.ligandSource === 'library' && !form.molecule_file) {
    return '未能解析所选示例分子库的路径，请改用「SMILES 文本」或「上传文件」。';
  }
  if (form.protonation && PROTONATION_POLICIES.indexOf(form.protonation) < 0) {
    return '质子化态策略只能是 neutralize / ph / keep。';
  }
  if (form.protonation === 'ph') {
    if (form.protonation_ph === null) return '选择 ph 策略时请填写目标 pH（默认 7.4）。';
    if (form.protonation_ph < PH_MIN || form.protonation_ph > PH_MAX) {
      return `目标 pH 必须在 ${PH_MIN} 到 ${PH_MAX} 之间。`;
    }
  }
  /* null = 勾了「自动」→ 不下发数值、由后端按库/盒自动规划（合法状态，不是错误） */
  if (form.exhaustiveness !== null
    && (form.exhaustiveness < 1 || form.exhaustiveness > 32)) {
    return 'exhaustiveness 必须在 1 到 32 之间（或勾选「自动」交给系统规划）。';
  }
  if (state.mode === 'agent' && state.health && state.health.llm_configured === false) {
    return '后端未配置 LLM，无法使用多 Agent 协作模式，请改用确定性流水线。';
  }
  return null;
}

/** 对话模式下高级设置的校验：宽松，只拦截明显非法的值与「选了上传却没上传」 */
function validateChatAdvanced(form) {
  if (form.exhaustiveness !== null && (form.exhaustiveness < 1 || form.exhaustiveness > 32)) {
    return '高级设置里的 exhaustiveness 必须在 1 到 32 之间（留空/勾「自动」则按系统规划）。';
  }
  if (state.ligandSource === 'upload' && !(state.upload.ligand.data && state.upload.ligand.data.path)) {
    return '高级设置里已选择「上传文件」，但小分子文件尚未上传成功；请上传、切换来源，或折叠高级设置。';
  }
  return null;
}

/** 附件派生字段：上传的受体/分子库是**明确的用户意图**，任何模式下都必须进请求体 */
const ATTACHMENT_BODY_FIELDS = ['receptor_file', 'molecule_file'];

/**
 * 把 Payload 变成真正发给服务端的请求体。
 * - 参数模式 + 多 Agent：带 mode=manual 与全部权威参数
 * - 参数模式 + 流水线：只发表单权威参数（流水线不需要 mode/advanced/message）
 *   —— 现在这些字段统一放进标准信封的 input（见 standardFrameEvents / runsStreamPath）
 * - 对话模式：advanced=false 时只带 mode/message/advanced + **附件派生字段**
 *   （receptor_file / molecule_file，有值才带）——附件是明确意图，折叠高级设置不能丢；
 *   其余参数字段仍然一个都不带，保持「对话不注入参数」的语义。
 */
function payloadToBody(payload) {
  /* 多轮会话：每次提交都带上同一个 conversation_id，服务端据此延续上一轮上下文 */
  const conversationId = state.conversationId || '';
  if (state.page === 'manual') {
    const body = Object.assign({}, payload.params);
    if (conversationId) body.conversation_id = conversationId;
    if (state.mode === 'agent') {
      body.mode = 'manual';
      if (payload.message) body.message = payload.message;
    }
    return body;
  }
  const body = {
    mode: 'chat',
    message: payload.message,
    advanced: Boolean(payload.advanced)
  };
  if (conversationId) body.conversation_id = conversationId;
  const params = payload.params || {};
  /* 真实缺陷：这里原来写的是 `if (payload.advanced) Object.assign(body, payload.params)`，
     advanced=false 时连 receptor_file 一起丢掉 → 服务端收不到上传受体 → 回退默认 thrombin。
     现在附件字段**先无条件带上**，其余参数仍只在 advanced=true 时带。 */
  ATTACHMENT_BODY_FIELDS.forEach((field) => {
    if (params[field]) body[field] = params[field];
  });
  if (payload.advanced) Object.assign(body, params);
  return body;
}

function resetExecution() {
  hideError();
  setWorkbenchHasRun(false);
  renderRunNotes(null);
  clear($('molecules-tbody'));
  clear($('log-box'));
  $('log-box').appendChild(el('p', 'empty', '等待运行…'));
  clear($('tool-trace'));
  $('tool-trace').appendChild(el('p', 'empty', '暂无工具调用。'));
  state.liveCount = 0;
  state.tokenBuffer = '';
  state.tokenRendered = '';
  state.serverElapsed = null;
  state.etaSec = null;
  state.cancelled = false;
  state.stopping = false;
  clearStopTimer();
  state.progress.index = 0;
  state.progress.total = 0;
  state.progress.percent = 0;
  state.progress.stage = '';
  state.progress.lastDecile = -1;
  $('token-stream').textContent = '等待模型输出…';
  $('token-stream').dataset.prompt = '[' + nowStamp() + '] $ ';
  $('live-count').textContent = '0';
  $('stage-current').textContent = '阶段 —';
  $('stage-counter').textContent = '— / —';
  $('exec-elapsed').textContent = '用时 —';
  $('progress-eta').textContent = '剩余时间 —';
  setProgress(0);
  updateProgressDone();
  const empty = $('molecules-empty');
  empty.textContent = '尚未产生分子结果。运行开始后这里会逐条显示。';
  show(empty);
  setLiveNote(state.estimatedCount);
  resetOrchestration();
  clearRunResults();
}

function startElapsedTimer() {
  stopElapsedTimer();
  state.startedAt = Date.now();
  state.serverElapsed = null;
  updateElapsed();
  state.elapsedTimer = setInterval(updateElapsed, 1000);
}

function stopElapsedTimer() {
  if (state.elapsedTimer) {
    clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
  }
  if (state.startedAt) updateElapsed();
  state.startedAt = 0;
  state.serverElapsed = null;
}

/* 停止兜底计时器：点击停止后等待 cancelled/done，超时才强制中断本地 fetch */
function clearStopTimer() {
  if (state.stopTimer) {
    clearTimeout(state.stopTimer);
    state.stopTimer = null;
  }
}

/** 工作台是否有可看的结果：决定是否收起空控件（排序工具条 / 表格 / 图例 / 图表）。
 *  首屏与「新对话」时收起，载入到有排序或报告后展开 —— 少看一堆空控件。 */
function setWorkbenchHasRun(has) {
  const view = $('view-workbench');
  if (view) view.classList.toggle('no-run', !has);
}

/** 运行详情（编排时间轴 / 日志 / 工具轨迹 / 实时结果）默认收起，跑起来才自动展开。 */
function setRunDetailsOpen(open) {
  const details = $('run-details');
  if (details) details.open = Boolean(open);
}

function setRunning(running) {
  state.running = running;
  $('btn-start').disabled = running;
  const stop = $('btn-stop');
  const stopLabel = $('btn-stop-label');
  if (stop) {
    stop.disabled = !running;
    stop.classList.remove('stopping');
  }
  if (stopLabel) stopLabel.textContent = '■ 停止';
  if (!running) state.stopping = false;
  $('start-spinner').classList.toggle('hidden', !running);
  $('start-label').textContent = running ? '运行中…' : '开始运行';
  // 运行期间禁止开新对话（避免本轮结果被写进新会话）
  const newChat = $('btn-chat-new');
  if (newChat) newChat.disabled = running;
  // 对话模式的「发送」按钮同步禁用，避免两种模式同时提交
  const send = $('chat-send');
  if (send) send.disabled = running;
  $('chat-send-spinner').classList.toggle('hidden', !(running && state.page === 'chat'));
  $('chat-send-label').textContent = running && state.page === 'chat' ? '运行中…' : '发送';
  $('chat-input').disabled = running;
  /* 开跑 → 自动展开「运行详情」（时间轴 / 日志 / 工具轨迹就在那里）；
     停下不再自动收起，用户可以自己折叠。 */
  if (running) setRunDetailsOpen(true);
  // 运行期间禁止切换子页、切换运行方式，避免串台
  document.querySelectorAll('#mode-tabs .page-tab').forEach((button) => { button.disabled = running; });
  document.querySelectorAll('#mode-switch .seg-btn').forEach((button) => { button.disabled = running; });
}

async function startRun() {
  if (state.running) return;
  const payload = buildPayload();

  // 分模式校验：对话模式宽松（高级设置未展开时无需任何参数）
  if (state.page === 'manual') {
    const problem = validateManualForm(payload.form || collectParamForm());
    if (problem) {
      setRunHint(problem, 'err');
      showError(problem);
      return;
    }
  } else if (payload.advanced) {
    const problem = validateChatAdvanced(collectParamForm());
    if (problem) {
      setRunHint(problem, 'err');
      showError(problem);
      return;
    }
  }

  const body = payloadToBody(payload);
  // 对话模式必须在有指令时才能提交；参数模式流水线不需要 message
  if (state.page === 'chat' && !payload.message) {
    const problem = '请输入要下达的指令后再发送。';
    setRunHint(problem, 'err');
    showError(problem);
    return;
  }

  updateConfigInfoBar();
  const estimate = state.estimatedCount || 0;
  /* 记录本次是否提供阳性对照：留空 → 后端跳过对照分析，编排图在收尾时把结合模式检测标记 SKIP */
  state.runPositiveControl = ($('positive-control').value || '').trim();
  resetExecution();
  setRunning(true);

  // 记录用户气泡（只显示用户输入 + 附件 chip；带绝对路径的传输文本仍下发服务端）
  if (state.page === 'chat') {
    appendChatMessage('user',
      payload.displayText !== undefined ? payload.displayText : payload.message,
      'queued', payload.paramChips, payload.advanced, payload.attachments);
    // 新一轮回复：清空上一轮助手引用与流式缓存，并预置助手气泡
    state.chatActiveId = null;
    state.tokenRendered = '';
    state.tokenBuffer = '';
    state.chatActiveId = appendChatMessage('assistant', '', '运行中');
    setRunHint(payload.advanced
      ? '已提交（advanced=true，高级设置作为可被指令覆盖的默认值）。正在建立实时数据流…'
      : '已提交（advanced=false，纯指令、不注入任何参数）。正在建立实时数据流…');
  } else {
    const chips = payload.paramChips || [];
    appendChatMessage('user',
      (state.mode === 'agent'
        ? '【参数模式 · 多 Agent 协作】'
        : '【参数模式 · 确定性流水线】') +
      (payload.message ? ' ' + payload.message : '（未填写目标描述，仅使用上方表单参数）'),
      'queued', chips, false);
    setRunHint('已提交（表单参数为权威参数）。正在建立实时数据流…');
  }

  /* 标准 Agent Protocol：线程 = 会话 id（首次使用时注册到服务端），运行走线程级 stream 端点 */
  const assistantId = STANDARD_ASSISTANTS[
    state.page === 'chat' ? 'chat' : (state.mode === 'agent' ? 'agent' : 'pipeline')];
  const threadId = state.conversationId || loadConversationId();
  await ensureThreadRegistered(threadId);
  const url = runsStreamPath(threadId);
  const requestBody = {
    assistant_id: assistantId,
    stream_mode: ['messages', 'updates', 'custom'],
    input: {
      ...body,
      conversation_id: threadId,
      messages: body.message ? [{ type: 'human', content: body.message }] : []
    }
  };
  const controller = new AbortController();
  state.controller = controller;
  state.runId = null;
  state.standardRunId = null;
  setRunIdLabel('run_id —');
  logLine('正在请求 ' + url + '（标准 Agent Protocol，assistant=' + assistantId + '）…', 'cmd');
  if (estimate > LARGE_LIBRARY_THRESHOLD) {
    logLine('本次约 ' + fmtInt(estimate) + ' 个分子（超过 500），预计耗时较长；' +
      '进度条会持续显示「已完成 / 总数」与剩余时间，实时表只保留最近 ' +
      LIVE_ROW_LIMIT + ' 条，完整结果见结果总览。', 'stage');
    setRunHint('本次约 ' + fmtInt(estimate) + ' 个分子，预计耗时较长；可随时点击「停止」。');
  }
  if (state.page === 'chat') {
    logLine('请求体：' + JSON.stringify(requestBody.input), 'info');
  }
  startElapsedTimer();

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(requestBody),
      signal: controller.signal
    });

    if (!response.ok) {
      const text = await response.text();
      let detail = '';
      try {
        const parsed = JSON.parse(text);
        detail = parsed.error_message || parsed.error || '';
      } catch (error) {
        detail = text.slice(0, 300);
      }
      throw new Error('HTTP ' + response.status + (detail ? '：' + detail : ''));
    }

    for await (const frame of sseEvents(response, controller.signal, true)) {
      for (const event of standardFrameEvents(frame)) {
        await handleEvent(event);
      }
    }

    if (!state.cancelled && state.progress.total && state.progress.index >= state.progress.total) {
      setProgress(100);
    }
    logLine('事件流已结束。', 'stage');
  } catch (error) {
    if (error && error.name === 'AbortError') {
      // 用户主动停止：先调用 cancel 接口，随后（或超时兜底）中断本地 fetch
      if (state.stopping || state.cancelled) {
        state.cancelled = true;
        clearStopTimer();
        logLine('本地数据流已中断（已请求后端取消）。', 'cmd');
        setRunHint('运行已取消：后端已收到取消请求，已完成的部分中间数据仍会落盘。', 'warn');
        orchCancel();
        if (state.page === 'chat' && state.chatActiveId) {
          const message = findChatMessage(state.chatActiveId);
          if (message && !message.text) message.text = '（运行已被用户取消）';
          setChatStatus(state.chatActiveId, '已取消');
        }
      } else {
        logLine('运行已被用户停止。', 'err');
        setRunHint('运行已停止。', 'warn');
        if (state.page === 'chat' && state.chatActiveId) {
          const message = findChatMessage(state.chatActiveId);
          if (message && !message.text) message.text = '（运行已被用户停止）';
          setChatStatus(state.chatActiveId, '已停止');
        }
      }
    } else if (isNetworkError(error)) {
      const message = '网络中断或服务端不可达（' + shortError(error) + '）。请确认后端服务仍在运行后重试。';
      showError(message);
      logLine(message, 'err');
      setRunHint('运行中断：网络错误。', 'err');
      orchFail(state.orch.current);
    } else if (error && error.status) {
      showError('服务端返回 HTTP ' + error.status + '：' + shortError(error));
      logLine('请求失败：' + shortError(error), 'err');
      setRunHint('运行失败。', 'err');
      orchFail(state.orch.current);
    } else {
      showError('运行失败：' + shortError(error));
      logLine('运行失败：' + shortError(error), 'err');
      setRunHint('运行失败。', 'err');
      orchFail(state.orch.current);
    }
    finishAssistant(state.cancelled ? '已取消' : '失败');
    stopElapsedTimer();
    clearStopTimer();
    setRunning(false);
    state.controller = null;
    $('start-spinner').classList.add('hidden');
    await refreshHistory();
    return;
  }

  stopElapsedTimer();
  clearStopTimer();
  setRunning(false);
  state.controller = null;
  finishAssistant(state.cancelled ? '已取消' : '已完成');

  const runId = state.runId;
  if (runId) {
    setRunHint(state.cancelled
      ? '运行已取消，正在载入已完成的部分结果…'
      : '运行结束，正在载入结果产物…');
    try {
      await loadRun(runId, { silent: true });
      /* 「没算任何东西」的运行（例如用户只说了句「你好」、受理层 reject）：
         后端不产规范报告也不产图，界面也不该把用户甩到空的结果总览、更不该说"结果已载入"。 */
      const noWork = !state.reportMarkdown && !(state.ranking.total > 0);
      if (noWork && !state.cancelled) {
        setRunHint('本次未执行计算（未生成报告）：' + runId +
          '　如需开始筛选，请给出候选分子与受体目标。');
        if (state.page === 'chat') {
          appendChatNote('本次未执行计算（未生成报告）· run_id ' + runId +
            '：受理层判定本次指令不是可执行的筛选任务，因此没有调用任何工具、也没有生成报告。');
        }
        await refreshHistory();
        return;
      }
      setRunHint(state.cancelled ? ('运行已取消：' + runId) : ('运行完成：' + runId));
      switchTab('overview');
      if (state.page === 'chat') {
        attachStandardReportToChat();
        appendChatNote((state.cancelled ? '运行已取消 · ' : '运行完成 · ') + 'run_id ' + runId +
          '，结果已载入下方「结果总览 / 分子详情 / 报告 / 中间数据」。');
      }
    } catch (error) {
      setRunHint('运行已完成，但载入结果失败：' + shortError(error), 'err');
      showError('载入运行结果失败：' + shortError(error));
    }
  } else {
    setRunHint('运行结束，但未获取到 run_id，可在「历史运行」中查看。', 'warn');
  }
  await refreshHistory();
}

/**
 * 运行结束后把**规范化报告**（`report.md`，唯一权威版）挂到当前助手气泡。
 *
 * 为什么需要：`final` 事件里 `state.reportMarkdown` 是**模型自己写的**文本（章节、措辞、
 * 甚至裸网址都不受控）；`applyRun()` 随后把它替换成落盘的 `report.md`（9 节、零网址、图表内嵌）。
 * 这里把后者作为独立小节渲染进气泡，模型自己的叙述与结论仍保留在气泡正文里（互不覆盖）。
 */
function attachStandardReportToChat() {
  const markdown = String(state.reportMarkdown || '');
  if (!markdown || state.page !== 'chat' || !state.chatActiveId) return;
  const message = findChatMessage(state.chatActiveId);
  if (!message) return;
  if (message.reportMarkdown === markdown) return;
  message.reportMarkdown = markdown;
  message.reportLabel = '规范报告（report.md · 唯一权威版）';
  renderChatHistory();
}

function isNetworkError(error) {
  if (!error) return false;
  if (error.status) return false;
  return error instanceof TypeError || /Failed to fetch|NetworkError|network/i.test(error.message || '');
}

/**
 * 停止运行（v0.5）——必须真正中断后端对接：
 *   1) 按钮进入「正在停止…」禁用态，直到收到 cancelled / done（或兜底中断）后复位；
 *   2) **先** POST /api/runs/{run_id}/cancel（后端用协作式取消标志终止对接进程池）；
 *   3) 再对本地 fetch 做 AbortController.abort()——但给 cancelled/done 留一小段到达窗口，
 *      超时才强制中断，这样既能显示后端回传的取消原因，又不会卡住界面。
 * run_id 来自 SSE 的 start 事件；尚未拿到 run_id 时只能中断本地流。
 */
async function stopRun() {
  if (!state.running || state.stopping) return;
  state.stopping = true;
  const stop = $('btn-stop');
  const label = $('btn-stop-label');
  if (stop) {
    stop.disabled = true;
    stop.classList.add('stopping');
  }
  if (label) label.textContent = '正在停止…';
  setRunHint('正在停止：已请求后端取消本次运行…', 'warn');
  setOrchCoord('正在取消运行');

  const runId = state.standardRunId || state.runId;
  if (runId) {
    /* 标准面：POST /threads/{tid}/runs/{run_id}/cancel（平台 id 与业务 id 都接受）；
       旧端点 /api/runs/{id}/cancel 作为兜底，保证任何装配下都能取消。 */
    const paths = [];
    if (state.conversationId) paths.push(runsCancelPath(state.conversationId, runId));
    if (state.runId) paths.push('/api/runs/' + encodeURIComponent(state.runId) + '/cancel');
    let done = false;
    for (const path of paths) {
      if (done) break;
      try {
        const response = await fetch(path, { method: 'POST', headers: { Accept: 'application/json' } });
        const text = await response.text();
        let data = null;
        try { data = JSON.parse(text); } catch (error) { data = null; }
        if (response.ok) {
          const status = (data && data.status) ? data.status : ('HTTP ' + response.status);
          logLine('已请求后端取消运行 ' + runId + '（' + status + '）', 'cmd');
          done = true;
        }
      } catch (error) {
        logLine('取消请求未能送达 ' + path + '（' + shortError(error) + '）。', 'err');
      }
    }
    if (!done) logLine('取消请求均未成功，将中断本地数据流。', 'warn');
  } else {
    logLine('尚未取得 run_id，无法通知后端取消，将中断本地数据流。', 'cmd');
  }

  clearStopTimer();
  if (state.controller) {
    // 给后端 cancelled/done 事件一个到达窗口，超时再强制断开前端 fetch
    state.stopTimer = setTimeout(() => {
      state.stopTimer = null;
      if (state.controller) {
        state.controller.abort();
      }
    }, CANCEL_ABORT_GRACE_MS);
  } else {
    state.stopping = false;
    setRunning(false);
  }
}

/* --------------------------------------------------------------------------
 * 12. 结果数据归一化
 * ------------------------------------------------------------------------ */

/**
 * 结合模式相似度相关字段（v0.3 补充的真实字段）。
 * 后端可能在顶层 / properties / binding / docking 任意一层给出，故串联查找；
 * 缺失时一律返回 null / ''，由界面优雅降级。
 */
function extractSimilarityFields(sources) {
  const list = sources.filter(Boolean);
  return {
    maccs: toNumber(pickFrom(list, ['maccs_tanimoto', 'maccs_similarity', 'maccs'])),
    combined: toNumber(pickFrom(list, [
      'combined_similarity', 'combined_similarity_score', 'combined_sim', 'overall_similarity'
    ])),
    consistency: toNumber(pickFrom(list, [
      'structural_consistency', 'structural_consistency_score', 'consistency', 'structure_consistency'
    ])),
    pharmacophore: pickFrom(list, ['pharmacophore', 'pharmacophore_match', 'pharmacophore_matches']),
    anchorMatch: pickFrom(list, ['anchor_match', 'anchor_matched', 'anchor_match_score', 'anchor']),
    bindingHint: pickFrom(list, ['binding_mode_hint', 'hint', 'mode_hint']),
    mwDelta: toNumber(pickFrom(list, ['mw_delta', 'molecular_weight_delta', 'delta_mw'])),
    logpDelta: toNumber(pickFrom(list, ['logp_delta', 'logP_delta', 'delta_logp'])),
    tpsaDelta: toNumber(pickFrom(list, ['tpsa_delta', 'delta_tpsa']))
  };
}

function extractMolecule(raw) {
  const properties = raw.properties || {};
  const affinity = toNumber(pick(raw, ['affinity_kcal_mol', 'affinity', 'total', 'binding_affinity']));
  const similar = extractSimilarityFields([raw, properties]);
  return {
    index: toNumber(pick(raw, ['index'], undefined)),
    name: pick(raw, ['name'], '') || '',
    smiles: pick(raw, ['smiles'], '') || '',
    affinity: affinity,
    engine: pick(raw, ['engine'], '') || '',
    exhaustiveness: pick(raw, ['exhaustiveness'], '') || '',
    mw: toNumber(pick(properties, ['molecular_weight', 'mol_weight', 'mw', 'molecular_weight_g_mol'])),
    logp: toNumber(pick(properties, ['logP', 'logp', 'xlogp', 'crippen_logp'])),
    tpsa: toNumber(pick(properties, ['tpsa', 'TPSA', 'polar_surface_area'])),
    hbd: toNumber(pick(properties, ['hbd', 'h_bond_donors', 'num_h_donors'])),
    hba: toNumber(pick(properties, ['hba', 'h_bond_acceptors', 'num_h_acceptors'])),
    rotatable: toNumber(pick(properties, ['rotatable_bonds', 'rotatable', 'num_rotatable_bonds'])),
    aromatic: toNumber(pick(properties, ['aromatic_rings', 'num_aromatic_rings'])),
    formula: pick(properties, ['formula', 'molecular_formula'], '') || '',
    violations: toNumber(pick(properties, ['lipinski_violations', 'num_lipinski_violations'])),
    drugPass: pick(properties, ['drug_likeness_pass', 'drug_like', 'drug_likeness'], undefined),
    similarity: toNumber(pick(raw, ['similarity_to_positive_control', 'similarity', 'fingerprint_similarity'])),
    maccs: similar.maccs,
    combined: similar.combined,
    consistency: similar.consistency,
    pharmacophore: similar.pharmacophore,
    anchorMatch: similar.anchorMatch,
    mwDelta: similar.mwDelta,
    logpDelta: similar.logpDelta,
    tpsaDelta: similar.tpsaDelta,
    poseUrl: pick(raw, ['pose_url', 'pose'], '') || '',
    energyInter: toNumber(pick(raw, ['intermolecular', 'intermolecular_kcal_mol', 'inter'])),
    energyIntra: toNumber(pick(raw, ['intramolecular', 'intramolecular_kcal_mol', 'intra'])),
    energyTorsion: toNumber(pick(raw, ['torsional', 'torsional_kcal_mol', 'torsion'])),
    bindingHint: similar.bindingHint || '',
    source: 'live'
  };
}

function buildDockingLookup(result) {
  const lookup = new Map();
  const docking = result && result.docking ? result.docking : null;
  if (!docking) return lookup;
  const entries = [];
  const receptors = Array.isArray(docking.receptors) ? docking.receptors : [];
  receptors.forEach((receptor) => {
    const list = Array.isArray(receptor.results) ? receptor.results : [];
    list.forEach((item) => entries.push(Object.assign({ receptor_key: receptor.receptor_key }, item)));
    if (!list.length && (receptor.affinity !== undefined || receptor.name !== undefined)) {
      entries.push(receptor);
    }
  });
  if (!entries.length && Array.isArray(docking.results)) {
    docking.results.forEach((item) => entries.push(item));
  }
  entries.forEach((item) => {
    const similar = extractSimilarityFields([item]);
    const record = {
      affinity: toNumber(pick(item, ['affinity_kcal_mol', 'affinity', 'total', 'binding_affinity'])),
      engine: pick(item, ['engine'], '') || '',
      exhaustiveness: pick(item, ['exhaustiveness'], '') || '',
      inter: toNumber(pick(item, ['intermolecular', 'intermolecular_kcal_mol', 'inter'])),
      intra: toNumber(pick(item, ['intramolecular', 'intramolecular_kcal_mol', 'intra'])),
      torsion: toNumber(pick(item, ['torsional', 'torsional_kcal_mol', 'torsion'])),
      poseUrl: pick(item, ['pose_url', 'pose'], '') || '',
      name: pick(item, ['name', 'ligand', 'ligand_name'], '') || '',
      maccs: similar.maccs,
      combined: similar.combined,
      consistency: similar.consistency,
      pharmacophore: similar.pharmacophore,
      anchorMatch: similar.anchorMatch,
      bindingHint: similar.bindingHint || '',
      mwDelta: similar.mwDelta,
      logpDelta: similar.logpDelta,
      tpsaDelta: similar.tpsaDelta
    };
    if (record.name) lookup.set('name:' + record.name, record);
    const smiles = pick(item, ['smiles', 'canonical_smiles'], '');
    if (smiles) lookup.set('smiles:' + smiles, record);
  });
  return lookup;
}

function lookupDocking(lookup, name, smiles) {
  return lookup.get('name:' + name) || lookup.get('smiles:' + smiles) || null;
}

/**
 * /api/runs/{id}/ranking 返回的行 → 内部统一结构。
 * 字段可能出现在顶层或 properties 中，缺失一律优雅降级为 null / ''。
 */
function normalizeRankingRow(raw) {
  const item = raw || {};
  const properties = (item.properties && typeof item.properties === 'object') ? item.properties : {};
  const sources = [item, properties];
  const similar = extractSimilarityFields(sources);
  return {
    rank: toNumber(pick(item, ['rank'], undefined)),
    name: pick(item, ['name', 'ligand', 'molecule'], '') || '',
    smiles: pick(item, ['smiles', 'canonical_smiles'], '') || '',
    affinity: toNumber(pickFrom(sources, ['affinity_kcal_mol', 'affinity', 'total', 'binding_affinity'])),
    engine: pickFrom(sources, ['engine']) || '',
    exhaustiveness: pickFrom(sources, ['exhaustiveness']) || '',
    mw: toNumber(pickFrom(sources, ['molecular_weight', 'mol_weight', 'mw', 'molecular_weight_g_mol'])),
    logp: toNumber(pickFrom(sources, ['logP', 'logp', 'xlogp', 'crippen_logp'])),
    tpsa: toNumber(pickFrom(sources, ['tpsa', 'TPSA', 'polar_surface_area'])),
    hbd: toNumber(pickFrom(sources, ['hbd', 'h_bond_donors', 'num_h_donors'])),
    hba: toNumber(pickFrom(sources, ['hba', 'h_bond_acceptors', 'num_h_acceptors'])),
    rotatable: toNumber(pickFrom(sources, ['rotatable_bonds', 'rotatable', 'num_rotatable_bonds'])),
    aromatic: toNumber(pickFrom(sources, ['aromatic_rings', 'num_aromatic_rings'])),
    formula: pickFrom(sources, ['formula', 'molecular_formula']) || '',
    violations: toNumber(pickFrom(sources, ['lipinski_violations', 'num_lipinski_violations'])),
    drugPass: pickFrom([item, properties], ['drug_likeness_pass', 'drug_like', 'drug_likeness']),
    similarity: toNumber(pickFrom(sources, [
      'similarity_to_positive_control', 'similarity', 'fingerprint_similarity'
    ])),
    maccs: similar.maccs,
    combined: similar.combined,
    consistency: similar.consistency,
    pharmacophore: similar.pharmacophore,
    anchorMatch: similar.anchorMatch,
    bindingHint: similar.bindingHint || '',
    mwDelta: similar.mwDelta,
    logpDelta: similar.logpDelta,
    tpsaDelta: similar.tpsaDelta,
    energyInter: toNumber(pickFrom(sources, ['intermolecular', 'intermolecular_kcal_mol', 'inter'])),
    energyIntra: toNumber(pickFrom(sources, ['intramolecular', 'intramolecular_kcal_mol', 'intra'])),
    energyTorsion: toNumber(pickFrom(sources, ['torsional', 'torsional_kcal_mol', 'torsion'])),
    poseUrl: pickFrom(sources, ['pose_url', 'pose']) || '',
    isPositive: false,
    source: 'run'
  };
}

/**
 * 阳性对照行：不再依赖内联 ranking，而是用 result.positive_control +
 * aggregates.positive_control_affinity 单独构造；始终高亮且不参与排序。
 */
function buildPositiveRow(result) {
  const res = result || {};
  const aggregates = (res.aggregates && typeof res.aggregates === 'object') ? res.aggregates : {};
  const positive = res.positive_control || null;
  const pcAffinity = toNumber(pick(aggregates, ['positive_control_affinity'], undefined));
  const hasIdentity = Boolean(positive && (positive.name || positive.smiles));
  if (!hasIdentity && pcAffinity === null) return null;
  const row = normalizeRankingRow(positive || {});
  if (!row.name) row.name = POSITIVE_ROLE;
  if (row.affinity === null) row.affinity = pcAffinity;
  row.rank = null;
  row.isPositive = true;
  if (row.similarity === null) row.similarity = 1;
  if (row.mwDelta === null) row.mwDelta = 0;
  if (row.logpDelta === null) row.logpDelta = 0;
  if (row.tpsaDelta === null) row.tpsaDelta = 0;
  if (!row.bindingHint) row.bindingHint = '阳性对照（相似度基准）';
  return row;
}

/** 当前页是否已包含该阳性对照（避免同一行重复出现） */
function positiveInRows(rows, positiveRow) {
  if (!positiveRow) return false;
  return (rows || []).some((row) => {
    if (positiveRow.smiles && row.smiles && row.smiles === positiveRow.smiles) return true;
    return Boolean(positiveRow.name && row.name && row.name === positiveRow.name);
  });
}

/**
 * 阳性对照可选（v0.5）：留空时后端跳过对照分子对接与结合模式比较，
 * 界面同步隐藏「与对照相似度 / MACCS / 综合相似度 / 结构一致性」空壳列、
 * 对照高亮行图例与「只看优于对照」筛选，避免出现一整列「—」。
 */
function applyPositiveVisibility() {
  const has = Boolean(state.positiveRow);
  const resultTable = $('result-table');
  if (resultTable) resultTable.classList.toggle('no-positive', !has);
  const liveTable = $('molecules-table');
  if (liveTable) liveTable.classList.toggle('no-positive', !has);
  const legend = $('legend-positive');
  if (legend) legend.classList.toggle('hidden', !has);
  const hits = $('hits-only');
  if (hits) {
    hits.disabled = !has;
    if (!has && hits.checked) {
      hits.checked = false;
      state.ranking.hitsOnly = false;
    }
  }
}

/* --------------------------------------------------------------------------
 * 13. 结果总览：服务端分页 + 排序 + 搜索（offset / limit / sort / order / q / hits_only）
 *     上万分子时只渲染当前页，绝不再一次性创建上万个 DOM 节点。
 * ------------------------------------------------------------------------ */

/** 构造 /ranking 查询串，参数名与 docs/api.md 第 7.4 节契约完全一致 */
function rankingUrl(offset, limit) {
  const params = new URLSearchParams();
  params.set('offset', String(Math.max(0, Math.round(offset))));
  params.set('limit', String(Math.max(1, Math.round(limit))));
  params.set('sort', state.ranking.sort || DEFAULT_RANKING_SORT);
  params.set('order', state.ranking.order === 'desc' ? 'desc' : 'asc');
  const query = state.ranking.query.trim();
  if (query) params.set('q', query);
  if (state.ranking.hitsOnly) params.set('hits_only', 'true');
  return '/api/runs/' + encodeURIComponent(state.runId) + '/ranking?' + params.toString();
}

function rankingTotalPages() {
  const limit = state.ranking.limit || 50;
  return Math.max(1, Math.ceil((state.ranking.total || 0) / limit));
}

function cardsTotalPages() {
  const limit = state.cards.limit || 12;
  return Math.max(1, Math.ceil((state.cards.total || 0) / limit));
}

function rankingCurrentPage() {
  return Math.min(rankingTotalPages(),
    Math.floor(state.ranking.offset / (state.ranking.limit || 50)) + 1);
}

function cardsCurrentPage() {
  return Math.min(cardsTotalPages(),
    Math.floor(state.cards.offset / (state.cards.limit || 12)) + 1);
}

function setDisabled(node, disabled) {
  if (node) node.disabled = Boolean(disabled);
}

/** 排序标记：由服务端排序状态驱动（不是只排当前页） */
function renderSortMarks() {
  document.querySelectorAll('#result-table thead th[data-key]').forEach((th) => {
    const mark = th.querySelector('.sort-mark');
    if (!mark) return;
    mark.textContent = (state.ranking.sort === th.dataset.key)
      ? (state.ranking.order === 'desc' ? '▼' : '▲')
      : '';
  });
}

/**
 * 载入结果总览某一页。seq 递增用于丢弃过期响应，
 * 快速翻页 / 连续搜索时不会出现旧响应覆盖新页的「串页」。
 */
async function loadRankingPage(options) {
  const opts = options || {};
  const runId = state.runId;
  if (!runId) {
    state.ranking.rows = [];
    state.ranking.total = 0;
    renderRanking();
    return;
  }
  const offset = Math.max(0, opts.offset !== undefined ? opts.offset : state.ranking.offset);
  const limit = state.ranking.limit;
  const seq = ++state.ranking.seq;
  state.ranking.loading = true;
  state.ranking.error = '';
  state.ranking.offset = offset;   // 乐观更新：先显示目标页码，旧行保持可见避免闪烁
  renderRanking();
  try {
    const data = await getJson(rankingUrl(offset, limit));
    if (seq !== state.ranking.seq || runId !== state.runId) return;
    const rows = Array.isArray(data.rows) ? data.rows.map(normalizeRankingRow) : [];
    const total = toNumber(data.total);
    const serverOffset = toNumber(data.offset);
    state.ranking.rows = rows;
    state.ranking.total = total !== null ? Math.max(0, Math.round(total)) : rows.length;
    state.ranking.limit = toNumber(data.limit) || limit;
    state.ranking.offset = serverOffset !== null ? Math.max(0, Math.round(serverOffset)) : offset;
    if (data.aggregates && typeof data.aggregates === 'object') state.ranking.aggregates = data.aggregates;
    state.ranking.loading = false;
    renderRanking();
  } catch (error) {
    if (seq !== state.ranking.seq || runId !== state.runId) return;
    state.ranking.loading = false;
    state.ranking.error = shortError(error);
    state.ranking.rows = [];
    renderRanking();
    logLine('载入排序结果失败：' + state.ranking.error, 'err');
  }
}

/** 载入分子详情某一页（复用同一个 /ranking 接口，只渲染当前页卡片） */
async function loadCardsPage(options) {
  const opts = options || {};
  const runId = state.runId;
  if (!runId) {
    state.cards.rows = [];
    state.cards.total = 0;
    renderCards();
    return;
  }
  const offset = Math.max(0, opts.offset !== undefined ? opts.offset : state.cards.offset);
  const limit = state.cards.limit;
  const seq = ++state.cards.seq;
  state.cards.loading = true;
  state.cards.error = '';
  state.cards.offset = offset;     // 乐观更新页码，旧卡片保持可见避免闪烁
  renderCards();
  try {
    const data = await getJson(rankingUrl(offset, limit));
    if (seq !== state.cards.seq || runId !== state.runId) return;
    const rows = Array.isArray(data.rows) ? data.rows.map(normalizeRankingRow) : [];
    const total = toNumber(data.total);
    const serverOffset = toNumber(data.offset);
    state.cards.rows = rows;
    state.cards.total = total !== null ? Math.max(0, Math.round(total)) : rows.length;
    state.cards.limit = toNumber(data.limit) || limit;
    state.cards.offset = serverOffset !== null ? Math.max(0, Math.round(serverOffset)) : offset;
    state.cards.loading = false;
    renderCards();
  } catch (error) {
    if (seq !== state.cards.seq || runId !== state.runId) return;
    state.cards.loading = false;
    state.cards.error = shortError(error);
    state.cards.rows = [];
    renderCards();
  }
}

/** 排序 / 搜索 / hits_only / 每页条数变化后，两张视图都从第一页重新拉取 */
function reloadResultViews() {
  state.ranking.offset = 0;
  state.cards.offset = 0;
  if (!state.runId) return;
  loadRankingPage({ offset: 0 });
  loadCardsPage({ offset: 0 });
}

/** 页面顶部聚合统计（服务端 aggregates：命中数 / 亲和力区间 / 均值 / 中位数） */
function renderAggregates() {
  const box = $('aggregates-bar');
  if (!box) return;
  clear(box);
  const agg = state.ranking.aggregates;
  if (!agg && !state.ranking.total) { hide(box); return; }
  show(box);
  const affinity = (agg && agg.affinity && typeof agg.affinity === 'object') ? agg.affinity : {};
  const range = (toNumber(affinity.min) === null && toNumber(affinity.max) === null)
    ? '—'
    : (fmtNum(affinity.min, 2) + ' ~ ' + fmtNum(affinity.max, 2) + ' kcal/mol');
  const hasPositive = Boolean(state.positiveRow) ||
    (agg && toNumber(pick(agg, ['positive_control_affinity'], undefined)) !== null);
  const items = [
    ['总分子数', fmtInt(agg && agg.total !== undefined ? agg.total : state.ranking.total)],
    ['亲和力区间', range],
    ['平均亲和力', fmtNum(affinity.mean, 2)],
    ['中位亲和力', fmtNum(affinity.median, 2)],
    ['引擎', fmtText(agg ? agg.engine : '')],
    ['exhaustiveness', fmtText(agg ? agg.exhaustiveness : '')]
  ];
  /* 未提供阳性对照时，不显示「命中数 / 阳性对照亲和力」这两个空壳指标 */
  if (hasPositive) {
    items.splice(1, 0, ['命中数（优于对照）', fmtInt(agg ? agg.hits : undefined)]);
    items.splice(5, 0, ['阳性对照亲和力', fmtNum(agg ? agg.positive_control_affinity : undefined, 2)]);
  }
  items.forEach(([label, value]) => {
    const stat = el('div', 'stat');
    stat.appendChild(el('span', 'stat-label', label));
    stat.appendChild(el('span', 'stat-value', String(value)));
    box.appendChild(stat);
  });
}

/** 结果总览整体重绘（当前页 + 阳性对照行 + 分页控件 + 聚合统计） */
function renderRanking() {
  const tbody = $('result-tbody');
  clear(tbody);
  const rows = state.ranking.rows;
  const fragment = document.createDocumentFragment();
  rows.forEach((row, index) => fragment.appendChild(resultRowNode(row, state.ranking.offset + index)));
  const positive = state.positiveRow;
  if (positive && !positiveInRows(rows, positive)) {
    fragment.appendChild(resultRowNode(positive, state.ranking.offset + rows.length));
  }
  /* 未提供阳性对照：不显示相似度空壳列与对照高亮行，改为一行明确提示 */
  if (state.runId && !positive && rows.length) {
    const noteRow = el('tr', 'no-pc-row');
    const noteCell = el('td', 'no-pc-note', '本次未提供阳性对照，未做对照分析。');
    noteCell.colSpan = 9;
    noteRow.appendChild(noteCell);
    fragment.appendChild(noteRow);
  }
  tbody.appendChild(fragment);
  applyPositiveVisibility();

  renderAggregates();
  renderSortMarks();

  const total = state.ranking.total;
  const pages = rankingTotalPages();
  const page = rankingCurrentPage();
  $('pager-info').textContent = '共 ' + fmtInt(total) + ' 条，第 ' + page + '/' + pages + ' 页';
  setDisabled($('pager-prev'), page <= 1);
  setDisabled($('pager-next'), page >= pages);
  const jump = $('pager-jump');
  jump.max = String(pages);
  jump.value = String(page);

  const hasData = rows.length > 0 || total > 0 || Boolean(positive);
  const empty = $('overview-empty');
  empty.classList.toggle('hidden', hasData);
  if (!hasData) {
    empty.textContent = state.runId
      ? '本次运行没有可展示的排序结果（可能所有分子都未成功对接）。完整信息见「中间数据」。'
      : '还没有结果：在上方输入指令开始一次筛选，或到「历史运行」载入既往记录。';
  }

  const hint = $('ranking-hint');
  if (hint) {
    if (!state.runId) hint.textContent = '尚未载入排序结果。';
    else if (state.ranking.loading) hint.textContent = '正在从服务端载入第 ' + page + ' 页…';
    else if (state.ranking.error) hint.textContent = '载入失败：' + state.ranking.error;
    else {
      const filters = [];
      if (state.ranking.query) filters.push('搜索「' + state.ranking.query + '」');
      if (state.ranking.hitsOnly) filters.push('只看优于阳性对照');
      hint.textContent = '已载入第 ' + page + '/' + pages + ' 页（本页 ' + fmtInt(rows.length) +
        ' 条），全库共 ' + fmtInt(total) + ' 条；每页 ' + fmtInt(state.ranking.limit) +
        ' 条 · 服务端排序 ' + state.ranking.sort + ' ' + state.ranking.order +
        (filters.length ? ' · 过滤：' + filters.join(' / ') : '');
    }
  }
}

function drugCell(value) {
  if (value === undefined || value === null) return el('span', 'badge badge-mute', '—');
  const pass = value === true || value === 'true' || value === 1 || value === 'pass' || value === 'yes';
  return el('span', 'badge ' + (pass ? 'badge-ok' : 'badge-no'), pass ? '合格' : '不合规');
}

function resultRowNode(row, displayIndex) {
  const tr = el('tr', row.isPositive ? 'highlight-pc' : null);
  const rankCell = el('td', 'num');
  if (row.isPositive) rankCell.textContent = '对照';
  else {
    rankCell.textContent = fmtInt(row.rank !== null && row.rank !== undefined ? row.rank : displayIndex + 1);
    if (toNumber(row.rank) === 1) rankCell.classList.add('rank-1');
  }
  tr.appendChild(rankCell);
  const nameCell = el('td', 'name-cell',
    fmtText(row.name) + (row.isPositive ? '（阳性对照）' : ''), row.name);
  // 大配体组：盒子与主组不同，分数不跨组比较 —— 行内明确标出，避免误读排序
  const group = row.box_group || row.boxGroup || '';
  if (group === 'large') {
    const chip = el('span', 'badge badge-warn', '大配体组');
    chip.title = '该分子用了更大的对接盒（' + (row.box_size || '—')
      + '），与主组盒子不同，分数不跨组比较';
    nameCell.appendChild(document.createTextNode(' '));
    nameCell.appendChild(chip);
  }
  tr.appendChild(nameCell);

  const affCell = el('td', 'num aff-cell ' + affinityClass(row.affinity));
  affCell.textContent = fmtNum(row.affinity, 2);
  tr.appendChild(affCell);

  tr.appendChild(el('td', null, fmtText(row.engine)));
  tr.appendChild(el('td', 'num', fmtText(row.exhaustiveness)));
  tr.appendChild(el('td', 'num', fmtNum(row.mw, 2)));
  tr.appendChild(el('td', 'num', fmtNum(row.logp, 2)));
  tr.appendChild(el('td', 'num', fmtNum(row.tpsa, 2)));
  const drugTd = el('td', 'num');
  drugTd.appendChild(drugCell(row.drugPass));
  tr.appendChild(drugTd);
  tr.appendChild(el('td', 'num', fmtNum(row.similarity, 3)));
  tr.appendChild(el('td', 'num', fmtNum(row.maccs, 3)));
  tr.appendChild(el('td', 'num', fmtNum(row.combined, 3)));
  tr.appendChild(el('td', 'num', fmtNum(row.consistency, 3)));
  return tr;
}

function renderSummary(run, moleculeCount) {
  const box = $('run-summary');
  clear(box);
  if (!run) return;
  const items = [
    ['run_id', run.run_id || state.runId || '—'],
    ['模式', run.kind === 'agent' ? '多 Agent 协作' : (run.kind === 'pipeline' ? '确定性流水线' : fmtText(run.kind))],
    ['受体', run.receptor_label || run.receptor || '—'],
    ['引擎', run.engine || ($('engine-select').value)],
    ['分子数', fmtInt(run.molecule_count !== undefined ? run.molecule_count : moleculeCount)],
    ['状态', run.status === 'cancelled' ? '已取消 (cancelled)' : (run.status || '—')],
    ['开始时间', fmtTime(run.created_at)],
    ['结束时间', fmtTime(run.finished_at)],
    ['用时', run.duration_sec !== undefined ? fmtDuration(run.duration_sec) : '—'],
    ['产物数', fmtInt(run.artifact_count)]
  ];
  if (Array.isArray(run.top) && run.top.length) {
    const top = run.top[0];
    items.push(['最佳分子', fmtText(top.name) + ' (' + fmtNum(top.affinity_kcal_mol, 2) + ' kcal/mol)']);
  }
  if (run.site && Array.isArray(run.site.center) && run.site.center.length) {
    items.push(['结合位点中心', run.site.center.map((v) => fmtNum(v, 2)).join(', ')]);
  }
  /* 备注不再整段进汇总表：超长备注会把网页拉得很长（用户实测反馈）。
     这里只报条数，正文交给下方的 #run-notes（每条最多 2 行 + 展开）。 */
  if (run.notes && run.notes.length) items.push(['运行笔记', run.notes.length + ' 条（见下方）']);
  if (run.error) items.push(['错误', String(run.error)]);
  items.forEach(([label, value]) => {
    const stat = el('div', 'stat');
    stat.appendChild(el('span', 'stat-label', label));
    stat.appendChild(el('span', 'stat-value', String(value)));
    box.appendChild(stat);
  });
}

/** 编排面板上的「对接盒」信息行：显示实际使用的盒子中心/尺寸与来源 */
/** 运行笔记：每条默认只显示 2 行，点「展开」看全文（超长备注不再拉长页面）。 */
function renderRunNotes(run) {
  const box = $('run-notes');
  if (!box) return;
  clear(box);
  const notes = Array.isArray(run && run.notes)
    ? run.notes.filter((note) => String(note || '').trim()) : [];
  if (!notes.length) {
    box.classList.add('hidden');
    return;
  }
  const head = el('div', 'run-notes-head');
  head.appendChild(el('span', 'run-notes-title', '运行笔记'));
  head.appendChild(el('span', 'hint-inline', notes.length + ' 条 · 点「展开」看全文'));
  box.appendChild(head);
  notes.forEach((note) => {
    const row = el('div', 'run-note');
    const text = el('p', 'run-note-text', String(note));
    const toggle = el('button', 'run-note-toggle', '展开');
    toggle.type = 'button';
    toggle.addEventListener('click', () => {
      const open = text.classList.toggle('is-open');
      toggle.textContent = open ? '收起' : '展开';
    });
    row.appendChild(text);
    row.appendChild(toggle);
    box.appendChild(row);
  });
  box.classList.remove('hidden');
}

function renderBoxLine(receptorBlocks) {
  const node = $('orch-box');
  if (!node) return;
  const block = (receptorBlocks || [])[0];
  if (!block || !block.box_center) {
    node.textContent = '对接盒：待确定（未给出显式位点时会用工具预测口袋）';
    return;
  }
  const source = block.box_source || (block.site || {}).source || '—';
  node.textContent = '对接盒：中心 [' + (block.box_center || []).join(', ') + '] · 尺寸 ['
    + (block.box_size || []).join(', ') + '] · 来源：' + source;
  node.title = source;
}

/** 结合位点与口袋预测：把工具输出的候选口袋列出来，并标出被采用的那个 */
function renderPocketAnalysis(payload) {
  const stats = $('pocket-stats');
  const tbody = $('pocket-tbody');
  const empty = $('pocket-empty');
  const wrap = $('pocket-table-wrap');
  if (!stats || !tbody) return;
  clear(stats);
  clear(tbody);
  const blocks = ((payload || {}).result || {}).docking
    ? (((payload.result.docking || {}).receptors) || []) : [];
  let block = null;
  for (const item of blocks) {
    if ((item.pockets || []).length || (item.site || {}).pockets) { block = item; break; }
  }
  const pockets = block ? (block.pockets || (block.site || {}).pockets || []) : [];
  if (!block || !pockets.length) {
    hide(wrap);
    show(empty);
    stats.appendChild(el('span', 'collab-stat', '未做口袋预测（可能显式指定了位点，或使用了 known_site 引擎）'));
    return;
  }
  show(wrap);
  hide(empty);
  const site = block.site || {};
  const engine = site.engine || (block.pockets || []).engine || '—';
  const chosen = (site.pocket || {}).name || '';
  const validation = block.box_validation || {};
  const items = [
    ['预测引擎', engine],
    ['候选口袋数', pockets.length],
    ['实际采用', chosen || '—'],
    ['盒子来源', block.box_source || site.source || '—'],
  ];
  if (validation && validation.status) {
    const verdict = validation.status === 'consistent' ? '与实验位点一致'
      : (validation.status === 'inconsistent' ? '与实验位点不一致' : '无参考位点');
    items.push(['实验位点比对', verdict + (validation.distance_angstrom != null
      ? '（相距 ' + validation.distance_angstrom + ' Å）' : '')]);
  }
  items.forEach(([label, value]) => {
    const node = el('span', 'collab-stat');
    node.appendChild(el('span', 'collab-stat-label', label));
    node.appendChild(el('span', 'collab-stat-value mono', String(value)));
    stats.appendChild(node);
  });
  pockets.slice(0, 12).forEach((pocket) => {
    const tr = document.createElement('tr');
    const cells = [
      String(pocket.rank == null ? '' : pocket.rank),
      String(pocket.name || ''),
      pocket.score == null ? '—' : String(pocket.score),
      (pocket.center || []).join(', '),
      (pocket.extent || []).join(' × '),
      (pocket.residues || []).slice(0, 6).join(' '),
      (pocket.name && pocket.name === chosen) ? '← 采用' : ''
    ];
    cells.forEach((text, index) => {
      const td = document.createElement('td');
      if (index === 0 || index === 2 || index === 3 || index === 4) td.className = 'num';
      td.textContent = text;
      tr.appendChild(td);
    });
    if (pocket.name && pocket.name === chosen) tr.classList.add('row-hit');
    tbody.appendChild(tr);
  });
}

function renderCollaboration() {
  const c = (state.collaboration && typeof state.collaboration === 'object') ? state.collaboration : {};
  const stats = (c.stats && typeof c.stats === 'object') ? c.stats : {};
  const notes = Array.isArray(c.notes) ? c.notes.filter((note) => !isBlank(note)) : [];
  const statsBox = $('collab-stats');
  const notesBox = $('collab-notes');
  const empty = $('collab-empty');
  clear(statsBox);
  clear(notesBox);

  const definitions = [['分子', stats.molecules], ['性质', stats.properties],
    ['对接', stats.docking], ['结合模式', stats.binding]];
  const hasStats = definitions.some(([, value]) => toNumber(value) !== null);
  if (hasStats) {
    definitions.forEach(([label, value]) => {
      const row = el('div', 'collab-stat');
      row.appendChild(el('span', 'collab-stat-label', label));
      row.appendChild(el('span', 'collab-stat-value mono', fmtInt(value)));
      statsBox.appendChild(row);
    });
  }
  notes.forEach((note) => {
    const row = el('div', 'collab-note');
    row.appendChild(el('span', 'collab-caret', '›'));
    row.appendChild(el('span', 'collab-note-text', String(note)));
    notesBox.appendChild(row);
  });
  statsBox.classList.toggle('hidden', !hasStats);
  notesBox.classList.toggle('hidden', notes.length === 0);

  const hasData = hasStats || notes.length > 0;
  const kind = state.currentRun && state.currentRun.kind ? String(state.currentRun.kind) : '';
  const isAgent = kind === 'agent' || (!kind && state.mode === 'agent');
  empty.textContent = isAgent
    ? '本次运行没有产生多 Agent 协作记录（共享黑板为空）。'
    : '确定性流水线不产生多 Agent 协作记录。';
  empty.classList.toggle('hidden', hasData);
}

/* --------------------------------------------------------------------------
 * 14. 图表与中间数据
 * ------------------------------------------------------------------------ */
function looksLikeChart(artifact) {
  const type = String(artifact.content_type || '');
  const path = String(artifact.path || artifact.name || '');
  return type.startsWith('image/') || /\.(png|jpe?g|svg|webp|gif)$/i.test(path);
}


/** 图表排序：docking_chart / similarity_chart / affinity_histogram / property_chart 优先展示 */
function chartRank(artifact) {
  const name = String(artifact.name || '');
  const index = CHART_ORDER.indexOf(name);
  return index === -1 ? CHART_ORDER.length : index;
}

function renderCharts() {
  const box = $('charts-box');
  clear(box);
  // 大库时服务端已自动把 docking_chart / similarity_chart 切为 Top-20 + 分布直方图，
  // 并可能新增 affinity_histogram；这里按固定顺序展示全部图片类产物。
  const charts = state.artifacts
    .filter((artifact) => looksLikeChart(artifact) && artifact.inline_url)
    .slice()
    .sort((a, b) => chartRank(a) - chartRank(b));
  if (!charts.length) {
    box.appendChild(el('p', 'empty', '尚无图表产物。运行完成后，服务端产出的对接对比图、相似度图与亲和力分布直方图会显示在这里。'));
    return;
  }
  charts.forEach((artifact) => {
    const card = el('figure', 'chart-card');
    card.appendChild(el('figcaption', null, fmtText(artifact.label || artifact.name)));
    const img = el('img');
    img.src = artifact.inline_url;
    img.alt = fmtText(artifact.label || artifact.name);
    img.loading = 'lazy';
    img.decoding = 'async';
    card.appendChild(img);
    if (artifact.download_url) {
      const links = el('div', 'chart-links');
      const link = el('a', 'btn btn-ghost btn-sm', '下载图片');
      link.href = artifact.download_url;
      links.appendChild(link);
      card.appendChild(links);
    }
    box.appendChild(card);
  });
}

function renderArtifacts() {
  const grid = $('artifact-grid');
  clear(grid);
  const artifacts = state.artifacts;
  $('artifacts-empty').classList.toggle('hidden', artifacts.length > 0);
  artifacts.forEach((artifact, index) => {
    const card = motionIn(el('div', 'artifact-card'), Math.min(240, index * 20));
    card.appendChild(el('div', 'artifact-name', fmtText(artifact.label || artifact.name)));
    const meta = el('div', 'artifact-meta', (artifact.content_type || '未知类型') + ' · ' + fmtSize(artifact.size));
    card.appendChild(meta);
    if (artifact.path) card.appendChild(el('div', 'artifact-path', '路径：' + artifact.path, artifact.path));
    if (looksLikeChart(artifact) && artifact.inline_url) {
      const img = el('img', 'artifact-thumb');
      img.src = artifact.inline_url;
      img.alt = fmtText(artifact.label || artifact.name);
      img.loading = 'lazy';
      card.appendChild(img);
    }
    const actions = el('div', 'artifact-actions');
    if (artifact.download_url) {
      const download = el('a', 'btn btn-ghost btn-sm', '下载');
      download.href = artifact.download_url;
      download.setAttribute('download', '');
      actions.appendChild(download);
      if (looksLikeChart(artifact) && artifact.inline_url) {
        const inline = el('a', 'btn btn-ghost btn-sm', '新窗口查看');
        inline.href = artifact.inline_url;
        inline.target = '_blank';
        inline.rel = 'noopener';
        actions.appendChild(inline);
      }
    } else {
      actions.appendChild(el('span', 'hint', '无下载地址'));
    }
    card.appendChild(actions);
    grid.appendChild(card);
  });

  const runId = state.runId;
  const zipAll = $('btn-zip-all');
  const zipPoses = $('btn-zip-poses');
  if (runId) {
    zipAll.href = '/api/runs/' + encodeURIComponent(runId) + '/download.zip';
    zipAll.classList.remove('disabled');
    zipAll.setAttribute('aria-disabled', 'false');
    zipPoses.href = '/api/runs/' + encodeURIComponent(runId) + '/poses.zip';
    zipPoses.classList.remove('disabled');
    zipPoses.setAttribute('aria-disabled', 'false');
    $('zip-hint').textContent = '当前运行：' + runId;
  } else {
    zipAll.href = '#';
    zipAll.classList.add('disabled');
    zipAll.setAttribute('aria-disabled', 'true');
    zipPoses.href = '#';
    zipPoses.classList.add('disabled');
    zipPoses.setAttribute('aria-disabled', 'true');
    $('zip-hint').textContent = '先运行或载入一次记录，才能打包下载。';
  }
}

async function downloadPosesZip(event) {
  event.preventDefault();
  const runId = state.runId;
  const message = $('zip-message');
  if (!runId) return;
  if (event.currentTarget.classList.contains('disabled')) return;
  try {
    const response = await fetch(event.currentTarget.href, { method: 'GET' });
    if (response.status === 404) {
      const text = await response.text();
      let detail = '';
      try {
        const parsed = JSON.parse(text);
        detail = parsed.error_message || '';
      } catch (error) {
        detail = text.slice(0, 200);
      }
      message.className = 'alert alert-warn';
      message.textContent = '本次运行没有可下载的位姿文件（服务端返回 404）。' +
        (detail ? '后端说明：' + detail : '请在配置中勾选「保存对接位姿文件」后重新运行。');
      show(message);
      return;
    }
    if (!response.ok) {
      const text = await response.text();
      let detail = '';
      try {
        const parsed = JSON.parse(text);
        detail = parsed.error_message || '';
      } catch (error) {
        detail = text.slice(0, 200);
      }
      message.className = 'alert alert-error';
      message.textContent = '下载位姿压缩包失败：HTTP ' + response.status + (detail ? '：' + detail : '');
      show(message);
      return;
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = 'poses_' + runId + '.zip';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 4000);
    message.className = 'alert alert-info';
    message.textContent = '位姿压缩包已开始下载。';
    show(message);
  } catch (error) {
    message.className = 'alert alert-error';
    message.textContent = '下载位姿压缩包失败：' + shortError(error);
    show(message);
  }
}

/* --------------------------------------------------------------------------
 * 15. 分子详情卡片
 * ------------------------------------------------------------------------ */
function infoItem(key, value) {
  const item = el('div', 'info-item');
  item.appendChild(el('span', 'info-key', key));
  item.appendChild(el('span', 'info-val', value));
  return item;
}

function moleculeCard(molecule) {
  const card = el('div', 'card');
  const head = el('div', 'card-head');
  head.appendChild(el('span', 'card-title', fmtText(molecule.name) +
    (molecule.isPositive ? '（阳性对照）' : '')));
  const sub = el('span', 'card-sub', '排名 ' + fmtText(molecule.rank) +
    ' · ' + fmtNum(molecule.affinity, 2) + ' kcal/mol' +
    (molecule.engine ? ' · ' + molecule.engine : ''));
  head.appendChild(sub);
  const chevron = el('span', 'card-chevron', '▾');
  head.appendChild(chevron);
  card.appendChild(head);
  head.addEventListener('click', () => {
    card.classList.toggle('collapsed');
    chevron.textContent = card.classList.contains('collapsed') ? '▸' : '▾';
  });

  const body = el('div', 'card-body');

  const depictBox = el('div', 'depict-box');
  if (molecule.smiles) {
    const img = el('img');
    img.src = '/api/molecule/depict?smiles=' + encodeURIComponent(molecule.smiles) + '&width=320&height=240';
    img.alt = fmtText(molecule.name) + ' 二维结构';
    img.loading = 'lazy';
    img.decoding = 'async';
    img.width = 320;
    img.height = 240;
    depictBox.appendChild(img);
    const smilesLine = el('div', 'depict-smiles', molecule.smiles, molecule.smiles);
    depictBox.appendChild(smilesLine);
  } else {
    depictBox.appendChild(el('p', 'empty', '缺少 SMILES，无法绘制二维结构。'));
  }
  body.appendChild(depictBox);

  const infoCol = el('div');
  const grid = el('div', 'info-grid');
  grid.appendChild(infoItem('分子式', fmtText(molecule.formula)));
  grid.appendChild(infoItem('分子量', fmtNum(molecule.mw, 2) + ' g/mol'));
  grid.appendChild(infoItem('LogP', fmtNum(molecule.logp, 2)));
  grid.appendChild(infoItem('TPSA', fmtNum(molecule.tpsa, 2) + ' Å²'));
  grid.appendChild(infoItem('氢键供体 (HBD)', fmtInt(molecule.hbd)));
  grid.appendChild(infoItem('氢键受体 (HBA)', fmtInt(molecule.hba)));
  grid.appendChild(infoItem('可旋转键', fmtInt(molecule.rotatable)));
  grid.appendChild(infoItem('芳香环', fmtInt(molecule.aromatic)));
  grid.appendChild(infoItem('Lipinski 违例', fmtInt(molecule.violations)));
  const drug = molecule.drugPass;
  grid.appendChild(infoItem('类药性', drug === undefined || drug === null
    ? '—' : (drug === true || drug === 'true' || drug === 1 ? '合格' : '不合规')));
  infoCol.appendChild(grid);

  const energyGrid = el('div', 'info-grid');
  energyGrid.appendChild(infoItem('亲和力 total', fmtNum(molecule.affinity, 2) + ' kcal/mol'));
  energyGrid.appendChild(infoItem('intermolecular', fmtNum(molecule.energyInter, 2)));
  energyGrid.appendChild(infoItem('intramolecular', fmtNum(molecule.energyIntra, 2)));
  energyGrid.appendChild(infoItem('torsional', fmtNum(molecule.energyTorsion, 2)));
  energyGrid.appendChild(infoItem('引擎', fmtText(molecule.engine)));
  energyGrid.appendChild(infoItem('exhaustiveness', fmtText(molecule.exhaustiveness)));
  infoCol.appendChild(el('h4', 'sub-head', '对接能量明细'));
  infoCol.appendChild(energyGrid);

  if (!molecule.energyInter && !molecule.energyIntra && !molecule.energyTorsion) {
    infoCol.appendChild(el('p', 'hint', '后端未提供能量分项明细（仅给出总亲和力）。'));
  }

  // 结合模式与相似度明细（字段缺失时由 fmtNum / fmtStruct 优雅降级为「—」）
  if (state.positiveRow) {
    const bindingGrid = el('div', 'info-grid');
    bindingGrid.appendChild(infoItem('与阳性对照相似度', fmtNum(molecule.similarity, 3)));
    bindingGrid.appendChild(infoItem('MACCS Tanimoto', fmtNum(molecule.maccs, 3)));
    bindingGrid.appendChild(infoItem('综合相似度', fmtNum(molecule.combined, 3)));
    bindingGrid.appendChild(infoItem('结构一致性', fmtNum(molecule.consistency, 3)));
    infoCol.appendChild(el('h4', 'sub-head', '结合模式与相似度'));
    infoCol.appendChild(bindingGrid);
  } else {
    // 本次未提供阳性对照：不渲染整片「—」空壳，改为一行提示
    infoCol.appendChild(el('h4', 'sub-head', '结合模式与相似度'));
    infoCol.appendChild(el('p', 'hint', '本次未提供阳性对照，未做对照分析。'));
  }

  const haveDelta = molecule.mwDelta !== null && molecule.mwDelta !== undefined ||
    molecule.logpDelta !== null && molecule.logpDelta !== undefined ||
    molecule.tpsaDelta !== null && molecule.tpsaDelta !== undefined;
  if (haveDelta) {
    const deltaGrid = el('div', 'info-grid');
    deltaGrid.appendChild(infoItem('分子量差值 Δ', fmtSigned(molecule.mwDelta)));
    deltaGrid.appendChild(infoItem('LogP 差值 Δ', fmtSigned(molecule.logpDelta)));
    deltaGrid.appendChild(infoItem('TPSA 差值 Δ', fmtSigned(molecule.tpsaDelta)));
    infoCol.appendChild(el('h4', 'sub-head', '与阳性对照性质差异'));
    infoCol.appendChild(deltaGrid);
  }

  const haveMode = (molecule.pharmacophore !== undefined && molecule.pharmacophore !== null &&
      molecule.pharmacophore !== '') ||
    (molecule.anchorMatch !== undefined && molecule.anchorMatch !== null && molecule.anchorMatch !== '') ||
    Boolean(molecule.bindingHint);
  if (haveMode) {
    const modeGrid = el('div', 'info-grid mode-grid');
    if (molecule.anchorMatch !== undefined && molecule.anchorMatch !== null && molecule.anchorMatch !== '') {
      modeGrid.appendChild(infoItem('药效团锚定匹配 anchor_match', fmtStruct(molecule.anchorMatch)));
    }
    if (molecule.pharmacophore !== undefined && molecule.pharmacophore !== null && molecule.pharmacophore !== '') {
      modeGrid.appendChild(infoItem('药效团 pharmacophore', fmtStruct(molecule.pharmacophore)));
    }
    infoCol.appendChild(el('h4', 'sub-head', '结合模式分析'));
    infoCol.appendChild(modeGrid);
    if (molecule.bindingHint) {
      infoCol.appendChild(el('p', 'mode-hint', '结合模式提示：' + molecule.bindingHint));
    }
  } else {
    infoCol.appendChild(el('p', 'hint', '后端未提供结合模式分析字段（pharmacophore / anchor_match / binding_mode_hint）。'));
  }

  const foot = el('div', 'card-foot');
  if (molecule.poseUrl) {
    const poseLink = el('a', 'btn btn-ghost btn-sm', '下载位姿文件');
    poseLink.href = molecule.poseUrl;
    poseLink.setAttribute('download', '');
    foot.appendChild(poseLink);
  } else {
    foot.appendChild(el('span', 'hint', '无位姿文件（未保存位姿或后端未返回 pose_url）。'));
  }
  const propLink = document.createElement('a');
  if (molecule.smiles) {
    propLink.className = 'btn btn-ghost btn-sm';
    propLink.textContent = '查看理化性质 JSON';
    propLink.href = '/api/molecule/properties?smiles=' + encodeURIComponent(molecule.smiles);
    propLink.target = '_blank';
    propLink.rel = 'noopener';
    foot.appendChild(propLink);
  }
  infoCol.appendChild(foot);
  body.appendChild(infoCol);

  card.appendChild(body);
  return card;
}

function renderCards() {
  const box = $('cards-box');
  clear(box);
  const rows = state.cards.rows;
  const total = state.cards.total;
  const pages = cardsTotalPages();
  const page = cardsCurrentPage();

  // 只渲染当前页卡片；阳性对照卡片仅在第一页附加一次
  const list = rows.slice();
  if (state.cards.offset === 0 && state.positiveRow && !positiveInRows(rows, state.positiveRow)) {
    list.push(state.positiveRow);
  }
  const fragment = document.createDocumentFragment();
  list.forEach((molecule) => fragment.appendChild(moleculeCard(molecule)));
  box.appendChild(fragment);

  const hasData = total > 0 || Boolean(state.positiveRow);
  const empty = $('cards-empty');
  empty.classList.toggle('hidden', hasData);
  if (!hasData) {
    empty.textContent = state.runId
      ? '本次运行没有分子详情可展示。'
      : '尚未载入分子。先在左侧配置任务并点击「开始运行」。';
  }

  const pager = $('cards-pager');
  if (pager) pager.classList.toggle('hidden', !(total > 0));
  $('cards-pager-info').textContent = '共 ' + fmtInt(total) + ' 条，第 ' + page + '/' + pages +
    ' 页 · 本页 ' + fmtInt(rows.length) + ' 张（每页 ' + fmtInt(state.cards.limit) + ' 张）';
  setDisabled($('cards-prev'), page <= 1);
  setDisabled($('cards-next'), page >= pages);

  const hint = $('cards-hint');
  if (hint) {
    if (!state.runId) hint.textContent = '每页只渲染当前页的分子卡片（二维结构图懒加载），避免上万张图片同时请求。';
    else if (state.cards.loading) hint.textContent = '正在载入第 ' + page + ' 页分子卡片…';
    else if (state.cards.error) hint.textContent = '载入分子详情失败：' + state.cards.error;
    else hint.textContent = '当前第 ' + page + '/' + pages + ' 页 · 本页 ' + fmtInt(rows.length) +
      ' 张卡片（全库 ' + fmtInt(total) + ' 条）· 二维结构图懒加载。';
  }
}

/* --------------------------------------------------------------------------
 * 16. 报告
 * ------------------------------------------------------------------------ */
function renderReport(markdown) {
  const box = $('report-box');
  clear(box);
  const source = String(markdown || '');
  const sections = reportSections(source);
  if (!source) {
    renderReportToc(sections, []);
    box.classList.add('hidden');
    show($('report-empty'));
    return;
  }
  box.classList.remove('hidden');
  hide($('report-empty'));
  const body = renderMarkdown(resolveReportImages(source));
  box.appendChild(body);
  markReportHeadings(box);        // 先给正文标题打锚点，目录才能跳转
  renderReportToc(sections, reportHeaderMeta(source));
  motionIn(body);
}

/**
 * 报告图片用相对路径内嵌（`charts/<name>.png`），这里按本次 run 解析成产物接口的
 * 真实图片地址；界面上只显示图片本身，不出现任何地址文本。
 */
function resolveReportImages(markdown) {
  const runId = state.runId ? String(state.runId) : '';
  if (!runId) return markdown;
  return String(markdown).replace(/!\[([^\]]*)\]\((charts\/[^)\s]+\.png)\)/g,
    (match, alt, rel) => {
      const name = rel.slice('charts/'.length).replace(/\.png$/i, '');
      return '![' + alt + '](/api/runs/' + encodeURIComponent(runId)
        + '/artifacts/' + encodeURIComponent(name) + '?inline=1)';
    });
}

/** 从报告抬头表（表 1）提取头部摘要：run id / 生成时间 / 受体 / 引擎 / 种子 */
function reportHeaderMeta(markdown) {
  const wanted = ['运行编号', '生成时间', '受体', '对接引擎', '随机种子'];
  const found = {};
  String(markdown || '').split('\n').forEach((line) => {
    if (!line.trim().startsWith('|') || isTableSeparator(line)) return;
    const cells = splitTableRow(line);
    if (cells.length < 2) return;
    const label = String(cells[0] || '').replace(/[*`]/g, '').trim();
    if (wanted.indexOf(label) === -1) return;
    const value = String(cells[1] || '').replace(/[*`]/g, '').trim();
    if (value && !found[label]) found[label] = value;
  });
  return wanted.filter((key) => found[key]).map((key) => ({ label: key, value: found[key] }));
}

/** 抽出报告里的章节标题（## / ###），用于生成目录与"几节"提示 */
function reportSections(markdown) {
  const out = [];
  String(markdown || '').split('\n').forEach((line) => {
    const m = /^#{2,3}\s+(.+?)\s*$/.exec(line);
    if (m) out.push(m[1].replace(/[*`]/g, '').trim());
  });
  return out;
}

/** 给正文里的 h2/h3 按顺序打 data-report-section / id，供目录跳转 */
function markReportHeadings(root) {
  if (!root) return;
  root.querySelectorAll('h2, h3').forEach((head, index) => {
    head.dataset.reportSection = String(index + 1);
    head.id = 'report-section-' + (index + 1);
  });
}

/** 报告目录：固定章节可定位（规范报告用起来才顺手） */
function renderReportToc(sections, meta) {
  const host = $('report-toc');
  if (!host) return;
  clear(host);
  const chips = Array.isArray(meta) ? meta.filter((m) => m && m.value) : [];
  if (chips.length) {
    const head = el('div', 'report-head');
    chips.forEach((item) => {
      const cell = el('span', 'report-head-item');
      cell.appendChild(el('span', 'report-head-label mono', item.label));
      cell.appendChild(el('span', 'report-head-value mono', item.value));
      head.appendChild(cell);
    });
    host.appendChild(head);
  }
  const list = Array.isArray(sections) ? sections : [];
  if (list.length < 2) return;
  host.appendChild(el('span', 'report-toc-title mono', '// ' + fmtInt(list.length) + ' 节'));
  list.forEach((title, index) => {
    const link = el('a', 'report-toc-link', title);
    link.href = '#report-section-' + (index + 1);
    link.addEventListener('click', (event) => {
      event.preventDefault();
      const target = document.querySelector('[data-report-section="' + (index + 1) + '"]');
      if (target && typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start' });
      }
      host.querySelectorAll('.report-toc-link').forEach((n) => n.classList.remove('active'));
      link.classList.add('active');
    });
    host.appendChild(link);
  });
}

async function copyReport() {
  const hint = $('copy-report-hint');
  if (!state.reportMarkdown) {
    hint.textContent = '当前没有可复制的报告原文。';
    return;
  }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(state.reportMarkdown);
      hint.textContent = '已复制 Markdown 原文。';
    } else {
      hint.textContent = '当前环境不支持剪贴板 API，请手动选择报告文本。';
    }
  } catch (error) {
    hint.textContent = '复制失败：' + shortError(error);
  }
}

/* --------------------------------------------------------------------------
 * 17. Tab 切换与结果装载
 * ------------------------------------------------------------------------ */
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.tab === name);
  });
  document.querySelectorAll('.tab-pane').forEach((pane) => {
    pane.classList.toggle('active', pane.id === 'tab-' + name);
  });
}

/** 清空分页/过滤/排序状态（新运行或切换子页时调用） */
function resetRankingState() {
  state.ranking.offset = 0;
  state.ranking.total = 0;
  state.ranking.rows = [];
  state.ranking.aggregates = null;
  state.ranking.error = '';
  state.ranking.loading = false;
  state.ranking.query = '';
  state.ranking.hitsOnly = false;
  state.ranking.sort = DEFAULT_RANKING_SORT;
  state.ranking.order = 'asc';
  state.ranking.seq += 1;
  state.cards.offset = 0;
  state.cards.total = 0;
  state.cards.rows = [];
  state.cards.error = '';
  state.cards.loading = false;
  state.cards.seq += 1;
  state.positiveRow = null;
}

function clearRunResults() {
  resetRankingState();
  state.artifacts = [];
  state.reportMarkdown = '';
  state.verification = {};
  state.collaboration = {};
  state.currentRun = null;
  const search = $('ranking-search');
  if (search) search.value = '';
  const hits = $('hits-only');
  if (hits) hits.checked = false;
  renderRanking();
  renderCards();
  renderReport('');
  renderArtifacts();
  renderCharts();
  clear($('run-summary'));
  renderBoxLine([]);
  renderPocketAnalysis(null);
  renderCollaboration();
  applyPositiveVisibility();
  updateExportLink();
}

/** 「导出完整 CSV」按钮指向 /api/runs/{id}/export.csv（不受分页限制） */
/* --------------------------------------------------------------------------
 * 8.6 动效助手：数字滚动 / 元素入场（尊重 prefers-reduced-motion）
 * ------------------------------------------------------------------------ */
function prefersReducedMotion() {
  try {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (error) {
    return false;
  }
}

/** 数字滚动：把元素文本从当前值平滑过渡到目标值（用于 KPI / 计数） */
function tweenNumber(node, to, options) {
  if (!node) return;
  const opts = options || {};
  const target = toNumber(to);
  if (target === null) {
    node.textContent = '—';
    return;
  }
  const decimals = opts.decimals === undefined ? 0 : opts.decimals;
  const suffix = opts.suffix || '';
  const prefix = opts.prefix || '';
  const current = toNumber(String(node.textContent).replace(/[^0-9.\-]/g, ''));
  if (prefersReducedMotion() || current === null || Math.abs(target - current) < 1e-9) {
    node.textContent = prefix + fmtNum(target, decimals === 0 ? null : decimals) + suffix;
    return;
  }
  const from = current;
  const duration = opts.duration || 520;
  const t0 = performance.now();
  node.classList.add('num-tween');
  const step = (now) => {
    const k = Math.min(1, (now - t0) / duration);
    const eased = 1 - Math.pow(1 - k, 3);
    const value = from + (target - from) * eased;
    node.textContent = prefix + fmtNum(value, decimals === 0 ? null : decimals) + suffix;
    if (k < 1) {
      requestAnimationFrame(step);
    } else {
      node.textContent = prefix + fmtNum(target, decimals === 0 ? null : decimals) + suffix;
      window.setTimeout(() => node.classList.remove('num-tween'), 200);
    }
  };
  requestAnimationFrame(step);
}

/** 给新插入的元素加一次入场动画（同一元素只加一次） */
function motionIn(node, delayMs) {
  if (!node || prefersReducedMotion()) return node;
  node.classList.add('motion-in');
  if (delayMs) node.style.animationDelay = delayMs + 'ms';
  return node;
}

/* --------------------------------------------------------------------------
 * 8.7 结果总览 KPI：用聚合统计渲染一行等宽数字指标
 * ------------------------------------------------------------------------ */
function renderKpis(run, res) {
  const host = $('run-kpis');
  if (!host) return;
  clear(host);
  if (!run || !state.runId) return;
  const aggregates = (res && res.aggregates) || state.ranking.aggregates || {};
  const rows = Array.isArray(res && res.ranking) ? res.ranking : [];
  const best = rows.length && rows[0] ? rows[0] : null;
  const cells = [
    { label: 'MOLECULES', value: run.molecule_count !== undefined ? run.molecule_count : null,
      hint: '本次对接的候选分子数' },
    { label: 'SCORED', value: aggregates.total !== undefined ? aggregates.total : null,
      hint: '有真实对接分数的分子数（含失败见产物）' },
    { label: 'HITS < 对照', value: aggregates.hits !== undefined ? aggregates.hits : null,
      hint: '亲和力优于阳性对照的分子数（未提供对照时为空）' },
    { label: 'BEST kcal/mol', value: best ? best.affinity_kcal_mol : (aggregates.min !== undefined ? aggregates.min : null),
      decimals: 2, hint: '本次最优（最负）对接亲和力' },
    { label: 'ERRORS', value: aggregates.with_errors !== undefined ? aggregates.with_errors : null,
      hint: '对接/准备失败的分子数（报告里会逐条列出）' },
    { label: 'ELAPSED', text: run.duration_sec !== undefined ? fmtDuration(run.duration_sec) : '—',
      hint: '本次运行墙钟耗时' },
  ];
  cells.forEach((cell) => {
    const box = el('div', 'kpi');
    const value = el('span', 'kpi-value num', cell.text !== undefined ? cell.text : '—');
    box.appendChild(value);
    box.appendChild(el('span', 'kpi-label', cell.label));
    if (cell.hint) box.title = cell.hint;
    host.appendChild(box);
    if (cell.text === undefined && cell.value !== undefined && cell.value !== null) {
      tweenNumber(value, cell.value, { decimals: cell.decimals || 0 });
    }
  });
  motionIn(host);
}

/* --------------------------------------------------------------------------
 * 8.8 导出条：报告 PDF / 报告 MD / 排序 CSV / 位姿 ZIP / 打包下载
 *   文件名一律取后端下发的 downloads（命名规则只有一处事实源），前端不再自己拼。
 * ------------------------------------------------------------------------ */
const DOWNLOAD_BUTTONS = [
  { id: 'btn-download-pdf', kind: 'report_pdf', url: (id) => '/api/runs/' + id + '/report.pdf',
    need: (ctx) => ctx.hasReport },
  { id: 'btn-download-report', kind: 'report_md', url: (id) => '/api/runs/' + id + '/artifacts/report_md',
    need: (ctx) => ctx.hasReport },
  { id: 'btn-download-csv', kind: 'ranking_csv', url: (id) => '/api/runs/' + id + '/export.csv',
    need: (ctx) => ctx.hasRanking },
  { id: 'btn-download-poses', kind: 'poses_zip', url: (id) => '/api/runs/' + id + '/poses.zip',
    need: (ctx) => ctx.hasPoses },
  { id: 'btn-download-zip', kind: 'data_zip', url: (id) => '/api/runs/' + id + '/download.zip',
    need: (ctx) => ctx.hasAny },
];

function updateExportLink() {
  const runId = state.runId;
  const artifacts = Array.isArray(state.artifacts) ? state.artifacts : [];
  const names = artifacts.map((a) => String(a.name || ''));
  const prefix = state.downloads && state.downloads.prefix ? state.downloads.prefix : '';
  const ctx = {
    hasReport: names.includes('report_md') || names.includes('report_pdf'),
    hasRanking: state.ranking.total > 0 || names.includes('ranking_csv'),
    hasPoses: names.some((n) => n.indexOf('pose_') === 0),
    hasAny: Boolean(runId),
  };
  DOWNLOAD_BUTTONS.forEach((spec) => {
    const node = $(spec.id);
    if (!node) return;
    const enabled = Boolean(runId) && spec.need(ctx);
    const name = (state.downloads && state.downloads[spec.kind]) || '';
    if (enabled) {
      node.href = spec.url(encodeURIComponent(runId));
      node.classList.remove('disabled');
      node.setAttribute('aria-disabled', 'false');
      if (name) node.setAttribute('download', name);
      node.title = (node.title || '').split(' — ')[0] + (name ? ' — ' + name : '');
    } else {
      node.href = '#';
      node.classList.add('disabled');
      node.setAttribute('aria-disabled', 'true');
      node.removeAttribute('download');
    }
  });

  // 中间数据页里已有的两个入口保持可用，并与导出条使用同一套文件名
  const zipAll = $('btn-zip-all');
  if (zipAll) {
    if (ctx.hasAny) {
      zipAll.href = '/api/runs/' + encodeURIComponent(runId) + '/download.zip';
      zipAll.classList.remove('disabled');
      zipAll.setAttribute('aria-disabled', 'false');
      if (state.downloads && state.downloads.data_zip) zipAll.setAttribute('download', state.downloads.data_zip);
    } else {
      zipAll.href = '#';
      zipAll.classList.add('disabled');
      zipAll.setAttribute('aria-disabled', 'true');
    }
  }
  const zipPoses = $('btn-zip-poses');
  if (zipPoses) {
    if (ctx.hasPoses) {
      zipPoses.href = '/api/runs/' + encodeURIComponent(runId) + '/poses.zip';
      zipPoses.classList.remove('disabled');
      zipPoses.setAttribute('aria-disabled', 'false');
      if (state.downloads && state.downloads.poses_zip) zipPoses.setAttribute('download', state.downloads.poses_zip);
    } else {
      zipPoses.href = '#';
      zipPoses.classList.add('disabled');
      zipPoses.setAttribute('aria-disabled', 'true');
    }
  }
  const legacyCsv = $('btn-export-csv');
  if (legacyCsv) {
    if (ctx.hasRanking) {
      legacyCsv.href = '/api/runs/' + encodeURIComponent(runId) + '/export.csv';
      legacyCsv.classList.remove('disabled');
      legacyCsv.setAttribute('aria-disabled', 'false');
      if (state.downloads && state.downloads.ranking_csv) legacyCsv.setAttribute('download', state.downloads.ranking_csv);
    } else {
      legacyCsv.href = '#';
      legacyCsv.classList.add('disabled');
      legacyCsv.setAttribute('aria-disabled', 'true');
    }
  }

  const hint = $('download-name-hint');
  if (hint) {
    hint.textContent = prefix ? prefix + '_*' : (runId ? '运行 ' + runId : '');
    hint.title = prefix
      ? '本次运行的规范文件名前缀：' + prefix
        + '（report.pdf / report.md / ranking.csv / data.zip / poses.zip）'
      : '运行后显示本次运行的规范文件名前缀';
  }
}

/**
 * 载入一次运行的元信息 / 报告 / 图表 / 中间数据。
 * 结果表与分子详情都改为调用 /ranking 分页接口，
 * 不再依赖 GET /api/runs/{id} 里内联的前 200 条 ranking。
 */
async function applyRun(payload) {
  const run = payload.run || {};
  const res = payload.result || {};
  state.artifacts = Array.isArray(payload.artifacts) ? payload.artifacts : [];
  state.downloads = (payload.downloads && typeof payload.downloads === 'object') ? payload.downloads : {};
  state.reportMarkdown = payload.report_markdown || '';
  state.runId = run.run_id || state.runId;
  state.currentRun = run;
  state.positiveRow = buildPositiveRow(res);
  state.collaboration = (payload.collaboration && typeof payload.collaboration === 'object')
    ? payload.collaboration : {};

  const inlineTotal = toNumber(pick(res, ['ranking_total'], undefined));
  const aggregates = (res.aggregates && typeof res.aggregates === 'object') ? res.aggregates : null;
  const aggTotal = aggregates ? toNumber(pick(aggregates, ['total'], undefined)) : null;
  const inlineRows = Array.isArray(res.ranking) ? res.ranking.length : 0;
  state.ranking.total = inlineTotal !== null ? Math.max(0, Math.round(inlineTotal))
    : (aggTotal !== null ? Math.max(0, Math.round(aggTotal)) : inlineRows);
  state.cards.total = state.ranking.total;
  if (aggregates) state.ranking.aggregates = aggregates;

  if (state.runId) setRunIdLabel('run_id ' + state.runId);
  /* 有排序行或报告 → 展开排序工具条/表格/图例；空运行（no_op）保持收起 */
  setWorkbenchHasRun(Boolean(state.ranking.total) || Boolean(state.reportMarkdown));
  renderSummary(run, state.ranking.total);
  renderRunNotes(run);
  renderKpis(run, res);
  renderCollaboration();
  renderBoxLine((res.docking || {}).receptors || []);
  renderPocketAnalysis(payload);
  renderReport(state.reportMarkdown);
  renderArtifacts();
  renderCharts();
  updateExportLink();
  renderRanking();
  renderCards();

  if (Array.isArray(payload.log) && payload.log.length) {
    clear($('log-box'));
    payload.log.slice(-400).forEach((line) => {
      $('log-box').appendChild(el('p', 'log-info', String(line)));
    });
    $('log-box').scrollTop = $('log-box').scrollHeight;
  }
  if (run.error) showError('该次运行记录包含错误信息：' + run.error);

  await Promise.all([
    loadRankingPage({ offset: 0 }),
    loadCardsPage({ offset: 0 })
  ]);
}

async function loadRun(runId, options) {
  const opts = options || {};
  if (!runId) return;
  if (!opts.silent) setRunHint('正在载入运行 ' + runId + ' …');
  try {
    const payload = await getJson('/api/runs/' + encodeURIComponent(runId));
    await applyRun(payload);
    // 实时刚跑完则保留最近 N 行；历史载入则不铺开全量，只提示去分页视图查看
    if (state.liveCount > 0) setLiveNote(state.ranking.total);
    else resetLiveTableForLoadedRun(state.ranking.total);
    if (!opts.silent) setRunHint('已载入运行 ' + runId + '（结果按服务端分页展示）。');
    return payload;
  } catch (error) {
    const detail = error.status ? ('HTTP ' + error.status + '：' + shortError(error)) : shortError(error);
    showError('载入运行记录失败：' + detail);
    setRunHint('载入运行记录失败。', 'err');
    throw error;
  }
}

/** 历史运行载入后：实时表只显示一行提示，避免一次性创建上万个 DOM 节点 */
function resetLiveTableForLoadedRun(total) {
  clear($('molecules-tbody'));
  state.liveCount = 0;
  const count = toNumber(total);
  $('live-count').textContent = count !== null ? fmtInt(count) : '0';
  const empty = $('molecules-empty');
  empty.textContent = '已载入该次运行的完整结果（共 ' + (count !== null ? fmtInt(count) : '—') +
    ' 条），请到「结果总览 / 分子详情」按页查看；此表仅在实时运行时显示最近 ' +
    LIVE_ROW_LIMIT + ' 条。';
  show(empty);
  setLiveNote(count);
}

/* --------------------------------------------------------------------------
 * 18. 历史运行
 * ------------------------------------------------------------------------ */
function historyRowNode(run) {
  const tr = el('tr', 'clickable');
  tr.appendChild(el('td', 'mono', fmtTime(run.created_at)));
  tr.appendChild(el('td', null, run.kind === 'agent' ? '多 Agent' : (run.kind === 'pipeline' ? '流水线' : fmtText(run.kind))));
  tr.appendChild(el('td', null, fmtText(run.receptor_label || run.receptor)));
  tr.appendChild(el('td', 'num', fmtInt(run.molecule_count)));
  const top = Array.isArray(run.top) && run.top.length ? run.top[0] : null;
  tr.appendChild(el('td', null, top ? fmtText(top.name) : '—'));
  tr.appendChild(el('td', 'num', top ? fmtNum(top.affinity_kcal_mol, 2) : '—'));
  const statusTd = el('td');
  const status = String(run.status || '');
  /* 状态用方括号标签：ok→[ OK ] / running→[ RUN ] / cancelled→[ CANCEL ] / 其他→[ FAIL ] */
  const statusKey = status === 'ok' ? 'ok'
    : (status === 'running' ? 'run'
      : (status === 'cancelled' ? 'cancel' : (status === 'no_op' ? 'skip' : 'fail')));
  statusTd.appendChild(el('span', 'tag tag-' + statusKey, ORCH_TAG_TEXT[statusKey]));
  if (status) statusTd.appendChild(el('span', 'status-text mono', status));
  if (run.error) statusTd.title = String(run.error);
  tr.appendChild(statusTd);
  const actionTd = el('td');
  const button = el('button', 'btn btn-ghost btn-sm', '载入');
  button.type = 'button';
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    loadRun(run.run_id).then(() => switchTab('overview')).catch(() => {});
  });
  actionTd.appendChild(button);
  tr.appendChild(actionTd);
  tr.addEventListener('click', () => {
    loadRun(run.run_id).then(() => switchTab('overview')).catch(() => {});
  });
  return tr;
}

/**
 * 历史运行列表按 HISTORY_PAGE_SIZE 条折叠展示（数据多时不会一次性铺开过多 DOM）。
 */
function renderHistory() {
  const tbody = $('history-tbody');
  clear(tbody);
  const runs = state.historyRuns;
  const shown = Math.min(state.historyShown, runs.length);
  const empty = $('history-empty');
  empty.classList.toggle('hidden', runs.length > 0);
  if (!runs.length) {
    empty.textContent = '暂无历史运行记录。';
  }
  const fragment = document.createDocumentFragment();
  runs.slice(0, shown).forEach((run) => fragment.appendChild(historyRowNode(run)));
  tbody.appendChild(fragment);

  const rest = runs.length - shown;
  const more = $('history-more');
  if (more) {
    more.classList.toggle('hidden', rest <= 0);
    more.textContent = '显示更多（剩余 ' + fmtInt(rest) + ' 条）';
  }
  const hint = $('history-hint');
  if (hint) {
    hint.textContent = '共 ' + fmtInt(runs.length) + ' 条记录，已显示 ' + fmtInt(shown) + ' 条' +
      (rest > 0 ? '（点击「显示更多」展开）' : '') + '，点击任意一行可载入。';
  }
}

async function refreshHistory() {
  const hint = $('history-hint');
  try {
    hint.textContent = '正在加载历史运行…';
    const data = await getJson('/api/runs?limit=' + HISTORY_FETCH_LIMIT);
    const runs = Array.isArray(data.runs) ? data.runs : [];
    state.historyRuns = runs;
    state.historyShown = Math.min(HISTORY_PAGE_SIZE, runs.length);
    renderHistory();
  } catch (error) {
    state.historyRuns = [];
    state.historyShown = 0;
    clear($('history-tbody'));
    const empty = $('history-empty');
    show(empty);
    empty.textContent = '历史运行加载失败：' + shortError(error);
    hint.textContent = '加载历史运行失败：' + shortError(error);
    const more = $('history-more');
    if (more) more.classList.add('hidden');
  }
}

/* --------------------------------------------------------------------------
 * 18.5 工具提示（统一气泡）
 *   触发元素加 `data-tip`（一句话）或 `data-tip-html`（白名单 HTML）。
 *   气泡挂在 <body> 上做视口自适应：默认在下方，空间不足翻到上方，左右夹紧，
 *   因此不会被 .panel-config 的圆角/滚动或 <details> 的 overflow:hidden 裁掉。
 *   显示期间临时摘掉原生 title，避免「气泡 + 原生提示」双重弹出（离开时原样恢复）。
 * ------------------------------------------------------------------------ */

/** data-tip-html 的标签白名单：只保留文字与结构，不保留任何属性（mono 除外） */
const TIP_TAGS = { B: 1, STRONG: 1, I: 1, EM: 1, BR: 1, CODE: 1, KBD: 1, SPAN: 1, SMALL: 1 };

/** 把白名单 HTML 转成真实节点：非白名单标签被剥掉、只留其文字内容 */
function tipNodesFromHtml(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = String(html === null || html === undefined ? '' : html);
  const host = document.createElement('div');
  const walk = (src, dst) => {
    Array.prototype.forEach.call(src.childNodes, (node) => {
      if (node.nodeType === 3) {                          // 文本节点
        dst.appendChild(document.createTextNode(node.nodeValue));
        return;
      }
      if (node.nodeType !== 1) return;                    // 注释等一律丢弃
      const tag = node.tagName;
      if (!TIP_TAGS[tag]) { walk(node, dst); return; }    // 丢弃标签本身，保留文字
      const copy = document.createElement(tag.toLowerCase());
      if (tag === 'SPAN') {
        const keep = String(node.className || '').split(/\s+/)
          .filter((name) => name === 'mono' || name === 'tip-k');
        if (keep.length) copy.className = keep.join(' ');
      }
      dst.appendChild(copy);
      walk(node, copy);
    });
  };
  walk(tpl.content, host);
  return Array.prototype.slice.call(host.childNodes);
}

let tipBox = null;
let tipOwner = null;

function ensureTipBox() {
  if (tipBox && document.body.contains(tipBox)) return tipBox;
  tipBox = document.createElement('div');
  tipBox.className = 'tip-box';
  tipBox.setAttribute('role', 'tooltip');
  tipBox.hidden = true;
  document.body.appendChild(tipBox);
  return tipBox;
}

function tipSourceOf(node) {
  if (node.hasAttribute('data-tip-html')) {
    return { html: true, value: node.getAttribute('data-tip-html') };
  }
  if (node.hasAttribute('data-tip')) return { html: false, value: node.getAttribute('data-tip') };
  return null;
}

/** 视口自适应定位：下方优先 → 空间不足翻到上方 → 左右夹紧并同步箭头位置 */
function placeTip(box, node) {
  const anchor = node.getBoundingClientRect();
  const size = box.getBoundingClientRect();
  const pad = 8;
  let left = anchor.left + anchor.width / 2 - size.width / 2;
  left = Math.max(pad, Math.min(left, window.innerWidth - size.width - pad));
  let top = anchor.bottom + 8;
  let above = false;
  if (top + size.height > window.innerHeight - pad && anchor.top - size.height - 8 >= pad) {
    top = anchor.top - size.height - 8;
    above = true;
  }
  box.style.left = Math.round(left) + 'px';
  box.style.top = Math.round(Math.max(pad, top)) + 'px';
  box.classList.toggle('tip-above', above);
  const arrow = anchor.left + anchor.width / 2 - left;
  box.style.setProperty('--tip-arrow-x', Math.max(12, Math.min(arrow, size.width - 12)) + 'px');
}

function showTip(node) {
  const source = tipSourceOf(node);
  if (!source) return;
  if (tipOwner === node && tipBox && !tipBox.hidden) { placeTip(tipBox, node); return; }
  hideTip();
  const box = ensureTipBox();
  clear(box);
  if (source.html) tipNodesFromHtml(source.value).forEach((child) => box.appendChild(child));
  else box.textContent = source.value;
  box.hidden = false;
  placeTip(box, node);
  tipOwner = node;
  node.classList.add('tip-active');
  if (node.hasAttribute('title')) {
    node.dataset.tipTitle = node.getAttribute('title');
    node.removeAttribute('title');
  }
}

function hideTip() {
  if (tipBox) tipBox.hidden = true;
  if (tipOwner) {
    if (Object.prototype.hasOwnProperty.call(tipOwner.dataset, 'tipTitle')) {
      tipOwner.setAttribute('title', tipOwner.dataset.tipTitle);
      delete tipOwner.dataset.tipTitle;
    }
    tipOwner.classList.remove('tip-active');
  }
  tipOwner = null;
}

function tipTrigger(target) {
  if (!target || target.nodeType !== 1 || !target.closest) return null;
  return target.closest('[data-tip],[data-tip-html]');
}

function bindTooltips() {
  /* 标记「JS 气泡已接管」：关掉 CSS 里的纯 ::after 兜底，避免两套气泡同时出现 */
  document.documentElement.classList.add('has-tip-js');
  document.addEventListener('mouseover', (event) => {
    const node = tipTrigger(event.target);
    if (node) showTip(node);
  });
  document.addEventListener('mouseout', (event) => {
    const node = tipTrigger(event.target);
    if (!node || node !== tipOwner) return;
    const next = event.relatedTarget;
    if (next && next.nodeType === 1 && node.contains(next)) return;   // 在同一触发元素内部移动
    hideTip();
  });
  document.addEventListener('focusin', (event) => {
    const node = tipTrigger(event.target);
    if (node) showTip(node);
  });
  document.addEventListener('focusout', () => { hideTip(); });
  /* (?) 图标常在 <label> / <summary> 内：阻止默认动作，避免点开折叠或激活字段 */
  document.addEventListener('click', (event) => {
    const node = tipTrigger(event.target);
    if (node && node.classList.contains('tip-icon')) event.preventDefault();
  }, true);
  window.addEventListener('scroll', () => { hideTip(); }, true);   // 捕获阶段：内层滚动容器也算
  window.addEventListener('resize', () => { hideTip(); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') hideTip();
  });
}

/* --------------------------------------------------------------------------
 * 18.6 共享参数面板：一键预设 / 恢复系统默认 / 折叠摘要
 * ------------------------------------------------------------------------ */

const PARAM_PRESETS = {
  'preset-fast': { label: '快速初筛', exhaustiveness: 4, nPoses: 1 },
  'preset-balanced': { label: '平衡', exhaustiveness: 16, nPoses: 1 },
  'preset-accuracy': { label: '高精度', exhaustiveness: 32, nPoses: 3 }
};

/** 轻提示：显示在预设行末尾，数秒后淡出（role=status，读屏可播报） */
let presetNoteTimer = 0;
function setPresetNote(text) {
  const node = $('preset-note');
  if (!node) return;
  window.clearTimeout(presetNoteTimer);
  node.textContent = text || '';
  node.classList.toggle('is-on', Boolean(text));
  if (text) {
    presetNoteTimer = window.setTimeout(() => {
      node.textContent = '';
      node.classList.remove('is-on');
    }, 3200);
  }
}

function setExhaustiveness(value) {
  const slider = $('exhaustiveness');
  const autoBox = $('exhaustiveness-auto');
  if (!slider) return;
  slider.value = String(value);
  const label = $('exhaustiveness-value');
  /* 勾着「自动」→ 标签显示「自动」（滑块位置只是备用的显式值，不下发） */
  if (label) label.textContent = (autoBox && autoBox.checked) ? '自动' : String(value);
}

function markPresetActive(activeId) {
  Object.keys(PARAM_PRESETS).forEach((id) => {
    const button = $(id);
    if (button) button.classList.toggle('is-active', id === activeId);
  });
}

/** 应用一键预设：写入搜索强度 / 位姿数，并给出「已应用预设：X」轻提示 */
function applyPreset(id) {
  const preset = PARAM_PRESETS[id];
  if (!preset) return;
  if ($('exhaustiveness-auto')) $('exhaustiveness-auto').checked = false;   // 预设 = 显式强度
  state.touchedFields.add('exhaustiveness');
  state.touchedFields.add('n-poses');
  syncExhaustivenessAuto();
  setExhaustiveness(preset.exhaustiveness);
  $('n-poses').value = String(preset.nPoses);
  markPresetActive(id);
  setPresetNote('已应用预设：' + preset.label);
  if (state.page === 'chat') state.advancedTouched = true;
  updateParamSummaries();
}

/** 设置页给出的系统默认值（缺失时退回内置默认） */
function dockingDefault(path, fallback) {
  const eff = (settingsState && settingsState.settingsDefaults) || {};
  const value = eff[path];
  return (value === null || value === undefined || value === '') ? fallback : value;
}

/**
 * 质子化态策略与目标 pH 的联动：只有策略 = ph 时目标 pH 才生效。
 * 置灰 + 提示写清楚，避免新手填了 pH 却以为已经生效（静默无效是最糟的界面行为）。
 */
function syncProtonationFields() {
  const select = $('protonation-select');
  const phInput = $('protonation-ph');
  if (!select || !phInput) return;
  const active = select.value === 'ph';
  phInput.disabled = !active;
  phInput.setAttribute('aria-disabled', active ? 'false' : 'true');
  const label = phInput.closest('label');
  if (label) label.classList.toggle('is-muted', !active);
  const tip = label ? label.querySelector('.tip-icon') : null;
  if (tip) {
    tip.setAttribute('data-tip', active
      ? '目标 pH（默认 7.4 生理 pH）：按内置 pKa 规则表把羧酸/胺/脒/胍/咪唑等加到该 pH 的状态。'
      : '当前质子化态策略不是 ph → 目标 pH 不生效。把策略改为 ph 后本项才会参与计算。');
  }
}

/**
 * 恢复系统默认：只把界面字段清回 placeholder / 设置页默认，
 * 不写任何配置文件，也不影响已上传的附件。
 */
function restoreParamDefaults() {
  $('receptor-select').value = '';
  renderReceptorInfo(null);
  clearSite();
  $('ligands-text').value = '';
  $('molecule-file').value = '';
  $('allow-example-fallback').checked = false;
  $('positive-control').value = String(dockingDefault('docking.positive_control', ''));
  $('engine-select').value = String(dockingDefault('docking.engine', 'vina'));
  if ($('protonation-select')) {
    $('protonation-select').value = String(dockingDefault('docking.protonation', 'ph'));
    const phDefault = toNumber(dockingDefault('docking.protonation_ph', PH_DEFAULT));
    $('protonation-ph').value = String(phDefault === null ? PH_DEFAULT : phDefault);
    syncProtonationFields();
  }
  const pocket = String(dockingDefault('runtime.pocket_engine', 'auto'));
  $('pocket-engine-select').value = (pocket === 'auto') ? '' : pocket;
  if ($('exhaustiveness-auto')) $('exhaustiveness-auto').checked = true;   // 恢复默认 = 回到自动
  syncExhaustivenessAuto();
  setExhaustiveness(toNumber(dockingDefault('docking.exhaustiveness', 16)) || 16);
  $('n-poses').value = String(toNumber(dockingDefault('docking.n_poses', 1)) || 1);
  $('max-ligands').value = String(toNumber(dockingDefault('docking.max_ligands', 0)) || 0);
  $('save-poses').checked = dockingDefault('docking.save_poses', true) !== false;
  setLigandSource('text');
  markPresetActive('');
  settingsState.formTouched.clear();     // 清掉「已手改」标记，设置页默认值可再次预填
  state.touchedFields.clear();           // 恢复默认后不再下发任何参数（全部回到自动/系统默认）
  state.siteTouched = false;
  state.uploadSiteFilled = false;
  setPresetNote('已恢复系统默认值（设置页已保存的配置未改动）');
  updateConfigInfoBar();
  updateParamSummaries();
}

/** 折叠摘要：不展开也能看到当前生效的关键值 */
function updateAdvancedSummary() {
  const node = $('chat-advanced-summary');
  if (!node) return;
  /* 运行参数（引擎/质子化/搜索强度/位姿数/阳性对照）已经常驻在对话框上方，
     这里只汇总"其余设置"里真正影响运行的两项：位点盒与口袋引擎。 */
  const pocketSelect = $('pocket-engine-select');
  const pocket = (pocketSelect && pocketSelect.value) ? pocketSelect.value : 'auto';
  const site = ($('center-x') && $('center-x').value) ? '手填位点盒' : '自动定盒';
  node.textContent = '· ' + site + ' / 口袋 ' + pocket;
}

/** 「搜索强度：自动」联动：勾上时禁用滑块并把数值标签显示为「自动」 */
function syncExhaustivenessAuto() {
  const box = $('exhaustiveness-auto');
  const slider = $('exhaustiveness');
  if (!box || !slider) return;
  const auto = box.checked;
  slider.disabled = auto;
  const label = $('exhaustiveness-value');
  if (label) label.textContent = auto ? '自动' : String(slider.value);
}

function updateMoreSummary() {
  const node = $('more-params-summary');
  if (!node) return;
  const select = $('pocket-engine-select');
  const pocket = (select && select.value) ? select.value : 'auto';
  const maxLigands = toNumber($('max-ligands').value) || 0;
  const savePoses = $('save-poses').checked;
  const parts = [
    '口袋 ' + pocket,
    savePoses ? '保存位姿' : '不保存位姿',
    maxLigands > 0 ? ('≤ ' + fmtInt(maxLigands) + ' 分子') : '不限分子数'
  ];
  if ($('positive-control').value.trim()) parts.push('含阳性对照');
  node.textContent = '· ' + parts.join(' / ');
}

function updateParamSummaries() {
  updateAdvancedSummary();
  updateMoreSummary();
}

/* --------------------------------------------------------------------------
 * 19. 事件绑定与初始化
 * ------------------------------------------------------------------------ */

/**
 * 挂载共享参数面板。
 * 全站只有一份参数 DOM（#params-panel，id 与改造前完全一致），
 * 在「对话模式的高级设置」与「参数模式的参数区」之间移动，天然避免两套参数互相覆盖。
 */
function mountParams(page) {
  const panel = $('params-panel');
  if (!panel) return;
  const quick = $('param-common');     // 运行参数：引擎 / 质子化 + 目标 pH / n_poses / 搜索强度 / 阳性对照…
  const rest = $('params-rest');       // 其余：配体来源 / 更多参数 / 上传受体
  panel.classList.remove('hidden');
  if (page === 'chat') {
    /* 对话模式：**运行参数直接放在对话框上**（不折叠），其余设置收进「其他设置」折叠。
       同一份 DOM 在两个宿主之间移动，避免出现两套同名字段。 */
    const quickHost = $('chat-quick-params');
    const restHost = $('chat-advanced-mount');
    if (quickHost && quick && quick.parentElement !== quickHost) quickHost.appendChild(quick);
    if (restHost && rest && rest.parentElement !== restHost) restHost.appendChild(rest);
  } else {
    const host = $('manual-params-mount');
    /* 参数模式：两块按顺序放回参数区（运行参数在前，其余在后） */
    if (host) {
      if (quick && quick.parentElement !== host) host.appendChild(quick);
      if (rest && rest.parentElement !== host) host.appendChild(rest);
    }
  }
  /* 对话模式：隐藏「注册表受体下拉 + 位点编辑」（受体由指令或系统默认值决定），
     但保留受体文件上传（上传是明确意图，只发送 receptor_file）。 */
  if (quick) quick.classList.toggle('chat-mode', page === 'chat');
  panel.classList.toggle('chat-mode', page === 'chat');   // 旧选择器兼容（.params-body.chat-mode）
  /* 对话模式：页面上的主操作是「发送」，「开始运行」按钮只在参数模式出现（CSS 收起） */
  const configPanel = $('config-panel');
  if (configPanel) configPanel.classList.toggle('mode-chat', page === 'chat');
  updateParamSummaries();
}

/* 子页深链接：对话模式 #chat / 参数模式 #manual */
const PAGE_HASH = { chat: '#chat', manual: '#manual' };

/** 从 URL hash 解析子页；无法识别时返回 null（保持默认「对话模式」） */
function pageFromHash() {
  const raw = String(location.hash || '').replace(/^#/, '').toLowerCase();
  return (raw === 'chat' || raw === 'manual') ? raw : null;
}

/** 把当前子页写回 URL hash，便于分享链接与自动化测试 */
function syncHash(page) {
  // 设置页面是顶层视图，hash 由 syncViewHash 维护（#/settings）
  if (state.view === 'settings') return;
  const want = PAGE_HASH[page === 'manual' ? 'manual' : 'chat'];
  if (location.hash === want) return;
  try {
    history.replaceState(null, '', want);
  } catch (error) {
    location.hash = want;   // 不支持 history API 时退化为直接赋值
  }
}

/**
 * 只更新子页的视觉状态，可重复调用。
 * 通过 .is-active 类 + CSS 的 opacity/transform 过渡切换（不是 display:none 硬切），
 * 两个面板叠放在同一网格单元，容器高度不受内容拉扯，结果区不会跳动。
 */
function applyPageView(page) {
  document.querySelectorAll('#mode-tabs .page-tab').forEach((button) => {
    const active = button.dataset.page === page;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  const chatPane = $('chat-pane');
  const manualPane = $('manual-pane');
  if (chatPane) {
    chatPane.classList.toggle('is-active', page === 'chat');
    chatPane.setAttribute('aria-hidden', page === 'chat' ? 'false' : 'true');
  }
  if (manualPane) {
    manualPane.classList.toggle('is-active', page === 'manual');
    manualPane.setAttribute('aria-hidden', page === 'manual' ? 'false' : 'true');
  }
  mountParams(page);

  // 执行区显示逻辑：对话模式固定显示工具轨迹，参数模式跟随运行方式
  const agentMode = state.mode === 'agent';
  $('exec-panel').classList.toggle('mode-chat', page === 'chat');
  $('agent-box').classList.toggle('hidden', page === 'chat' ? false : !agentMode);
  $('agent-box-title').textContent = page === 'chat' ? '工具调用轨迹与阶段进展' : '工具调用轨迹';
}

/** 切换子页：只切换配置区内容，结果区（执行 / 结果总览 / …）两者共用 */
function setPage(page) {
  const next = page === 'manual' ? 'manual' : 'chat';
  if (state.running) {
    setRunHint('运行中，暂不能切换模式；请先「停止」或等待本次运行结束。', 'warn');
    syncHash(state.page);
    return;
  }
  if (state.page === next) {
    applyPageView(next);
    syncHash(next);
    return;
  }
  state.page = next;
  applyPageView(next);
  syncHash(next);

  // 切换子页即清理上一次的运行状态，避免两种模式串台
  state.runId = null;
  state.controller = null;
  resetExecution();
  clearChatHistory();
  setRunIdLabel('run_id —');

  setRunHint(next === 'chat'
    ? '对话模式：直接输入指令即可，默认不注入任何参数。'
    : '参数模式：表单参数为权威参数，目标描述不会覆盖参数。');
}

function bindStaticEvents() {
  // 受体
  $('receptor-select').addEventListener('change', () => {
    const receptor = currentReceptor();
    renderReceptorInfo(receptor);
    fillSite(receptor);
  });
  $('btn-fill-site').addEventListener('click', () => {
    const receptor = currentReceptor();
    if (!receptor) {
      setRunHint('请先选择受体。', 'err');
      return;
    }
    fillSite(receptor);
    setRunHint('已填入注册位点。');
  });
  $('btn-clear-site').addEventListener('click', () => {
    clearSite();
    setRunHint('已清空位点，运行时将使用受体默认位点。');
  });

  // 配体来源（四选一：SMILES 文本 / 上传文件 / 服务端路径 / 示例库）
  document.querySelectorAll('#ligand-source .seg-btn').forEach((button) => {
    button.addEventListener('click', () => { setLigandSource(button.dataset.source); });
  });
  $('library-select').addEventListener('change', () => {
    const library = state.libraries.find((item) => (item.id || item.path) === $('library-select').value);
    renderLibraryPreview(library || null);
  });

  // 文件上传：点击 / 拖拽，两种模式共用同一份上传区（参数面板在子页间移动挂载）
  setupDropzone($('ligand-upload-zone'), $('ligand-file-input'), 'ligand');
  setupDropzone($('receptor-upload-zone'), $('receptor-file-input'), 'receptor');
  renderUploadCard('ligand');
  renderUploadCard('receptor');

  // 位点字段一旦被手动编辑，上传受体就不再自动覆盖
  ['center-x', 'center-y', 'center-z', 'size-x', 'size-y', 'size-z'].forEach((id) => {
    $(id).addEventListener('input', () => {
      state.siteTouched = true;
      state.uploadSiteFilled = false;
    });
  });

  // 参数控件
  $('exhaustiveness').addEventListener('input', () => {
    syncExhaustivenessAuto();
  });
  syncExhaustivenessAuto();

  // 子页：对话模式 / 参数模式
  document.querySelectorAll('#mode-tabs .page-tab').forEach((button) => {
    button.addEventListener('click', () => { setPage(button.dataset.page); });
  });

  // URL hash 深链接 #chat / #manual：手动改 hash 或浏览器前进后退都会同步子页
  window.addEventListener('hashchange', () => {
    const target = pageFromHash();
    if (target && target !== state.page) setPage(target);
  });

  // 高级设置：只有用户真正展开过，才认为「用到了参数」
  const advanced = $('chat-advanced');
  advanced.addEventListener('toggle', () => {
    if (advanced.open && !state.advancedTouched) {
      state.advancedTouched = true;
      $('chat-advanced-hint').textContent =
        '已展开：参数作为「可被指令覆盖的默认值」随指令下发。';
      if (!state.running) setRunHint('高级设置已启用：参数将作为默认值随指令下发（指令优先）。');
    }
    updateParamSummaries();   // 折叠摘要行显示当前生效的关键值（展开 / 收起都刷新）
  });

  // 统一工具提示：data-tip / data-tip-html（含字段旁的 (?) 图标）
  bindTooltips();

  // 一键预设 + 恢复系统默认（两种模式共用同一份参数 DOM）
  Object.keys(PARAM_PRESETS).forEach((id) => {
    const button = $(id);
    if (button) button.addEventListener('click', () => { applyPreset(id); });
  });
  const restoreDefaults = $('btn-restore-defaults');
  if (restoreDefaults) {
    restoreDefaults.addEventListener('click', () => { restoreParamDefaults(); });
  }

  // 手动改动任一参数：取消预设高亮并刷新折叠摘要
  const paramsPanel = $('params-panel');
  if (paramsPanel) {
    const onParamChange = (event) => {
      const target = event.target;
      if (target && target.id && PARAM_PRESETS[target.id]) return;   // 预设按钮自身不算手改
      markPresetActive('');
      updateParamSummaries();
    };
    paramsPanel.addEventListener('input', onParamChange);
    paramsPanel.addEventListener('change', onParamChange);
  }

  // 对话输入：Enter 发送 / Shift+Enter 换行
  /* 附件：按钮 → 文件选择 → 上传；支持拖拽；@ 唤起引用选择器 */
  const attachBtn = $('chat-attach-btn');
  const fileInput = $('chat-file-input');
  if (attachBtn && fileInput) {
    attachBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', async () => {
      await chatAttachFiles(fileInput.files);
      fileInput.value = '';           // 允许重复选择同一个文件
    });
  }
  const mentionBtn = $('chat-mention-btn');
  if (mentionBtn) {
    mentionBtn.addEventListener('click', () => {
      openMention('');
      const input = $('chat-input');
      if (input) {
        if (!/\s$/.test(input.value) && input.value) input.value += ' ';
        input.value += '@';
        input.focus();
      }
    });
  }
  const chatInput = $('chat-input');
  if (chatInput) {
    chatInput.addEventListener('input', () => {
      const match = /@([^\s@，。；、]*)$/.exec(chatInput.value);
      if (match) openMention(match[1]); else closeMention();
    });
    chatInput.addEventListener('blur', () => window.setTimeout(closeMention, 180));
  }
  const chatForm = $('chat-form');
  if (chatForm) {
    ['dragenter', 'dragover'].forEach((type) => {
      chatForm.addEventListener(type, (event) => {
        if (!event.dataTransfer) return;
        event.preventDefault();
        chatForm.classList.add('dragover');
      });
    });
    ['dragleave', 'dragend'].forEach((type) => {
      chatForm.addEventListener(type, () => chatForm.classList.remove('dragover'));
    });
    chatForm.addEventListener('drop', async (event) => {
      if (!event.dataTransfer) return;
      event.preventDefault();
      chatForm.classList.remove('dragover');
      const files = event.dataTransfer.files;
      if (files && files.length) await chatAttachFiles(files);
    });
  }

  $('chat-form').addEventListener('submit', (event) => {
    event.preventDefault();
    startRun();
  });
  $('chat-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      startRun();
    }
  });
  $('chat-send').addEventListener('click', () => { startRun(); });
  $('btn-chat-new').addEventListener('click', () => { startNewConversation(); });

  // 运行方式（仅参数模式使用）
  document.querySelectorAll('#mode-switch .seg-btn').forEach((button) => {
    button.addEventListener('click', () => {
      state.mode = button.dataset.mode;
      document.querySelectorAll('#mode-switch .seg-btn').forEach((other) => {
        other.classList.toggle('active', other === button);
      });
      const agentMode = state.mode === 'agent';
      $('agent-only').classList.toggle('hidden', !agentMode);
      // 对话模式始终显示工具轨迹；参数模式仅在多 Agent 下显示
      const showAgentBox = state.page === 'chat' ? true : agentMode;
      $('agent-box').classList.toggle('hidden', !showAgentBox);
      $('mode-hint').textContent = agentMode
        ? '多 Agent 协作：协调 Agent 调度 3 个子 Agent，输出 Markdown 报告（需要 LLM）。表单参数为权威参数。'
        : '确定性流水线：不需要 LLM，速度快、结果可复现。表单参数为权威参数。';
    });
  });

  // 运行控制
  $('btn-start').addEventListener('click', () => { startRun(); });
  $('btn-stop').addEventListener('click', () => { stopRun(); });

  // Tab
  document.querySelectorAll('.tab-btn').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
  });

  // 结果总览表头排序：走服务端 sort/order（全库排序，而非仅排当前页）
  document.querySelectorAll('#result-table thead th[data-key]').forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.key;
      if (!key) return;
      if (state.ranking.sort === key) {
        state.ranking.order = state.ranking.order === 'asc' ? 'desc' : 'asc';
      } else {
        state.ranking.sort = key;
        state.ranking.order = 'asc';
      }
      reloadResultViews();
    });
  });

  // 结果搜索（q）：输入防抖后回到第 1 页，由服务端过滤名称 / SMILES
  let searchTimer = 0;
  $('ranking-search').addEventListener('input', () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.ranking.query = $('ranking-search').value;
      reloadResultViews();
    }, 320);
  });
  $('ranking-search').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      window.clearTimeout(searchTimer);
      state.ranking.query = $('ranking-search').value;
      reloadResultViews();
    }
  });

  // 只看优于阳性对照（hits_only）
  $('hits-only').addEventListener('change', () => {
    state.ranking.hitsOnly = $('hits-only').checked;
    reloadResultViews();
  });

  // 每页条数（50 / 100 / 200）
  $('page-size').addEventListener('change', () => {
    const value = toNumber($('page-size').value);
    state.ranking.limit = (value === 100 || value === 200) ? value : 50;
    reloadResultViews();
  });

  // 页码：上一页 / 下一页 / 跳转
  $('pager-prev').addEventListener('click', () => {
    const limit = state.ranking.limit;
    loadRankingPage({ offset: Math.max(0, state.ranking.offset - limit) });
  });
  $('pager-next').addEventListener('click', () => {
    const limit = state.ranking.limit;
    const maxOffset = Math.max(0, (rankingTotalPages() - 1) * limit);
    loadRankingPage({ offset: Math.min(maxOffset, state.ranking.offset + limit) });
  });
  $('btn-pager-go').addEventListener('click', () => {
    const target = Math.max(1, Math.min(rankingTotalPages(), Math.round(toNumber($('pager-jump').value) || 1)));
    loadRankingPage({ offset: (target - 1) * state.ranking.limit });
  });
  $('pager-jump').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      $('btn-pager-go').click();
    }
  });

  // 分子详情分页（每页 12 / 24 / 48 张卡片）
  $('cards-page-size').addEventListener('change', () => {
    const value = toNumber($('cards-page-size').value);
    state.cards.limit = (value === 24 || value === 48) ? value : 12;
    state.cards.offset = 0;
    if (state.runId) loadCardsPage({ offset: 0 });
  });
  $('cards-prev').addEventListener('click', () => {
    loadCardsPage({ offset: Math.max(0, state.cards.offset - state.cards.limit) });
  });
  $('cards-next').addEventListener('click', () => {
    const limit = state.cards.limit;
    const maxOffset = Math.max(0, (cardsTotalPages() - 1) * limit);
    loadCardsPage({ offset: Math.min(maxOffset, state.cards.offset + limit) });
  });

  // 导出完整 CSV（不受分页限制）
  $('btn-export-csv').addEventListener('click', (event) => {
    if (event.currentTarget.classList.contains('disabled')) event.preventDefault();
  });

  // 大库规模提示（>500 个分子时给出小样本建议，但不阻止运行）
  $('ligands-text').addEventListener('input', () => { updateConfigInfoBar(); });
  $('max-ligands').addEventListener('input', () => { updateConfigInfoBar(); });
  $('library-select').addEventListener('change', () => { updateConfigInfoBar(); });

  // 阳性对照（v0.5 可选、默认留空）：只有用户主动点击才填入示例对照
  $('btn-fill-positive').addEventListener('click', () => {
    state.touchedFields.add('positive-control');
    const example = state.examplePositive;
    const input = $('positive-control');
    if (!example) {
      setRunHint('示例对照尚未载入（后端未返回 positive_control），请手动填写 SMILES。', 'warn');
      return;
    }
    input.value = example.smiles;
    setRunHint('已填入示例对照 ' + example.name + '（' + example.smiles + '）；本次将执行对照分析。');
  });
  $('btn-clear-positive').addEventListener('click', () => {
    state.touchedFields.add('positive-control');   // 显式选择"不做对照"
    $('positive-control').value = '';
    setRunHint('已清空阳性对照：本次将跳过对照分子对接与结合模式比较。', 'warn');
  });

  // 报告复制
  $('btn-copy-report').addEventListener('click', () => { copyReport(); });

  // 位姿 zip
  $('btn-zip-poses').addEventListener('click', downloadPosesZip);
  $('btn-zip-all').addEventListener('click', (event) => {
    if (event.currentTarget.classList.contains('disabled')) event.preventDefault();
  });

  // 历史
  $('btn-refresh-history').addEventListener('click', () => { refreshHistory(); });
  $('history-more').addEventListener('click', () => {
    state.historyShown += HISTORY_PAGE_SIZE;
    renderHistory();
  });

  // 窗口错误兜底提示
  window.addEventListener('error', (event) => {
    if (event && event.message) logLine('页面脚本错误：' + event.message, 'err');
  });
}

/* ==========================================================================
 * 15. 顶层视图切换（工作台 / 设置）
 *     - 导航栏点击 + URL hash（#/settings）双向同步
 *     - 视图用 .is-active 切换；工作台的运行状态不受影响（SSE 仍在后台推进）
 * ======================================================================== */
const VIEW_IDS = ['view-workbench', 'view-settings'];
/* 工作台上会被设置页默认值预填的字段（用户手动改过的字段不再被覆盖） */
const PREFILL_FIELDS = ['engine-select', 'protonation-select', 'protonation-ph',
  'pocket-engine-select', 'exhaustiveness', 'n-poses', 'max-ligands', 'save-poses',
  'positive-control'];
/* 质子化态策略白名单（与后端 settings.PROTONATION_POLICIES / core.protonation 保持一致） */
const PROTONATION_POLICIES = ['neutralize', 'ph', 'keep'];
/* 目标 pH 允许范围（与后端 docking.protonation_ph 的 0–14 一致）；默认生理 pH */
const PH_MIN = 0.5, PH_MAX = 14, PH_DEFAULT = 7.4;
/* 新手常用的 pH 取值（与后端 PH_PRESETS 一致，只作提示不限制输入） */
const PH_PRESETS = ['1.5', '2.0', '4.5', '5.5', '7.4', '8.0'];

function viewFromHash() {
  const raw = String(location.hash || '').replace(/^#\/?/, '').toLowerCase();
  return raw === 'settings' ? 'settings' : 'workbench';
}

function syncViewHash(view) {
  // 回到工作台时保留当前子页（#chat / #manual），避免从设置页返回时把「参数模式」丢掉
  const want = view === 'settings' ? '#/settings'
    : PAGE_HASH[state.page === 'manual' ? 'manual' : 'chat'];
  if (location.hash === want) return;
  try {
    history.replaceState(null, '', want);
  } catch (error) {
    location.hash = want;
  }
}

/** 只更新视图视觉状态与导航栏，可重复调用 */
function applyViewView(view) {
  const name = view === 'settings' ? 'settings' : 'workbench';
  VIEW_IDS.forEach((id) => {
    const node = $(id);
    if (node) node.classList.toggle('is-active', id === 'view-' + name);
  });
  document.querySelectorAll('#topnav .nav-btn').forEach((button) => {
    const active = button.dataset.view === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.body.dataset.view = name;
}

/** 设置页的「返回工作台」按钮与 Esc 快捷键（用户反馈：设置页没有返回入口）。 */
function bindSettingsBack() {
  const back = $('btn-settings-back');
  if (back) {
    back.addEventListener('click', () => setView('workbench'));
  }
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || state.view !== 'settings') return;
    const tag = String((event.target && event.target.tagName) || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;   // 正在输入时不劫持
    setView('workbench');
  });
}

function setView(view) {
  const next = view === 'settings' ? 'settings' : 'workbench';
  const changed = state.view !== next;
  state.view = next;
  applyViewView(next);
  syncViewHash(next);
  if (next === 'settings') {
    if (!settingsState.loaded) {
      loadSettings(true);
    } else if (changed) {
      setSettingsStatus(settingsState.dirty
        ? '有未保存的修改，点击「保存设置」写入 config/local_settings.json。'
        : '设置已载入。');
    }
  }
}

/* ==========================================================================
 * 16. 设置页面（字段表由服务端 /api/settings 驱动）
 * ======================================================================== */
const settingsState = {
  loaded: false,
  loading: false,
  saving: false,
  data: null,
  initial: {},
  dirty: false,
  models: [],
  modelsError: '',
  testing: '',
  formTouched: new Set()
};

function setSettingsStatus(text, kind) {
  const node = $('settings-status');
  if (!node) return;
  node.textContent = text;
  node.classList.toggle('is-err', kind === 'err');
  node.classList.toggle('is-ok', kind === 'ok');
}

function setSettingsAlert(id, message, kind) {
  const node = $(id);
  if (!node) return;
  if (!message) {
    hide(node);
    node.textContent = '';
    return;
  }
  node.textContent = message;
  show(node);
}

function markSettingsDirty(dirty) {
  settingsState.dirty = Boolean(dirty);
  const dot = $('nav-settings-dot');
  if (dot) dot.classList.toggle('hidden', !settingsState.dirty);
}

function settingsSourceClass(source, spec) {
  if (spec && spec.env_priority && String(source).indexOf('环境变量') === 0) return 'src-protected';
  if (String(source).indexOf('界面设置') === 0) return 'src-ui';
  if (String(source).indexOf('环境变量') === 0) return 'src-env';
  return 'src-default';
}

function settingsValueText(spec, value) {
  if (value === null || value === undefined) return '—';
  if (spec.kind === 'bool') return value ? '启用' : '关闭';
  if (spec.kind === 'json') return JSON.stringify(value);
  return String(value);
}

/** 生成一个字段控件（返回的元素带 data-setting-path / data-setting-kind） */
function settingsInput(spec, value, data) {
  const kind = spec.kind;
  const effective = data.effective[spec.path];
  if (kind === 'bool') {
    const row = el('label', 'check-row');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = value === undefined || value === null ? Boolean(spec.default) : Boolean(value);
    box.dataset.settingPath = spec.path;
    box.dataset.settingKind = 'bool';
    row.appendChild(box);
    row.appendChild(el('span', '', spec.label));
    return row;
  }
  if (kind === 'enum') {
    const select = document.createElement('select');
    select.className = 'set-select';
    select.dataset.settingPath = spec.path;
    select.dataset.settingKind = 'enum';
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = '（不设置 → 继承 / 默认）';
    select.appendChild(empty);
    (spec.choices || []).forEach((choice) => {
      if (choice === '') return;
      const option = document.createElement('option');
      option.value = String(choice);
      option.textContent = String(choice);
      select.appendChild(option);
    });
    select.value = value === null || value === undefined ? '' : String(value);
    return select;
  }
  if (kind === 'json') {
    const area = document.createElement('textarea');
    area.className = 'set-textarea';
    area.spellcheck = false;
    area.dataset.settingPath = spec.path;
    area.dataset.settingKind = 'json';
    area.value = value === null || value === undefined ? '' : JSON.stringify(value, null, 0);
    area.placeholder = spec.placeholder || '留空 = 不发送';
    return area;
  }
  const input = document.createElement('input');
  input.className = 'set-input';
  input.dataset.settingPath = spec.path;
  input.dataset.settingKind = kind;
  if (kind === 'secret') {
    input.type = 'password';
    input.autocomplete = 'new-password';
    const role = spec.role;
    const setFlag = role ? data.secret[role + '_api_key_set'] : data.secret.api_key_set;
    const hint = role ? '' : (data.secret.api_key_hint || '');
    input.placeholder = setFlag ? ('已配置 ' + (hint || '••••') + '（留空 = 不修改）') : '未配置';
  } else if (kind === 'int' || kind === 'float') {
    input.type = 'number';
    if (spec.step) input.step = String(spec.step);
    if (spec.min !== null && spec.min !== undefined) input.min = String(spec.min);
    if (spec.max !== null && spec.max !== undefined) input.max = String(spec.max);
  } else {
    input.type = 'text';
    input.spellcheck = false;
  }
  input.value = value === null || value === undefined ? '' : String(value);
  if (!input.placeholder && spec.placeholder) input.placeholder = spec.placeholder;
  if (spec.readonly) {
    // 只读项（如 PORT）：显示生效值但不可改，提交时也必须跳过（否则整次保存会被后端拒绝）
    input.disabled = true;
    input.dataset.settingReadonly = 'true';
    input.title = '该项由启动参数决定，界面不可修改';
    if (input.value === '') input.value = String(effective === null || effective === undefined ? '' : effective);
  }
  if (spec.field === 'model' || spec.path === 'llm.model') {
    input.setAttribute('list', 'settings-model-list');
  }
  if (effective !== undefined && effective !== null && effective !== '' && kind !== 'secret') {
    input.placeholder = '继承：' + settingsValueText(spec, effective);
  }
  return input;
}

function settingsFieldNode(spec, data) {
  const node = document.createElement('label');
  node.className = 'set-field' + (spec.kind === 'json' || spec.kind === 'secret' ? ' set-field-wide' : '');
  const head = el('span', 'set-label');
  head.appendChild(el('span', 'set-label-text', spec.label));
  head.appendChild(el('span', 'src-chip ' + settingsSourceClass(data.sources[spec.path], spec),
    data.sources[spec.path] || '内置默认'));
  const input = settingsInput(spec, data.values[spec.path], data);
  if (spec.kind === 'bool') {
    // 布尔项：控件本身就是 label，这里只放标题行与说明
    node.appendChild(head);
    node.appendChild(input);
  } else {
    node.appendChild(head);
    node.appendChild(input);
  }
  if (spec.help) node.appendChild(el('p', 'set-help', spec.help));
  return node;
}

function renderSettingsGroups(data) {
  const host = $('settings-groups');
  if (!host) return;
  clear(host);
  const byGroup = {};
  (data.specs || []).forEach((spec) => {
    (byGroup[spec.group] = byGroup[spec.group] || []).push(spec);
  });
  (data.groups || []).forEach((group) => {
    const details = document.createElement('details');
    details.className = 'field-group';
    /* 默认只展开第一组：设置页有 6 组 60+ 字段，全部展开会长到 4700px；
       其余组靠左侧锚点 / 点击标题展开（内容仍是同一份 DOM，行为不变）。 */
    details.open = group.id === (data.groups || [])[0]?.id;
    details.dataset.group = group.id;
    details.id = SETTINGS_GROUP_ID_PREFIX + group.id;
    const summary = document.createElement('summary');
    summary.appendChild(el('span', 'settings-group-title', group.label));
    const specsForGroup = byGroup[group.id] || [];
    if (specsForGroup.length) {
      summary.appendChild(el('span', 'settings-group-count', String(specsForGroup.length) + ' 项'));
    }
    details.appendChild(summary);
    if (group.help) details.appendChild(el('p', 'hint', group.help));

    const specs = byGroup[group.id] || [];
    if (group.id === 'roles') {
      details.appendChild(renderRoleCards(specs, data));
    } else if (group.id === 'models') {
      details.appendChild(renderModelsPanel(data));
    } else if (specs.length) {
      const grid = el('div', 'set-grid');
      specs.forEach((spec) => grid.appendChild(settingsFieldNode(spec, data)));
      details.appendChild(grid);
    }
    host.appendChild(details);
  });
  // 关键字：模型名可下拉选择（datalist 由 renderModelsPanel 生成）
  renderModelDatalist(data);
  renderSettingsAnchor(data.groups || []);
}

/** 设置分组容器的 id 前缀（DOM 里由 renderSettingsGroups 生成） */
const SETTINGS_GROUP_ID_PREFIX = 'settings-group-';

/** 设置页左侧锚点导航：点击展开对应分组并平滑滚动（长页面少滚动、好定位） */
function renderSettingsAnchor(groups) {
  const host = $('settings-anchor');
  if (!host) return;
  clear(host);
  if (!groups.length) return;
  host.appendChild(el('span', 'settings-anchor-title', '// 分区'));
  groups.forEach((group) => {
    const link = el('a', 'settings-anchor-link', group.label);
    link.href = '#' + SETTINGS_GROUP_ID_PREFIX + group.id;
    link.dataset.target = SETTINGS_GROUP_ID_PREFIX + group.id;
    link.addEventListener('click', (event) => {
      event.preventDefault();
      // 前缀用常量拼接：字面量直接写进 getElementById 会被静态门禁误判为「引用了不存在的 id」
      const target = document.getElementById(SETTINGS_GROUP_ID_PREFIX + group.id);
      if (!target) return;
      target.open = true;
      if (typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start' });
      }
      host.querySelectorAll('.settings-anchor-link').forEach((n) => n.classList.remove('active'));
      link.classList.add('active');
    });
    host.appendChild(link);
  });
  if (typeof IntersectionObserver === 'function') {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const id = entry.target.id;
        host.querySelectorAll('.settings-anchor-link').forEach((n) => {
          n.classList.toggle('active', n.dataset.target === id);
        });
      });
    }, { rootMargin: '-20% 0px -70% 0px' });
    groups.forEach((group) => {
      // 前缀用常量拼接：字面量直接写进 getElementById 会被静态门禁误判为「引用了不存在的 id」
      const target = document.getElementById(SETTINGS_GROUP_ID_PREFIX + group.id);
      if (target) observer.observe(target);
    });
  }
}

function renderRoleCards(specs, data) {
  const grid = el('div', 'role-grid');
  (data.roles || []).forEach((role) => {
    const card = el('div', 'role-card');
    card.dataset.role = role;
    const head = el('div', 'role-head');
    head.appendChild(el('span', 'role-name', (data.role_labels || {})[role] || role));
    const badge = el('span', 'src-chip src-default', role);
    head.appendChild(badge);
    card.appendChild(head);

    const body = el('div', 'role-body');
    const grid2 = el('div', 'set-grid');
    specs.filter((spec) => spec.role === role).forEach((spec) => {
      grid2.appendChild(settingsFieldNode(spec, data));
    });
    body.appendChild(grid2);

    const actions = el('div', 'role-actions');
    const test = el('button', 'btn btn-ghost btn-sm', '测试连通性');
    test.type = 'button';
    test.dataset.roleTest = role;
    actions.appendChild(test);
    const inherit = el('button', 'btn btn-ghost btn-sm', '全部继承全局');
    inherit.type = 'button';
    inherit.dataset.roleClear = role;
    actions.appendChild(inherit);
    const result = el('span', 'role-test', '');
    result.dataset.roleTestResult = role;
    actions.appendChild(result);
    body.appendChild(actions);
    card.appendChild(body);
    grid.appendChild(card);
  });
  return grid;
}

function renderModelDatalist(data) {
  let list = document.getElementById('settings-model-list');
  if (!list) {
    list = document.createElement('datalist');
    list.id = 'settings-model-list';
    document.body.appendChild(list);
  }
  clear(list);
  const current = new Set();
  (data.specs || []).forEach((spec) => {
    const value = data.values[spec.path];
    if (spec.field === 'model' && value) current.add(String(value));
  });
  (settingsState.models || []).forEach((model) => {
    const option = document.createElement('option');
    option.value = model;
    list.appendChild(option);
  });
  void current;
}

function renderModelsPanel(data) {
  const box = el('div', '');
  box.appendChild(el('p', 'hint',
    '从当前端点（' + ((data.meta && data.meta.base_url) || '见 LLM 接入') +
    '）的 GET /models 拉取可用模型；模型输入框带自动补全，点击下方标签填入「默认模型」。'));
  const chips = el('div', 'model-chips');
  chips.id = 'settings-model-chips';
  if (settingsState.modelsError) {
    chips.appendChild(el('span', 'role-test is-err', settingsState.modelsError));
  } else if (!settingsState.models.length) {
    chips.appendChild(el('span', 'hint', '尚未拉取。点击上方「拉取可用模型」。'));
  } else {
    settingsState.models.forEach((model) => {
      const chip = el('button', 'chip', model);
      chip.type = 'button';
      chip.dataset.modelChip = model;
      chips.appendChild(chip);
    });
  }
  box.appendChild(chips);

  const current = (data.agent_models || {});
  const keys = Object.keys(current);
  box.appendChild(el('p', 'settings-sub', '本进程已构建的各 Agent 实例'));
  if (!keys.length) {
    box.appendChild(el('p', 'settings-meta', '尚未构建（首次运行或点击「重新加载 Agent」后构建）。'));
  } else {
    const lines = keys.map((role) => {
      const meta = current[role] || {};
      return role + ' → ' + (meta.actual_model || meta.model || '—') +
        '（调用 ' + (meta.calls || 0) + ' 次 · 实例 ' + (meta.instance_id || '—') + '）';
    });
    box.appendChild(el('p', 'settings-meta', lines.join('　|　')));
  }
  return box;
}

function renderSettings(data) {
  settingsState.data = data;
  settingsState.loaded = true;
  settingsState.initial = {};
  const specMap = {};
  (data.specs || []).forEach((spec) => { specMap[spec.path] = spec; });
  Object.keys(data.values || {}).forEach((path) => {
    const value = data.values[path];
    const spec = specMap[path];
    // 初始快照必须与控件显示的状态完全一致，否则「没改过」也会被判定为已修改
    let text;
    if (spec && spec.kind === 'bool') {
      const shown = (value === null || value === undefined) ? Boolean(spec.default) : Boolean(value);
      text = shown ? 'true' : 'false';
    } else if (value === null || value === undefined) {
      text = '';
    } else if (typeof value === 'object') {
      text = JSON.stringify(value);
    } else {
      text = String(value);
    }
    settingsState.initial[path] = text;
  });
  const hint = $('settings-file-hint');
  if (hint) {
    const meta = data.meta || {};
    hint.textContent = (meta.exists ? '配置文件：' : '尚未创建：') + (meta.relative || '') +
      (meta.path ? '（' + meta.path + '）' : '');
  }
  renderSettingsGroups(data);
  markSettingsDirty(false);
}

async function loadSettings(force) {
  if (settingsState.loading) return;
  if (settingsState.loaded && !force) return;
  settingsState.loading = true;
  setSettingsStatus('正在加载设置…');
  try {
    const data = await getJson('/api/settings');
    renderSettings(data);
    applySettingsToWorkbench(data);
    setSettingsStatus('设置已载入（' + (data.specs || []).length + ' 个配置项）。');
    setSettingsAlert('settings-error', '');
  } catch (error) {
    setSettingsStatus('加载失败：' + shortError(error), 'err');
    setSettingsAlert('settings-error', '设置加载失败：' + shortError(error));
  } finally {
    settingsState.loading = false;
  }
}

function setByPath(target, path, value) {
  const parts = path.split('.');
  let node = target;
  parts.slice(0, -1).forEach((part) => {
    if (typeof node[part] !== 'object' || node[part] === null) node[part] = {};
    node = node[part];
  });
  node[parts[parts.length - 1]] = value;
  return target;
}

/** 只收集**改动过**的字段，避免把继承值固化成显式设置 */
function collectSettingsDraft() {
  const updates = {};
  document.querySelectorAll('[data-setting-path]').forEach((node) => {
    // 只读项（deploy.port 等）由启动参数决定：既不参与「已修改」判定，也不提交
    if (node.dataset.settingReadonly === 'true' || node.disabled) return;
    const path = node.dataset.settingPath;
    const kind = node.dataset.settingKind;
    let value;
    if (kind === 'bool') value = node.checked ? 'true' : 'false';
    else value = node.value;
    const current = kind === 'bool'
      ? (settingsState.initial[path] === 'true' ? 'true' : 'false')
      : String(settingsState.initial[path] === undefined ? '' : settingsState.initial[path]);
    if (String(value) !== current) setByPath(updates, path, value);
  });
  return updates;
}

function refreshDirtyState() {
  const updates = collectSettingsDraft();
  markSettingsDirty(Object.keys(updates).length > 0);
  return updates;
}

/** 保存失败时把出错的字段标红并聚焦（校验错误带 path） */
function markInvalidSettings(errors) {
  document.querySelectorAll('[data-setting-path].is-invalid').forEach((node) => {
    node.classList.remove('is-invalid');
  });
  if (!Array.isArray(errors) || !errors.length) return;
  let first = null;
  errors.forEach((item) => {
    if (!item || !item.path) return;
    const node = document.querySelector('[data-setting-path="' + item.path + '"]');
    if (!node) return;
    node.classList.add('is-invalid');
    node.title = item.message || '';
    if (!first) first = node;
  });
  if (first && typeof first.focus === 'function') first.focus();
}

/** 改动里是否包含「需要重启服务才生效」的项 */
function settingsNeedsRestart(paths) {
  const specs = (settingsState.data || {}).specs || [];
  const restart = new Set(specs.filter((s) => s.needs_restart).map((s) => s.path));
  return paths.filter((path) => restart.has(path));
}

async function saveSettings() {
  if (settingsState.saving) return;
  const updates = collectSettingsDraft();
  const paths = Object.keys(updates);
  if (!paths.length) {
    setSettingsStatus('没有需要保存的修改。', 'ok');
    return;
  }
  settingsState.saving = true;
  setSettingsStatus('正在保存 ' + paths.length + ' 项…');
  try {
    const out = await reqJson('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings: updates })
    });
    renderSettings(out.settings);
    applySettingsToWorkbench(out.settings, true);
    const applied = (out.applied_env || []).length;
    const needReload = paths.some((p) => p.indexOf('llm.') === 0 || p.indexOf('roles.') === 0);
    let message = '已保存 ' + paths.length + ' 项到 config/local_settings.json';
    if (applied) message += '，已刷新 ' + applied + ' 个运行参数';
    if (needReload) {
      message += '；模型/端点改动需重载 Agent 后生效，正在重载…';
      setSettingsStatus(message);
      const reload = await reqJson('/api/settings/reload', { method: 'POST' });
      message = '已保存 ' + paths.length + ' 项，并已重载 Agent（下次运行使用新模型）。';
      void reload;
    }
    const restart = settingsNeedsRestart(paths);
    if (restart.length) {
      message += '；其中 ' + restart.join('、') + ' 需要**重启服务**后才生效。';
    }
    setSettingsStatus(message + '。', 'ok');
    setSettingsAlert('settings-info', message + '。', 'info');
    setSettingsAlert('settings-error', '');
  } catch (error) {
    const detail = (error.payload && error.payload.error_message) || shortError(error);
    setSettingsStatus('保存失败：' + detail, 'err');
    setSettingsAlert('settings-error', '保存失败：' + detail);
    markInvalidSettings(error.payload && error.payload.errors);
  } finally {
    settingsState.saving = false;
  }
}

async function resetSettings() {
  if (!window.confirm('清除界面设置（删除 config/local_settings.json），回到 .env + 内置默认？')) return;
  try {
    const out = await reqJson('/api/settings/reset', { method: 'POST' });
    renderSettings(out.settings);
    applySettingsToWorkbench(out.settings, true);
    setSettingsStatus(out.removed ? '界面设置已清除。' : '本来就没有界面设置。', 'ok');
    setSettingsAlert('settings-info', '已回到 .env + 内置默认。', 'info');
  } catch (error) {
    setSettingsStatus('清除失败：' + shortError(error), 'err');
  }
}

async function reloadAgents() {
  setSettingsStatus('正在重新加载 Agent 与模型实例…');
  try {
    await reqJson('/api/settings/reload', { method: 'POST' });
    await loadSettings(true);
    setSettingsStatus('已重载：下次运行会按当前设置重新构建各 Agent 的模型实例。', 'ok');
  } catch (error) {
    setSettingsStatus('重载失败：' + shortError(error), 'err');
  }
}

async function fetchModels() {
  setSettingsStatus('正在从端点拉取模型列表…');
  settingsState.modelsError = '';
  try {
    const out = await getJson('/api/models');
    if (out.status !== 'ok') {
      settingsState.models = [];
      settingsState.modelsError = '拉取失败：' + (out.error || '未知错误');
      setSettingsStatus(settingsState.modelsError, 'err');
    } else {
      settingsState.models = out.models || [];
      setSettingsStatus('拉取到 ' + settingsState.models.length + ' 个模型。', 'ok');
    }
    if (settingsState.data) renderSettingsGroups(settingsState.data);
  } catch (error) {
    settingsState.modelsError = '拉取失败：' + shortError(error);
    setSettingsStatus(settingsState.modelsError, 'err');
    if (settingsState.data) renderSettingsGroups(settingsState.data);
  }
}

async function testRole(role) {
  if (settingsState.testing) return;
  settingsState.testing = role;
  const node = document.querySelector('[data-role-test-result="' + role + '"]');
  if (node) {
    node.className = 'role-test';
    node.textContent = '测试中…';
  }
  try {
    const out = await reqJson('/api/settings/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role: role })
    });
    if (node) {
      if (out.status === 'ok') {
        node.className = 'role-test is-ok';
        node.textContent = '连通 ' + (out.actual_model || out.model || '') +
          ' · ' + out.latency_ms + 'ms · 回显「' + (out.sample || '').slice(0, 12) + '」';
      } else {
        node.className = 'role-test is-err';
        node.textContent = '失败：' + (out.error || '未知错误');
      }
    }
  } catch (error) {
    if (node) {
      node.className = 'role-test is-err';
      node.textContent = '失败：' + shortError(error);
    }
  } finally {
    settingsState.testing = '';
  }
}

/** 用设置里的对接默认值预填工作台表单（用户改过的字段不覆盖） */
function applySettingsToWorkbench(data, force) {
  const eff = (data && data.effective) || {};
  const apply = (id, value, setter) => {
    if (value === null || value === undefined || value === '') return;
    if (!force && settingsState.formTouched.has(id)) return;
    const node = $(id);
    if (!node) return;
    setter(node, value);
  };
  apply('engine-select', eff['docking.engine'], (node, value) => { node.value = String(value); });
  apply('protonation-select', eff['docking.protonation'], (node, value) => {
    const text = String(value);
    if (PROTONATION_POLICIES.indexOf(text) >= 0) node.value = text;
  });
  apply('protonation-ph', eff['docking.protonation_ph'], (node, value) => {
    const num = toNumber(value);
    if (num !== null) node.value = String(num);
  });
  syncProtonationFields();
  apply('pocket-engine-select', eff['runtime.pocket_engine'], (node, value) => {
    const text = String(value);
    node.value = (text === 'auto' || text === '') ? '' : text;
  });
  apply('exhaustiveness', eff['docking.exhaustiveness'], (node, value) => {
    node.value = String(value);
  });
  apply('n-poses', eff['docking.n_poses'], (node, value) => { node.value = String(value); });
  apply('max-ligands', eff['docking.max_ligands'], (node, value) => { node.value = String(value); });
  apply('save-poses', eff['docking.save_poses'], (node, value) => { node.checked = Boolean(value); });
  apply('positive-control', eff['docking.positive_control'], (node, value) => {
    if (!node.value) node.value = String(value);
  });
  settingsState.settingsDefaults = eff;
  syncExhaustivenessAuto();
  updateConfigInfoBar();
  updateParamSummaries();
}

function bindViewEvents() {
  document.querySelectorAll('#topnav .nav-btn').forEach((button) => {
    button.addEventListener('click', () => { setView(button.dataset.view); });
  });
  // 设置页返回入口（按钮 + Esc），见 bindSettingsBack
  bindSettingsBack();

  const save = $('btn-settings-save');
  if (save) save.addEventListener('click', saveSettings);
  const reset = $('btn-settings-reset');
  if (reset) reset.addEventListener('click', resetSettings);
  const reload = $('btn-settings-reload');
  if (reload) reload.addEventListener('click', reloadAgents);
  const models = $('btn-fetch-models');
  if (models) models.addEventListener('click', fetchModels);

  const groups = $('settings-groups');
  if (groups) {
    groups.addEventListener('input', (event) => {
      const node = event.target;
      if (!node || !node.dataset || !node.dataset.settingPath) return;
      node.classList.remove('is-invalid');
      node.title = '';
      refreshDirtyState();
      if (settingsState.dirty) {
        setSettingsStatus('有未保存的修改，点击「保存设置」写入 config/local_settings.json。');
      }
    });
    groups.addEventListener('change', (event) => {
      const node = event.target;
      if (!node || !node.dataset || !node.dataset.settingPath) return;
      refreshDirtyState();
    });
    groups.addEventListener('click', (event) => {
      const chip = event.target && event.target.closest ? event.target.closest('[data-model-chip]') : null;
      if (chip) {
        const input = document.querySelector('[data-setting-path="llm.model"]');
        if (input) {
          input.value = chip.dataset.modelChip;
          refreshDirtyState();
          setSettingsStatus('已把「' + chip.dataset.modelChip + '」填入默认模型，记得保存。');
        }
        event.preventDefault();
        return;
      }
      const target = event.target && event.target.closest ? event.target.closest('button') : null;
      if (!target || !target.dataset) return;
      if (target.dataset.roleTest) { testRole(target.dataset.roleTest); event.preventDefault(); }
      else if (target.dataset.roleClear) {
        const role = target.dataset.roleClear;
        document.querySelectorAll('[data-setting-path^="roles.' + role + '."]').forEach((field) => {
          if (field.dataset.settingKind === 'bool') field.checked = false;
          else field.value = '';
        });
        refreshDirtyState();
        setSettingsStatus('已把「' + role + '」的所有字段置为继承全局，记得保存。');
        event.preventDefault();
      }
    });
  }

  // 质子化态策略 ↔ 目标 pH 联动
  const protonationSelect = $('protonation-select');
  if (protonationSelect) {
    protonationSelect.addEventListener('change', syncProtonationFields);
    syncProtonationFields();
  }

  /* 谁被用户改过，谁才下发（未改动 = 自动/系统默认）。位点盒这类专家项同理：
     手填坐标或点「填入上传位点」才算显式指定。 */
  const INJECTABLE_IDS = ['engine-select', 'protonation-select', 'protonation-ph', 'n-poses',
    'exhaustiveness', 'positive-control', 'pocket-engine-select', 'max-ligands', 'save-poses',
    'center-x', 'center-y', 'center-z', 'size-x', 'size-y', 'size-z'];
  INJECTABLE_IDS.forEach((id) => {
    const node = $(id);
    if (!node) return;
    const mark = () => { state.touchedFields.add(id); updateParamSummaries(); };
    node.addEventListener('input', mark);
    node.addEventListener('change', mark);
  });
  /* 「搜索强度：自动」：勾上 = 不下发（回到自动规划），取消 = 显式使用滑块数值 */
  const autoBox = $('exhaustiveness-auto');
  if (autoBox) {
    autoBox.addEventListener('change', () => {
      syncExhaustivenessAuto();
      if (autoBox.checked) state.touchedFields.delete('exhaustiveness');
      else state.touchedFields.add('exhaustiveness');
      updateParamSummaries();
    });
    syncExhaustivenessAuto();
  }

  // 记录工作台里被用户手动改过的字段（设置页预填时不覆盖用户输入）
  PREFILL_FIELDS.forEach((id) => {
    const node = $(id);
    if (!node) return;
    const mark = () => { settingsState.formTouched.add(id); };
    node.addEventListener('input', mark);
    node.addEventListener('change', mark);
  });

  // hash 深链接：#/settings ↔ 工作台
  window.addEventListener('hashchange', () => {
    const view = viewFromHash();
    if (view !== state.view) {
      setView(view);
      return;
    }
    if (view === 'workbench') {
      const page = pageFromHash();
      if (page && page !== state.page) setPage(page);
    }
  });
}

async function init() {
  // 初始顶层视图：优先 URL hash（#/settings），否则「工作台」
  const initialView = viewFromHash();
  state.view = initialView;
  // 初始子页：优先 URL hash（#chat / #manual），否则默认「对话模式」
  const initialPage = pageFromHash();
  if (initialPage) state.page = initialPage;
  bindStaticEvents();
  bindViewEvents();
  /* 会话 id：从 localStorage 恢复（刷新后继续同一段对话），没有则生成一个 */
  state.conversationId = loadConversationId();
  setConversationIdLabel();
  setProgress(0);
  updateProgressDone();
  renderRanking();
  renderCards();
  renderArtifacts();
  renderCharts();
  renderChatHistory();
  // 编排示意图初始为「全部待机」
  resetOrchestration();
  renderProgressBarText();
  // 阳性对照默认留空：相似度列与对照行默认隐藏
  applyPositiveVisibility();
  $('token-stream').dataset.prompt = '[--:--:--] $ ';
  // 高级设置默认折叠，未展开时不向服务端注入任何参数
  applyPageView(state.page);
  applyViewView(state.view);
  syncHash(state.page);
  syncViewHash(state.view);
  await Promise.all([loadHealth(), loadReceptors(), loadLibraries(), loadSettings(true)]);
  updateConfigInfoBar();
  updateExportLink();
  await refreshHistory();
  setRunHint(state.page === 'chat'
    ? '就绪：在「对话模式」输入指令后点「发送」（Enter 发送 / Shift+Enter 换行）。'
    : (state.page === 'chat'
      ? '就绪：输入指令后按 Enter 或点「发送」。'
      : '就绪：填写参数后点击「开始运行」。'));
  if (state.view === 'settings') {
    setSettingsStatus('设置已载入（' + ((settingsState.data || {}).specs || []).length + ' 个配置项）。');
  }
  /* 启动流程（含设置 / 受体 / 历史加载）真正结束的标记：
     自动化脚本（scripts/ui_e2e.js）据此等待，避免在 init 的异步续体还没跑完时关窗。 */
  window.__dshReady = true;
}

init();
