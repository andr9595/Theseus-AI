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
and the whole five-phase loop runs for real against a real directory: it
writes a spec, implements a two-file Python module with a genuine bug in it,
fails its own test suite, is handed the trace, fixes it, proposes an
enhancement and writes a README. Nothing about that sequence is faked - the
tests really run and really fail - which is the only way to know the loop
works rather than merely to believe it.
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
# `--role`, because the same chair is asked for different things in different
# phases: the architect designs in phase 1 and writes the README in phase 5,
# and QA verifies a round in phase 3 and the whole build in phase 5. The
# engine's prompts name the turn on one line, so that line is what this reads.
#
# What it builds is deliberately tiny and real: a module, a test for it, and a
# test run that genuinely passes or fails. That is enough for the five-phase
# loop to be exercised end to end - including a failing build handed back to
# the developer - without any of it being pretended.

PROJECT_TURNS = [
    ("# Your turn: architecture", "architecture"),
    ("# Your turn: implement", "implement"),
    ("# Your turn: fix the build", "fix"),
    ("# Your turn: build and verify", "qa"),
    ("# Your turn: decide what comes next", "expand"),
    ("# Your turn: final integrity check", "integrity"),
    ("# Your turn: hand it over", "handoff"),
]


def project_turn(prompt: str) -> str:
    """Which phase this prompt is asking for, or '' if it is not a project."""
    for marker, name in PROJECT_TURNS:
        if marker in prompt:
            return name
    return ""


def report(**fields) -> int:
    """Emit the JSON block the engine parses, and end the turn."""
    payload = {
        "reasoning": "",
        "files_modified": [],
        "status": "ok",
        "notes": "mock-agent: this build is a fixture, not real engineering.",
    }
    payload.update(fields)
    emit()
    emit("```json")
    for line in json.dumps(payload, indent=2).splitlines():
        emit(line)
    emit("```")
    return 0


def run_project_turn(turn: str, brief: str) -> int:
    theseus = Path(".theseus")
    theseus.mkdir(parents=True, exist_ok=True)

    if turn == "architecture":
        Path("SPEC.md").write_text(
            f"# Specification\n\n**Brief:** {brief}\n\n"
            "## Toolchain\n\nPython 3, standard library only.\n\n"
            "- Build command: `python3 -m compileall -q .`\n"
            "- Test command: `python3 -m unittest discover -s . -q`\n\n"
            "## Layout\n\n- `greeter.py` - the module.\n"
            "- `test_greeter.py` - its tests.\n\n"
            "## Acceptance criteria\n\n"
            "1. `greet(name)` returns a greeting containing the name.\n"
            "2. The test suite passes.\n",
            encoding="utf-8",
        )
        (theseus / "ROADMAP.md").write_text(
            "# Roadmap\n\n- [ ] task_1 - write `greeter.py`\n"
            "- [ ] task_2 - write `test_greeter.py`\n",
            encoding="utf-8",
        )
        emit("Designed a two-file Python module and wrote the spec.")
        return report(
            reasoning="Laid out a minimal Python module and its test suite.",
            files_modified=["SPEC.md", ".theseus/ROADMAP.md"],
            tasks=[
                {"id": "task_1", "description": "write greeter.py",
                 "status": "pending", "assigned_to": "coder"},
                {"id": "task_2", "description": "write test_greeter.py",
                 "status": "pending", "assigned_to": "coder"},
            ],
        )

    if turn in ("implement", "fix"):
        # The first pass leaves a real bug in, so the QA turn genuinely fails
        # and the build-failure path is exercised rather than described. The
        # fix turn writes the correct version.
        broken = turn == "implement" and not Path("greeter.py").exists()
        Path("greeter.py").write_text(
            'def greet(name):\n'
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
        emit(f"{'Implemented' if turn == 'implement' else 'Fixed'} the module and its test.")
        return report(
            reasoning="Wrote the module and the unit test.",
            files_modified=["greeter.py", "test_greeter.py"],
            tasks=[
                {"id": "task_1", "status": "completed"},
                {"id": "task_2", "status": "completed"},
            ],
        )

    if turn in ("qa", "integrity"):
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", ".", "-q"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        passing = proc.returncode == 0
        output = (proc.stdout + proc.stderr).strip()
        emit(f"Ran the test suite: exit {proc.returncode}.")
        with (theseus / "CRITIQUE.log").open("a", encoding="utf-8") as fh:
            fh.write(f"\n## mock-agent {turn}: {'passing' if passing else 'failing'}\n")
        return report(
            reasoning=f"Ran the suite; it {'passed' if passing else 'failed'}.",
            build_status="passing" if passing else "failing",
            last_execution_error=(
                "" if passing
                else f"$ python3 -m unittest discover -s . -q\n"
                     f"exit {proc.returncode}\n{output}"
            ),
        )

    if turn == "expand":
        with (theseus / "ROADMAP.md").open("a", encoding="utf-8") as fh:
            fh.write("\n## Expansion\n\n- [ ] task_3 - accept an empty name\n")
        emit("Proposed one enhancement.")
        return report(
            reasoning="Empty input is the obvious gap in v1.",
            files_modified=[".theseus/ROADMAP.md"],
            tasks=[{"id": "task_3", "description": "accept an empty name",
                    "status": "pending", "assigned_to": "coder"}],
        )

    # handoff
    Path("README.md").write_text(
        f"# Greeter\n\n{brief}\n\n## Usage\n\n"
        "```python\nfrom greeter import greet\ngreet(\"Ada\")\n```\n\n"
        "## Tests\n\n```\npython3 -m unittest discover -s . -q\n```\n\n"
        "## Not verified\n\nBuilt by mock-agent as a fixture.\n",
        encoding="utf-8",
    )
    emit("Wrote the README.")
    return report(reasoning="Documented usage and tests.",
                  files_modified=["README.md"])


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
    )
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

    if args.role == "solo":
        return run_solo(task, write=zero_touch)
    if args.role == "drafter":
        return run_drafter(task)
    # The senior stage only writes when it has been granted permission, which
    # mirrors how the real CLIs gate their edit tools.
    return run_polisher(task, write=zero_touch or os.environ.get("MOCK_APPLY") == "1")


if __name__ == "__main__":
    sys.exit(main())
