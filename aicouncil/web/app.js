/* ==========================================================================
   Theseus AI - dashboard client
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
  // Git status of the chosen working folder, or null when none is chosen.
  // `is_repo: false` is a normal state here, not an error: the folder decides
  // which git-backed features are on offer, not whether a run may start.
  workspaceStatus: null,
  // Where a run lands with no folder chosen. From the server, which is the
  // only side that knows the XDG path.
  scratchWorkspace: '',
  usage: {},
  roles: [],
  busy: false,
  streamLines: [],
  // The OS user, for the sidebar footer and the greeting. There are no
  // accounts in this app; this is only who it is running as.
  user: '',
  // Project is a placeholder tab, held here and never written to config: the
  // server knows 'council' and 'solo' only, and a mode it cannot run is not one
  // the app should still be sitting in after a restart.
  tab: '',
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
  // Which mode `state.chats` was fetched for, so a list that arrives after the
  // operator has switched away can be discarded rather than shown.
  chatMode: '',
  openChat: null,
  // Which concrete model each alias stands for right now — `opus` is only a
  // pointer, and the version it points at is the thing worth knowing. Filled
  // in from /api/models; empty until the CLI has been asked once.
  resolvedModels: {},
};

const STATE_LABELS = {
  idle: 'Idle',
  drafting: 'Drafting',
  awaiting_approval: 'Awaiting your approval',
  polishing: 'Applying changes',
  running: 'Thinking',
  complete: 'Complete',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

const WORKING_STATES = ['drafting', 'polishing', 'running'];

/** The mode the *next* message starts, from config. Never 'project': that tab
 *  runs nothing, so the mode a run would start in is whichever real one the
 *  operator was last in. */
function selectedMode() {
  return ((state.config || {}).mode === 'solo') ? 'solo' : 'council';
}

/** The tab on screen, which is `selectedMode` plus the Project placeholder. */
function uiMode() {
  if (state.tab === 'project') return 'project';
  return visibleMode();
}

/** The mode the main pane should be showing. A run in flight owns the screen —
 *  it carries the mode it was started with, and switching the selector under a
 *  running agent must not swap the output surface out from under it. */
function visibleMode() {
  const run = state.run;
  if (state.busy && run) return run.mode || (run.solo ? 'solo' : 'council');
  return selectedMode();
}

/** The folder the operator chose, or '' for the scratch workspace. */
function workspacePath() {
  return ((state.config || {}).workspace) || '';
}

/** Whether the chosen folder is a git repository. False with no folder chosen:
 *  the scratch workspace deliberately is not one, so diff, snapshot, rollback
 *  and pull-request delivery are all off there. */
function workspaceIsRepo() {
  return !!(workspacePath() && state.workspaceStatus && state.workspaceStatus.is_repo);
}

/** Where a run persisted its work. Older transcripts call it `repo`. */
function runWorkspace(run) {
  return (run && (run.workspace || run.repo)) || '';
}

function renderStatus() {
  const run = state.run;
  const s = run ? run.state : 'idle';

  // No status pill any more: what a stage is doing reads off the member that
  // is doing it, and what the *run* is doing lands in the status bar. Only
  // states that are not already obvious from the thread are worth the words.
  const meta = [];
  if (run) {
    if (WORKING_STATES.includes(s) || s === 'awaiting_approval') {
      meta.push(STATE_LABELS[s] || s);
    }
    if (run.zero_touch) meta.push('ZERO-TOUCH');
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
  renderContinuation();

  const runBtn = $('#run-btn');
  // A working folder is not a precondition. The only thing standing between a
  // fresh install and a first answer is having typed something.
  const hasTask = $('#task-input').value.trim().length > 0;
  const project = uiMode() === 'project';
  runBtn.disabled = state.busy || !hasTask || project;
  runBtn.classList.toggle('busy', state.busy);
  runBtn.title = project ? 'Project mode does not run anything yet.'
    : state.busy ? 'A run is already in progress.'
    : hasTask ? 'Send' : 'Describe what you want first.';

  // The gate itself is placed in the thread by `renderThread`, next to the
  // draft it is judging. What it says still depends only on the run.
  const gated = !!(run && !run.solo && run.state === 'awaiting_approval');
  if (gated) {
    $('#approval-copy').innerHTML =
      'The draft above is ready. <b>Nothing has been written to disk yet.</b> ' +
      'Approving lets the senior stage apply changes in ' +
      `<b>${esc(runWorkspace(run))}</b>.` +
      // Whether a rollback point exists changes what approving costs, and the
      // gate is the last moment that can change the answer.
      (run.snapshot_planned ? '' :
        ' <b>No safety snapshot will be taken</b>, so this run cannot be ' +
        'rolled back.');
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

/** Attach a transcript to the next run, and measure what that will replay.
 *  ``mode`` is the mode that conversation was held in; the server refuses to
 *  continue it in the other one, so switch first rather than fail later. */
async function continueRun(file, task, mode = '', quiet = false) {
  if (mode && mode !== selectedMode()) {
    await patchConfig({ mode });
  }
  state.continueFrom = file;
  state.continueTask = (task || '').slice(0, 90);
  state.continueContext = null;
  state.compactContext = false;
  renderStatus();
  $('#task-input').focus();
  // Silent when opening a conversation from the sidebar: the thread appearing
  // on screen already says what happened, and a toast on every click through
  // history is noise.
  if (!quiet) toast('That conversation is attached. Type your follow-up.', 'ok', 4200);

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

/** A short label for a stage's role. The catalogue is editable, so this is the
 *  operator's own wording uppercased — not a fixed PLAN/REVIEW pair, which
 *  would go on saying PLAN after the role behind it had been changed. */
function roleTag(role) {
  return String(role || '').trim().toUpperCase();
}

/** The council strip: one compact button per stage, in a single row above the
 *  thread. Clicking a member opens everything about it — CLI, model, effort
 *  and role — rather than spreading those across chips that widen the strip
 *  until it no longer fits on one line. */
function renderStrip() {
  const strip = $('#council-strip');
  const council = uiMode() === 'council';
  strip.classList.toggle('hidden', !council);
  if (!council) { strip.innerHTML = ''; return; }

  const run = state.run;
  const providers = (state.config || {}).providers || {};
  const probeFor = (id) => state.providers.find(p => p.id === id);

  strip.innerHTML = '<div class="strip-inner">' + ['drafter', 'polisher'].map(id => {
    const p = providers[id] || {};
    const stage = run && run.stages ? run.stages[id] : null;
    const info = probeFor(id);
    const available = !info || info.available;

    let st = stage ? stage.state : 'pending';
    if (run && run.state === 'awaiting_approval' && id === 'polisher') st = 'waiting';

    const initial = (p.label || id).slice(0, 2).toUpperCase();
    const model = p.model || 'default model';
    const title =
      `${p.label || id} — ${p.role || 'no role'} · ${modelDetail(model)}` +
      (p.effort ? ` · ${p.effort}` : '') +
      (available ? '' : ` · ${(p.command || [])[0] || 'CLI'} not found`) +
      '\nClick to change CLI, model, effort or role.';

    return (
      `<button class="member ${st}${available ? '' : ' unavailable'}" type="button" ` +
        `data-member="${id}" data-agent="${id}" title="${esc(title)}">` +
        `<span class="member-mark">${esc(initial)}</span>` +
        `<span class="member-body">` +
          `<span class="member-name">${esc(p.label || id)}</span>` +
          `<span class="member-model">${esc(model)}</span>` +
        `</span>` +
        `<span class="member-tail">` +
          `<span class="member-role">${esc(roleTag(p.role))}</span>` +
          memberQuotaHtml(id) +
          `<span class="member-dot"></span>` +
        `</span>` +
      `</button>`
    );
  }).join('') + '</div>';
}

/** Quota on the strip, shown only once it is worth interrupting for. A chip on
 *  every member at all times would double the strip's width to say "fine"; the
 *  full reading is a click away on the member itself. */
function memberQuotaHtml(id) {
  const u = (state.usage || {})[id];
  if (!u || !u.supported || !u.worst) return '';
  const threshold = Number((state.config || {}).usage_warn_percent ?? 85);
  return u.worst.percent >= threshold ? usageChipHtml(id) : '';
}

/** Chat's pickers. In Council these live on the strip instead: there are two
 *  agents there, and a chip in the composer could not say which it belonged
 *  to without repeating the agent's name in front of every one of them. */
function renderComposerChips() {
  const host = $('#composer-chips');
  if (uiMode() !== 'solo') { host.innerHTML = ''; return; }

  const p = ((state.config || {}).providers || {}).solo || {};
  const agent = state.agents.find(a => a.id === agentOf(p));
  const agentLabel = agent && (agent.command || []).length ? agent.label : 'custom';
  const modelLabel = p.model || 'default model';
  const effortLabel = p.effort || 'default';
  const hasEffort = (p.effort_args || []).length > 0;
  const caret =
    `<svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" ` +
    `stroke-width="3" stroke-linecap="round" stroke-linejoin="round">` +
    `<path d="M6 9l6 6 6-6"/></svg>`;

  host.innerHTML =
    `<button class="model-chip agent-chip set" type="button" data-agent-for="solo" ` +
      `title="Which CLI answers — ${esc(agentLabel)}">` +
      `<span class="chip-label">${esc(agentLabel)}</span>${caret}</button>` +
    `<button class="model-chip${p.model ? ' set' : ''}" type="button" data-model-for="solo" ` +
      `title="Model — ${esc(modelDetail(modelLabel))}">` +
      `<span class="chip-label">${esc(modelLabel)}</span>${caret}</button>` +
    (hasEffort
      ? `<button class="model-chip effort-chip${p.effort ? ' set' : ''}" type="button" ` +
        `data-effort-for="solo" title="How hard it is asked to think — ` +
        `${esc(p.effort || "the CLI's own default")}">` +
        `<span class="chip-label">${esc(effortLabel)}</span>${caret}</button>`
      : '');
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

/** Show the commit bar whenever the working folder is a repository with
 *  uncommitted work in it, whether a council run produced it or you did.
 *  Hidden mid-run: committing under a running agent captures a tree it is
 *  still editing. */
function renderCommitBar() {
  const bar = $('#commit-bar');
  if (!bar) return;
  const st = state.workspaceStatus;
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
      body: { message, workspace: workspacePath() },
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

/** The working-folder panel: what was chosen, and what that folder can do.
 *  Nothing chosen is a first-class state rather than an empty one — it names
 *  the scratch folder a run would land in, so "no folder" never reads as
 *  "nowhere". */
/** A path short enough for an 11px status bar. Ellipsising in CSS would clip
 *  the tail, which is the half that says which folder this is, so the head is
 *  dropped instead and the full path stays in the tooltip. */
function shortPath(path) {
  const parts = String(path || '').split('/').filter(Boolean);
  return parts.length > 2 ? `…/${parts.slice(-2).join('/')}` : path;
}

/** The status bar's folder control. Working nowhere in particular is a
 *  supported way to use this, so the scratch workspace is named as a choice
 *  rather than left looking like something the operator forgot to set. */
function renderWorkspace() {
  const folder = workspacePath();
  $('#workspace-label').textContent = folder ? shortPath(folder) : 'Scratch workspace';
  $('#workspace-btn').title =
    (folder || `No folder chosen — runs happen in ${state.scratchWorkspace || 'a scratch folder'}`) +
    '\nClick to work somewhere else.';

  const st = state.workspaceStatus;
  const chips = [];
  if (!folder) {
    // Not a warning: this says what working nowhere costs, not that it is wrong.
    chips.push('<span class="chip">no git</span>');
  } else if (st && st.is_repo) {
    chips.push(`<span class="chip">${esc(st.branch || '?')}</span>`);
    chips.push(st.clean
      ? '<span class="chip clean">clean</span>'
      : `<span class="chip dirty">${st.dirty_count} uncommitted</span>`);
  } else if (st) {
    chips.push('<span class="chip">not a git repository</span>');
  }
  $('#workspace-git').innerHTML = chips.join('');

  // Recents moved into the picker with the folder control. They were beside a
  // sidebar button that no longer exists, and the picker is now the only place
  // the operator goes to change folder.
  const recent = ((state.config || {}).recent_workspaces || [])
    .filter(r => r !== folder).slice(0, 5);
  $('#recent-workspaces').innerHTML = recent.map(r =>
    `<button class="recent-item" data-workspace="${esc(r)}" type="button" title="${esc(r)}">${esc(r)}</button>`
  ).join('');
}

function renderMode() {
  const mode = uiMode();
  // Which mode a *run* is in, so that a run in flight can still be returned to
  // after a look at Project.
  const runMode = visibleMode();
  $$('#mode-switch [data-mode]').forEach(btn => {
    const active = btn.dataset.mode === mode;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-checked', String(active));
    // Swapping Council for Chat mid-run would leave the selector and the
    // thread describing different things. Project runs nothing, so looking at
    // it while a run finishes elsewhere costs nothing and stays allowed.
    btn.disabled = state.busy &&
      btn.dataset.mode !== 'project' && btn.dataset.mode !== runMode;
  });

  const name = ((state.config || {}).display_name || '').trim() || state.user;
  const who = name ? `, ${name}` : '';
  $('#hero-title').textContent = mode === 'solo'
    ? `Hey${who}. Ready to dive in?`
    : `Hey${who}. What shall we build today?`;
  // What this mode may do to your files, which Zero-Touch changes. Chat has
  // no gate, so Zero-Touch is the whole of the difference there between a
  // conversation about the folder and one that rewrites it — worth saying
  // plainly, and worth colouring, rather than leaving to a toggle elsewhere.
  const armed = !!(state.config || {}).zero_touch;
  const sub = $('#hero-sub');
  sub.textContent = mode === 'solo'
    ? (armed
      ? 'One agent, one conversation — and Zero-Touch is on, so it can change files in your working folder.'
      : 'One agent, one conversation. Read-only: turn Zero-Touch on to let it change files.')
    : (armed
      ? 'One agent drafts, the other applies — and Zero-Touch is on, so it will not stop to ask.'
      : 'One agent drafts, you approve, the other applies.');
  sub.classList.toggle('armed', armed);
  $('#gear-btn').classList.toggle('armed', armed);

  $('#continue-copy').textContent = mode === 'solo'
    ? 'the assistant will be given that exchange as context.'
    : 'the council will be given that exchange as context.';
  $('#task-input').placeholder = mode === 'solo'
    ? 'Ask Theseus AI…'
    : 'Ask the council…';
}

function renderToggles() {
  const c = state.config || {};
  $('#zero-touch').checked = !!c.zero_touch;
  $('#safety-snapshot').checked = c.safety_snapshot !== false;
  $('#clean-worktree').checked = !!c.require_clean_worktree;
  $('#pull-request-mode').checked = !!c.pull_request_mode;
  $('#zero-touch-warning').classList.toggle('hidden', !c.zero_touch);

  // All three delivery toggles are git features. Left on and left silent in a
  // folder with no git, they read as protection that is switched on — and the
  // pull-request one turns into a run refused after the operator has already
  // typed the task. They stay operable: this says what they are worth here,
  // rather than deciding for the operator which of the two to change.
  const note = $('#no-git-warning');
  note.classList.toggle('hidden', workspaceIsRepo());
  note.textContent =
    (workspacePath() ? 'This folder' : 'The scratch workspace') +
    ' is not a git repository, so there is no diff, no snapshot and nothing ' +
    'to roll back to. Pull request mode will refuse to start.';

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
}

/* ---- The thread --------------------------------------------------------
   Council's stages are messages in the conversation rather than tabs beside
   it, so the gate, the console and the diff all sit next to the exchange that
   produced them. Both modes render through this one path: a Chat turn is a
   council turn with one stage and no gate.
   ----------------------------------------------------------------------- */

/** Widgets holding state a re-render must not destroy — a half-typed approval
 *  note, a scrolled console, an unsent commit message. They live in #parking
 *  and are *moved* into the thread rather than rebuilt from a string. Anything
 *  still sitting in the thread has to go home before its innerHTML is
 *  replaced, or the next render deletes it along with what was typed into it. */
const PARKED = ['approval-gate', 'console-block', 'diff-block'];

function park() {
  const home = $('#parking');
  PARKED.forEach(id => {
    const node = document.getElementById(id);
    if (node && node.parentNode !== home) home.appendChild(node);
  });
}

function fillSlots(root) {
  PARKED.forEach(id => {
    const slot = $(`[data-slot="${id}"]`, root);
    if (slot) slot.replaceWith(document.getElementById(id));
  });
}

/** The run's stages in the order it runs them, each tagged with its own id so
 *  a message can be coloured for the job rather than for the CLI. */
function stagesOf(run) {
  const stages = run.stages || {};
  return (run.stage_order || Object.keys(stages))
    .map(id => (stages[id] ? Object.assign({}, stages[id], { id }) : null))
    .filter(Boolean)
    // A stage that has not started yet has nothing to say, and an empty bubble
    // reading "(no output)" looks like a stage that failed quietly. What is
    // coming next is already legible on the strip above.
    .filter(s => s.state !== 'pending' || (s.output || '').trim());
}

// "done" on every finished message is noise — the output is the evidence. Only
// states that explain an absence of output earn a word.
const STAGE_WORDS = {
  running: 'working', failed: 'failed', skipped: 'skipped',
  pending: 'queued', waiting: 'gated',
};

function userHtml(task) {
  return '<div class="chat-message user-message"><div class="markdown">' +
    renderMarkdown(task || '(no message)') + '</div></div>';
}

function messageHtml(reply, id) {
  const who = reply.label || reply.stage || id || 'Agent';
  const st = reply.state || '';
  const dur = reply.duration ? ` · ${fmtDuration(reply.duration)}` : '';
  const word = STAGE_WORDS[st] || '';
  const body = renderMarkdown(reply.output || '') ||
    `<p class="history-none">${esc(reply.error || `(${st || 'no output'})`)}</p>`;
  return (
    `<div class="chat-message assistant-message" data-agent="${esc(id || '')}">` +
      `<div class="msg-head">` +
        `<span class="msg-mark">${esc(String(who).slice(0, 2).toUpperCase())}</span>` +
        `<span class="msg-who">${esc(who)}</span>` +
        (reply.role ? `<span class="msg-role">${esc(roleTag(reply.role))}</span>` : '') +
        `<span class="msg-state${st === 'failed' ? ' failed' : ''}">` +
          `${esc(word)}${esc(dur)}</span>` +
      `</div>` +
      // `data-live` marks the one body `stage_output` may append to. Only a
      // running stage has it, so a finished message can never be scribbled on.
      `<div class="markdown"${st === 'running' ? ' data-live="1"' : ''}>${body}</div>` +
    `</div>`
  );
}

function renderThread() {
  const mode = uiMode();
  const main = $('.main');
  const thread = $('#thread');
  const stream = $('#stream');
  const scrolled = stream.scrollTop;

  // Rescue the movable widgets before the innerHTML below would delete them.
  park();

  $('#project-empty').classList.toggle('hidden', mode !== 'project');
  if (mode === 'project') {
    $('#hero').classList.add('hidden');
    thread.innerHTML = '';
    main.classList.add('empty');
    return;
  }

  // Whichever conversation is on screen: the one opened from history, or the
  // live run. They are the same shape, so one path renders both.
  const run = (state.openChat && state.openChat.run) || state.run;
  const live = !state.openChat;

  $('#hero').classList.toggle('hidden', !!run);
  main.classList.toggle('empty', !run);
  if (!run) { thread.innerHTML = ''; return; }

  const council = !run.solo;
  const gated = live && council && run.state === 'awaiting_approval';
  const hasDiff = !!(run.diff && run.diff.trim());

  // Earlier turns of the same thread, replayed as context rather than run now.
  const earlier = (run.conversation || []).map(t =>
    '<div class="turn earlier">' +
      userHtml(t.task) +
      (t.replies || []).map(r => messageHtml(r, r.stage)).join('') +
      (t.compacted
        ? '<div class="turn-note">Replayed compacted to fit the context window</div>'
        : '') +
    '</div>'
  ).join('');

  const current =
    '<div class="turn">' +
      userHtml(run.task) +
      stagesOf(run).map(s =>
        messageHtml(s, s.id) +
        // The gate belongs against the draft it is judging, not at the top of
        // the screen where it used to sit with nothing to read beside it.
        (gated && s.id === 'drafter' ? '<div data-slot="approval-gate"></div>' : '')
      ).join('') +
      (run.reviewer_note
        ? `<div class="turn-note">Your note at the gate: ${esc(run.reviewer_note)}</div>`
        : '') +
      (council && live ? '<div data-slot="console-block"></div>' : '') +
      (hasDiff ? '<div data-slot="diff-block"></div>' : '') +
      (run.rollback_note ? `<div class="turn-note">${esc(run.rollback_note)}</div>` : '') +
      (run.error ? `<div class="turn-note">${esc(run.error)}</div>` : '') +
    '</div>';

  thread.innerHTML = earlier + current;

  if (hasDiff) {
    $('#diff-view').innerHTML = renderDiff(run.diff, run.diff_stat);
    const st = run.diff_stat || {};
    $('#diff-summary').textContent = st.files
      ? `Changes — ${st.files} file${st.files === 1 ? '' : 's'} ` +
        `+${st.insertions}/-${st.deletions}`
      : 'Changes';
  }

  fillSlots(thread);
  stream.scrollTop = scrolled;
  renderCommitBar();
}

/** The sidebar footer. There are no accounts here — the app is a local tool on
 *  loopback — so this is whatever name Settings was given, or failing that the
 *  OS user it was launched as. It signs you in to nothing. */
function renderProfile() {
  const chosen = ((state.config || {}).display_name || '').trim();
  const name = chosen || state.user || '';
  $('#profile-name').textContent = name || 'local';
  $('#profile-mark').textContent = (name || '?').slice(0, 2).toUpperCase();
  $('.profile').title = chosen
    ? `${chosen} — set in Settings. Theseus AI has no account of its own.`
    : state.user
      ? `Running as ${state.user}. Set a name in Settings → App.`
      : 'Theseus AI has no account of its own.';
}

function renderAll() {
  renderStatus();
  renderMode();
  renderProfile();
  renderStrip();
  renderComposerChips();
  renderWorkspace();
  renderToggles();
  renderThread();
}

/* ==========================================================================
   7. Live stream
   ========================================================================== */

const MAX_STREAM_LINES = 4000;

function pushLine(kind, tag, text) {
  const stream = $('#stream');
  // The console scrolls itself now, and there is no Follow checkbox: being
  // near the bottom already means "still watching", and scrolling up to read
  // something is the only signal the operator has stopped.
  const nearBottom =
    stream.scrollHeight - stream.scrollTop - stream.clientHeight < 120;

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

  if (nearBottom) stream.scrollTop = stream.scrollHeight;
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

/** Keep what the CLI said an alias resolves to, so the chips and the council
 *  strip can name the generation too — not just the menu that asked. */
function rememberResolved(map) {
  Object.assign(state.resolvedModels, map || {});
}

/** "opus (claude-opus-5)" once the resolution is known, "opus" until then.
 *  Never a guess: an alias with no answer yet is left to speak for itself. */
function modelDetail(model) {
  const points = state.resolvedModels[model];
  return points && points !== model ? `${model} (${points})` : model;
}

/** Ask once, in the background, so the very first tooltip is already specific.
 *  Costs nothing per alias — the server kills each probe at the handshake —
 *  and the answer is cached there for the life of the process. */
function prefetchResolvedModels() {
  const providers = (state.config || {}).providers || {};
  const wanted = Object.keys(providers).filter(id =>
    /^[a-z]+$/.test(providers[id].model || '')
  );
  if (!wanted.length) return;
  // Every one of them, not just the first: the two council seats can be on
  // different CLIs, and only one of them may be the one with aliases. The
  // server caches per binary, so asking twice about the same CLI is free.
  Promise.all(wanted.map(id =>
    api(`/api/models?provider=${encodeURIComponent(id)}`)
      .then(data => rememberResolved(data.resolved))
      .catch(() => { /* A tooltip is not worth surfacing a failure for. */ })
  )).then(() => { renderComposerChips(); renderStrip(); });
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
  let resolved = {};
  try {
    const data = await api(`/api/models?provider=${encodeURIComponent(providerId)}`);
    if (data.models && data.models.length) {
      models = data.models;
      source = data.source || '';
    }
    resolved = data.resolved || {};
    rememberResolved(resolved);
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
    models.map(m => {
      // An alias tracks the newest model in its family, so on its own it does
      // not say which generation you are about to run. Name the model it
      // points at right now; a pinned id already names itself.
      const points = resolved[m] || '';
      const note = points ? `→ ${points}` : (/^[a-z]+$/.test(m) ? 'alias · always latest' : '');
      const why = points
        ? `${m} is an alias — it currently runs ${points}, and will follow that family as it is updated.`
        : '';
      return (
        `<button class="model-opt${m === current ? ' active' : ''}" data-value="${esc(m)}"` +
          `${why ? ` title="${esc(why)}"` : ''}>` +
          `<span class="model-opt-name">${esc(m)}</span>` +
          `<span class="model-opt-note">${esc(note)}</span>` +
        `</button>`
      );
    }).join('') +
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

/** Everything about one council member, from one click on it. Each row opens
 *  the menu that already owns that setting rather than reimplementing it here:
 *  four settings, four existing menus, no fifth copy of the model list to drift
 *  out of step with the other one. */
function openMemberMenu(anchor, providerId) {
  closeModelMenu();
  const provider = ((state.config || {}).providers || {})[providerId] || {};
  const agent = state.agents.find(a => a.id === agentOf(provider));
  const agentLabel = agent && (agent.command || []).length ? agent.label : 'custom command';
  const hasEffort = (provider.effort_args || []).length > 0;

  const rows = [
    ['agent', 'CLI', agentLabel],
    ['model', 'Model', provider.model ? modelDetail(provider.model) : 'the CLI’s default'],
    ...(hasEffort ? [['effort', 'Effort', provider.effort || 'the CLI’s default']] : []),
    ['role', 'Role', provider.role || 'none'],
  ];

  const menu = document.createElement('div');
  menu.className = 'model-menu';
  menu.innerHTML =
    `<div class="model-menu-head">${esc(provider.label || providerId)}</div>` +
    rows.map(([key, label, value]) =>
      `<button class="model-opt" data-open="${key}">` +
        `<span class="model-opt-name">${esc(label)}</span>` +
        `<span class="model-opt-note">${esc(value)}</span>` +
      `</button>`
    ).join('') +
    `<div class="model-menu-source">Applies to this stage only.</div>`;
  document.body.appendChild(menu);
  positionModelMenu(menu, anchor);

  menu.addEventListener('click', (e) => {
    const opt = e.target.closest('[data-open]');
    if (!opt) return;
    closeModelMenu();
    const open = {
      agent: openAgentMenu, model: openModelMenu,
      effort: openEffortMenu, role: openRoleMenu,
    }[opt.dataset.open];
    if (open) open(anchor, providerId);
  });

  setTimeout(() => {
    document.addEventListener('click', onDocClickCloseModel, { once: true });
  }, 0);
}

/** Role menu. The catalogue is the server's, the same one Settings edits, so
 *  a role written there is selectable here the moment it is saved. */
function openRoleMenu(anchor, providerId) {
  closeModelMenu();
  const provider = ((state.config || {}).providers || {})[providerId] || {};
  const current = provider.role_template || '';

  const menu = document.createElement('div');
  menu.className = 'model-menu';
  menu.innerHTML =
    `<div class="model-menu-head">${esc(provider.label || providerId)} role</div>` +
    (state.roles || []).map(r =>
      `<button class="model-opt${r.id === current ? ' active' : ''}" ` +
        `data-value="${esc(r.id)}" title="${esc(r.summary || '')}">` +
        `<span class="model-opt-name">${esc(r.name || r.id)}</span>` +
        `<span class="model-opt-note">${r.writes ? 'writes files' : 'read-only'}</span>` +
      `</button>`
    ).join('') +
    `<div class="model-menu-source">` +
      (provider.role_system
        ? 'This stage has edited role text; picking one here replaces it.'
        : 'Wording is editable in Settings → Roles.') +
    `</div>`;
  document.body.appendChild(menu);
  positionModelMenu(menu, anchor);

  menu.addEventListener('click', async (e) => {
    const opt = e.target.closest('.model-opt');
    if (!opt) return;
    closeModelMenu();
    const role = (state.roles || []).find(r => r.id === opt.dataset.value);
    if (!role || role.id === current) return;
    const roleName = role.name || role.id;
    // `role_system` blank means "use the template", so it is cleared with the
    // swap — otherwise the old role's edited text would be sent under the new
    // role's name, which is the one combination that reads as neither.
    await patchConfig({ providers: { [providerId]: {
      role_template: role.id, role: roleName, role_system: '',
    } } });
    toast(`${provider.label || providerId} → ${roleName}`, 'ok', 2600);
  });

  setTimeout(() => {
    document.addEventListener('click', onDocClickCloseModel, { once: true });
  }, 0);
}

/** The gear beside the composer: the two options that get changed per run.
 *  The rest are a click further on, in Settings, because they are decisions
 *  about the project rather than about the message being sent. */
function openGearMenu(anchor) {
  closeModelMenu();
  const c = state.config || {};
  // Both apply in both modes. Zero-Touch means the same thing either way —
  // the CLI gets its auto-approve flags — it is just that Council can also be
  // granted that at the gate, and Chat, having no gate, cannot.
  const chat = uiMode() === 'solo';

  const row = (key, label, on, danger) =>
    `<button class="menu-toggle${on ? ' on' : ''}${danger ? ' danger' : ''}" ` +
      `data-toggle="${key}">` +
      `<span class="menu-box">✓</span>${esc(label)}</button>`;

  const menu = document.createElement('div');
  menu.className = 'model-menu gear-menu';
  menu.innerHTML =
    row('zero_touch', 'Zero-Touch mode', !!c.zero_touch, true) +
    row('pull_request_mode', 'Pull request mode', !!c.pull_request_mode, false) +
    `<hr>` +
    `<button class="model-opt" data-open="settings">` +
      `<span class="model-opt-name">More…</span>` +
      `<span class="model-opt-note">Settings</span>` +
    `</button>` +
    `<div class="model-menu-source">` +
      (chat
        ? (c.zero_touch
          ? 'Chat can change files in the working folder.'
          : 'Chat is read-only. Zero-Touch is the only way to let it write, ' +
            'because it has no approval gate.')
        : 'Applies to every council run until changed.') +
    `</div>`;
  document.body.appendChild(menu);
  positionModelMenu(menu, anchor);

  menu.addEventListener('click', (e) => {
    if (e.target.closest('[data-open="settings"]')) {
      closeModelMenu();
      $('#settings-btn').click();
      return;
    }
    const btn = e.target.closest('[data-toggle]');
    if (!btn || btn.disabled) return;
    closeModelMenu();
    // Routed through the real checkbox so the confirmations live in one place:
    // the gear must not be a second, quieter way to switch Zero-Touch on.
    const box = $(btn.dataset.toggle === 'zero_touch' ? '#zero-touch' : '#pull-request-mode');
    box.checked = !box.checked;
    box.dispatchEvent(new Event('change'));
  });

  setTimeout(() => {
    document.addEventListener('click', onDocClickCloseModel, { once: true });
  }, 0);
}

/** Agent menu. The list is the server's catalogue, so the browser still
 *  carries no command and no permission flag of its own — it sends an id and
 *  the server expands it. */
function openAgentMenu(anchor, providerId) {
  closeModelMenu();
  const provider = ((state.config || {}).providers || {})[providerId] || {};
  const current = agentOf(provider);

  const menu = document.createElement('div');
  menu.className = 'model-menu';
  menu.innerHTML =
    `<div class="model-menu-head">${esc(provider.label || providerId)} agent</div>` +
    // "Custom command" is what a hand-edited template reads back as, not
    // something this menu can apply: there is no preset behind it. Writing one
    // stays in Settings, where the command itself is.
    state.agents.filter(a => (a.command || []).length).map(a =>
      `<button class="model-opt${a.id === current ? ' active' : ''}" ` +
        `data-value="${esc(a.id)}">` +
        `<span class="model-opt-name">${esc(a.label)}</span>` +
        `<span class="model-opt-note">${esc(a.command[0])}</span>` +
      `</button>`
    ).join('') +
    `<div class="model-menu-source">` +
      (current === 'custom'
        ? 'Running a hand-written command; picking one here replaces it.'
        : 'Swaps the command and its permission flags together.') +
    `</div>`;
  document.body.appendChild(menu);
  positionModelMenu(menu, anchor);

  menu.addEventListener('click', (e) => {
    const opt = e.target.closest('.model-opt');
    if (!opt) return;
    closeModelMenu();
    setAgent(providerId, opt.dataset.value);
  });

  setTimeout(() => {
    document.addEventListener('click', onDocClickCloseModel, { once: true });
  }, 0);
}

async function setAgent(providerId, agentId) {
  const provider = ((state.config || {}).providers || {})[providerId] || {};
  if (agentId === agentOf(provider)) return;
  // Only the id: the server pairs the command with its own permission flags,
  // and clears the model and reasoning level, which are not interchangeable.
  await patchConfig({ providers: { [providerId]: { agent: agentId } } });
  // The quota on the card was the departing CLI's. Drop it rather than let it
  // sit under the new one's name until the next reading lands.
  if (state.usage) delete state.usage[providerId];
  // A different executable, which may not be installed at all.
  await refreshDoctor();
  const now = ((state.config || {}).providers || {})[providerId] || {};
  toast(`${provider.label || providerId} → ${now.label || agentId}`, 'ok', 2600);
  api('/api/usage/refresh', { method: 'POST' })
    .then(d => { state.usage = d.usage || {}; renderStrip(); })
    .catch(() => {});
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
    // Named in full at the moment of choosing, which is when it matters most:
    // picking "opus" should confirm which generation that is today.
    value ? `${provider.label || providerId} → ${modelDetail(value)}`
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
    // Antigravity treats every model it lists as a complete selection and
    // rejects `--effort` beside one rather than picking a winner. That is a
    // definite "no", unlike the empty list below, so it clears on its own.
    if (data.conflicts_with_model) {
      await patchConfig({ providers: { [providerId]: { effort: '' } } });
      toast(data.error || 'That model takes no separate effort.', 'warn', 6000);
      return;
    }
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

  // Any folder is selectable. Being a repository is not a requirement, it is
  // the difference between a run you can undo and one you cannot — so the
  // status line says which of the two this folder is buying.
  const status = listing.is_repo
    ? 'A git repository — diff, safety snapshot and rollback all work here.'
    : 'Not a git repository — the agents can work here, but there is no diff ' +
      'and no rollback.';
  $('#picker-status').textContent = listing.error || status;
  $('#picker-select').disabled = !!listing.error;

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

/** Point the app at a folder. Any folder: `status.path` is the repository root
 *  when there is one, so a subdirectory of a project still resolves to the
 *  place the diff and the snapshot operate on. */
async function selectWorkspace(path) {
  try {
    const { status } = await api(`/api/repo?path=${encodeURIComponent(path)}`);
    const { config } = await api('/api/config', {
      method: 'POST',
      body: { workspace: status.path },
    });
    state.config = config;
    state.workspaceStatus = status;
    renderAll();
    closeModal('picker');
    toast(
      status.is_repo
        ? `Working in ${status.path}`
        : `Working in ${status.path} — not a git repository, so no diff or ` +
          `rollback.`,
      'ok', status.is_repo ? 3200 : 5200
    );
  } catch (err) {
    toast(err.message, 'error');
  }
}

/** Go back to having no folder. Runs then happen in the scratch workspace,
 *  which is the state a fresh install starts in. */
async function clearWorkspace() {
  try {
    const { config } = await api('/api/config', {
      method: 'POST',
      body: { workspace: '' },
    });
    state.config = config;
    state.workspaceStatus = null;
    renderAll();
    closeModal('picker');
    const where = state.scratchWorkspace || 'the scratch workspace';
    toast(`No working folder. Runs now happen in ${where}.`, 'ok', 5200);
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

  // The council's two stages and Solo's one assistant, configured
  // independently. `council` decides which half of the form each one gets:
  // a stage is told what job to do and picks its agent here, the assistant is
  // only ever given a behaviour the operator typed and picks its agent from
  // its own card, where changing it is a gesture rather than a visit.
  const FORMS = [
    { id: 'drafter', num: 'Stage 1', council: true },
    { id: 'polisher', num: 'Stage 2', council: true },
    { id: 'solo', num: 'Chat', council: false },
  ];

  host.innerHTML = FORMS.map(({ id, num, council }) => {
    const p = providers[id] || {};
    const info = state.providers.find(x => x.id === id);
    const probeHtml = info
      ? `<span class="probe ${info.available ? 'ok' : 'miss'}">` +
        `${info.available ? 'found' : 'not found'}</span>`
      : '';
    return (
      `<div class="provider-form" data-provider="${id}" ` +
        `data-council="${council ? '1' : ''}">` +
        `<h4><span class="stage-num">${esc(num)}</span> ` +
          `${esc(p.role || (council ? id : 'Assistant'))} ${probeHtml}</h4>` +
        (council
          ? `<div class="field-row">` +
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
            `</div>`
          // Solo's agent is the chip on its card. Two places to pick it would
          // be two apparent sources of truth, and the modal is the slower one.
          : `<div class="field">` +
              `<label>Display name ` +
                `<span class="field-hint">— the agent itself is the first chip ` +
                `on the Assistant card</span></label>` +
              `<input type="text" data-field="label" value="${esc(p.label || '')}">` +
            `</div>`) +
        (council
          ? `<div class="field">` +
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
            `</div>`
          // No role, no template and no fallback: blank here means blank, so a
          // new Solo conversation reaches the CLI as the message alone.
          : `<div class="field">` +
              `<label>Behaviour ` +
                `<span class="field-hint">— optional; blank sends your message ` +
                `on its own</span></label>` +
              `<textarea rows="5" class="role-system" data-field="behavior" ` +
                `placeholder="e.g. Answer briefly, and always show the code.">` +
                `${esc(p.behavior || '')}</textarea>` +
            `</div>`) +
        // Everything below is the CLI plumbing the agent picker fills in for
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
            (council ? '' :
              `<div class="field">` +
                `<label>Read-only arguments ` +
                  `<span class="field-hint">— added whenever Chat is ` +
                  `read-only, which is any run with Zero-Touch off. With it on, ` +
                  `the auto-approve arguments are sent instead</span></label>` +
                `<textarea rows="2" data-field="read_only_args">` +
                  `${esc((p.read_only_args || []).join('\n'))}</textarea>` +
              `</div>`) +
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
  $('#display-name').value = conf.display_name || '';
  $('#port-input').value = conf.port || 8760;
  $('#open-browser').checked = conf.open_browser !== false;
}

/** Flag a behaviour whose write expectation disagrees with the stage's actual
 *  permission. Not resolved automatically: guessing which of the two the
 *  operator meant is how a safety setting stops being trustworthy. */
function updateRoleWarning(form) {
  // Solo has no role and no per-stage permission, so there is no mismatch it
  // could be in.
  if (!form || !form.dataset.council) return;
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
      // otherwise - so a hand-edited command below still wins. Solo has no
      // field here: it picks its agent on its card, and a stale value carried
      // by this form would quietly undo that on the next save.
      ...(field('agent') ? { agent: field('agent').value } : {}),
      label: field('label').value.trim() || id,
      command,
      auto_approve_args: lines('auto_approve_args'),
      stream_args: lines('stream_args'),
      // A council stage has a role; the Solo assistant has a behaviour and
      // nothing else. Writing the other one's keys would leave a dead setting
      // behind that still reads as though it decided something.
      ...(form.dataset.council
        ? {
            role_template: field('role_template').value,
            role_system: field('role_system').value.trim(),
          }
        : {
            behavior: field('behavior').value.trim(),
            read_only_args: lines('read_only_args'),
          }),
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
        display_name: $('#display-name').value.trim(),
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
    renderStrip();
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
  // Ask for the mode on screen. Council and Chat conversations cannot be
  // continued in each other, so a mixed list would offer rows that clicking
  // one of cannot do what the click promises.
  const mode = selectedMode();
  state.chatMode = mode;
  const list = $('#chat-list');
  if (!list.childElementCount) {
    list.innerHTML = '<div class="picker-empty">Loading…</div>';
  }
  try {
    const { runs } = await api(`/api/history?mode=${encodeURIComponent(mode)}`);
    // A mode switch while this was in flight makes the answer the wrong list.
    if (state.chatMode !== mode) return;
    state.chats = runs;
    renderChats();
  } catch (err) {
    list.innerHTML = `<div class="picker-empty">${esc(err.message)}</div>`;
  }
}

// Buckets matching how the operator thinks about "the one from yesterday".
// Everything past a week falls into one group rather than a heading per day,
// which would make the labels longer than the list they label.
const GROUP_ORDER = ['Today', 'Yesterday', 'Previous 7 days', 'Older'];

function chatGroup(ts) {
  if (!ts) return 'Older';
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  // Whole days between the start of today and the conversation. Negative means
  // it is later than midnight this morning, i.e. today.
  const days = Math.floor((midnight.getTime() - ts * 1000) / 86400000);
  if (days < 0) return 'Today';
  if (days === 0) return 'Yesterday';
  return days < 7 ? 'Previous 7 days' : 'Older';
}

/** A time for today's rows and a date for older ones: a bare "10:42" under
 *  "Previous 7 days" does not say which of those days it was. */
function chatWhen(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return chatGroup(ts) === 'Today'
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function renderChats() {
  const list = $('#chat-list');
  const chats = state.chats || [];

  if (!chats.length) {
    list.innerHTML = '<div class="picker-empty">' +
      (state.chatMode === 'solo'
        ? 'No chats yet. Ask something to start one.'
        : 'No council runs yet. Describe a change to start one.') +
      '</div>';
    return;
  }

  const open = state.openChat ? state.openChat.file : '';
  const buckets = new Map(GROUP_ORDER.map(g => [g, []]));
  chats.forEach(c => buckets.get(chatGroup(c.created_at)).push(c));

  list.innerHTML = GROUP_ORDER.filter(g => buckets.get(g).length).map(g =>
    `<div class="chat-group">${esc(g)}</div>` +
    buckets.get(g).map(c =>
      `<div class="chat-item">` +
      `<button class="chat-row ${esc(c.state)}${c.file === open ? ' open' : ''}" ` +
        `type="button" data-chat-file="${esc(c.file)}" ` +
        `title="${esc(c.workspace || 'Scratch workspace')}">` +
        `<span class="chat-row-title">${esc(c.title || c.task || '(no task)')}</span>` +
        `<span class="chat-row-meta">${esc(chatWhen(c.created_at))}` +
          `${c.zero_touch ? ' · zero-touch' : ''}</span>` +
      `</button>` +
      // Outside the row's own button, not inside it: a button within a button
      // is invalid HTML and the browser hoists it out, which lands it in the
      // wrong place. Hidden until the row is hovered or focused.
      `<button class="chat-del" type="button" data-del-chat="${esc(c.file)}" ` +
        `title="Delete this conversation" aria-label="Delete conversation">&times;</button>` +
      `</div>`
    ).join('')
  ).join('');
}

/** Open a conversation in the thread *and* attach it to the composer, so the
 *  next message continues it where it is. Reading a thread and continuing it
 *  used to be two steps with a button between them; they are one gesture now,
 *  which is the whole difference between a chat list and a log. */
async function openChat(file) {
  // Marked open before the fetch, not after: `renderThread` decides what is on
  // screen from this, and an event arriving mid-load would otherwise drop the
  // live run on top of the conversation being opened.
  state.openChat = { file, run: null };
  state.tab = '';
  renderChats();

  try {
    const { run } = await api(`/api/run?file=${encodeURIComponent(file)}`);
    if (!state.openChat || state.openChat.file !== file) return;
    state.openChat = { file, run };

    // A thread continues in the mode it already was. Sending a council run
    // into a chat is refused by the server anyway, so the selector follows the
    // conversation rather than letting the operator walk into that.
    const earlier = run.conversation || [];
    const title = (earlier.length ? earlier[0].task : run.task) || '';
    await continueRun(file, title, run.mode || (run.solo ? 'solo' : 'council'), true);
    renderAll();
  } catch (err) {
    toast(err.message, 'error');
    state.openChat = null;
    renderAll();
  }
}

/** How many conversations each mode is holding, for the Settings buttons.
 *  Counted server-side per mode rather than inferred from the sidebar, which
 *  only ever holds the list for the mode currently on screen. */
async function renderHistoryCounts() {
  const set = (sel, n) => {
    const node = $(sel);
    node.textContent = n == null ? '' : `(${n})`;
    // A button that would delete nothing says so by being inert, rather than
    // asking for a confirmation and then reporting "0 deleted".
    node.parentElement.disabled = n === 0;
  };
  set('#count-council', null);
  set('#count-chat', null);
  try {
    const [council, chat] = await Promise.all([
      api('/api/history?mode=council'),
      api('/api/history?mode=solo'),
    ]);
    set('#count-council', (council.runs || []).length);
    set('#count-chat', (chat.runs || []).length);
  } catch {
    // Leave the counts blank; the buttons still work and will report what
    // they actually deleted.
  }
}

/** Remove one conversation. The transcript is the only copy — there is no bin
 *  to fish it out of — so this confirms by name rather than on a count. */
async function deleteChat(file) {
  const row = (state.chats || []).find(c => c.file === file);
  const title = (row && (row.title || row.task)) || 'this conversation';
  if (!confirm(`Delete "${title}"?\n\nThe transcript is deleted from disk. ` +
               `This cannot be undone.`)) return;
  try {
    await api('/api/history/delete', { method: 'POST', body: { file } });
    // It may be the one on screen, or the one the composer is attached to.
    if (state.openChat && state.openChat.file === file) closeChat();
    if (state.continueFrom === file) clearContinuation();
    await loadChats();
    toast('Conversation deleted.', 'ok', 2600);
  } catch (err) {
    toast(err.message, 'error');
  }
}

/** Clear one mode's history, from Settings. Scoped to the mode on screen for
 *  the same reason the sidebar is: they are two separate lists. */
async function clearHistory(mode) {
  const label = mode === 'solo' ? 'chat' : 'council';
  const count = (state.chatMode === mode ? (state.chats || []).length : null);
  if (!confirm(
    `Delete every ${label} conversation?` +
    (count != null ? `\n\n${count} conversation${count === 1 ? '' : 's'} ` +
                     `will be deleted from disk.` : '') +
    `\n\nThis cannot be undone.`
  )) return;
  try {
    const { deleted } = await api('/api/history/delete',
      { method: 'POST', body: { all: true, mode } });
    closeChat();
    clearContinuation();
    await loadChats();
    renderHistoryCounts();
    toast(`${deleted} conversation${deleted === 1 ? '' : 's'} deleted.`, 'ok');
  } catch (err) {
    toast(err.message, 'error');
  }
}

/** Back to the live run, and detached: leaving a conversation on screen while
 *  the composer still pointed at it is what made the old Continue button
 *  necessary in the first place. */
function closeChat() {
  if (!state.openChat) return;
  state.openChat = null;
  clearContinuation();
  renderAll();
  renderChats();
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
    // A conversation opened from history is on screen where the answer is
    // about to render, and a run starting is where attention belongs.
    state.openChat = null;
    state.tab = '';
    // The unsent commit message belonged to the run that just ended; carried
    // into this one it would describe the wrong change.
    $('#commit-message').value = '';
    if (!d.run.solo) {
      clearStream();
      pushDivider('Run started');
      pushLine('sys', 'system', `Task: ${d.run.task}`);
      pushLine('sys', 'system', `Folder: ${runWorkspace(d.run)}`);
      if (d.run.zero_touch) {
        pushLine('warn', 'system', 'Zero-Touch Mode: approvals will be skipped.');
      }
      // Open while the run is going, and left alone afterwards — closing it on
      // completion would fight an operator who had opened it to read something.
      $('#console-block').open = true;
    }
    renderAll();
    renderChats();
  });

  on('state', (d) => {
    state.run = d.run;
    state.busy = !['complete', 'failed', 'cancelled'].includes(d.state);
    const solo = !!d.run && d.run.solo;
    if (d.state === 'awaiting_approval') {
      toast('Approval needed. Nothing has been written yet.', 'warn', 8000);
    }
    if (d.state === 'complete') {
      toast(solo ? 'Reply complete.' : 'Run complete.', 'ok');
      // What the run changed is the thing to read next, so it is open on
      // arrival. Only here, never on a plain re-render, so an operator who
      // collapses it is not overruled by the next event.
      if (d.run && d.run.diff) $('#diff-block').open = true;
    }
    if (d.state === 'failed') toast(d.run.error || 'Run failed.', 'error', 9000);
    if (d.state === 'cancelled') toast('Run cancelled.', 'warn');
    // The transcript is written when the run reaches a terminal state, which is
    // when the conversation list can show it — and when a follow-up has folded
    // its parent into itself and must replace it there.
    if (!state.busy) {
      loadChats();
      // The run has just written to the working tree, so the git status the
      // status bar and the commit bar are drawn from is now a run out of date.
      // Nothing else re-reads it, and the commit bar stays hidden until it does.
      loadState();
    }
    renderAll();
  });

  on('stage_started', (d) => {
    state.run = d.run;
    if (!d.run.solo) {
      const stage = d.run.stages[d.stage];
      pushDivider(`${stage.label} · ${stage.role}`);
      if (stage.command && stage.command.length) {
        pushLine('sys', 'exec', stage.command.join(' '));
      }
    }
    renderAll();
  });

  on('stage_output', (d) => {
    if (state.run && state.run.solo) {
      // Progressive, and provisional: `stage_finished` replaces this with the
      // CLI's own final answer, which is prose rather than a transcript of it.
      // Written straight into the live message, which no render between here
      // and `stage_finished` rebuilds.
      const live = $('#thread .assistant-message .markdown[data-live]');
      if (live) live.textContent += `${d.line}\n`;
      return;
    }
    // Tag with the stage's own label: either agent can hold either job, so a
    // hardcoded "codex"/"claude" would mislabel half the stream.
    const stage = state.run && state.run.stages ? state.run.stages[d.stage] : null;
    pushLine(`${d.stage} ${d.stream === 'stderr' ? 'stderr' : ''}`,
             (stage && stage.label) || d.stage, d.line);
  });

  on('stage_finished', (d) => {
    state.run = d.run;
    if (!d.run.solo) {
      const stage = d.run.stages[d.stage];
      pushLine(d.ok ? 'sys' : 'err', 'exit',
        `${stage.label} finished in ${fmtDuration(stage.duration)}` +
        (d.ok ? '' : ` — ${stage.error || 'failed'}`));
    }
    renderAll();
  });

  on('log', (d) => {
    // Every log line the engine emits is about council machinery — gates,
    // branches, snapshots — none of which Solo has.
    if (state.run && state.run.solo) return;
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
    renderStrip();
  });

  on('config', (d) => {
    const was = selectedMode();
    state.config = d.config;
    // Another tab, or a settings reset, can move the mode out from under this
    // one. The sidebar is then showing the wrong mode's conversations.
    if (selectedMode() !== was) loadChats();
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
    state.workspaceStatus = data.workspace_status;
    state.scratchWorkspace = data.scratch_workspace || '';
    state.usage = data.usage || {};
    state.user = data.user || '';
    renderAll();
  } catch (err) {
    toast(err.message, 'error', 12000);
  }
}

async function startRun() {
  const task = $('#task-input').value.trim();
  if (!task) return;
  const conf = state.config || {};

  const solo = selectedMode() === 'solo';

  // Quota warning. Advisory only: the reading is a snapshot, and only the
  // operator knows whether this particular task is worth the remaining budget.
  const worst = worstUsageFor(solo ? ['solo'] : ['drafter', 'polisher']);
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

  if (!solo && conf.zero_touch) {
    const ok = confirm(
      'Zero-Touch Mode is ON.\n\n' +
      'The pipeline will run to completion without pausing, and the senior ' +
      'stage will modify files in:\n\n' +
      (workspacePath() || `${state.scratchWorkspace} (scratch workspace)`) +
      (workspaceIsRepo() ? '' :
        '\n\nThat folder is not a git repository, so this run cannot be ' +
        'rolled back.') +
      '\n\nContinue?'
    );
    if (!ok) return;
  }
  try {
    await api('/api/start', {
      method: 'POST',
      body: {
        task,
        workspace: workspacePath(),
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
  // -- mode -------------------------------------------------------------
  $('#mode-switch').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-mode]');
    if (!btn || btn.disabled) return;
    const want = btn.dataset.mode;
    if (want === uiMode()) return;

    // Project is a placeholder, so it is remembered in the page and nowhere
    // else. Leaving it puts the operator back in the real mode config still
    // holds, which is why nothing is patched on the way out either.
    if (want === 'project') { state.tab = 'project'; renderAll(); return; }
    state.tab = '';

    // A conversation belongs to the mode it was started in, so switching
    // detaches whatever is attached rather than continuing it in the other.
    if (want !== selectedMode()) {
      if (state.continueFrom) clearContinuation();
      closeChat();
      // Awaited: the sidebar lists one mode's conversations, and `loadChats`
      // reads the mode back out of the config it is about to be given. Fired
      // without waiting, it asks for the list of the mode being left.
      await patchConfig({ mode: want });
      loadChats();
    } else {
      renderAll();
    }
  });

  // -- composer ---------------------------------------------------------
  const input = $('#task-input');
  const autosize = () => {
    // Reset first: without it the box can only ever grow, because scrollHeight
    // is measured against the height already set.
    input.style.height = 'auto';
    input.style.height = `${input.scrollHeight}px`;
  };
  input.addEventListener('input', () => { autosize(); renderStatus(); });
  input.addEventListener('keydown', (e) => {
    // Enter sends, as in every chat box; Shift+Enter is the newline. Ctrl+Enter
    // still works, because that is what the old composer taught.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      startRun();
    }
  });
  $('#run-btn').addEventListener('click', startRun);

  // -- run options (the gear) -------------------------------------------
  $('#gear-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    if ($('.model-menu')) { closeModelMenu(); return; }
    openGearMenu($('#gear-btn'));
  });

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
        'Auto-approve flags (--dangerously-skip-permissions) will be passed ' +
        'to the CLI. Files will be created, modified and deleted without ' +
        'asking you first.\n\n' +
        '· Council runs will not stop at the approval gate.\n' +
        '· Chat stops being read-only and can change files too.\n\n' +
        'This applies in both modes. Keep "Safety snapshot" on so you can ' +
        'roll back.'
      );
      if (!ok) { e.target.checked = false; return; }
    }
    patchConfig({ zero_touch: e.target.checked });
  });
  $('#safety-snapshot').addEventListener('change', e =>
    patchConfig({ safety_snapshot: e.target.checked }));
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

  // -- agent, model, effort and role pickers ----------------------------
  // Delegated: both hosts are re-rendered on every state change. A member on
  // the strip opens one menu covering all four settings; Chat's chips in the
  // composer open the single menu each of them names.
  $('#council-strip').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-usage-for]');
    if (chip) {
      e.stopPropagation();
      chip.classList.add('checking');
      api('/api/usage/refresh', { method: 'POST' })
        .then(d => { state.usage = d.usage || {}; renderStrip(); })
        .catch(err => toast(err.message, 'error'));
      return;
    }
    const member = e.target.closest('.member');
    if (!member) return;
    e.stopPropagation();
    if ($('.model-menu')) { closeModelMenu(); return; }  // toggle
    openMemberMenu(member, member.dataset.member);
  });

  $('#composer-chips').addEventListener('click', (e) => {
    const chip = e.target.closest('.model-chip');
    if (!chip) return;
    e.stopPropagation();
    if ($('.model-menu')) { closeModelMenu(); return; }  // toggle
    if (chip.dataset.agentFor) openAgentMenu(chip, chip.dataset.agentFor);
    else if (chip.dataset.effortFor) openEffortMenu(chip, chip.dataset.effortFor);
    else openModelMenu(chip, chip.dataset.modelFor);
  });
  window.addEventListener('resize', closeModelMenu);

  // -- working-folder picker --------------------------------------------
  $('#workspace-btn').addEventListener('click', () => {
    openModal('picker');
    loadPicker(workspacePath() || '~');
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
  $('#picker-select').addEventListener('click', () => selectWorkspace(pickerPath));
  $('#picker-clear').addEventListener('click', clearWorkspace);

  $('#recent-workspaces').addEventListener('click', (e) => {
    const btn = e.target.closest('.recent-item');
    if (btn) selectWorkspace(btn.dataset.workspace);
  });

  // -- settings ---------------------------------------------------------
  $('#settings-btn').addEventListener('click', async () => {
    await refreshDoctor(true);
    renderSettings();
    renderRoleList();
    renderHistoryCounts();
    switchSettingsTab('stages');
    openModal('settings');
  });
  $('#clear-council').addEventListener('click', () => clearHistory('council'));
  $('#clear-chat').addEventListener('click', () => clearHistory('solo'));
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

  // Picking a council stage's agent fills in its command and flags straight
  // away, so the form shows what will actually be saved. The server performs
  // the same swap on save; this is the preview, not the source of truth.
  // Delegated because the provider forms are rebuilt every time Settings opens.
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
    // No read-only arguments to preview: they are Solo's, and Solo picks its
    // agent on its card. The server still swaps them there, in one step with
    // the command, which is the pairing that has to stay honest.
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
  // Delegated: the list is rebuilt whenever it is reloaded.
  $('#chat-list').addEventListener('click', (e) => {
    const del = e.target.closest('[data-del-chat]');
    if (del) { e.stopPropagation(); deleteChat(del.dataset.delChat); return; }
    const row = e.target.closest('[data-chat-file]');
    if (row) openChat(row.dataset.chatFile);
  });
  $('#new-chat').addEventListener('click', () => {
    state.tab = '';
    state.run = null;
    clearContinuation();
    closeChat();
    renderAll();
    renderChats();
    $('#task-input').focus();
  });

  // -- continuation -----------------------------------------------------
  $('#continue-clear').addEventListener('click', () => {
    clearContinuation();
    closeChat();
  });
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
  prefetchResolvedModels();
  $('#task-input').focus();
}

boot();
