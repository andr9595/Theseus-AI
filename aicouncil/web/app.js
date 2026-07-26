/* ==========================================================================
   AI Council - dashboard client
   Vanilla ES2020, no framework, no bundler. Loaded as a classic script so it
   satisfies the `script-src 'self'` CSP with no inline handlers anywhere.

   Contents
     1. Utilities        6. State + rendering
     2. API client       7. Live stream
     3. Markdown         8. Modals (picker, settings) + conversations
     4. Highlighter      9. Event stream (SSE)
     5. Diff viewer     10. Boot
   ========================================================================== */
'use strict';

/* ==========================================================================
   1. Utilities
   ========================================================================== */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Escape text for safe interpolation into an HTML string.
 *  Every piece of agent output passes through here before it reaches innerHTML. */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtDuration(seconds) {
  if (!seconds || seconds < 0) return '';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  return `${m}m ${Math.round(seconds % 60)}s`;
}

function fmtWhen(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return sameDay ? time : `${d.toLocaleDateString()} ${time}`;
}

function toast(message, kind = 'info', ttl = 5200) {
  const host = $('#toasts');
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.textContent = message;
  host.appendChild(node);
  setTimeout(() => {
    node.classList.add('out');
    setTimeout(() => node.remove(), 240);
  }, ttl);
}

/* ==========================================================================
   2. API client
   ========================================================================== */

/** The launcher hands us the session token in the query string. Strip it from
 *  the visible URL immediately so it does not end up in a screenshot, a
 *  bookmark or the window title. */
const TOKEN = (() => {
  const params = new URLSearchParams(location.search);
  const t = params.get('token') || sessionStorage.getItem('ac_token') || '';
  if (params.get('token')) {
    sessionStorage.setItem('ac_token', t);
    history.replaceState(null, '', location.pathname);
  }
  return t;
})();

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: {
      'X-AC-Token': TOKEN,
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  return data;
}

/* ==========================================================================
   3. Markdown renderer
   Deliberately small: the subset that coding agents actually emit.
   ========================================================================== */

/** Render inline spans. Input is raw text; output is escaped HTML.
 *  Code spans are lifted out first so their contents are never treated as
 *  emphasis or link syntax. */
function inlineMd(text) {
  const codes = [];
  // Lift `code` and ``code with ` inside`` out of the way.
  let work = String(text).replace(/(`+)([\s\S]*?)\1/g, (_, ticks, code) => {
    codes.push(code.trim());
    return `\u0000${codes.length - 1}\u0000`;
  });

  work = esc(work);

  // [label](url) - only http(s), mailto and relative targets are linkified;
  // anything else (javascript:, data:) is left as literal text.
  work = work.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g,
    (m, label, url) => {
      if (/^(https?:\/\/|mailto:|\/|#|\.)/i.test(url)) {
        return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      }
      return m;
    });

  // Bare URLs.
  work = work.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
    (_, pre, url) => `${pre}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);

  work = work.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
  work = work.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  work = work.replace(/(^|[^*\w])\*([^*\n]+)\*(?![*\w])/g, '$1<em>$2</em>');
  work = work.replace(/(^|[^_\w])_([^_\n]+)_(?![_\w])/g, '$1<em>$2</em>');
  work = work.replace(/~~([^~]+)~~/g, '<del>$1</del>');

  // Restore code spans, escaping their contents now.
  return work.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
}

let codeBlockSeq = 0;

function codeBlockHtml(lang, code) {
  const id = `cb${++codeBlockSeq}`;
  const label = lang || 'text';
  return (
    `<div class="code-block">` +
      `<div class="code-head">` +
        `<span class="code-lang">${esc(label)}</span>` +
        `<button class="copy-btn" data-copy="${id}" type="button">Copy</button>` +
      `</div>` +
      `<pre><code id="${id}" data-raw="${esc(code)}">${highlight(code, lang)}</code></pre>` +
    `</div>`
  );
}

/** Convert a Markdown document to HTML. */
function renderMarkdown(src) {
  if (!src || !src.trim()) return '';
  const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let i = 0;

  // Stack of currently-open list elements, tracked by indent width so nested
  // lists close in the right order.
  const listStack = [];

  const closeLists = (toIndent = -1) => {
    while (listStack.length && listStack[listStack.length - 1].indent > toIndent) {
      out.push(`</${listStack.pop().tag}>`);
    }
  };

  while (i < lines.length) {
    const line = lines[i];

    // -- fenced code block ------------------------------------------------
    const fence = line.match(/^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/);
    if (fence) {
      closeLists();
      const marker = fence[2][0];
      const len = fence[2].length;
      const lang = fence[3];
      const body = [];
      i++;
      while (i < lines.length) {
        const close = lines[i].match(/^(\s*)(`{3,}|~{3,})\s*$/);
        if (close && close[2][0] === marker && close[2].length >= len) { i++; break; }
        body.push(lines[i]);
        i++;
      }
      out.push(codeBlockHtml(lang, body.join('\n')));
      continue;
    }

    // -- blank ------------------------------------------------------------
    if (!line.trim()) { closeLists(); i++; continue; }

    // -- heading ----------------------------------------------------------
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeLists();
      const level = Math.min(heading[1].length, 4);
      out.push(`<h${level}>${inlineMd(heading[2].trim())}</h${level}>`);
      i++;
      continue;
    }

    // -- horizontal rule --------------------------------------------------
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      closeLists();
      out.push('<hr>');
      i++;
      continue;
    }

    // -- table ------------------------------------------------------------
    // Requires a header row followed by a |---|---| separator.
    if (line.includes('|') && i + 1 < lines.length &&
        /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      closeLists();
      const cells = (row) => row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|');
      out.push('<table><thead><tr>');
      cells(line).forEach(c => out.push(`<th>${inlineMd(c.trim())}</th>`));
      out.push('</tr></thead><tbody>');
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        out.push('<tr>');
        cells(lines[i]).forEach(c => out.push(`<td>${inlineMd(c.trim())}</td>`));
        out.push('</tr>');
        i++;
      }
      out.push('</tbody></table>');
      continue;
    }

    // -- blockquote -------------------------------------------------------
    if (/^\s*>/.test(line)) {
      closeLists();
      const quoted = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        quoted.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      out.push(`<blockquote>${renderMarkdown(quoted.join('\n'))}</blockquote>`);
      continue;
    }

    // -- list item --------------------------------------------------------
    const item = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (item) {
      const indent = item[1].replace(/\t/g, '    ').length;
      const tag = /^\d/.test(item[2]) ? 'ol' : 'ul';
      closeLists(indent);
      const top = listStack[listStack.length - 1];
      if (!top || top.indent < indent) {
        listStack.push({ indent, tag });
        out.push(`<${tag}>`);
      } else if (top.tag !== tag) {
        out.push(`</${listStack.pop().tag}>`);
        listStack.push({ indent, tag });
        out.push(`<${tag}>`);
      }
      // Absorb continuation lines that are indented under this item.
      const parts = [item[3]];
      i++;
      while (i < lines.length && lines[i].trim() &&
             !lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+/) &&
             (lines[i].match(/^\s*/)[0].length > indent)) {
        parts.push(lines[i].trim());
        i++;
      }
      out.push(`<li>${inlineMd(parts.join(' '))}</li>`);
      continue;
    }

    // -- paragraph --------------------------------------------------------
    closeLists();
    const para = [];
    while (i < lines.length && lines[i].trim() &&
           !/^(#{1,6})\s/.test(lines[i]) &&
           !/^\s*(`{3,}|~{3,})/.test(lines[i]) &&
           !/^\s*>/.test(lines[i]) &&
           !/^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i]) &&
           !/^\s*([-*_])(\s*\1){2,}\s*$/.test(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    if (para.length) out.push(`<p>${inlineMd(para.join('\n'))}</p>`);
  }

  closeLists();
  return out.join('\n');
}

/* ==========================================================================
   4. Syntax highlighter
   One tokenizer covering the C-family, Python, Go, Rust, shell and markup.
   Good enough to read a diff by; not a parser.
   ========================================================================== */

const KEYWORDS = new Set((
  'abstract als and any as async await base bool break byte case catch chan ' +
  'char class const constexpr continue crate declare def default defer del ' +
  'delete do double elif else end enum event except export extends false ' +
  'final finally float fn for from func function global go goto if impl ' +
  'implements import in instanceof int interface is lambda let let* loop ' +
  'macro map match mod module move mut namespace new nil none not null ' +
  'nullptr operator or package pass private protected pub public raise range ' +
  'readonly ref return select self short sizeof static string struct super ' +
  'switch template this throw throws trait true try type typedef typeof ' +
  'union unsafe use using var void volatile where while with yield'
).split(' '));

const LANG_ALIASES = {
  js: 'js', javascript: 'js', jsx: 'js', ts: 'js', typescript: 'js', tsx: 'js',
  py: 'py', python: 'py', rb: 'py', ruby: 'py',
  sh: 'sh', bash: 'sh', zsh: 'sh', shell: 'sh', console: 'sh',
  go: 'c', rust: 'c', rs: 'c', c: 'c', cpp: 'c', 'c++': 'c', h: 'c',
  java: 'c', cs: 'c', csharp: 'c', swift: 'c', kotlin: 'c', php: 'c',
  json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'yaml', ini: 'yaml',
  html: 'xml', xml: 'xml', svg: 'xml', diff: 'diff', patch: 'diff',
};

function highlight(code, lang) {
  const family = LANG_ALIASES[String(lang || '').toLowerCase()] || 'c';
  if (family === 'diff') return highlightDiffText(code);

  // Line comments differ per family; `#` would eat a C preprocessor line and
  // `//` would be a path in shell, so pick the right one up front.
  const lineComment = (family === 'py' || family === 'sh' || family === 'yaml')
    ? '#' : '\\/\\/';
  const blockComment = (family === 'py' || family === 'sh' || family === 'yaml')
    ? '' : '\\/\\*[\\s\\S]*?\\*\\/|';

  const pattern = new RegExp(
    '(' + blockComment + lineComment + '[^\\n]*)' +          // 1 comment
    '|("""[\\s\\S]*?"""|\'\'\'[\\s\\S]*?\'\'\')' +            // 2 triple string
    '|("(?:[^"\\\\\\n]|\\\\.)*"|\'(?:[^\'\\\\\\n]|\\\\.)*\'|`(?:[^`\\\\]|\\\\.)*`)' + // 3 string
    '|\\b(0[xXbBoO][0-9a-fA-F_]+|\\d[\\d_]*\\.?[\\d_]*(?:[eE][+-]?\\d+)?)\\b' +       // 4 number
    '|\\b([A-Za-z_$][\\w$]*)(?=\\s*\\()' +                    // 5 call
    '|\\b([A-Z][A-Za-z0-9_]*)\\b' +                           // 6 type-ish
    '|\\b([a-z_$][\\w$]*)\\b',                                // 7 word
    'g'
  );

  let out = '';
  let last = 0;
  let m;
  while ((m = pattern.exec(code)) !== null) {
    out += esc(code.slice(last, m.index));
    last = pattern.lastIndex;
    if (m[1])      out += `<span class="tok-com">${esc(m[1])}</span>`;
    else if (m[2]) out += `<span class="tok-str">${esc(m[2])}</span>`;
    else if (m[3]) out += `<span class="tok-str">${esc(m[3])}</span>`;
    else if (m[4]) out += `<span class="tok-num">${esc(m[4])}</span>`;
    else if (m[5]) {
      out += KEYWORDS.has(m[5])
        ? `<span class="tok-kw">${esc(m[5])}</span>`
        : `<span class="tok-fn">${esc(m[5])}</span>`;
    }
    else if (m[6]) out += `<span class="tok-typ">${esc(m[6])}</span>`;
    else if (m[7]) {
      out += KEYWORDS.has(m[7])
        ? `<span class="tok-kw">${esc(m[7])}</span>`
        : esc(m[7]);
    }
  }
  out += esc(code.slice(last));
  return out;
}

/** Colourise a diff pasted inside a fenced block (not the Diff tab). */
function highlightDiffText(code) {
  return code.split('\n').map(line => {
    if (/^\+\+\+|^---/.test(line)) return `<span class="tok-com">${esc(line)}</span>`;
    if (line.startsWith('+'))      return `<span class="tok-str">${esc(line)}</span>`;
    if (line.startsWith('-'))      return `<span class="tok-num">${esc(line)}</span>`;
    if (line.startsWith('@@'))     return `<span class="tok-kw">${esc(line)}</span>`;
    return esc(line);
  }).join('\n');
}

/* ==========================================================================
   5. Diff viewer
   ========================================================================== */

/** Parse a unified diff into per-file records with hunks and line numbers. */
function parseDiff(text) {
  const files = [];
  let current = null;
  const lines = String(text || '').split('\n');
  let oldNo = 0;
  let newNo = 0;

  for (const line of lines) {
    // git prefixes even a blank context line with a space, so a genuinely
    // empty line is never content - it is the separator between two
    // concatenated file diffs, or the trailing newline. Skipping it here
    // keeps a phantom row from appearing at the end of every file.
    if (line === '') continue;

    if (line.startsWith('diff --git ')) {
      const m = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
      current = {
        path: m ? m[2] : line.slice(11),
        oldPath: m ? m[1] : '',
        status: '',
        rows: [],
        add: 0,
        del: 0,
      };
      files.push(current);
      continue;
    }

    // `git diff --no-index` output for untracked files has no `diff --git`
    // preamble in every git version; synthesise a file record from `+++`.
    if (!current && (line.startsWith('--- ') || line.startsWith('+++ '))) {
      current = { path: line.slice(4).replace(/^[ab]\//, ''), oldPath: '', status: 'new', rows: [], add: 0, del: 0 };
      files.push(current);
      continue;
    }
    if (!current) continue;

    if (line.startsWith('new file mode')) { current.status = 'new'; continue; }
    if (line.startsWith('deleted file mode')) { current.status = 'deleted'; continue; }
    if (line.startsWith('rename from') || line.startsWith('rename to')) {
      current.status = 'renamed';
      continue;
    }
    if (/^(index |similarity |old mode|new mode|--- |\+\+\+ |Binary files)/.test(line)) {
      if (line.startsWith('Binary files')) {
        current.rows.push({ kind: 'hunk', text: line });
      }
      continue;
    }

    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/);
    if (hunk) {
      oldNo = parseInt(hunk[1], 10);
      newNo = parseInt(hunk[2], 10);
      current.rows.push({ kind: 'hunk', text: line });
      continue;
    }

    if (line.startsWith('+')) {
      current.rows.push({ kind: 'add', text: line.slice(1), newNo });
      current.add++; newNo++;
    } else if (line.startsWith('-')) {
      current.rows.push({ kind: 'del', text: line.slice(1), oldNo });
      current.del++; oldNo++;
    } else if (line.startsWith('\\')) {
      current.rows.push({ kind: 'meta', text: line });
    } else {
      current.rows.push({ kind: 'ctx', text: line.slice(1), oldNo, newNo });
      oldNo++; newNo++;
    }
  }
  return files;
}

function renderDiff(text, stat) {
  const files = parseDiff(text);
  if (!files.length) {
    return '<div class="empty-state">No changes in the working tree.</div>';
  }

  const totalAdd = stat && stat.insertions != null
    ? stat.insertions : files.reduce((n, f) => n + f.add, 0);
  const totalDel = stat && stat.deletions != null
    ? stat.deletions : files.reduce((n, f) => n + f.del, 0);

  const parts = [
    `<div class="diff-summary">` +
      `<strong>${files.length}</strong> file${files.length === 1 ? '' : 's'} changed` +
      `<span class="add">+${totalAdd}</span>` +
      `<span class="del">&minus;${totalDel}</span>` +
    `</div>`,
  ];

  for (const file of files) {
    // Collapse very large files by default so the page stays navigable.
    const collapsed = file.rows.length > 600 ? ' collapsed' : '';
    const tag = file.status
      ? `<span class="diff-tag ${file.status}">${file.status}</span>` : '';

    const rows = file.rows.map(row => {
      if (row.kind === 'hunk' || row.kind === 'meta') {
        return `<div class="diff-row hunk">` +
                 `<span class="diff-gutter"></span><span class="diff-mark"></span>` +
                 `<span class="diff-text">${esc(row.text)}</span></div>`;
      }
      const gutter = row.kind === 'add' ? row.newNo
                   : row.kind === 'del' ? row.oldNo
                   : row.newNo;
      const mark = row.kind === 'add' ? '+' : row.kind === 'del' ? '-' : ' ';
      return `<div class="diff-row ${row.kind}">` +
               `<span class="diff-gutter">${gutter == null ? '' : gutter}</span>` +
               `<span class="diff-mark">${mark}</span>` +
               `<span class="diff-text">${esc(row.text)}</span></div>`;
    }).join('');

    parts.push(
      `<div class="diff-file${collapsed}">` +
        `<div class="diff-file-head">` +
          `<span class="diff-caret">&#9660;</span>` +
          `<span class="diff-path">${esc(file.path)}</span>${tag}` +
          `<span class="diff-counts"><span class="add">+${file.add}</span> ` +
          `<span class="del">&minus;${file.del}</span></span>` +
        `</div>` +
        `<div class="diff-body">${rows}</div>` +
      `</div>`
    );
  }
  return parts.join('');
}

/* ==========================================================================
   6. State + rendering
   ========================================================================== */

const state = {
  config: null,
  run: null,
  providers: [],
  // Agents assignable to either job, served by /api/state. The browser never
  // holds its own copy of a command or a permission flag.
  agents: [],
  repoStatus: null,
  usage: {},
  roles: [],
  busy: false,
  streamLines: [],
  activeTab: 'stream',
  // The transcript the next run continues, if any. Held as a filename because
  // that is what the server accepts; the task text is kept alongside it purely
  // so the composer can name what is being continued.
  continueFrom: '',
  continueTask: '',
  // What replaying that transcript costs, from /api/context, and whether the
  // operator asked to compact it before the next run.
  continueContext: null,
  compactContext: false,
  // The conversation list in the sidebar, and the one open in the main pane.
  chats: [],
  openChat: null,
};

const STATE_LABELS = {
  idle: 'Idle',
  drafting: 'Drafting',
  awaiting_approval: 'Awaiting your approval',
  polishing: 'Applying changes',
  complete: 'Complete',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

function renderStatus() {
  const run = state.run;
  const pill = $('#status-pill');
  const s = run ? run.state : 'idle';

  pill.className = 'status-pill';
  if (s === 'drafting' || s === 'polishing') pill.classList.add('active');
  else if (s === 'awaiting_approval') pill.classList.add('waiting');
  else if (s === 'complete') pill.classList.add('ok');
  else if (s === 'failed' || s === 'cancelled') pill.classList.add('bad');

  $('#status-text').textContent = STATE_LABELS[s] || s;

  const meta = [];
  if (run) {
    meta.push(`run ${run.id}`);
    if (run.zero_touch) meta.push('ZERO-TOUCH');
    if (run.solo) meta.push('solo');
    if (run.work_branch) meta.push(run.work_branch);
    if (run.diff_stat && run.diff_stat.files) {
      meta.push(`${run.diff_stat.files} file(s) +${run.diff_stat.insertions}/-${run.diff_stat.deletions}`);
    }
    if (run.error) meta.push(run.error);
  }
  $('#topbar-meta').textContent = meta.join('  ·  ');

  $('#cancel-btn').classList.toggle('hidden', !state.busy);
  $('#rollback-btn').classList.toggle('hidden', !(run && run.can_rollback));
  $('#pr-btn').classList.toggle('hidden', !(run && run.pull_request && run.pull_request.url));
  // A finished run is the one an operator most often wants to follow up on,
  // so it is offered here rather than only from History.
  $('#continue-btn').classList.toggle('hidden', !(run && !state.busy && run.file));
  renderContinuation();

  const runBtn = $('#run-btn');
  const hasRepo = !!(state.config && state.config.target_repo);
  const hasTask = $('#task-input').value.trim().length > 0;
  runBtn.disabled = state.busy || !hasRepo || !hasTask;
  runBtn.classList.toggle('busy', state.busy);
  $('.btn-label', runBtn).textContent = state.busy ? 'Council in session' : 'Convene the council';

  const gated = !!(run && run.state === 'awaiting_approval');
  $('#approval-gate').classList.toggle('hidden', !gated);
  if (gated) {
    // Solo Mode has no draft to show, so the gate is approving the run itself
    // rather than a proposal.
    $('#approval-copy').innerHTML = run.solo
      ? 'Solo Mode: there is no draft to review. <b>Nothing has been written to ' +
        'disk yet.</b> Approving lets the senior stage work directly on your ' +
        'repository.'
      : 'The draft is ready — see the <b>Draft</b> tab. <b>Nothing has been ' +
        'written to disk yet.</b> Approving lets the senior stage apply changes ' +
        'to your repository.';
  }
}

/** One line describing what a thread costs to replay.
 *  Every figure here is an estimate and says so: no CLI reports its tokenizer's
 *  count back to us, and the window is a configured number, not a vendor one.
 *  It also covers the replayed conversation only — the task, the draft and
 *  whatever the agent reads for itself land in the same window. */
function contextLine(context) {
  if (!context || !context.stored_turns) return '';
  const parts = [`${context.stored_turns} earlier message${context.stored_turns === 1 ? '' : 's'}`];
  // Thousands once there are thousands: "~0.4k tokens" is a worse reading of
  // 441 than the number itself.
  parts.push(context.estimated_tokens >= 1000
    ? `~${Math.round(context.estimated_tokens / 100) / 10}k tokens`
    : `~${context.estimated_tokens} tokens`);
  if (context.percent != null) {
    parts.push(`≈${context.percent}% of a ${Math.round(context.window_tokens / 1000)}k window`);
  }
  if (context.compacted_turns) {
    parts.push(`${context.compacted_turns} compacted`);
  }
  return parts.join(' · ');
}

/** True once the replay is close enough to the history budget that the next
 *  follow-up will be compacted whether or not the operator asks. */
function contextIsTight(context) {
  return !!context && !!context.budget_characters &&
    context.characters >= context.budget_characters * 0.75;
}

/** Show what the next run will be handed, if it is a follow-up. Standing state
 *  the operator can see and undo — a placeholder would vanish on the first
 *  keystroke and leave the attachment invisible. */
function renderContinuation() {
  $('#continue-banner').classList.toggle('hidden', !state.continueFrom);
  $('#continue-task').textContent = state.continueTask || 'an earlier run';

  const context = state.continueContext;
  const meter = $('#continue-context');
  meter.textContent = context
    ? contextLine(state.compactContext ? context.compacted : context)
    : '';
  meter.classList.toggle('warn', contextIsTight(context) && !state.compactContext);

  // The button stays put whenever a conversation is attached, and says why it
  // cannot do anything when it cannot. Hiding it instead made a working
  // feature read as a missing one - the same mistake as dropping a stage card
  // in Solo Mode - and it is the control the operator goes looking for by
  // name, so it has to be findable before it is useful.
  const btn = $('#compact-btn');
  const room = !!(context && context.compacted &&
    context.compacted.compacted_turns > context.compacted_turns);
  btn.classList.toggle('hidden', !state.continueFrom);
  btn.classList.toggle('active', state.compactContext);
  btn.disabled = !room && !state.compactContext;
  btn.textContent = state.compactContext ? 'Compacted ✓' : 'Compact';
  btn.title = state.compactContext
    ? 'Earlier turns will be summarised. Click to send them in full instead.'
    : room
      ? 'Summarise every earlier turn, keeping the newest in full.'
      : !context
        ? 'Measuring what this conversation replays…'
        : 'Nothing to compact yet — the newest message is always sent in ' +
          'full, and it is the only one here.';
}

/** Attach a transcript to the next run, and measure what that will replay. */
async function continueRun(file, task) {
  state.continueFrom = file;
  state.continueTask = (task || '').slice(0, 90);
  state.continueContext = null;
  state.compactContext = false;
  renderStatus();
  $('#task-input').focus();
  toast('That conversation is attached. Type your follow-up.', 'ok', 4200);

  try {
    const { context } = await api(`/api/context?file=${encodeURIComponent(file)}`);
    // The operator may have detached or switched conversation while this was
    // in flight; a stale reading is worse than none.
    if (state.continueFrom !== file) return;
    state.continueContext = context;
    renderContinuation();
  } catch (err) {
    toast(`Could not measure the conversation's context: ${err.message}`, 'warn');
  }
}

function clearContinuation() {
  state.continueFrom = '';
  state.continueTask = '';
  state.continueContext = null;
  state.compactContext = false;
  renderContinuation();
}

/** Which catalogued agent a provider runs, judged by its executable — the
 *  same rule the server applies in `config.agent_for`. */
function agentOf(provider) {
  const exe = String(((provider || {}).command || [])[0] || '').split('/').pop();
  const hit = state.agents.find(a => (a.command || []).length && exe.includes(a.id));
  return hit ? hit.id : 'custom';
}

function renderAgents() {
  const rail = $('#agent-rail');
  const run = state.run;
  const conf = state.config || {};
  const providers = conf.providers || {};
  // Always render both stages. Solo Mode used to drop the drafter card
  // entirely, which reads as "Codex is missing" rather than "you switched this
  // off" - a disabled stage should look disabled, not absent.
  const order = ['drafter', 'polisher'];
  // A run carries the stage it was started with; outside a run, whatever is
  // configured now. Either way it is the *other* slot that sits idle.
  const solo = run ? run.solo : !!conf.solo_mode;
  const finalStage = solo
    ? ((run && run.solo_stage) || conf.solo_stage || 'polisher')
    : 'polisher';
  const soloSkipped = (id) => solo && id !== finalStage;

  const probeFor = (id) => state.providers.find(p => p.id === id);

  rail.innerHTML = order.map((id, idx) => {
    const provider = providers[id] || {};
    const stage = run && run.stages ? run.stages[id] : null;
    const info = probeFor(id);
    const available = !info || info.available;

    let stageState = stage ? stage.state : 'pending';
    if (run && run.state === 'awaiting_approval' && id === finalStage) stageState = 'waiting';
    if (soloSkipped(id)) stageState = 'skipped';

    const label = soloSkipped(id) ? 'solo · off'
      : { pending: 'idle', running: 'working', done: 'done',
          failed: 'failed', skipped: 'skipped',
          waiting: 'gated' }[stageState] || stageState;

    const duration = stage && stage.duration ? ` · ${fmtDuration(stage.duration)}` : '';
    const initial = (provider.label || id).slice(0, 2).toUpperCase();
    // An empty model means the CLI picks; say so rather than showing a blank.
    const modelLabel = provider.model || 'default model';
    // Same for effort. Shown only when the command has the knob at all, so a
    // hand-written template does not sprout a chip that cannot do anything.
    const hasEffort = (provider.effort_args || []).length > 0;

    return (
      `<div class="agent-card ${stageState} ${available ? '' : 'unavailable'}" data-agent="${id}">` +
        `<div class="agent-avatar">${esc(initial)}</div>` +
        `<div class="agent-body">` +
          `<div class="agent-name">${esc(provider.label || id)}</div>` +
          `<div class="agent-role">${idx + 1}. ${esc(provider.role || '')}</div>` +
          (available ? '' :
            `<div class="agent-missing">${esc(provider.command ? provider.command[0] : '?')} not found</div>`) +
          // Say *why* the stage is inert, and how to undo it — an unexplained
          // greyed-out card is barely better than a missing one.
          (soloSkipped(id)
            ? `<button class="agent-hint" type="button" data-disable-solo="1">` +
              `Skipped by Solo mode — click to re-enable</button>`
            : '') +
        `</div>` +
        `<div class="agent-right">` +
          `<div class="agent-state">${esc(label)}${esc(duration)}</div>` +
          usageChipHtml(id) +
          `<div class="chip-row">` +
            `<button class="model-chip${provider.model ? ' set' : ''}" type="button" ` +
              `data-model-for="${id}" title="Change the model for this stage">` +
              `${esc(modelLabel)}` +
              `<svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" ` +
              `stroke-width="3" stroke-linecap="round" stroke-linejoin="round">` +
              `<path d="M6 9l6 6 6-6"/></svg>` +
            `</button>` +
            (hasEffort
              ? `<button class="model-chip effort-chip${provider.effort ? ' set' : ''}" ` +
                `type="button" data-effort-for="${id}" ` +
                `title="How hard this stage is asked to think">` +
                `${esc(provider.effort || 'default effort')}` +
                `<svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" ` +
                `stroke-width="3" stroke-linecap="round" stroke-linejoin="round">` +
                `<path d="M6 9l6 6 6-6"/></svg>` +
              `</button>`
              : '') +
          `</div>` +
        `</div>` +
      `</div>`
    );
  }).join('');

  // The CSP forbids inline style attributes, so bar widths are set through the
  // CSSOM after the markup lands. Writing `style="width:24%"` into the string
  // above looks correct in the DOM inspector and renders as a 0px bar.
  $$('[data-fill]', rail).forEach(el => { el.style.width = `${el.dataset.fill}%`; });
}


/** Quota chip. Shows the vendor's own percentage, or says plainly that the
 *  agent cannot report one — never a number this app inferred. */
function fmtAge(minutes) {
  if (minutes < 90) return `${Math.round(minutes)} min`;
  const h = minutes / 60;
  return h < 36 ? `${Math.round(h)} h` : `${Math.round(h / 24)} d`;
}

function usageChipHtml(providerId) {
  const u = (state.usage || {})[providerId];
  if (!u) return '';
  if (!u.supported) {
    return `<span class="usage-chip none" title="${esc(u.note || '')}">no quota data</span>`;
  }
  if (!u.worst) {
    const why = u.error || 'Not checked yet.';
    return `<span class="usage-chip none" title="${esc(why)}">quota ?</span>`;
  }
  // Lead with the shortest window — Claude's 5-hour session is what actually
  // stops work; its weekly moves slowly and is the wrong number to read at a
  // glance. Codex has only a weekly, so that is what it leads with.
  const lead = u.primary || u.worst;
  const pct = Math.round(lead.percent);
  // Colour tracks the *worst* limit, not the one on display: a weekly at 95%
  // must turn the chip red even while the session sits at 10%.
  const worstPct = Math.round(u.worst.percent);
  const level = worstPct >= 90 ? 'crit' : worstPct >= 75 ? 'warn' : 'ok';
  // Flag when the colour is being driven by a limit other than the number.
  const hidden = u.worst !== lead && worstPct >= 75 && u.worst.label !== lead.label;
  // Codex's figure comes from its last run's log, not a live query. Mark it
  // once it is old enough to mislead, rather than presenting stale as current.
  const ageMin = lead.as_of ? (Date.now() / 1000 - lead.as_of) / 60 : 0;
  const stale = ageMin > 30;
  const line = l => `${l.label === lead.label ? '▸ ' : '  '}${l.label}: `
    + `${l.percent}% used${l.resets ? ` · resets ${l.resets}` : ''}`;
  const tip = u.limits.map(line).join('\n')
    + (hidden ? `\n\n${u.worst.label} is the constraint right now.` : '')
    + (u.note ? `\n\n${u.note}` : '')
    + (stale ? `\n(measured ${fmtAge(ageMin)} ago)` : '')
    + (u.error ? `\n\n(last poll failed: ${u.error})` : '');
  return (
    `<button class="usage-chip ${level}" type="button" data-usage-for="${providerId}" ` +
      `title="${esc(tip)}">` +
      `<span class="usage-bar"><span data-fill="${Math.min(100, pct)}"></span></span>` +
      `${pct}%${hidden ? '<span class="usage-other">!</span>' : ''}${stale ? '<span class="usage-stale">*</span>' : ''}` +
    `</button>`
  );
}

/** Highest reported usage across the agents a run will actually use. */
function worstUsageFor(providerIds) {
  let worst = null;
  for (const id of providerIds) {
    const u = (state.usage || {})[id];
    if (u && u.worst && (!worst || u.worst.percent > worst.percent)) {
      worst = { ...u.worst, agent: id };
    }
  }
  return worst;
}


/* ==========================================================================
   Commit
   ========================================================================== */

/** Show the commit bar whenever the target repo has uncommitted work, whether
 *  a council run produced it or you did. Hidden mid-run: committing under a
 *  running agent captures a tree it is still editing. */
function renderCommitBar() {
  const bar = $('#commit-bar');
  if (!bar) return;
  const st = state.repoStatus;
  const dirty = st && st.is_repo ? st.dirty_count : 0;
  const show = dirty > 0 && !state.busy;
  bar.classList.toggle('hidden', !show);
  if (!show) return;

  $('#commit-hint').textContent =
    `${dirty} uncommitted change${dirty === 1 ? '' : 's'} on ${st.branch || '?'}`;
  const box = $('#commit-message');
  if (!box.value && state.run && state.run.task) {
    // The task is a reasonable first draft of the subject; the operator can
    // rewrite it, and an empty message is refused server-side either way.
    box.value = state.run.task.split('\n')[0].slice(0, 72);
  }
}

async function doCommit() {
  const message = $('#commit-message').value.trim();
  if (!message) { toast('A commit message is required.', 'warn'); return; }
  const btn = $('#commit-btn');
  btn.disabled = true;
  try {
    const { commit } = await api('/api/commit', {
      method: 'POST',
      body: { message, repo: (state.config || {}).target_repo },
    });
    $('#commit-message').value = '';
    toast(
      `Committed ${commit.short} on ${commit.branch} — ${commit.files} file(s), ` +
      `+${commit.insertions}/-${commit.deletions}`, 'ok', 6000);
    await loadState();
  } catch (err) {
    toast(err.message, 'error', 9000);
  } finally {
    btn.disabled = false;
  }
}

/* ==========================================================================
   Role manager
   ========================================================================== */

function renderRoleList() {
  const host = $('#role-list');
  if (!host) return;
  host.innerHTML = (state.roles || []).map(r => `
    <div class="role-row" data-role="${esc(r.id)}">
      <div class="role-row-head">
        <span class="role-name">${esc(r.name)}</span>
        <span class="role-badges">
          ${r.builtin ? '<span class="role-badge">built-in</span>' : '<span class="role-badge custom">custom</span>'}
          ${r.edited ? '<span class="role-badge edited">edited</span>' : ''}
          ${r.writes ? '<span class="role-badge writes">writes</span>' : '<span class="role-badge">read-only</span>'}
        </span>
        <button class="link-btn role-edit" type="button">Edit</button>
      </div>
      <div class="role-summary">${esc(r.summary || '')}</div>
    </div>`).join('');
}

/** One editor for every role — built-in or not, the same form. */
function openRoleEditor(roleId) {
  const existing = (state.roles || []).find(r => r.id === roleId);
  const isNew = !existing;
  const r = existing || { id: '', name: '', summary: '', system: '', writes: false,
                          builtin: false, edited: false };

  const dlg = document.createElement('div');
  dlg.className = 'modal';
  dlg.id = 'role-editor';
  dlg.innerHTML = `
    <div class="modal-card modal-card-tall">
      <header class="modal-head">
        <h3>${isNew ? 'New role' : `Edit ${esc(r.name)}`}</h3>
        <button class="icon-btn" data-close-role type="button" aria-label="Close">&times;</button>
      </header>
      <div class="modal-body">
        <div class="field-row">
          <div class="field">
            <label>Name</label>
            <input type="text" id="role-f-name" value="${esc(r.name)}">
          </div>
          <div class="field">
            <label>Id ${r.builtin || !isNew ? '<span class="field-hint">— fixed</span>' : ''}</label>
            <input type="text" id="role-f-id" value="${esc(r.id)}"
              ${isNew ? '' : 'disabled'} placeholder="e.g. perf_reviewer">
          </div>
        </div>
        <div class="field">
          <label>Summary <span class="field-hint">— one line, shown in the dropdown</span></label>
          <input type="text" id="role-f-summary" value="${esc(r.summary || '')}">
        </div>
        <div class="field field-check">
          <label class="checkline">
            <input type="checkbox" id="role-f-writes" ${r.writes ? 'checked' : ''}>
            This behaviour expects to modify files
          </label>
          <span class="field-hint">
            Advisory: permission is granted per stage, and Settings flags a mismatch.
          </span>
        </div>
        <div class="field">
          <label>Prompt</label>
          <textarea id="role-f-system" rows="18" class="role-system">${esc(r.system || '')}</textarea>
        </div>
      </div>
      <footer class="modal-foot">
        ${r.builtin && r.edited
          ? '<button class="btn btn-quiet" id="role-restore" type="button">Restore shipped text</button>'
          : (!r.builtin && !isNew
             ? '<button class="btn btn-quiet" id="role-delete" type="button">Delete role</button>'
             : '')}
        <span class="modal-foot-spacer"></span>
        <button class="btn btn-quiet" data-close-role type="button">Cancel</button>
        <button class="btn btn-primary" id="role-save" type="button">Save</button>
      </footer>
    </div>`;
  document.body.appendChild(dlg);

  const close = () => dlg.remove();
  $$('[data-close-role]', dlg).forEach(b => b.addEventListener('click', close));
  dlg.addEventListener('click', e => { if (e.target === dlg) close(); });

  $('#role-save', dlg).addEventListener('click', async () => {
    try {
      const { roles } = await api('/api/roles', { method: 'POST', body: {
        id: isNew ? $('#role-f-id', dlg).value : r.id,
        name: $('#role-f-name', dlg).value,
        summary: $('#role-f-summary', dlg).value,
        system: $('#role-f-system', dlg).value,
        writes: $('#role-f-writes', dlg).checked,
      }});
      state.roles = roles;
      renderRoleList(); renderSettings();
      close();
      toast('Role saved.', 'ok', 3000);
    } catch (err) { toast(err.message, 'error', 8000); }
  });

  const remove = $('#role-restore', dlg) || $('#role-delete', dlg);
  if (remove) remove.addEventListener('click', async () => {
    const builtin = !!r.builtin;
    if (!confirm(builtin
      ? `Restore "${r.name}" to its shipped text? Your edits are discarded.`
      : `Delete "${r.name}"? Stages using it fall back to the stage default.`)) return;
    try {
      const { roles } = await api('/api/roles/delete', { method: 'POST', body: { id: r.id } });
      state.roles = roles;
      renderRoleList(); renderSettings();
      close();
      toast(builtin ? 'Shipped text restored.' : 'Role deleted.', 'ok', 3000);
    } catch (err) { toast(err.message, 'error', 8000); }
  });
}

function renderRepo() {
  const conf = state.config || {};
  const repo = conf.target_repo || '';
  const btn = $('#repo-btn');
  btn.classList.toggle('unset', !repo);
  $('#repo-label').textContent = repo || 'Choose a repository…';
  btn.title = repo || '';

  const meta = $('#repo-meta');
  const st = state.repoStatus;
  if (repo && st) {
    const chips = [];
    if (st.is_repo) {
      chips.push(`<span class="chip">${esc(st.branch || '?')}</span>`);
      if (st.head) chips.push(`<span class="chip">${esc(st.head.slice(0, 7))}</span>`);
      chips.push(st.clean
        ? '<span class="chip clean">clean</span>'
        : `<span class="chip dirty">${st.dirty_count} uncommitted</span>`);
    } else {
      chips.push(`<span class="chip warn">${esc(st.error || 'not a git repo')}</span>`);
    }
    meta.innerHTML = chips.join('');
    meta.classList.remove('hidden');
  } else {
    meta.classList.add('hidden');
  }

  const recent = (conf.recent_repos || []).filter(r => r !== repo).slice(0, 4);
  $('#recent-repos').innerHTML = recent.map(r =>
    `<button class="recent-item" data-repo="${esc(r)}" type="button" title="${esc(r)}">${esc(r)}</button>`
  ).join('');
}

function renderToggles() {
  const c = state.config || {};
  $('#zero-touch').checked = !!c.zero_touch;
  $('#safety-snapshot').checked = c.safety_snapshot !== false;
  $('#solo-mode').checked = !!c.solo_mode;
  $('#clean-worktree').checked = !!c.require_clean_worktree;
  $('#pull-request-mode').checked = !!c.pull_request_mode;
  $('#zero-touch-warning').classList.toggle('hidden', !c.zero_touch);

  // Pull-request mode changes what the other two delivery toggles are worth,
  // and neither one is switched off for you: it enforces a clean tree itself
  // whatever "Require clean tree" says, and it takes rollback away the moment
  // the PR exists - but not before, which is exactly when publishing fails.
  const pr = !!c.pull_request_mode;
  $('#clean-tree-note').textContent = pr
    ? 'Enforced by Pull request mode regardless'
    : 'Refuse to run on uncommitted work';
  $('#snapshot-note').textContent = pr
    ? 'Rollback until the pull request is open'
    : 'Enable one-click rollback';

  // Which stage's configuration runs alone. Only worth showing once Solo mode
  // is actually on.
  const providers = c.providers || {};
  const soloStage = c.solo_stage || 'polisher';
  $('#solo-stage').innerHTML = ['drafter', 'polisher'].map((id, idx) =>
    `<option value="${id}"${id === soloStage ? ' selected' : ''}>` +
    `Stage ${idx + 1} · ${esc((providers[id] || {}).label || id)}</option>`
  ).join('');
  $('#solo-target').classList.toggle('hidden', !c.solo_mode);
}

function renderOutputs() {
  const run = state.run;
  const draft = run && run.stages && run.stages.drafter ? run.stages.drafter.output : '';
  const final = run && run.stages && run.stages.polisher ? run.stages.polisher.output : '';

  const draftView = $('#draft-view');
  if (draft && draft.trim()) {
    draftView.innerHTML = renderMarkdown(draft);
    draftView.classList.remove('empty-state');
    setBadge('#badge-draft', 'ready');
  } else {
    setBadge('#badge-draft', '');
  }

  const finalView = $('#final-view');
  if (final && final.trim()) {
    finalView.innerHTML = renderMarkdown(final);
    finalView.classList.remove('empty-state');
    setBadge('#badge-final', 'ready');
  } else {
    setBadge('#badge-final', '');
  }

  const diffView = $('#diff-view');
  if (run && run.diff && run.diff.trim()) {
    diffView.innerHTML = renderDiff(run.diff, run.diff_stat);
    diffView.classList.remove('empty-state');
    const files = (run.diff_stat && run.diff_stat.files) || 0;
    setBadge('#badge-diff', files ? `${files}` : '', true);
  } else {
    setBadge('#badge-diff', '');
  }
}

function setBadge(sel, text, positive = false) {
  const node = $(sel);
  node.textContent = text;
  node.classList.toggle('hidden', !text);
  node.classList.toggle('add', positive);
}

function renderAll() {
  renderStatus();
  renderAgents();
  renderRepo();
  renderToggles();
  renderOutputs();
  renderCommitBar();
}

/* ==========================================================================
   7. Live stream
   ========================================================================== */

const MAX_STREAM_LINES = 4000;

function pushLine(kind, tag, text) {
  const stream = $('#stream');
  const follow = $('#autoscroll').checked;
  const nearBottom =
    stream.scrollHeight - stream.parentElement.scrollTop - stream.parentElement.clientHeight < 120;

  const row = document.createElement('div');
  row.className = `line ${kind}`;
  const tagEl = document.createElement('span');
  tagEl.className = 'line-tag';
  tagEl.textContent = tag;
  const bodyEl = document.createElement('span');
  bodyEl.className = 'line-body';
  bodyEl.textContent = text;
  row.append(tagEl, bodyEl);
  stream.appendChild(row);

  // Bound the DOM: a chatty agent can emit tens of thousands of lines.
  while (stream.childElementCount > MAX_STREAM_LINES) {
    stream.removeChild(stream.firstElementChild);
  }

  if (follow && nearBottom) {
    stream.parentElement.scrollTop = stream.parentElement.scrollHeight;
  }
}

function pushDivider(text) {
  const stream = $('#stream');
  const div = document.createElement('div');
  div.className = 'stream-divider';
  div.textContent = text;
  stream.appendChild(div);
}

function clearStream() {
  $('#stream').innerHTML = '';
}

/* ==========================================================================
   8. Model and effort pickers
   Both lists are asked for at open time rather than shipped: Codex publishes
   an account-scoped catalogue and Claude will name its own effort levels when
   asked. The model menu keeps a free-text entry on top of that, so a model
   released after the catalogue was fetched is still one keystroke away.
   ========================================================================== */

function closeModelMenu() {
  const open = $('.model-menu');
  if (open) open.remove();
}

/** Anchor below the chip, then nudge back inside the viewport. Called twice:
 *  once for the loading state and again once the real list has resized it. */
function positionModelMenu(menu, anchor) {
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${r.bottom + 6}px`;
  menu.style.left = `${Math.min(r.left, window.innerWidth - menu.offsetWidth - 12)}px`;
  if (menu.getBoundingClientRect().bottom > window.innerHeight - 8) {
    menu.style.top = `${Math.max(8, r.top - menu.offsetHeight - 6)}px`;
  }
}

async function openModelMenu(anchor, providerId) {
  closeModelMenu();
  const provider = ((state.config || {}).providers || {})[providerId] || {};
  const current = provider.model || '';

  const menu = document.createElement('div');
  menu.className = 'model-menu';
  menu.innerHTML =
    `<div class="model-menu-head">${esc(provider.label || providerId)} model</div>` +
    `<div class="model-loading">Asking ${esc(provider.label || providerId)}…</div>`;
  document.body.appendChild(menu);
  positionModelMenu(menu, anchor);

  // Ask the CLI what it can actually run. A list shipped in this app would be
  // wrong for accounts with different entitlements — a ChatGPT-account Codex
  // login rejects models an API key would accept, with a 400 at run time.
  let models = provider.models || [];
  let source = 'configured in Settings';
  let error = '';
  try {
    const data = await api(`/api/models?provider=${encodeURIComponent(providerId)}`);
    if (data.models && data.models.length) {
      models = data.models;
      source = data.source || '';
    }
    error = data.error || '';
  } catch (err) {
    error = err.message;
  }

  menu.innerHTML =
    `<div class="model-menu-head">${esc(provider.label || providerId)} model</div>` +
    `<button class="model-opt${current ? '' : ' active'}" data-value="">` +
      `<span class="model-opt-name">CLI default</span>` +
      `<span class="model-opt-note">whatever the CLI is set to</span>` +
    `</button>` +
    models.map(m =>
      `<button class="model-opt${m === current ? ' active' : ''}" data-value="${esc(m)}">` +
        `<span class="model-opt-name">${esc(m)}</span>` +
        // Bare aliases track the newest model in a family; pinned IDs don't.
        `<span class="model-opt-note">${/^[a-z]+$/.test(m) ? 'alias · always latest' : ''}</span>` +
      `</button>`
    ).join('') +
    (error ? `<div class="model-menu-error">${esc(error)}</div>` : '') +
    `<div class="model-menu-custom">` +
      `<input class="model-custom-input" placeholder="Other model…" spellcheck="false" ` +
        `value="${esc(models.includes(current) ? '' : current)}">` +
      `<button class="btn btn-quiet btn-sm model-custom-apply" type="button">Set</button>` +
    `</div>` +
    (source ? `<div class="model-menu-source">${esc(source)}</div>` : '');

  positionModelMenu(menu, anchor);

  const apply = (value) => {
    closeModelMenu();
    setModel(providerId, value);
  };

  menu.addEventListener('click', (e) => {
    const opt = e.target.closest('.model-opt');
    if (opt) { apply(opt.dataset.value); return; }
    if (e.target.closest('.model-custom-apply')) {
      apply($('.model-custom-input', menu).value.trim());
    }
  });
  menu.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.classList.contains('model-custom-input')) {
      apply(e.target.value.trim());
    }
  });

  // Defer so the click that opened the menu doesn't immediately close it.
  setTimeout(() => {
    document.addEventListener('click', onDocClickCloseModel, { once: true });
  }, 0);
}

function onDocClickCloseModel(e) {
  if (e.target.closest('.model-menu') || e.target.closest('.model-chip')) {
    document.addEventListener('click', onDocClickCloseModel, { once: true });
    return;
  }
  closeModelMenu();
}

/** Reasoning-effort menu. Shares the model menu's chrome deliberately: it is
 *  the same gesture on the same card, and a second set of styles would drift.
 *  No free-text entry here — an effort the CLI does not know is either ignored
 *  with a warning (Claude) or fatal (Codex), and neither is worth offering. */
async function openEffortMenu(anchor, providerId) {
  closeModelMenu();
  const provider = ((state.config || {}).providers || {})[providerId] || {};
  const current = provider.effort || '';
  const who = provider.label || providerId;

  const menu = document.createElement('div');
  menu.className = 'model-menu';
  menu.innerHTML =
    `<div class="model-menu-head">${esc(who)} reasoning effort</div>` +
    `<div class="model-loading">Asking ${esc(who)}…</div>`;
  document.body.appendChild(menu);
  positionModelMenu(menu, anchor);

  let levels = [], fallback = '', source = '', error = '', model = '';
  try {
    const data = await api(`/api/efforts?provider=${encodeURIComponent(providerId)}`);
    levels = data.levels || [];
    fallback = data.default || '';
    source = data.source || '';
    error = data.error || '';
    model = data.model || '';
  } catch (err) {
    error = err.message;
  }

  menu.innerHTML =
    `<div class="model-menu-head">${esc(who)} reasoning effort` +
      // Codex's levels differ per model, so name the one they belong to.
      (model ? ` · ${esc(model)}` : '') +
    `</div>` +
    `<button class="model-opt${current ? '' : ' active'}" data-value="">` +
      `<span class="model-opt-name">CLI default</span>` +
      `<span class="model-opt-note">` +
        `${fallback ? esc(fallback) : 'whatever the CLI is set to'}</span>` +
    `</button>` +
    levels.map(l =>
      `<button class="model-opt${l.effort === current ? ' active' : ''}" ` +
        `data-value="${esc(l.effort)}" title="${esc(l.description || '')}">` +
        `<span class="model-opt-name">${esc(l.effort)}</span>` +
        `<span class="model-opt-note">${esc(l.description || '')}</span>` +
      `</button>`
    ).join('') +
    // A level set before the model changed, or typed into Settings by hand.
    // Codex fails the run on one it does not recognise, so say so here rather
    // than letting it surface as a launch error minutes into a task.
    (current && levels.length && !levels.some(l => l.effort === current)
      ? `<div class="model-menu-error">` +
        `${esc(current)} is set but not offered here. It will be rejected at ` +
        `launch — pick one above.</div>`
      : '') +
    (error ? `<div class="model-menu-error">${esc(error)}</div>` : '') +
    (source ? `<div class="model-menu-source">${esc(source)}</div>` : '');

  positionModelMenu(menu, anchor);

  menu.addEventListener('click', (e) => {
    const opt = e.target.closest('.model-opt');
    if (!opt) return;
    closeModelMenu();
    setEffort(providerId, opt.dataset.value);
  });

  setTimeout(() => {
    document.addEventListener('click', onDocClickCloseModel, { once: true });
  }, 0);
}

async function setEffort(providerId, value) {
  const provider = ((state.config || {}).providers || {})[providerId] || {};
  await patchConfig({ providers: { [providerId]: { effort: value } } });
  toast(
    value ? `${provider.label || providerId} → ${value} effort`
          : `${provider.label || providerId} → CLI default effort`,
    'ok', 2600
  );
}

async function setModel(providerId, value) {
  const providers = (state.config || {}).providers || {};
  const provider = providers[providerId] || {};
  // Remember a hand-typed model so it appears in the list next time.
  const models = provider.models || [];
  const patch = { providers: { [providerId]: { model: value } } };
  if (value && !models.includes(value)) {
    patch.providers[providerId].models = [...models, value];
  }
  await patchConfig(patch);
  toast(
    value ? `${provider.label || providerId} → ${value}`
          : `${provider.label || providerId} → CLI default`,
    'ok', 2600
  );
  await dropUnsupportedEffort(providerId, provider);
}

/** Clear a reasoning level the newly chosen model does not offer.
 *
 *  Codex varies its levels per model — only some accept `ultra` — and it fails
 *  the run outright on one it does not know. Leaving a stale level set would
 *  turn a model change into a run that dies at launch, minutes later, for a
 *  reason nothing on screen explains. */
async function dropUnsupportedEffort(providerId, provider) {
  const effort = provider.effort || '';
  if (!effort) return;
  try {
    const data = await api(`/api/efforts?provider=${encodeURIComponent(providerId)}`);
    const levels = (data.levels || []).map(l => l.effort);
    // An empty list means discovery failed, not that nothing is supported —
    // clearing on that would silently undo a deliberate choice.
    if (!levels.length || levels.includes(effort)) return;
    await patchConfig({ providers: { [providerId]: { effort: '' } } });
    toast(
      `${provider.label || providerId} does not offer ${effort} effort on ` +
      `that model — reset to the CLI default.`, 'warn', 5000
    );
  } catch (err) {
    /* Discovery is best-effort; a failed check must not block a model change. */
  }
}

/* ==========================================================================
   9. Modals
   ========================================================================== */

function openModal(id) { $(`#${id}`).classList.remove('hidden'); }
function closeModal(id) { $(`#${id}`).classList.add('hidden'); }

/* ---- Directory picker ---- */

let pickerPath = '';

async function loadPicker(path) {
  const listing = await api(`/api/fs?path=${encodeURIComponent(path)}`)
    .catch(err => { toast(err.message, 'error'); return null; });
  if (!listing) return;

  pickerPath = listing.path;
  $('#picker-path').value = listing.path;
  $('#picker-up').disabled = !listing.parent;

  const status = listing.is_repo
    ? 'This folder is a git repository.'
    : 'This folder is not a git repository.';
  $('#picker-status').textContent = listing.error || status;
  $('#picker-select').disabled = !listing.is_repo;

  const folderIcon =
    `<span class="folder"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" ` +
    `stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">` +
    `<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>` +
    `</svg></span>`;

  const list = $('#picker-list');
  if (!listing.entries.length) {
    list.innerHTML = '<div class="picker-empty">No subfolders here.</div>';
    return;
  }
  list.innerHTML = listing.entries.map(e =>
    `<button class="picker-row ${e.is_repo ? 'is-repo' : ''}" type="button" ` +
      `data-path="${esc(e.path)}">${folderIcon}` +
      `<span class="name">${esc(e.name)}</span>` +
      (e.is_repo ? '<span class="repo-badge">git</span>' : '') +
    `</button>`
  ).join('');
}

async function selectRepo(path) {
  try {
    const { status } = await api(`/api/repo?path=${encodeURIComponent(path)}`);
    if (!status.is_repo) {
      toast(`${path} is not a git repository.`, 'error');
      return;
    }
    const { config } = await api('/api/config', {
      method: 'POST',
      body: { target_repo: status.path },
    });
    state.config = config;
    state.repoStatus = status;
    renderAll();
    closeModal('picker');
    toast(`Target set to ${status.path}`, 'ok', 3200);
  } catch (err) {
    toast(err.message, 'error');
  }
}

/* ---- Settings ---- */

/** Own selectors, not the output pane's `.tab`: that click handler is bound
 *  document-wide at boot and would fire on these too. */
function switchSettingsTab(name) {
  $$('.settings-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.settingsTab === name));
  $$('.settings-panel').forEach(p =>
    p.classList.toggle('active', p.dataset.settingsPanel === name));
}

function renderSettings() {
  const conf = state.config || {};
  const providers = conf.providers || {};
  const host = $('#provider-forms');

  host.innerHTML = ['drafter', 'polisher'].map((id, idx) => {
    const p = providers[id] || {};
    const info = state.providers.find(x => x.id === id);
    const probeHtml = info
      ? `<span class="probe ${info.available ? 'ok' : 'miss'}">` +
        `${info.available ? 'found' : 'not found'}</span>`
      : '';
    return (
      `<div class="provider-form" data-provider="${id}">` +
        `<h4><span class="stage-num">Stage ${idx + 1}</span> ` +
          `${esc(p.role || id)} ${probeHtml}</h4>` +
        `<div class="field-row">` +
          `<div class="field">` +
            `<label>Agent ` +
              `<span class="field-hint">— swaps command and flags</span></label>` +
            `<select data-field="agent">` +
              state.agents.map(a =>
                `<option value="${esc(a.id)}"` +
                `${a.id === agentOf(p) ? ' selected' : ''}>${esc(a.label)}</option>`
              ).join('') +
            `</select>` +
          `</div>` +
          `<div class="field">` +
            `<label>Display name</label>` +
            `<input type="text" data-field="label" value="${esc(p.label || '')}">` +
          `</div>` +
        `</div>` +
        `<div class="field">` +
          `<label>Role — what this stage is told to do` +
            `<span class="field-hint"> — the shipped text is a starting point</span>` +
          `</label>` +
          `<select data-field="role_template">` +
            (state.roles || []).map(r =>
              `<option value="${esc(r.id)}"${r.id === (p.role_template || '') ? ' selected' : ''}>` +
                `${esc(r.name)} — ${esc(r.summary)}</option>`
            ).join('') +
          `</select>` +
        `</div>` +
        `<div class="field">` +
          `<label>Behaviour ` +
            `<button class="link-btn" type="button" data-reset-role="${id}">` +
              `reset to the template</button>` +
          `</label>` +
          `<textarea rows="5" class="role-system" data-field="role_system" ` +
            `placeholder="Using the template above. Type here to override it.">` +
            `${esc(p.role_system || '')}</textarea>` +
          `<span class="field-hint" data-role-warn="${id}"></span>` +
        `</div>` +
        // Everything below is the CLI plumbing the Agent dropdown fills in for
        // you. Folded away because reading it is how you check a custom CLI,
        // not how you configure a stage.
        `<details class="advanced">` +
          `<summary>Command line</summary>` +
          `<div class="advanced-body">` +
            `<div class="field">` +
              `<label>Command (one argument per line, <code>{prompt}</code> is substituted)</label>` +
              `<textarea rows="3" data-field="command">${esc((p.command || []).join('\n'))}</textarea>` +
            `</div>` +
            `<div class="field">` +
              `<label>Auto-approve arguments (added only when execution is approved)</label>` +
              `<textarea rows="2" data-field="auto_approve_args">${esc((p.auto_approve_args || []).join('\n'))}</textarea>` +
            `</div>` +
            `<div class="field">` +
              `<label>Streaming arguments ` +
                `<span class="field-hint">— always added; make the CLI report ` +
                `progress as it works instead of only at the end</span></label>` +
              `<textarea rows="2" data-field="stream_args">${esc((p.stream_args || []).join('\n'))}</textarea>` +
            `</div>` +
            `<div class="field-row">` +
              `<div class="field">` +
                `<label>Model (blank = the CLI's own default)</label>` +
                `<input type="text" data-field="model" value="${esc(p.model || '')}">` +
              `</div>` +
              `<div class="field">` +
                `<label>Model flag (<code>{model}</code> substituted)</label>` +
                `<input type="text" data-field="model_args" ` +
                  `value="${esc((p.model_args || []).join(' '))}">` +
              `</div>` +
            `</div>` +
            `<div class="field">` +
              `<label>Selectable models, one per line ` +
                `<span class="field-hint">— shown in the picker on the agent card</span></label>` +
              `<textarea rows="4" data-field="models">${esc((p.models || []).join('\n'))}</textarea>` +
            `</div>` +
            `<div class="field-row">` +
              `<div class="field">` +
                `<label>Reasoning effort (blank = the CLI's own default)</label>` +
                `<input type="text" data-field="effort" value="${esc(p.effort || '')}">` +
              `</div>` +
              `<div class="field">` +
                `<label>Effort flag (<code>{effort}</code> substituted)</label>` +
                `<input type="text" data-field="effort_args" ` +
                  `value="${esc((p.effort_args || []).join(' '))}">` +
              `</div>` +
            `</div>` +
            `<div class="field-row">` +
              `<div class="field">` +
                `<label>Timeout (seconds)</label>` +
                `<input type="number" min="30" max="21600" data-field="timeout_seconds" ` +
                  `value="${Number(p.timeout_seconds) || 900}">` +
              `</div>` +
              `<div class="field field-check">` +
                `<label class="checkline">` +
                  `<input type="checkbox" data-field="prompt_on_stdin" ` +
                    `${p.prompt_on_stdin ? 'checked' : ''}> Pipe the prompt on stdin` +
                `</label>` +
              `</div>` +
            `</div>` +
          `</div>` +
        `</details>` +
      `</div>`
    );
  }).join('');

  $$('.provider-form').forEach(updateRoleWarning);
  $('#house-rules').value = conf.house_rules || '';
  $('#port-input').value = conf.port || 8760;
  $('#open-browser').checked = conf.open_browser !== false;
}

/** Flag a behaviour whose write expectation disagrees with the stage's actual
 *  permission. Not resolved automatically: guessing which of the two the
 *  operator meant is how a safety setting stops being trustworthy. */
function updateRoleWarning(form) {
  if (!form) return;
  const id = form.dataset.provider;
  const chosen = $('[data-field="role_template"]', form).value;
  const role = (state.roles || []).find(r => r.id === chosen);
  const note = $(`[data-role-warn="${id}"]`, form.parentElement) || $(`[data-role-warn="${id}"]`);
  if (!note || !role) return;

  // Today permission is per stage: the drafter is read-only, the polisher
  // writes once approved.
  const stageWrites = id === 'polisher';
  if (role.writes && !stageWrites) {
    note.textContent = `"${role.name}" expects to modify files, but this stage `
      + `is read-only — it will produce a proposal, not changes.`;
    note.className = 'field-hint warn';
  } else if (!role.writes && stageWrites) {
    note.textContent = `"${role.name}" is a report-only behaviour, but this `
      + `stage may write once approved. It will be told not to.`;
    note.className = 'field-hint warn';
  } else {
    note.textContent = '';
    note.className = 'field-hint';
  }
}

async function saveSettings() {
  const providers = {};
  let valid = true;

  $$('.provider-form').forEach(form => {
    const id = form.dataset.provider;
    const field = (name) => $(`[data-field="${name}"]`, form);
    const lines = (name) => field(name).value.split('\n')
      .map(s => s.trim()).filter(Boolean);

    const command = lines('command');
    const cmdInput = field('command');
    if (!command.length) {
      cmdInput.classList.add('invalid');
      // The command lives behind a disclosure, and a field the operator cannot
      // see is not a validation message - open it so the toast has a referent.
      const advanced = cmdInput.closest('.advanced');
      if (advanced) advanced.open = true;
      valid = false;
    } else {
      cmdInput.classList.remove('invalid');
    }

    providers[id] = {
      id,
      // The server expands this into the agent's command and permission
      // flags when it differs from what the stage runs today, and ignores it
      // otherwise - so a hand-edited command below still wins.
      agent: field('agent').value,
      label: field('label').value.trim() || id,
      command,
      auto_approve_args: lines('auto_approve_args'),
      stream_args: lines('stream_args'),
      role_template: field('role_template').value,
      role_system: field('role_system').value.trim(),
      model: field('model').value.trim(),
      // Space-separated on this form, since a model flag is always short.
      model_args: field('model_args').value.trim().split(/\s+/).filter(Boolean),
      models: lines('models'),
      effort: field('effort').value.trim(),
      effort_args: field('effort_args').value.trim().split(/\s+/).filter(Boolean),
      timeout_seconds: Math.max(30, parseInt(field('timeout_seconds').value, 10) || 900),
      prompt_on_stdin: field('prompt_on_stdin').checked,
    };
  });

  if (!valid) {
    toast('Every stage needs a command.', 'error');
    return;
  }

  try {
    const { config } = await api('/api/config', {
      method: 'POST',
      body: {
        providers,
        house_rules: $('#house-rules').value,
        port: parseInt($('#port-input').value, 10) || 8760,
        open_browser: $('#open-browser').checked,
      },
    });
    state.config = config;
    await refreshDoctor();
    renderAll();
    closeModal('settings');
    toast('Settings saved.', 'ok', 3000);
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function refreshDoctor(show = false) {
  try {
    const data = await api('/api/doctor');
    state.providers = data.providers || [];
    if (show) {
      const lines = data.providers.map(p =>
        `${p.available ? '[ OK ]' : '[MISS]'} ${p.label.padEnd(8)} ${p.executable.padEnd(10)} ` +
        `${p.path || 'not on PATH'}${p.version ? '  ' + p.version : ''}`
      );
      lines.push('', `config: ${data.config_path}`, `runs:   ${data.runs_path}`);
      $('#doctor-out').textContent = lines.join('\n');
    }
    renderAgents();
    if (show) renderSettings();
  } catch (err) {
    toast(err.message, 'error');
  }
}

/* ---- Conversations ---- */

/** The sidebar list: one row per conversation, newest first. The server groups
 *  runs into threads, so a follow-up does not appear as its own entry — it *is*
 *  the conversation it continued. */
async function loadChats() {
  const list = $('#chat-list');
  if (!list.childElementCount) {
    list.innerHTML = '<div class="picker-empty">Loading…</div>';
  }
  try {
    const { runs } = await api('/api/history');
    state.chats = runs;
    renderChats();
  } catch (err) {
    list.innerHTML = `<div class="picker-empty">${esc(err.message)}</div>`;
  }
}

function renderChats() {
  const list = $('#chat-list');
  const chats = state.chats || [];
  const count = $('#chat-count');
  count.textContent = chats.length ? String(chats.length) : '';
  count.classList.toggle('hidden', !chats.length);

  if (!chats.length) {
    list.innerHTML =
      '<div class="picker-empty">No conversations yet. Run a task to start one.</div>';
    return;
  }
  const open = state.openChat ? state.openChat.file : '';
  list.innerHTML = chats.map(c =>
    `<button class="chat-row ${esc(c.state)}${c.file === open ? ' open' : ''}" ` +
      `type="button" data-chat-file="${esc(c.file)}" title="${esc(c.repo || '')}">` +
      `<span class="chat-row-title">${esc(c.title || c.task || '(no task)')}</span>` +
      `<span class="chat-row-meta">${fmtWhen(c.created_at)} · ` +
        `${c.messages} message${c.messages === 1 ? '' : 's'}` +
        `${c.zero_touch ? ' · zero-touch' : ''}</span>` +
    `</button>`
  ).join('');
}

function historySection(title, bodyHtml, cls = 'markdown') {
  if (!bodyHtml) return '';
  return (
    `<section class="history-block"><h3>${esc(title)}</h3>` +
    `<div class="${cls}">${bodyHtml}</div></section>`
  );
}

/** One exchange: what the human asked, then what each agent answered.
 *  `compacted` marks a turn whose answers were summarised to fit the context
 *  budget, so a clipped outline is not shown as if it were the whole reply. */
function historyTurn(task, replies, prefix = '', compacted = false) {
  const note = compacted ? ' · compacted' : '';
  return (
    historySection(`${prefix}You asked`, renderMarkdown(task || '(no message)')) +
    replies.map(r => historySection(
      `${prefix}${r.label || r.stage || 'Agent'}${note}`,
      renderMarkdown(r.output || '') ||
        `<p class="history-none">${esc(r.error || `(${r.state || 'no output'})`)}</p>`
    )).join('')
  );
}

/** Open one conversation in the main pane: every turn of the thread, then the
 *  result of its latest run. The live run's own output is hidden rather than
 *  cleared, so closing this puts it back untouched. */
async function openChat(file) {
  const transcript = $('#chat-transcript');
  $('.output').classList.add('hidden');
  $('#chat-view').classList.remove('hidden');
  $('#chat-continue').disabled = true;
  $('#chat-context').textContent = '';
  transcript.innerHTML = '<div class="picker-empty">Loading…</div>';

  try {
    const { run } = await api(`/api/run?file=${encodeURIComponent(file)}`);
    state.openChat = { file, run };
    renderChats();
    // Earlier turns of the same thread travel inside the transcript, so a
    // follow-up reads as the conversation it was rather than a lone message.
    const earlier = run.conversation || [];
    $('#chat-title').textContent =
      (earlier.length ? earlier[0].task : run.task) || 'Conversation';
    $('#chat-continue').disabled = false;

    const stages = (run.stage_order || Object.keys(run.stages || {}))
      .map(id => (run.stages || {})[id])
      .filter(Boolean);

    transcript.innerHTML =
      earlier.map(t =>
        historyTurn(t.task, t.replies || [], 'Earlier · ', t.compacted)).join('') +
      historyTurn(run.task, stages) +
      historySection('Your note at the approval gate',
        renderMarkdown(run.reviewer_note || '')) +
      historySection('Repository changes',
        run.diff ? renderDiff(run.diff, run.diff_stat) : '', 'diff-view') +
      historySection('Rolled back', renderMarkdown(run.rollback_note || '')) +
      historySection('Error', renderMarkdown(run.error || ''));

    // What a follow-up to *this* conversation would replay — measured now
    // rather than after the operator has committed to it.
    const { context } = await api(`/api/context?file=${encodeURIComponent(file)}`);
    if (state.openChat && state.openChat.file === file) {
      const cost = contextLine(context);
      const meter = $('#chat-context');
      meter.textContent = `${fmtWhen(run.created_at)} · ${run.state}` +
        (cost ? ` · continuing replays ${cost}` : '');
      meter.classList.toggle('warn', contextIsTight(context));
    }
  } catch (err) {
    transcript.innerHTML = `<div class="picker-empty">${esc(err.message)}</div>`;
  }
}

function closeChat() {
  state.openChat = null;
  $('#chat-view').classList.add('hidden');
  $('.output').classList.remove('hidden');
  renderChats();
}

function switchSidebarTab(name) {
  $$('.sidebar-tab').forEach(t => {
    const active = t.dataset.sidebarTab === name;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', String(active));
  });
  $$('.sidebar-panel').forEach(p =>
    p.classList.toggle('active', p.dataset.sidebarPanel === name));
  if (name === 'chats') loadChats();
}

/* ==========================================================================
   9. Event stream
   ========================================================================== */

let source = null;
let lastEventId = 0;
let reconnectDelay = 1000;

function setConn(cls, text) {
  const node = $('#conn-state');
  node.className = `conn ${cls}`;
  $('.conn-text', node).textContent = text;
}

function connect() {
  if (source) source.close();
  source = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}&last_id=${lastEventId}`);

  source.onopen = () => {
    setConn('live', 'live');
    reconnectDelay = 1000;
  };

  source.onerror = () => {
    setConn('dead', 'reconnecting');
    source.close();
    // EventSource auto-reconnects, but not after the server closes cleanly.
    // Back off so a stopped server does not spin the tab at full speed.
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 15000);
  };

  source.onmessage = () => { /* unnamed events are heartbeats */ };

  const on = (name, fn) => source.addEventListener(name, ev => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (data.id) lastEventId = data.id;
    fn(data);
  });

  on('run_started', (d) => {
    state.run = d.run;
    state.busy = true;
    // A saved conversation is hiding the output pane the stream renders into,
    // and a run starting is where the operator's attention belongs.
    closeChat();
    clearStream();
    pushDivider('Run started');
    pushLine('sys', 'system', `Task: ${d.run.task}`);
    pushLine('sys', 'system', `Repo: ${d.run.repo}`);
    if (d.run.zero_touch) {
      pushLine('warn', 'system', 'Zero-Touch Mode: approvals will be skipped.');
    }
    switchTab('stream');
    renderAll();
  });

  on('state', (d) => {
    state.run = d.run;
    state.busy = !['complete', 'failed', 'cancelled'].includes(d.state);
    if (d.state === 'awaiting_approval') {
      // Solo Mode produces no draft, so leave the operator on the live stream.
      if (!(d.run && d.run.solo)) switchTab('draft');
      toast('Approval needed. Nothing has been written yet.', 'warn', 8000);
    }
    if (d.state === 'complete') {
      switchTab(d.run && d.run.diff ? 'diff' : 'final');
      toast('Run complete.', 'ok');
    }
    if (d.state === 'failed') toast(d.run.error || 'Run failed.', 'error', 9000);
    if (d.state === 'cancelled') toast('Run cancelled.', 'warn');
    // The transcript is written when the run reaches a terminal state, which is
    // when the conversation list can show it — and when a follow-up has folded
    // its parent into itself and must replace it there.
    if (!state.busy) loadChats();
    renderAll();
  });

  on('stage_started', (d) => {
    state.run = d.run;
    const stage = d.run.stages[d.stage];
    pushDivider(`${stage.label} · ${stage.role}`);
    if (stage.command && stage.command.length) {
      pushLine('sys', 'exec', stage.command.join(' '));
    }
    renderAll();
  });

  on('stage_output', (d) => {
    // Tag with the stage's own label: either agent can hold either job, so a
    // hardcoded "codex"/"claude" would mislabel half the stream.
    const stage = state.run && state.run.stages ? state.run.stages[d.stage] : null;
    pushLine(`${d.stage} ${d.stream === 'stderr' ? 'stderr' : ''}`,
             (stage && stage.label) || d.stage, d.line);
  });

  on('stage_finished', (d) => {
    state.run = d.run;
    const stage = d.run.stages[d.stage];
    pushLine(d.ok ? 'sys' : 'err', 'exit',
      `${stage.label} finished in ${fmtDuration(stage.duration)}` +
      (d.ok ? '' : ` — ${stage.error || 'failed'}`));
    renderAll();
  });

  on('log', (d) => {
    pushLine(d.level === 'warn' ? 'warn' : d.level === 'error' ? 'err' : 'sys',
             'system', d.message);
  });

  on('committed', (d) => {
    pushLine('sys', 'git', `committed ${d.commit.short} — ${d.commit.message}`);
    loadState();
  });

  on('rolled_back', (d) => {
    state.run = d.run;
    pushLine('warn', 'system', d.message);
    toast(d.message, 'ok', 7000);
    renderAll();
  });

  on('usage', (d) => {
    state.usage = d.usage || {};
    renderAgents();
  });

  on('config', (d) => {
    state.config = d.config;
    // Roles live in config now, so a change from another tab must refresh the
    // catalogue too - otherwise the dropdown and the list disagree.
    api('/api/roles').then(r => { state.roles = r.roles; renderRoleList(); })
      .catch(() => {});
    renderAll();
  });
}

/* ==========================================================================
   10. Wiring + boot
   ========================================================================== */

function switchTab(name) {
  state.activeTab = name;
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $$('.tab-panel').forEach(p => p.classList.toggle('active', p.dataset.panel === name));
}

async function patchConfig(patch) {
  try {
    const { config } = await api('/api/config', { method: 'POST', body: patch });
    state.config = config;
    renderAll();
  } catch (err) {
    toast(err.message, 'error');
    await loadState();
  }
}

async function loadState() {
  try {
    const data = await api('/api/state');
    state.config = data.config;
    state.run = data.run;
    state.busy = data.busy;
    state.agents = data.agents || [];
    state.providers = data.providers_status || [];
    state.roles = data.roles || [];
    state.repoStatus = data.repo_status;
    state.usage = data.usage || {};
    renderAll();
  } catch (err) {
    toast(err.message, 'error', 12000);
  }
}

async function startRun() {
  const task = $('#task-input').value.trim();
  if (!task) return;
  const conf = state.config || {};

  // Quota warning. Advisory only: the reading is a snapshot, and only the
  // operator knows whether this particular task is worth the remaining budget.
  const stages = conf.solo_mode ? [conf.solo_stage || 'polisher'] : ['drafter', 'polisher'];
  const worst = worstUsageFor(stages);
  const threshold = Number(conf.usage_warn_percent ?? 85);
  if (worst && worst.percent >= threshold) {
    const ok = confirm(
      `Quota warning\n\n` +
      `${worst.label} is at ${Math.round(worst.percent)}% used` +
      (worst.resets ? `, resets ${worst.resets}` : '') + `.\n\n` +
      `This run may exhaust it. Continue anyway?`
    );
    if (!ok) return;
  }

  if (conf.zero_touch) {
    const ok = confirm(
      'Zero-Touch Mode is ON.\n\n' +
      'The pipeline will run to completion without pausing, and the senior ' +
      'stage will modify files in:\n\n' + (conf.target_repo || '?') +
      '\n\nContinue?'
    );
    if (!ok) return;
  }
  try {
    await api('/api/start', {
      method: 'POST',
      body: {
        task,
        repo: conf.target_repo,
        continue_from: state.continueFrom,
        compact_context: state.compactContext,
      },
    });
    // Only once the server has accepted it: a rejected start leaves the
    // attachment in place so the operator can fix the task and try again.
    clearContinuation();
  } catch (err) {
    toast(err.message, 'error', 9000);
  }
}

function wire() {
  // -- composer ---------------------------------------------------------
  $('#task-input').addEventListener('input', renderStatus);
  $('#task-input').addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); startRun(); }
  });
  $('#run-btn').addEventListener('click', startRun);

  // -- run controls -----------------------------------------------------
  $('#cancel-btn').addEventListener('click', () =>
    api('/api/cancel', { method: 'POST' }).catch(e => toast(e.message, 'error')));

  $('#rollback-btn').addEventListener('click', async () => {
    if (!confirm(
      'Roll back?\n\nThis resets the working tree to the snapshot taken before ' +
      'the senior stage ran. Any change made during this run is discarded.'
    )) return;
    try {
      await api('/api/rollback', { method: 'POST' });
    } catch (err) { toast(err.message, 'error', 9000); }
  });

  $('#pr-btn').addEventListener('click', () => {
    const pr = state.run && state.run.pull_request;
    if (pr && pr.url) window.open(pr.url, '_blank', 'noopener');
  });

  $('#approve-btn').addEventListener('click', async () => {
    try {
      await api('/api/approve', { method: 'POST', body: { note: $('#approval-note').value } });
      $('#approval-note').value = '';
      switchTab('stream');
    } catch (err) { toast(err.message, 'error'); }
  });

  $('#reject-btn').addEventListener('click', async () => {
    try {
      await api('/api/reject', { method: 'POST', body: { note: $('#approval-note').value } });
      $('#approval-note').value = '';
    } catch (err) { toast(err.message, 'error'); }
  });

  // -- toggles ----------------------------------------------------------
  $('#zero-touch').addEventListener('change', (e) => {
    if (e.target.checked) {
      const ok = confirm(
        'Enable Zero-Touch Mode?\n\n' +
        'Runs will proceed with no approval step, and auto-approve flags ' +
        '(--dangerously-skip-permissions) will be passed to the CLI. Files ' +
        'will be created, modified and deleted without asking you first.\n\n' +
        'Keep "Safety snapshot" on so you can roll back.'
      );
      if (!ok) { e.target.checked = false; return; }
    }
    patchConfig({ zero_touch: e.target.checked });
  });
  $('#safety-snapshot').addEventListener('change', e =>
    patchConfig({ safety_snapshot: e.target.checked }));
  $('#solo-mode').addEventListener('change', e =>
    patchConfig({ solo_mode: e.target.checked }));
  $('#solo-stage').addEventListener('change', e =>
    patchConfig({ solo_stage: e.target.value }));
  $('#clean-worktree').addEventListener('change', e =>
    patchConfig({ require_clean_worktree: e.target.checked }));
  $('#pull-request-mode').addEventListener('change', (e) => {
    if (e.target.checked) {
      const ok = confirm(
        'Deliver runs as pull requests?\n\n' +
        'Each run will start from a clean tree, work on a branch of its own, ' +
        'then commit, push to origin and open a PR with the GitHub CLI. The ' +
        'branch you started on is never written to, and nothing is merged ' +
        'for you.'
      );
      if (!ok) { e.target.checked = false; return; }
    }
    patchConfig({ pull_request_mode: e.target.checked });
  });

  // -- tabs -------------------------------------------------------------
  $$('.tab').forEach(tab => tab.addEventListener('click', () => switchTab(tab.dataset.tab)));

  // -- model picker -----------------------------------------------------
  // Delegated: the agent rail is re-rendered on every state change.
  $('#agent-rail').addEventListener('click', (e) => {
    if (e.target.closest('[data-disable-solo]')) {
      patchConfig({ solo_mode: false });
      toast('Solo mode off — the draft stage is back in the pipeline.', 'ok', 3600);
      return;
    }
    const usageChip = e.target.closest('[data-usage-for]');
    if (usageChip) {
      usageChip.classList.add('checking');
      api('/api/usage/refresh', { method: 'POST' })
        .then(d => { state.usage = d.usage || {}; renderAgents(); })
        .catch(err => toast(err.message, 'error'));
      return;
    }
    const chip = e.target.closest('.model-chip');
    if (!chip) return;
    e.stopPropagation();
    if ($('.model-menu')) { closeModelMenu(); return; }  // toggle
    if (chip.dataset.effortFor) openEffortMenu(chip, chip.dataset.effortFor);
    else openModelMenu(chip, chip.dataset.modelFor);
  });
  window.addEventListener('resize', closeModelMenu);

  // -- repo picker ------------------------------------------------------
  $('#repo-btn').addEventListener('click', () => {
    openModal('picker');
    loadPicker((state.config && state.config.target_repo) || '~');
  });
  $('#picker-up').addEventListener('click', () => {
    const parent = pickerPath.replace(/\/[^/]+\/?$/, '') || '/';
    loadPicker(parent);
  });
  $('#picker-home').addEventListener('click', () => loadPicker('~'));
  $('#picker-path').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') loadPicker($('#picker-path').value.trim());
  });
  $('#picker-list').addEventListener('click', (e) => {
    const row = e.target.closest('.picker-row');
    if (row) loadPicker(row.dataset.path);
  });
  $('#picker-select').addEventListener('click', () => selectRepo(pickerPath));

  $('#recent-repos').addEventListener('click', (e) => {
    const btn = e.target.closest('.recent-item');
    if (btn) selectRepo(btn.dataset.repo);
  });

  // -- settings ---------------------------------------------------------
  $('#settings-btn').addEventListener('click', async () => {
    await refreshDoctor(true);
    renderSettings();
    renderRoleList();
    switchSettingsTab('stages');
    openModal('settings');
  });
  $('.settings-tabs').addEventListener('click', (e) => {
    const tab = e.target.closest('.settings-tab');
    if (tab) switchSettingsTab(tab.dataset.settingsTab);
  });
  $('#provider-forms').addEventListener('click', (e) => {
    const reset = e.target.closest('[data-reset-role]');
    if (!reset) return;
    const form = reset.closest('.provider-form');
    $('[data-field="role_system"]', form).value = '';
    updateRoleWarning(form);
    toast('Back to the template text.', 'ok', 2400);
  });

  $('#provider-forms').addEventListener('change', (e) => {
    if (e.target.matches('[data-field="role_template"]')) {
      updateRoleWarning(e.target.closest('.provider-form'));
    }
  });

  $('#commit-btn').addEventListener('click', doCommit);
  $('#commit-message').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doCommit();
  });

  $('#role-new').addEventListener('click', () => openRoleEditor(null));
  $('#role-list').addEventListener('click', (e) => {
    const row = e.target.closest('.role-row');
    if (row && e.target.closest('.role-edit')) openRoleEditor(row.dataset.role);
  });

  $('#save-settings').addEventListener('click', saveSettings);

  // Picking an agent fills in its command and flags straight away, so the form
  // shows what will actually be saved. The server performs the same swap on
  // save; this is the preview, not the source of truth. Delegated because the
  // provider forms are rebuilt every time Settings opens.
  $('#provider-forms').addEventListener('change', (e) => {
    if (e.target.dataset.field !== 'agent') return;
    const preset = state.agents.find(a => a.id === e.target.value);
    if (!preset || !(preset.command || []).length) return;  // "Custom": leave it
    const form = e.target.closest('.provider-form');
    $('[data-field="label"]', form).value = preset.label;
    $('[data-field="command"]', form).value = preset.command.join('\n');
    $('[data-field="auto_approve_args"]', form).value =
      (preset.auto_approve_args || []).join('\n');
    $('[data-field="stream_args"]', form).value =
      (preset.stream_args || []).join('\n');
    $('[data-field="model_args"]', form).value = (preset.model_args || []).join(' ');
    $('[data-field="effort_args"]', form).value = (preset.effort_args || []).join(' ');
    // Neither model names nor effort levels are interchangeable between CLIs.
    $('[data-field="model"]', form).value = '';
    $('[data-field="models"]', form).value = '';
    $('[data-field="effort"]', form).value = '';
  });

  // The probe results print into the Stages panel, so show it.
  $('#run-doctor').addEventListener('click', () => {
    switchSettingsTab('stages');
    refreshDoctor(true);
  });
  $('#reset-config').addEventListener('click', async () => {
    if (!confirm('Reset every setting to its default?')) return;
    try {
      const { config } = await api('/api/config/reset', { method: 'POST' });
      state.config = config;
      renderSettings();
      renderAll();
      toast('Settings reset.', 'ok');
    } catch (err) { toast(err.message, 'error'); }
  });

  // -- conversations ----------------------------------------------------
  $('.sidebar-tabs').addEventListener('click', (e) => {
    const tab = e.target.closest('.sidebar-tab');
    if (tab) switchSidebarTab(tab.dataset.sidebarTab);
  });
  // Delegated: the list is rebuilt whenever it is reloaded.
  $('#chat-list').addEventListener('click', (e) => {
    const row = e.target.closest('[data-chat-file]');
    if (row) openChat(row.dataset.chatFile);
  });
  $('#chat-close').addEventListener('click', closeChat);
  $('#chat-continue').addEventListener('click', () => {
    const open = state.openChat;
    if (!open) return;
    // The server enforces this too; checking here turns a rejected run into
    // an answerable message before anything is started.
    const repo = (state.config || {}).target_repo || '';
    if (open.run.repo !== repo) {
      toast(
        `That conversation was in ${open.run.repo}. Select it as the target ` +
        `repository to continue it.`, 'error', 9000
      );
      return;
    }
    continueRun(open.file, open.run.task);
    closeChat();
  });
  $('#new-chat').addEventListener('click', () => {
    clearContinuation();
    closeChat();
    switchSidebarTab('council');
    $('#task-input').focus();
  });

  // -- continuation -----------------------------------------------------
  $('#continue-btn').addEventListener('click', () => {
    if (state.run) continueRun(state.run.file, state.run.task);
  });
  $('#continue-clear').addEventListener('click', clearContinuation);
  // Compaction applies to the run about to start; the transcript on disk is
  // never rewritten, so the full text of every turn stays readable in its own
  // conversation.
  // A toggle, not a one-way switch: compaction here is a choice about the run
  // about to start, and one the operator can still take back - unlike Claude
  // Code's `/compact`, which rewrites a live session there and then.
  $('#compact-btn').addEventListener('click', () => {
    if (!state.continueContext) return;
    state.compactContext = !state.compactContext;
    renderContinuation();
    toast(
      state.compactContext
        ? 'Earlier turns will be summarised for the next run, keeping the ' +
          'window clear for the work itself.'
        : 'Earlier turns will be sent in full again.',
      'ok', 5200
    );
  });

  // -- modal dismissal --------------------------------------------------
  $$('[data-close]').forEach(btn =>
    btn.addEventListener('click', () => closeModal(btn.dataset.close)));
  $$('.modal').forEach(modal => modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.classList.add('hidden');
  }));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeModelMenu();
      $$('.modal').forEach(m => m.classList.add('hidden'));
    }
  });

  // -- delegated: copy buttons and diff collapse ------------------------
  document.addEventListener('click', (e) => {
    const copy = e.target.closest('.copy-btn');
    if (copy) {
      const code = document.getElementById(copy.dataset.copy);
      if (code) {
        navigator.clipboard.writeText(code.dataset.raw || code.textContent).then(() => {
          copy.textContent = 'Copied';
          copy.classList.add('copied');
          setTimeout(() => { copy.textContent = 'Copy'; copy.classList.remove('copied'); }, 1600);
        }).catch(() => toast('Clipboard access was blocked.', 'warn'));
      }
      return;
    }
    const head = e.target.closest('.diff-file-head');
    if (head) head.parentElement.classList.toggle('collapsed');
  });

  // Warn before closing the window mid-run.
  window.addEventListener('beforeunload', (e) => {
    if (state.busy) { e.preventDefault(); e.returnValue = ''; }
  });
}

async function boot() {
  if (!TOKEN) {
    document.body.innerHTML =
      '<div class="boot-error">' +
      '<h2>Missing session token</h2>' +
      '<p>Open the dashboard using the URL the launcher ' +
      'printed &mdash; it carries a one-time token for this session.</p></div>';
    return;
  }
  wire();
  await loadState();
  await refreshDoctor();
  await loadChats();
  connect();
  $('#task-input').focus();
}

boot();
