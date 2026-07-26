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
"""

from __future__ import annotations

import argparse
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
    the fall-through is the answer there rather than a last resort.
    """
    for marker in ("# Task", "# Message"):
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
    emit("# AI Council demo artifact")
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
        "# AI Council demo artifact\n\n"
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


def run_solo(task: str) -> int:
    """Answer the message. Solo Mode is a conversation, so nothing is written."""
    emit(f"You asked: **{task}**")
    emit()
    emit(
        "I am the mock assistant. Solo Mode runs one agent with no draft "
        "stage, no approval gate and no write permission, so this is a reply "
        "rather than a change to your repository."
    )
    files = repo_files(6)
    if files:
        emit()
        emit("From here I can see:")
        emit()
        for f in files:
            emit(f"- `{f}`")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock agent CLI for AI Council.")
    parser.add_argument(
        "--role", choices=["drafter", "polisher", "solo"], default="drafter"
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

    if args.role == "solo":
        return run_solo(task)
    if args.role == "drafter":
        return run_drafter(task)
    # The senior stage only writes when it has been granted permission, which
    # mirrors how the real CLIs gate their edit tools.
    return run_polisher(task, write=zero_touch or os.environ.get("MOCK_APPLY") == "1")


if __name__ == "__main__":
    sys.exit(main())
