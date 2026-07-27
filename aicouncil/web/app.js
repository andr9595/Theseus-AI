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
  // Which surface is on screen. 'project' is a third one rather than a third
  // mode: it starts a project, not a run, and the server's `mode` knows only
  // the two a run can be started in. Held here and never persisted.
  tab: '',
  // The Projects tab, from /api/project. `project` is null when the chosen
  // folder has no build in it; `running` says whether agents are moving right
  // now, and `resumable` whether there is one on disk to pick up.
  project: null,
  projectRunning: false,
  projectResumable: false,
  // Availability of the three chairs, so a missing CLI is visible in the
  // matrix before the start button is pressed rather than in the refusal.
  projectRoles: [],
  // The bounds on an autonomous build, from the server. Held so Settings can
  // render them without a second request.
  projectSettings: {},
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
  // Who the router would seat for what is currently in the composer, from
  // /api/council/route. Refreshed as you type, because the bench is chosen
  // from the prompt and a strip showing last run's council would be worse
  // than one showing none. Null until the first answer arrives.
  seating: null,
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

/** The same label, trimmed for a seat on the bench. "Council Member" is the
 *  neutral persona's full name and reads fine in a menu, but on a row of
 *  council seats the first word is true of every one of them and costs the
 *  width the CLI's name needs. */
function seatTag(role) {
  return roleTag(String(role || '').replace(/^council\s+/i, ''));
}

/** The council strip: one compact button per stage, in a single row above the
 *  thread. Clicking a member opens everything about it — CLI, model, effort
 *  and role — rather than spreading those across chips that widen the strip
 *  until it no longer fits on one line. */
/** The seating the strip should draw: the live run's if there is one, the
 *  router's preview of the composer otherwise.
 *
 *  A run's own seating always wins. The bench is frozen when a run starts —
 *  the human approves a council at the gate and must get that one — so
 *  redrawing the strip from a preview mid-run would show a council that is not
 *  the one working. */
function activeSeating() {
  const run = (state.openChat && state.openChat.run) || state.run;
  if (run && !run.solo && run.seating) return run.seating;
  return state.seating;
}

/** Whether the bench is drawn at all. A display choice, toggled from the gear
 *  beside the composer and kept in the config so it survives a reload. */
function seatsShown() {
  return ((state.config || {}).council || {}).show_seats !== false;
}

function renderStrip() {
  const strip = $('#council-strip');
  const seating = activeSeating();
  // Nothing to draw is not the same as switched off, but on screen it is: an
  // empty bordered row explaining itself is worse than no row. The sentence
  // that used to live here is the composer's placeholder now.
  const show = uiMode() === 'council' && seatsShown() && !!seating;
  strip.classList.toggle('hidden', !show);
  if (!show) { strip.innerHTML = ''; return; }

  const run = (state.openChat && state.openChat.run) || state.run;
  const providers = (state.config || {}).providers || {};
  const seats = [...(seating.members || []), seating.chair].filter(Boolean);

  // Inside a `.column`, so the bench lines up with the composer it seats
  // rather than being centred on its own and drifting out of step with it.
  //
  // The notes go *under* the bench, not beside it. They were siblings of the
  // row inside a flex container, which laid them out as another column of it -
  // a paragraph of small print alongside the seats, growing the strip
  // sideways until the seats themselves had no room left to spell their names.
  strip.innerHTML =
    '<div class="column">' +
      '<div class="strip-inner">' +
      seats.map(seat => seatHtml(seat, run, providers)).join('') +
      '</div>' +
      ((seating.notes || []).length
        ? `<div class="strip-notes">` +
            (seating.notes || []).map(n =>
              `<p class="strip-note-line">${esc(n)}</p>`
            ).join('') +
          `</div>`
        : '') +
    '</div>';
}

/** One seat on the strip. Coloured by which CLI is in it rather than by which
 *  chair it is: the chair moves between runs, and a colour that followed the
 *  chair would recolour the whole strip every time the routing changed. */
function seatHtml(seat, run, providers) {
  const p = providers[seat.provider_id] || {};
  const stageId = seat.chairman ? 'chair' : seat.id;
  const stage = run && run.stages ? run.stages[stageId] : null;
  const info = state.providers.find(x => x.id === seat.provider_id);
  const available = !info || info.available;

  let st = stage ? stage.state : 'pending';
  if (run && run.state === 'awaiting_approval' && seat.chairman) st = 'waiting';

  const who = p.label || seat.agent;
  // Spelled out in the tooltip, where there is room; abbreviated on the seat,
  // where it is the line under a name that already fills the cell.
  const model = p.model || 'default';
  const role = seat.chairman ? 'Chair' : (seat.persona_name || 'Member');
  const reasons = (seat.reasons || []).join(' · ');

  const title =
    `${who} — ${role}` +
    (seat.alias ? ` · appears to its peers as ${seat.alias}` : '') +
    ` · ${modelDetail(p.model || 'default model')}` +
    (p.effort ? ` · ${p.effort}` : '') +
    (available ? '' : ` · ${(p.command || [])[0] || 'CLI'} not found`) +
    (reasons ? `\n\nSeated because: ${reasons}` : '') +
    (seat.pinned ? '\nPinned to this seat, so the routing works around it.' : '') +
    '\n\nClick to seat a CLI here, or to change its model, effort or behaviour.';

  // Stacked in the same order as a project chair's tile - what the seat is,
  // then who is in it, then the detail - because they are the same question
  // asked twice and reading them the same way costs nothing. Laid out across
  // instead, the three competed for one line and the CLI's name was the part
  // that lost: a bench reading "Cla…, Antigr…, Co…" cannot be checked at a
  // glance, which is the only thing it is there for.
  const detail = seat.alias
    ? `${seat.alias}${p.model ? ` · ${p.model}` : ''}`
    : model;

  return (
    `<button class="member ${st}${available ? '' : ' unavailable'}` +
      `${seat.chairman ? ' is-chair' : ''}" type="button" ` +
      `data-member="${esc(seat.provider_id)}" data-agent="${esc(seat.agent)}" ` +
      `data-seat="${esc(seat.id)}" data-seat-label="${esc(role)}" ` +
      `title="${esc(title)}">` +
      `<span class="member-mark">${esc(String(who).slice(0, 2).toUpperCase())}</span>` +
      `<span class="member-body">` +
        `<span class="member-role">${esc(seatTag(role))}</span>` +
        `<span class="member-name">${esc(who)}` +
          (seat.pinned ? '<span class="member-pin" title="pinned">·</span>' : '') +
        `</span>` +
        `<span class="member-model">${esc(detail)}</span>` +
      `</span>` +
      `<span class="member-tail">` +
        memberQuotaHtml(seat.provider_id) +
        `<span class="member-dot"></span>` +
      `</span>` +
    `</button>`
  );
}

/** The three stages, as a stepper. Built from the run's own stage records so
 *  "6 of 6 answered" is a count of what happened rather than of what was
 *  planned — a council that lost a member to a timeout says so here. */
const COUNCIL_STEPS = [
  { key: 'position', label: 'Independent perspectives', states: ['deliberating'] },
  { key: 'critique', label: 'Cross-evaluating', states: ['critiquing'] },
  { key: 'chair', label: 'Synthesising the verdict',
    states: ['synthesizing', 'awaiting_approval'] },
];

function renderCouncilSteps() {
  const host = $('#council-steps');
  const run = (state.openChat && state.openChat.run) || state.run;
  const show = uiMode() === 'council' && run && !run.solo && run.seating;
  host.classList.toggle('hidden', !show);
  if (!show) { $('.project-strip', host).innerHTML = ''; return; }

  const stages = Object.values(run.stages || {});
  $('.project-strip', host).innerHTML = COUNCIL_STEPS.map(step => {
    const mine = stages.filter(s => s.kind === step.key);
    const done = mine.filter(s => s.state === 'done').length;
    const failed = mine.filter(s => s.state === 'failed').length;
    const skipped = mine.length > 0 && mine.every(s => s.state === 'skipped');
    const live = step.states.includes(run.state);

    let note;
    if (skipped) note = 'skipped — nothing to compare';
    else if (live && step.key === 'chair' && run.state === 'awaiting_approval')
      note = 'waiting for you';
    else if (live) note = `${done} of ${mine.length} answered`;
    else if (mine.length) note = `${done} of ${mine.length}` +
      (failed ? `, ${failed} failed` : '');
    else note = 'not reached';

    return (
      `<span class="strip-cell${live ? ' live' : ''}${skipped ? ' muted' : ''}">` +
        `<span class="strip-label">${esc(step.label)}</span>` +
        `<span class="strip-value">${live ? 'Working' : (done ? 'Done' : 'Waiting')}</span>` +
        `<span class="strip-note">${esc(note)}</span>` +
      `</span>`
    );
  }).join('');
}

/** Ask the server who would sit for what is in the composer.
 *
 *  Debounced, and the answer is dropped if the box has changed since — the
 *  routing is cheap but not free, and a stale bench arriving late would
 *  overwrite the right one. */
let seatingTimer = null;
let seatingSeq = 0;
function scheduleSeating() {
  if (uiMode() !== 'council' || !seatsShown()) return;
  clearTimeout(seatingTimer);
  seatingTimer = setTimeout(refreshSeating, 250);
}

async function refreshSeating() {
  if (uiMode() !== 'council' || !seatsShown()) return;
  // An empty box is a real question, not a reason to skip: it asks who the
  // standing bench is. That answer is what makes the seats clickable before a
  // word has been typed, which is the only way to pick a council by hand.
  const task = $('#task-input').value || '';
  const seq = ++seatingSeq;
  try {
    const res = await api('/api/council/route', {
      method: 'POST', body: { task },
    });
    if (seq !== seatingSeq) return;
    state.seating = res.seating;
    renderStrip();
  } catch (e) {
    // A routing preview that fails is not worth a toast: the run itself seats
    // the council again anyway, and the strip simply keeps what it had.
  }
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
  // Council's placeholder is where the routing is explained, because that is
  // where the routing is *driven from*: the bench above changes as this box is
  // typed into. Saying it on the empty strip instead put the sentence in the
  // one place it could not be acted on.
  $('#task-input').placeholder = mode === 'solo'
    ? 'Ask Theseus AI…'
    : 'Describe the task and the council seats itself…';
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

/** A figure an agent reported about itself, as a chip.
 *
 *  Only ever drawn from a number the agent actually stated. A stage that gave
 *  none renders nothing here, and the verdict card says "not stated" in words
 *  — this app has no way to measure confidence and must not appear to. */
function confidenceChip(value, label, why) {
  if (value === null || value === undefined) return '';
  const band = value >= 75 ? 'high' : value >= 45 ? 'mid' : 'low';
  const title = `${label}: ${value}% — the agent's own figure, not measured` +
    (why ? `\n\nIt said it could be wrong if: ${why}` : '');
  return (
    `<span class="conf-chip ${band}" title="${esc(title)}">` +
      `<span class="conf-label">${esc(label)}</span>` +
      `<span class="conf-value">${value}%</span>` +
    `</span>`
  );
}

function messageHtml(reply, id) {
  const who = reply.label || reply.stage || id || 'Agent';
  const st = reply.state || '';
  const dur = reply.duration ? ` · ${fmtDuration(reply.duration)}` : '';
  const word = STAGE_WORDS[st] || '';
  const chair = reply.kind === 'chair';
  // The trailer lines are stripped from the body because they are rendered as
  // chips above it. Left in, the same figure would appear twice — once as a
  // badge and once as a stray line of prose at the bottom of the card.
  const body = renderMarkdown(stripTrailer(reply.output || '')) ||
    `<p class="history-none">${esc(reply.error || `(${st || 'no output'})`)}</p>`;

  return (
    `<div class="chat-message assistant-message${chair ? ' verdict' : ''}" ` +
      `data-agent="${esc(reply.agent || id || '')}">` +
      `<div class="msg-head">` +
        `<span class="msg-mark">${esc(String(who).slice(0, 2).toUpperCase())}</span>` +
        `<span class="msg-who">${esc(who)}</span>` +
        (reply.role ? `<span class="msg-role">${esc(roleTag(reply.role))}</span>` : '') +
        // The alias is what its peers knew it as. Shown on the member's own
        // card so a critique that says "Agent B was wrong" can be followed
        // back to a CLI without reading the transcript.
        (reply.alias && !chair
          ? `<span class="msg-alias" title="How this seat appeared to its peers">` +
            `${esc(reply.alias)}</span>`
          : '') +
        (chair && reply.consensus !== null && reply.consensus !== undefined
          ? confidenceChip(reply.consensus, 'consensus', '')
          : '') +
        confidenceChip(reply.confidence, 'confidence', reply.because) +
        `<span class="msg-state${st === 'failed' ? ' failed' : ''}">` +
          `${esc(word)}${esc(dur)}</span>` +
      `</div>` +
      // `data-live` marks the one body `stage_output` may append to. Only a
      // running stage has it, so a finished message can never be scribbled on.
      `<div class="markdown"${st === 'running' ? ' data-live="1"' : ''}>${body}</div>` +
      (chair && st === 'done' && (reply.confidence === null ||
                                  reply.confidence === undefined)
        ? '<div class="turn-note">The chairman did not state a confidence. ' +
          'Nothing here fills that in.</div>'
        : '') +
    `</div>`
  );
}

/** Remove the contract trailer so it is not shown twice. Matches the parser in
 *  prompts.py; a line the parser would not have read is left alone. */
function stripTrailer(text) {
  return String(text || '')
    .replace(/^[ \t]*(?:CONFIDENCE|CONSENSUS):[ \t]*\d{1,3}[ \t]*$/gim, '')
    .replace(/^[ \t]*BECAUSE:[ \t]*.*$/gim, '')
    .trimEnd();
}

/** The stages of a turn, grouped under the stage they belong to.
 *
 *  Still messages in the conversation rather than a tabbed grid beside it —
 *  that is how every other agent in this app speaks, and a council that needed
 *  its own layout would read as a different product bolted on. The headings
 *  are the only addition, and they exist because seven messages in a row with
 *  no divisions gives no clue which are positions and which are reviews. */
const STAGE_HEADINGS = {
  position: 'Independent positions',
  critique: 'Peer critique',
  chair: 'The verdict',
};

function councilBodyHtml(run, gated) {
  const stages = stagesOf(run);
  let last = null;
  return stages.map(s => {
    const heading = (s.kind && s.kind !== last && STAGE_HEADINGS[s.kind])
      ? `<h4 class="project-head-sm council-divider">${esc(STAGE_HEADINGS[s.kind])}</h4>`
      : '';
    last = s.kind || last;
    // The gate sits immediately before the stage it is gating, which is the
    // chairman - the only stage that can write. Anchored on the last critique
    // so it reads as "here is everything the council said; now decide".
    const gateHere = gated && s.kind === 'chair';
    return (gateHere ? '<div data-slot="approval-gate"></div>' : '') +
      heading + messageHtml(s, s.id);
  }).join('') +
    // A gated run has not reached the chairman yet, so there is no chair
    // message to anchor the gate to. It goes at the end of what has been said.
    (gated && !stages.some(s => s.kind === 'chair')
      ? '<h4 class="project-head-sm council-divider">The verdict</h4>' +
        '<div data-slot="approval-gate"></div>'
      : '');
}

function renderThread() {
  const mode = uiMode();
  const main = $('.main');
  const thread = $('#thread');
  const stream = $('#stream');
  const scrolled = stream.scrollTop;

  // Rescue the movable widgets before the innerHTML below would delete them.
  park();

  // Projects owns the whole pane when it is showing: there is no message to
  // send, so the composer would be a text box wired to nothing.
  const project = mode === 'project';
  $('#project-pane').classList.toggle('hidden', !project);
  $('.composer-dock').classList.toggle('hidden', project);
  if (project) {
    $('#hero').classList.add('hidden');
    thread.innerHTML = '';
    main.classList.remove('empty');
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
      councilBodyHtml(run, gated) +
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

/* ---- Projects ----------------------------------------------------------
   Two faces and never both. The initializer is a form; the tracker is a live
   read of files on disk, which is also exactly what the agents are working
   from — so what you are watching is the thing itself rather than a mirror of
   it kept somewhere else.
   ---------------------------------------------------------------------- */

const BOARD_COLUMNS = ['backlog', 'in_progress', 'in_review', 'done'];
const COLUMN_NAMES = {
  backlog: 'Backlog', in_progress: 'In progress', in_review: 'Review', done: 'Done',
};
const PROJECT_ROLES = ['architect', 'coder', 'qa'];
const ROLE_NAMES = { architect: 'Architect', coder: 'Developer', qa: 'QA' };

/** What the slider is promising, in words. The number on its own says nothing
 *  about the thing people actually want to know: will it touch my repo beyond
 *  what I asked for? */
const INNOVATION_NOTES = [
  'Builds exactly what you asked for, then stops. The right setting for a ' +
    'repository you care about.',
  'One round: once the board is clear and the build is green, the architect ' +
    'proposes a few additions and the council builds them.',
  'Two rounds of proposals after the goal is met.',
  'Three rounds. Each one builds on what the last added.',
  'Four rounds. Expect work you did not ask for and will have to read.',
  'Five rounds. The goal becomes a starting point rather than a target.',
];

/** Whether a project is on screen at all — running, paused or finished. */
function hasProject() {
  return !!(state.project && state.project.id);
}

function renderProject() {
  if (uiMode() !== 'project') return;
  const live = hasProject();
  $('#project-setup').classList.toggle('hidden', live);
  $('#project-live').classList.toggle('hidden', !live);
  if (live) renderProjectLive();
  else renderProjectSetup();
}

function renderProjectSetup() {
  const folder = workspacePath();
  $('#project-folder-label').textContent = folder ? shortPath(folder) : 'Scratch workspace';
  $('#project-folder').title = folder || state.scratchWorkspace || '';
  $('#project-folder-hint').textContent = folder
    ? (workspaceIsRepo()
      ? 'A git repository, so a snapshot is taken before the first write and ' +
        'you can read the whole build as a diff afterwards.'
      : 'Not a git repository, so nothing is snapshotted and there is nothing ' +
        'to undo the build with. The agents will still work here.')
    : 'No folder chosen, so it builds in the scratch workspace — a contained ' +
      'directory of the app\'s own. Pick a folder to build somewhere you keep.';

  $('#project-matrix').innerHTML = PROJECT_ROLES.map(chairHtml).join('');
  renderInnovation();

  // Resuming is offered only when the server found a build on disk. A project
  // left half-finished by a closed window is the one case where the empty form
  // is the wrong answer.
  const resumable = state.projectResumable && !hasProject();
  $('#project-resume-found').classList.toggle('hidden', !resumable);

  const missing = (state.projectRoles || []).filter(r => !r.available);
  $('#project-start').disabled = missing.length > 0;
  $('#project-start-hint').textContent = missing.length
    ? `${missing.map(r => ROLE_NAMES[r.id] || r.id).join(' and ')} ` +
      `${missing.length === 1 ? 'has' : 'have'} no CLI installed. ` +
      `A project runs unattended, so it will not start with a seat it cannot fill.`
    : '';
}

/** The innovation slider: how much the council may invent once the goal is met.
 *
 *  Seeded from the saved default exactly once, and only once the server has
 *  actually said what that default is — the pane renders before
 *  `/api/project` answers, so seeding from a placeholder would pin the slider
 *  to a number nobody chose and then mark itself done, and the real setting
 *  would never arrive.
 *
 *  A touch by the operator settles it for good. Otherwise a drag to zero made
 *  while that request was still in flight would be overwritten by the default
 *  landing a moment later — and this is the one control where the difference
 *  between 0 and 2 is "does it touch my repository beyond what I asked for". */
function renderInnovation() {
  const slider = $('#project-innovation');
  const saved = (state.projectSettings || {}).innovation_rounds;
  if (!slider.dataset.settled && saved !== undefined) {
    slider.value = String(saved);
    slider.dataset.settled = '1';
  }
  const n = Math.max(0, Math.min(5, parseInt(slider.value, 10) || 0));
  $('#project-innovation-out').textContent =
    n === 0 ? 'off' : `${n} round${n === 1 ? '' : 's'}`;
  $('#project-innovation-hint').textContent = INNOVATION_NOTES[n];
}

/** One chair in the agent matrix. The same provider object the council strip
 *  renders, so clicking it opens the same menu and a change here is a change
 *  everywhere. */
function chairHtml(role) {
  const p = ((state.config || {}).providers || {})[role] || {};
  const info = (state.projectRoles || []).find(r => r.id === role);
  const available = !info || info.available;
  const agent = state.agents.find(a => a.id === agentOf(p));
  const agentLabel = agent && (agent.command || []).length ? agent.label : 'custom command';
  const model = p.model || 'default model';
  const running = hasProject() && state.projectRunning &&
    state.project.active_agent === role;

  const title =
    `${ROLE_NAMES[role]} — ${agentLabel} · ${modelDetail(model)}` +
    (p.effort ? ` · ${p.effort}` : '') +
    (available ? '' : ` · ${(p.command || [])[0] || 'CLI'} not found`) +
    '\nClick to change CLI, model or effort.';

  return (
    `<button class="chair${available ? '' : ' unavailable'}${running ? ' active' : ''}" ` +
      `type="button" data-chair="${role}" data-role="${role}" title="${esc(title)}">` +
      `<span class="chair-mark">${esc(ROLE_NAMES[role].slice(0, 2).toUpperCase())}</span>` +
      `<span class="chair-body">` +
        `<span class="chair-role">${esc(ROLE_NAMES[role])}</span>` +
        `<span class="chair-agent">${esc(available ? agentLabel : agentLabel + ' — missing')}</span>` +
        `<span class="chair-model">${esc(model)}</span>` +
      `</span>` +
    `</button>`
  );
}

function renderProjectLive() {
  const p = state.project;
  const running = state.projectRunning;
  const done = !!p.done;

  $('#project-goal-line').textContent = p.goal || '(no goal recorded)';
  $('#project-goal-line').title = p.goal || '';
  $('#project-where').textContent =
    `${shortPath(p.workspace)} · ${p.steps_used} turn${p.steps_used === 1 ? '' : 's'}` +
    (((p.tooling || {}).stack || []).length ? ` · ${p.tooling.stack.join(', ')}` : '') +
    (p.innovation_rounds
      ? ` · ${p.innovation_rounds} innovation round${p.innovation_rounds === 1 ? '' : 's'} left`
      : '');
  $('#project-where').title = p.workspace;

  // Controls. Pause and Resume are the same button's two states rather than
  // two live buttons, so there is never a pair where only one does anything.
  $('#project-pause').classList.toggle('hidden', !running || p.paused);
  $('#project-resume').classList.toggle('hidden', !(running && p.paused));
  $('#project-handoff').classList.toggle('hidden', !running);
  $('#project-stop').classList.toggle('hidden', !running);
  $('#project-new').classList.toggle('hidden', running);

  renderStatusStrip(p, running, done);
  renderProjectBanner(p, running, done);
  renderBoard(p);

  // Newest first: a run of thirty turns is read from the end.
  $('#project-steps').innerHTML = (p.steps || []).slice().reverse().map(stepHtml).join('')
    || '<li class="step"><span class="step-why">Nothing has run yet.</span></li>';

  // The last build output, shown only when there is a failure to read. A
  // passing build has nothing to say and an empty block invites a click.
  const hasLog = !!(p.last_build_log || '').trim();
  $('#project-build-block').classList.toggle('hidden', !hasLog);
  $('#project-build-log').textContent = p.last_build_log || '';

  $('#project-board-json').textContent = p.board_json || '';

  // Adopt the shared console. `renderThread` parks it on every render, so this
  // has to claim it back each time rather than only once.
  const slot = $('#project-console-slot');
  const console_ = $('#console-block');
  if (console_ && console_.parentNode !== slot) slot.appendChild(console_);

  loadProjectCritique();
}

/** The critique log, which is the one project file the engine does not carry
 *  on every event: it is append-only and grows all run, so it is fetched when
 *  the tab is looked at instead of pushed down the stream. */
async function loadProjectCritique() {
  const block = $('#project-critique-block');
  if (!block.open) return;
  try {
    const data = await api(
      `/api/project/file?name=critique&workspace=${encodeURIComponent(
        (state.project || {}).workspace || workspacePath())}`
    );
    $('#project-critique').textContent = data.text || '(nothing logged yet)';
  } catch {
    $('#project-critique').textContent = '(could not read the log)';
  }
}

/** Build health, who is working, and why. There is no phase number to show
 *  any more — the board decides each turn — so what replaces it is the state
 *  the board actually dispatches on. */
function renderStatusStrip(p, running, done) {
  const health = p.build_health || 'UNKNOWN';
  const healthClass =
    health === 'PASSING' ? 'ok' : health === 'FAILING' ? 'bad' : 'unknown';
  const healthNote = {
    PASSING: 'built and tested since the last edit',
    FAILING: 'the developer has the trace',
    UNKNOWN: 'changed since anyone last ran it',
  }[health] || '';

  const last = (p.steps || [])[(p.steps || []).length - 1];
  const who = done
    ? ''
    : p.paused
      ? 'Paused'
      : running
        ? `${ROLE_NAMES[p.active_agent] || p.active_agent} working`
        : 'Idle';
  const why = !done && running && last ? last.trigger || '' : '';

  $('#project-status-strip').innerHTML =
    `<span class="strip-cell strip-health ${healthClass}">` +
      `<span class="strip-label">Build</span>` +
      `<span class="strip-value">${esc(health.toLowerCase())}</span>` +
      `<span class="strip-note">${esc(healthNote)}</span>` +
    `</span>` +
    (who
      ? `<span class="strip-cell${running && !p.paused ? ' live' : ''}">` +
          `<span class="strip-label">Now</span>` +
          `<span class="strip-value">${esc(who)}</span>` +
          `<span class="strip-note">${esc(why ? `because: ${why}` : '')}</span>` +
        `</span>`
      : '') +
    `<span class="strip-cell">` +
      `<span class="strip-label">Status</span>` +
      `<span class="strip-value">${esc((p.status || '').toLowerCase())}</span>` +
      `<span class="strip-note">${esc(cardTally(p))}</span>` +
    `</span>`;
}

/** "3 of 7 cards done", counted off the columns - which is what the payload
 *  actually carries. The engine holds cards flat and publishes them grouped. */
function cardTally(p) {
  const counts = p.counts || {};
  const total = BOARD_COLUMNS.reduce((n, c) => n + (counts[c] || 0), 0);
  if (!total) return 'no cards yet';
  return `${counts.done || 0} of ${total} card${total === 1 ? '' : 's'} done`;
}

/** The board itself, straight from BOARD.json. */
function renderBoard(p) {
  const counts = p.counts || {};
  const labels = p.column_labels || COLUMN_NAMES;
  const cards = p.columns || {};

  $('#project-board').innerHTML = BOARD_COLUMNS.map(name => {
    const list = cards[name] || [];
    return (
      `<section class="board-col" data-column="${name}">` +
        `<h4 class="board-col-head">` +
          `${esc(labels[name] || name)}` +
          `<span class="count">${counts[name] || 0}</span>` +
        `</h4>` +
        `<ul class="board-cards">` +
          (list.length
            ? list.map(c => cardHtml(c, name)).join('')
            : `<li class="board-empty">&mdash;</li>`) +
        `</ul>` +
      `</section>`
    );
  }).join('');
}

function cardHtml(c, column) {
  const bug = c.kind === 'bug';
  const invented = c.origin === 'innovation';
  const title = c.title || c.id;
  const tip = [c.detail, c.note && `Note: ${c.note}`].filter(Boolean).join('\n\n');

  return (
    `<li class="board-card${bug ? ' bug' : ''}${column === 'done' ? ' done' : ''}"` +
      `${tip ? ` title="${esc(tip)}"` : ''}>` +
      `<div class="board-card-top">` +
        `<span class="board-card-id">${esc(c.id)}</span>` +
        (bug ? `<span class="board-tag bug">bug</span>` : '') +
        (invented ? `<span class="board-tag idea" title="Proposed by the ` +
          `council, not asked for">idea</span>` : '') +
      `</div>` +
      `<div class="board-card-title">${esc(title)}</div>` +
      (c.note && column === 'backlog'
        ? `<div class="board-card-note">${esc(c.note)}</div>` : '') +
    `</li>`
  );
}

function renderProjectBanner(p, running, done) {
  const banner = $('#project-banner');
  let kind = '';
  let text = '';

  if (p.status === 'COMPLETED') {
    kind = 'ok';
    text = `${p.note || 'Finished.'} Everything is in ${p.workspace} — read ` +
           `the diff first, then .theseus/CRITIQUE.log for what was left open.`;
  } else if (p.status === 'FAILED') {
    kind = 'error';
    text = p.error || 'The project stopped.';
  } else if (p.paused) {
    kind = 'warn';
    text = 'Paused. The agent that was running finished its turn, so the tree ' +
           'is consistent — nothing was interrupted mid-write.';
  } else if (!running) {
    kind = 'warn';
    text = 'Not running. Everything so far is on disk; starting it again picks ' +
           'up from the board.';
  } else if (p.continuation_needed) {
    kind = 'warn';
    text = 'An agent ran out of context or quota. The turn was handed to ' +
           'another one, which starts from the board rather than from a ' +
           'conversation it never saw.';
  } else if (p.build_health === 'FAILING') {
    kind = 'warn';
    text = `Build failing — the developer has the trace and is on attempt ` +
           `${(p.fix_attempts || 0) + 1}.`;
  } else if (p.status === 'AUDITING') {
    kind = '';
    text = 'Reading the workspace. This turn runs read-only — it has no write ' +
           'permission at all, so nothing here can change yet.';
  } else if (p.status === 'INNOVATING') {
    kind = 'warn';
    text = 'The goal is met and the build is green. The architect is now ' +
           'proposing work you did not ask for; the slider on the initializer ' +
           'is what bounds this.';
  } else {
    kind = '';
    text = 'Running unattended. Every turn after the audit carries its CLI\'s ' +
           'auto-approve flags, so files are changing without confirmation.';
  }

  banner.className = `project-banner${kind ? ' ' + kind : ''}`;
  banner.classList.toggle('hidden', !text);
  banner.textContent = text.trim();
}

function stepHtml(s) {
  const files = (s.files_modified || []).slice(0, 6);
  return (
    `<li class="step ${esc(s.state)}">` +
      `<div class="step-head">` +
        `<span class="step-who">${esc(s.role_label || s.role)}</span>` +
        `<span>${esc(s.heading)}</span>` +
        (s.read_only
          ? `<span class="step-tag" title="Invoked with no write permission">read-only</span>`
          : '') +
        `<span class="step-meta">` +
          `${esc(s.label)}${s.duration ? ' · ' + esc(fmtDuration(s.duration)) : ''}` +
        `</span>` +
      `</div>` +
      (s.trigger ? `<div class="step-trigger">chosen because: ${esc(s.trigger)}</div>` : '') +
      (s.handoff_from
        ? `<div class="step-handoff">Handed over from the ` +
          `${esc(ROLE_NAMES[s.handoff_from] || s.handoff_from)} chair.</div>`
        : '') +
      (s.reasoning ? `<div class="step-why">${esc(s.reasoning)}</div>` : '') +
      (s.error ? `<div class="step-err">${esc(s.error)}</div>` : '') +
      (files.length
        ? `<div class="step-files" title="${esc((s.files_modified || []).join('\n'))}">` +
          `${esc(files.join(', '))}` +
          `${s.files_modified.length > files.length
            ? ` +${s.files_modified.length - files.length} more` : ''}</div>`
        : '') +
    `</li>`
  );
}

/** Start, or pick up, an autonomous build.
 *
 *  The confirmation is not ceremony. Every other surface in this app either
 *  asks before it writes or has to be armed with Zero-Touch first; a project
 *  cannot — it writes a codebase, so it carries the auto-approve flags by
 *  construction — and pressing this button *is* the grant. Naming the folder
 *  in the prompt is the part that matters: it is the one thing that turns a
 *  misclick into a question. */
async function startProject(resume) {
  const goal = $('#project-goal').value.trim();
  const folder = workspacePath();
  const innovation = Math.max(0, Math.min(5,
    parseInt($('#project-innovation').value, 10) || 0));
  const where = folder || `the scratch workspace (${state.scratchWorkspace || 'app folder'})`;

  if (!resume && !goal) {
    toast('Describe what you want built.', 'warn');
    $('#project-goal').focus();
    return;
  }
  if (!confirm(
    `Start an autonomous build in:\n\n${where}\n\n` +
    `Three agents will create, edit and delete files there without asking, ` +
    `for as long as it takes — no approval gate.` +
    (innovation
      ? `\n\nInnovation is set to ${innovation} round${innovation === 1 ? '' : 's'}, ` +
        `so once your goal is met it will also design and build things you ` +
        `did not ask for. Set the slider to zero to stop at the goal.`
      : '') +
    (workspaceIsRepo()
      ? '\n\nA git snapshot is taken first, so the whole build can be undone.'
      : '\n\nThis is not a git repository, so nothing is snapshotted and ' +
        'there is no way to undo it.')
  )) return;

  try {
    await api('/api/project/start', {
      method: 'POST',
      body: { goal, workspace: folder, resume: !!resume, innovation },
    });
    $('#project-goal').value = '';
    await loadProject(false);
    toast(resume ? 'Project resumed.' : 'Project started.', 'ok');
  } catch (err) {
    toast(err.message, 'error', 9000);
  }
}

/** Force the next step onto a chair the phase cycle would not have chosen.
 *  For the failure the engine cannot see: an agent answering, exiting zero,
 *  and going in circles. */
function openHandoffMenu(anchor) {
  closeModelMenu();
  const active = (state.project || {}).active_agent;
  const providers = (state.config || {}).providers || {};

  const menu = document.createElement('div');
  menu.className = 'model-menu';
  menu.innerHTML =
    `<div class="model-menu-head">Run the next step on</div>` +
    PROJECT_ROLES.map(role =>
      `<button class="model-opt${role === active ? ' active' : ''}" data-value="${role}">` +
        `<span class="model-opt-name">${esc(ROLE_NAMES[role])}</span>` +
        `<span class="model-opt-note">${esc((providers[role] || {}).label || role)}</span>` +
      `</button>`
    ).join('') +
    `<div class="model-menu-source">` +
      `Takes effect on the next step. The one running now finishes first.` +
    `</div>`;
  document.body.appendChild(menu);
  positionModelMenu(menu, anchor);

  menu.addEventListener('click', async (e) => {
    const opt = e.target.closest('.model-opt');
    if (!opt) return;
    closeModelMenu();
    try {
      await api('/api/project/handoff', {
        method: 'POST', body: { role: opt.dataset.value },
      });
      toast(`Next step goes to the ${ROLE_NAMES[opt.dataset.value]} chair.`, 'ok');
    } catch (err) { toast(err.message, 'error'); }
  });

  setTimeout(() => {
    document.addEventListener('click', onDocClickCloseModel, { once: true });
  }, 0);
}

/** Re-read the tab from the server. Called on open, on a folder change, and
 *  whenever a project event says the shape of it moved. */
async function loadProject(quiet = true) {
  try {
    const data = await api(
      `/api/project?workspace=${encodeURIComponent(workspacePath())}`
    );
    state.project = data.project;
    state.projectRunning = !!data.running;
    state.projectResumable = !!data.resumable;
    state.projectRoles = data.roles || [];
    if (data.settings) state.projectSettings = data.settings;
    renderProject();
  } catch (err) {
    if (!quiet) toast(err.message, 'error');
  }
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
  renderCouncilSteps();
  renderComposerChips();
  renderWorkspace();
  renderToggles();
  renderThread();
  renderProject();
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

/** Everything about one seat, from one click on it. Each row opens the menu
 *  that already owns that setting rather than reimplementing it here: four
 *  settings, four existing menus, no fifth copy of the model list to drift out
 *  of step with the other one. */
function openMemberMenu(anchor, providerId, { note = '', seat = '', seatLabel = '' } = {}) {
  closeModelMenu();
  const council = (state.config || {}).council || {};
  const provider = ((state.config || {}).providers || {})[providerId] || {};
  const agent = state.agents.find(a => a.id === agentOf(provider));
  const agentLabel = agent && (agent.command || []).length ? agent.label : 'custom command';
  const hasEffort = (provider.effort_args || []).length > 0;

  // A council seat is not the same object as a project chair, and the first
  // two rows differ because of it:
  //
  //  - "Seat" instead of "CLI". A council provider *is* its agent - the id is
  //    `council_codex` - so rewriting its command to claude's would leave the
  //    router seating a codex that runs claude. Which CLI sits here is a pin,
  //    which the router honours and works around.
  //  - "Behaviour" instead of "Role". A seat's lens comes from the persona the
  //    router assigns it, held in `council.personas` and read straight off the
  //    seating - the provider carries no behaviour at all. Wording is still
  //    the Roles catalogue's.
  //
  // A project chair keeps neither: what it is told to do comes from the board.
  const chair = seat === 'chair';
  const pinned = (council.pins || {})[seat] || '';
  const persona = (council.personas || {})[seat] || '';
  const personaRole = (state.roles || []).find(r => r.id === persona);

  const rows = seat ? [
    ['seat', 'Seat', pinned ? `${agentLabel} — pinned` : `${agentLabel} — routed`],
    ['model', 'Model', provider.model ? modelDetail(provider.model) : 'the CLI’s default'],
    ...(hasEffort ? [['effort', 'Effort', provider.effort || 'the CLI’s default']] : []),
    ...(chair ? [] : [['persona', 'Behaviour',
      personaRole ? (personaRole.name || persona) : 'chosen per run']]),
  ] : [
    ['agent', 'CLI', agentLabel],
    ['model', 'Model', provider.model ? modelDetail(provider.model) : 'the CLI’s default'],
    ...(hasEffort ? [['effort', 'Effort', provider.effort || 'the CLI’s default']] : []),
  ];

  const head = seat
    ? `${seatLabel || (chair ? 'Chair' : 'Seat')} — ${provider.label || agentLabel}`
    : (provider.label || providerId);

  const menu = document.createElement('div');
  menu.className = 'model-menu';
  menu.innerHTML =
    `<div class="model-menu-head">${esc(head)}</div>` +
    rows.map(([key, label, value]) =>
      `<button class="model-opt" data-open="${key}">` +
        `<span class="model-opt-name">${esc(label)}</span>` +
        `<span class="model-opt-note">${esc(value)}</span>` +
      `</button>`
    ).join('') +
    `<div class="model-menu-source">` +
      esc(note || (seat
        ? (chair
          ? 'The chair is the only seat that writes. Model and effort apply ' +
            'wherever this CLI sits.'
          : 'Pin a seat and the routing works around it. Model and effort ' +
            'apply wherever this CLI sits.')
        : 'Applies to this stage only.')) +
    `</div>`;
  document.body.appendChild(menu);
  positionModelMenu(menu, anchor);

  menu.addEventListener('click', (e) => {
    const opt = e.target.closest('[data-open]');
    if (!opt) return;
    closeModelMenu();
    if (opt.dataset.open === 'seat') { openSeatMenu(anchor, seat); return; }
    if (opt.dataset.open === 'persona') { openPersonaMenu(anchor, seat); return; }
    const open = {
      agent: openAgentMenu, model: openModelMenu, effort: openEffortMenu,
    }[opt.dataset.open];
    if (open) open(anchor, providerId);
  });

  setTimeout(() => {
    document.addEventListener('click', onDocClickCloseModel, { once: true });
  }, 0);
}

/** Write one seat's pin or persona and re-seat.
 *
 *  "Auto" is stored as an empty string rather than by dropping the key. The
 *  config is deep-merged on save — a key left out keeps whatever was there —
 *  so unpinning by omission would silently do nothing at all. */
async function patchSeat(field, seatId, value) {
  if (!seatId) return;
  await patchConfig({ council: { [field]: { [seatId]: value || '' } } });
  await refreshSeating();
}

/** Which CLI holds a seat: the manual half of the routing.
 *
 *  This is the pin, not a command swap. Pinning is honoured whether or not the
 *  routing is on, and the router fills the seats around it — so a bench can be
 *  set by hand one seat at a time without switching the routing off wholesale.
 *  Settings → Council still has the switch for freezing all of it. */
function openSeatMenu(anchor, seatId) {
  closeModelMenu();
  const council = (state.config || {}).council || {};
  const current = (council.pins || {})[seatId] || '';
  const manual = String(council.routing || 'auto') === 'manual';
  const agents = (state.agents || []).filter(a => (a.command || []).length);

  const menu = document.createElement('div');
  menu.className = 'model-menu';
  menu.innerHTML =
    `<div class="model-menu-head">` +
      `${esc(seatId === 'chair' ? 'Chair' : `Seat ${seatId.replace('seat', '')}`)}` +
    `</div>` +
    `<button class="model-opt${current ? '' : ' active'}" data-value="">` +
      `<span class="model-opt-name">Auto</span>` +
      `<span class="model-opt-note">routed from the prompt</span>` +
    `</button>` +
    agents.map(a => {
      const info = state.providers.find(x => x.id === `council_${a.id}`);
      const available = !info || info.available;
      return (
        `<button class="model-opt${a.id === current ? ' active' : ''}" ` +
          `data-value="${esc(a.id)}">` +
          `<span class="model-opt-name">${esc(a.label)}</span>` +
          `<span class="model-opt-note">` +
            `${esc(available ? a.command[0] : `${a.command[0]} — not installed`)}` +
          `</span>` +
        `</button>`
      );
    }).join('') +
    `<div class="model-menu-source">` +
      (manual
        ? 'The routing is off, so every seat is whatever you pin here.'
        : 'A pinned seat is fixed; the rest are still routed around it.') +
    `</div>`;
  document.body.appendChild(menu);
  positionModelMenu(menu, anchor);

  menu.addEventListener('click', (e) => {
    const opt = e.target.closest('.model-opt');
    if (!opt) return;
    closeModelMenu();
    if (opt.dataset.value === current) return;
    patchSeat('pins', seatId, opt.dataset.value);
  });

  setTimeout(() => {
    document.addEventListener('click', onDocClickCloseModel, { once: true });
  }, 0);
}

/** What a seat is told to be. The list is the Roles catalogue, so a behaviour
 *  written in Settings → Roles is selectable here the moment it is saved —
 *  the wording lives in one place and this only chooses between them.
 *
 *  The chairman is not offered: it is what the third stage *is*, not a lens a
 *  member can wear. A behaviour that expects to write says so, because a
 *  member seat is read-only whatever it is told — the same mismatch Settings
 *  warns about, reported before it is chosen rather than after. */
function openPersonaMenu(anchor, seatId) {
  closeModelMenu();
  const council = (state.config || {}).council || {};
  const current = (council.personas || {})[seatId] || '';

  const menu = document.createElement('div');
  menu.className = 'model-menu';
  menu.innerHTML =
    `<div class="model-menu-head">` +
      `Seat ${esc(seatId.replace('seat', ''))} behaviour` +
    `</div>` +
    `<button class="model-opt${current ? '' : ' active'}" data-value="">` +
      `<span class="model-opt-name">Auto</span>` +
      `<span class="model-opt-note">a lens the task calls for</span>` +
    `</button>` +
    (state.roles || []).filter(r => r.id !== 'chairman').map(r =>
      `<button class="model-opt${r.id === current ? ' active' : ''}" ` +
        `data-value="${esc(r.id)}" title="${esc(r.summary || '')}">` +
        `<span class="model-opt-name">${esc(r.name || r.id)}</span>` +
        `<span class="model-opt-note">` +
          `${esc(r.writes ? 'expects to write — this seat cannot' : (r.summary || 'read-only'))}` +
        `</span>` +
      `</button>`
    ).join('') +
    `<div class="model-menu-source">` +
      'Wording is editable in Settings → Roles.' +
    `</div>`;
  document.body.appendChild(menu);
  positionModelMenu(menu, anchor);

  menu.addEventListener('click', (e) => {
    const opt = e.target.closest('.model-opt');
    if (!opt) return;
    closeModelMenu();
    if (opt.dataset.value === current) return;
    patchSeat('personas', seatId, opt.dataset.value);
  });

  setTimeout(() => {
    document.addEventListener('click', onDocClickCloseModel, { once: true });
  }, 0);
}

// The per-provider Role menu is gone with the fixed Stage 1 / Stage 2 pair
// it belonged to. A council seat's behaviour is its persona, chosen on the
// seat itself (`openPersonaMenu`) and stored against the seat rather than
// against the CLI - the same CLI can sit in two seats with two lenses. The
// wording is still edited in one place: Settings -> Roles.

/** The gear beside the composer, or at the head of Project: the options that
 *  get changed per mode.
 *  The rest are a click further on, in Settings, because they are decisions
 *  about the project rather than about the message being sent. */
function openGearMenu(anchor) {
  closeModelMenu();
  const c = state.config || {};
  // Both apply in both modes. Zero-Touch means the same thing either way —
  // the CLI gets its auto-approve flags — it is just that Council can also be
  // granted that at the gate, and Chat, having no gate, cannot.
  const mode = uiMode();
  const project = mode === 'project';
  const chat = mode === 'solo';
  const cavemanMode = project ? 'project' : (chat ? 'chat' : 'council');
  const efficiencyMode = project ? 'project' : (chat ? 'chat' : 'council');

  const row = (key, label, on, danger) =>
    `<button class="menu-toggle${on ? ' on' : ''}${danger ? ' danger' : ''}" ` +
      `data-toggle="${key}">` +
      `<span class="menu-box">✓</span>${esc(label)}</button>`;

  const menu = document.createElement('div');
  menu.className = 'model-menu gear-menu';
  menu.innerHTML =
    (project ? '' : row('zero_touch', 'Zero-Touch mode', !!c.zero_touch, true)) +
    row('caveman', 'Caveman mode', cavemanOn(cavemanMode), false) +
    row('efficiency', 'Efficiency mode', efficiencyOn(efficiencyMode), false) +
    (project ? '' :
      row('pull_request_mode', 'Pull request mode', !!c.pull_request_mode, false)) +
    // Council only: Chat has one agent and no bench to show. It is a display
    // choice rather than a permission, so unlike the two above it is applied
    // here directly - there is no confirmation for it to bypass.
    (chat || project ? '' :
      row('show_seats', 'Show the council seats', seatsShown(), false)) +
    `<hr>` +
    `<button class="model-opt" data-open="settings">` +
      `<span class="model-opt-name">More…</span>` +
      `<span class="model-opt-note">Settings</span>` +
    `</button>` +
    `<div class="model-menu-source">` +
      (project
        ? 'Applies to the Architect, Developer and QA prompts in Projects.'
        : chat
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
      openSettings(project ? 'project' : 'stages');
      return;
    }
    const btn = e.target.closest('[data-toggle]');
    if (!btn || btn.disabled) return;
    closeModelMenu();
    if (btn.dataset.toggle === 'caveman') {
      patchConfig({ caveman: { [cavemanMode]: !cavemanOn(cavemanMode) } });
      return;
    }
    if (btn.dataset.toggle === 'efficiency') {
      patchConfig({
        efficiency: {
          [efficiencyMode]: !efficiencyOn(efficiencyMode),
        },
      });
      return;
    }
    if (btn.dataset.toggle === 'show_seats') {
      const show = !seatsShown();
      // `patchConfig` re-renders, so the strip appears or goes on its own.
      // The one thing it cannot do is fill a bench that was never asked for:
      // switched off, the routing preview is skipped.
      patchConfig({ council: { show_seats: show } })
        .then(() => { if (show && !activeSeating()) refreshSeating(); });
      return;
    }
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
    // The Projects tab reports on the folder it is looking at, so a different
    // folder is a different project - or none.
    loadProject();
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
    loadProject();
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

async function openSettings(tab = 'stages') {
  await refreshDoctor(true);
  renderSettings();
  renderRoleList();
  renderHistoryCounts();
  switchSettingsTab(tab);
  openModal('settings');
}

/** Writing modes are scoped to 'council', 'chat' or 'project'. Each mode gets
 *  its own switches because what brevity costs differs — a Chat answer is read
 *  and discarded, a council deliberation is the record of a change. */
function cavemanOn(mode) {
  return !!((state.config || {}).caveman || {})[mode];
}

/** Concise normal prose, independently selectable for every app mode. */
function efficiencyOn(mode) {
  return !!((state.config || {}).efficiency || {})[mode];
}

function renderSettings() {
  const conf = state.config || {};
  const providers = conf.providers || {};
  const host = $('#provider-forms');

  // Every chair in the app, configured independently. `kind` decides which
  // half of the form each one gets, because what a chair is *told to do*
  // arrives differently in each of the three:
  //
  //   seat     a council CLI. It is told what to be by the persona its seat
  //            carries, which is routed per run and pinned in the Council
  //            panel - so there is no role box here. What is here is the CLI
  //            itself: its command, model, effort and ceiling, wherever it
  //            ends up sitting.
  //   chat     the assistant gets only a behaviour the operator typed, and
  //            picks its agent from its own card rather than from this panel;
  //   project  a project chair gets neither - its instruction comes from the
  //            phase it is in, so a role box here would be a setting nothing
  //            reads.
  //
  // There is no Stage 1 / Stage 2 pair any more. `drafter` and `polisher`
  // survive in the config so archived transcripts still render, but they are
  // not chairs anybody sits in, and a form for them would be one that decided
  // nothing about the next run.
  const FORMS = [
    ...(state.agents || []).filter(a => (a.command || []).length).map(a => ({
      id: `council_${a.id}`, num: 'Council', kind: 'seat', name: a.label,
    })),
    { id: 'solo', num: 'Chat', kind: 'chat' },
    { id: 'architect', num: 'Project', kind: 'project', name: 'Architect' },
    { id: 'coder', num: 'Project', kind: 'project', name: 'Developer' },
    { id: 'qa', num: 'Project', kind: 'project', name: 'QA' },
  ];

  host.innerHTML = FORMS.map(({ id, num, kind, name }) => {
    const p = providers[id] || {};
    const info = state.providers.find(x => x.id === id);
    const probeHtml = info
      ? `<span class="probe ${info.available ? 'ok' : 'miss'}">` +
        `${info.available ? 'found' : 'not found'}</span>`
      : '';
    return (
      `<div class="provider-form" data-provider="${id}" data-kind="${kind}">` +
        `<h4><span class="stage-num">${esc(num)}</span> ` +
          `${esc(name || p.label || 'Assistant')} ${probeHtml}</h4>` +
        (kind === 'project'
          ? `<p class="settings-note">` +
              `Its agent, model and reasoning depth are set in the Projects ` +
              `tab's matrix. There is no role to choose: what this chair is ` +
              `told to do comes from the phase it is in.</p>`
          : '') +
        (kind === 'seat'
          ? `<p class="settings-note">` +
              `How this CLI runs whenever the council seats it. Which seat it ` +
              `holds, and what it is told to be, are decided per run &mdash; ` +
              `pin either on the <b>Council</b> tab or on the bench above the ` +
              `composer. Members are read-only; only the chair writes.</p>`
          : '') +
        // Nobody picks an agent here. Chat's is the chip on its card, a
        // project chair's is its tile in the matrix, and a council seat's card
        // *is* one CLI — rewriting `council_codex` to run claude would leave
        // the router seating a codex that is not one. Two places to pick it
        // would be two apparent sources of truth, and the modal is the slower.
        `<div class="field">` +
          `<label>Display name ` +
            (kind === 'seat'
              ? `<span class="field-hint">— what this CLI is called on the ` +
                `bench</span>`
              : `<span class="field-hint">— the agent itself is picked on ` +
                `${kind === 'project' ? "the Projects tab" : 'the Assistant card'}` +
                `</span>`) +
            `</label>` +
          `<input type="text" data-field="label" value="${esc(p.label || '')}">` +
        `</div>` +
        // No role, no template and no fallback: blank here means blank, so a
        // new Solo conversation reaches the CLI as the message alone. A
        // council seat and a project chair get neither box — see FORMS.
        (kind === 'chat'
          ? `<div class="field">` +
              `<label>Behaviour ` +
                `<span class="field-hint">— optional; blank sends your message ` +
                `on its own</span></label>` +
              `<textarea rows="5" class="role-system" data-field="behavior" ` +
                `placeholder="e.g. Answer briefly, and always show the code.">` +
                `${esc(p.behavior || '')}</textarea>` +
            `</div>`
          : '') +
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
            // Chat and council seats are both invoked read-only; a project
            // chair always writes, so showing the field there would offer a
            // setting nothing sends.
            (kind === 'chat' || kind === 'seat'
              ? `<div class="field">` +
                `<label>Read-only arguments ` +
                  (kind === 'seat'
                    ? `<span class="field-hint">— added to every deliberating ` +
                      `and critiquing seat. Only the chair is invoked without ` +
                      `them, and only once approved</span>`
                    : `<span class="field-hint">— added whenever Chat is ` +
                      `read-only, which is any run with Zero-Touch off. With ` +
                      `it on, the auto-approve arguments are sent instead</span>`) +
                  `</label>` +
                `<textarea rows="2" data-field="read_only_args">` +
                  `${esc((p.read_only_args || []).join('\n'))}</textarea>` +
              `</div>`
              : '') +
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

  $('#house-rules').value = conf.house_rules || '';
  $('#caveman-project').checked = cavemanOn('project');
  $('#efficiency-project').checked = efficiencyOn('project');
  $('#display-name').value = conf.display_name || '';
  $('#port-input').value = conf.port || 8760;
  $('#open-browser').checked = conf.open_browser !== false;

  const proj = conf.project || {};
  $('#project-max-steps').value = proj.max_steps ?? 40;
  $('#project-max-fixes').value = proj.max_fix_attempts ?? 3;
  $('#project-innovation-default').value = proj.innovation_rounds ?? 2;

  renderCouncilSettings();
}

/* ---- Council settings --------------------------------------------------- */

/** What each strictness step actually changes, in words. The number alone
 *  says nothing about the thing worth knowing: how hard will they be on each
 *  other, and will the chairman tell me when they disagreed? */
const STRICTNESS_NOTES = [
  ['Collegial', 'Members raise only what would change the outcome, and the ' +
    'chair prefers whatever they converged on.'],
  ['Measured', 'Adds anything that would mislead a reader of the final answer.'],
  ['Balanced', 'Every factual claim about the code gets checked. The chair ' +
    'says plainly when the council was split.'],
  ['Exacting', 'Peers are assumed wrong until verified against the code, and ' +
    'the chair implements the conservative option where they were not.'],
  ['Adversarial', 'Members actively try to construct the input each position ' +
    'fails on. Unrefuted is treated as unproven, not correct.'],
  ['Hostile', 'The default verdict is that the peer is wrong. Nothing enters ' +
    'the verdict that the chair did not verify itself.'],
];

/** The axes a capability profile is scored on. Mirrors router.DIMENSIONS —
 *  the server is the authority, this is only what to label the sliders. */
const CAPABILITY_AXES = [
  ['implementation', 'Building'],
  ['debugging', 'Debugging'],
  ['review', 'Reviewing'],
  ['security', 'Security'],
  ['architecture', 'Architecture'],
  ['analysis', 'Analysis'],
];

function renderCouncilSettings() {
  const conf = state.config || {};
  const council = conf.council || {};

  $('#council-seats').value = council.seat_count ?? 3;
  $('#council-chair-timeout').value = council.chair_timeout_seconds ?? 1800;
  $('#council-routing').checked = (council.routing || 'auto') !== 'manual';
  $('#council-chair-deliberates').checked = council.chair_deliberates !== false;
  $('#council-strictness').value = council.strictness ?? 2;
  updateStrictnessNote();

  // Seating. One row per seat: which CLI holds it, and what it is told to be.
  // The same two choices the strip offers on a click - this is the whole bench
  // at once, and there it is one seat at a time.
  //
  // The chair has no behaviour column: the chairman is what the third stage
  // is, not a lens over it.
  const seats = ['chair'];
  for (let i = 1; i <= (Number(council.seat_count) || 3); i++) seats.push(`seat${i}`);
  const pins = council.pins || {};
  const personas = council.personas || {};
  const agents = (state.agents || []).filter(a => (a.command || []).length);
  const behaviours = (state.roles || []).filter(r => r.id !== 'chairman');

  $('#council-pins').innerHTML = seats.map(id => {
    const label = id === 'chair' ? 'Chair' : `Seat ${id.replace('seat', '')}`;
    return (
      `<div class="council-pin-row">` +
        `<span class="council-pin-label">${esc(label)}</span>` +
        `<select data-pin="${esc(id)}" aria-label="${esc(label)} CLI">` +
          `<option value=""${pins[id] ? '' : ' selected'}>Auto — routed per run</option>` +
          agents.map(a =>
            `<option value="${esc(a.id)}"${pins[id] === a.id ? ' selected' : ''}>` +
            `${esc(a.label)}</option>`
          ).join('') +
        `</select>` +
        (id === 'chair'
          ? `<span class="council-pin-fixed">Chairman</span>`
          : `<select data-persona="${esc(id)}" aria-label="${esc(label)} behaviour">` +
              `<option value=""${personas[id] ? '' : ' selected'}>` +
                `Auto — a lens the task calls for</option>` +
              behaviours.map(r =>
                `<option value="${esc(r.id)}"${personas[id] === r.id ? ' selected' : ''}>` +
                `${esc(r.name || r.id)}</option>`
              ).join('') +
            `</select>`) +
      `</div>`
    );
  }).join('');

  // Capability profiles.
  const caps = council.capabilities || {};
  $('#council-capabilities').innerHTML = agents.map(a => {
    const mine = caps[a.id] || {};
    return (
      `<div class="council-cap" data-cap-agent="${esc(a.id)}">` +
        `<h5 class="council-cap-name">${esc(a.label)}</h5>` +
        CAPABILITY_AXES.map(([axis, name]) => {
          // Blank means "use the shipped profile", and the server decides what
          // that is. An empty box here is therefore a real value, not a gap.
          const value = mine[axis];
          return (
            `<label class="council-cap-row">` +
              `<span class="council-cap-axis">${esc(name)}</span>` +
              `<input type="range" min="0" max="100" step="5" ` +
                `data-cap-axis="${esc(axis)}" ` +
                `value="${value === undefined ? '' : Math.round(value * 100)}"` +
                `${value === undefined ? ' data-unset="1"' : ''}>` +
              `<output>${value === undefined ? 'default' : Math.round(value * 100)}</output>` +
            `</label>`
          );
        }).join('') +
      `</div>`
    );
  }).join('');
}

function updateStrictnessNote() {
  const level = Number($('#council-strictness').value);
  const [name, note] = STRICTNESS_NOTES[level] || STRICTNESS_NOTES[2];
  $('#council-strictness-out').textContent = name;
  $('#council-strictness-hint').textContent = note;
}

/** The council block, read back off the panel. */
function readCouncilSettings() {
  // "Auto" is written as an empty string rather than by leaving the key out.
  // The server deep-merges a saved config, so an omitted seat keeps the pin it
  // already had - setting one back to Auto would appear to work and change
  // nothing. An empty value overwrites; the router reads it as unpinned.
  const pins = {};
  const personas = {};
  $$('#council-pins [data-pin]').forEach(sel => { pins[sel.dataset.pin] = sel.value; });
  $$('#council-pins [data-persona]').forEach(sel => {
    personas[sel.dataset.persona] = sel.value;
  });

  const capabilities = {};
  $$('#council-capabilities .council-cap').forEach(card => {
    const agent = card.dataset.capAgent;
    const axes = {};
    $$('input[data-cap-axis]', card).forEach(input => {
      // Untouched sliders stay absent so the server keeps using its shipped
      // profile. Writing 0.6 for every axis the operator never looked at
      // would freeze today's defaults into their config for good.
      if (input.dataset.unset) return;
      axes[input.dataset.capAxis] = Number(input.value) / 100;
    });
    if (Object.keys(axes).length) capabilities[agent] = axes;
  });

  return {
    seat_count: Number($('#council-seats').value) || 3,
    chair_timeout_seconds: Number($('#council-chair-timeout').value) || 1800,
    routing: $('#council-routing').checked ? 'auto' : 'manual',
    chair_deliberates: $('#council-chair-deliberates').checked,
    strictness: Number($('#council-strictness').value),
    pins,
    personas,
    capabilities,
  };
}

// The write-expectation warning that used to live here belonged to the fixed
// Stage 1 / Stage 2 pair, where a stage's permission was known from its id. It
// is not: a seat's permission comes from where it sits, so the same check now
// runs where the behaviour is *chosen* — see `openPersonaMenu`, which marks a
// behaviour that expects to write as one this seat cannot honour.

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
      // No `agent` key from this panel. Every card here is one CLI already,
      // and a stale value carried by the form would quietly undo a choice made
      // on the card or the bench at the next save.
      label: field('label').value.trim() || id,
      command,
      auto_approve_args: lines('auto_approve_args'),
      stream_args: lines('stream_args'),
      // A council stage has a role; the Chat assistant has a behaviour; a
      // project chair has neither, and is told what to do by the phase it is
      // in. Writing another kind's keys would leave a dead setting behind that
      // still reads as though it decided something.
      ...({
        seat: () => ({ read_only_args: lines('read_only_args') }),
        chat: () => ({
          behavior: field('behavior').value.trim(),
          read_only_args: lines('read_only_args'),
        }),
        project: () => ({}),
      }[form.dataset.kind] || (() => ({})))(),
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
    toast('Every agent needs a command.', 'error');
    return;
  }

  try {
    const { config } = await api('/api/config', {
      method: 'POST',
      body: {
        providers,
        council: readCouncilSettings(),
        house_rules: $('#house-rules').value,
        // Council and Chat live in their own composer menus. This form owns
        // only Project, and the config store deep-merges this leaf without
        // disturbing either chat mode.
        caveman: {
          project: $('#caveman-project').checked,
        },
        efficiency: {
          project: $('#efficiency-project').checked,
        },
        display_name: $('#display-name').value.trim(),
        port: parseInt($('#port-input').value, 10) || 8760,
        open_browser: $('#open-browser').checked,
        project: {
          max_steps: Math.max(1, parseInt($('#project-max-steps').value, 10) || 40),
          max_fix_attempts:
            Math.max(1, parseInt($('#project-max-fixes').value, 10) || 3),
          // The only one of the three that may legitimately be zero: no
          // expansion rounds ships the brief and nothing more.
          innovation_rounds:
            Math.max(0, parseInt($('#project-innovation-default').value, 10) || 0),
        },
      },
    });
    state.config = config;
    await refreshDoctor();
    renderAll();
    closeModal('settings');
    // The bench depends on every one of those settings, so the strip on screen
    // is now describing a council that would no longer be seated.
    refreshSeating();
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
      // Keep the conversation in the composer as well as on screen. Without
      // this, `startRun` cleared the attachment accepted for the previous
      // message and every ordinary next message became a new sidebar row.
      // "New chat" remains the explicit way to detach.
      const earlier = (d.run && d.run.conversation) || [];
      const title = (earlier.length ? earlier[0].task : d.run && d.run.task) || '';
      if (d.run && d.run.file) {
        continueRun(
          d.run.file,
          title,
          d.run.mode || (d.run.solo ? 'solo' : 'council'),
          true,
        );
      }
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
      // Chat shows the answer, not the agent's working transcript. The stage
      // header already says "working"; `stage_finished` renders the CLI's
      // final answer when it is ready.
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

  // -- projects ---------------------------------------------------------
  // A project runs for an hour and the operator is not expected to sit on the
  // tab, so these keep the state current wherever they are — and pull them to
  // the tab when a build starts, because that is where attention belongs.
  on('project_started', (d) => {
    state.project = d.project;
    state.projectRunning = true;
    state.projectResumable = false;
    state.tab = 'project';
    clearStream();
    pushDivider('Project started');
    pushLine('sys', 'project', `Goal: ${d.project.goal}`);
    pushLine('sys', 'project', `Folder: ${d.project.workspace}`);
    pushLine('warn', 'project',
      'The first turn is a read-only audit. Every turn after it runs with ' +
      'auto-approve, so files change without asking.');
    $('#console-block').open = true;
    renderAll();
    loadProject();
  });

  on('project_state', (d) => {
    state.project = d.project;
    // A paused project is still live — it is holding, not over — so what ends
    // it is a terminal status and nothing else.
    state.projectRunning = !d.project.done;
    if (d.project.done) {
      toast(
        d.project.status === 'COMPLETED' ? 'Project complete.' : (d.project.error || 'Project stopped.'),
        d.project.status === 'COMPLETED' ? 'ok' : 'error',
        12000,
      );
      // The tree has changed under the status bar and the commit bar, and
      // nothing else re-reads git.
      loadState();
    }
    renderProject();
  });

  on('project_step', (d) => {
    state.project = d.project;
    state.projectRunning = !d.project.done;
    pushDivider(`Phase ${d.step.phase} · ${d.step.role_label} · ${d.step.heading}`);
    pushLine('sys', 'project',
      `${d.step.role_label} · ${d.step.heading} (phase ${d.step.phase})`);
    renderProject();
  });

  on('project_output', (d) => {
    // Tagged with the chair rather than the CLI: which binary holds which
    // chair is a setting, so a hardcoded name would mislabel a third of the
    // stream the moment the matrix is changed.
    pushLine(`${d.role}${d.stream === 'stderr' ? ' stderr' : ''}`,
             ROLE_NAMES[d.role] || d.role, d.line);
  });

  on('project_step_done', (d) => {
    state.project = d.project;
    pushLine(d.step.ok ? 'sys' : 'err', 'exit',
      `${d.step.role_label} finished in ${fmtDuration(d.step.duration)}` +
      (d.step.ok ? '' : ` — ${d.step.error || 'failed'}`));
    renderProject();
  });

  on('project_log', (d) => {
    pushLine(d.level === 'warn' ? 'warn' : d.level === 'error' ? 'err' : 'sys',
             'project', d.message);
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
  const input = $('#task-input');
  const submittedValue = input.value;
  const task = submittedValue.trim();
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
    const { run: acceptedRun } = await api('/api/start', {
      method: 'POST',
      body: {
        task,
        workspace: workspacePath(),
        continue_from: state.continueFrom,
        compact_context: state.compactContext,
      },
    });
    // Only once the server has accepted it: a rejected start leaves the
    // message and attachment in place so the operator can fix them and retry.
    // Preserve anything typed while the request was in flight.
    if (input.value === submittedValue) {
      input.value = '';
      input.dispatchEvent(new Event('input'));
    }
    // A very fast agent can finish, publish its terminal event and attach its
    // new transcript before this POST response arrives. Do not let the older
    // request cleanup detach that freshly completed conversation.
    const alreadyAttachedLatest = acceptedRun && state.run &&
      state.run.id === acceptedRun.id &&
      ['complete', 'failed', 'cancelled'].includes(state.run.state);
    if (!alreadyAttachedLatest) clearContinuation();
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

    // Project is a surface rather than a mode, so it is remembered in the page
    // and nowhere else. Leaving it puts the operator back in the real mode
    // config still holds, which is why nothing is patched on the way out.
    if (want === 'project') {
      state.tab = 'project';
      renderAll();
      loadProject();
      return;
    }
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
    // Arriving in Council with an empty box: ask who the standing bench is,
    // so the seats are there to be clicked rather than appearing on the first
    // keystroke.
    refreshSeating();
  });

  // -- composer ---------------------------------------------------------
  const input = $('#task-input');
  const autosize = () => {
    // Reset first: without it the box can only ever grow, because scrollHeight
    // is measured against the height already set.
    input.style.height = 'auto';
    input.style.height = `${input.scrollHeight}px`;
  };
  // The council is seated from what is in this box, so the strip follows it.
  input.addEventListener('input', () => {
    autosize(); renderStatus(); scheduleSeating();
  });

  // -- council settings --------------------------------------------------
  $('#council-strictness').addEventListener('input', updateStrictnessNote);
  // Changing the member count adds or removes a row in the seating list, so
  // the list is rebuilt rather than left showing chairs that no longer exist.
  $('#council-seats').addEventListener('change', renderCouncilSettings);
  $('#council-capabilities').addEventListener('input', (e) => {
    const slider = e.target.closest('input[data-cap-axis]');
    if (!slider) return;
    // Touching a slider is what turns "use the shipped profile" into a value
    // of the operator's own. Until then it is left unset on purpose.
    delete slider.dataset.unset;
    const out = slider.parentElement.querySelector('output');
    if (out) out.textContent = slider.value;
  });
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
  $$('.project-gear-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if ($('.model-menu')) { closeModelMenu(); return; }
      openGearMenu(btn);
    });
  });

  // -- projects ---------------------------------------------------------
  // The agent matrix. A chair is the same provider object the council strip
  // renders, so this opens the same menu — minus Role, which a project chair
  // does not have.
  $('#project-matrix').addEventListener('click', (e) => {
    const chair = e.target.closest('[data-chair]');
    if (!chair) return;
    e.stopPropagation();
    if ($('.model-menu')) { closeModelMenu(); return; }
    openMemberMenu(chair, chair.dataset.chair, {
      note: 'What this chair is told to do comes from the board, not a role.',
    });
  });

  // One folder picker for the whole app: a project builds where runs run.
  $('#project-folder').addEventListener('click', () => {
    loadPicker(workspacePath() || undefined);
    openModal('picker');
  });

  // Touching it settles it: a deliberate drag outranks the saved default,
  // even if that default is still in flight when the drag happens.
  $('#project-innovation').addEventListener('input', (e) => {
    e.currentTarget.dataset.settled = '1';
    renderInnovation();
  });
  $('#project-start').addEventListener('click', () => startProject(false));
  $('#project-resume-found').addEventListener('click', () => startProject(true));

  $('#project-pause').addEventListener('click', () =>
    api('/api/project/pause', { method: 'POST' })
      .then(loadProject).catch(e => toast(e.message, 'error')));

  $('#project-resume').addEventListener('click', () =>
    api('/api/project/resume', { method: 'POST' })
      .then(loadProject).catch(e => toast(e.message, 'error')));

  $('#project-stop').addEventListener('click', async () => {
    if (!confirm(
      'Stop the project?\n\n' +
      'The agent that is running is killed where it stands, so a file it was ' +
      'part-way through writing stays part-written. Pause instead if you only ' +
      'want it to hold — that waits for the turn to finish.'
    )) return;
    try {
      await api('/api/project/stop', { method: 'POST' });
    } catch (err) { toast(err.message, 'error'); }
  });

  // Fetched on open rather than pushed: it is append-only and grows all run.
  $('#project-critique-block').addEventListener('toggle', loadProjectCritique);

  $('#project-handoff').addEventListener('click', (e) => {
    e.stopPropagation();
    if ($('.model-menu')) { closeModelMenu(); return; }
    openHandoffMenu($('#project-handoff'));
  });

  // Clearing the tab is what puts the initializer back. The project on disk is
  // untouched — this is closing the report, not deleting the build.
  $('#project-new').addEventListener('click', () => {
    state.project = null;
    state.projectResumable = false;
    renderProject();
    loadProject();
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
    // The seat id is what turns this into a bench editor: with it the menu
    // offers which CLI sits here and what it is told to be, both of which are
    // council settings rather than provider ones.
    openMemberMenu(member, member.dataset.member, {
      seat: member.dataset.seat || '',
      seatLabel: member.dataset.seatLabel || '',
    });
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
  $('#settings-btn').addEventListener('click', () => openSettings('stages'));
  $('#clear-council').addEventListener('click', () => clearHistory('council'));
  $('#clear-chat').addEventListener('click', () => clearHistory('solo'));
  $('.settings-tabs').addEventListener('click', (e) => {
    const tab = e.target.closest('.settings-tab');
    if (tab) switchSettingsTab(tab.dataset.settingsTab);
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

  // No agent picker lives on these forms any more, so there is nothing here to
  // preview a swap for. Every card is one CLI: Chat's is chosen on its card,
  // a project chair's in the matrix, and a council seat's card *is* the CLI.
  // The server still pairs a command with its permission flags on the paths
  // that do swap - that pairing is the part that has to stay honest.

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
  // After `refreshDoctor`, which is what decides who is available to seat.
  refreshSeating();
  // A project outlives the window it was started from, so the tab is opened
  // for you when one is still running - the alternative is an app that looks
  // idle while three agents rewrite a folder.
  await loadProject();
  if (state.projectRunning) {
    state.tab = 'project';
    renderAll();
  }
  $('#task-input').focus();
}

boot();
