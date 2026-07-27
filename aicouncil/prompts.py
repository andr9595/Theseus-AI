"""Prompt construction for the two council stages.

The division of labour is the whole point of the quota optimisation:

* **Stage 1 (Codex, the junior)** does the expensive *exploratory* thinking -
  reading the repo, weighing approaches, writing a first cut. It is explicitly
  told **not** to modify files. Its output is a proposal document.

* **Stage 2 (Claude, the senior)** receives that proposal as pre-digested
  context. Because the survey work is already done, Claude spends its rationed
  quota on judgement and application rather than on rediscovering the codebase.

Stage 2 is instructed to treat the draft as *untrusted input* - a suggestion
from a colleague, not a specification. That framing matters: it keeps the
senior stage from rubber-stamping a confidently-wrong draft, which is the main
failure mode of a naive two-model chain.

Solo Mode is the exception to all of the above and lives here only for the
company: ``build_chat_prompt`` adds no persona, no house rules and no
repository preamble, because a plain conversation is supposed to reach the CLI
as the operator typed it.

Projects Mode has three roles rather than two and runs them in a loop with
nobody watching, so its prompts are built differently again - see the
"Projects" section at the foot of this file. What they have in common with the
council is that the shipped wording lives here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MAX_DRAFT_CHARS = 60_000
# How much earlier conversation a follow-up run may carry. A thread is bounded
# per turn when it is recorded (see ``pipeline._conversation_turn``); this is
# the second bound, on the whole rendered block, because a long enough thread
# would otherwise crowd out the task itself.
MAX_HISTORY_CHARS = 40_000
# How much of one stage's answer survives compaction. Enough to carry what was
# decided; the full text stays on disk in that run's own transcript.
MAX_COMPACT_REPLY_CHARS = 700
# Tokenizers differ per agent and per model, and none of the CLIs report their
# count back to us. Four characters per token is the usual prose approximation.
# Everything derived from it is an estimate and is labelled as one.
ESTIMATED_CHARS_PER_TOKEN = 4


DRAFT_SYSTEM = """\
You are the JUNIOR ENGINEER on a two-stage code review council.

Your job is to produce a high-quality implementation PROPOSAL. A senior staff \
architect will review, correct and apply your work in the next stage.

RULES - these are strict:
1. DO NOT modify, create or delete any file. Do not run commands that write to \
disk, stage changes, or commit. This stage is read-only.
2. DO read the repository as much as you need to ground your proposal in the \
code that actually exists. Cite real file paths and real symbol names.
3. Produce your answer as Markdown with these sections, in order:

## Understanding
What the task is asking for, and any ambiguity you had to resolve.

## Relevant code
The specific files, functions and call sites involved. Quote the few lines \
that matter. If you could not find something, say so explicitly rather than \
guessing.

## Approach
The plan, and the alternatives you rejected with a one-line reason each.

## Proposed changes
For every file you would touch, a fenced code block containing the proposed \
content or a unified diff. Label each block with the file path.

## Risks and unknowns
What could break, what you are unsure about, and what the senior reviewer \
should double-check. Be honest here - flagging a genuine uncertainty is worth \
more than false confidence.

Prefer the smallest change that fully solves the task. Match the surrounding \
code's existing style, naming and error-handling conventions.\
"""


POLISH_SYSTEM = """\
You are the SENIOR STAFF ARCHITECT on a two-stage code review council, and the \
only stage permitted to write to disk.

A junior engineer has produced a draft proposal for the task below. Treat that \
draft as a colleague's suggestion, NOT as a specification. It was written by a \
different model that may have misread the codebase, hallucinated a symbol, or \
solved the wrong problem.

YOUR JOB:
1. VERIFY the draft against the real repository. Check that every file, \
function and import it references actually exists and behaves as claimed. \
Where the draft is wrong, discard that part - do not try to salvage it out of \
politeness.
2. CORRECT the approach where your judgement differs. You have final say on \
architecture, naming, error handling and test strategy.
3. IMPLEMENT the change. Apply the edits to the working tree yourself.
4. MATCH the repository's existing conventions - its comment density, naming, \
import ordering, test layout and error-handling idiom. The result should read \
as though the person who wrote the surrounding code wrote it.
5. Do not add dependencies unless the task requires it and you say why.
6. If the draft was fundamentally wrong, say so plainly in one line and \
implement the correct solution instead.

WHEN YOU ARE DONE, end your response with a short section:

## Summary of changes
- `path/to/file.py` - what changed and why, one line each.

## Verification
How you confirmed this works, or precisely what you could not verify.\
"""


DIRECT_SYSTEM = """\
You are a SENIOR STAFF ARCHITECT working directly on the task below, with no \
draft stage preceding you.

Ground every change in the code that actually exists: read before you write. \
Prefer the smallest change that fully solves the task, and match the \
repository's existing conventions for naming, comments, error handling and \
tests.

When you are done, end your response with:

## Summary of changes
- `path/to/file.py` - what changed and why, one line each.

## Verification
How you confirmed this works, or precisely what you could not verify.\
"""


REVIEWER_SYSTEM = """\
You are an ADVERSARIAL REVIEWER. Your job is to find what is wrong with the \
code, not to fix it.

RULES - these are strict:
1. DO NOT modify, create or delete any file. Report findings only.
2. Assume the code is wrong until you have checked. Read the actual \
implementation rather than trusting names, comments or docstrings.
3. Rank findings by what they would cost if they shipped, not by how easy they \
are to describe.

For each finding give:

### <one-line summary>
**Where:** `path/file.py:LINE`
**Why it is wrong:** the specific mechanism, not a category.
**How it fails:** concrete inputs or sequence that produces the bad outcome.
**Confidence:** high / medium / low, and what would settle it.

Finish with a short section listing what you checked and found *correct*, so \
the reader knows what the review covered. If you found nothing serious, say so \
plainly - a review that invents problems to look thorough is worse than a \
short one.\
"""


TEST_WRITER_SYSTEM = """\
You are a TEST ENGINEER. You write tests that would have caught real bugs.

Read the implementation first and test what it actually does, including the \
paths that only run when something goes wrong. Match the repository's existing \
test framework, layout and naming - do not introduce a new one.

Prefer:
- Tests that pin a behaviour someone could plausibly break.
- One clear assertion per test, named for the property it protects.
- Real inputs and real collaborators where practical; mock only what is slow, \
non-deterministic or external.

Avoid tests that only restate the implementation, tests that pass regardless \
of the code under test, and coverage for its own sake. If a behaviour is \
already well covered, say so instead of duplicating it.

End with a note on what you deliberately did not test, and why.\
"""


SECURITY_SYSTEM = """\
You are a SECURITY REVIEWER. Report only findings with a plausible attacker \
and a plausible path.

RULES:
1. DO NOT modify any file. Report only.
2. For each finding, name the trust boundary being crossed and who is on the \
other side of it. A "vulnerability" with no untrusted input is not one.
3. Trace the data from where it enters to where it does damage, citing real \
file paths and line numbers.

For each finding:

### <one-line summary>
**Where:** `path/file.py:LINE`
**Trust boundary:** what crosses it, and from where.
**Impact:** what an attacker gains, concretely.
**Severity and why:** tied to the impact, not to the category name.
**Fix direction:** one or two lines - not a patch.

Explicitly list what you examined and considered safe. Do not report generic \
best-practice advice as a finding; if the code is sound, say so.\
"""


# The role catalogue. Each entry is a starting point the operator can edit -
# the shipped text is a default, not a law.
#
# ``writes`` records whether the behaviour expects to modify files. It is
# advisory today: permission is still granted per stage, so assigning a
# read-only behaviour to the writing stage produces an agent told not to write
# that nonetheless *may*. The UI flags that mismatch rather than silently
# resolving it, because guessing which of the two the operator meant is exactly
# the kind of quiet reinterpretation that makes a safety setting untrustworthy.
ROLE_TEMPLATES: Dict[str, Dict] = {
    "junior_draft": {
        "name": "Junior Draft",
        "summary": "Surveys the repo and proposes a change. Never writes.",
        "system": DRAFT_SYSTEM,
        "writes": False,
    },
    "senior_polish": {
        "name": "Senior Polish",
        "summary": "Verifies the draft against the code, corrects it, applies it.",
        "system": POLISH_SYSTEM,
        "writes": True,
    },
    # Named for what it does rather than for Solo Mode, which no longer has a
    # role at all - this is a council behaviour for a stage asked to work
    # without a draft in front of it.
    "solo": {
        "name": "Direct Implementer",
        "summary": "Works the task directly, with no draft to review.",
        "system": DIRECT_SYSTEM,
        "writes": True,
    },
    "adversarial_review": {
        "name": "Adversarial Reviewer",
        "summary": "Hunts for defects and reports them. Fixes nothing.",
        "system": REVIEWER_SYSTEM,
        "writes": False,
    },
    "test_writer": {
        "name": "Test Writer",
        "summary": "Writes tests that would have caught real bugs.",
        "system": TEST_WRITER_SYSTEM,
        "writes": True,
    },
    "security_review": {
        "name": "Security Reviewer",
        "summary": "Reports findings with a real attacker and a real path.",
        "system": SECURITY_SYSTEM,
        "writes": False,
    },
}

# Which template a stage falls back to when its setting is missing or unknown.
DEFAULT_TEMPLATE = {"drafter": "junior_draft", "polisher": "senior_polish"}


def shipped_role(role_id: str) -> Optional[Dict]:
    """The as-shipped definition of a built-in role, ignoring any edits."""
    tpl = ROLE_TEMPLATES.get(role_id)
    return dict(tpl, id=role_id, builtin=True) if tpl else None


def role_catalog(stored: Optional[Dict[str, Dict]] = None) -> List[Dict]:
    """Every selectable role: the built-ins, plus whatever has been added.

    ``stored`` is the saved catalogue from config. A built-in that has been
    edited appears with the edited text and ``edited: True``, so the UI can
    offer to restore the shipped wording; one that has not appears as shipped.
    Roles the operator created carry ``builtin: False`` and are otherwise
    identical - same list, same editor, no second-class citizens.
    """
    stored = stored or {}
    out: List[Dict] = []

    for key, tpl in ROLE_TEMPLATES.items():
        saved = stored.get(key) or {}
        merged = dict(tpl)
        merged.update({k: v for k, v in saved.items() if k != "id"})
        out.append({
            **merged,
            "id": key,
            "builtin": True,
            # Compared against the shipped text rather than trusting a flag,
            # so an edit that happens to restore the original stops being
            # reported as an edit.
            "edited": str(merged.get("system", "")).strip()
                      != str(tpl["system"]).strip(),
        })

    for key, saved in stored.items():
        if key in ROLE_TEMPLATES or not isinstance(saved, dict):
            continue
        out.append({
            "id": key,
            "name": saved.get("name") or key,
            "summary": saved.get("summary") or "",
            "system": saved.get("system") or "",
            "writes": bool(saved.get("writes")),
            "builtin": False,
            "edited": False,
        })
    return out


def role_by_id(role_id: str, stored: Optional[Dict[str, Dict]] = None) -> Optional[Dict]:
    """One role from the merged catalogue."""
    for role in role_catalog(stored):
        if role["id"] == role_id:
            return role
    return None


def resolve_system(
    stage_id: str,
    provider: Optional[Dict] = None,
    stored_roles: Optional[Dict[str, Dict]] = None,
) -> str:
    """The system prompt a stage should actually use.

    Precedence: text typed against the stage itself, then the role it is
    assigned (as edited, if it has been), then the shipped default for the
    stage. Blank at any level means "fall through", so clearing a box restores
    the layer beneath rather than sending an empty prompt.
    """
    provider = provider or {}
    custom = str(provider.get("role_system") or "").strip()
    if custom:
        return custom

    key = str(provider.get("role_template") or "").strip()
    role = role_by_id(key, stored_roles) if key else None
    if role and str(role.get("system") or "").strip():
        return role["system"]

    fallback = role_by_id(DEFAULT_TEMPLATE.get(stage_id, "solo"), stored_roles)
    if fallback and str(fallback.get("system") or "").strip():
        return fallback["system"]
    # Every layer empty: a hand-edited config could do this, and an empty
    # system prompt is worse than the shipped one.
    return ROLE_TEMPLATES[DEFAULT_TEMPLATE.get(stage_id, "solo")]["system"]


def clip(text: str, limit: int) -> str:
    """Trim an over-long block to ``limit``, keeping its head and its tail.

    That is where the signal is - the opening states the intent and the closing
    states the caveats; the middle is usually bulk code listing.
    """
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n\n... [truncated for length] ...\n\n{tail}"


def _workspace_block(workspace: str, workspace_status: Optional[Dict]) -> str:
    """Describe the folder the agent is working in, so it knows where it is.

    Not always a repository: a run can happen in any folder, and telling an
    agent otherwise invites it to go looking for a git history to ground itself
    in. Saying plainly that there is none also tells it why nobody will be
    reviewing a diff of its work.
    """
    lines = [f"Working folder: {workspace}"]
    if workspace_status and workspace_status.get("is_repo"):
        branch = workspace_status.get("branch") or "?"
        lines.append(f"Current branch: {branch}")
        subject = workspace_status.get("head_subject")
        if subject:
            lines.append(f"HEAD commit: {subject}")
        dirty = workspace_status.get("dirty_count", 0)
        if dirty:
            lines.append(
                f"NOTE: the working tree has {dirty} uncommitted change(s) "
                f"already present. Do not revert or clean them."
            )
    else:
        lines.append(
            "This folder is not a git repository, so there is no diff to "
            "review and no snapshot to undo your work with. Be correspondingly "
            "careful about what you change."
        )
    return "\n".join(lines)


def _rules_block(house_rules: str) -> str:
    if not house_rules.strip():
        return ""
    return (
        "\n\n# Standing project rules\n"
        "These override the general guidance above where they conflict.\n\n"
        f"{house_rules.strip()}\n"
    )


@dataclass(frozen=True)
class ConversationContext:
    """What replaying a thread costs, and the turns that fit inside the budget.

    ``conversation`` is the thread after compaction, which is what should be
    stored on the run: compacting at prompt time only and throwing the result
    away would re-summarise the same turns on every follow-up, and would let
    the transcript claim it carried detail the agents never saw.
    """

    conversation: List[Dict[str, Any]] = field(default_factory=list)
    rendered: str = ""
    compacted_turns: int = 0

    @property
    def stored_turns(self) -> int:
        return len(self.conversation)

    @property
    def characters(self) -> int:
        return len(self.rendered)

    @property
    def estimated_tokens(self) -> int:
        return (
            self.characters + ESTIMATED_CHARS_PER_TOKEN - 1
        ) // ESTIMATED_CHARS_PER_TOKEN

    def to_dict(self) -> Dict[str, int]:
        return {
            "stored_turns": self.stored_turns,
            "compacted_turns": self.compacted_turns,
            "characters": self.characters,
            "estimated_tokens": self.estimated_tokens,
            "budget_characters": MAX_HISTORY_CHARS,
        }


def _render_turn(turn: Dict[str, Any]) -> str:
    """One turn of the thread as the agents will read it."""
    summarised = bool(turn.get("compacted"))
    parts = [f"## The human asked\n{turn.get('task') or '(empty)'}"]
    for reply in turn.get("replies") or []:
        who = reply.get("label") or reply.get("stage") or "agent"
        said = str(reply.get("output") or "").strip()
        if not said:
            said = f"(no output - {reply.get('error') or 'the stage produced nothing'})"
        # Say so when the text is a summary. An agent that mistakes a clipped
        # summary for the whole answer will assume the parts it cannot see did
        # not exist, which is worse than knowing it is reading an outline.
        answered = f"{who} answered (compacted summary)" if summarised else f"{who} answered"
        parts.append(f"## {answered}\n{said}")
    note = str(turn.get("reviewer_note") or "").strip()
    if note:
        parts.append(f"## The human's steer at the approval gate\n{note}")
    outcome = str(turn.get("outcome") or "").strip()
    if outcome:
        parts.append(f"## Outcome\n{outcome}")
    return "\n\n".join(parts)


def _compact_turn(turn: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce one turn to its decisions, keeping the shape of a turn.

    Deterministic on purpose. Asking a model to summarise would spend the quota
    this app exists to conserve, and would put a paraphrase of what an agent
    said into the record of what it said. What survives is what a follow-up
    actually refers back to: the message, the outcome, the steer at the gate,
    and the opening and closing of each answer.
    """
    replies = []
    for reply in turn.get("replies") or []:
        text = " ".join(str(reply.get("output") or "").split())
        replies.append(
            {
                "stage": str(reply.get("stage") or ""),
                "label": str(reply.get("label") or reply.get("stage") or "agent"),
                "output": clip(text, MAX_COMPACT_REPLY_CHARS),
                "error": str(reply.get("error") or ""),
            }
        )
    return {
        "run_id": str(turn.get("run_id") or ""),
        "task": str(turn.get("task") or "").strip(),
        "replies": replies,
        "reviewer_note": str(turn.get("reviewer_note") or "").strip(),
        "outcome": str(turn.get("outcome") or "").strip(),
        "compacted": True,
    }


def conversation_context(
    conversation: Optional[List[Dict[str, Any]]],
    force: bool = False,
) -> ConversationContext:
    """Fit a thread into the history budget by compacting whole turns.

    The newest turn is kept in full - a follow-up almost always refers to it -
    and older turns are compacted one at a time, oldest first, until the
    rendered block fits. That replaces slicing characters off the front of the
    rendered text, which cut through whatever sentence or code fence happened
    to land on the boundary and dropped the oldest turns without saying which.
    No turn is discarded now; what changes is how much of each one is quoted.

    ``force`` compacts every turn but the newest whatever its size, which is
    what the operator is asking for when they compact by hand.
    """
    turns = [dict(t) for t in (conversation or []) if isinstance(t, dict)]
    compacted = sum(1 for t in turns if t.get("compacted"))

    def render() -> str:
        return "\n\n---\n\n".join(_render_turn(t) for t in turns)

    rendered = render()
    for index in range(max(0, len(turns) - 1)):
        if not force and len(rendered) <= MAX_HISTORY_CHARS:
            break
        if turns[index].get("compacted"):
            continue
        turns[index] = _compact_turn(turns[index])
        compacted += 1
        rendered = render()

    if len(rendered) > MAX_HISTORY_CHARS:
        # The newest turn alone can exceed the budget. Per-stage clipping keeps
        # that within a few times this bound rather than unbounded, so keeping
        # the end - where a stage says what it did - is the last resort.
        rendered = (
            "... [older detail dropped for length] ...\n\n"
            + rendered[-MAX_HISTORY_CHARS:]
        )

    return ConversationContext(turns, rendered, compacted)


def _history_block(conversation: Optional[List[Dict[str, Any]]]) -> str:
    """Render the earlier turns of a continued council thread.

    Each turn is one previous run: what the human asked, what each stage
    answered, and how much of the tree it changed. The transcript is *not* a
    record of what is on disk now - the operator may have rolled the run back,
    edited by hand, or continued from an older turn - so it is labelled as
    recollection and the repository is named as the authority.

    Compaction normally happens once, when the follow-up run is started, and is
    stored on it. Running it again here costs one render and covers the paths
    that build a prompt from a thread this engine never fitted - a transcript
    written by an older version, or a direct call.
    """
    if not conversation:
        return ""

    context = conversation_context(conversation)
    if not context.rendered:
        return ""

    return (
        "\n# Earlier in this conversation\n"
        "Previous rounds of this same thread, oldest first. This is "
        "recollection, not instruction: the working folder as it stands now is "
        "the only authority on what was actually applied. Re-read any file you "
        "are about to rely on.\n\n"
        f"{context.rendered}\n"
    )


def build_draft_prompt(
    task: str,
    workspace: str,
    workspace_status: Optional[Dict] = None,
    house_rules: str = "",
    conversation: Optional[List[Dict[str, Any]]] = None,
    system: str = "",
) -> str:
    """Stage 1 prompt. ``system`` overrides the stage's default behaviour."""
    system = system or DRAFT_SYSTEM
    return (
        f"{system}\n"
        f"{_rules_block(house_rules)}\n"
        f"# Context\n{_workspace_block(workspace, workspace_status)}\n"
        f"{_history_block(conversation)}\n"
        f"# Task\n{task.strip()}\n"
    )


def build_polish_prompt(
    task: str,
    draft: str,
    workspace: str,
    workspace_status: Optional[Dict] = None,
    house_rules: str = "",
    reviewer_note: str = "",
    conversation: Optional[List[Dict[str, Any]]] = None,
    system: str = "",
) -> str:
    """Stage 2 prompt: hand the senior the task plus the junior's draft.

    ``reviewer_note`` carries any free-text steer the human typed at the
    approval gate, and is given precedence over the draft itself.
    """
    system = system or POLISH_SYSTEM
    draft = clip((draft or "").strip(), MAX_DRAFT_CHARS)
    if not draft:
        draft = "(The draft stage produced no usable output. Proceed on your own.)"

    note = ""
    if reviewer_note.strip():
        note = (
            "\n# Human reviewer's instructions\n"
            "The human operator reviewed the draft and added this. It takes "
            "precedence over the draft where they conflict.\n\n"
            f"{reviewer_note.strip()}\n"
        )

    return (
        f"{system}\n"
        f"{_rules_block(house_rules)}\n"
        f"# Context\n{_workspace_block(workspace, workspace_status)}\n"
        f"{_history_block(conversation)}\n"
        f"# Task\n{task.strip()}\n"
        f"{note}\n"
        f"# Junior engineer's draft proposal\n"
        f"Everything below this line is the draft. Verify it before relying on "
        f"any part of it.\n\n"
        f"---\n\n{draft}\n"
    )


def _chat_history_block(conversation: Optional[List[Dict[str, Any]]]) -> str:
    """Render the earlier turns of a Solo conversation as plain dialogue.

    Deliberately thinner than the council's own history block: there is no
    draft to distinguish from a review, no gate to quote a steer from and no
    diff to warn about, so what is left is what was said.
    """
    if not conversation:
        return ""

    context = conversation_context(conversation)
    if not context.rendered:
        return ""

    return f"# Earlier in this conversation\n{context.rendered}"


def build_chat_prompt(
    task: str,
    conversation: Optional[List[Dict[str, Any]]] = None,
    behavior: str = "",
) -> str:
    """The whole prompt for one Solo Mode turn.

    Nothing is added that the operator did not put there. With no behaviour
    configured and no thread to replay this returns the message itself - which
    is the point of Solo Mode, and the one thing the council-shaped version it
    replaces could not do: that one always injected a persona, the house rules
    and a repository preamble, so a plain question never arrived as one.
    """
    message = task.strip()
    behavior = behavior.strip()
    history = _chat_history_block(conversation)
    if not behavior and not history:
        return message

    parts = [p for p in (behavior, history) if p]
    parts.append(f"# Message\n{message}")
    return "\n\n".join(parts) + "\n"


# ==========================================================================
# Projects
# ==========================================================================
#
# Three roles instead of two, and no human between the turns. That changes
# what a prompt has to carry.
#
# The council replays its own transcript, because a council thread is a
# conversation and the conversation is the point. A project cannot: it runs
# for dozens of turns across three CLIs and two of them will hit a context
# limit long before the work is done. So a project turn carries *state*, not
# history - the roadmap, the task list, the last failure, the diff - all of
# which are files on disk that any agent can re-read for itself. The engine
# passes a bounded rendering of them so the turn starts from something even
# when the file is large; the file is always the authority.
#
# Each budget below is a clip, not a cap on what the agent may look at. An
# agent that needs the whole of SPEC.md opens SPEC.md.

MAX_STATE_CHARS = 6_000
MAX_ROADMAP_CHARS = 8_000
MAX_SPEC_CHARS = 14_000
MAX_ERROR_CHARS = 6_000
MAX_DIFF_CHARS = 8_000


# What every project agent must end its turn with. One contract, quoted into
# all three system prompts, because the engine has exactly one parser.
#
# Asked for as a fenced block rather than a bare object because none of the
# three CLIs has a JSON output mode that survives a coding session - Codex and
# Antigravity have no `--output-format` for this at all, and Claude's
# stream-json wraps prose it has already written. A fence is the one thing all
# three produce reliably, and the engine reads the last one in the reply.
REPORT_CONTRACT = """\
# How to end your turn

Finish your reply with a single fenced JSON block, exactly like this and \
nothing after it:

```json
{
  "reasoning": "one short paragraph: what you did and why",
  "files_modified": ["path/one.py", "path/two.md"],
  "status": "ok",
  "tasks": [{"id": "task_1", "status": "completed"}],
  "notes": ""
}
```

Rules for that block:
- `status` is `ok` when your part of the work succeeded, `blocked` when you \
could not proceed and need another agent to act, `failed` when you tried and \
it did not work.
- `files_modified` lists every path you created, edited or deleted, relative \
to the project root. An empty list means you changed nothing.
- `tasks` is optional and only carries the ids whose status you changed. \
Leave it out if you changed none.
- Say what is true. This block is machine-read: a `status` of `ok` on work \
that failed sends the next agent off to verify something that was never \
written, and costs a full round of everyone's quota.\
"""


PROJECT_ARCHITECT_SYSTEM = """\
You are the CHIEF ARCHITECT of an autonomous build. You do the thinking that \
the other two agents execute against: system design, file and directory \
layout, breaking the work into tasks, reviewing what came back, and deciding \
what the project needs next.

You are one of three agents working the same repository in a loop with no \
human in it. A developer agent implements your tasks; a QA agent builds and \
tests the result. You will not be asked to write the bulk of the code - if \
you find yourself doing that, you have taken work that belongs to the \
developer.

HOW TO WORK:
1. Read what exists before you design anything. An empty directory and a \
half-finished project need different plans, and only one of them is what you \
have.
2. Prefer the smallest architecture that fully satisfies the brief. Every \
directory you invent is one three agents have to keep straight.
3. Write tasks a developer can act on without asking you a question. "Add \
input validation" is not a task; "reject a negative --interval in \
cmd/root.go with a usage error" is.
4. Be concrete about the toolchain: language, build command, test command. \
The QA agent runs exactly what you name, so name something that exists.\
"""


PROJECT_CODER_SYSTEM = """\
You are the LEAD DEVELOPER of an autonomous build. You write the code.

You are one of three agents working the same repository in a loop with no \
human in it. An architect has already decided the structure and written the \
task list; a QA agent will build and test whatever you produce and hand you \
back the failures.

HOW TO WORK:
1. Implement the tasks you are given, in the files the architect named. Write \
real, complete code - no stubs, no `TODO: implement`, no placeholder that \
returns a constant. Whatever you leave unfinished, QA will fail and hand \
straight back to you.
2. Write the unit tests for what you build, in the project's own test layout.
3. When you are handed a build failure, fix the cause. Read the stack trace, \
find the line, understand why it is wrong. Deleting the test, weakening the \
assertion or catching and swallowing the exception are not fixes and QA will \
be told to look for exactly that.
4. Match the conventions already in the tree - naming, error handling, import \
order, comment density. The project should read as though one person wrote it.
5. If a task is wrong or impossible as written, implement what it should have \
said and report that plainly in `notes`. Do not silently do something else.\
"""


PROJECT_QA_SYSTEM = """\
You are the QA and ENVIRONMENT specialist of an autonomous build. You are the \
only agent that decides whether the project actually works.

You are one of three agents working the same repository in a loop with no \
human in it. An architect plans, a developer implements, and you are what \
stands between "the developer says it is done" and it being done.

HOW TO WORK:
1. Inspect the tree as it really is. List the files. Read what was written, \
not what the task list claims was written.
2. Run the real build, the real linter and the real test suite for this \
project's toolchain. Actually execute them - a build you reasoned about is \
not a build.
3. When something fails, capture the ACTUAL output: the command you ran, the \
exit status, and the stack trace or compiler error verbatim. That text is \
handed to the developer as the whole of what they get to work from, so a \
paraphrase costs a round trip.
4. Report `passing` only when the build and the tests both ran and both \
succeeded. Not "no obvious problems", not "should work". If there is no test \
suite yet, that is `failing` with a note saying so - the developer's job is \
then to write one.
5. Look for work that was faked rather than done: tests that assert nothing, \
functions that return a constant to satisfy a caller, exceptions caught and \
discarded, assertions weakened since the last round. Report those as failures \
with the file and line.\
"""


def _clip_file(text: str, limit: int) -> str:
    """A file's contents for a prompt, or a plain note that there are none."""
    text = (text or "").strip()
    return clip(text, limit) if text else "(empty)"


def project_context_block(
    project_id: str,
    root: str,
    state_json: str,
    roadmap: str = "",
    spec: str = "",
    last_error: str = "",
    diff: str = "",
    house_rules: str = "",
) -> str:
    """The shared preamble every project turn starts from.

    Deliberately files rather than conversation. Three CLIs take turns here and
    none of them can see what the others were told, so the only context that
    survives a hand-off is the context that is written down - which is also
    what makes the run resumable after a crash, a context limit or a restart.
    """
    parts = [
        "# Where you are",
        f"Project id: {project_id}",
        f"Project root: {root}",
        "",
        "The orchestrator keeps its state in `.theseus/` inside that root:",
        "",
        "- `.theseus/STATE.json` - execution state, phase, and the task list.",
        "- `.theseus/ROADMAP.md` - the living checklist.",
        "- `.theseus/CRITIQUE.log` - review findings and build failures.",
        "",
        "Every path below is relative to the project root, and the files "
        "themselves are the authority - what follows is a bounded copy so you "
        "start from something. Re-read any file you are about to change.",
        "",
        "## .theseus/STATE.json",
        "```json",
        _clip_file(state_json, MAX_STATE_CHARS),
        "```",
    ]

    if roadmap.strip():
        parts += ["", "## .theseus/ROADMAP.md", _clip_file(roadmap, MAX_ROADMAP_CHARS)]
    if spec.strip():
        parts += ["", "## SPEC.md", _clip_file(spec, MAX_SPEC_CHARS)]
    if last_error.strip():
        parts += [
            "",
            "## The last failure, verbatim",
            "This is what QA captured. It is the real output of a real "
            "command, not a summary.",
            "",
            "```",
            _clip_file(last_error, MAX_ERROR_CHARS),
            "```",
        ]
    if diff.strip():
        parts += [
            "",
            "## Uncommitted changes in the working tree",
            "```diff",
            _clip_file(diff, MAX_DIFF_CHARS),
            "```",
        ]
    if house_rules.strip():
        parts += [
            "",
            "## Standing project rules",
            "These override the general guidance where they conflict.",
            "",
            house_rules.strip(),
        ]
    return "\n".join(parts)


def build_architecture_prompt(brief: str, context: str, system: str = "") -> str:
    """Phase 1. Turn the operator's brief into a spec and a task list."""
    return (
        f"{system or PROJECT_ARCHITECT_SYSTEM}\n\n"
        f"{context}\n\n"
        f"# The brief\n"
        f"This is what the operator asked for, verbatim. It is the only thing "
        f"in this run that came from a human.\n\n"
        f"{brief.strip()}\n\n"
        f"# Your turn: architecture\n"
        f"Do all of the following, writing the files yourself:\n\n"
        f"1. Write `SPEC.md` at the project root: what is being built, the "
        f"toolchain, the directory layout, and the acceptance criteria that "
        f"decide when it is finished. Name the exact build command and the "
        f"exact test command - QA runs what you name.\n"
        f"2. Write `.theseus/ROADMAP.md`: the task checklist, as Markdown "
        f"checkboxes, in the order they should be done.\n"
        f"3. Return the same tasks in your JSON block so the orchestrator can "
        f"track them. Give each an id (`task_1`, `task_2`, ...), a one-line "
        f"description a developer can act on, and `\"status\": \"pending\"`.\n\n"
        f"Between five and fifteen tasks. Fewer than five usually means a task "
        f"is really a phase; more than fifteen means you are designing the "
        f"whole of v2 before v1 builds.\n\n"
        f"Do not implement the project. That is the developer's turn, and it "
        f"is next.\n\n"
        f"{REPORT_CONTRACT}\n"
    )


def build_implementation_prompt(
    brief: str, context: str, tasks: str, fixing: bool = False, system: str = ""
) -> str:
    """Phase 2. Implement the pending tasks, or fix what QA broke."""
    if fixing:
        job = (
            "# Your turn: fix the build\n"
            "QA ran the build and it failed. The failure is quoted above, "
            "verbatim.\n\n"
            "Find the cause and fix it. Read the file and line the trace "
            "names before you change anything - the first plausible "
            "explanation is often not the right one. If the test is correct "
            "and the code is wrong, fix the code; if the test is genuinely "
            "wrong, fix the test and say so explicitly in `notes`.\n\n"
            "Do not delete the failing test, weaken its assertion, or wrap "
            "the failure in a try/except to make it pass. QA is told to look "
            "for exactly that, and it will hand it straight back.\n"
        )
    else:
        job = (
            "# Your turn: implement\n"
            "Implement the pending tasks below. Write complete, working code "
            "and the tests that go with it, in the layout SPEC.md describes.\n\n"
            "Mark each task you finished in your JSON block. Leave a task "
            "`pending` if you did not get to it - QA will run either way, and "
            "claiming a task you did not do is what turns a build into three "
            "agents chasing a file that was never written.\n"
        )

    return (
        f"{system or PROJECT_CODER_SYSTEM}\n\n"
        f"{context}\n\n"
        f"# The brief\n{brief.strip()}\n\n"
        f"{job}\n"
        f"## Tasks in front of you\n{tasks or '(none listed - work from SPEC.md)'}\n\n"
        f"{REPORT_CONTRACT}\n"
    )


def build_qa_prompt(brief: str, context: str, system: str = "") -> str:
    """Phase 3. Build it, test it, and say plainly whether it works."""
    return (
        f"{system or PROJECT_QA_SYSTEM}\n\n"
        f"{context}\n\n"
        f"# The brief\n{brief.strip()}\n\n"
        f"# Your turn: build and verify\n"
        f"1. Inspect the tree. List what is actually there against what "
        f"SPEC.md and the task list claim.\n"
        f"2. Run the build command and the test command SPEC.md names. If it "
        f"names none, work out the right ones for this toolchain and say in "
        f"`notes` what you ran.\n"
        f"3. Append your findings to `.theseus/CRITIQUE.log` - date them and "
        f"add to the file, never overwrite it. It is the record of every "
        f"round, and the developer reads it to see whether a failure is a new "
        f"one or the same one again.\n\n"
        f"Then report, in the JSON block:\n\n"
        f"- `build_status`: `passing` or `failing`. Passing means the build "
        f"and the tests both ran and both succeeded, and you watched them do "
        f"it.\n"
        f"- `last_execution_error`: on a failure, the command, the exit "
        f"status and the stack trace or compiler output *verbatim*. This is "
        f"the whole of what the developer gets. On a pass, an empty string.\n"
        f"- `status`: `ok` when you completed the verification, whichever way "
        f"it came out. `failed` only if you could not run it at all.\n\n"
        f"A failing build reported as passing is the worst outcome available "
        f"to you: the loop moves on, builds more on top of it, and nobody "
        f"finds out until the end.\n\n"
        f"{REPORT_CONTRACT}\n"
    )


def build_expansion_prompt(brief: str, context: str, system: str = "") -> str:
    """Phase 4. The base build passes; decide what v1.1 should be."""
    return (
        f"{system or PROJECT_ARCHITECT_SYSTEM}\n\n"
        f"{context}\n\n"
        f"# The brief\n{brief.strip()}\n\n"
        f"# Your turn: decide what comes next\n"
        f"The base requirements are implemented and the build passes. Propose "
        f"**two or three** enhancements that a competent maintainer would do "
        f"next - the ones that make this genuinely usable rather than merely "
        f"complete.\n\n"
        f"Judge each against the brief. An enhancement nobody asked for, in a "
        f"direction the brief never pointed, is scope you are inventing on "
        f"someone else's machine. Good candidates are usually the things a "
        f"first version leaves out: configuration, error paths, an install or "
        f"service story, sensible defaults, the flag everyone reaches for.\n\n"
        f"Do all of the following:\n\n"
        f"1. Append the new tasks to `.theseus/ROADMAP.md` under a heading "
        f"that says which round they came from.\n"
        f"2. Return them in your JSON block as `tasks`, with fresh ids that do "
        f"not collide with the existing ones, and `\"status\": \"pending\"`.\n"
        f"3. Update `SPEC.md` so the acceptance criteria cover them.\n\n"
        f"If the honest answer is that the project is finished and anything "
        f"more would be padding, say so in `notes` and return an empty "
        f"`tasks` list. That is a real answer and the orchestrator handles it.\n\n"
        f"{REPORT_CONTRACT}\n"
    )


def build_integrity_prompt(brief: str, context: str, system: str = "") -> str:
    """Phase 5, first half. Does the whole thing hold together?"""
    return (
        f"{system or PROJECT_QA_SYSTEM}\n\n"
        f"{context}\n\n"
        f"# The brief\n{brief.strip()}\n\n"
        f"# Your turn: final integrity check\n"
        f"This is the last verification before the project is handed back. "
        f"Check the whole of it, not the last change:\n\n"
        f"1. Every acceptance criterion in SPEC.md - met, or not.\n"
        f"2. A clean build and a full test run, from the state on disk.\n"
        f"3. Leftovers: stubs, dead files, `TODO` markers left where real code "
        f"was supposed to go, tests that assert nothing.\n\n"
        f"Append the result to `.theseus/CRITIQUE.log`. Report `build_status` "
        f"and, if it fails, the verbatim output in `last_execution_error` "
        f"exactly as in a normal round.\n\n"
        f"{REPORT_CONTRACT}\n"
    )


def build_handoff_prompt(brief: str, context: str, system: str = "") -> str:
    """Phase 5, second half. Write the README a human will read first."""
    return (
        f"{system or PROJECT_ARCHITECT_SYSTEM}\n\n"
        f"{context}\n\n"
        f"# The brief\n{brief.strip()}\n\n"
        f"# Your turn: hand it over\n"
        f"The build is verified. Write `README.md` at the project root, for "
        f"the person who will use this and was not here while it was built.\n\n"
        f"Cover: what it is and what it is for; how to install or build it; "
        f"how to run it, with a real example and its real output; the "
        f"configuration and flags that exist; how to run the tests. Then, "
        f"honestly, what is not finished or not verified - read "
        f"`.theseus/CRITIQUE.log` and carry forward anything still open.\n\n"
        f"Write what the code actually does. A README that describes the flag "
        f"you meant to add is worse than no README, because it is believed.\n\n"
        f"{REPORT_CONTRACT}\n"
    )
