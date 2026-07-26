# AI Council

A local, dark-themed desktop dashboard that runs a **two-stage coding pipeline**
across your existing AI subscriptions — with **zero per-token API cost**.

```text
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

Stage 2 is explicitly instructed to treat the draft as **untrusted input** — a
colleague's suggestion, not a specification. That framing is deliberate: the
main failure mode of a naive two-model chain is the second model politely
rubber-stamping a confidently-wrong first draft.

That pairing is the default, not a rule: **either agent can be assigned to
either job** from Settings, including the same agent twice. See
[Assigning agents to jobs](#assigning-agents-to-jobs).

**Nothing in this application reads, stores or transmits an API key.** It
drives the CLIs you have already authenticated, so the marginal cost of a run
is zero.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python **3.9+** | Standard library only — no `pip install`, no virtualenv, no build step |
| `git` | For the repo picker, diff viewer and rollback |
| A browser | Chromium-family gets a frameless app window; Firefox gets a plain window |
| `codex` CLI | Optional — Stage 1. See [Installing the agent CLIs](#installing-the-agent-clis) |
| `claude` CLI | Optional — Stage 2 |
| `gh` CLI | Optional — only for [Pull-request mode](#pull-request-mode). `./scripts/install-deps.sh --extras` installs it |

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

```text
AI Council v1.0.0
  python      : 3.12.3 (/usr/bin/python3)
  config      : /home/you/.config/ai-council/config.json
  runs        : /home/you/.config/ai-council/runs
  zero-touch  : off
  target repo : (none selected)
  pull request: off

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
| `--version` | Print the version, then exit |

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

```text
python3
/absolute/path/to/ai-council/scripts/mock-agent.py
--role
drafter                       ← use "polisher" for Stage 2
{prompt}
```

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
8. **Or keep going.** **Continue this run** turns the finished run into the
   first message of a conversation — see [Continuing a run](#continuing-a-run).

### The tabs

| Tab | Contents |
|---|---|
| **Live stream** | Interleaved stdout/stderr from both CLIs, tagged by agent |
| **Draft** | Stage 1's proposal, rendered as Markdown with highlighted code |
| **Senior review** | Stage 2's review, change summary and verification notes |
| **Diff** | The real working-tree diff, syntax-marked and collapsible per file |

---

## Continuing a run

A single task is rarely the whole conversation. The **Chats** tab in the
sidebar lists your conversations, newest first, the way a chat client does.
Select one to read it in the main pane — every message, what each agent
answered, the note you left at the approval gate, and the diff that resulted.
**+ New conversation** clears the attachment and puts you back at the composer.

A follow-up is not a separate entry in that list. It carries the earlier turns
inside its own transcript, so the newest run of a thread *is* the conversation,
and the list shows one row per thread named for the message it opened with.
Continue the same run twice and you get two rows, because the history really is
a tree and folding one branch away silently would lose it.

**Continue** — on the open conversation, or in the top bar as soon as a run
finishes — attaches that exchange to your next message. The follow-up is a
**new run** with its own transcript, approval gate and rollback point; nothing
about the earlier one is overwritten. Both stages are given the thread, so the
junior drafts with the earlier reasoning in view and the senior sees what it
already told you.

Two deliberate limits:

- **It replays the council's transcript, not the CLI's session.** A stage can
  be any command you configure, and a custom one has no session to resume.
  Replaying the transcript works for every agent identically.
- **The repository is the authority, not the recollection.** A remembered run
  may since have been rolled back or edited over by hand, so the prompt says so
  and the old diff is deliberately not replayed — the working tree already
  carries it, more accurately.

Continuation only works within the repository the run started in.

### The context meter, and compaction

Attaching a conversation shows what replaying it will cost, next to the banner
in the composer and under the title in the conversation view:

```
3 earlier messages · ~7.4k tokens · ≈4% of a 200k window
```

Read all three figures as estimates, because that is what they are. No CLI
reports its tokenizer's count back to this app, so the token figure is the usual
four-characters-per-token approximation. The window is the
`context_window_tokens` setting in your config — 200k by default, which suits
current Claude and Codex models — and not a number any vendor told us. And it
measures the replayed conversation only: your task, the junior's draft and
whatever the agent reads for itself all land in the same window, so treat it as
a floor rather than a total.

How far below the total is worth knowing. A `claude -p` run with a 22-character
prompt — one this meter would price at ~6 tokens — reports **36,560 input
tokens**, because Claude Code loads its own system prompt, tool schemas and
`CLAUDE.md` before your text arrives. That is roughly 18% of a 200k window spent
before the council has said anything. So the meter is a reliable guide to
whether *the thread* is getting expensive, and not a reading of how full the
window actually is.

A thread is bounded at both ends. Each stage's answer is trimmed when it is
recorded, and once the rendered thread would exceed its budget the oldest turns
are **compacted**: your message, the outcome, your steer at the gate and the
opening and closing of each answer are kept, and the bulk of the older replies
is dropped. Compaction works on whole turns at semantic boundaries, so it never
cuts through the middle of a sentence or a code fence, and the newest turn is
always carried in full. Nothing is silently discarded — a compacted answer is
labelled as a summary both in the prompt and in the conversation view, so no
agent mistakes an outline for the whole reply.

**Compact** next to the banner does it early, before the thread gets that far.
It is a toggle on the run you are about to start, not an action taken there and
then: click it and the meter immediately re-reads for the compacted thread, so
you can see what it buys before spending anything, and click it again to send
the turns in full after all. The transcripts already on disk are never
rewritten. The button is present whenever a conversation is attached and says
in its tooltip when there is nothing yet to compact — with only one earlier
message there is nothing to summarise, because the newest turn is always sent
whole.

Compaction happens here, on the council's own transcript, rather than through a
CLI's own `/compact`. Every stage is a fresh process replaying that transcript,
either agent can hold either job, and a custom configured command may have no
session to compact at all — so doing it once, in one place, is what keeps both
council members working from the same conversation.

---

## Zero-Touch mode

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

The sidebar splits them by what they decide. **Run** is how the next run
behaves; **Delivery & recovery** is where the work lands and how to get it back.
Both groups are Council-only and are hidden in Solo Mode, which has nothing for
them to decide.

| Toggle | Group | Effect |
|---|---|---|
| **Pull request** | Delivery | Deliver the run on a branch of its own and open a GitHub PR instead of writing to the checked-out branch. See below. |
| **Require clean tree** | Delivery | Refuse to start if the repo has uncommitted changes. Pull-request mode enforces this itself, on or off. |
| **Safety snapshot** | Delivery | Capture the worktree before Stage 2 so **Roll back** works. Leave on. |

---

## Council or Solo

The switch at the top of the sidebar decides which of two things your next
message starts. It is the first choice, above everything it changes, because
the two share almost nothing.

**Council** is the pipeline this README is mostly about: Junior Draft →
approval gate → Senior Polish, with Live stream, Draft, Senior review and Diff
views, delivery controls, snapshots and rollback.

**Solo** is one assistant answering one message, the way opening `claude` or
ChatGPT is. It has:

- **Its own agent**, configured under Settings → Agents. It borrows neither
  council stage, so Solo can run Codex while the council runs Claude.
- **No behaviour by default.** With the **Behaviour** box empty and no thread
  to replay, your message reaches the CLI *exactly as typed* — no persona, no
  house rules, no repository preamble. Type something into that box and it is
  put in front of the message; that is the whole of it.
- **No council furniture.** No draft, no approval gate, no Zero-Touch, no
  pull request, no snapshot, no rollback, and none of the four output tabs —
  just the message and the reply.
- **No write permission.** Solo is invoked with its agent's read-only
  arguments (`--sandbox read-only` for `codex`, `--permission-mode plan` for
  `claude`) and never receives an auto-approve flag. It reads the selected
  repository and talks about it; Council is the path for changing it.

Conversations from the two modes are kept apart: continuing one switches the
selector to the mode it was held in, and the server refuses the mismatch
outright rather than replay a council transcript into a plain chat.

Configurations written before this existed are migrated on load. The old
**Solo mode** toggle becomes the mode; the stage the old **Solo mode runs**
selector pointed at becomes the initial Solo assistant, keeping its CLI, model
and reasoning level but not its council role.

---

## Pull-request mode

Off by default. On, a run never writes to the branch you started on — it
delivers to a branch of its own and leaves the merge to you, which is what
makes a protected `main` workable with Zero-Touch on:

```text
main (untouched)  ──────────────────────────────────────────>
                   \
                    ai-council/add-rate-limiting-9f2c1a  ──> pushed ──> PR
                     ^                      ^                            ^
                     created after the      Stage 2 works here           you merge
                     approval gate
```

1. **Before anything starts**, every precondition is checked: a clean tree, a
   commit to branch from, a named branch (not a detached HEAD), a git identity
   to commit with, an `origin` remote, `gh` on `PATH`, and `gh auth status`
   passing. Failing late — after the senior stage has spent its quota — would
   strand the work on a branch nobody asked for.
2. **The branch is created after the approval gate**, so rejecting still leaves
   the repository completely untouched.
3. Stage 2 works on that branch as it normally would.
4. On success the run commits everything it changed, pushes with
   `--set-upstream origin`, and runs `gh pr create --base <the branch you
   started on> --head <the run's branch>`. The senior stage's own summary
   becomes the PR body.
5. **It then checks the base branch back out.** Left on the delivery branch,
   the next run would quietly take *it* as the base.

Nothing is ever merged for you, and the base branch is the branch that was
checked out when you pressed Run — so `main`, `master` and release branches all
work without another setting.

Two consequences worth knowing:

- **The clean-tree requirement is not the "Require clean tree" toggle**, and
  applies whether or not that toggle is on. The run commits everything it
  finds; anything you already had in flight would be swept into the pull
  request.
- **Roll back is not offered once a PR is open.** It restores a working tree,
  and by then the work is a pushed branch and an open pull request. Close the
  PR and delete the branch instead. If publishing fails *before* the PR is
  created, rollback stays available and the run tells you which branch the work
  is sitting on.

This mode narrows what a safety snapshot is *for*, without replacing it. It
still runs, and it is still the only recovery on offer between the moment
Stage 2 starts writing and the moment the PR exists — the window a failed push
or an unauthenticated `gh` leaves you in. What it no longer protects is your
own uncommitted work, because there wasn't any: the run refused to start
otherwise. In pull-request mode a rollback discards the run's work and returns
the delivery branch to the commit it forked from. Outside this mode the
snapshot is doing the harder job of putting *your* in-flight edits back, which
is why the toggle is not something pull-request mode can retire.

An agent that commits its own work is fine: the run commits whatever is left
outstanding and then judges success on whether the branch is actually ahead of
its base. The Diff tab shows `base...branch`, which is what the reviewer will
see.

Branch protection itself lives on GitHub, not here. This mode keeps the app off
your base branch; enabling a ruleset is what stops everything else.

---

## How rollback works

Before Stage 2 writes anything, the app records your worktree — tracked edits
and untracked files alike — as a real commit object, anchored under its own ref
in `refs/ai-council/snapshots/` so `git gc` can't reap it and a later run can't
orphan an earlier one's snapshot (the twenty most recent are kept).

It writes that commit through a **scratch index**, so your real index — and any
carefully staged hunks in it — is never touched. It deliberately does *not* use
`git stash create`, which cannot capture untracked files: a rollback built on it
would delete every new file you hadn't committed yet.

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

The defaults:

| Stage | Agent | Command | Auto-approve |
|---|---|---|---|
| 1 · Junior Draft | Codex | `codex exec {prompt}` | `--dangerously-bypass-approvals-and-sandbox` |
| 2 · Senior Polish | Claude | `claude -p {prompt}` | `--dangerously-skip-permissions` |

**Standing project rules** are appended to every prompt in both stages — a good
place for "use tabs", "never add a dependency without asking", "all new code
needs tests".

Config lives at `~/.config/ai-council/config.json`. Run transcripts are written
to `~/.config/ai-council/runs/` and grouped into threads under the **Chats**
tab, where a conversation can be read in full or
[continued](#continuing-a-run).

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

The Solo assistant has its own entry in the same list, with its own Agent
dropdown, display name, model, reasoning effort, command and optional
**Behaviour**. It has no Role, because it is not a stage in anything.

### Roles

What each stage is *told to do* is a setting, not a constant. Settings → per
stage → **Role**:

| Template | Behaviour | Writes |
|---|---|---|
| Junior Draft | Surveys the repo, proposes a change | no |
| Senior Polish | Verifies the draft, corrects it, applies it | yes |
| Direct Implementer | Works the task directly, no draft to review | yes |
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
of the two you meant is how a safety setting stops being trustworthy.

### Why the stream needs "streaming arguments"

A CLI only narrates its work if you ask it to. `claude -p` in its default text
mode prints **nothing at all** until the run is over, then prints the finished
answer in one block. `codex exec` narrates by default and needs nothing extra.

So Claude is launched with `--output-format stream-json --verbose`, which emits
one JSON object per step as it happens, and the app translates those events back
into lines:

```text
· model <id>                       the model actually in use
· <text>                           the agent's reasoning as it thinks
<text>                             the agent's own words
→ Read aicouncil/server.py         a tool call, with its main argument
← def do_GET(self): (+118 lines)   what came back, summarised
```

The **Draft** and **Senior review** tabs still show the agent's final answer
alone, not this transcript — the events carry both, and each pane gets the one
it wants.

If a future CLI release renames these flags, edit the field; nothing here is
compiled into the app. Clearing it is safe too — you simply get the old
all-at-the-end behaviour back, and the app stops trying to parse events.

### Quota

Each agent card carries a percentage chip showing the vendor's own figure, read
two different ways because the CLIs differ:

| Agent | Source | Freshness |
|---|---|---|
| Claude | `claude -p "/usage"` — the slash command, answered locally with no model call | Live; polled at launch and every 5 min |
| Codex | the `rate_limits` headers it writes to `$CODEX_HOME/sessions/*.jsonl` | As of the last Codex run; marked `*` when older than 30 min |

The chip leads with the **shortest window**, because that is the one that
bites first: Claude's 5-hour session, Codex's weekly. Hover for every window —
Claude's weekly is listed underneath, with `▸` marking the one on display.
Click to force a refresh.

Colour comes from the *worst* limit rather than the one displayed, so a weekly
at 91% turns the chip red even while the session sits at 12%, and a `!` says a
different limit is the constraint. Showing the short window without that would
be a comfortable number hiding an imminent wall.

Amber at 75%, red at 90%. At 85% a run warns first — a warning only, always
forceable, because the reading is a snapshot and only you know what the task
is worth.

### Choosing a model per stage

Click the model chip on either agent card to switch that stage's model. The
picker offers the configured list, the CLI's own default, and a free-text box
for anything else — typing a model adds it to the list for next time.

The picker asks each CLI what it can actually run. Codex publishes an
account-scoped list in `$CODEX_HOME/models_cache.json` — read live, so it
reflects your login's entitlements. Claude ships no such file, so the picker
offers its documented `--model` aliases. Nothing is hardcoded, deliberately: a
shipped list is wrong the moment a model is renamed, and wrong *per account*
regardless.

Aliases (`opus`, `sonnet`, `haiku`, `fable`) always resolve to the newest model
in that family; a pinned ID stays where you put it. The picker labels which is
which, because the difference only shows up months later when a pinned stage is
quietly running a superseded model.

Blank — the default — passes no `--model` flag at all, so each CLI uses
whatever it is configured for. That is the setting most likely to still be
correct in six months. A practical split: put the cheap, generous-quota model
on Stage 1 and spend the rationed one on Stage 2, which is where judgement
actually matters.

### Choosing a reasoning effort per stage

Beside the model chip is a second one for **reasoning effort** — the same knob
`/effort` sets in Claude Code and the reasoning selector sets in Codex. Depth
costs quota, and the two stages want different amounts of it: a junior sketching
an approach rarely needs what a senior verifying it against the real code does.

The levels are asked for, not shipped, for the same reason the model list is —
and here it matters more, because the legal set is **per model**. Codex
publishes `supported_reasoning_levels` for each model in the same
`models_cache.json` the model picker reads, along with its default and the
vendor's own one-line description of each level, so a model offering `ultra` and
one stopping at `xhigh` are told apart rather than averaged. Claude Code will
name its own levels if you hand it one it does not recognise, and does so
without reaching the model, so the picker simply asks — at no cost in tokens or
quota.

With no model pinned the CLI chooses one, so Codex's menu offers only the levels
*every* selectable model accepts. Pin a model to unlock the rest.

Changing the model re-checks the level you had set, and clears it if the new
model does not offer it. That check exists because the two CLIs fail
differently: Claude warns and falls back to its default, which is survivable,
while Codex rejects the run outright — minutes after launch, for a reason
nothing on screen would explain.

Blank — the default — passes no effort flag at all and lets each CLI use the
depth its vendor tuned for that model.

The flag itself lives in `effort_args` under **Command line** in Settings, next
to `model_args`, because there is no common spelling: Claude takes
`--effort high` and Codex takes `-c model_reasoning_effort=high`. A configured
command with no `effort_args` has no effort knob, and gets no chip — nothing is
guessed at, since a wrong guess would be read as the prompt or rejected.

---

## Architecture

```text
aicouncil/
├── __main__.py     Entry point, browser launcher, --doctor
├── server.py       http.server + SSE, token auth, Origin/Host validation
├── pipeline.py     The state machine: drafting → gate → polishing → complete
├── providers.py    CLI adapters: argv construction, streaming, cancellation
├── prompts.py      Role catalogue and stage prompt construction
├── gitutil.py      Repo status, diffs, snapshot/rollback & pull-request plumbing
├── config.py       Atomic JSON config with deep-merged defaults
├── events.py       Pub/sub bus with replay and per-subscriber backpressure
├── usage.py        Per-agent quota readings and the background poller
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

Standard library only. They drive real subprocesses, real sockets and real git
repositories in temporary directories rather than mocking them — the parts most
likely to break are exactly the ones a mock would paper over.

Coverage focuses on the properties that matter if they're wrong:

- Auto-approve flags reach the child process **if and only if** permission was
  granted, and never reach Stage 1.
- The approval gate is reached with a **pristine working tree**, and the
  configuration approved there is the one that runs — a settings change at the
  gate cannot swap the command about to be granted write permission.
- Rollback restores agent changes **without destroying pre-existing uncommitted
  work**, and a snapshot that failed is never offered as a rollback point.
- Assigning an agent to a job moves its command **and** its permission flag
  together, so the two can never be mismatched.
- Pull-request mode leaves the base branch **byte-for-byte as it was**, opens
  the PR against the branch that was checked out, and says where the work is
  when publishing fails.
- A continued run carries the earlier exchange to **both** stages and refuses a
  transcript from another repository.
- Cross-origin and bad-token requests are rejected; path traversal is blocked.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `codex`/`claude` shows **not found** | Not on `PATH`. Run `./scripts/install-deps.sh`, or set an absolute path in Settings. |
| Stage 2 finishes but the diff is empty | The CLI ran without write permission. With Zero-Touch off, you must click **Approve & execute** — that's what grants it. |
| "Missing session token" | The dashboard was opened without the launcher's URL. Restart with `./run.sh`. |
| Pull-request mode refuses to start | It says which precondition failed — a dirty tree, no `origin`, no git identity, or `gh` missing or logged out. Fix that one thing. |
| Run hangs, no output | The CLI is waiting on interactive input. Check its auto-approve arguments in Settings. |
| Port already in use | The server falls back to a free port automatically; read the URL it prints. |
| Stream shows "reconnecting" | The server stopped. It reconnects with backoff once it's back. |

---

## License

MIT — see [LICENSE](LICENSE).

<!-- commit bar verification -->
