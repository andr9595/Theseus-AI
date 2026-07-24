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

from typing import Dict, Optional

MAX_DRAFT_CHARS = 60_000


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


def build_draft_prompt(
    task: str,
    repo_path: str,
    repo_status: Optional[Dict] = None,
    house_rules: str = "",
) -> str:
    """Stage 1 prompt: ask the junior for a read-only proposal."""
    return (
        f"{DRAFT_SYSTEM}\n"
        f"{_rules_block(house_rules)}\n"
        f"# Context\n{_repo_block(repo_path, repo_status)}\n\n"
        f"# Task\n{task.strip()}\n"
    )


def build_polish_prompt(
    task: str,
    draft: str,
    repo_path: str,
    repo_status: Optional[Dict] = None,
    house_rules: str = "",
    reviewer_note: str = "",
) -> str:
    """Stage 2 prompt: hand the senior the task plus the junior's draft.

    ``reviewer_note`` carries any free-text steer the human typed at the
    approval gate, and is given precedence over the draft itself.
    """
    draft = (draft or "").strip()
    if len(draft) > MAX_DRAFT_CHARS:
        # Keep the head (understanding/approach) and the tail (risks), which
        # is where the signal is; the middle is usually bulk code listing.
        head = draft[: MAX_DRAFT_CHARS // 2]
        tail = draft[-MAX_DRAFT_CHARS // 2 :]
        draft = f"{head}\n\n... [draft truncated for length] ...\n\n{tail}"
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
        f"{POLISH_SYSTEM}\n"
        f"{_rules_block(house_rules)}\n"
        f"# Context\n{_repo_block(repo_path, repo_status)}\n\n"
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
) -> str:
    """Single-stage prompt used when Solo Mode bypasses the draft stage."""
    return (
        f"{SOLO_SYSTEM}\n"
        f"{_rules_block(house_rules)}\n"
        f"# Context\n{_repo_block(repo_path, repo_status)}\n\n"
        f"# Task\n{task.strip()}\n"
    )
