/* Markdown → DOM 渲染器（两套界面共用的**唯一实现**）。
 *
 * 为什么单独成文件：简易模式（默认首页）原先把助手回执当纯文本塞进 `<p>`，
 * 于是模型输出的 Markdown 在页面上是「## 对接结果 / - MOL1 / **加粗**」这种源码形态，
 * 与市面主流对话产品的观感差距极大；而高级模式早就有一份能用的渲染器。
 * 两份实现必然发散，所以把它抽到这里：index.html 与 simple.html 都在各自客户端之前
 * 加载本文件，`window.DockingMarkdown` 是唯一入口。
 *
 * 安全（不变式，勿破坏）：
 *   1. 先 `escapeHtml` 再按 Markdown 规则生成标签 —— 原文里的 HTML 永远不会变成标签；
 *   2. 链接经 `safeUrl` 白名单（`http/https/站内绝对路径/锚点`），挡掉 `javascript:`、
 *      `data:`、`vbscript:` 以及协议相对写法 `//evil.example`；非法方案退化成纯文本；
 *   3. 图片同样过白名单，失败走捕获阶段委托降级为占位文案（不用内联 onerror，避开 CSP）。
 */
(function () {
  'use strict';

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function escapeHtml(value) {
    return String(value === undefined || value === null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function safeUrl(url) {
    const raw = String(url === null || url === undefined ? '' : url).trim();
    if (!raw) return '#';
    /* `\/(?!\/)` 允许站内绝对路径，但挡住协议相对写法 `//evil.example/x` */
    if (/^(https?:|#|\/(?!\/)|\.\/|\.\.\/)/i.test(raw)) return raw;
    return '#';
  }

  /**
   * 报告里的内嵌图片：![alt](url) → 图片容器 + 原图链接 + 失败占位。
   * - 容器 max-width:100% + overflow:hidden（见 styles.css），大图绝不撑破布局；
   * - 图片本体 display:block、max-width:100%、loading=lazy/decoding=async；
   * - 点击在新标签打开原图（target=_blank + rel=noopener）；
   * - 加载失败时隐藏图片并显示「图片加载失败」占位文案，不出现破图。
   */
  function imageHtml(url, alt) {
    const href = escapeHtml(safeUrl(url));
    const safeAlt = escapeHtml(alt || '图片');
    const fallback = escapeHtml(alt ? ('图片加载失败：' + alt) : '图片加载失败（资源不可用）');
    return '<span class="md-img-box">' +
      '<a class="md-img-link" href="' + href + '" target="_blank" rel="noopener">' +
      '<img class="md-img" src="' + href + '" alt="' + safeAlt + '" loading="lazy" decoding="async">' +
      '<span class="md-img-fallback" hidden>' + fallback + '</span>' +
      '</a></span>';
  }

  /** 图片加载失败降级（捕获阶段委托；`error` 不冒泡但会被捕获）。只需装一次。 */
  function installImageFallback(doc) {
    if (!doc || doc.__mdImgFallbackInstalled) return;
    doc.__mdImgFallbackInstalled = true;
    doc.addEventListener('error', (event) => {
      const node = event.target;
      if (!node || node.tagName !== 'IMG' || !node.classList.contains('md-img')) return;
      node.style.display = 'none';
      const box = node.parentNode;
      const fallback = box ? box.querySelector('.md-img-fallback') : null;
      if (fallback) fallback.hidden = false;
    }, true);
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
    /* 其它链接（报告里指向 run 产物等）：先过方案白名单再输出，新标签打开 */
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      (match, label, url) => {
        const href = safeUrl(url);
        return href === '#'
          ? label
          : '<a href="' + escapeHtml(href) + '" target="_blank" rel="noopener">' + label + '</a>';
      });
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


  /* ------------------------------------------------------------------------
   * 大块工具数据折叠（两套界面共用）
   *
   * 真实反馈（2026-09-24）：协调 Agent 把工具返回的口袋/配体 JSON 原样粘进正文，
   * 气泡被几万字符的数据占满。工具回执本身只进右侧「工具轨迹」，所以这些 JSON 是
   * **模型自己抄的**——提示词约束不足以保证，必须在渲染前做确定性处理。
   *
   * `splitDataBlocks(markdown)` 把正文切成 [{kind:'md'},{kind:'data'}]：
   * 只认「看起来就是 JSON」的大块（自己成段、成对括号、长度 ≥ 阈值），
   * 普通正文（包括代码片段、含大括号的句子）原样保留。
   * ---------------------------------------------------------------------- */
  var DATA_BLOCK_MIN_CHARS = 600;

  function _balancedJsonEnd(text, start) {
    var depth = 0, inStr = false, esc = false;
    for (var i = start; i < text.length; i += 1) {
      var ch = text[i];
      if (inStr) {
        if (esc) { esc = false; } else if (ch === '\\') { esc = true; }
        else if (ch === '"') { inStr = false; }
        continue;
      }
      if (ch === '"') { inStr = true; continue; }
      if (ch === '{' || ch === '[') { depth += 1; continue; }
      if (ch === '}' || ch === ']') {
        depth -= 1;
        if (depth === 0) { return i + 1; }
      }
    }
    return -1;
  }

  function splitDataBlocks(markdown, minChars) {
    var source = String(markdown || '');
    var limit = minChars || DATA_BLOCK_MIN_CHARS;
    var parts = [];
    var buffer = '';
    var i = 0;
    var flush = function () {
      if (buffer) { parts.push({ kind: 'md', text: buffer }); buffer = ''; }
    };
    while (i < source.length) {
      var ch = source[i];
      var atBoundary = (i === 0) || source[i - 1] === '\n';
      if ((ch === '{' || ch === '[') && atBoundary) {
        var end = _balancedJsonEnd(source, i);
        var chunk = end > 0 ? source.slice(i, end) : '';
        var looksJson = chunk.length >= limit && chunk.indexOf('"') !== -1 && chunk.indexOf(':') !== -1;
        var tailOk = end > 0 && (end >= source.length || /[\s`]/.test(source[end]));
        if (looksJson && tailOk) {
          flush();
          parts.push({ kind: 'data', text: chunk });
          i = end;
          continue;
        }
      }
      buffer += ch;
      i += 1;
    }
    flush();
    return parts;
  }

  window.DockingMarkdown = {
    el: el,
    escapeHtml: escapeHtml,
    safeUrl: safeUrl,
    imageHtml: imageHtml,
    installImageFallback: installImageFallback,
    renderInline: renderInline,
    renderMarkdown: renderMarkdown,
    /* 大块工具数据折叠：正文只留结论，数据进可展开块（两套界面共用同一实现） */
    splitDataBlocks: splitDataBlocks,
    /* 报告抬头表解析也要用：表格切分规则只有一份 */
    splitTableRow: splitTableRow,
    isTableSeparator: isTableSeparator,
  };
})();
