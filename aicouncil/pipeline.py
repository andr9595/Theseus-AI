"""The Junior Draft / Senior Polish orchestration engine.

Two modes, and they share only their start: Council is the pipeline below,
Solo is one assistant answering one message.

State machine (Council)
-----------------------

    idle
      |  start()
      v
    drafting
      |
      v
    awaiting_approval       (skipped in Zero-Touch Mode)
      |  approve()      \\  reject() / cancel()
      v                  v
    polishing --------> cancelled
      |
      +--> complete  (exit 0)
      +--> failed    (non-zero exit, timeout, or an unhandled error)

State machine (Solo)
--------------------

    idle -> running -> complete | failed | cancelled

Solo has no gate, no delivery branch, no safety snapshot and no diff, because
it never writes: its provider is invoked with ``read_only_args`` instead of
ever being granted the auto-approve flags. What it is for is a conversation
with one agent, and what it produces is an answer.

Neither mode requires a repository. A run works in the folder it was given -
or in the scratch workspace when it was given none - and the git-backed half
of the safety model (snapshot, rollback, diff, pull-request delivery) is
available exactly when that folder happens to be a git repository.

Permission to write to disk is carried by ``execute_approved``. It is true
only when Zero-Touch Mode granted it up front or a human granted it at the
gate, and it is what decides whether the Stage 2 CLI receives its
auto-approve flags. Stage 1 is read-only and never receives them.

Pull-request mode changes where that permission lands, not whether it is
granted: the branch is created after the gate, Stage 2 works on it, and a
successful run is committed, pushed and opened as a pull request. The branch
the operator started on is never written to.

Only one run executes at a time. The engine owns a worker thread; every public
method is safe to call from the HTTP handler threads.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config as cfg
from . import gitutil, prompts
from .events import EventBus
from .providers import ProviderResult, ProviderRunner, probe

# Pipeline states
IDLE = "idle"
DRAFTING = "drafting"
AWAITING_APPROVAL = "awaiting_approval"
POLISHING = "polishing"
# Solo's only working state. Its own rather than a reused POLISHING: nothing
# is being polished, and a state name that lies shows up in the status pill.
RUNNING = "running"
COMPLETE = "complete"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = {COMPLETE, FAILED, CANCELLED}

# How much of one stage's answer a follow-up run carries forward. Bounded here,
# where the turn is recorded, rather than only at prompt time: a thread copies
# its predecessor's turns into every new transcript, so an unbounded reply
# would be duplicated into each one for as long as the thread runs.
MAX_TURN_CHARS = 12_000

# How many transcripts a conversation listing reads. Threads are grouped by
# lineage, and a child is always newer than its parent, so a window of the
# newest files holds every thread whose latest run is in it. The cap is what
# keeps opening the sidebar cheap for someone with a year of runs on disk.
HISTORY_SCAN_LIMIT = 200

# How much of the senior stage's own summary is quoted into the pull request
# body. It is the description a reviewer reads first, but it is not the review
# itself - the diff is.
MAX_PR_BODY_CHARS = 8_000
# Git's own convention for a commit subject, and what GitHub shows before it
# starts eliding a pull-request title.
MAX_PR_TITLE_CHARS = 72


def resolve_workspace(folder: str) -> str:
    """The absolute directory a run should execute in.

    Blank means the scratch workspace: neither mode requires a project, and
    refusing to start without one made a plain question about Python cost a
    trip through a directory picker first.

    A folder inside a git repository resolves to that repository's root, which
    is what the diff, the snapshot and the delivery branch all operate on.
    Anything else is taken as it stands - a folder that is not a repository is
    a legitimate place to work, it simply has none of that machinery.
    """
    folder = (folder or "").strip()
    if not folder:
        return str(cfg.workspace_dir())
    path = Path(folder).expanduser()
    if not path.is_dir():
        raise ValueError(
            f"{folder!r} is not a folder that exists. Pick another one, or "
            f"clear it to work in the scratch workspace."
        )
    return gitutil.repo_root(path) or str(path.resolve())


def _transcript_workspace(data: Dict[str, Any]) -> str:
    """Where a persisted run worked. ``repo`` is what older ones called it."""
    return str(data.get("workspace") or data.get("repo") or "")


def _conversation_turn(data: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a persisted transcript to the one turn a follow-up needs.

    Deliberately not the whole transcript: the diff is left behind because the
    repository already carries it far more accurately than a snapshot taken at
    the end of a run that may since have been rolled back or hand-edited.
    """
    stages = data.get("stages") or {}
    order = data.get("stage_order") or list(stages)
    replies = []
    for stage_id in order:
        stage = stages.get(stage_id) or {}
        output = str(stage.get("output") or "").strip()
        error = str(stage.get("error") or "").strip()
        if not output and not error:
            continue
        replies.append(
            {
                "stage": stage_id,
                "label": str(stage.get("label") or stage_id),
                "output": prompts.clip(output, MAX_TURN_CHARS),
                "error": error,
            }
        )

    stat = data.get("diff_stat") or {}
    outcome = str(data.get("state") or "")
    if stat.get("files"):
        outcome += (
            f", {stat['files']} file(s) changed "
            f"(+{stat.get('insertions', 0)}/-{stat.get('deletions', 0)})"
        )
    if data.get("rollback_note"):
        outcome += ", then rolled back"

    return {
        "run_id": str(data.get("id") or ""),
        "task": str(data.get("task") or "").strip(),
        "replies": replies,
        "reviewer_note": str(data.get("reviewer_note") or "").strip(),
        "outcome": outcome,
    }


def _context_summary(
    context: prompts.ConversationContext, window_tokens: int
) -> Dict[str, Any]:
    """The context reading the UI shows for a thread.

    ``window_tokens`` is configured, not reported: no CLI tells us the size of
    the window its model was given. The percentage is therefore an estimate
    measured against an estimate, and the UI says so rather than presenting it
    as a vendor figure. It also measures only the replayed conversation - the
    task, the draft and whatever the agent reads for itself all land in the
    same window, so this is a floor on the real usage, not the total.
    """
    out: Dict[str, Any] = dict(context.to_dict())
    out["window_tokens"] = window_tokens
    out["percent"] = (
        round(100.0 * out["estimated_tokens"] / window_tokens, 1)
        if window_tokens > 0
        else None
    )
    return out


def _pull_request_text(run: Run) -> Tuple[str, str]:
    """The commit subject and pull-request body for a finished run.

    The senior stage's own summary is quoted rather than paraphrased: it is the
    only account of what was changed and why that was written by whoever made
    the change.
    """
    task = " ".join(run.task.split())
    title = task[:MAX_PR_TITLE_CHARS].rstrip()
    if len(task) > MAX_PR_TITLE_CHARS:
        title = title.rsplit(" ", 1)[0] + "..."

    stage = run.stages.get("polisher")
    summary = prompts.clip((stage.output if stage else "").strip(), MAX_PR_BODY_CHARS)

    lines = [f"**Task**\n\n{task}", ""]
    if run.reviewer_note:
        lines += [f"**Reviewer note at the approval gate**\n\n{run.reviewer_note}", ""]
    if summary:
        lines += ["**What the senior stage reported**", "", summary, ""]
    lines += [
        "---",
        "",
        f"Opened by Theseus AI, run `{run.id}`. Deliberately not merged: "
        f"review the diff before it reaches `{run.base_branch}`.",
    ]
    return title, "\n".join(lines)


@dataclass
class StageRecord:
    """One stage's execution record, as surfaced to the UI."""

    id: str
    label: str
    role: str
    state: str = "pending"  # pending | running | done | failed | skipped
    started_at: float = 0.0
    ended_at: float = 0.0
    output: str = ""
    error: str = ""
    command: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    model: str = ""  # "" means the CLI's own default was used
    effort: str = ""  # reasoning depth; "" means the CLI's own default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "role": self.role,
            "state": self.state,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration": round(self.ended_at - self.started_at, 2)
            if self.ended_at and self.started_at
            else 0.0,
            "output": self.output,
            "error": self.error,
            "command": self.command,
            "exit_code": self.exit_code,
            "model": self.model,
            "effort": self.effort,
        }


@dataclass
class Run:
    """A single end-to-end pipeline execution."""

    id: str
    task: str
    # The folder the agents run in. Not necessarily a git repository, and not
    # necessarily one the operator chose - blank in Settings means the scratch
    # workspace, which is resolved before the run is built so the transcript
    # records where the work actually happened.
    workspace: str
    zero_touch: bool
    mode: str = "council"  # "council" | "solo"
    # Pull-request delivery. The branch is named at start but only created
    # after write permission is granted, so a rejected run leaves no trace.
    pull_request_mode: bool = False
    base_branch: str = ""
    work_branch: str = ""
    pull_request: Optional[gitutil.PullRequest] = None
    # Whether this run will have a rollback point. Decided once at start, from
    # the frozen config and the folder - the toggle is worth nothing without a
    # git repository to anchor to - because the approval gate quotes it, and
    # that is the last moment it can change the operator's answer.
    snapshot_planned: bool = False
    state: str = IDLE
    created_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    stages: Dict[str, StageRecord] = field(default_factory=dict)
    diff: str = ""
    diff_stat: Dict[str, int] = field(default_factory=dict)
    snapshot: Optional[gitutil.Snapshot] = None
    reviewer_note: str = ""
    error: str = ""
    rollback_note: str = ""
    # Continuation lineage. A follow-up run is a new, independently auditable
    # run that carries the earlier turns of its thread rather than reopening
    # the transcript it came from.
    parent_run_id: str = ""
    conversation: List[Dict[str, Any]] = field(default_factory=list)
    # What that thread costs to replay, measured on the text the agents were
    # actually given. Persisted so the transcript cannot later imply it carried
    # turns in full that were compacted before either stage saw them.
    context: Dict[str, Any] = field(default_factory=dict)
    # The configuration this run was started with, read once. Settings changed
    # while the run is parked at the approval gate - including by a second app
    # instance sharing the config file - must not swap the command the human
    # approved, nor the flags it is about to be handed.
    config: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def solo(self) -> bool:
        """Whether this is a Solo conversation rather than a council run."""
        return self.mode == "solo"

    @property
    def transcript_name(self) -> str:
        """The filename this run's transcript is persisted under.

        Served with the run so the UI can offer "continue" on a run that has
        just finished, without having to reconstruct the name itself.
        """
        return f"{int(self.created_at)}-{self.id}.json"

    def to_dict(self) -> Dict[str, Any]:
        # `config` is deliberately absent: transcripts are rendered in the
        # browser and replayed from disk, and a full copy of every provider
        # command in each one is noise rather than history.
        return {
            "id": self.id,
            "file": self.transcript_name,
            "task": self.task,
            "workspace": self.workspace,
            # Kept alongside `workspace` for transcripts read by a version that
            # only knew the folder as a repository.
            "repo": self.workspace,
            "zero_touch": self.zero_touch,
            "mode": self.mode,
            # Kept alongside `mode` for transcripts written by, and read by,
            # a version that only knew the boolean.
            "solo": self.solo,
            "pull_request_mode": self.pull_request_mode,
            "base_branch": self.base_branch,
            "work_branch": self.work_branch,
            "pull_request": self.pull_request.to_dict() if self.pull_request else None,
            "snapshot_planned": self.snapshot_planned,
            "state": self.state,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
            "stage_order": [
                s for s in ("solo", "drafter", "polisher") if s in self.stages
            ],
            "diff": self.diff,
            "diff_stat": self.diff_stat,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            # Not offered once a pull request exists: rollback restores a
            # worktree, and the branch this run's work now lives on has already
            # been pushed. Undoing half of that and calling it a rollback is
            # worse than saying plainly that the PR has to be closed by hand.
            "can_rollback": (
                bool(self.snapshot)
                and self.state in TERMINAL_STATES
                and self.pull_request is None
            ),
            "reviewer_note": self.reviewer_note,
            "error": self.error,
            "rollback_note": self.rollback_note,
            "parent_run_id": self.parent_run_id,
            "conversation": self.conversation,
            "context": self.context,
        }


class PipelineBusy(RuntimeError):
    """A run is already in flight."""


class Pipeline:
    """Owns the current run and drives it on a background thread."""

    def __init__(
        self,
        store: cfg.ConfigStore,
        bus: EventBus,
        runs_dir: Optional[Path] = None,
    ) -> None:
        self.store = store
        self.bus = bus
        # Bound once, here - never re-read from the environment on each write.
        # A run persists from its worker thread, which can outlive the caller
        # that started it; resolving the directory per write means a caller
        # that changes XDG_CONFIG_HOME in between (a test restoring the real
        # environment during cleanup) sends a late transcript somewhere the
        # run was never meant to touch.
        self._runs_dir = Path(runs_dir) if runs_dir else cfg.runs_dir()
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._run: Optional[Run] = None
        self._thread: Optional[threading.Thread] = None
        self._runner: Optional[ProviderRunner] = None
        # Signalled when the human resolves the approval gate.
        self._gate = threading.Event()
        self._gate_decision = ""  # "approve" | "reject"
        self._cancel_requested = False

    # -- introspection -----------------------------------------------------

    @property
    def runs_dir(self) -> Path:
        """The directory this pipeline persists transcripts to."""
        return self._runs_dir

    @property
    def run(self) -> Optional[Run]:
        with self._lock:
            return self._run

    def snapshot_state(self) -> Dict[str, Any]:
        """Full UI state: current run plus provider availability."""
        with self._lock:
            run = self._run.to_dict() if self._run else None
        providers = self.store.get("providers", {})
        return {
            "run": run,
            "busy": self.is_busy(),
            "config": self.store.all(),
            # The agents assignable to either job. Served rather than hardcoded
            # in the browser so commands and permission flags have one home.
            "agents": cfg.agent_catalog(),
            # The role behaviours a stage can be assigned. Served rather than
            # duplicated in the browser, so the shipped text has one home.
            "roles": prompts.role_catalog(self.store.get("roles", {})),
            "providers_status": [
                probe(providers[k])
                for k in ("drafter", "polisher", "solo")
                if k in providers
            ],
        }

    def is_busy(self) -> bool:
        with self._lock:
            return self._run is not None and self._run.state not in TERMINAL_STATES

    # -- control -----------------------------------------------------------

    def start(
        self,
        task: str,
        workspace: str = "",
        continue_from: str = "",
        compact_context: bool = False,
    ) -> Run:
        """Kick off a new run. Raises PipelineBusy if one is already active.

        ``workspace`` is the folder the agents run in. Blank is allowed and
        means the scratch workspace, and a folder that is not a git repository
        is allowed too - what a run needs is somewhere to work, not a project.

        ``continue_from`` is the filename of a persisted transcript to continue.
        Its thread is carried into the new run's prompts, so the agents see
        what was asked and answered before. Continuation is deliberately
        provider-neutral - it replays the council's own transcript rather than
        resuming a CLI's private session, which not every configurable agent
        has and none of them expose. That is also why compaction happens here,
        on the council's own transcript, rather than being left to a CLI's
        ``/compact``: only one of the configurable agents has one.

        ``compact_context`` summarises every earlier turn up front instead of
        waiting for the thread to reach the budget. A long thread is compacted
        either way; this is the operator asking for it sooner.
        """
        task = (task or "").strip()
        if not task:
            raise ValueError("Task description is empty.")

        # Remembered only when the operator chose it: resolving blank to the
        # scratch workspace and then storing that would pin a folder nobody
        # picked, and the next run would open with it selected.
        chosen = (workspace or "").strip()
        root = resolve_workspace(chosen)

        conf = self.store.all()
        mode = str(conf.get("mode") or "council")
        if mode not in ("council", "solo"):
            mode = "council"
        # Delivery is council machinery. Solo never writes, never branches and
        # never commits, so there is nothing here for these to protect - and
        # refusing a conversation because the tree is dirty would be absurd.
        pull_request_mode = mode == "council" and bool(conf.get("pull_request_mode"))
        if pull_request_mode:
            # Stricter than the toggle below and checked whether or not it is
            # on: every precondition for committing, pushing and opening the PR
            # is verified here, before any agent spends quota.
            blocker = gitutil.pull_request_blocker(root)
            if blocker:
                raise ValueError(blocker)
        elif mode == "council" and conf.get("require_clean_worktree"):
            st = gitutil.status(root)
            if not st.clean:
                raise ValueError(
                    f"Working tree has {len(st.staged) + len(st.modified) + len(st.untracked)} "
                    f"uncommitted change(s) and 'require clean worktree' is on. "
                    f"Commit or stash them first."
                )

        parent_run_id = ""
        conversation: List[Dict[str, Any]] = []
        if continue_from:
            previous = self.load_run(continue_from)
            if previous is None:
                raise ValueError(
                    "That run transcript no longer exists, so there is nothing "
                    "to continue."
                )
            # Both paths went through `resolve_workspace`, so this compares
            # canonical folders rather than whatever the operator typed.
            if _transcript_workspace(previous) != root:
                raise ValueError(
                    "A conversation can only be continued in the folder it "
                    "started in."
                )
            # A council thread replayed into a plain conversation reads as an
            # agent talking to itself about a draft that no longer exists, and
            # the reverse hands the council a transcript with no stages in it.
            # `solo` covers transcripts written before `mode` existed.
            previous_mode = str(previous.get("mode") or "") or (
                "solo" if previous.get("solo") else "council"
            )
            if previous_mode != mode:
                raise ValueError(
                    f"That is a {previous_mode} conversation. Switch to "
                    f"{previous_mode.title()} mode to continue it."
                )
            earlier = previous.get("conversation")
            if isinstance(earlier, list):
                conversation.extend(t for t in earlier if isinstance(t, dict))
            conversation.append(_conversation_turn(previous))
            parent_run_id = str(previous.get("id") or "")

        # Fit the thread before the run is built, so what is stored on it is
        # what the prompts will render rather than an ideal it never used.
        context = prompts.conversation_context(conversation, force=compact_context)
        conversation = context.conversation

        with self._lock:
            if self.is_busy():
                raise PipelineBusy("A run is already in progress.")

            # Zero-Touch grants write permission, and Solo has none to grant.
            zero_touch = mode == "council" and bool(conf.get("zero_touch"))
            providers = conf.get("providers", {})

            run_id = uuid.uuid4().hex[:12]
            run = Run(
                id=run_id,
                task=task,
                workspace=root,
                zero_touch=zero_touch,
                mode=mode,
                pull_request_mode=pull_request_mode,
                # Whatever is checked out is what the pull request targets, so
                # this works for main, master or a release branch without
                # another setting to keep in sync.
                base_branch=gitutil.status(root).branch if pull_request_mode else "",
                work_branch=(
                    gitutil.branch_for(run_id, task) if pull_request_mode else ""
                ),
                snapshot_planned=(
                    mode == "council"
                    and bool(conf.get("safety_snapshot", True))
                    and gitutil.repo_root(root) is not None
                ),
                parent_run_id=parent_run_id,
                conversation=conversation,
                context=_context_summary(
                    context, int(conf.get("context_window_tokens") or 0)
                ),
                config=conf,
            )
            if mode == "solo":
                # One agent with a configuration of its own. Recorded as a
                # stage because that is how output, timings and the command
                # echo are carried to the UI - not because it is one.
                p = providers.get("solo", {})
                run.stages["solo"] = StageRecord(
                    id="solo",
                    label=p.get("label", "Assistant"),
                    role="Assistant",
                    model=str(p.get("model") or ""),
                    effort=str(p.get("effort") or ""),
                )
            else:
                d = providers.get("drafter", {})
                run.stages["drafter"] = StageRecord(
                    id="drafter",
                    label=d.get("label", "Codex"),
                    role=d.get("role", "Junior Draft"),
                    model=str(d.get("model") or ""),
                    effort=str(d.get("effort") or ""),
                )
                p = providers.get("polisher", {})
                run.stages["polisher"] = StageRecord(
                    id="polisher",
                    label=p.get("label", "Claude"),
                    role=p.get("role", "Senior Polish"),
                    model=str(p.get("model") or ""),
                    effort=str(p.get("effort") or ""),
                )

            self._run = run
            self._cancel_requested = False
            self._gate.clear()
            self._gate_decision = ""

        if chosen:
            self.store.remember_workspace(root)
        self.bus.clear()
        self.bus.publish("run_started", run=run.to_dict())

        thread = threading.Thread(
            target=self._execute, args=(run,), name=f"run-{run.id}", daemon=True
        )
        with self._lock:
            self._thread = thread
        thread.start()
        return run

    def approve(self, note: str = "") -> None:
        """Release the approval gate and continue to Stage 2."""
        with self._lock:
            if not self._run or self._run.state != AWAITING_APPROVAL:
                raise ValueError("Nothing is waiting for approval.")
            self._run.reviewer_note = (note or "").strip()
            self._gate_decision = "approve"
        self._gate.set()

    def reject(self, note: str = "") -> None:
        """Abandon the run at the approval gate without touching any files."""
        with self._lock:
            if not self._run or self._run.state != AWAITING_APPROVAL:
                raise ValueError("Nothing is waiting for approval.")
            self._run.reviewer_note = (note or "").strip()
            self._gate_decision = "reject"
        self._gate.set()

    def cancel(self) -> None:
        """Stop the run: kill any child process and release the gate."""
        with self._lock:
            self._cancel_requested = True
            runner = self._runner
            waiting = self._run is not None and self._run.state == AWAITING_APPROVAL
            if waiting:
                self._gate_decision = "reject"
        if runner:
            runner.cancel()
        if waiting:
            self._gate.set()
        self.bus.publish("log", level="warn", message="Cancellation requested.")

    def wait_for_worker(self, timeout: float = 30.0) -> bool:
        """Block until the worker thread has fully wound down.

        `is_busy()` goes false as soon as the run reaches a terminal state, but
        the worker still has to write the transcript after that. Anything that
        needs the run to be *finished* rather than merely settled - a test
        tearing down its fixtures, a shutdown path - has to wait for the
        thread itself. Returns False if it was still alive at the timeout.
        """
        with self._lock:
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def rollback(self) -> str:
        """Undo everything the run wrote, using the pre-Stage-2 snapshot."""
        with self._lock:
            run = self._run
            if not run:
                raise ValueError("No run to roll back.")
            if run.state not in TERMINAL_STATES:
                raise ValueError("Cannot roll back while a run is in progress.")
            snap = run.snapshot
        if not snap:
            raise ValueError(
                "No snapshot was taken for this run, so there is nothing to "
                "restore. Turn on 'Safety snapshot' under Delivery & recovery."
            )

        note = gitutil.restore_snapshot(snap)
        with self._lock:
            run.rollback_note = note
            run.diff = gitutil.working_diff(run.workspace)
            run.diff_stat = gitutil.diff_stat(run.workspace)
        self.bus.publish("rolled_back", message=note, run=run.to_dict())
        return note

    # -- execution ---------------------------------------------------------

    def _set_state(self, run: Run, state: str, **extra: Any) -> None:
        with self._lock:
            run.state = state
            if state in TERMINAL_STATES:
                run.ended_at = time.time()
        self.bus.publish("state", state=state, run=run.to_dict(), **extra)

    def _stage_output_cb(self, stage_id: str):
        """Build a per-stage streaming callback for ProviderRunner."""

        def cb(stream: str, line: str) -> None:
            self.bus.publish("stage_output", stage=stage_id, stream=stream, line=line)

        return cb

    def _run_stage(
        self,
        run: Run,
        stage_id: str,
        provider: Dict[str, Any],
        prompt: str,
        auto_approve: bool,
        read_only: bool = False,
    ) -> ProviderResult:
        """Run one stage. ``provider`` comes from the run's frozen config."""
        stage = run.stages[stage_id]
        stage.model = str(provider.get("model") or "")
        stage.effort = str(provider.get("effort") or "")
        stage.state = "running"
        stage.started_at = time.time()
        self.bus.publish("stage_started", stage=stage_id, run=run.to_dict())

        runner = ProviderRunner(provider, self._stage_output_cb(stage_id))
        with self._lock:
            self._runner = runner
            if self._cancel_requested:
                runner.cancel()

        result = runner.run(
            prompt, cwd=run.workspace, auto_approve=auto_approve, read_only=read_only
        )

        with self._lock:
            self._runner = None
        stage.ended_at = time.time()
        stage.output = result.stdout
        stage.command = result.command
        stage.exit_code = result.exit_code
        stage.error = result.error
        stage.state = "done" if result.ok else "failed"

        self.bus.publish(
            "stage_finished",
            stage=stage_id,
            ok=result.ok,
            run=run.to_dict(),
            result=result.to_dict(),
        )
        return result

    def _execute(self, run: Run) -> None:
        """Worker-thread body for one run, in whichever mode it was started."""
        try:
            if run.solo:
                self._execute_solo(run)
            else:
                self._execute_council(run)
        except Exception as exc:  # a crash here must not wedge the engine
            run.error = f"{type(exc).__name__}: {exc}"
            self._set_state(run, FAILED)
        finally:
            self._persist(run)

    def _execute_solo(self, run: Run) -> None:
        """One assistant, one message, one answer.

        No gate, no branch, no snapshot and no diff: there is nothing between
        the message and the reply to review, and the provider is invoked
        read-only so there is nothing to recover from either.
        """
        provider = (run.config.get("providers") or {}).get("solo", {})
        self._set_state(run, RUNNING)
        result = self._run_stage(
            run,
            "solo",
            provider,
            prompts.build_chat_prompt(
                run.task,
                run.conversation,
                behavior=str(provider.get("behavior") or ""),
            ),
            auto_approve=False,
            read_only=True,
        )

        if self._is_cancelled():
            self._finish_cancelled(run)
        elif not result.ok:
            run.error = result.error or "The assistant failed."
            self._set_state(run, FAILED)
        else:
            self._set_state(run, COMPLETE)

    def _execute_council(self, run: Run) -> None:
        """Worker-thread body for one council run: draft, gate, polish, deliver."""
        conf = run.config
        providers = conf.get("providers", {})
        house_rules = conf.get("house_rules", "")
        workspace_status = gitutil.status(run.workspace).to_dict()

        draft_text = ""
        # Whether Stage 2 may modify files. Zero-Touch grants this up front;
        # otherwise it is granted by the human at the approval gate. Reaching
        # Stage 2 without it would hand the CLI a task it cannot complete: it
        # would block on an interactive permission prompt that nothing in this
        # pipeline can answer.
        execute_approved = run.zero_touch
        # Set when Stage 1 fails, which forces the gate back on.
        draft_failed = False

        # ---- Stage 1: Junior Draft --------------------------------------
        self._set_state(run, DRAFTING)
        draft_prompt = prompts.build_draft_prompt(
            run.task, run.workspace, workspace_status, house_rules,
            run.conversation,
            system=prompts.resolve_system(
                "drafter", providers.get("drafter", {}),
                conf.get("roles", {}),
            ),
        )
        # Stage 1 is read-only by instruction, so it never receives the
        # auto-approve flags regardless of the Zero-Touch setting.
        result = self._run_stage(
            run,
            "drafter",
            providers.get("drafter", {}),
            draft_prompt,
            auto_approve=False,
        )

        if self._is_cancelled():
            self._finish_cancelled(run)
            return

        if not result.ok:
            # A failed junior is recoverable - the senior can work from the
            # task alone - but it is not what the operator asked for.
            # Continuing unattended would turn "junior drafts, senior
            # verifies" into a lone agent writing to the repo with no draft
            # and nobody watching, which is a combination nobody selected.
            # Degrade to the approval gate instead of escalating past it; the
            # operator can still approve.
            draft_failed = True
            self.bus.publish(
                "log",
                level="warn",
                message=(
                    f"Draft stage failed ({result.error}). "
                    + (
                        "Pausing for approval: Zero-Touch assumes a draft to "
                        "verify, and there is none."
                        if run.zero_touch
                        else "The senior stage can continue alone."
                    )
                ),
            )
        draft_text = result.stdout

        # ---- Approval gate ----------------------------------------------
        if not run.zero_touch or draft_failed:
            self._set_state(run, AWAITING_APPROVAL, draft=draft_text)
            self.bus.publish(
                "log",
                level="info",
                message=(
                    "Paused for review. Nothing has been written to disk yet."
                    if not draft_failed
                    else "Paused for review: the draft stage failed, so there "
                         "is nothing to verify. Approving runs the senior "
                         "stage alone against your task."
                ),
            )
            self._gate.wait()
            with self._lock:
                decision = self._gate_decision
            if decision != "approve" or self._is_cancelled():
                self._finish_cancelled(run, "Rejected at the approval gate.")
                return
            execute_approved = True

        # ---- Delivery branch --------------------------------------------
        # Created here, after permission to write has been granted, so
        # rejecting at the gate still leaves the repository untouched.
        if run.pull_request_mode:
            gitutil.create_branch(run.workspace, run.work_branch)
            self.bus.publish(
                "log",
                level="info",
                message=(
                    f"Working on {run.work_branch}. {run.base_branch} will "
                    f"not change until you merge the pull request."
                ),
            )

        # ---- Safety snapshot --------------------------------------------
        # Taken immediately before the only stage that writes to disk.
        if conf.get("safety_snapshot", True):
            try:
                run.snapshot = gitutil.take_snapshot(run.workspace)
                if run.snapshot:
                    self.bus.publish(
                        "log",
                        level="info",
                        message=f"Safety snapshot taken at {run.snapshot.head[:8]}.",
                    )
                else:
                    # Two ways to have nothing to anchor to, and the operator
                    # can act on one of them. Saying "no commits yet" about a
                    # folder that is not a repository at all sends them looking
                    # for a git history that was never going to be there.
                    why = (
                        "this repository has no commits yet"
                        if gitutil.repo_root(run.workspace)
                        else "this folder is not a git repository"
                    )
                    self.bus.publish(
                        "log",
                        level="warn",
                        message=(
                            f"No safety snapshot: {why}, so there is no state "
                            f"to restore. Rollback is unavailable for this run."
                        ),
                    )
            except (gitutil.GitError, OSError) as exc:
                # Leave no half-snapshot behind: an object that cannot restore
                # anything must not be offered as a rollback point.
                run.snapshot = None
                self.bus.publish(
                    "log",
                    level="warn",
                    message=(
                        f"Could not take a safety snapshot ({exc}). "
                        f"Rollback is unavailable for this run."
                    ),
                )

        if self._is_cancelled():
            self._finish_cancelled(run)
            return

        # ---- Stage 2: Senior Polish -------------------------------------
        self._set_state(run, POLISHING)
        polish_prompt = prompts.build_polish_prompt(
            run.task,
            draft_text,
            run.workspace,
            workspace_status,
            house_rules,
            run.reviewer_note,
            run.conversation,
            system=prompts.resolve_system(
                "polisher", providers.get("polisher", {}),
                conf.get("roles", {}),
            ),
        )

        result = self._run_stage(
            run,
            "polisher",
            providers.get("polisher", {}),
            polish_prompt,
            auto_approve=execute_approved,
        )

        # ---- Collect the diff -------------------------------------------
        try:
            run.diff = gitutil.working_diff(run.workspace)
            run.diff_stat = gitutil.diff_stat(run.workspace)
        except (gitutil.GitError, OSError) as exc:
            self.bus.publish(
                "log", level="warn", message=f"Could not read the diff: {exc}"
            )

        if self._is_cancelled():
            self._finish_cancelled(run)
            return

        if not result.ok:
            run.error = result.error or "The senior stage failed."
            self._set_state(run, FAILED)
        elif run.pull_request_mode:
            self._publish(run)
            self._set_state(run, FAILED if run.error else COMPLETE)
        else:
            self._set_state(run, COMPLETE)

    def _publish(self, run: Run) -> None:
        """Commit, push and open the pull request. Records failure on the run.

        Never raises: a publication that fails has still left the operator with
        real work on a real branch, and the run has to say where it is rather
        than crash the worker thread.
        """
        title, body = _pull_request_text(run)
        try:
            run.pull_request = gitutil.publish_pull_request(
                run.workspace, run.base_branch, run.work_branch, title, body
            )
        except (gitutil.GitError, OSError) as exc:
            run.error = (
                f"{exc} Everything this run wrote is on {run.work_branch}; "
                f"{run.base_branch} is unchanged."
            )
            return

        self.bus.publish(
            "log",
            level="info",
            message=f"Pull request opened: {run.pull_request.url}",
        )
        try:
            # The worktree diff collected a moment ago is empty now that the
            # work is committed. What the reviewer will see is the branch
            # against its base, which is also what an agent that committed its
            # own work leaves behind.
            run.diff = gitutil.branch_diff(
                run.workspace, run.base_branch, run.work_branch
            )
            run.diff_stat = gitutil.branch_diff_stat(
                run.workspace, run.base_branch, run.work_branch
            )
            # Put the operator back where they started. Left on the delivery
            # branch, the next run would quietly take *it* as the base.
            gitutil.checkout(run.workspace, run.base_branch)
        except (gitutil.GitError, OSError) as exc:
            self.bus.publish(
                "log",
                level="warn",
                message=(
                    f"The pull request is open, but tidying up afterwards "
                    f"failed ({exc}). You may still be on {run.work_branch}."
                ),
            )

    # -- helpers -----------------------------------------------------------

    def _is_cancelled(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def _finish_cancelled(self, run: Run, message: str = "Run cancelled.") -> None:
        for stage in run.stages.values():
            if stage.state in ("pending", "running"):
                stage.state = "skipped"
        run.error = message
        # A cancellation after the gate leaves the operator on the delivery
        # branch. Saying so is the difference between "nothing happened" and
        # "your repository is on a branch you did not create".
        if run.work_branch and gitutil.status(run.workspace).branch == run.work_branch:
            self.bus.publish(
                "log",
                level="warn",
                message=(
                    f"Cancelled on {run.work_branch}. Nothing was pushed and "
                    f"{run.base_branch} is unchanged."
                ),
            )
        # Only a council run can have changed the tree. Reading a diff after a
        # cancelled conversation would attribute the operator's own
        # uncommitted work to an agent that never had permission to write.
        if not run.solo:
            try:
                run.diff = gitutil.working_diff(run.workspace)
                run.diff_stat = gitutil.diff_stat(run.workspace)
            except (gitutil.GitError, OSError):
                pass
        self._set_state(run, CANCELLED)
        self._persist(run)

    def _persist(self, run: Run) -> None:
        """Write the run transcript to disk for later inspection."""
        try:
            path = self._runs_dir / run.transcript_name
            path.write_text(
                json.dumps(run.to_dict(), indent=2, default=str), encoding="utf-8"
            )
        except OSError:
            pass  # a transcript we cannot save is not worth failing a run over

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """One summary per conversation, newest first.

        A follow-up is not a separate entry in the list. It carries every
        earlier turn of its thread inside its own transcript, so the newest run
        of a thread *is* the conversation and its ancestors would only repeat
        it. What is listed is therefore the leaves of the lineage - which is
        also precisely what continuing the conversation should attach.

        A run continued twice has two leaves, and both are listed: the data
        model is a tree, and folding one branch away silently would lose work
        the operator can still see no other way.
        """
        try:
            files = sorted(
                self._runs_dir.glob("*.json"), reverse=True
            )[:HISTORY_SCAN_LIMIT]
        except OSError:
            return []

        # Read the whole window before picking leaves out of it. Two runs can
        # share a timestamp, so the filename sort alone does not guarantee a
        # child is seen before its parent, and a parent mistaken for a leaf
        # would list the same conversation twice.
        loaded: List[Tuple[str, Dict[str, Any]]] = []
        parents: set = set()
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            loaded.append((f.name, data))
            parent = str(data.get("parent_run_id") or "")
            if parent:
                parents.add(parent)

        out: List[Dict[str, Any]] = []
        for name, data in loaded:
            if str(data.get("id") or "") in parents:
                continue
            conversation = [
                t for t in (data.get("conversation") or []) if isinstance(t, dict)
            ]
            first = conversation[0] if conversation else data
            out.append(
                {
                    "id": data.get("id"),
                    # A conversation is named for what it was opened about, so
                    # the title is its first message and not its latest.
                    "title": str(first.get("task") or "")[:200],
                    "task": (data.get("task") or "")[:200],
                    "workspace": _transcript_workspace(data),
                    "state": data.get("state"),
                    "created_at": data.get("created_at"),
                    # Served so the browser can switch to the right mode before
                    # continuing, rather than let the server refuse it after.
                    # `solo` covers transcripts written before `mode` existed.
                    "mode": str(data.get("mode") or "")
                            or ("solo" if data.get("solo") else "council"),
                    "zero_touch": data.get("zero_touch"),
                    "diff_stat": data.get("diff_stat") or {},
                    # Every earlier turn, plus this run's own message.
                    "messages": len(conversation) + 1,
                    "context": data.get("context") or {},
                    "file": name,
                }
            )
            if len(out) >= limit:
                break
        return out

    def context_preview(self, name: str) -> Dict[str, Any]:
        """What continuing a persisted transcript would replay, and what it costs.

        Reported before the run starts, so the operator can compact first
        rather than discover afterwards that the thread crowded out the task.
        ``compacted`` is the same reading for the thread with every earlier turn
        summarised - what the **Compact** button would buy.
        """
        previous = self.load_run(name)
        if previous is None:
            raise ValueError("No such run transcript.")
        conversation = [
            t for t in (previous.get("conversation") or []) if isinstance(t, dict)
        ]
        conversation.append(_conversation_turn(previous))
        window = int(self.store.get("context_window_tokens") or 0)
        return {
            **_context_summary(prompts.conversation_context(conversation), window),
            "compacted": _context_summary(
                prompts.conversation_context(conversation, force=True), window
            ),
        }

    def load_run(self, name: str) -> Optional[Dict[str, Any]]:
        """Load a persisted transcript by filename."""
        # Reject any path separator: this value comes from a query string.
        if "/" in name or "\\" in name or ".." in name:
            return None
        path = self._runs_dir / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
