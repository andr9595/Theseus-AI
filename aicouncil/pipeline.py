"""The deliberating council orchestration engine.

Two modes, and they share only their start: Council is the pipeline below,
Solo is one assistant answering one message.

State machine (Council)
-----------------------

    idle
      |  start()
      v
    deliberating             (members answer independently, read-only)
      |
      v
    critiquing                (members review each other, anonymised)
      |
      v
    awaiting_approval       (skipped in Zero-Touch Mode)
      |  approve()      \\  reject() / cancel()
      v                  v
    synthesizing ------> cancelled
      |
      +--> complete  (exit 0)
      +--> failed    (non-zero exit, timeout, or an unhandled error)

A run that predates this three-stage rewrite may still report the two-stage
``drafting`` / ``polishing`` states it was written in; those constants are
kept only so an old transcript's status pill still resolves to a label.

State machine (Solo)
--------------------

    idle -> running -> complete | failed | cancelled

Solo has no approval gate: nothing stands between the message and the reply
for a human to review. What it is for is a conversation with one agent, and
what it produces is an answer.

Neither mode requires a repository. A run works in the folder it was given -
or in the scratch workspace when it was given none - and the git-backed half
of the safety model (snapshot, rollback, diff, pull-request delivery) is
available exactly when that folder happens to be a git repository.

Permission to write to disk
---------------------------

One rule, both modes: a CLI receives its auto-approve flags only where that
permission has been granted, and is invoked with its ``read_only_args``
everywhere else. The two modes differ only in who can grant it.

* **Council** can always be granted it. Zero-Touch Mode grants it up front;
  otherwise a human grants it at the gate. The deliberating and critiquing
  stages are read-only by role and never receive it either way; only the
  chairman carries it, in ``execute_approved``.
* **Solo** has no gate, so Zero-Touch is the only thing that can grant it.
  Without Zero-Touch the conversation is read-only, which is the default and
  what makes "what does this repo do?" safe to ask.

Whatever writes gets the same protection: the snapshot is taken immediately
before it, the diff is collected immediately after, and rollback is offered if
the snapshot took. A read-only run skips all three - reading a diff after one
would report whatever the operator had already left in the tree as though the
agent had done it.

Pull-request mode changes where that permission lands, not whether it is
granted: the branch is created once permission exists, the writing stage works
on it, and a successful run is committed, pushed and opened as a pull request.
The branch the operator started on is never written to.

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
from . import gitutil, prompts, router
from .events import EventBus
from .providers import ProviderResult, ProviderRunner, probe_all, resolve_binary

# Pipeline states
IDLE = "idle"
# The deliberating council's three stages.
DELIBERATING = "deliberating"   # Stage 1: members answer independently
CRITIQUING = "critiquing"       # Stage 2: members review each other, anonymised
SYNTHESIZING = "synthesizing"   # Stage 3: the chairman decides and applies
# The two-stage council these replaced. No longer entered, and kept only so a
# transcript written before the rewrite still reports a state the UI can label.
DRAFTING = "drafting"
POLISHING = "polishing"
AWAITING_APPROVAL = "awaiting_approval"
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

# How long a degraded Zero-Touch run waits at the gate it did not ask for.
# Zero-Touch means nobody is at the keyboard, so the gate that a failed
# deliberation forces on has nobody to answer it; without a bound the worker
# thread parks forever and the engine refuses every later run. Overridable per
# install as ``council.gate_timeout_seconds``.
DEFAULT_GATE_TIMEOUT = 3600.0


def chat_stage_id(agent: str) -> str:
    """The stage id one CLI answers a multi-agent Chat turn under.

    Distinct from the council seat ids on purpose. Both read the same per-CLI
    configuration, but a transcript has to say which of the two a stage was:
    a council member was anonymised, critiqued and weighed, and one of three
    Chat answers was none of those things.
    """
    return f"chat_{agent}"


def stage_succeeded(result: ProviderResult) -> bool:
    """Whether a stage actually produced something, not merely exited zero.

    A CLI that hits its own quota wall, refuses a sandbox or dies inside its
    own harness can still exit 0 with an empty stdout - one of the three
    catalogued agents does exactly that. Counted as success it costs twice
    over: the seat is dropped from the deliberation, because there is no answer
    to carry, while the transcript shows it green and the router is told the
    agent did fine.
    """
    return bool(result.ok and result.stdout.strip())


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

    # The chairman's own account, falling back to the stage the two-stage
    # council used to write with so an archived run still describes itself.
    stage = run.stages.get("chair") or run.stages.get("polisher")
    summary = prompts.clip((stage.output if stage else "").strip(), MAX_PR_BODY_CHARS)

    lines = [f"**Task**\n\n{task}", ""]
    if run.reviewer_note:
        lines += [f"**Reviewer note at the approval gate**\n\n{run.reviewer_note}", ""]
    if run.seating:
        bench = ", ".join(
            f"{s.agent} as {s.alias}" for s in run.seating.members
        )
        lines += [
            f"**Council**\n\n{bench}; chaired by {run.seating.chair.agent}.",
            "",
        ]
    if summary:
        lines += ["**What the chairman reported**", "", summary, ""]
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
    # -- council seating ---------------------------------------------------
    # Which seat this record belongs to, and which of that seat's two turns it
    # is. `kind` is "position" | "critique" | "chair" | "solo" | "legacy".
    seat: str = ""
    kind: str = "legacy"
    agent: str = ""   # catalogued CLI: codex | claude | agy | custom
    alias: str = ""   # the anonymous name peers saw this seat under
    persona: str = ""
    # What this agent said about its own confidence, 0-100, and why. None means
    # it did not say - which is a real outcome and is displayed as such. This
    # app never fills the gap with a figure of its own; see prompts.py.
    confidence: Optional[int] = None
    because: str = ""
    # How far the chairman judged the members to have agreed, 0-100. Only the
    # chair states one. Carried on the stage as well as on the run because the
    # verdict card renders from the stage, and a chip that had to reach across
    # to the run for one of its two figures would go missing exactly where the
    # two are meant to be read together.
    consensus: Optional[int] = None

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
            "seat": self.seat,
            "kind": self.kind,
            "agent": self.agent,
            "alias": self.alias,
            "persona": self.persona,
            "confidence": self.confidence,
            "because": self.because,
            "consensus": self.consensus,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StageRecord":
        """Rebuild a stage record from a persisted transcript.

        Only the fields a resumed run has to know about: what this stage is,
        whether it got an answer, and the answer itself. `duration` is derived
        on the way out and is not read back.
        """
        return cls(
            id=str(data.get("id") or ""),
            label=str(data.get("label") or ""),
            role=str(data.get("role") or ""),
            state=str(data.get("state") or "pending"),
            started_at=float(data.get("started_at") or 0.0),
            ended_at=float(data.get("ended_at") or 0.0),
            output=str(data.get("output") or ""),
            error=str(data.get("error") or ""),
            command=list(data.get("command") or []),
            exit_code=data.get("exit_code"),
            model=str(data.get("model") or ""),
            effort=str(data.get("effort") or ""),
            seat=str(data.get("seat") or ""),
            kind=str(data.get("kind") or "legacy"),
            agent=str(data.get("agent") or ""),
            alias=str(data.get("alias") or ""),
            persona=str(data.get("persona") or ""),
            confidence=data.get("confidence"),
            because=str(data.get("because") or ""),
            consensus=data.get("consensus"),
        )


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
    # Whether a human has already let this run past the approval gate. Carried
    # on the run so continuing a failed chairman does not ask again: what the
    # gate approved was this bench and these positions, and continuing reuses
    # both unchanged. A run that never reached the gate has nothing approved,
    # so it stops there exactly as it did the first time.
    approved: bool = False
    # How many times this run has been continued. Kept because a transcript
    # showing one chairman answer that took three attempts should say so.
    resumed: int = 0
    # Continuation lineage. A follow-up run is a new, independently auditable
    # run that carries the earlier turns of its thread rather than reopening
    # the transcript it came from.
    # Who sat on this council, why, and under which anonymous alias. Decided
    # once at start from the task itself, then frozen: the router must not be
    # allowed to re-seat the bench between the gate and the chairman, or the
    # human would have approved one council and got another.
    seating: Optional[router.Seating] = field(default=None, repr=False)
    # How hard the critique stage was told to push, 0-5. Read off the run
    # rather than the store for the same reason.
    strictness: int = prompts.DEFAULT_STRICTNESS
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
    def stage_order(self) -> List[str]:
        """The stages of this run, in the order they were reached.

        Derived from the seating rather than stored, so a council of two and a
        council of four both render without a second list to keep in step. The
        legacy ids are the tail: a transcript from the two-stage council has no
        seating, and must still list its stages.
        """
        order: List[str] = []
        if self.seating:
            order += [s.id for s in self.seating.members]
            order += [f"{s.id}_critique" for s in self.seating.members]
            order.append("chair")
        # A multi-agent Chat turn has one stage per CLI, listed in the
        # catalogue's order so the same three answers appear in the same three
        # places every time - they are read side by side, and a bench that
        # reshuffled itself per turn would make that comparison work.
        order += [chat_stage_id(agent) for agent in cfg.AGENTS]
        return [
            s for s in [*order, "solo", "drafter", "polisher"]
            if s in self.stages
        ]

    @property
    def consensus(self) -> Optional[int]:
        """How far the chairman judged the members to have agreed, 0-100."""
        chair = self.stages.get("chair")
        return chair.consensus if chair else None

    @property
    def confidence(self) -> Optional[int]:
        """The chairman's stated confidence in the verdict, 0-100."""
        chair = self.stages.get("chair")
        return chair.confidence if chair else None

    @property
    def unfinished_stages(self) -> List[str]:
        """The stages a continuation would have to run again.

        Anything that failed, and anything the run never reached. A stage that
        answered is not in here, which is the whole point: its output is on the
        record and continuing replays it instead of paying for it twice.
        """
        return [
            sid for sid in self.stage_order
            if self.stages[sid].state in ("failed", "pending", "running")
        ]

    @property
    def can_resume(self) -> bool:
        """Whether Continue is worth offering on this run.

        Only a failed one, and only while there is something left to run. A
        cancelled run is not offered: it stopped because somebody said stop.
        """
        return self.state == FAILED and bool(self.unfinished_stages)

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
            "stage_order": self.stage_order,
            "seating": self.seating.to_dict() if self.seating else None,
            "strictness": self.strictness,
            "strictness_name": prompts.strictness(self.strictness)["name"],
            # The chairman's own two figures, lifted to the top of the run
            # because they are what the verdict card shows. Both are the
            # chairman's claims, not this app's arithmetic, and both are None
            # when it did not make them.
            "consensus": self.consensus,
            "confidence": self.confidence,
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
            "approved": self.approved,
            "resumed": self.resumed,
            # What Continue would cost, decided here rather than in the browser
            # so the button and the engine cannot disagree about whether there
            # is anything left to do.
            "can_resume": self.can_resume,
            "unfinished_stages": self.unfinished_stages,
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
        # Every provider currently executing, keyed by stage id. A dict rather
        # than the single slot this used to be: Stage 1 and Stage 2 run one
        # subprocess per seat concurrently, and Cancel has to reach all of them.
        # A single slot would leave the other seats running after the run had
        # gone terminal, still writing to the event bus.
        self._runners: Dict[str, ProviderRunner] = {}
        # Signalled when the human resolves the approval gate.
        self._gate = threading.Event()
        self._gate_decision = ""  # "approve" | "reject"
        self._cancel_requested = False
        # Optional: returns {agent: percent-of-window-used}, for the router to
        # route around an agent that is nearly out of quota. Wired by the
        # server, which owns the usage poller. Left None the router simply has
        # one less signal - it must not require a poller to seat a council.
        self.quota_source: Optional[Any] = None

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
            "providers_status": probe_all(providers, cfg.PROVIDER_ORDER),
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
        # Whether this run may modify files at all. Council earns it either at
        # the approval gate or up front with Zero-Touch, so it always *may*.
        # Chat has no gate - there is no draft between the message and the
        # reply to review - so Zero-Touch is the only way to grant it there,
        # and without it the provider is invoked read-only.
        zero_touch = bool(conf.get("zero_touch"))
        # A multi-agent Chat turn is read-only whatever Zero-Touch says, so it
        # is settled here, before `writes` decides whether this run branches,
        # snapshots or opens a pull request. Three CLIs turned loose on one
        # folder at the same time would interleave their edits, and the diff
        # afterwards could not say which agent wrote which line.
        multi_agent = mode == "solo" and bool(conf.get("multi_agent"))
        writes = (mode == "council" or zero_touch) and not multi_agent
        # Delivery protects a run that writes. On a read-only conversation
        # there is nothing to branch, nothing to commit, and refusing to start
        # because the tree is dirty would be absurd.
        pull_request_mode = writes and bool(conf.get("pull_request_mode"))
        if pull_request_mode:
            # Stricter than the toggle below and checked whether or not it is
            # on: every precondition for committing, pushing and opening the PR
            # is verified here, before any agent spends quota.
            blocker = gitutil.pull_request_blocker(root)
            if blocker:
                raise ValueError(blocker)
        elif writes and conf.get("require_clean_worktree"):
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
                    writes
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
                bench = self.available_agents(providers) if multi_agent else []
                if len(bench) > 1:
                    # One stage per installed CLI, each reading that CLI's own
                    # card in Settings. Built here rather than in the worker so
                    # all three are on screen as "queued" the moment the
                    # message is sent, instead of appearing one at a time.
                    for agent in bench:
                        p = providers.get(cfg.council_provider_id(agent)) or {}
                        stage_id = chat_stage_id(agent)
                        run.stages[stage_id] = StageRecord(
                            id=stage_id,
                            label=str(p.get("label") or cfg.AGENTS[agent]["label"]),
                            role="Assistant",
                            model=str(p.get("model") or ""),
                            effort=str(p.get("effort") or ""),
                            kind="chat",
                            agent=agent,
                        )
                else:
                    # One agent with a configuration of its own. Recorded as a
                    # stage because that is how output, timings and the command
                    # echo are carried to the UI - not because it is one.
                    #
                    # Also where a multi-agent turn lands when only one CLI is
                    # installed: a bench of one is the ordinary Chat assistant,
                    # and refusing to answer would be worse than answering.
                    p = providers.get("solo", {})
                    run.stages["solo"] = StageRecord(
                        id="solo",
                        label=p.get("label", "Assistant"),
                        role="Assistant",
                        model=str(p.get("model") or ""),
                        effort=str(p.get("effort") or ""),
                        kind="solo",
                    )
            else:
                run.strictness = prompts.strictness(
                    (conf.get("council") or {}).get("strictness")
                )["level"]
                run.seating = self.seat_council(run.task, conf, run_id=run_id)
                self._build_council_stages(run, providers, conf.get("roles", {}))

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

    # -- seating -----------------------------------------------------------

    def available_agents(self, providers: Dict[str, Any]) -> List[str]:
        """The catalogued CLIs that are actually installed and enabled.

        Returned in the catalogue's own order, which is what the router breaks
        ties on. Seating an agent whose binary does not resolve would cost a
        member per run for nothing - the seat would fail at launch every time.
        """
        out: List[str] = []
        for agent in cfg.AGENTS:
            provider = providers.get(cfg.council_provider_id(agent)) or {}
            if not provider.get("enabled", True):
                continue
            if resolve_binary(provider.get("command") or []):
                out.append(agent)
        return out

    def seat_council(
        self, task: str, conf: Dict[str, Any], run_id: str = ""
    ) -> router.Seating:
        """Route the bench for a task against a frozen configuration.

        Public because the UI asks for the same seating before a run starts, so
        the operator can see who would be seated - and re-pin - without
        spending a token to find out. Same inputs, same answer.
        """
        council = conf.get("council") or {}
        providers = conf.get("providers") or {}
        available = self.available_agents(providers)

        quota: Dict[str, Optional[float]] = {}
        if self.quota_source:
            try:
                quota = self.quota_source() or {}
            except Exception:  # noqa: BLE001 - routing must not need a poller
                quota = {}

        seating = router.route(
            # Manual routing scores every agent identically, so the pins decide
            # and the leftovers fall in catalogue order. The seating still
            # explains itself; it just stops depending on what was typed.
            task if str(council.get("routing") or "auto") != "manual" else "",
            available,
            seat_count=int(council.get("seat_count") or 3),
            chair_deliberates=bool(council.get("chair_deliberates", True)),
            pins=council.get("pins") or {},
            personas=council.get("personas") or {},
            capabilities=council.get("capabilities") or {},
            stats=council.get("stats") or {},
            quota=quota,
            run_id=run_id,
        )

        # The router decides *which CLI*; the provider id that carries its
        # command is this app's business, not the router's.
        roles = conf.get("roles") or {}
        for seat in seating.seats:
            seat.provider_id = cfg.council_provider_id(seat.agent)
            role = prompts.role_by_id(seat.persona, roles) if seat.persona else None
            seat.persona_name = str((role or {}).get("name") or "")
        return seating

    def _build_council_stages(
        self, run: Run, providers: Dict[str, Any], roles: Dict[str, Any]
    ) -> None:
        """Create every stage record this seating implies, up front.

        All of them, before any thread starts: the parallel stages publish
        `run.to_dict()` from several threads at once, and a dict that grew
        while one of them was iterating it would raise. Pre-creating the keys
        means the workers only ever mutate records, never the map.
        """
        seating = run.seating
        if seating is None:
            return
        for seat in seating.members:
            provider = providers.get(seat.provider_id) or {}
            common = {
                "seat": seat.id,
                "agent": seat.agent,
                "alias": seat.alias,
                "persona": seat.persona,
                "model": str(provider.get("model") or ""),
                "effort": str(provider.get("effort") or ""),
            }
            run.stages[seat.id] = StageRecord(
                id=seat.id,
                label=str(provider.get("label") or seat.agent),
                role=seat.persona_name or "Council Member",
                kind="position",
                **common,
            )
            run.stages[f"{seat.id}_critique"] = StageRecord(
                id=f"{seat.id}_critique",
                label=str(provider.get("label") or seat.agent),
                role="Peer critique",
                kind="critique",
                **common,
            )

        chair = seating.chair
        chair_provider = providers.get(chair.provider_id) or {}
        run.stages["chair"] = StageRecord(
            id="chair",
            label=str(chair_provider.get("label") or chair.agent),
            role="Chairman",
            kind="chair",
            seat="chair",
            agent=chair.agent,
            persona="chairman",
            model=str(chair_provider.get("model") or ""),
            effort=str(chair_provider.get("effort") or ""),
        )

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
            runners = list(self._runners.values())
            waiting = self._run is not None and self._run.state == AWAITING_APPROVAL
            if waiting:
                self._gate_decision = "reject"
        for runner in runners:
            runner.cancel()
        if waiting:
            self._gate.set()
        self.bus.publish("log", level="warn", message="Cancellation requested.")

    def revive(self, name: str) -> Run:
        """Load a failed transcript back into the engine so it can be continued.

        For the case the in-memory path cannot cover: the app was closed - or
        crashed, or was restarted to install the CLI update that fixes the
        failure - between the failed run and the decision to continue it. The
        transcript has every answer already given, so continuing from disk
        costs exactly what continuing in memory costs.

        What is *not* on the transcript is the configuration, which is not
        written into it. A revived run is therefore configured from Settings as
        it stands now. That is the same rule `resume` follows for providers,
        applied to the whole run because there is nothing else to apply.
        """
        data = self.load_run(name)
        if data is None:
            raise ValueError("That run transcript no longer exists.")
        if str(data.get("state") or "") != FAILED:
            raise ValueError("Only a failed run can be continued.")
        if data.get("pull_request_mode"):
            # A PR run's branch, its commits and possibly a published pull
            # request are all outside this transcript. Reconstructing half of
            # that and calling it the same run would be a guess about the
            # repository, so it is refused in as many words instead.
            raise ValueError(
                "That run was delivering a pull request, and the app has been "
                "restarted since. Continue it by starting a new run on the "
                "work branch."
            )

        seating = router.Seating.from_dict(data.get("seating") or {})
        mode = str(data.get("mode") or "") or ("solo" if data.get("solo") else "council")
        if mode == "council" and seating is None:
            raise ValueError(
                "That transcript has no seating recorded, so there is no bench "
                "to put back. It can be read, but not continued."
            )

        workspace = str(data.get("workspace") or data.get("repo") or "")
        if workspace and not Path(workspace).is_dir():
            raise ValueError(
                f"The folder that run worked in is gone: {workspace}"
            )

        with self._lock:
            if self._run is not None and self._run.state not in TERMINAL_STATES:
                raise PipelineBusy("A run is already in progress.")

            run = Run(
                id=str(data.get("id") or ""),
                task=str(data.get("task") or ""),
                workspace=resolve_workspace(workspace),
                zero_touch=bool(data.get("zero_touch")),
                mode=mode,
                snapshot_planned=bool(data.get("snapshot_planned")),
                state=FAILED,
                created_at=float(data.get("created_at") or time.time()),
                ended_at=float(data.get("ended_at") or 0.0),
                stages={
                    sid: StageRecord.from_dict(sdata)
                    for sid, sdata in (data.get("stages") or {}).items()
                },
                diff=str(data.get("diff") or ""),
                diff_stat=dict(data.get("diff_stat") or {}),
                snapshot=(
                    gitutil.Snapshot(**data["snapshot"])
                    if isinstance(data.get("snapshot"), dict)
                    else None
                ),
                reviewer_note=str(data.get("reviewer_note") or ""),
                error=str(data.get("error") or ""),
                approved=bool(data.get("approved")),
                resumed=int(data.get("resumed") or 0),
                seating=seating,
                strictness=int(
                    data.get("strictness") or prompts.DEFAULT_STRICTNESS
                ),
                parent_run_id=str(data.get("parent_run_id") or ""),
                conversation=list(data.get("conversation") or []),
                context=dict(data.get("context") or {}),
                config=self.store.all(),
            )
            self._run = run
            self._cancel_requested = False
            self._gate.clear()
            self._gate_decision = ""

        self.bus.publish("run_started", run=run.to_dict())
        return self.resume()

    def resume(self) -> Run:
        """Run a failed run's unfinished stages again, keeping the rest.

        The saving is the point: a council that lost only its chairman - to a
        quota wall, a timeout, a CLI that fell over - has already paid for
        every member position and every peer critique, and they are on the
        record. Continuing replays those and asks only the stage that failed.

        Not a new run. The same run resumes under the same id, so the
        transcript stays one auditable history of one task rather than two
        halves the operator has to line up by hand.
        """
        with self._lock:
            run = self._run
            if run is None:
                raise ValueError("There is no run to continue.")
            if run.state not in TERMINAL_STATES:
                raise PipelineBusy("A run is already in progress.")
            if run.state != FAILED:
                raise ValueError(
                    "Only a failed run can be continued. This one "
                    f"{run.state.replace('_', ' ')}."
                )
            if not run.unfinished_stages:
                raise ValueError(
                    "Every stage of that run answered, so there is nothing "
                    "left to continue."
                )

            # A stage that failed starts again from nothing. Leaving its error
            # and its half-written output in place would put the first
            # attempt's failure next to the second attempt's answer with no
            # way to tell which is which.
            for stage in run.stages.values():
                if stage.state in ("failed", "skipped", "running"):
                    stage.state = "pending"
                    stage.error = ""
                    stage.output = ""
                    stage.exit_code = None
                    stage.command = []
                    stage.started_at = 0.0
                    stage.ended_at = 0.0
                    stage.confidence = None
                    stage.because = ""
                    stage.consensus = None

            # Providers and roles are re-read from Settings; everything else
            # about the run stays frozen. Deliberate, and the one exception to
            # the rule that a run's configuration is fixed at start: the
            # commonest reason to continue is that a seat hit its quota wall,
            # and the commonest fix is to point that seat at a different model
            # first. Freezing the command here would mean continuing into the
            # same wall. What is *not* re-read is what the run is allowed to do
            # - Zero-Touch, pull-request delivery, the snapshot, the bench -
            # because those were decided, and in some cases approved, once.
            fresh = self.store.all()
            run.config = {
                **run.config,
                "providers": fresh.get("providers") or {},
                "roles": fresh.get("roles") or {},
            }

            run.error = ""
            run.ended_at = 0.0
            run.resumed += 1
            run.state = RUNNING
            self._cancel_requested = False
            self._gate.clear()
            self._gate_decision = ""

        kept = [
            sid for sid in run.stage_order if run.stages[sid].state == "done"
        ]
        self.bus.publish(
            "log",
            level="info",
            message=(
                f"Continuing this run. {len(kept)} answer(s) already given are "
                f"being reused; {len(run.unfinished_stages)} stage(s) will run "
                f"again."
            ),
        )
        self.bus.publish("state", state=run.state, run=run.to_dict())

        thread = threading.Thread(
            target=self._execute, args=(run,), name=f"run-{run.id}-resume", daemon=True
        )
        with self._lock:
            self._thread = thread
        thread.start()
        return run

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
        # The operator undoing the work is the strongest signal the council got
        # it wrong, and the only one that does not rely on an agent's account
        # of itself. Recorded before the event goes out.
        self._record_router_feedback(run, rolled_back=True)
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
                # A terminal event tells the browser it can attach this run to
                # the next message. Put the transcript on disk first so its
                # context preview and the follow-up itself cannot race the
                # worker thread's final persistence.
                self._persist(run)
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
        """Run one stage. ``provider`` comes from the run's frozen config.

        A stage that already answered is replayed rather than re-run. During a
        first execution nothing is `done` before it runs, so this costs
        nothing; on a continuation it is the whole saving - the members and
        their critiques are already on the record, and only the stage that
        failed is asked to spend anything.
        """
        stage = run.stages[stage_id]
        if stage.state == "done" and stage.output.strip():
            self.bus.publish(
                "log",
                level="info",
                message=(
                    f"Reusing {stage.label}'s answer from the first attempt - "
                    f"it is not being asked again."
                ),
            )
            replayed = ProviderResult(
                provider_id=str(provider.get("id") or stage_id),
                ok=True,
                exit_code=stage.exit_code if stage.exit_code is not None else 0,
                stdout=stage.output,
                stderr="",
                duration=0.0,
                command=list(stage.command),
            )
            self.bus.publish(
                "stage_finished",
                stage=stage_id,
                ok=True,
                run=run.to_dict(),
                result=replayed.to_dict(),
            )
            return replayed

        stage.model = str(provider.get("model") or "")
        stage.effort = str(provider.get("effort") or "")
        stage.state = "running"
        stage.started_at = time.time()
        self.bus.publish("stage_started", stage=stage_id, run=run.to_dict())

        runner = ProviderRunner(provider, self._stage_output_cb(stage_id))
        with self._lock:
            self._runners[stage_id] = runner
            if self._cancel_requested:
                runner.cancel()

        try:
            result = runner.run(
                prompt, cwd=run.workspace, auto_approve=auto_approve, read_only=read_only
            )
        finally:
            # Always deregistered, including on the way out of an exception. A
            # runner left in the map would be cancelled by the *next* run's
            # Cancel button, against a process that has already exited.
            with self._lock:
                self._runners.pop(stage_id, None)

        stage.ended_at = time.time()
        stage.output = result.stdout
        stage.command = result.command
        stage.exit_code = result.exit_code
        stage.error = result.error
        ok = stage_succeeded(result)
        if result.ok and not ok:
            # `result.error` is whatever the CLI said on the way out. A CLI
            # that explains itself and still exits 0 - Antigravity naming the
            # tool permission it auto-denied, say - has already given the
            # operator the one sentence worth reading, and burying it under a
            # generic "printed nothing" is how a fixable configuration problem
            # comes to look like an act of God.
            stage.error = (
                f"The CLI exited cleanly but printed nothing: {result.error}"
                if result.error else
                "The CLI exited cleanly but printed nothing. Nothing from this "
                "stage reached the rest of the run."
            )
        stage.state = "done" if ok else "failed"
        # Read the agent's own trailer off its reply. Absent is a legitimate
        # answer and stays None rather than becoming a default.
        trailer = prompts.parse_trailer(result.stdout)
        stage.confidence = trailer["confidence"]
        stage.because = trailer["because"]
        stage.consensus = trailer["consensus"]

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
            if run.solo and "solo" not in run.stages:
                # Chat, asked of every CLI at once. Told apart by the stages
                # built at start rather than by re-reading the toggle, which
                # can be flipped while the run is in flight.
                self._execute_chat_bench(run)
            elif run.solo:
                self._execute_solo(run)
            else:
                self._execute_council(run)
        except Exception as exc:  # a crash here must not wedge the engine
            run.error = f"{type(exc).__name__}: {exc}"
            self._set_state(run, FAILED)
        finally:
            if not run.solo:
                self._record_router_feedback(run)
            self._persist(run)

    def _chat_prompt(self, run: Run, behavior: str = "") -> str:
        """The prompt for one Chat turn, in either shape.

        Shared so a multi-agent turn cannot drift from a single-agent one: the
        three CLIs must be asked exactly what one would have been asked, or
        their answers are not comparable and the feature is worth nothing.
        """
        return prompts.build_chat_prompt(
            run.task,
            run.conversation,
            behavior=behavior,
            **cfg.writing_styles(run.config, "chat"),
        )

    def _execute_chat_bench(self, run: Run) -> None:
        """Every installed CLI answers the same message, at once, on its own.

        Not a council: nobody is anonymised, nothing is critiqued and nothing
        is synthesised. Three answers to one question, left as three answers -
        the comparison is the operator's to make, and this app does not rank
        them or pick a winner.

        Read-only without exception, and the run carries no diff, no snapshot
        and no delivery for the reason given where `multi_agent` is defined.
        """
        providers = run.config.get("providers") or {}
        self._set_state(run, RUNNING)

        specs = []
        for stage_id in run.stage_order:
            stage = run.stages[stage_id]
            provider = providers.get(cfg.council_provider_id(stage.agent)) or {}
            # A behaviour typed for Chat is the operator's instruction to the
            # assistant, not to a CLI, so all three carry it. Without it a
            # multi-agent turn would silently drop an instruction that a
            # single-agent turn honours.
            behavior = str((providers.get("solo") or {}).get("behavior") or "")
            specs.append((
                stage_id,
                provider,
                self._chat_prompt(run, behavior),
                False,  # never auto-approve
                True,   # always read-only
            ))

        results = self._run_parallel(run, specs)

        if self._is_cancelled():
            self._finish_cancelled(run)
            return
        # One CLI that could not run is a bench of two, which still answers the
        # question. The run fails only when nothing at all came back.
        if not any(stage_succeeded(r) for r in results.values()):
            run.error = "No agent answered."
            self._set_state(run, FAILED)
            return
        self._set_state(run, COMPLETE)

    def _execute_solo(self, run: Run) -> None:
        """One assistant, one message, one answer.

        There is no gate here - nothing stands between the message and the
        reply for a human to review - so whether it may write is settled before
        it starts. Zero-Touch grants it, exactly as it does for the council;
        without it the provider is invoked with its read-only arguments and
        there is nothing to recover from either.
        """
        provider = (run.config.get("providers") or {}).get("solo", {})
        # Read off the run, not the store: the toggle can be flipped mid-run,
        # and what this conversation was granted was decided when it started.
        writes = run.zero_touch

        if writes:
            self._prepare_branch(run)
            self._take_snapshot(run)
            if self._is_cancelled():
                self._finish_cancelled(run)
                return

        self._set_state(run, RUNNING)
        result = self._run_stage(
            run,
            "solo",
            provider,
            # Still bare, even when it may write. Nothing is added that the
            # operator did not put there: permission to change files is not a
            # reason to start injecting a persona, the house rules or a
            # repository preamble into a message they typed themselves. The
            # style switches below are the operator's own, set for Chat in
            # Settings, and add nothing when they are off.
            self._chat_prompt(run, str(provider.get("behavior") or "")),
            auto_approve=writes,
            read_only=not writes,
        )

        # Only when it could have written. Asking git for a diff after a
        # read-only conversation would report whatever the operator had already
        # left in the tree as though the assistant had done it.
        if writes:
            try:
                run.diff = gitutil.working_diff(run.workspace)
                run.diff_stat = gitutil.diff_stat(run.workspace)
            except (gitutil.GitError, OSError) as exc:
                self.bus.publish(
                    "log", level="warn", message=f"Could not read the diff: {exc}"
                )

        if self._is_cancelled():
            self._finish_cancelled(run)
        elif not stage_succeeded(result):
            run.error = run.stages["solo"].error or "The assistant failed."
            self._set_state(run, FAILED)
        elif writes and run.pull_request_mode:
            self._publish(run)
            self._set_state(run, FAILED if run.error else COMPLETE)
        else:
            self._set_state(run, COMPLETE)

    def _run_parallel(
        self, run: Run, specs: List[Tuple[str, Dict[str, Any], str, bool, bool]]
    ) -> Dict[str, ProviderResult]:
        """Run several stages at once, one thread and one subprocess each.

        Returns the results that completed, keyed by stage id; a seat that
        raised is absent rather than present-and-broken, so callers filter by
        membership instead of inspecting a sentinel.

        A seat that throws must not take the council with it. One CLI missing
        its binary is a bench of two, which is still a council - the run only
        fails when nothing at all survives, and that is decided by the caller
        rather than here.
        """
        results: Dict[str, ProviderResult] = {}

        def work(
            stage_id: str,
            provider: Dict[str, Any],
            prompt: str,
            auto_approve: bool,
            read_only: bool,
        ) -> None:
            try:
                results[stage_id] = self._run_stage(
                    run, stage_id, provider, prompt, auto_approve, read_only
                )
            except Exception as exc:  # noqa: BLE001 - one seat, not the run
                stage = run.stages.get(stage_id)
                if stage:
                    stage.state = "failed"
                    stage.error = f"{type(exc).__name__}: {exc}"
                    stage.ended_at = time.time()
                self.bus.publish(
                    "log",
                    level="warn",
                    message=f"{stage_id} could not run: {exc}",
                )
                self.bus.publish(
                    "stage_finished", stage=stage_id, ok=False, run=run.to_dict(),
                    result={"error": str(exc), "ok": False},
                )

        threads = [
            threading.Thread(
                target=work, args=spec, name=f"{run.id}-{spec[0]}", daemon=True
            )
            for spec in specs
        ]
        for t in threads:
            t.start()
        # Joined without a timeout on purpose: each ProviderRunner already
        # enforces its own, and Cancel reaches every one of them through
        # `_runners`. A timeout here would orphan a live subprocess.
        for t in threads:
            t.join()
        return results

    def _skip_unrun_critiques(self, run: Run, seating: router.Seating) -> None:
        """Close off the critique stages that were never dispatched.

        A seat only critiques if its own position survived, so a bench of
        three with one silent member cross-evaluates in two. The stage record
        for the third has to say `skipped` rather than stay `pending`: the
        thread hides a pending stage with no output, so what the operator saw
        was three positions, two critiques and no explanation.
        """
        for seat in seating.members:
            stage = run.stages.get(f"{seat.id}_critique")
            if stage and stage.state == "pending":
                stage.state = "skipped"
                stage.error = "No position from this seat, so it had no peers to review."

    def _persona_system(self, seat: router.Seat, roles: Dict[str, Any]) -> str:
        """The lens a seat brings, as edited in the Roles catalogue.

        Blank for the neutral council member, which is what the stage contract
        alone should feel like.
        """
        role = prompts.role_by_id(seat.persona, roles) if seat.persona else None
        return str((role or {}).get("system") or "")

    def _seat_provider(self, run: Run, seat: router.Seat) -> Dict[str, Any]:
        """The frozen provider config a seat runs under."""
        providers = run.config.get("providers", {})
        return providers.get(seat.provider_id) or {}

    def _execute_council(self, run: Run) -> None:
        """One council run: deliberate, critique, gate, synthesise, deliver.

        Three stages, and the permission model is positional exactly as it was
        with two: every seat in Stages 1 and 2 is invoked with its CLI's
        read-only arguments and never receives the auto-approve flags, and the
        chairman in Stage 3 is the only thing that can write. The gate still
        sits immediately before the only stage that can change a file, so
        rejecting still leaves the repository untouched.
        """
        conf = run.config
        roles = conf.get("roles", {})
        house_rules = conf.get("house_rules", "")
        # One reading for the whole run, off the config frozen at start: a
        # mid-run edit in the gear would otherwise leave the chairman writing in
        # a different voice to the members it is synthesising. Projects is the
        # deliberate exception - see `ProjectEngine._context`.
        styles = cfg.writing_styles(conf, "council")
        workspace_status = gitutil.status(run.workspace).to_dict()
        seating = run.seating
        if seating is None:  # start() always seats a council run
            raise RuntimeError("Council run reached execution with no seating.")

        # Zero-Touch grants it up front; `approved` is a grant a human already
        # made at the gate on an earlier attempt at this same run.
        execute_approved = run.zero_touch or run.approved
        # Set when the deliberation produced too little to synthesise from,
        # which forces the gate back on even under Zero-Touch.
        degraded = False

        # ---- Stage 1: independent positions ------------------------------
        self._set_state(run, DELIBERATING)
        self.bus.publish(
            "log",
            level="info",
            message=(
                f"Council seated: "
                + ", ".join(f"{s.alias} ({s.agent})" for s in seating.members)
                + f", chaired by {seating.chair.agent}."
            ),
        )
        for note in seating.notes:
            self.bus.publish("log", level="warn", message=note)

        # Read-only is a role here, enforced by what the CLI is invoked with.
        # A provider that declares no `read_only_args` - the Custom command
        # preset ships with none - is held to it by the prompt alone, which is
        # a weaker promise than the one the stage makes and is worth saying out
        # loud rather than implying.
        unguarded = sorted({
            str(self._seat_provider(run, seat).get("label") or seat.agent)
            for seat in seating.members
            if not (self._seat_provider(run, seat).get("read_only_args") or [])
        })
        if unguarded:
            self.bus.publish(
                "log",
                level="warn",
                message=(
                    f"{', '.join(unguarded)} has no read-only arguments "
                    f"configured, so nothing but the prompt stops it writing "
                    f"during the deliberation. Set them under Agents in "
                    f"Settings."
                ),
            )

        results = self._run_parallel(
            run,
            [
                (
                    seat.id,
                    self._seat_provider(run, seat),
                    prompts.build_member_prompt(
                        run.task,
                        run.workspace,
                        workspace_status,
                        house_rules,
                        run.conversation,
                        persona_system=self._persona_system(seat, roles),
                        **styles,
                    ),
                    False,  # never auto-approved
                    True,   # and explicitly read-only
                )
                for seat in seating.members
            ],
        )

        if self._is_cancelled():
            self._finish_cancelled(run)
            return

        # What survived. A seat that failed is simply not at the table for the
        # rest of the run; it is not replaced, because a substitute would not
        # have deliberated independently with the others.
        positions = [
            {"seat": seat, "alias": seat.alias, "output": results[seat.id].stdout}
            for seat in seating.members
            if seat.id in results and stage_succeeded(results[seat.id])
        ]
        if len(positions) < len(seating.members):
            lost = len(seating.members) - len(positions)
            self.bus.publish(
                "log",
                level="warn",
                message=(
                    f"{lost} of {len(seating.members)} members produced no "
                    f"answer. The council continues with {len(positions)}."
                ),
            )

        if not positions:
            degraded = True
            self.bus.publish(
                "log",
                level="warn",
                message=(
                    "No member produced an answer. "
                    + (
                        "Pausing for approval: Zero-Touch assumes a "
                        "deliberation to synthesise, and there is none."
                        if run.zero_touch
                        else "The chairman can still work the task alone."
                    )
                ),
            )
        elif len({p["seat"].agent for p in positions}) < 2:
            # One voice is not a quorum however many chairs were laid out. It
            # happens two ways: seats duplicated onto the only installed CLI,
            # and everyone but one member failing. Either way what reaches the
            # chairman is a single opinion with nothing to weigh it against,
            # and Zero-Touch writing that unattended is not what the toggle
            # promises - so the gate comes back on.
            degraded = True
            self.bus.publish(
                "log",
                level="warn",
                message=(
                    f"Only one CLI ({positions[0]['seat'].agent}) produced an "
                    f"answer, so there was no second opinion to weigh it "
                    f"against. "
                    + (
                        "Pausing for approval rather than writing on one "
                        "voice unattended."
                        if run.zero_touch
                        else "The chairman is working from a single position."
                    )
                ),
            )

        # ---- Stage 2: peer critique --------------------------------------
        # Skipped when there are not two positions to compare: a lone member
        # handed its own answer back under an alias would be reviewing itself
        # while believing it was reviewing a colleague, which is worse than
        # not running the stage at all.
        critiques: List[Dict[str, Any]] = []
        if len(positions) >= 2:
            self._set_state(run, CRITIQUING)
            critique_results = self._run_parallel(
                run,
                [
                    (
                        f"{p['seat'].id}_critique",
                        self._seat_provider(run, p["seat"]),
                        prompts.build_critique_prompt(
                            run.task,
                            # Every position except this seat's own.
                            [
                                {"alias": o["alias"], "output": o["output"]}
                                for o in positions
                                if o["seat"].id != p["seat"].id
                            ],
                            run.workspace,
                            workspace_status,
                            house_rules,
                            persona_system=self._persona_system(p["seat"], roles),
                            strictness_level=run.strictness,
                            **styles,
                            # Its own answer, named as its own. A fresh process
                            # has no memory of Stage 1, so without this it
                            # cannot say where a peer differs from what it
                            # argued itself.
                            own_position=p["output"],
                        ),
                        False,
                        True,
                    )
                    for p in positions
                ],
            )
            if self._is_cancelled():
                self._finish_cancelled(run)
                return
            # A seat with no position has no peers to review and never ran.
            # Saying so is the difference between a bench of three that
            # cross-evaluated in pairs and one that silently did two of three:
            # a stage left `pending` is filtered out of the thread entirely,
            # so the missing card looked like a rendering quirk.
            self._skip_unrun_critiques(run, seating)
            critiques = [
                {
                    "alias": p["seat"].alias,
                    "output": critique_results[f"{p['seat'].id}_critique"].stdout,
                }
                for p in positions
                if f"{p['seat'].id}_critique" in critique_results
                and stage_succeeded(critique_results[f"{p['seat'].id}_critique"])
            ]
        else:
            self._skip_unrun_critiques(run, seating)
            if positions:
                self.bus.publish(
                    "log",
                    level="info",
                    message=(
                        "Only one position survived, so there was nothing to "
                        "peer-review. Going straight to the chairman."
                    ),
                )

        # ---- Approval gate ----------------------------------------------
        # Unmoved: it is still the last thing before the only stage that can
        # write, and what it shows is now the whole deliberation rather than
        # one draft.
        # `run.approved` is the continuation case: this bench and these
        # positions have already been through the gate once, and re-asking
        # would make the operator approve the identical deliberation twice to
        # get one chairman answer.
        if (not run.zero_touch or degraded) and not run.approved:
            self._set_state(run, AWAITING_APPROVAL)
            self.bus.publish(
                "log",
                level="info",
                message=(
                    "Paused for review. Nothing has been written to disk yet."
                    if not degraded
                    else "Paused for review: the council did not reach a "
                         "quorum. Approving runs the chairman on what little "
                         "there is."
                ),
            )
            # A gate the operator chose waits as long as they need. A gate
            # forced onto a Zero-Touch run has nobody to answer it - that is
            # what the toggle said - so it waits a bounded time and then gives
            # up, which is what it would have done had the chairman failed.
            # Parking forever also leaves `is_busy()` true and refuses every
            # later run and project.
            timeout = (
                float(
                    (conf.get("council") or {}).get("gate_timeout_seconds")
                    or DEFAULT_GATE_TIMEOUT
                )
                if run.zero_touch
                else None
            )
            if not self._gate.wait(timeout):
                self._finish_cancelled(
                    run,
                    f"Zero-Touch was on, the council did not reach a quorum, "
                    f"and nobody approved the run within {int(timeout or 0)}s. "
                    f"Nothing was written.",
                )
                return
            with self._lock:
                decision = self._gate_decision
            if decision != "approve" or self._is_cancelled():
                self._finish_cancelled(run, "Rejected at the approval gate.")
                return
            execute_approved = True
            run.approved = True

        # ---- Delivery branch and safety snapshot ------------------------
        # Both happen here, after permission to write has been granted, so
        # rejecting at the gate still leaves the repository untouched.
        self._prepare_branch(run)
        self._take_snapshot(run)

        if self._is_cancelled():
            self._finish_cancelled(run)
            return

        # ---- Stage 3: the chairman synthesises and applies ---------------
        self._set_state(run, SYNTHESIZING)
        chair_role = prompts.role_by_id("chairman", roles) or {}
        chair_provider = dict(self._seat_provider(run, seating.chair))
        # The chair is usually the same CLI as one of the members, but it is
        # the only one that applies a patch, so it gets its own ceiling.
        chair_timeout = (conf.get("council") or {}).get("chair_timeout_seconds")
        if chair_timeout:
            chair_provider["timeout_seconds"] = chair_timeout

        result = self._run_stage(
            run,
            "chair",
            chair_provider,
            prompts.build_chairman_prompt(
                run.task,
                [{"alias": p["alias"], "output": p["output"]} for p in positions],
                critiques,
                run.workspace,
                workspace_status,
                house_rules,
                run.reviewer_note,
                run.conversation,
                strictness_level=run.strictness,
                system=str(chair_role.get("system") or ""),
                **styles,
            ),
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

        if not stage_succeeded(result):
            run.error = run.stages["chair"].error or "The chairman failed."
            # A failed chairman in delivery mode leaves the operator standing
            # on the work branch, and the next run reads the current branch as
            # its base - which is the hazard `_publish` checks out to avoid on
            # the way past. Not checked out here: whatever the chairman half
            # wrote is still uncommitted in the tree, and carrying that onto
            # the base branch is worse than staying put. Said out loud
            # instead, as the cancelled path does.
            self._warn_left_on_work_branch(run)
            self._set_state(run, FAILED)
        elif run.pull_request_mode:
            self._publish(run)
            self._set_state(run, FAILED if run.error else COMPLETE)
        else:
            self._set_state(run, COMPLETE)

    def _prepare_branch(self, run: Run) -> None:
        """Cut the delivery branch, if this run is delivering on one.

        Called once permission to write exists, never before: a run that is
        rejected at the gate, or a read-only conversation, must leave the
        repository exactly as it found it.
        """
        if not run.pull_request_mode:
            return
        if run.resumed and gitutil.status(run.workspace).branch == run.work_branch:
            # A continuation is already standing on the branch the first
            # attempt cut. `git checkout -b` would fail on it, and failing the
            # run over a branch that already exists is the opposite of what
            # continuing is for.
            return
        gitutil.create_branch(run.workspace, run.work_branch)
        self.bus.publish(
            "log",
            level="info",
            message=(
                f"Working on {run.work_branch}. {run.base_branch} will "
                f"not change until you merge the pull request."
            ),
        )

    def _take_snapshot(self, run: Run) -> None:
        """Anchor the tree immediately before anything writes to it."""
        if not run.config.get("safety_snapshot", True):
            return
        if run.snapshot is not None:
            # A continuation keeps the first attempt's anchor. Re-taking it
            # here would anchor to whatever the failed chairman had already
            # written, and rollback would then restore the half-applied state
            # rather than the tree the run started from.
            return
        try:
            run.snapshot = gitutil.take_snapshot(run.workspace)
            if run.snapshot:
                self.bus.publish(
                    "log",
                    level="info",
                    message=f"Safety snapshot taken at {run.snapshot.head[:8]}.",
                )
            else:
                # Two ways to have nothing to anchor to, and the operator can
                # act on one of them. Saying "no commits yet" about a folder
                # that is not a repository at all sends them looking for a git
                # history that was never going to be there.
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

    def _warn_left_on_work_branch(self, run: Run, why: str = "") -> None:
        """Say so when a run ends with the repository on its delivery branch.

        The next run takes whatever is checked out as its pull-request base, so
        being left somewhere nobody chose is not cosmetic.
        """
        if not run.work_branch:
            return
        try:
            here = gitutil.status(run.workspace).branch
        except (gitutil.GitError, OSError):
            return
        if here != run.work_branch:
            return
        self.bus.publish(
            "log",
            level="warn",
            message=(
                f"{why or 'Ended'} on {run.work_branch}. Nothing was pushed and "
                f"{run.base_branch} is unchanged - switch back to "
                f"{run.base_branch} before the next run, or it will branch "
                f"from here."
            ),
        )

    def _finish_cancelled(self, run: Run, message: str = "Run cancelled.") -> None:
        for stage in run.stages.values():
            if stage.state in ("pending", "running"):
                stage.state = "skipped"
        run.error = message
        # A cancellation after the gate leaves the operator on the delivery
        # branch. Saying so is the difference between "nothing happened" and
        # "your repository is on a branch you did not create".
        self._warn_left_on_work_branch(run, "Cancelled")
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

    def _record_router_feedback(self, run: Run, rolled_back: bool = False) -> None:
        """Fold this run's seats back into the router's history.

        This is the only part of LLMRouter's training loop that survives the
        port: there is no benchmark sweep here, so the signal is whatever the
        runs themselves produce. Bounded hard in `router.score_history` for
        exactly that reason.

        A rollback is recorded against the chairman alone. It is the seat that
        wrote the code the operator threw away; the members proposed, which is
        not the same thing and should not be penalised the same way.

        What is recorded is deliberately narrow. A stage that never ran carries
        no information about the agent that would have run it - an exception in
        the engine, or a cancellation, used to be written into the history as a
        failure by every seat - so only stages that actually finished are
        sampled. Even then the signal is "the CLI answered", not "the answer
        was good"; the only judgement in here is the operator's rollback.
        """
        if not run.seating:
            return
        try:
            council = dict(self.store.get("council") or {})
            stats = dict(council.get("stats") or {})
            weights = run.seating.profile.weights

            if rolled_back:
                router.record_outcome(
                    stats, run.seating.chair.agent, weights, ok=False,
                    rolled_back=True,
                )
            else:
                for seat in run.seating.members:
                    stage = run.stages.get(seat.id)
                    if not stage or stage.state not in ("done", "failed"):
                        continue
                    router.record_outcome(
                        stats, seat.agent, weights,
                        ok=stage.state == "done",
                    )
                chair_stage = run.stages.get("chair")
                if chair_stage and chair_stage.state in ("done", "failed"):
                    router.record_outcome(
                        stats, run.seating.chair.agent, weights,
                        ok=chair_stage.state == "done",
                    )

            council["stats"] = stats
            self.store.update({"council": council})
        except Exception:  # noqa: BLE001
            # Losing a feedback sample is not worth failing a finished run
            # over, and the router works without any history at all.
            pass

    def _persist(self, run: Run) -> None:
        """Write the run transcript to disk for later inspection."""
        try:
            path = self._runs_dir / run.transcript_name
            path.write_text(
                json.dumps(run.to_dict(), indent=2, default=str), encoding="utf-8"
            )
        except OSError:
            pass  # a transcript we cannot save is not worth failing a run over

    def history(self, limit: int = 50, mode: str = "") -> List[Dict[str, Any]]:
        """One summary per conversation, newest first.

        ``mode`` narrows the list to conversations of that kind. The two are
        not interchangeable - the server refuses to replay a council thread
        into a chat, and the reverse - so listing them together offers the
        operator conversations that clicking cannot continue.

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
            # `solo` covers transcripts written before `mode` existed.
            run_mode = str(data.get("mode") or "") or (
                "solo" if data.get("solo") else "council"
            )
            if mode and run_mode != mode:
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
                    "mode": run_mode,
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

    def _transcript_path(self, name: str) -> Optional[Path]:
        """Resolve a transcript filename to a path inside the runs directory.

        The name arrives from a query string or a request body, so anything
        that could climb out of the directory is refused outright rather than
        normalised - there is no legitimate transcript name containing a
        separator, and this is the only guard between a client and `unlink`.
        """
        if not name or "/" in name or "\\" in name or ".." in name:
            return None
        if not name.endswith(".json"):
            return None
        path = self._runs_dir / name
        return path if path.parent == self._runs_dir else None

    def delete_run(self, name: str) -> bool:
        """Delete one transcript. False if there was nothing to delete."""
        path = self._transcript_path(name)
        if path is None or not path.exists():
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True

    def clear_history(self, mode: str = "") -> int:
        """Delete every transcript, or every one of a given mode.

        Returns how many went. Unreadable files are counted as neither kept
        nor deleted when a mode is given: without knowing which mode a
        transcript belongs to, removing it would be a guess.
        """
        try:
            files = list(self._runs_dir.glob("*.json"))
        except OSError:
            return 0

        removed = 0
        for f in files:
            if mode:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(data, dict):
                    continue
                run_mode = str(data.get("mode") or "") or (
                    "solo" if data.get("solo") else "council"
                )
                if run_mode != mode:
                    continue
            try:
                f.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def load_run(self, name: str) -> Optional[Dict[str, Any]]:
        """Load a persisted transcript by filename."""
        path = self._transcript_path(name)
        if path is None or not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
