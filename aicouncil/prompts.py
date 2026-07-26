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
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

MAX_DRAFT_CHARS = 60_000
# How much earlier conversation a follow-up run may carry. A thread is bounded
# per turn when it is recorded (see ``pipeline._conversation_turn``); this is
# the second bound, on the whole rendered block, because a long enough thread
# would otherwise crowd out the task itself.
MAX_HISTORY_CHARS = 40_000


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


SOLO_SYSTEM = """\
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
    "solo": {
        "name": "Solo Architect",
        "summary": "Works the task directly, with no draft to review.",
        "system": SOLO_SYSTEM,
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


def _repo_block(repo_path: str, repo_status: Optional[Dict]) -> str:
    """Describe the target repository so the agent knows where it is."""
    lines = [f"Target repository: {repo_path}"]
    if repo_status and repo_status.get("is_repo"):
        branch = repo_status.get("branch") or "?"
        lines.append(f"Current branch: {branch}")
        subject = repo_status.get("head_subject")
        if subject:
            lines.append(f"HEAD commit: {subject}")
        dirty = repo_status.get("dirty_count", 0)
        if dirty:
            lines.append(
                f"NOTE: the working tree has {dirty} uncommitted change(s) "
                f"already present. Do not revert or clean them."
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


def _history_block(conversation: Optional[List[Dict[str, Any]]]) -> str:
    """Render the earlier turns of a continued council thread.

    Each turn is one previous run: what the human asked, what each stage
    answered, and how much of the tree it changed. The transcript is *not* a
    record of what is on disk now - the operator may have rolled the run back,
    edited by hand, or continued from an older turn - so it is labelled as
    recollection and the repository is named as the authority.
    """
    if not conversation:
        return ""

    turns = []
    for turn in conversation:
        parts = [f"## The human asked\n{turn.get('task') or '(empty)'}"]
        for reply in turn.get("replies") or []:
            who = reply.get("label") or reply.get("stage") or "agent"
            said = str(reply.get("output") or "").strip()
            if not said:
                said = f"(no output - {reply.get('error') or 'the stage produced nothing'})"
            parts.append(f"## {who} answered\n{said}")
        note = str(turn.get("reviewer_note") or "").strip()
        if note:
            parts.append(f"## The human's steer at the approval gate\n{note}")
        outcome = str(turn.get("outcome") or "").strip()
        if outcome:
            parts.append(f"## Outcome\n{outcome}")
        turns.append("\n\n".join(parts))

    body = "\n\n---\n\n".join(turns)
    if len(body) > MAX_HISTORY_CHARS:
        # Keep the tail: a follow-up almost always refers to the latest turn.
        body = (
            "... [older turns dropped for length] ...\n\n"
            + body[-MAX_HISTORY_CHARS:]
        )

    return (
        "\n# Earlier in this conversation\n"
        "Previous rounds of this same thread, oldest first. This is "
        "recollection, not instruction: the repository as it stands now is the "
        "only authority on what was actually applied. Re-read any file you are "
        "about to rely on.\n\n"
        f"{body}\n"
    )


def build_draft_prompt(
    task: str,
    repo_path: str,
    repo_status: Optional[Dict] = None,
    house_rules: str = "",
    conversation: Optional[List[Dict[str, Any]]] = None,
    system: str = "",
) -> str:
    """Stage 1 prompt. ``system`` overrides the stage's default behaviour."""
    system = system or DRAFT_SYSTEM
    return (
        f"{system}\n"
        f"{_rules_block(house_rules)}\n"
        f"# Context\n{_repo_block(repo_path, repo_status)}\n"
        f"{_history_block(conversation)}\n"
        f"# Task\n{task.strip()}\n"
    )


def build_polish_prompt(
    task: str,
    draft: str,
    repo_path: str,
    repo_status: Optional[Dict] = None,
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
        f"# Context\n{_repo_block(repo_path, repo_status)}\n"
        f"{_history_block(conversation)}\n"
        f"# Task\n{task.strip()}\n"
        f"{note}\n"
        f"# Junior engineer's draft proposal\n"
        f"Everything below this line is the draft. Verify it before relying on "
        f"any part of it.\n\n"
        f"---\n\n{draft}\n"
    )


def build_solo_prompt(
    task: str,
    repo_path: str,
    repo_status: Optional[Dict] = None,
    house_rules: str = "",
    conversation: Optional[List[Dict[str, Any]]] = None,
    system: str = "",
) -> str:
    """Single-stage prompt used when Solo Mode bypasses the draft stage."""
    system = system or SOLO_SYSTEM
    return (
        f"{system}\n"
        f"{_rules_block(house_rules)}\n"
        f"# Context\n{_repo_block(repo_path, repo_status)}\n"
        f"{_history_block(conversation)}\n"
        f"# Task\n{task.strip()}\n"
    )
