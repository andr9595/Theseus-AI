# AI Council

A local, dark-themed desktop dashboard that runs a **two-stage coding pipeline**
across your existing AI subscriptions — with **zero per-token API cost**.

```
   Your task
       |
       v
  ┌──────────────────┐   read-only     ┌──────────────────┐   writes to disk
  │     Stage 1      │ ─── draft ───>  │     Stage 2      │ ──> your repo
  │   Junior Draft   │                 │  Senior Polish   │
  │  (Codex, or…)    │                 │  (Claude, or…)   │
  └──────────────────┘                 └──────────────────┘
                            ^
                            └── approval gate (unless Zero-Touch is on)
```

Stage 1 shells out to the `codex` CLI (your ChatGPT Plus/Pro subscription) to
survey the repository and write an implementation proposal. Stage 2 shells out
to the `claude` CLI (your Claude Pro subscription) to verify that proposal
against the real code, correct it, and apply the change.

That pairing is the default, not a rule: **either agent can be assigned to
either job** from Settings, including the same agent twice. See
[Assigning agents to jobs](#assigning-agents-to-jobs).

**Nothing in this application reads, stores or transmits an API key.** It
drives the CLIs you have already authenticated, so the marginal cost of a run
is zero.

---

## Why the pipeline is ordered this way

Claude Pro's usage is rationed on a rolling window; Codex's is comparatively
generous. The expensive part of any coding task is the *exploration* — reading
the codebase, weighing approaches, discarding dead ends. So the junior stage
absorbs that cost, and the senior stage receives a pre-digested proposal and
spends its scarcer quota on judgement and application.

Stage 2 is explicitly instructed to treat the draft as **untrusted input** —
a colleague's suggestion, not a specification. That framing is deliberate: the
main failure mode of a naive two-model chain is the second model politely
rubber-stamping a confidently-wrong first draft.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python **3.9+** | Standard library only — no `pip install`, no virtualenv, no build step |
| `git` | For the repo picker, diff viewer and rollback |
| A browser | Chromium-family gets a frameless app window; Firefox gets a plain window |
| `codex` CLI | Optional — Stage 1. See [Installing the agent CLIs](#installing-the-agent-clis) |
| `claude` CLI | Optional — Stage 2 |

The app itself has **no dependencies at all**. If the two CLIs are not
installed yet, everything still runs — point the providers at the bundled mock
agent (below) and the full pipeline is exercisable end to end.

---

## Quick start

```bash
git clone https://github.com/andr9595/ai-council.git
cd ai-council
./run.sh
```

That is the whole install. `run.sh` locates a Python 3.9+ interpreter, starts a
loopback-only server on port 8760, and opens the dashboard in a browser window.

Check what the app can see:

```bash
./run.sh --doctor
```

```
AI Council v1.0.0
  python      : 3.12.3 (/usr/bin/python3)
  config      : /home/you/.config/ai-council/config.json
  zero-touch  : off
  target repo : (none selected)

Providers:
  [MISS] Junior Draft   Codex    codex      -> not found on PATH
  [MISS] Senior Polish  Claude   claude     -> not found on PATH
```

### Launcher flags

| Flag | Effect |
|---|---|
| `--doctor` | Report environment and CLI availability, then exit |
| `--no-browser` | Start the server without opening a window |
| `--port N` | Preferred port (falls back to a free one if taken) |
| `--print-url` | Print only the dashboard URL, then serve |

---

## Using it

1. **Pick a target repository.** Click the folder button in the sidebar. The
   picker badges directories that are git repos; only those can be selected.
2. **Describe the task.** Be specific about files, behaviour and edge cases —
   the draft stage is only as good as its brief.
3. **Run it** with the button or <kbd>Ctrl</kbd>+<kbd>Enter</kbd>.
4. **Watch the council work.** The sidebar rail shows which agent is active;
   the Live stream tab carries their output line by line as it arrives.
5. **Review and approve.** The run pauses with the draft in the **Draft** tab
   and nothing yet written to disk. Optionally type a steer — it takes
   precedence over the draft — then click **Approve & execute**.
6. **Inspect the result.** The **Diff** tab renders the actual `git diff` of
   your working tree, per file, with line numbers.
7. **Roll back** if you don't like it. One click restores the tree exactly.

### The tabs

| Tab | Contents |
|---|---|
| **Live stream** | Interleaved stdout/stderr from both CLIs, tagged by agent |
| **Draft** | Stage 1's proposal, rendered as Markdown with highlighted code |
| **Senior review** | Stage 2's review, change summary and verification notes |
| **Diff** | The real working-tree diff, syntax-marked and collapsible per file |

---

## Zero-Touch Mode

The toggle the whole design orbits around.

**Off (default).** The run pauses at the approval gate. The draft is read-only
by instruction, so at that moment *nothing has been written to disk*. Clicking
**Approve & execute** is what grants Stage 2 permission to modify files.

**On.** No gate. The pipeline runs start to finish unattended, and Stage 2
receives its CLI's auto-approve flag (`--dangerously-skip-permissions` for
`claude`, `--dangerously-bypass-approvals-and-sandbox` for `codex`).

Three properties hold in both modes, and they are covered by tests:

- **Stage 1 never receives an auto-approve flag.** It is read-only by contract
  regardless of the toggle.
- **The flags are never baked into the command template.** They live in a
  separate config field and are appended only when permission has actually been
  granted — so switching Zero-Touch off is sufficient to guarantee they are not
  passed.
- **A safety snapshot is taken immediately before Stage 2 runs**, so any run is
  reversible.

> **Zero-Touch means what it says.** An agent will create, modify and delete
> files in your repository with no further confirmation. Use it on a branch,
> keep Safety Snapshot on, and don't point it at anything you can't afford to
> lose.

### Other toggles

| Toggle | Effect |
|---|---|
| **Safety snapshot** | Capture the worktree before Stage 2 so **Roll back** works. Leave on. |
| **Solo mode** | Skip the draft and run a single agent. Costs more quota; use for tasks too small to be worth a draft. |
| **Solo mode runs** | Which stage's configuration works alone — so Solo Mode can use a different agent than a full council run does. |
| **Require clean tree** | Refuse to start if the repo has uncommitted changes. |

Solo Mode still stops at the approval gate unless Zero-Touch is on: there is no
draft to read, but the operator is still authorising an agent to write.

---

## How rollback works

Before Stage 2 writes anything, the app records your worktree as a real commit
object — anchored under its own ref in `refs/ai-council/snapshots/`, so `git gc`
can't reap it and a later run can't orphan an earlier one's snapshot (the
twenty most recent are kept):

```
GIT_INDEX_FILE=<scratch> git add -A     # tracked edits AND untracked files
GIT_INDEX_FILE=<scratch> git write-tree
git commit-tree <tree> -p HEAD
```

It uses a **scratch index**, so your real index — and any carefully staged
hunks in it — is never touched. It deliberately does *not* use `git stash
create`, which cannot capture untracked files: a rollback built on it would
delete every new file you hadn't committed yet.

Rollback then resets to HEAD, cleans, and lays the snapshot tree back down.
Ignored files (`node_modules/`, `.venv/`, build output) are neither captured
nor deleted. The one thing not reproduced is the staged/unstaged split:
everything uncommitted before the run is uncommitted after it, but changes
that were staged come back unstaged.

Snapshotting **fails closed**. If any part of it fails — or the repository has
no commits yet to anchor to — the run is told so in the live stream and
**Roll back** is not offered at all. A snapshot that captured nothing cannot
distinguish "your tree was clean" from "we recorded nothing", and resetting on
that assumption would destroy the work the snapshot exists to protect.

---

## Installing the agent CLIs

The pipeline needs `codex` and `claude` on your `PATH`. Both vendors ship a
first-party installer that drops a standalone binary into `~/.local/bin` — **no
Node, no npm, no sudo**:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | bash
curl -fsSL https://claude.ai/install.sh | bash
source ~/.bashrc
```

Or let the bundled script do it, which is the same thing plus a PATH check:

```bash
./scripts/install-deps.sh              # CLIs only, no sudo
./scripts/install-deps.sh --check      # report what's present, install nothing
./scripts/install-deps.sh --extras     # also gh + python3-pip/venv (needs sudo)
./scripts/install-deps.sh --vscode     # also VS Code (implies --extras)
```

> Both installers pipe a remote script to `bash`. They are the official
> sources, but you can read them first:
> `curl -fsSL https://chatgpt.com/codex/install.sh | less`

Avoid `npm install -g @anthropic-ai/claude-code` unless you already run Node —
it now requires Node ≥ 22, which pulls in a whole toolchain for no benefit over
the standalone binary. The Codex installer also places `codex-code-mode-host`
next to the main binary, which downloading a release asset by hand misses.

Then authenticate each CLI once, interactively:

```bash
codex login     # browser login for ChatGPT Plus/Pro
claude          # browser login for Claude Pro
```

These are **subscription logins, not API keys** — that is what keeps runs at
zero per-token cost. Setting an API key instead would put every run on metered
billing.

Confirm the app can see them with `./run.sh --doctor`.

### Trying it without the CLIs

A mock agent ships in `scripts/`. It streams realistic Markdown and writes a
real file, so the full Draft → Approve → Polish → Diff → Rollback loop works:

Settings → for each stage, set the command to (one argument per line):

```
python3
/absolute/path/to/ai-council/scripts/mock-agent.py
--role
drafter                       ← use "polisher" for Stage 2
{prompt}
```

---

## Configuring the providers

Each stage is just a command template. `{prompt}` is replaced with the
generated prompt; it can appear anywhere, including inside a larger argument
like `--message={prompt}`. (A prompt over 96 KB has to move to stdin, and a
decorated placeholder cannot follow it there — that combination is refused
with an explanation rather than quietly sending an empty `--message=`.)

| Field | Meaning |
|---|---|
| **Agent** | Which CLI runs this job. Changing it swaps the command and auto-approve arguments together. |
| **Command** | argv, one argument per line. Never passed through a shell. |
| **Auto-approve arguments** | Appended *only* when permission has been granted. |
| **Streaming arguments** | Always appended. How this CLI is asked to narrate its work. See below. |
| **Model** | Blank means the CLI's own default. See below. |
| **Model flag** | How the model is passed — `--model {model}`, `-m {model}`, `--model={model}`. |
| **Selectable models** | The list offered in the picker, one per line. |
| **Timeout** | Seconds before the child process group is killed. |
| **Pipe the prompt on stdin** | For CLIs that prefer stdin. Automatic above 96 KB regardless. |

### Assigning agents to jobs

The *agent* (which CLI) and the *job* (Junior Draft / Senior Polish) are
separate settings. Settings → each stage has an **Agent** dropdown: Codex,
Claude, or Custom command. Claude can draft and Codex can be the senior; the
same agent can hold both jobs.

Picking one swaps that stage's command, display name, auto-approve argument,
streaming arguments and model flag **as a single unit**, because those only
make sense together —
`--dangerously-skip-permissions` on `codex` is rejected outright, and the
reverse is worse: the CLI starts, finds no permission grant, and blocks on a
prompt nothing in this pipeline can answer. The swap also clears the stage's
model, since a Codex slug handed to `claude --model` fails at launch.

Everything else about the stage — its job, prompt, timeout and approval
behaviour — is untouched. Editing the command by hand still works and simply
reads back as **Custom command**; the command is the source of truth, and the
dropdown is derived from it, so the two can never disagree.

Solo Mode picks its agent the same way: the **Solo mode runs** selector under
the toggle chooses which stage's configuration works alone, so solo runs can
use a different agent from a full council run.

### Why the stream needs "streaming arguments"

A CLI only narrates its work if you ask it to. `claude -p` in its default text
mode prints **nothing at all** until the run is over, then prints the finished
answer in one block — so the Live stream sat empty for the whole run and filled
up at the end. `codex exec` narrates by default and needs nothing extra.

So Claude is launched with `--output-format stream-json --verbose`, which emits
one JSON object per step as it happens, and the app translates those events back
into lines:

```
· model claude-opus-4-8            the model actually in use
· <text>                           the agent's reasoning as it thinks
<text>                             the agent's own words
→ Read aicouncil/server.py         a tool call, with its main argument
← def do_GET(self): (+118 lines)   what came back, summarised
```

Measured on a three-tool-call task: first output at **1.2s** with the streaming
flags, versus **8.7s of a 9.3s run** without them.

The **Draft** and **Senior review** tabs still show the agent's final answer
alone, not this transcript — the events carry both, and each pane gets the one
it wants.

If a future CLI release renames these flags, edit the field; nothing here is
compiled into the app. Clearing it is safe too — you simply get the old
all-at-the-end behaviour back, and the app stops trying to parse events.

### Roles

What each stage is *told to do* is a setting, not a constant. Settings → per
stage → **Role**:

| Template | Behaviour | Writes |
|---|---|---|
| Junior Draft | Surveys the repo, proposes a change | no |
| Senior Polish | Verifies the draft, corrects it, applies it | yes |
| Solo Architect | Works the task directly, no draft to review | yes |
| Adversarial Reviewer | Hunts for defects, fixes nothing | no |
| Test Writer | Writes tests that would have caught real bugs | yes |
| Security Reviewer | Findings with a real attacker and a real path | no |

Pick one and it takes effect on the next run. The text box below it overrides
the template entirely — edit it, or clear it to go back. Blank means "use the
template", so clearing the box restores the default rather than sending an
empty prompt.

Combined with agent assignment, that already covers a lot: put Claude on the
draft stage as an **Adversarial Reviewer** and Codex on the polish stage, and
Claude is no longer the final voice.

**A caveat the UI states rather than hides.** Permission is still granted *per
stage* — Stage 1 is read-only, Stage 2 writes once approved. So a writing role
on Stage 1 produces an agent told to modify files that cannot, and a
report-only role on Stage 2 gets told not to write while still permitted to.
Settings flags the mismatch instead of silently resolving it; guessing which
of the two you meant is how a safety setting stops being trustworthy. Making
`can_write` a property of the role rather than the stage is the next step.

### Quota

Each agent card carries a percentage chip showing the vendor's own figure, read
two different ways because the CLIs differ:

| Agent | Source | Freshness |
|---|---|---|
| Claude | `claude -p "/usage"` — the slash command, answered locally with no model call | Live; polled at launch and every 5 min |
| Codex | the `rate_limits` headers it writes to `$CODEX_HOME/sessions/*.jsonl` | As of the last Codex run; marked `*` when older than 30 min |

Hover for every window; click to force a refresh. The chip shows whichever
window is closest to exhaustion and turns amber at 75%, red at 90%. At 85% a
run warns first — a warning only, always forceable, because the reading is a
snapshot and only you know what the task is worth.

`codex exec "/status"` is **not** how to get this: Codex has no non-interactive
slash commands, so the text goes to the model as a prompt, costs ~16k tokens,
and comes back with the model explaining it cannot see account limits.

### Choosing a model per stage

Click the model chip on either agent card to switch that stage's model. The
picker offers the configured list, the CLI's own default, and a free-text box
for anything else — typing a model adds it to the list for next time.

The picker asks each CLI what it can actually run. Codex publishes an
account-scoped list in `$CODEX_HOME/models_cache.json` — read live, so it
reflects your login's entitlements. Claude ships no such file, so the picker
offers its documented `--model` aliases.

Nothing is hardcoded, deliberately: a shipped list is wrong the moment a model
is renamed, and wrong *per account* regardless. A ChatGPT-account Codex login
rejects models an API key would accept, with a 400 at run time rather than
anything you could see when choosing.

Aliases (`opus`, `sonnet`, `haiku`, `fable`) always resolve to the newest model
in that family; a pinned ID (`claude-opus-4-8`) stays where you put it. The
picker labels which is which, because the difference only shows up months later
when a pinned stage is quietly running a superseded model.

Blank — the default — passes no `--model` flag at all, so each CLI uses
whatever it is configured for. That is the setting most likely to still be
correct in six months.

A practical split: put the cheap, generous-quota model on Stage 1 and spend the
rationed one on Stage 2, which is where judgement actually matters.

Defaults — Codex drafts because its quota is the generous one, but the
assignment is yours to change:

| Stage | Default agent | Command | Auto-approve |
|---|---|---|---|
| 1 · Junior Draft | Codex | `codex exec {prompt}` | `--dangerously-bypass-approvals-and-sandbox` |
| 2 · Senior Polish | Claude | `claude -p {prompt}` | `--dangerously-skip-permissions` |

**Standing project rules** are appended to every prompt in both stages — a good
place for "use tabs", "never add a dependency without asking", "all new code
needs tests".

Config lives at `~/.config/ai-council/config.json`. Run transcripts are written
to `~/.config/ai-council/runs/` and surfaced in the History panel.

---

## Architecture

```
aicouncil/
├── __main__.py     Entry point, browser launcher, --doctor
├── server.py       http.server + SSE, token auth, Origin/Host validation
├── pipeline.py     The state machine: drafting → gate → polishing → complete
├── providers.py    CLI adapters: argv construction, streaming, cancellation
├── prompts.py      Stage 1 / Stage 2 / solo prompt construction
├── gitutil.py      Repo status, diffs, snapshot & rollback plumbing
├── config.py       Atomic JSON config with deep-merged defaults
├── events.py       Pub/sub bus with replay and per-subscriber backpressure
└── web/            index.html · app.css · app.js  (no build step)
```

**Stack rationale.** A local web GUI, not Electron or Qt. It needs no package
manager, no compiler and no `sudo` to run; it renders Markdown, syntax and
diffs natively; and it works on Wayland without a toolkit. The cost is a
hand-written Markdown renderer, highlighter and diff viewer in `app.js` —
roughly 400 lines, which buys a zero-dependency install.

**Streaming** is Server-Sent Events, not WebSockets: the wire format is three
lines of text over an ordinary HTTP response, where RFC 6455 frame masking in
`http.server` would be a lot of fragile code for a stream that only ever flows
server → browser. Subscribers have bounded queues, so a stalled tab sheds old
events instead of blocking the pipeline thread.

### Security

`localhost` is not a trust boundary — any web page you visit can issue requests
to `127.0.0.1:8760`. Since this app's API can run a coding agent with
auto-approve flags, four defences are layered:

1. **Per-launch session token**, never persisted, required on every `/api/` call.
2. **Origin validation** — rejects requests from real remote sites (drive-by CSRF).
3. **Host validation** — rejects non-loopback `Host` headers (DNS rebinding).
4. **No shell** — commands run as argv lists with `shell=False`, so a prompt
   containing `` ` ``, `$(...)` or `;` is inert data.

The UI is served under a strict CSP with no external assets, so agent output
rendered as Markdown can never pull in a third party. Binding to anything other
than loopback is refused outright.

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

98 tests, standard library only. They drive real subprocesses, real sockets and
real git repositories in temporary directories rather than mocking them — the
parts most likely to break are exactly the ones a mock would paper over.

Coverage focuses on the properties that matter if they're wrong:

- Auto-approve flags reach the child process **if and only if** permission was
  granted, and never reach Stage 1.
- The approval gate is reached with a **pristine working tree**, and the
  configuration approved there is the one that runs — a settings change at the
  gate cannot swap the command about to be granted write permission.
- Rollback restores agent changes **without destroying pre-existing uncommitted
  work** — the regression that made `git stash create` unusable here — and a
  snapshot that failed is never offered as a rollback point.
- Assigning an agent to a job moves its command **and** its permission flag
  together, so the two can never be mismatched.
- Cross-origin and bad-token requests are rejected; path traversal is blocked.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `codex`/`claude` shows **not found** | Not on `PATH`. Run `./scripts/install-deps.sh`, or set an absolute path in Settings. |
| Stage 2 finishes but the diff is empty | The CLI ran without write permission. With Zero-Touch off, you must click **Approve & execute** — that's what grants it. |
| "Missing session token" | The dashboard was opened without the launcher's URL. Restart with `./run.sh`. |
| Run hangs, no output | The CLI is waiting on interactive input. Check its auto-approve arguments in Settings. |
| Port already in use | The server falls back to a free port automatically; read the URL it prints. |
| Stream shows "reconnecting" | The server stopped. It reconnects with backoff once it's back. |

---

## License

MIT — see [LICENSE](LICENSE).
