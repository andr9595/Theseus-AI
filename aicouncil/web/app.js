/* ==========================================================================
   AI Council - dashboard client
   Vanilla ES2020, no framework, no bundler. Loaded as a classic script so it
   satisfies the `script-src 'self'` CSP with no inline handlers anywhere.

   Contents
     1. Utilities        6. State + rendering
     2. API client       7. Live stream
     3. Markdown         8. Modals (picker, settings, history)
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
  busy: false,
  streamLines: [],
  activeTab: 'stream',
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
    if (run.diff_stat && run.diff_stat.files) {
      meta.push(`${run.diff_stat.files} file(s) +${run.diff_stat.insertions}/-${run.diff_stat.deletions}`);
    }
    if (run.error) meta.push(run.error);
  }
  $('#topbar-meta').textContent = meta.join('  ·  ');

  $('#cancel-btn').classList.toggle('hidden', !state.busy);
  $('#rollback-btn').classList.toggle('hidden', !(run && run.can_rollback));

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
          `<button class="model-chip${provider.model ? ' set' : ''}" type="button" ` +
            `data-model-for="${id}" title="Change the model for this stage">` +
            `${esc(modelLabel)}` +
            `<svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" ` +
            `stroke-width="3" stroke-linecap="round" stroke-linejoin="round">` +
            `<path d="M6 9l6 6 6-6"/></svg>` +
          `</button>` +
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
  const pct = Math.round(u.worst.percent);
  const level = pct >= 90 ? 'crit' : pct >= 75 ? 'warn' : 'ok';
  // Codex's figure comes from its last run's log, not a live query. Mark it
  // once it is old enough to mislead, rather than presenting stale as current.
  const ageMin = u.worst.as_of ? (Date.now() / 1000 - u.worst.as_of) / 60 : 0;
  const stale = ageMin > 30;
  const tip = u.limits.map(l =>
    `${l.label}: ${l.percent}% used${l.resets ? ` · resets ${l.resets}` : ''}`
  ).join('\n')
    + (u.note ? `\n\n${u.note}` : '')
    + (stale ? `\n(measured ${fmtAge(ageMin)} ago)` : '')
    + (u.error ? `\n\n(last poll failed: ${u.error})` : '');
  return (
    `<button class="usage-chip ${level}" type="button" data-usage-for="${providerId}" ` +
      `title="${esc(tip)}">` +
      `<span class="usage-bar"><span data-fill="${Math.min(100, pct)}"></span></span>` +
      `${pct}%${stale ? '<span class="usage-stale">*</span>' : ''}` +
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
  $('#zero-touch-warning').classList.toggle('hidden', !c.zero_touch);

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
   8. Model picker
   Neither CLI can enumerate its own models, so the list is whatever the user
   has configured plus a free-text entry. That keeps a newly released model
   one keystroke away instead of requiring an app update.
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
      `</div>`
    );
  }).join('');

  $('#house-rules').value = conf.house_rules || '';
  $('#port-input').value = conf.port || 8760;
  $('#open-browser').checked = conf.open_browser !== false;
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
      model: field('model').value.trim(),
      // Space-separated on this form, since a model flag is always short.
      model_args: field('model_args').value.trim().split(/\s+/).filter(Boolean),
      models: lines('models'),
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

/* ---- History ---- */

async function showHistory() {
  openModal('history');
  const list = $('#history-list');
  list.innerHTML = '<div class="picker-empty">Loading…</div>';
  try {
    const { runs } = await api('/api/history');
    if (!runs.length) {
      list.innerHTML = '<div class="picker-empty">No runs yet.</div>';
      return;
    }
    list.innerHTML = runs.map(r => {
      const stat = r.diff_stat || {};
      const changed = stat.files
        ? `<span class="history-stat"><span class="stat-add">+${stat.insertions || 0}</span> ` +
          `<span class="stat-del">&minus;${stat.deletions || 0}</span></span>`
        : '';
      return (
        `<div class="history-row ${esc(r.state)}">` +
          `<div class="history-main">` +
            `<div class="history-task">${esc(r.task || '(no task)')}</div>` +
            `<div class="history-sub">${fmtWhen(r.created_at)} · ${esc(r.state)} ` +
              `${r.zero_touch ? '· zero-touch ' : ''}· ${esc(r.repo || '')}</div>` +
          `</div>${changed}` +
        `</div>`
      );
    }).join('');
  } catch (err) {
    list.innerHTML = `<div class="picker-empty">${esc(err.message)}</div>`;
  }
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
    await api('/api/start', { method: 'POST', body: { task, repo: conf.target_repo } });
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
    openModelMenu(chip, chip.dataset.modelFor);
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
    openModal('settings');
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
    // Model names are not interchangeable between CLIs.
    $('[data-field="model"]', form).value = '';
    $('[data-field="models"]', form).value = '';
  });

  $('#run-doctor').addEventListener('click', () => refreshDoctor(true));
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

  // -- history ----------------------------------------------------------
  $('#history-btn').addEventListener('click', showHistory);

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
  connect();
  $('#task-input').focus();
}

boot();
