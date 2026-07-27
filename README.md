# Theseus AI

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

There is also a third tab. **[Projects](#projects)** points three agents — an
architect, a developer and a QA specialist — at one folder and lets them work a
shared Kanban board with nobody approving anything, until the board is clear and
the build is green. Each turn the board decides who goes next: a failing build
outranks new features, an unverified build outranks a code review, and only a QA
turn can call anything green. It is the same machinery with the human taken out,
which is exactly why it takes a deliberate press of a button and names the
folder before it starts.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python **3.9+** | Standard library only — no `pip install`, no virtualenv, no build step |
| `git` | Optional — powers the diff viewer, safety snapshot and rollback when the working folder is a repository |
| A browser | Chromium-family gets a frameless app window; Firefox gets a plain window |
| `codex` CLI | Optional — Stage 1. See [Installing the agent CLIs](#installing-the-agent-clis) |
| `claude` CLI | Optional — Stage 2 |
| `agy` CLI | Optional — Google's Antigravity, assignable to either stage or to Chat |
| `gh` CLI | Optional — only for [Pull-request mode](#pull-request-mode). `./scripts/install-deps.sh --extras` installs it |

The app itself has **no dependencies at all**. If no agent CLI is installed
yet, everything still runs — point the providers at the bundled mock
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
Theseus AI v1.0.0
  python      : 3.12.3 (/usr/bin/python3)
  config      : /home/you/.config/ai-council/config.json
  runs        : /home/you/.config/ai-council/runs
  zero-touch  : off
  workspace   : /home/you/.config/ai-council/workspace (scratch)
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

The pipeline needs at least one agent CLI on your `PATH` — `codex` and `claude`
by default, with Google's `agy` a third option. All three vendors ship a
first-party installer that drops a standalone binary into `~/.local/bin` — **no
Node, no npm, no sudo**:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | bash
curl -fsSL https://claude.ai/install.sh | bash
curl -fsSL https://antigravity.google/cli/install.sh | bash   # optional
source ~/.bashrc
```

Or let the bundled script do it, which is the same thing plus a PATH check:

```bash
./scripts/install-deps.sh                # codex + claude, no sudo
./scripts/install-deps.sh --check        # report what's present, install nothing
./scripts/install-deps.sh --antigravity  # also agy (~190 MB, opt-in)
./scripts/install-deps.sh --extras       # also gh + python3-pip/venv (needs sudo)
./scripts/install-deps.sh --vscode       # also VS Code (implies --extras)
```

> Each installer pipes a remote script to `bash`. They are the official
> sources, but you can read them first:
> `curl -fsSL https://chatgpt.com/codex/install.sh | less`

Then authenticate each CLI once, interactively:

```bash
codex login     # browser login for ChatGPT Plus/Pro
claude          # browser login for Claude Pro
agy             # browser login with a Google account
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

1. **Optionally pick a working folder.** Click it in the status bar along the
   bottom, which also shows the branch and whether the tree is clean. Any
   folder will do, and none is a fine answer too: with nothing chosen, runs
   happen in a scratch folder of the app's own
   (`~/.config/ai-council/workspace`), which is what makes "just ask it
   something" work before you have configured anything. See
   [The working folder](#the-working-folder).
2. **Describe the task.** Be specific about files, behaviour and edge cases —
   the draft stage is only as good as its brief.
3. **Send it** with <kbd>Enter</kbd>, or the arrow button.
   <kbd>Shift</kbd>+<kbd>Enter</kbd> is a newline.
4. **Watch the council work.** The strip above the conversation shows which
   member is active. Each stage's answer arrives as a message in the thread;
   the raw stdout/stderr is in the **Console output** block beneath it.
5. **Review and approve.** The run pauses with a gate card sitting directly
   under the draft, and nothing yet written to disk. Optionally type a steer —
   it takes precedence over the draft — then click **Approve & execute**.
6. **Inspect the result.** A **Changes** block closes the turn, holding the
   real `git diff` per file with line numbers, and the commit bar.
7. **Roll back** if you don't like it. One click restores the tree exactly.
8. **Or keep going.** Just type again — the composer stays attached to the
   conversation. See [Continuing a run](#continuing-a-run).

### The three tabs

| Tab | What it is |
|---|---|
| **Council** | The two-stage pipeline: draft, approve, apply. Each member's CLI, model, effort and role are set by clicking it on the strip. |
| **Chat** | One agent, one conversation, no gate and no writes. Its CLI, model and effort are the pickers under the composer. |
| **Project** | An autonomous build. Three agents work a shared Kanban board against one folder until it is clear and the build is green. Nobody approves anything. See [Projects](#projects). |

Everything a run does lands in the conversation itself — the gate, the console
and the diff are blocks in the thread rather than tabs beside it, so each one
sits next to the exchange that produced it.

Council and Chat are two ways of running *one task*. Project is a different
thing on the same machinery: it runs for as long as the work takes, and only one
of the three can be working the folder at a time — the app refuses to start a
run while a project is going, and the reverse.

---

## The working folder

Both modes run their agents in one folder. Choosing it is optional, and it does
not have to be a git repository — the picker badges the ones that are, but any
folder can be selected, and **Use no folder** goes back to having none.

What the folder decides is which half of the safety model is available:

| Working folder | You get | You do not get |
|---|---|---|
| A git repository | Everything below | — |
| Any other folder | Draft, approval gate, console, conversations, and agents that can still write to it | Diff, safety snapshot, rollback, commit bar, pull-request mode |
| None chosen | The same, in `~/.config/ai-council/workspace` | The same |

None of that is enforced by refusing to start. The status bar says which
features the current folder is buying, a run with no diff to show names the
reason rather than implying it did nothing, and the approval gate tells you
before you approve whether a rollback point will exist. The one thing that *is* refused up
front is pull-request mode without a repository to branch from — checked before
either agent spends any quota.

The scratch workspace is a real directory you can open, not a temporary one:
whatever a run writes with no folder chosen is still there afterwards. It is
deliberately not a git repository, which is why a run there cannot be rolled
back.

> A folder inside a repository resolves to that repository's root, because the
> diff, the snapshot and the delivery branch all operate on the root. Picking
> `project/src` and picking `project` are the same choice.

---

## Continuing a run

A single task is rarely the whole conversation. The sidebar lists your
conversations grouped under Today, Yesterday and Previous 7 days, the way a
chat client does. Click one to open it in the thread — every message, what each
agent answered, the note you left at the approval gate, and the diff that
resulted. Opening it also attaches it to the composer, so typing again
continues that conversation rather than starting a new one. **New chat**
detaches and gives you an empty thread.

**Council and Chat keep separate histories.** Neither can be continued in the
other — the server refuses it — so each mode lists only its own conversations
rather than offering rows that clicking cannot act on.

Deleting is in two places. Hovering a row reveals a **×** that removes just
that conversation; **Settings → App → Conversations** has a button per mode
that clears the lot, each showing how many it would delete. Both confirm first,
both are immediate and permanent — the transcript is the only copy, there is no
bin, and neither touches your files or your git history.

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
- **The working folder is the authority, not the recollection.** A remembered run
  may since have been rolled back or edited over by hand, so the prompt says so
  and the old diff is deliberately not replayed — the working tree already
  carries it, more accurately.

Continuation only works within the folder the run started in.

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

It is the one thing that hands a CLI its auto-approve flag
(`--dangerously-skip-permissions` for `claude`,
`--dangerously-bypass-approvals-and-sandbox` for `codex`), and it applies in
both modes — but it means something slightly different in each, because only
one of them has a gate.

| | Zero-Touch **off** (default) | Zero-Touch **on** |
|---|---|---|
| **Council** | Pauses at the gate. Nothing is on disk yet; **Approve & execute** is what grants Stage 2 permission. | No gate. Runs start to finish unattended, Stage 2 writing as it goes. |
| **Chat** | Read-only. The assistant is invoked with its agent's read-only arguments (`--sandbox read-only`, `--permission-mode plan`) and can talk about the folder but not change it. | The assistant can create, modify and delete files, exactly as Stage 2 can. |

Chat has no approval gate — nothing stands between the message and the reply
for a human to review — so Zero-Touch is the *only* way to grant it write
permission. That is deliberate: it means "what does this repo do?" is safe to
ask by default, and arming it is a single, visible decision. The greeting says
which of the two you are in and turns amber when it is armed.

Four properties hold throughout, and they are covered by tests:

- **Stage 1 never receives an auto-approve flag.** It is read-only by contract
  regardless of the toggle.
- **Read-only and auto-approve are never both sent.** They are opposite grants;
  a provider gets one or the other.
- **The flags are never baked into the command template.** They live in a
  separate config field and are appended only when permission has actually been
  granted — so switching Zero-Touch off is sufficient to guarantee they are not
  passed.
- **Whatever writes is protected the same way.** The safety snapshot is taken
  immediately before it and the diff collected immediately after, whether that
  is Stage 2 or a Chat turn. A read-only run skips both, because reading a diff
  after one would report your own uncommitted work as the agent's.

> **Zero-Touch means what it says.** An agent will create, modify and delete
> files in your working folder with no further confirmation. Use it on a
> branch, keep Safety Snapshot on, and don't point it at anything you can't
> afford to lose.

### Other toggles

The gear beside the composer carries the two that change per run — Zero-Touch
and Pull request. The rest live in **Settings → Run**, split by what they
decide: how the next run behaves, and where the work lands. All of them are
Council-only, and the gear says so rather than going quiet in Chat, which has
no gate and no branch for them to decide anything about.

| Toggle | Group | Effect |
|---|---|---|
| **Pull request** | Delivery | Deliver the run on a branch of its own and open a GitHub PR instead of writing to the checked-out branch. See below. |
| **Require clean tree** | Delivery | Refuse to start if the repo has uncommitted changes. Pull-request mode enforces this itself, on or off. |
| **Safety snapshot** | Delivery | Capture the worktree before Stage 2 so **Roll back** works. Leave on. |

---

## Council or Chat

The selector centred at the top decides which of two things your next message
starts. It is the first choice, above everything it changes, because the two
share almost nothing. (The third tab, Project, is a placeholder and starts
nothing at all — it is deliberately not written to config, so the app never
comes back up sitting in a mode it cannot run.)

**Council** is the pipeline this README is mostly about: Junior Draft →
approval gate → Senior Polish, with the console, the diff, delivery controls,
snapshots and rollback.

**Chat** is one assistant answering one message, the way opening `claude` or
ChatGPT is. It has:

- **Its own agent**, picked from the first chip under the composer, beside its
  model and reasoning level — switching CLI mid-conversation is one click, not
  a visit to Settings. It borrows neither council stage, so Chat can run Codex
  while the council runs Claude.
- **No behaviour by default.** With the **Behaviour** box empty and no thread
  to replay, your message reaches the CLI *exactly as typed* — no persona, no
  house rules, no folder preamble. Type something into that box and it is
  put in front of the message; that is the whole of it.
- **No draft and no approval gate**, and no member strip — just the message
  and the reply.
- **Read-only until you say otherwise.** By default Chat is invoked with its
  agent's read-only arguments (`--sandbox read-only` for `codex`,
  `--permission-mode plan` for `claude`, `--mode plan` for `agy`) and never
  receives an auto-approve flag. Turn Zero-Touch on and it can change files,
  takes a snapshot first and shows the diff afterwards, exactly as the
  council's writing stage does. See [Zero-Touch mode](#zero-touch-mode).

Conversations from the two modes are kept apart, in the sidebar and on the
wire: each mode lists only its own, continuing one switches the selector to the
mode it was held in, and the server refuses the mismatch outright rather than
replay a council transcript into a plain chat.

Configurations written before this existed are migrated on load. The old
**Solo mode** toggle becomes the mode; the stage the old **Solo mode runs**
selector pointed at becomes the initial Chat assistant, keeping its CLI, model
and reasoning level but not its council role. The mode is still stored as
`solo` on disk — only what the tab is called has changed.

---

## Projects

The other two tabs run one task. Projects runs a *build*: you write a goal, pick
a folder, and three agents work a shared Kanban board against it until the board
is clear and the build is green — across far more turns than any one of them
could hold in a single context window.

### It is a board, not a pipeline

There is no step 1, step 2, step 3. Every turn the engine reads
`.theseus/BOARD.json` and asks one question — *what does this project need
next?* — and the answer picks the agent:

```text
  build FAILING ........ the developer fixes it
  build UNKNOWN ........ QA builds and tests it
  cards in review ...... the architect reviews the diff
  cards in backlog ..... the developer claims the top one
  board clear, green ... the architect proposes what is missing
  nothing left ......... COMPLETED
```

That ordering *is* the design, and it is the part worth arguing about:

- **A failing build outranks everything.** No new features while it is red.
- **An unverified build outranks a code review.** Reviewing code nobody has
  compiled wastes the reviewer's turn, and half the time reviews something that
  does not build.
- **A review outranks starting more work**, so the queue cannot run away from
  the person who has to read it.
- **Only a QA turn can set the build to PASSING**, and the engine sets it back
  to UNKNOWN the moment anyone writes code. A green board therefore always
  means somebody ran the tests *after* the last edit.

Bugs are cards too. QA raises them off a build failure, and they jump the
developer's queue ahead of the backlog.

### The three chairs

| Chair | Default CLI | Fires when | What it does |
|---|---|---|---|
| **Architect** | `claude` | a goal needs decomposing, cards are in review, or the board is clear | Turns the goal into cards, reviews diffs and approves or bounces them, and proposes enhancements once the goal is met. |
| **Developer** | `codex` | there are cards in the backlog, or the build is failing | Claims the top card, writes the code and its tests, fixes build failures. One card per turn. |
| **QA** | `agy` | at startup, and whenever code has changed | Audits the workspace read-only, then runs the project's real build and test commands and captures the output verbatim. |

That pairing follows the work rather than the vendor — judgement to the best
reasoner, bulk implementation to the most generous quota, build-and-verify to
the one happiest running commands — and none of it is a rule. Click any chair in
the matrix to reassign it, exactly as you would a council member. The three are
ordinary providers under the hood, so the model picker, the reasoning-depth
picker and the quota chip all work on them unchanged.

If a chair's CLI is not installed, the project **will not start**, and says
which one. A build that runs unattended for an hour should not discover on its
fourth turn that its QA binary was never there. `agy` in particular is opt-in —
install it with `scripts/install-deps.sh --antigravity`, or move QA to a CLI you
already have.

### Pointing it at code that already exists

Most projects are not empty folders, and this one is built for that case.

**The first turn is read-only.** Before anything is written, the QA chair is
invoked with its read-only flags and *no auto-approve grant at all*, and asked
what is already here: the layout, the configs, what of the goal exists, and
anything that would break if an agent touched it. Nothing can change during that
turn, by construction rather than by instruction.

**It adopts your build, it does not invent one.** The engine reads the tooling
off disk itself — `go.mod`, `package.json`, `Cargo.toml`, `pyproject.toml`,
`Makefile` and friends — and puts the real commands on the board. QA runs
`go test ./...` because that is what this project uses. `package.json` scripts
are read rather than guessed, because `npm test` against a package with no test
script fails in a way that looks exactly like a broken build.

**A repository that arrives red is known to be red.** A workspace with existing
build tooling gets one baseline verification before any card is worked, so a
build that was already failing when you pointed at it is your starting position
rather than something discovered three cards later and blamed on the developer.
An empty folder skips that, because verifying an empty directory reports a
failing build and sends the developer off to fix a project that does not exist.

**Edits are surgical.** The developer is told to change the lines that need
changing in the files that need changing — not to rewrite a file it was only
meant to edit, not to reformat code it did not touch. A whole-file rewrite
destroys the diff the reviewer reads.

**`.theseus/` is added to `.gitignore`** on the way in, if the folder is a git
repository, by appending — never by rewriting a file that is already yours. The
engine's working state stays out of your pull request.

### Permission: there is no gate

A project writes. Apart from that first audit it cannot do anything else — so
**every turn after the audit is invoked with its agent's auto-approve flags**,
and no setting changes that. Zero-Touch is not involved; pressing **Start
project** *is* the grant. That is why the confirmation names the folder: it is
the one thing that turns a misclick into a question.

A git snapshot is taken before the first write where there is one to take, so an
entire build can be undone. In a folder that is not a repository there is nothing
to snapshot and the app says so before you start.

### What it writes

Inside the project root:

| Path | Owner | What it is |
|---|---|---|
| `.theseus/BOARD.json` | the engine | The board: goal, build health, the four columns, the last build output, the detected tooling. |
| `.theseus/CRITIQUE.log` | the agents | Append-only: every build failure, review finding and verification result. |
| `.theseus/SPEC.md` | the architect | Design notes, when the design needs explaining. |

Everything else it writes is your project's own source.

The split matters. The engine owns `BOARD.json` and is its only writer, so there
is always one authoritative record of where the run is; the agents own the prose
files, because prose tolerates three writers and a state machine does not. Cards
move by an agent *reporting* the move in a fenced JSON block at the end of its
turn, which the engine parses and applies.

Omission is never deletion: a card an agent forgets to mention is left exactly
where it was. One careless reply cannot wipe the board.

### Surviving a context limit

No turn depends on the previous turn's conversation. Every prompt is rebuilt
from the board and the working diff on disk — never from a transcript — so an
agent that dies mid-turn costs one turn, not the run. Terminal output from
earlier turns is never replayed to anyone: it is the largest thing that could be
sent and the least useful per token.

When a turn fails in a way that reads as exhaustion (a token or quota limit, a
timeout, a non-zero exit with the right words in it), the engine sets
`continuation_needed`, hands that *same turn* to a different chair, and carries
on. The replacement starts from the board, not from a conversation it never saw.
That is the reason the roles are three independent providers rather than one CLI
asked three different things.

This is a heuristic on the CLIs' own prose — none of them has a distinct exit
code for "out of context" — and it is treated as one: a false positive costs a
single hand-off, which is survivable.

### The controls

- **Pause** stops the loop *after* the running agent finishes its turn. It does
  not interrupt: a CLI killed between two file writes leaves a tree nothing in
  the run knows is half-written. It costs a few minutes and leaves the folder
  consistent.
- **Resume** picks up from the board.
- **Hand off** forces the next turn onto a chair you choose. For the failure the
  board cannot see — an agent answering, exiting zero, and going in circles.
- **Stop** ends it now, killing whatever is executing. Use Pause unless you mean
  that.

Close the window mid-build and the project is still on disk; reopening the app
finds it and offers to resume. A project that is still running when you open the
app pulls you to the tab, because an idle-looking window while three agents
rewrite a folder is the wrong thing to show.

### Proactive innovation

The slider on the initializer decides how much the council may invent once your
goal is met and the build is green. At **zero** it builds what you asked for and
stops — the right setting for a repository you care about. Above zero, the
architect proposes two or three enhancements, they become ordinary backlog
cards, and the developer builds and QA verifies them exactly as it did the rest.
Cards the council invented are tagged **idea** on the board and counted
separately in the completion note, so "what did it do that I did not ask for?"
is answerable at a glance.

An architect that honestly has nothing worth adding returns no cards, and the
run ends there rather than inventing work to fill the budget.

### Bounds

Under **Settings → Run → Projects**, because the loop has no human in it and
without them a project that cannot make progress will keep spending quota on the
same failure until somebody notices:

| Setting | Default | What it stops |
|---|---|---|
| Step limit | 40 | Total agent turns before it stops and says so. |
| Fix attempts | 3 | Failing builds in a row before it gives up rather than handing back the same trace again. |
| Innovation rounds | 2 | Where the initializer's slider starts. |

There is also an unconfigurable one: if three consecutive turns leave the board
completely unchanged — a reviewer giving no verdict, a developer claiming a card
and writing nothing — the run stops and says the board is not moving. Three
agents can all run cleanly and make no progress, and the step limit is too blunt
an instrument to notice that an hour early.

Hitting a limit is not data loss: everything built is on disk, the board says
where it got to, and the project can be resumed once you have unstuck it.

### Trying it without spending quota

`scripts/mock-agent.py` speaks the project protocol. Point all three chairs at
it and the whole decision loop runs for real against a real directory: it audits
the folder read-only, plans two cards, implements a Python module **with a
genuine bug in it**, fails its own test suite, is handed the real trace, fixes
it, gets the cards reviewed and proposes one enhancement before declaring itself
finished. Nothing in that sequence is faked — the tests really run and really
fail — which is the only way to know the loop works rather than to believe it.

---

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

Snapshotting **fails closed**. If any part of it fails — or there is nothing
to anchor to, because the folder has no commits yet or no git at all — the run
is told so in the live stream and
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
Claude, Antigravity, or Custom command. Claude can draft and Codex can be the
senior; the same agent can hold both jobs.

**Antigravity** is Google's `agy`, which replaced Gemini CLI for personal
accounts on 18 June 2026. Install it with
`curl -fsSL https://antigravity.google/cli/install.sh | bash` and run `agy`
once to sign in with a Google account; the binary lands in `~/.local/bin`. It
differs from the other two in three ways worth knowing:

- **The prompt is a flag's value**, not an argument of its own — hence the
  `--prompt={prompt}` template. Do not rewrite it as `--print {prompt}`: the
  model and permission flags are inserted immediately ahead of the prompt, and
  `--print` would take `--model` as the thing to answer.
- **No streaming format.** `agy` has no `--output-format`, so its answer is
  read as the plain text it is.
- **No quota reading.** It publishes no equivalent of `claude /usage`, so its
  member shows *no quota data* rather than a number this app made up.

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

The Chat assistant has its own entry in the same list, with its own display
name, command and optional **Behaviour**. It has no Role, because it is not a
stage in anything, and no Agent dropdown: the CLI Chat runs is the first chip
under the composer, where the same swap happens on one click. Everything the dropdown
would have written — command, permission flags, a cleared model and reasoning
level — is written by the server either way.

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
| Antigravity | — it publishes no quota anywhere this app can read | Shows *no quota data*, never an invented number |

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

In Chat, click the model chip under the composer. In Council, click a member on
the strip and choose **Model**. The picker offers the configured list, the
CLI's own default, and a free-text box for anything else — typing a model adds
it to the list for next time.

The picker asks each CLI what it can actually run. Codex publishes an
account-scoped list in `$CODEX_HOME/models_cache.json` — read live, so it
reflects your login's entitlements. Antigravity has a subcommand for it, so the
picker runs `agy models` and shows exactly what it prints (which includes
Anthropic and open-weight models served through Antigravity, not just Gemini
ones). Claude ships neither, so the picker offers its documented `--model`
aliases. Nothing is hardcoded, deliberately: a shipped list is wrong the moment
a model is renamed, and wrong *per account* regardless.

Aliases (`opus`, `sonnet`, `haiku`, `fable`) always resolve to the newest model
in that family; a pinned ID stays where you put it. On its own, though, an
alias does not say *which* generation you are about to run, so the picker shows
what each one points at right now — `opus → claude-opus-5` — and the toast
confirms it when you choose. That reading comes from `claude` itself: the CLI
expands the alias locally, before it opens a connection, and names the result
in the first line of its `--output-format stream-json` handshake. The app reads
that line and kills the process, so the prompt is never sent and the check
costs nothing. If the CLI cannot be reached the aliases are still offered,
unlabelled — the list is not wrong, it is just less specific.

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

Antigravity takes `low`, `medium` or `high`, and will name them the same way
Claude does when handed a value it does not know. It also does something
neither of the others do: **every model `agy models` lists is already a
complete selection**, and it refuses `--effort` beside one. Sometimes that is
because the level is in the name (`gemini-3.6-flash-high`) and sometimes
because the model has no such knob at all (`claude-sonnet-4-6`) — the list is
what decides, not the shape of the name. Pick a listed model and the effort
menu says so instead of offering a level; pick one while a level is set and the
level is cleared for you.

To choose the effort yourself, type a **base** name the list does not show —
`gemini-3.6-flash` rather than `gemini-3.6-flash-high` — into the model box.
The CLI accepts those with `--effort`; it just does not enumerate them.

Changing the model re-checks the level you had set, and clears it if the new
model does not offer it. That check exists because the CLIs fail differently:
Claude warns and falls back to its default, which is survivable, while Codex
and Antigravity reject the run outright — for Codex minutes after launch, for
a reason nothing on screen would explain.

Blank — the default — passes no effort flag at all and lets each CLI use the
depth its vendor tuned for that model.

The flag itself lives in `effort_args` under **Command line** in Settings, next
to `model_args`, because there is no common spelling: Claude and Antigravity
take `--effort high`, Codex takes `-c model_reasoning_effort=high`. A configured
command with no `effort_args` has no effort knob, and gets no chip — nothing is
guessed at, since a wrong guess would be read as the prompt or rejected.

---

## Architecture

```text
aicouncil/
├── __main__.py     Entry point, browser launcher, --doctor
├── server.py       http.server + SSE, token auth, Origin/Host validation
├── pipeline.py     The state machine: drafting → gate → polishing → complete
├── projects.py     The board-driven autonomous loop and its .theseus/ board
├── providers.py    CLI adapters: argv construction, streaming, cancellation
├── prompts.py      Role catalogue, stage prompts and the project phase prompts
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
  transcript from another folder.
- Both modes start with no working folder and in a folder with no git; what
  those runs lose is the diff, the snapshot and pull-request delivery, and a
  config written when the folder was a mandatory repository still migrates.
- Cross-origin and bad-token requests are rejected; path traversal is blocked.
- A project runs **the whole decision loop against a real directory** with the
  mock agents: it writes real files, its test suite really fails, the trace
  really reaches the developer, and the fix really passes. Every unit test
  around that one would still pass if the turns had quietly stopped connecting.
- The **ordering policy** is pinned directly by handing the engine a board and
  asking what it would do next — a failing build outranks a review, a review
  outranks new work, a bug jumps the queue — so a reordering cannot pass as a
  refactor.
- A corrupt or hand-mangled `BOARD.json` resumes rather than crashing a worker
  thread, an unreadable `build_health` resumes as **unknown and never passing**,
  and an agent's silence about a card is never read as deleting it.
- QA reporting no build status is treated as **failing, never passing** — a
  silent verification turning into a green build is the one outcome the loop
  must not build on top of. Writing code sets the build back to unknown, so a
  green board cannot survive an edit.
- The first turn of a run is verified to be **read-only against a real
  invocation**: no write grant, and no files modified.
- `.theseus/` is appended to an existing `.gitignore` rather than replacing it,
  and never added twice.
- Build tooling is **read off disk, not guessed** — `package.json` scripts that
  do not exist are never run.
- A project and a run refuse each other, in both directions.

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
