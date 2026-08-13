# Theseus AI

A local, dark-themed desktop dashboard that runs a **deliberating council**
across your existing AI subscriptions — with **zero per-token API cost**.

```text
   Your task
       |
       v
  ┌──────────────────┐   read-only    ┌──────────────────┐   read-only    ┌──────────────────┐   writes to disk
  │     Members      │ ── answer ───> │     Critique      │ ── weigh ───> │     Chairman      │ ──> your repo
  │ (one seat per     │                │ (each member      │                │ (decides, then    │
  │  agent CLI)       │                │  reviews the      │                │  applies)         │
  │                   │                │  others, blind)   │                │                   │
  └──────────────────┘                └──────────────────┘                └──────────────────┘
                                                                   ^
                                                                   └── approval gate (unless Zero-Touch is on)
```

Every agent you have added gets its own seat. Each member shells out to its CLI
(your ChatGPT Plus/Pro, Claude Pro, or other existing subscription) to answer
the task independently and read-only. Each then critiques the others'
anonymised answers. A chairman — one of the same agents, routed by the council
— weighs every answer and every critique, decides, and, once permitted, is the
only seat that writes to disk.

The chairman is explicitly instructed to treat every answer and critique as
**untrusted input** — a colleague's opinion, not a specification. That framing
is deliberate: the main failure mode of a naive multi-model chain is the
deciding model politely rubber-stamping a confidently-wrong answer.

Which agent sits in which seat, and which one chairs, is configurable from
Settings, including the same agent in more than one seat. See
[Assigning agents to seats](#assigning-agents-to-seats).

**Nothing in this application reads, stores or transmits an API key.** It
drives the CLIs you have authenticated, so the marginal cost of a run is zero.
Signing in from Settings → Agents runs the vendor's own login command and the
account stays with their CLI — there is no field anywhere in this app that
takes a key or a password.

**No agent is required, and none is preferred.** Which AI you use is your
choice: add one, two or all three in [Settings → Agents](#connecting-your-agents),
in whatever combination you have access to. Installing a CLI does not add it —
a machine that carries all three binaries and a subscription for one still runs
only what you chose.

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
| At least one agent | To do AI work. **Which one is entirely your choice** — see [Connecting your agents](#connecting-your-agents) |
| `gh` CLI | Optional — only for [Pull-request mode](#pull-request-mode). Settings → Agents → GitHub installs and connects it, or `./scripts/install-deps.sh --gh` (rootless, into `~/.local/bin`) |

The app itself has **no dependencies at all**, and it ships configured for
nobody: on a fresh install no agent is added, so the first thing the dashboard
asks is which ones you want. Three are catalogued — Codex, Claude and
Antigravity — and adding one, two or all three is the same amount of work.
Nothing here treats any of them as the default.

Everything that is not an AI run works before you add anything, and the full
pipeline is exercisable end to end without a vendor CLI at all: point a chair at
the [bundled mock agent](#trying-it-without-the-clis).

---

## Quick start

```bash
git clone https://github.com/andr9595/ai-council.git
cd ai-council
./run.sh
```

That is the whole install. `run.sh` locates a Python 3.9+ interpreter, starts a
loopback-only server on port 8760, and opens the dashboard in a browser window.

Then open **Settings → Agents** and add whichever AI you have access to. The
panel installs the CLI and signs it in for you if you have not already — see
[Connecting your agents](#connecting-your-agents). Nothing else needs
configuring to start.

Check what the app can see:

```bash
./run.sh --doctor
```

```text
Theseus AI v1.0.0
  python      : 3.12.3 (/usr/bin/python3)
  config      : /home/you/.config/ai-council/config.json
  runs        : /home/you/.config/ai-council/runs
  mode        : council
  zero-touch  : off
  workspace   : /home/you/.config/ai-council/workspace (scratch)
  pull request: off

Providers:
  [MISS] council_codex  Codex    codex      -> not found on PATH
  [MISS] council_claude Claude   claude     -> not found on PATH
  [MISS] council_agy    Antigravity agy        -> not found on PATH
  [MISS] (retired)      Codex    codex      -> not found on PATH
  [MISS] (retired)      Claude   claude     -> not found on PATH
  [MISS] Chat assistant Claude   claude     -> not found on PATH
  [MISS] Project: arch  Claude   claude     -> not found on PATH
  [MISS] Project: dev   Codex    codex      -> not found on PATH
  [MISS] Project: QA    Antigravity agy        -> not found on PATH

  Agents added: none yet.
  Add, install or sign in to one in Settings -> Agents.
```

Every configured provider gets a row, not only the three council seats: the two
retired stages, the Chat assistant and the three project chairs are all listed,
because a project that cannot start for want of `agy` is exactly what this
command exists to find first.

There are three marks, not two, because there are two separate ways for a chair
to be unfillable:

| Mark | Meaning |
|---|---|
| `[OK  ]` | Added, installed and ready to be seated |
| `[OFF ]` | Not added. The binary may well be installed — you have not asked to use it |
| `[MISS]` | Added, but its executable is not on `PATH` |

The trailing count is printed only when an *added* agent is missing its CLI:
one you never added is not a problem to fix by installing something.

### Launcher flags

| Flag | Effect |
|---|---|
| `--doctor` | Report environment and CLI availability, then exit |
| `--host IP` | Bind address, default `127.0.0.1`. Loopback only — `127.0.0.1`, `localhost` and `::1` are accepted and anything else exits with status 2, because the app can run an agent with auto-approve flags. |
| `--no-browser` | Start the server without opening a window |
| `--port N` | Preferred port (falls back to a free one if taken) |
| `--print-url` | Print only the dashboard URL, then serve |
| `--version` | Print the version, then exit |

---

## Connecting your agents

**Settings → Agents** is where you say which AI this install uses. There is one
card per catalogued CLI, each with four states that are deliberately kept
apart — because a setup screen that reports "ready" on the strength of
`--version` succeeding is how you end up with a run that fails at launch:

| State | What it means |
|---|---|
| **Not added** | Theseus will not seat it. This is where every agent starts |
| **CLI not installed** | You added it; its binary is not on `PATH` yet. The card offers **Install the CLI** |
| **Not signed in** | Installed, and the vendor says there is no account behind it. The card offers **Sign in** |
| **CLI found** | Installed, and its vendor offers no way to ask — Antigravity is the only one. Treated as maybe-signed-in rather than as a red cross |
| **Signed in** | Ready. Its model is picked from the card's own **Default model** list, under **How each CLI is run** just below |

Adding is the only thing that seats an agent. Installing a CLI does not, and
neither does having installed it years ago for something else.

### Add, install, sign in

1. Run `./run.sh` and open **Settings → Agents**.
2. Press **Add** on each agent you have access to. Any combination is fine, and
   so is one.
3. If its CLI is missing, press **Install the CLI**. That runs
   `scripts/install-deps.sh --agent <name>`, which pipes the vendor's own
   first-party installer to `bash` — a standalone binary into `~/.local/bin`,
   **no Node, no npm, no sudo**. The output is shown as it runs.
4. Press **Sign in**. That runs the vendor's own login command in a terminal the
   app owns and surfaces the URL it prints as a button. The browser flow is
   theirs, the account is theirs, and the token it writes is theirs.
5. Optionally pick a **Default model** and a **Reasoning effort**, on that CLI's
   card under **How each CLI is run** directly below. Both lists are read from
   the CLI, so they reflect your account's entitlements rather than a catalogue
   this README would have to keep in step — and the effort list follows the
   model you pick, because which levels are legal is the model's answer. A model it does not report — a preview, or one only your org
   has — goes in **Selectable models** under **Command line**, and is then
   offered in the same list.

**No API keys, ever.** There is no field in this app that takes one. These are
**subscription logins**, which is what keeps runs at zero per-token cost —
setting an API key instead would put every run on metered billing. Theseus
stores only which agents you added, each one's model and reasoning depth, and
the command templates. Credentials live in each vendor's own config directory.

The one thing that is yours to check: a CLI is launched with your environment,
so an `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` already exported in your shell is
one the CLI can decide to bill against. Theseus never reads, sets or asks for
one — but it does not unset yours either, because a key you exported on purpose
is not this app's to remove. Sign in through the panel and leave those unset if
zero per-token cost is the point.

> Antigravity is the exception to step 4: `agy` 1.1.12 has no `auth` subcommand
> and signs in inside its full-screen session, which a scrollback pane cannot
> honestly draw. Its card hands you the command to paste into a terminal
> instead of pretending otherwise.

### Or from the terminal

Nothing above is required. The same work is three commands, and the app reads
the result either way:

```bash
./scripts/install-deps.sh --agent codex    # ChatGPT Plus/Pro/Business
./scripts/install-deps.sh --agent claude   # Claude Pro/Max
./scripts/install-deps.sh --agent agy      # Google account (~190 MB)
source ~/.bashrc
```

Passing no `--agent` installs no agent — it prints the choices and stops. Other
flags:

```bash
./scripts/install-deps.sh --check        # report what's present, install nothing
./scripts/install-deps.sh --extras       # also gh + python3-pip/venv (needs sudo)
./scripts/install-deps.sh --vscode       # also VS Code (implies --extras)
```

Or skip the script entirely and use each vendor's installer directly:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | bash
curl -fsSL https://claude.ai/install.sh | bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

> Each installer pipes a remote script to `bash`. They are the official
> sources, but you can read them first:
> `curl -fsSL https://chatgpt.com/codex/install.sh | less`

Then sign in once, interactively:

```bash
codex login                     # browser login for ChatGPT Plus/Pro
claude auth login --claudeai    # browser login for Claude Pro/Max
agy                             # sign in inside the session, Google account
```

You still have to **add** each one in Settings → Agents afterwards: a CLI on
`PATH` is not a request to use it. Confirm with `./run.sh --doctor`.

### Removing an agent

Press **Remove**. Nothing is destroyed — the CLI, its login, and that agent's
model and reasoning depth all stay exactly where they were, and adding it back
restores it. What changes is that it stops being seated on the council, stops
appearing in Chat's multi-agent answer, and stops being offered in the pickers.
If it was holding the Chat assistant or a project chair, that chair moves to an
agent you kept, and the app says which.

If you remove the last one, nothing is broken — the dashboard, history, diffs
and the working folder all still work. What refuses is starting an AI run, and
it refuses by saying so before spending anything.

### Trying it without the CLIs

A mock agent ships in `scripts/`. It streams realistic Markdown and writes a
real file, so the full Deliberate → Critique → Approve → Synthesize → Diff →
Rollback loop works with no vendor CLI and no account at all.

In Settings → Agents, **Add** any agent — its CLI does not have to be installed
and you do not have to sign in. Then under **How each CLI is run**, open that
card's **Command line** and replace the command with (one argument per line):

```text
python3
/absolute/path/to/ai-council/scripts/mock-agent.py
{prompt}
```

A hand-written command is nobody's catalogued agent, so it answers to nobody's
selection and runs on its own terms. A council turn is recognised by what its
prompt asks for, not by a flag, so the same command works whichever seat it
lands in.

---

## Using it

1. **Optionally pick a working folder.** Click it in the status bar along the
   bottom, which also shows the branch and whether the tree is clean. Any
   folder will do, and none is a fine answer too: with nothing chosen the
   council still seats, deliberates, critiques and delivers a verdict, but
   nothing is written anywhere — code comes back in the reply instead. That is
   what makes "just ask it something" work before you have configured
   anything. See [The working folder](#the-working-folder).
2. **Describe the task.** Be specific about files, behaviour and edge cases —
   every seat answers the brief you wrote, and no council is better than it.
3. **Send it** with <kbd>Enter</kbd>, or the arrow button.
   <kbd>Shift</kbd>+<kbd>Enter</kbd> is a newline.
4. **Watch the council work.** The strip above the conversation shows which
   member is active. Each stage's answer arrives as a message in the thread;
   the raw stdout/stderr is in the **Console output** block beneath it.
5. **Review and approve.** The run pauses with a gate card sitting directly
   under the deliberation, and nothing yet written to disk. Optionally type a
   steer — it takes precedence over every member — then click
   **Approve & execute**.
6. **Inspect the result.** A **Changes** block closes the turn, holding the
   real `git diff` per file with line numbers, and the commit bar.
7. **Roll back** if you don't like it. One click restores the tree exactly.
8. **Continue it if it fell over.** A failed run keeps every answer it already
   got and offers **Continue**, which re-runs only the stage that failed. See
   [Continuing a run that failed](#continuing-a-run-that-failed).
9. **Or keep going.** Just type again — the composer stays attached to the
   conversation. See [Continuing a run](#continuing-a-run).

None of that has to be kept. The hat-and-glasses button at the top right starts
an [incognito](#incognito) conversation, which is written neither to the Chats
list nor to the agents' own session history.

### The three tabs

| Tab | What it is |
|---|---|
| **Council** | Members answer independently, critique each other anonymised, then a chairman decides and applies. Each seat's CLI, model, effort and role are set by clicking it on the strip. |
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
| Any other folder | Deliberation, critique, approval gate, console, conversations, and a chairman that can still write to it | Diff, safety snapshot, rollback, commit bar, pull-request mode |
| None chosen | Deliberation, critique, console, conversations, and a verdict with its code written into the reply | Anything that touches a file: the chairman's edits, the approval gate, and all of the row above |

**No folder chosen means nothing is written.** Not by the council, not by Chat,
and not by Zero-Touch — that is the point of the choice, and it is what makes
"what would you three do about X?" a question you can ask without picking a
directory first. The council still runs in full; the chairman is invoked
read-only and asked for the answer, with any code in fenced blocks per file.
There is no approval gate on such a run, because there is nothing to approve.
Runs happen in a scratch folder of the app's own
(`~/.config/ai-council/workspace`) purely because a process has to start
somewhere; it is not the subject of the task and the agents are told so.

None of that is enforced by refusing to start. The status bar says which
features the current folder is buying, a run with no diff to show names the
reason rather than implying it did nothing, and the approval gate tells you
before you approve whether a rollback point will exist. The one thing that *is* refused up
front is pull-request mode without a repository to branch from — checked before
either agent spends any quota.

The scratch workspace is a real directory you can open, not a temporary one.
Projects is the one mode that still builds in it with no folder chosen — a
project has to put a codebase somewhere — and whatever it writes there is still
there afterwards. It is deliberately not a git repository, which is why work
done there cannot be rolled back.

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

Opening a conversation — or finishing a run — attaches it to your next
message, and the banner above the composer says which one. The follow-up is a
**new run** with its own transcript, approval gate and rollback point; nothing
about the earlier one is overwritten. Every seat is given the thread, so each
member answers with the earlier reasoning in view, each critique knows what was
already argued, and the chairman sees what it already told you.

Two deliberate limits:

- **It replays the council's transcript, not the CLI's session.** A stage can
  be any command you configure, and a custom one has no session to resume.
  Replaying the transcript works for every agent identically.
- **The working folder is the authority, not the recollection.** A remembered run
  may since have been rolled back or edited over by hand, so the prompt says so
  and the old diff is deliberately not replayed — the working tree already
  carries it, more accurately.

Continuation only works within the folder the run started in.

### Continuing a run that failed

Different thing, same word. Above is continuing a *conversation*, which starts
a new run. This is finishing *one* run that stopped halfway.

A council run is five or seven agent invocations, and the expensive ones come
first. When the chairman falls over — a quota wall, a timeout, a CLI that dies
in its own harness — every member position and every peer critique has already
been paid for. Starting again spends all of it a second time to get back to
where it stopped.

A failed run therefore offers **Continue**, in the top bar and beside the
failure in the thread. Its tooltip says exactly what that costs:

```text
Runs Chairman (Claude) again, reusing 4 answers already given.
```

Clicking it runs the unfinished stages and replays the rest from the record. It
is the same run — same id, same transcript, same thread — not a new one.

What continuing does and does not carry over:

- **Answers already given are reused, not re-asked.** A stage that produced an
  answer is replayed from the transcript. Only what failed is dispatched again.
- **A failed chairman's own half-answer is carried into the next attempt.**
  The one it lost is the one that was applying the outcome, so what it had
  written — and the reason it stopped — is quoted to the chairman that
  continues, labelled as an attempt that did not finish. That is what stops the
  second one redoing work the first had already started or contradicting it.
  The folder is still the authority: the attempt is recollection, and the new
  chairman is told to check it against the tree rather than trust it. A verdict
  that *answered* is never quoted this way — a chair re-run because a member it
  quoted changed is stale, not failed.
- **The approval gate is not asked twice.** If you already approved this run,
  the chairman keeps the write permission you granted — the bench and the
  positions it was approving are exactly the ones being reused. A run that
  failed *before* the gate still stops there.
- **The safety snapshot is the first attempt's.** Rollback after a
  continuation undoes the whole run, including anything the failed chairman had
  already written.
- **Providers and roles are re-read from Settings; nothing else is.** This is
  the one place a run's frozen configuration is deliberately refreshed: the
  usual reason to continue is that a seat hit its quota, and the usual fix is
  to point that seat at another model or another CLI first. Zero-Touch,
  pull-request delivery, the snapshot setting and the bench itself stay as they
  were — the bench especially, because a run whose seats moved halfway through
  would leave a transcript nobody can read.
- **It survives a restart.** The button also appears on a failed run opened
  from history, so closing the app — or restarting it to install the CLI update
  that fixes the failure — costs nothing. The answers are on disk. The
  exception is a pull-request run, which is refused after a restart: its
  branch, its commits and possibly a published PR live outside the transcript,
  and half-reconstructing that would be a guess about your repository.

Chat can be continued the same way. There is only one stage, so nothing is
reused — but the message, the thread and the folder are still there, which is
the retyping it saves.

The sidebar marks the rows worth going back to: a conversation that stopped
halfway reads **· can continue** under its title, so finding it does not mean
opening every failed run in the list.

### Running one seat again

Continue finishes a run that stopped. This replaces an answer you have and do
not want — a seat that timed out into two lines, hit its wall mid-sentence, or
simply argued badly — without disturbing the seats that did fine. Hover any
answer in the thread and an **again** button appears on its card. It is offered
on finished runs of either kind, so the common case Continue cannot reach —
*the council carried on with two of three, the chairman answered, and the run
says `complete`* — is one click rather than a whole new run.

What it re-runs is not only the seat you clicked, and the button says so before
you click it:

| You run again | So does | Why |
|---|---|---|
| A member's position | Every peer critique, and the verdict | Each critique quotes every other position, and the chairman quotes all of them |
| A critique | The verdict | The chairman weighed it |
| The verdict | Nothing else | Nothing reads the chairman |

That cascade is the honest cost. Re-running a member and keeping the reviews
would leave a transcript whose critiques discuss an answer that is no longer in
it, and a verdict that weighed one the run no longer contains. Continuing a
failed run applies the same rule for the same reason: if a member died in Stage
1 and the critiques were written without it, continuing rewrites those
critiques rather than pretending they saw it.

Everything else is replayed from the record, exactly as with Continue — and,
also as with Continue, the seat's provider is re-read from Settings first, so
pointing it at another model between attempts is the point rather than a
side-effect.

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
measures the replayed conversation only: your task, the other members' answers
quoted into a critique, and whatever the agent reads for itself all land in the
same window, so treat it as a floor rather than a total.

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

(`agy` is the exception to the shape, not to the rule: its read-only flags
carry an approval too, because headless `agy` denies its own *reads* without
one. Plan mode is what refuses the write either way — see [Antigravity's
read-only flags](#antigravitys-read-only-flags).)

| | Zero-Touch **off** (default) | Zero-Touch **on** |
|---|---|---|
| **Council** | Pauses at the gate. Nothing is on disk yet; **Approve & execute** is what grants the chairman permission. | No gate. Runs start to finish unattended, the chairman writing as it goes. |
| **Chat** | Read-only. The assistant is invoked with its agent's read-only arguments (`--sandbox read-only`, `--permission-mode plan`, `--mode plan`) and can talk about the folder but not change it. | The assistant can create, modify and delete files, exactly as the chairman can. |

Chat has no approval gate — nothing stands between the message and the reply
for a human to review — so Zero-Touch is the *only* way to grant it write
permission. That is deliberate: it means "what does this repo do?" is safe to
ask by default, and arming it is a single, visible decision. The greeting says
which of the two you are in and turns amber when it is armed.

Four properties hold throughout, and they are covered by tests:

- **A member or critique seat never receives an auto-approve flag.** They are
  read-only by contract regardless of the toggle; only the chairman can be
  granted one.
- **Read-only and auto-approve are never both sent.** They are opposite grants;
  a provider gets one or the other.
- **The flags are never baked into the command template.** They live in a
  separate config field and are appended only when permission has actually been
  granted — so switching Zero-Touch off is sufficient to guarantee they are not
  passed.
- **Whatever writes is protected the same way.** The safety snapshot is taken
  immediately before it and the diff collected immediately after, whether that
  is the chairman or a Chat turn. A read-only run skips both, because reading a
  diff after one would report your own uncommitted work as the agent's.

**One exception, and it is worth knowing before you configure one.** Read-only
is enforced by the arguments a CLI is launched with, so it is only as strong as
the `read_only_args` that provider declares. The three shipped agents declare
theirs; the **Custom command** preset ships with an empty list, and nothing
substitutes for it. A member seat on a custom command is therefore held to
read-only by the prompt alone — an executable that writes regardless of what it
was asked will write, before the approval gate and before the safety snapshot,
which then records the already-modified tree as the starting state. The app
does not refuse that seat; it publishes a warning naming each unguarded provider
when the run starts. Give a custom command its own read-only flag in Settings,
or don't seat it.

[Incognito](#incognito) is the same kind of promise made the other way round,
and takes the opposite decision: a provider with no `incognito_args` is *not*
seated on a private run. The difference is what a broken promise costs. A
read-only seat that writes leaves the evidence in your working folder, where the
diff and the snapshot will show it; a private seat that saves leaves it in that
CLI's own history, where this app cannot see it, warn about it or delete it.

> **Zero-Touch means what it says.** An agent will create, modify and delete
> files in your working folder with no further confirmation. Use it on a
> branch, keep Safety Snapshot on, and don't point it at anything you can't
> afford to lose.

### Writing modes

Two style switches, both off by default, both on the gear in **all three
tabs** — beside the composer in Council and Chat, and in Projects both beside
the goal box and in the tracker header of a running project.

| Mode | What it asks for |
|---|---|
| **Efficiency mode** | Concise professional prose. Lead with the answer, drop filler, repetition and unrequested examples, keep the reasoning, assumptions, uncertainty and safety notes that make the answer usable. |
| **Caveman mode** | The same goal pushed much harder: telegraphic grammar, no articles, no preamble. Cheapest and bluntest. |

Both carve out the same exception — code blocks, shell commands, file paths,
variables, configuration and error messages stay complete and byte-exact.

The confidence trailer and the Project report contract are carved out too, by a
sentence this app appends after whichever bodies are on: *this changes your
voice, not the contract*. It is kept out of the two instruction texts
themselves, so what you paste into a style stays what you pasted. It exists
because both bodies were written about prose and name only *code* as
unshortenable — an agent pruning "unnecessary headings" has no way to know one
of those headings is what the engine parses.

A run records which switches it answered under, and the top bar shows
**CAVEMAN** / **EFFICIENCY** beside Zero-Touch. It is read off the run, not off
the gear: an archived answer that reads strangely should be able to say why,
and the switch that did it may since have been turned off.

**They are one implementation, reaching every tab through the same seam.** The
instruction text lives once in `aicouncil/prompts.py`, is composed once by
`_style_block()`, and is read once per run by `config.writing_styles()`. What
differs per tab is only where the block is injected:

| Tab | Reaches |
|---|---|
| **Council** | Every member, every critique and the chairman — one reading taken at run start, so the chairman does not end up writing in a different voice to the answers it is synthesising. |
| **Chat** | Every turn, including each CLI of a multi-agent answer. |
| **Projects** | Every Architect, Developer and QA turn. Read live rather than frozen: a project runs for hours, so toggling it from the tracker header changes the *next* turn, not the next project. |

Each tab stores its own value, so Efficiency in Chat does not switch it on for
Council. Selecting both modes at once is supported and produces one combined
instruction, not two competing ones: Caveman sets the voice and Efficiency is
applied inside it.

These are style instructions to the agent and nothing more. They do not compact
the conversation history, change the model or its reasoning depth, or impose a
hard output-token ceiling — whether a given CLI actually answers shorter is up
to the model.

### Other toggles

The gear beside the composer also carries Zero-Touch and Pull request, which
change per run, plus **Multi-agent answer** in Chat and **Show the council
seats** in Council. The rest live in **Settings → Run**, split by what they
decide: how the next run behaves, and where the work lands. All of those are
Council-only, and the gear says so rather than going quiet in Chat, which has
no gate and no branch for them to decide anything about.

| Toggle | Group | Effect |
|---|---|---|
| **Pull request** | Delivery | Deliver the run on a branch of its own and open a GitHub PR instead of writing to the checked-out branch. See below. |
| **Require clean tree** | Delivery | Refuse to start if the repo has uncommitted changes. Pull-request mode enforces this itself, on or off. |
| **Safety snapshot** | Delivery | Capture the worktree before the chairman writes so **Roll back** works. Leave on. |

---

## Incognito

The hat-and-glasses button at the **top right** starts a conversation that is
not recorded. Dim is off; bright — accent-coloured, with a halo — is on. Click
it before you send, and the run it starts carries that choice for its whole
life. A live run is on the record it was started under: the button is disabled
while one is going, and a run started incognito stays labelled `INCOGNITO` in
the status line after the toggle is switched back off.

An incognito conversation:

- **is never written to `~/.config/ai-council/runs/`**, so it is not in the
  **Chats** sidebar and is not there after a restart;
- **does not teach the router.** The seating history in `config.json` is a
  persistent record of who sat and how it went, so an incognito run contributes
  no sample to it;
- **passes each CLI its own no-save flag** — `--ephemeral` to Codex,
  `--no-session-persistence` to Claude — so the turn is absent from that agent's
  own history too, not merely from this app's;
- **can still be continued** for as long as Theseus is open. It is held in
  memory, so the composer stays attached and follow-ups work exactly as they
  otherwise would. A follow-up to a private conversation is private whatever
  the button says at the time — it carries the earlier turns with it. Closing
  the app is what ends it.

**Antigravity is not seated on an incognito run.** Its CLI offers `--continue`
and `--conversation` to resume a saved conversation and nothing at all to stop
one being saved, so there is no flag that would make the promise true. A CLI
with no way to keep its own history clean is left off rather than run and
quietly recorded, and the same applies to a hand-written **Custom command**
until you give it `incognito_args` in Settings. If nothing installed can run
incognito, the run is refused with that reason rather than started.

What it is not: incognito does not anonymise you to the model provider. The
prompt still goes to the same subscription over the same account, and whatever
that vendor retains server-side is between you and them. Nor does it change
what a run may write — a folder chosen is still a folder written to, under the
same gate and the same snapshot.

**Projects cannot run incognito.** The button is disabled on that tab, and a
start that asks for it anyway is refused rather than quietly given the opposite.
A build's `.theseus/BOARD.json`, `SPEC.md` and `CRITIQUE.log` are the state it
resumes from; a project that kept none of them could not be paused, resumed or
audited, which is most of what a project is.

---

## Council or Chat

The selector centred at the top decides which of three things your next
message (or, for Project, a goal) starts. It is the first choice, above
everything it changes, because the three share almost nothing.

**Council** is the pipeline this README is mostly about: independent member
answers → anonymised critique → approval gate → chairman synthesis and
application, with the console, the diff, delivery controls, snapshots and
rollback. See [Projects](#projects) for the third tab.

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
- **No deliberation and no approval gate**, and no member strip — just the
  message and the reply.
- **Read-only until you say otherwise.** By default Chat is invoked with its
  agent's read-only arguments (`--sandbox read-only` for `codex`,
  `--permission-mode plan` for `claude`, and `--mode plan
  --dangerously-skip-permissions` for `agy` — [that pair is not the typo it
  looks like](#antigravitys-read-only-flags)) and never receives an
  auto-approve flag of its own. Turn Zero-Touch on and it can change files,
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
  never audited ........ QA reads the folder, read-only
  board empty .......... the architect breaks the goal into cards
  build FAILING ........ the developer fixes it
  changes unverified ... QA builds and tests it
  cards in review ...... the architect reviews the diff
  cards in backlog ..... the developer claims the top one
  board clear .......... QA starts the application and uses it
  accepted ............. the architect proposes what is missing
  nothing left ......... COMPLETED
```

Read the fourth line precisely: the trigger is **unverified changes**, not an
`UNKNOWN` build. The engine raises that flag when an agent writes code, and QA
lowers it by running the build. A board that reads `UNKNOWN` with nothing
unverified — an audited folder in which no tooling was detected, or a resumed
board whose flag was already cleared — does not schedule a QA turn on health
alone, so it can reach the last line with the build still `UNKNOWN`. The
completion note says so; see [When a project stops](#when-a-project-stops).

That ordering *is* the design, and it is the part worth arguing about:

- **A failing build outranks everything.** No new features while it is red.
- **An unverified build outranks a code review.** Reviewing code nobody has
  compiled wastes the reviewer's turn, and half the time reviews something that
  does not build.
- **A review outranks starting more work**, so the queue cannot run away from
  the person who has to read it.
- **Only a QA turn can set the build to PASSING**, and the engine sets it back
  to UNKNOWN the moment anyone writes code. A green board therefore always
  means somebody ran the tests *after* the last edit. Writing code is the only
  thing that clears it: a review that sends a card back has moved a card, not
  edited a file, and retesting an untouched tree costs a QA turn per bounce.
- **A QA turn that never ran is not a red build.** A CLI that crashed or timed
  out has tested nothing, so the tree stays *unverified* rather than being
  recorded as `FAILING` — which would hand the developer a crash report from
  the app itself to fix, and spend the whole fix budget failing to. A QA turn
  that *ran* and reported nothing usable is still `FAILING`; that half is
  deliberate and unchanged.
- **An agent that reports `blocked` or `failed` has not done the work**, even
  if its CLI exited zero. Its card stays in progress instead of going to a
  reviewer to read a diff that was never written.
- **A green build is not a working application.** A project assembled one card
  at a time can pass every test it wrote for itself while never having been
  started by anybody. So the last gate before a project may finish is an
  *acceptance* turn: QA installs it from clean, runs it, walks the journeys the
  goal describes, tries the unhappy paths, and says whether the thing does what
  was asked. Anything it cannot do comes back as a bug card, the loop reopens,
  and writing code clears acceptance exactly as it clears the build — so the
  gate is only ever passed against the tree as it finally stands. The tracker
  shows this beside the build as **Works**.

Bugs are cards too. QA raises them off a build failure, and they jump the
developer's queue ahead of the backlog.

### The three chairs

| Chair | Default CLI | Fires when | What it does |
|---|---|---|---|
| **Architect** | `claude` | a goal needs decomposing, cards are in review, or the board is clear | Reads the goal as a product brief, turns it into cards that each say what "done" looks like, reviews every diff against those criteria, and proposes enhancements once the goal is met. |
| **Developer** | `codex` | there are cards in the backlog, or the build is failing | Claims the top card, writes the code and its tests, wires it into how the program actually starts, fixes build failures. One card per turn. |
| **QA** | `agy` | at startup, whenever code has changed, and once the board is clear | Audits the workspace read-only, runs the project's real build and test commands and captures the output verbatim, then finally starts the application and uses it against the goal. |

That pairing follows the work rather than the vendor. The architect seat is the
one where a bad judgement costs the most turns to undo, so it goes to the
strongest reviewer; the developer seat spends the most turns and the most quota,
so it goes to the strongest implementer with the most room; and the QA seat is
an independent read of somebody else's build, which is analysis, not authorship.
The matrix says which CLI each seat is recommended for and tells you when a
chair is sitting on something else — but none of it is a rule. Click any chair
to reassign it, exactly as you would a council member. The three are ordinary
providers under the hood, so the model picker, the reasoning-depth picker and
the quota chip all work on them unchanged.

**Three different CLIs is the point, not decoration.** When one runs out of
context or quota mid-turn, the turn is rebuilt from the board and handed to
another chair — and a chair pointed at the CLI that just ran out is not a spare
one, so the engine skips it rather than spending a turn to rediscover that. The
same goes for judgement: an agent reviewing its own diff, or accepting the
application it wrote, is not a second opinion. Doubling up is allowed and the
initializer says what it costs.

If a chair is pointed at an agent you have not added, or at a CLI that is not
installed, the project **will not start**, and says which one and which of the
two it is. A build that runs unattended for an hour should not discover on its
fourth turn that its QA seat was never fillable. QA ships pointed at `agy`, so
if that is not one of the agents you added, move QA to one that is — the matrix
only offers agents you have connected.

### Pointing it at code that already exists

Most projects are not empty folders, and this one is built for that case.

**The first turn is read-only.** Before any of *your* files are touched, the QA
chair is invoked with its read-only flags and *no write grant at all*, and asked
what is already here: the layout, the configs, what of the goal exists, and
anything that would break if an agent touched it. Nothing can change during that
turn, by construction rather than by instruction.

Two things are written before that turn, and both are the engine's own
bookkeeping rather than an agent's work: `.theseus/` is created with the
starting `BOARD.json` in it, and `.theseus/` is appended to `.gitignore` if the
folder is a git repository. Both happen while the project is being started, so
the safety snapshot taken immediately afterwards already contains them — rolling
a project back by hand will not remove the `.gitignore` line.

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

A git snapshot is taken before the first write where there is one to take, using
the same mechanism a council run uses — see [How rollback
works](#how-rollback-works). In a folder that is not a repository there is
nothing to snapshot and the app says so before you start.

**There is no Project rollback button, and no undo-the-build endpoint.** The
snapshot is recorded and stored on the board, but nothing in the app restores
it: the **Roll back** control belongs to the last council or Chat run, not to a
project. Undo a build by hand instead. The snapshot is a real commit, anchored
under `refs/ai-council/snapshots/` and also recorded in `.theseus/BOARD.json`
under `snapshot`, so either of these finds it:

```bash
git -C <project folder> for-each-ref refs/ai-council/snapshots
python3 -c "import json;print(json.load(open('.theseus/BOARD.json'))['snapshot'])"
```

The board's `snapshot` object holds `head` (the commit HEAD was on), `commit`
(the commit whose tree is the pre-project worktree) and `ref` (the anchor
keeping it alive). Those are the two ids below, and this is the sequence the
app's own rollback runs, in order — the `clean` must come before the tree is
laid down, or it deletes the files it just restored:

```bash
git reset --hard -q <head-at-snapshot-time>
git clean -fdq
git read-tree -u --reset <snapshot-commit>
git reset --mixed -q <head-at-snapshot-time>
```

Reopening the app does not reload the snapshot into memory, so read the commit
id off the ref or the board rather than expecting the UI to offer it.

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
  consistent. Until that agent exits the tab says **Pausing**, because "nothing
  was interrupted mid-write" is only true once it has.
- **Resume** picks up from the board — a pause, or a run that stopped at one of
  the bounds below.
- **Hand off** forces the next turn onto a chair you choose. For the failure the
  board cannot see — an agent answering, exiting zero, and going in circles.
- **Stop** ends it now, killing whatever is executing. Use Pause unless you mean
  that.

Close the window mid-build and the project is still on disk; reopening the app
finds it and offers to resume. The safety snapshot is read back off the board
with it, so a resumed project keeps pointing at the tree as it was before the
first agent wrote anything rather than anchoring the half-built one. A project
that is still running when you open the app pulls you to the tab, because an
idle-looking window while three agents rewrite a folder is the wrong thing to
show.

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

### When a project stops

A project ends in one of two states, and the distinction is narrower than it
sounds:

- **FAILED** — the step limit was reached, the fix budget ran out, the board
  stopped moving for three turns, the operator pressed **Stop**, or the worker
  crashed. The board keeps the reason.
- **COMPLETED** — the engine had nothing left to schedule.

**The first ending wins.** A run that gave up says so and stays saying so:
nothing downstream may relabel a project that stopped as one that finished.

**`COMPLETED` does not on its own mean green.** The last rung of the ladder is
"nothing left to do", not "the build passes", so a project can complete with the
build `UNKNOWN` — nobody ever verified it, because nothing was ever flagged as
needing it. It cannot complete `FAILING`: a red build outranks everything until
it is green or the fix budget runs out, and running out is a `FAILED` run, not a
finished one. Acceptance is the same shape: a project *can* complete having been
run and found wanting, because refusing to finish would only mean burning the
step limit on a card the agents have already shown they cannot close. The
completion note is the authoritative reading and states both verdicts
explicitly — *"Finished: 6 of 7 cards done, build unknown, in 22 agent turns.
Note: the build is UNKNOWN; nobody ran the application itself"* — along with any
cards that never reached Done. Read that line, or read `build_health` and
`release_health` in `.theseus/BOARD.json`, before treating a finished project as
a working one.

**`FAILED` is not the end of the project, only of that run.** Everything built
is on disk, the board says where it got to, and **Resume** picks it up from
there — which is the whole point of bounding a loop nobody is watching. The two
counters that ended the run are cleared as it restarts, so a project that ran
out of fix attempts gets the full budget again rather than giving up on its
first turn for a reason it has already reported. `COMPLETED` is the one ending
that cannot be resumed: there is nothing left to schedule. Start a new project,
or move `.theseus/` aside.

**New project** closes the report and puts the initializer back. It deletes
nothing — the board is marked closed so the tab stops offering it, and the
build stays exactly where the agents left it.

### Trying it without spending quota

`scripts/mock-agent.py` speaks the project protocol. Point all three chairs at
it and the whole decision loop runs for real against a real directory: it audits
the folder read-only, plans two cards, implements a Python module **with a
genuine bug in it**, fails its own test suite, is handed the real trace, fixes
it, gets the cards reviewed, runs the finished module the way its user would,
and proposes one enhancement before declaring itself finished. Nothing in that
sequence is faked — the tests really run and really fail, and the acceptance
turn really calls `greet("Ada")` — which is the only way to know the loop works
rather than to believe it.

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
                     created after the      chairman works here         you merge
                     approval gate
```

1. **Before anything starts**, every precondition is checked: a clean tree, a
   commit to branch from, a named branch (not a detached HEAD), a git identity
   to commit with, an `origin` remote, `gh` on `PATH`, and `gh auth status`
   passing. Failing late — after the chairman has spent its quota — would
   strand the work on a branch nobody asked for.

   The last two are what **Settings → Agents → GitHub** is for: it installs
   `gh` into `~/.local/bin` without sudo, and connects it. See
   [Connecting GitHub](#connecting-github) below.
2. **The branch is created after the approval gate**, so rejecting still leaves
   the repository completely untouched.
3. The chairman works on that branch as it normally would.
4. On success the run commits everything it changed, pushes with
   `--set-upstream origin`, and runs `gh pr create --base <the branch you
   started on> --head <the run's branch>`. The chairman's own summary
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
the chairman starts writing and the moment the PR exists — the window a failed push
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

### Connecting GitHub

**Settings → Agents → GitHub.** Two buttons: one installs the GitHub CLI into
`~/.local/bin` (a static binary — no sudo and no package manager), the other
takes a [personal access token](https://github.com/settings/tokens) with the
`repo` scope. Add `workflow` too if you expect a run to edit anything under
`.github/workflows`; the card names any scope you are missing rather than
letting a run discover it at the push.

**This app does not store the token.** It is piped to `gh auth login
--with-token` on standard input and then dropped — it is never written to
`config.json`, never placed in an argument (arguments are readable in `ps`),
and never put into an agent's environment. After that `gh` owns the
credential, which is also what makes it work for the agents: a CLI that runs
`gh pr create` in the working folder is already authenticated, and `gh auth
setup-git` means a plain `git push` over HTTPS is too. One login, no copies.

Where it does land is `gh`'s business, and the card says which happened: your
system keyring if one is available, otherwise `~/.config/gh/hosts.yml` at mode
`0600`. That is the same place `gh auth login` in a terminal would have put it.
**Disconnect** runs `gh auth logout`; this app has nothing of its own to clear.

> This is the one credential the app accepts, and it is deliberately not an LLM
> key. No agent API key is ever asked for, stored or sent — that boundary is
> what keeps a run free at the point of use.

Branch protection itself lives on GitHub, not here. This mode keeps the app off
your base branch; enabling a ruleset is what stops everything else.

---

## How rollback works

Before the chairman writes anything, the app records your worktree — tracked edits
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

**Rollback belongs to the last council or Chat run, and it does not know about
projects.** Starting a project does not retire the previous run, so its **Roll
back** button stays on screen and stays live — and clicking it resets and cleans
the working tree while the project's agents are writing to it, discarding their
work and whatever else was in flight. The two features refuse each other at
*start* (a run will not start during a project and a project will not start
during a run), but not here. If you started a project in the same folder as a
finished council run, treat that button as armed until the project is over.

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
| **Model** | Blank means the CLI's own default. Belongs to the CLI, so it is shared by every card running it. See below. |
| **Model flag** | How the model is passed — `--model {model}`, `-m {model}`, `--model={model}`. |
| **Selectable models** | The list offered in the picker, one per line. Shared by the CLI's cards, like the model itself. |
| **Timeout** | Seconds before the child process group is killed. |
| **Pipe the prompt on stdin** | For CLIs that prefer stdin. Automatic above 96 KB regardless. |

The defaults:

| Agent | Command | Auto-approve |
|---|---|---|
| Codex | `codex exec {prompt}` | `--dangerously-bypass-approvals-and-sandbox` |
| Claude | `claude -p {prompt}` | `--dangerously-skip-permissions` |
| Antigravity | `agy --print-timeout 60m --prompt={prompt}` | `--dangerously-skip-permissions` |

**Standing project rules** are appended to every member's and the chairman's
prompt — a good place for "use tabs", "never add a dependency without
asking", "all new code needs tests".

Config lives at `~/.config/ai-council/config.json`. Every run transcript except
an [incognito](#incognito) one is written to `~/.config/ai-council/runs/` and
grouped into threads under the **Chats** tab, where a conversation can be read
in full or [continued](#continuing-a-run).

### Assigning agents to seats

The *agent* (which CLI) and the *seat* (member, or chairman) are separate
settings. Under Settings → **Agents → How each CLI is run** there is one card
per agent *you added* — with its command, auto-approve argument, model and
streaming flags. An agent you have not added has no card there, because it has
no job to configure. Settings → **Council → Seating** is where each seat is
pinned to one of your agents, or left on **Auto** for the router to pick per
run from what the task looks like; the same pin-or-auto choice is one click
away on any seat in the bench above the composer. Every one of those pickers
offers only the agents you connected.

**Antigravity** is Google's `agy`, which replaced Gemini CLI for personal
accounts on 18 June 2026. Add it like any other agent — its card will install
the binary into `~/.local/bin` for you — and sign in by running `agy` once with
a Google account. It differs from the other two in four ways worth knowing:

- **The prompt is a flag's value**, not an argument of its own — hence the
  `--prompt={prompt}` template. Do not rewrite it as `--print {prompt}`: the
  model and permission flags are inserted immediately ahead of the prompt, and
  `--print` would take `--model` as the thing to answer.
- **No streaming format.** `agy` has no `--output-format`, so its answer is
  read as the plain text it is.
- **No quota reading.** It publishes no equivalent of `claude /usage`, so its
  member shows *no quota data* rather than a number this app made up.
- **No no-save flag.** `agy` offers `--continue` and `--conversation` to resume
  a saved conversation and nothing to stop one being saved, so it is left off
  [incognito](#incognito) runs rather than run and quietly recorded.

Editing a CLI's command by hand still works and simply reads back as
**Custom command**; the command is the source of truth, and the card is
derived from it, so the two can never disagree.

The Chat assistant has its own card in the same list, with its own display
name, command and optional **Behaviour**. It has no Role, because it is not a
seat in the council, and no seat to pin it to: the CLI Chat runs is the first
chip under the composer, where the same swap happens on one click.

### Roles

What a seat is *told to be* is a persona, pinned per seat the same way an
agent is — Settings → **Council → Seating**, or left on **Auto** for the
router to pick one the task calls for. The catalogue of personas is edited
under Settings → **Roles**:

| Template | Behaviour | Lens | Writes |
|---|---|---|---|
| Council Member | The neutral lens — what an unrouted seat gets | yes | no |
| Pragmatist | Smallest change that works, shipped today | yes | no |
| Visionary | The longer view, and what this decision costs later | yes | no |
| Adversary | Doubts the code and the framing, answers anyway | yes | no |
| Threat Modeller | Who is on the other side of the boundary, and what they gain | yes | no |
| Adversarial Reviewer | Hunts for defects, fixes nothing | no | no |
| Test Writer | Writes tests that would have caught real bugs | no | yes |
| Security Reviewer | Findings with a real attacker and a real path | no | no |
| Direct Implementer | Works the task directly, nothing to review | no | yes |
| Chairman | Weighs the bench and applies the outcome | no | yes |
| Junior Draft *(legacy)* | Surveys the repo, proposes a change | no | no |
| Senior Polish *(legacy)* | Verifies a draft, corrects it, applies it | no | yes |

**Lens** is whether the template composes onto a council stage. A lens says
what to *look at* and leaves the shape of the reply to the stage; the templates
marked `no` are standalone roles carrying an output contract of their own, and
a seat handed one holds two contracts and answers to neither. Auto only ever
picks a lens — a security question seats the **Threat Modeller**, a review or a
bug seats the **Adversary**. The standalone roles stay selectable by hand, at
the top of the list after the lenses and labelled *standalone*, because the
operator may want exactly that.

**Writes** is what the persona *expects to do*, not what its seat is allowed to
do. It is advisory metadata on the template — it grants nothing and blocks
nothing. Permission is granted per seat, so a `yes` persona on a member seat is
an agent told to modify files that cannot, which is the mismatch the paragraph
below describes and the one Settings flags rather than silently resolving.

**Chairman** is not offered as a member's lens: it is what the third stage
*is*, not a lens over it, so the chair's seat has no persona picker and edits
to that template change how the chair is briefed. Junior Draft and Senior
Polish are the two-stage council's personas, kept selectable but no longer
wired to a stage that resolves them by name; every member seat today is
read-only regardless of which persona it holds, and only the chairman can
write. Pick a persona and it takes effect on the next run.
The text box below it overrides the template entirely — edit it, or clear it
to go back. Blank means "use the template", so clearing the box restores the
default rather than sending an empty prompt.

Combined with agent assignment, that already covers a lot: pin one seat to
Claude as an **Adversary** and leave the rest on Auto, and Claude is no longer
the final voice.

**A caveat the UI states rather than hides.** Permission is still granted *per
seat* — a member or critique seat is read-only, the chairman writes once
approved. So a writing persona on a member seat produces an agent told to
modify files that cannot, and a report-only persona on the chairman gets told
not to write while still permitted to. Settings flags the mismatch instead of
silently resolving it; guessing which of the two you meant is how a safety
setting stops being trustworthy.

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

A seat's card in the thread still shows that agent's final answer alone, not
this transcript — the events carry both, and each pane gets the one it wants.

If a future CLI release renames these flags, edit the field; nothing here is
compiled into the app. Clearing it is safe too — you simply get the old
all-at-the-end behaviour back, and the app stops trying to parse events.

### Antigravity's read-only flags

The other two CLIs take one flag to be read-only. `agy` takes two, and the
second one is called `--dangerously-skip-permissions`, which looks like exactly
the wrong flag to find in a read-only slot. It is not.

The two do different jobs. `--mode plan` is the restriction: it is what refuses
the write, and it refuses it whether the write comes from an edit tool or from
a shell redirect. The permission prompt is a separate mechanism, and in
`--print` mode `agy` has no terminal to ask it on — so it auto-**denies**
instead, including the `read_file` that a read-only stage exists to do. A
plan-mode run without the second flag therefore reads nothing, answers nothing,
and still exits 0:

```text
jetski: no output produced — a tool required the "read_file" permission that
headless mode cannot prompt for, so it was auto-denied.
```

Which is what every Antigravity council seat did until this pairing was fixed:
the seat came back blank, was dropped from the deliberation, and the strip said
`2 of 3, 1 failed`. Both flags together give the stage its reads back while
plan mode keeps refusing the writes — verified against `agy` 1.1.10 by asking
it to overwrite a file both ways and confirming the file was untouched.

A config written before the fix is repaired on load, but only if it still
carries the exact broken pair; hand-edited flags are left alone.

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

### Choosing a model per CLI

In Chat, click the model chip under the composer. In Council, click a member on
the strip and choose **Model**. On the Projects tab, click a chair in the
matrix and choose **Model**. The picker offers the configured list, the CLI's
own default, and a free-text box for anything else — typing a model adds it to
the list for next time.

**The choice belongs to the CLI, not to the tab it was made on.** Antigravity
is one login with one catalogue, so picking a model for it on the Projects tab
is the same act as picking one on the bench: the council seat, the chat
assistant and every project chair that runs `agy` all follow. The one exception
is a project already running, which keeps the settings it started with — a turn
that changed CLI halfway is a different run, not a restyled one. Under the
hood, `agent_settings` in the config file is where they live; the copies on
each provider are projected from it on every load.

The picker asks each CLI what it can actually run. Codex publishes an
account-scoped list in `$CODEX_HOME/models_cache.json` — read live, so it
reflects your login's entitlements. Antigravity has a subcommand for it, so the
picker runs `agy models` and shows exactly what it prints — the id you select
and, beside it, the name that listing gives it, because `gemini-3.6-flash-high`
and `gemini-3.5-flash-high` are one character apart. (It includes the Anthropic
and open-weight models served through Antigravity, not just the Gemini ones.) Claude ships neither, so the picker offers its documented `--model`
aliases. Nothing is hardcoded, deliberately: a shipped list is wrong the moment
a model is renamed, and wrong *per account* regardless.

Asking costs a process launch — `agy models` takes seconds on a cold start and
up to a minute on a loaded machine — so each complete answer is written to
`~/.config/ai-council/catalog.json` and served from there, across restarts
included. The menu says which it is showing (`agy models · saved 40 min ago`)
and carries a **Refresh** button for asking again. It re-asks on its own when
the binary changes or, for Codex, when its account cache does. A refusal is
never stored: a signed-out `agy`, or a level set the CLI was too busy to name,
is shown once and asked for again next time.

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
on the member seats and spend the rationed one on the chairman, which is where
judgement actually matters.

### Choosing a reasoning effort per CLI

Beside the model chip is a second one for **reasoning effort** — the same knob
`/effort` sets in Claude Code and the reasoning selector sets in Codex. It is
the CLI's, like the model, and follows it into every seat and chair it holds.

Depth costs quota, and the stages do not all want the same amount of it: a
member sketching an independent answer rarely needs what a chairman weighing
every answer and critique against the real code does. That split is
`deliberation_effort` in **Settings → Council**, which sets the depth for
Stages 1 and 2 and leaves the chairman on the level its CLI carries.

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

### What a council run costs, and where it goes

A three-seat council is seven CLI launches, and those seven are not seven model
requests — each one is an agentic loop that reads, greps and re-reads until it
is satisfied. That loop, not the prompt, is what the run spends. On one
measured run here the prompts came to about 83,000 characters of text against
6.4 *million* input tokens processed: a quarter of one percent.

The bench is also not evenly loaded. The chair is picked first and from the
whole field, so the strongest CLI tends to chair — and with
`chair_deliberates` on it holds a member seat as well, which is three of the
seven launches for one agent. On that same run:

| stage | requests | input | output |
|---|---:|---:|---:|
| member seat | 31 | 2,175,599 | 24,383 |
| its critique | 9 | 390,603 | 13,082 |
| chairing | 52 | 3,838,229 | 24,129 |

Two things follow, and they point at different fixes. The member seat and its
critique are **40% of that agent's spend** — the part a second seat costs, and
the part `chair_deliberates: false` removes. But the chair alone is 60%, and no
seating change touches it: it re-derived a repository three members had already
read and cited.

So there are two knobs, and they are meant to be used together:

- **Deliberation effort** (Settings → Council) overrides the reasoning effort
  of Stages 1 and 2 only. Effort otherwise belongs to the CLI, so the seat an
  agent holds and the chair it also runs share one setting and there was no way
  to buy a cheaper bench without demoting the only stage that writes. Blank —
  the default — leaves every seat on its own setting.
- **Chair deliberates** (Settings → Council) benches the chair for Stage 1.
  With three agents added that costs a position: the member pool excludes the
  chair, so a bench of three becomes a bench of two. The seating says so when
  it happens. It is the bigger single saving and the bigger single loss.

There is also a saving nobody has to choose. A machine with **one CLI
installed** seats one member, not two: a second seat is the same agent
answering the same question twice and then critiquing itself under an alias —
four launches before the chairman, and no second opinion in any of them. The
peer critique is skipped whenever every surviving position came from the same
CLI, however the seats were filled, and the run still stops at the gate,
because one voice was never a quorum.

Neither is measured by guesswork. `scripts/council-cost.py` joins a finished
transcript to the logs the CLIs write for themselves and prints the run seat by
seat:

```bash
python3 scripts/council-cost.py              # the most recent run
python3 scripts/council-cost.py <run-id>
```

It opens nothing for writing and computes no figure of its own beyond summing
what the vendors reported — the same rule `usage.py` follows. Two caveats it
prints rather than hides: an agent whose CLI logs no usage (Antigravity, today)
is left out of the shares, and the input figures are dominated by **cache
reads**, which are not billed as fresh tokens and whose weight against a
subscription quota no vendor publishes. Read it to compare one run against
another, not as an invoice.

---

## Architecture

```text
aicouncil/
├── __main__.py     Entry point, browser launcher, --doctor
├── server.py       http.server + SSE, token auth, Origin/Host validation
├── pipeline.py     The state machine: deliberate → critique → gate → synthesize → complete,
│                   plus continuing a failed run from the stages that answered
├── router.py       Seats the bench: task profile → capability profile → score → seating
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
   The token itself never reaches a command line: the launcher's URL carries a
   **single-use ticket**, which the page trades for the token through
   `POST /api/session` on load. A browser's argv is readable by every other user
   on the machine, and the ticket is spent by the time they can look. One
   consequence: the printed URL opens the dashboard *once*. Reload the tab
   freely — reopening the link needs a fresh launch.
2. **Origin validation** — rejects requests from real remote sites (drive-by CSRF).
3. **Host validation** — rejects non-loopback `Host` headers (DNS rebinding).
4. **No shell** — commands run as argv lists with `shell=False`, so a prompt
   containing `` ` ``, `$(...)` or `;` is inert data.

The UI is served under a strict CSP with no external assets, so agent output
rendered as Markdown can never pull in a third party. Binding to anything other
than loopback is refused outright. Everything written under
`~/.config/ai-council` — the config, the workspace and every run transcript,
which holds the task, each stage's output and the full diff — is created
owner-only (`0700`/`0600`) rather than at whatever the ambient umask allows.
[Incognito](#incognito) writes no transcript at all and passes each CLI its own
no-save flag, which keeps a conversation off this disk; it is not an anonymity
feature, and says nothing about what the model provider retains.

What this does **not** defend against: a coding agent granted write permission
runs unsandboxed, as you, with your whole environment. In Projects Mode the QA
turn runs the repository's own `npm test`/`make test`, so opening a repository
you do not trust and starting a project is equivalent to running its scripts by
hand. Treat an untrusted repository accordingly — a container or a throwaway
user account, not just a git snapshot, which is a recovery aid and not
containment.

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
  granted, and never reach a member or critique seat.
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
- A continued conversation carries the earlier exchange to **every seat** and
  refuses a transcript from another folder.
- Continuing a *failed* run re-runs only what failed: the members' timestamps
  and answers are asserted to be **the ones from the first attempt**, the
  approval gate is not asked a second time, the first snapshot is kept, and a
  fresh engine handed nothing but the transcript still finishes the run.
- Running one seat again re-runs **what quoted it and nothing else**: a member
  takes the critiques and the verdict with it, a critique takes the verdict, the
  chairman takes nothing — checked on the clock, so a stage that was "kept" is
  one that really did not run twice.
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
  invocation**: the audit turn is launched with its read-only arguments and no
  write grant, and reports having modified nothing. The "modified nothing" half
  is the agent's own account of its turn, not a diff of the tree taken around
  it; the grant is what is enforced.
- `.theseus/` is appended to an existing `.gitignore` rather than replacing it,
  and never added twice.
- Build tooling is **read off disk, not guessed** — `package.json` scripts that
  do not exist are never run.
- A project and a run refuse each other, in both directions — at *start*. That
  is the covered case; rolling back a finished run while a project is live is
  not blocked, and is the caveat under [How rollback
  works](#how-rollback-works).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| An agent shows **not added** | It is installed but you have not asked to use it. Press **Add** on its card in Settings → Agents. |
| An added agent shows **not found** | Its CLI is not on `PATH`. Press **Install the CLI** on its card, run `./scripts/install-deps.sh --agent <name>`, or set an absolute path in Settings. |
| An agent shows **not signed in** | Press **Sign in** on its card, or run the vendor's login command yourself. Theseus never asks for an API key. |
| The chairman finishes but the diff is empty | The CLI ran without write permission. With Zero-Touch off, you must click **Approve & execute** — that's what grants it. |
| "Missing session token" | The dashboard was opened without the launcher's URL, or with one whose one-time ticket has already been claimed by another window. Restart with `./run.sh`. |
| Pull-request mode refuses to start | It says which precondition failed — a dirty tree, no `origin`, no git identity, or `gh` missing or logged out. Fix that one thing. |
| Run hangs, no output | The CLI is waiting on interactive input. Check its auto-approve arguments in Settings. |
| A seat hit its quota and the run failed | Don't start again — the answers already given are kept. Point that seat at another model or CLI, then click **Continue**: only the stage that failed runs. It works on a failed run reopened from history too, so restarting the app costs nothing. See [Continuing a run that failed](#continuing-a-run-that-failed). |
| A seat says **"exited cleanly but printed nothing"** | The CLI produced no answer despite a zero exit status. Click the step in the strip: whatever the CLI said on its way out is on that seat's row. For `agy` the usual cause is a missing read grant — see [Antigravity's read-only flags](#antigravitys-read-only-flags). |
| Port already in use | The server falls back to a free port automatically; read the URL it prints. |
| Stream shows "reconnecting" | The server stopped. It reconnects with backoff once it's back. |

---

## License

MIT — see [LICENSE](LICENSE).
