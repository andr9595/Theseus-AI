#!/usr/bin/env python3
"""A stand-in coding agent, for demos and tests.

Neither `codex` nor `claude` needs to be installed to exercise the pipeline:
point a provider at this script and the full Draft -> Approve -> Polish ->
Diff -> Rollback loop runs for real, including writing files to the target
repository so the diff viewer has something to show.

Wire it up in Settings, one argument per line:

    Stage 1 command                    Stage 2 command
    ---------------                    ---------------
    python3                            python3
    /abs/path/scripts/mock-agent.py    /abs/path/scripts/mock-agent.py
    --role                             --role
    drafter                            polisher
    {prompt}                           {prompt}

Or run `./run.sh --doctor` after setting AI_COUNCIL_MOCK=1 to have the
defaults point here automatically.

It also speaks Projects Mode. Point the architect, coder and QA chairs at it
and the whole decision loop runs for real against a real directory: it audits
the folder read-only, plans two cards, implements a Python module with a
genuine bug in it, fails its own test suite, is handed the trace, fixes it,
gets the cards reviewed and proposes one enhancement before declaring itself
finished. Nothing about that sequence is faked - the tests really run and
really fail - which is the only way to know the loop works rather than merely
to believe it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Deliberately slow so the UI's streaming, spinners and agent rail are visible
# rather than completing in a single frame.
LINE_DELAY = float(os.environ.get("MOCK_AGENT_DELAY", "0.06"))


def emit(line: str = "") -> None:
    print(line, flush=True)
    if LINE_DELAY:
        time.sleep(LINE_DELAY)


def read_prompt(argv_prompt: str | None) -> str:
    if argv_prompt:
        return argv_prompt
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def summarise_task(prompt: str) -> str:
    """Pull the task text back out of the generated prompt.

    Solo Mode sends the message on its own when nothing else is configured, so
    the fall-through is the answer there rather than a last resort. A project
    turn calls it "the brief" and puts it in the same shape.
    """
    for marker in ("# Task", "# Message", "# The brief"):
        if marker in prompt:
            tail = prompt.split(marker, 1)[1].strip()
            return tail.splitlines()[0] if tail else "(empty task)"
    return (prompt.strip().splitlines() or ["(empty task)"])[0]


def repo_files(limit: int = 12) -> list[str]:
    """List tracked files so the output cites paths that actually exist."""
    try:
        proc = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, timeout=20, check=False
        )
        files = [f for f in proc.stdout.splitlines() if f]
        return files[:limit]
    except (OSError, subprocess.TimeoutExpired):
        return []


def run_drafter(task: str) -> int:
    files = repo_files()
    emit("## Understanding")
    emit()
    emit(f"The task is: **{task}**")
    emit()
    emit(
        "I am the junior stage, so this pass is read-only. Below is a proposal "
        "for the senior architect to verify and apply."
    )
    emit()
    emit("## Relevant code")
    emit()
    if files:
        emit(f"The repository has {len(files)} tracked file(s) in view. The ones I read:")
        emit()
        for f in files[:6]:
            emit(f"- `{f}`")
    else:
        emit("No tracked files were visible from this working directory.")
    emit()
    emit("## Approach")
    emit()
    emit("1. Add a marker file that records what the council was asked to do.")
    emit("2. Keep the change to a single file so the diff is easy to read.")
    emit()
    emit("Alternatives rejected:")
    emit()
    emit("- Editing an existing source file — too invasive for a demonstration run.")
    emit("- Doing nothing — would leave the diff viewer with nothing to render.")
    emit()
    emit("## Proposed changes")
    emit()
    emit("Create `AI_COUNCIL_DEMO.md`:")
    emit()
    emit("```markdown")
    emit("# Theseus AI demo artifact")
    emit()
    emit(f"Task: {task}")
    emit("```")
    emit()
    emit("## Risks and unknowns")
    emit()
    emit(
        "- This is a **mock** agent. It does not reason about your code; it only "
        "proves the pipeline plumbing works end to end."
    )
    emit("- The senior stage should confirm the file name does not already exist.")
    return 0


def run_polisher(task: str, write: bool) -> int:
    emit("Reviewing the junior's draft against the working tree...")
    emit()
    emit("## Review")
    emit()
    emit(
        "The draft's plan is sound for a demonstration run, though it did not "
        "check whether the target file already exists. Handling that below."
    )
    emit()

    target = Path("AI_COUNCIL_DEMO.md")
    if not write:
        emit("_Dry run: no files were modified._")
        return 0

    existed = target.exists()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    body = (
        "# Theseus AI demo artifact\n\n"
        f"This file was written by the mock senior stage at {stamp}.\n\n"
        f"**Task:** {task}\n\n"
        "It exists to demonstrate that the pipeline can modify a real working\n"
        "tree and that the diff viewer and rollback both operate on real git\n"
        "state. Deleting it is safe.\n"
    )
    try:
        target.write_text(body, encoding="utf-8")
    except OSError as exc:
        emit(f"error: could not write {target}: {exc}")
        return 1

    emit(f"{'Updated' if existed else 'Created'} `{target}`.")
    emit()
    emit("## Summary of changes")
    emit()
    emit(f"- `{target}` — {'rewrote' if existed else 'added'} the demo artifact "
         f"recording the requested task.")
    emit()
    emit("## Verification")
    emit()
    emit("- Confirmed the file was written and is readable.")
    emit("- The Diff tab should now show this change; Roll back reverts it.")
    return 0


# --------------------------------------------------------------------------
# The deliberating council
# --------------------------------------------------------------------------
#
# Like a project turn, a council turn is identified by what the prompt asks for
# rather than by `--role`: one CLI holds a member seat, critiques its peers and
# may chair, and the seating is decided per run by the router. There is no
# fixed chair to point a `--role` at.
#
# Order matters. The chairman's prompt quotes every member's answer verbatim,
# so it contains the member contract's own wording; checking for the chair
# marker first is what stops a chairman turn being mistaken for a member one.

COUNCIL_MARKERS = [
    ("# Stage 1 - independent positions", "chair"),
    ("# Your fellow members' answers", "critique"),
    ("You are one member of an AI council", "member"),
]


def council_turn(prompt: str) -> str:
    """Which council stage this prompt is asking for, or '' if none."""
    for marker, name in COUNCIL_MARKERS:
        if marker in prompt:
            return name
    return ""


def council_lens(prompt: str) -> str:
    """The persona this seat was given, so members do not all answer alike.

    A council whose members return identical text would let the critique and
    synthesis stages pass while proving nothing about either - the whole point
    of the fixture is that the chairman receives genuinely differing positions
    to reconcile.
    """
    if "PRAGMATISM" in prompt:
        return "pragmatist"
    if "LONGER VIEW" in prompt:
        return "visionary"
    if "SECURITY REVIEWER" in prompt:
        return "security"
    if "ADVERSARIAL REVIEWER" in prompt:
        return "reviewer"
    return "neutral"


def peer_aliases(prompt: str) -> list[str]:
    """The anonymised peers quoted in a critique prompt, in order."""
    return [
        line[3:].strip()
        for line in prompt.splitlines()
        if line.startswith("## Agent ")
    ]


def trailer(confidence: int, because: str) -> None:
    """The two lines every council stage is contracted to end with."""
    emit()
    emit(f"CONFIDENCE: {confidence}")
    emit(f"BECAUSE: {because}")


def run_council_member(task: str, lens: str) -> int:
    positions = {
        "pragmatist": (
            "Add the marker file and stop there. Anything larger is not paid "
            "for by this task.",
            65,
            "the task may want more than a marker file",
        ),
        "visionary": (
            "Add the marker file, but note the council has no place to record "
            "an artifact list - that is the recurring gap.",
            55,
            "the structural claim is not grounded in a file I read",
        ),
        "security": (
            "Adding the marker file is safe: it crosses no trust boundary and "
            "takes no untrusted input.",
            80,
            "I did not check whether the path is attacker-controlled",
        ),
    }
    position, confidence, because = positions.get(
        lens,
        (
            "Add a marker file recording what the council was asked to do.",
            70,
            "I did not verify the file does not already exist",
        ),
    )

    files = repo_files()
    emit("## Position")
    emit()
    emit(f"{position}")
    emit()
    emit(f"The task, as I read it: **{task}**")
    emit()
    emit("## Grounding")
    emit()
    if files:
        emit(f"I read {len(files)} tracked file(s):")
        emit()
        for f in files[:5]:
            emit(f"- `{f}`")
    else:
        emit("No tracked files were visible from this working directory.")
    emit()
    emit("## Proposal")
    emit()
    emit("Create `AI_COUNCIL_DEMO.md` recording the task.")
    emit()
    emit("## Where I could be wrong")
    emit()
    emit(f"- {because.capitalize()}.")
    emit("- I am a mock agent, so this proves plumbing rather than judgement.")
    trailer(confidence, because)
    return 0


def run_council_critique(prompt: str) -> int:
    aliases = peer_aliases(prompt)
    emit("Checked each peer's claims against the working tree.")
    emit()
    for alias in aliases or ["Agent A"]:
        emit(f"### {alias}")
        emit("**Hallucinations:** none found - the files cited do exist.")
        emit(
            "**Errors:** the position does not say what happens if the target "
            "file is already present."
        )
        emit("**Missing:** no test covers the artifact being written.")
        emit("**Sound:** the read-only survey of the tree was accurate.")
        emit()
    trailer(
        60,
        "I verified the file paths but not the git history behind them",
    )
    return 0


def run_council_chair(task: str, write: bool) -> int:
    emit("Weighed the positions against the critiques and the tree itself.")
    emit()
    emit("## Verdict")
    emit()
    emit(
        f"Write the marker file recording the task. The council agreed on the "
        f"action and split only on how much more to do, which this task does "
        f"not pay for."
    )
    emit()
    emit("## Consensus")
    emit()
    emit("- The marker file is the right change, and it is safe.")
    emit("- The tree was read accurately by every member.")
    emit()
    emit("## Disagreement")
    emit()
    emit(
        "- One member wanted a broader structural change. Not carried: no "
        "member grounded it in a file, and the critiques said so."
    )
    emit()

    target = Path("AI_COUNCIL_DEMO.md")
    if not write:
        emit("_Dry run: no files were modified._")
        emit()
        emit("## Summary of changes")
        emit()
        emit("- None. This stage was not granted permission to write.")
        emit()
        emit("## Verification")
        emit()
        emit("- Nothing to verify; nothing was applied.")
        emit()
        emit("CONSENSUS: 80")
        trailer(70, "the council was not unanimous on scope")
        return 0

    existed = target.exists()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        target.write_text(
            "# Theseus AI demo artifact\n\n"
            f"Written by the mock chairman at {stamp}.\n\n"
            f"**Task:** {task}\n\n"
            "The council deliberated, critiqued each other and this stage\n"
            "applied the outcome. Deleting this file is safe.\n",
            encoding="utf-8",
        )
    except OSError as exc:
        emit(f"error: could not write {target}: {exc}")
        return 1

    emit("## Summary of changes")
    emit()
    emit(f"- `{target}` - {'rewrote' if existed else 'added'} the demo artifact.")
    emit()
    emit("## Verification")
    emit()
    emit("- Confirmed the file was written and is readable.")
    emit()
    emit("CONSENSUS: 80")
    trailer(75, "the council was not unanimous on scope")
    return 0


def run_solo(task: str, write: bool) -> int:
    """Answer the message, and write if permission arrived with it.

    Mirrors the real CLIs the same way `run_polisher` does: the edit only
    happens when the auto-approve flag was actually passed, so a mock run
    exercises the same permission path the live one takes.
    """
    emit(f"You asked: **{task}**")
    emit()
    emit(
        "I am the mock assistant. Chat runs one agent with no draft stage and "
        "no approval gate, so this is a single reply rather than a pipeline."
    )
    files = repo_files(6)
    if files:
        emit()
        emit("From here I can see:")
        emit()
        for f in files:
            emit(f"- `{f}`")

    if not write:
        emit()
        emit("_Read-only: no files were modified._")
        return 0

    target = Path("AI_COUNCIL_DEMO.md")
    emit()
    emit(f"Writing `{target}`.")
    try:
        target.write_text(
            f"# Theseus AI demo artifact\n\n"
            f"Written by the mock assistant in Chat mode.\n\n"
            f"Task: {task}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"mock-agent: could not write {target}: {exc}",
              file=sys.stderr, flush=True)
        return 1
    emit()
    emit(f"Done. `{target}` written.")
    return 0


# --------------------------------------------------------------------------
# Projects Mode
# --------------------------------------------------------------------------
#
# A project turn is dispatched on what the prompt asks for rather than on
# `--role`, because the board decides who does what and the same chair is asked
# for different things at different times: the architect plans, reviews and
# innovates, and QA both audits and verifies. The engine's prompts name the
# turn on one line, so that line is what this reads.
#
# What it builds is deliberately tiny and real: a module, a test for it, and a
# test run that genuinely passes or fails. That is enough for the whole
# decision loop to be exercised end to end - including a failing build handed
# back to the developer - without any of it being pretended.

PROJECT_TURNS = [
    ("# Your turn: audit", "audit"),
    ("# Your turn: plan", "plan"),
    ("# Your turn: implement", "implement"),
    ("# Your turn: fix the build", "fix"),
    ("# Your turn: verify", "verify"),
    ("# Your turn: review", "review"),
    ("# Your turn: innovate", "innovate"),
]


def project_turn(prompt: str) -> str:
    """Which turn this prompt is asking for, or '' if it is not a project."""
    for marker, name in PROJECT_TURNS:
        if marker in prompt:
            return name
    return ""


def read_board() -> dict:
    """The board, read the way a real agent reads it: off disk, in the cwd."""
    try:
        with open(Path(".theseus") / "BOARD.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def column(board: dict, name: str) -> list:
    columns = board.get("columns")
    if not isinstance(columns, dict):
        return []
    return [c for c in (columns.get(name) or []) if isinstance(c, dict)]


def report(**fields) -> int:
    """Emit the JSON block the engine parses, and end the turn."""
    payload = {
        "reasoning": "",
        "files_modified": [],
        "status": "ok",
    }
    payload.update(fields)
    emit()
    emit("```json")
    for line in json.dumps(payload, indent=2).splitlines():
        emit(line)
    emit("```")
    return 0


def run_tests() -> tuple[bool, str]:
    """Actually run the fixture's test suite. Nothing here is simulated."""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", ".", "-q"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, (
        f"$ python3 -m unittest discover -s . -q\nexit {proc.returncode}\n{output}"
    )


def run_project_turn(turn: str, goal: str) -> int:
    board = read_board()
    theseus = Path(".theseus")

    if turn == "audit":
        # Read-only, and it means it: this turn is invoked without a write
        # grant, so writing here would be the fixture lying about the one
        # property the audit turn exists to have.
        listing = sorted(p.name for p in Path(".").iterdir() if not p.name.startswith("."))
        emit(f"Inspected the workspace: {len(listing)} visible entries.")
        return report(
            reasoning=(
                f"Workspace contains: {', '.join(listing) or '(empty)'}. "
                f"Python 3, standard library only; tests run with "
                f"`python3 -m unittest discover -s . -q`. Nothing of the goal "
                f"is implemented yet."
            ),
        )

    if turn == "plan":
        theseus.mkdir(parents=True, exist_ok=True)
        (theseus / "SPEC.md").write_text(
            f"# Specification\n\n**Goal:** {goal}\n\n"
            "## Toolchain\n\nPython 3, standard library only.\n\n"
            "- Test command: `python3 -m unittest discover -s . -q`\n\n"
            "## Layout\n\n- `greeter.py` - the module.\n"
            "- `test_greeter.py` - its tests.\n",
            encoding="utf-8",
        )
        emit("Planned a two-file Python module.")
        return report(
            reasoning="Broke the goal into a module and its test suite.",
            files_modified=[".theseus/SPEC.md"],
            tasks=[
                {"id": "t1", "title": "write greeter.py",
                 "detail": "greet(name) returns a greeting containing the name",
                 "column": "backlog", "kind": "task", "assigned_to": "coder"},
                {"id": "t2", "title": "write test_greeter.py",
                 "detail": "cover greet() with a real assertion",
                 "column": "backlog", "kind": "task", "assigned_to": "coder"},
            ],
        )

    if turn in ("implement", "fix"):
        # The first implementation leaves a real bug in, so the verify turn
        # genuinely fails and the build-failure path is exercised rather than
        # described. The fix turn writes the correct version.
        broken = turn == "implement" and not Path("greeter.py").exists()
        Path("greeter.py").write_text(
            "def greet(name):\n"
            + ('    return "Hello"\n' if broken else '    return f"Hello, {name}!"\n'),
            encoding="utf-8",
        )
        Path("test_greeter.py").write_text(
            "import unittest\n\nfrom greeter import greet\n\n\n"
            "class TestGreet(unittest.TestCase):\n"
            "    def test_greeting_contains_the_name(self):\n"
            '        self.assertIn("Ada", greet("Ada"))\n',
            encoding="utf-8",
        )
        files = ["greeter.py", "test_greeter.py"]

        if turn == "fix":
            emit("Read the trace, fixed the return value.")
            return report(
                reasoning="greet() ignored its argument. It now interpolates it.",
                files_modified=files,
            )

        # The engine has already moved the claimed card to In progress, so the
        # board says which one this turn is holding.
        claimed = column(board, "in_progress")
        emit(f"Implemented {len(files)} file(s).")
        return report(
            reasoning="Wrote the module and its unit test.",
            files_modified=files,
            tasks=[{"id": c["id"], "column": "in_review"} for c in claimed],
        )

    if turn == "verify":
        passing, output = run_tests()
        emit(f"Ran the test suite: {'passed' if passing else 'FAILED'}.")
        theseus.mkdir(parents=True, exist_ok=True)
        with (theseus / "CRITIQUE.log").open("a", encoding="utf-8") as fh:
            fh.write(f"\n## mock-agent verify: {'passing' if passing else 'failing'}\n")
        return report(
            reasoning=f"Ran the suite; it {'passed' if passing else 'failed'}.",
            build={
                "health": "PASSING" if passing else "FAILING",
                "log": "" if passing else output,
            },
        )

    if turn == "review":
        pending = column(board, "in_review")
        emit(f"Reviewed {len(pending)} card(s).")
        return report(
            reasoning="Read the diff; the implementation matches the cards.",
            reviews=[
                {"id": c["id"], "verdict": "approve", "note": ""} for c in pending
            ],
        )

    # innovate. One proposal, then nothing - so the loop terminates rather than
    # inventing work forever, which is the behaviour worth having a fixture for.
    already = [
        c for name in ("backlog", "in_progress", "in_review", "done")
        for c in column(board, name)
        if str(c.get("origin")) == "innovation"
    ]
    if already:
        emit("Nothing further worth adding.")
        return report(reasoning="The project is finished; more would be padding.")

    emit("Proposed one enhancement.")
    return report(
        reasoning="Empty input is the obvious gap in v1.",
        tasks=[{"id": "idea_1", "title": "handle an empty name",
                "detail": "greet('') should not return a bare comma",
                "column": "backlog", "kind": "task", "assigned_to": "coder",
                "origin": "innovation"}],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock agent CLI for Theseus AI.")
    parser.add_argument(
        "--role",
        choices=["drafter", "polisher", "solo", "architect", "coder", "qa"],
        default="drafter",
    )
    parser.add_argument("--version", action="version", version="mock-agent 1.0.0")
    parser.add_argument("prompt", nargs="?", default=None)
    # Accept and ignore the auto-approve flags so the Zero-Touch code path can
    # be exercised exactly as it would be against a real CLI.
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("--dangerously-bypass-approvals-and-sandbox", action="store_true")
    # The mirror image: what a stage that will never be approved is invoked
    # with. Accepted as a real flag rather than swallowed by `parse_known_args`
    # so that a test can assert a deliberating seat actually received it.
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--fail", action="store_true", help="exit non-zero, to test error handling")
    args, unknown = parser.parse_known_args()

    if unknown:
        print(f"note: ignoring unrecognised arguments: {' '.join(unknown)}",
              file=sys.stderr, flush=True)

    prompt = read_prompt(args.prompt)
    task = summarise_task(prompt)

    zero_touch = (
        args.dangerously_skip_permissions
        or args.dangerously_bypass_approvals_and_sandbox
    ) and not args.read_only
    emit(f"[mock-agent] role={args.role} zero-touch={'yes' if zero_touch else 'no'} "
         f"cwd={Path.cwd()}")
    emit(f"[mock-agent] received a {len(prompt)} character prompt")
    emit()

    if args.fail:
        print("mock-agent: simulated failure", file=sys.stderr, flush=True)
        return 3

    turn = project_turn(prompt)
    if turn:
        # A project turn is identified by what it asks for, not by the chair it
        # was sent to - see PROJECT_TURNS.
        return run_project_turn(turn, task)

    seat = council_turn(prompt)
    if seat == "member":
        return run_council_member(task, council_lens(prompt))
    if seat == "critique":
        return run_council_critique(prompt)
    if seat == "chair":
        # The chairman writes only where permission actually arrived, which is
        # the same gate the real CLIs apply to their edit tools.
        return run_council_chair(
            task, write=zero_touch or os.environ.get("MOCK_APPLY") == "1"
        )

    if args.role == "solo":
        return run_solo(task, write=zero_touch)
    if args.role == "drafter":
        return run_drafter(task)
    # The senior stage only writes when it has been granted permission, which
    # mirrors how the real CLIs gate their edit tools.
    return run_polisher(task, write=zero_touch or os.environ.get("MOCK_APPLY") == "1")


if __name__ == "__main__":
    sys.exit(main())
