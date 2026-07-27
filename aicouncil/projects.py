"""The Projects engine: three agents, five phases, nobody watching.

Council is one task, two stages and a human at the gate. Projects is the other
end of that: a brief goes in, and an architect, a developer and a QA agent
take turns against the same directory until the thing builds, passes its own
tests and has a README - across more turns than any one of them can hold in a
context window.

The phase cycle
---------------

::

    [1 Architecture] --> [2 Implementation] --> [3 QA & build]
           ^                     ^                     |
           |                     | build failing       |
           |                     +---------------------+
           |                                           | build passing
    [5 Finalisation] <-- [4 Feature expansion] <--------+
           |
           v
       COMPLETED

Phase 4 does not implement anything itself: it decides what v1.1 should be,
adds those tasks, and drops the run back into phase 2 so the same developer
and the same QA agent build and verify them the way they did the base. When
the expansion budget is spent, a passing build goes to phase 5 instead -
a final integrity check, then a README - and the project is done.

Who owns what
-------------

The engine owns ``.theseus/STATE.json``. Agents are told to read it and never
to write it, and the engine rewrites it after every step, so a run always has
one authoritative record of where it is. What agents *do* own is
``.theseus/ROADMAP.md`` (the living checklist, prose) and
``.theseus/CRITIQUE.log`` (append-only findings), plus the project's own
files. Structured changes - a task finished, a build failed - come back
through the JSON block each agent ends its turn with, which the engine parses
and applies. That split is deliberate: prose files tolerate three agents
editing them, a state machine does not.

Surviving a context limit
-------------------------

No turn depends on the previous turn's conversation. Every prompt is built
from files on disk - the state, the roadmap, the spec, the last failure
verbatim - so an agent that dies mid-phase costs one step, not the run. When a
step fails in a way that looks like exhaustion (a token or quota limit, a
timeout, a non-zero exit), the engine marks ``continuation_token_needed`` and
re-runs that same step on a *different* agent, which is why the roles are
configured as three independent providers rather than one CLI with three
prompts.

Permission to write
-------------------

A project writes. It cannot do anything else: phase 1 creates SPEC.md, phase 2
writes the codebase, phase 3 appends to CRITIQUE.log. So every project step is
invoked with its agent's auto-approve flags, and there is no configuration
that changes that. Starting a project *is* the grant - which is why it takes a
deliberate press of a button, names the folder it will write to, and takes a
git snapshot first where there is one to take.

Only one project runs at a time, and never alongside a council or chat run:
they would be two agents editing the same tree with no idea the other existed.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config as cfg
from . import gitutil, prompts
from .events import EventBus
from .providers import ProviderResult, ProviderRunner, probe

# -- The state machine -----------------------------------------------------

# Status values. These are the schema's enum and the whole of it: nothing else
# is ever written to `status`, because the file is read by agents that were
# told what the six values mean.
PLANNING = "PLANNING"
IMPLEMENTING = "IMPLEMENTING"
TESTING = "TESTING"
EXPANDING = "EXPANDING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

TERMINAL_STATUSES = {COMPLETED, FAILED}

# Phases are what the engine actually dispatches on. `status` is derived from
# the phase below, and cannot carry the machine on its own: phase 5 has two
# steps, a verification and a write-up, and the schema has no word for either
# that is not already spoken for.
PHASE_ARCHITECTURE = 1
PHASE_IMPLEMENTATION = 2
PHASE_QA = 3
PHASE_EXPANSION = 4
PHASE_FINALISATION = 5

PHASE_NAMES = {
    PHASE_ARCHITECTURE: "Architecture",
    PHASE_IMPLEMENTATION: "Implementation",
    PHASE_QA: "QA & build",
    PHASE_EXPANSION: "Feature expansion",
    PHASE_FINALISATION: "Finalisation",
}

# The three jobs, and the provider each is configured under. The ids are the
# provider keys in config, so every picker the council already has - agent,
# model, effort, availability - works on these unchanged.
ROLES = ("architect", "coder", "qa")
ROLE_LABELS = {
    "architect": "Architect",
    "coder": "Developer",
    "qa": "QA",
}

# What a task looks like. `assigned_to` is a role id, not an agent id: which
# CLI holds the developer's chair is a setting, and a task written in round
# one must not name a binary the operator reassigned in round three.
TASK_STATUSES = ("pending", "in_progress", "completed")

# Files the engine manages, relative to the project root.
THESEUS_DIR = ".theseus"
STATE_FILE = "STATE.json"
ROADMAP_FILE = "ROADMAP.md"
CRITIQUE_FILE = "CRITIQUE.log"

# How much of the roadmap and the state file the browser is sent. The tracker
# renders both live, and a project that has run for an hour has a critique log
# far larger than anything worth pushing down an SSE stream on every step.
MAX_UI_TEXT = 24_000
# Step records held for the UI. A long project produces hundreds; the whole
# history is in CRITIQUE.log and the transcript, and the tracker only ever
# shows the recent ones.
MAX_STEP_RECORDS = 200

# A reply that ends in a fenced JSON block, which is the contract in
# `prompts.REPORT_CONTRACT`. Matched non-greedily from the *last* fence, since
# an agent explaining the contract back to us would otherwise win.
FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.DOTALL)

# What exhaustion looks like from outside the process. None of the three CLIs
# has a distinct exit code for it, so this reads their prose - which means it
# is a heuristic and is treated as one: a false positive costs one hand-off to
# another agent, which is survivable, and a false negative just looks like an
# ordinary failed step.
EXHAUSTION_RE = re.compile(
    r"(context (?:window |length |limit )?(?:exceeded|too long|full)"
    r"|maximum context"
    r"|prompt is too long"
    r"|too many tokens"
    r"|token limit"
    r"|exceeds? the (?:maximum )?(?:token|context)"
    r"|rate limit"
    r"|usage limit reached"
    r"|quota (?:exceeded|exhausted)"
    r"|out of (?:credits|quota)"
    r"|please try again later)",
    re.IGNORECASE,
)


class ProjectBusy(RuntimeError):
    """A project is already running."""


# --------------------------------------------------------------------------
# Reading what an agent reported
# --------------------------------------------------------------------------


def parse_report(text: str) -> Dict[str, Any]:
    """Pull the JSON block out of an agent's reply. Never raises.

    Returns ``{}`` when there is nothing parseable, which the caller treats as
    "it did the work but did not tell me about it" rather than as a failure.
    That distinction matters: a phase failed on a missing report would burn a
    retry on an agent whose only mistake was formatting, and the files it
    wrote are on disk either way for QA to find.

    Fences are searched from the last one backwards. An agent that quotes the
    contract while explaining itself puts an example block earlier in the
    reply, and the real report is always the thing it ends with.
    """
    if not text:
        return {}

    candidates = [m.strip() for m in FENCED_JSON_RE.findall(text)]
    # A reply with no fence at all may still end in a bare object.
    tail = text.rstrip()
    if tail.endswith("}"):
        start = tail.rfind("\n{")
        if start != -1:
            candidates.append(tail[start + 1 :])

    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and (
            "status" in data or "files_modified" in data or "reasoning" in data
        ):
            return data
    return {}


def _as_task(entry: Any, fallback_role: str = "coder") -> Optional[Dict[str, Any]]:
    """Normalise one reported task, or None if it is not one."""
    if not isinstance(entry, dict):
        return None
    task_id = str(entry.get("id") or "").strip()
    if not task_id:
        return None
    status = str(entry.get("status") or "pending").strip().lower()
    if status not in TASK_STATUSES:
        status = "pending"
    assigned = str(entry.get("assigned_to") or "").strip().lower()
    return {
        "id": task_id,
        "description": str(entry.get("description") or "").strip(),
        "status": status,
        "assigned_to": assigned if assigned in ROLES else fallback_role,
    }


def merge_tasks(
    existing: List[Dict[str, Any]], reported: Any, fallback_role: str = "coder"
) -> List[Dict[str, Any]]:
    """Apply an agent's reported tasks onto the list the engine is holding.

    A reported task with an id already present updates that entry; a new id is
    appended. Nothing is ever removed - an agent that omits a task from its
    report has said nothing about it, and reading silence as deletion would let
    one forgetful reply erase the roadmap.

    A status-only report (``{"id": "task_3", "status": "completed"}``) keeps
    the description it already had, which is what the contract asks agents to
    send once the list exists.
    """
    out = [dict(t) for t in existing]
    index = {t["id"]: i for i, t in enumerate(out)}
    for entry in reported if isinstance(reported, list) else []:
        task = _as_task(entry, fallback_role)
        if task is None:
            continue
        if task["id"] in index:
            current = out[index[task["id"]]]
            current["status"] = task["status"]
            if task["description"]:
                current["description"] = task["description"]
            if isinstance(entry, dict) and entry.get("assigned_to"):
                current["assigned_to"] = task["assigned_to"]
        else:
            index[task["id"]] = len(out)
            out.append(task)
    return out


# --------------------------------------------------------------------------
# The .theseus directory
# --------------------------------------------------------------------------


class Workspace:
    """The ``.theseus/`` directory inside a project root.

    Every read here is forgiving and every write is atomic. The files sit in a
    folder three coding agents have write access to, so a truncated or
    hand-mangled STATE.json has to degrade to "the engine keeps what it had"
    rather than take the run down.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @property
    def dir(self) -> Path:
        return self.root / THESEUS_DIR

    @property
    def state_path(self) -> Path:
        return self.dir / STATE_FILE

    @property
    def roadmap_path(self) -> Path:
        return self.dir / ROADMAP_FILE

    @property
    def critique_path(self) -> Path:
        return self.dir / CRITIQUE_FILE

    @property
    def spec_path(self) -> Path:
        return self.root / "SPEC.md"

    def ensure(self) -> None:
        """Create the directory and seed the two agent-owned files.

        Seeded rather than left absent so phase 1 has something to append to
        and the tracker has something to render, and so a `.theseus` directory
        in a repository is self-explanatory to whoever finds it in a diff.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.roadmap_path.exists():
            self.roadmap_path.write_text(
                "# Roadmap\n\n"
                "Maintained by the Theseus AI project engine and the agents it "
                "runs. The authoritative task list is in `STATE.json`; this is "
                "the readable one.\n\n"
                "_Waiting for the architect._\n",
                encoding="utf-8",
            )
        if not self.critique_path.exists():
            self.critique_path.write_text(
                "# Critique log\n\n"
                "Append-only. Every build failure, review finding and "
                "verification result, oldest first.\n",
                encoding="utf-8",
            )

    # -- reads (never raise) ----------------------------------------------

    def read_text(self, path: Path, limit: int = 0) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return prompts.clip(text, limit) if limit and len(text) > limit else text

    def read_state(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def roadmap(self, limit: int = 0) -> str:
        return self.read_text(self.roadmap_path, limit)

    def critique(self, limit: int = 0) -> str:
        return self.read_text(self.critique_path, limit)

    def spec(self, limit: int = 0) -> str:
        return self.read_text(self.spec_path, limit)

    # -- writes ------------------------------------------------------------

    def write_state(self, state: Dict[str, Any]) -> None:
        """Persist STATE.json atomically. Failure is reported, not fatal."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")
        tmp.replace(self.state_path)

    def append_critique(self, text: str) -> None:
        """Add one entry to the log. Append-only, always."""
        if not text.strip():
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.critique_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n{text.rstrip()}\n")
        except OSError:
            pass


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


@dataclass
class StepRecord:
    """One agent invocation, as the tracker shows it."""

    index: int
    phase: int
    role: str
    label: str  # which CLI ran it, e.g. "Claude"
    heading: str  # what it was asked to do, e.g. "Fix the build"
    state: str = "running"  # running | done | failed | skipped
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    ok: bool = False
    error: str = ""
    reported_status: str = ""
    files_modified: List[str] = field(default_factory=list)
    reasoning: str = ""
    command: List[str] = field(default_factory=list)
    handoff_from: str = ""  # the role whose agent gave up, on a continuation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "phase": self.phase,
            "phase_name": PHASE_NAMES.get(self.phase, str(self.phase)),
            "role": self.role,
            "role_label": ROLE_LABELS.get(self.role, self.role),
            "label": self.label,
            "heading": self.heading,
            "state": self.state,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration": round(self.ended_at - self.started_at, 2)
            if self.ended_at
            else 0.0,
            "ok": self.ok,
            "error": self.error,
            "reported_status": self.reported_status,
            "files_modified": self.files_modified,
            "reasoning": self.reasoning,
            "command": self.command,
            "handoff_from": self.handoff_from,
        }


@dataclass
class Project:
    """One autonomous build, from brief to hand-off."""

    id: str
    brief: str
    workspace: str
    status: str = PLANNING
    phase: int = PHASE_ARCHITECTURE
    active_role: str = "architect"
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    build_status: str = "untested"
    last_execution_error: str = ""
    continuation_token_needed: bool = False
    last_run_timestamp: str = ""
    created_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    steps_used: int = 0
    fix_attempts: int = 0
    expansions_remaining: int = 0
    error: str = ""
    # How it ended, when it ended well. Kept apart from `error` rather than
    # reusing it: a summary in the error field reads as a failure everywhere
    # that checks whether one is set, starting with the banner.
    note: str = ""
    paused: bool = False
    snapshot: Optional[gitutil.Snapshot] = None
    steps: List[StepRecord] = field(default_factory=list)
    # Frozen at start, exactly as a council run freezes its own: a project runs
    # for an hour and the operator will reasonably change settings during it.
    # Which CLI holds which chair must not change under a running phase.
    config: Dict[str, Any] = field(default_factory=dict, repr=False)

    # -- the state file ----------------------------------------------------

    def state_document(self) -> Dict[str, Any]:
        """``.theseus/STATE.json`` as the engine writes it.

        The first nine keys are the schema agents are told to expect; the rest
        is the engine's own bookkeeping, kept in the same file so that a run
        resumed after a restart - or picked up by a human reading the folder -
        needs to open exactly one thing.
        """
        return {
            "project_id": self.id,
            "status": self.status,
            "current_phase": self.phase,
            "active_agent": self.active_role,
            "last_run_timestamp": self.last_run_timestamp,
            "continuation_token_needed": self.continuation_token_needed,
            "tasks": copy.deepcopy(self.tasks),
            "last_execution_error": self.last_execution_error,
            "build_status": self.build_status,
            # -- engine bookkeeping --------------------------------------
            "brief": self.brief,
            "phase_name": PHASE_NAMES.get(self.phase, ""),
            "created_at": self.created_at,
            "steps_used": self.steps_used,
            "fix_attempts": self.fix_attempts,
            "expansions_remaining": self.expansions_remaining,
            "error": self.error,
            "note": self.note,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }

    def to_dict(self, roadmap: str = "", state_json: str = "") -> Dict[str, Any]:
        doc = self.state_document()
        return {
            **doc,
            "id": self.id,
            "workspace": self.workspace,
            "phase_names": PHASE_NAMES,
            "role_labels": ROLE_LABELS,
            "paused": self.paused,
            "ended_at": self.ended_at,
            "done": self.status in TERMINAL_STATUSES,
            "steps": [s.to_dict() for s in self.steps],
            "agents": {
                role: str(
                    ((self.config.get("providers") or {}).get(role) or {}).get("label")
                    or role
                )
                for role in ROLES
            },
            "roadmap": roadmap,
            "state_json": state_json,
        }

    # -- restoring from disk ----------------------------------------------

    @classmethod
    def from_state(cls, workspace: str, data: Dict[str, Any]) -> "Project":
        """Rebuild a project from a STATE.json written by an earlier session.

        Only the schema fields are trusted: a hand-edited or half-written file
        should resume the run, not decide how many steps it has left. Anything
        missing falls back to a value that makes the run finishable rather than
        one that makes it look finished.
        """
        tasks = [
            t
            for t in (_as_task(e) for e in (data.get("tasks") or []))
            if t is not None
        ]
        status = str(data.get("status") or PLANNING).upper()
        if status not in {PLANNING, IMPLEMENTING, TESTING, EXPANDING, COMPLETED, FAILED}:
            status = PLANNING
        try:
            phase = int(data.get("current_phase") or PHASE_ARCHITECTURE)
        except (TypeError, ValueError):
            phase = PHASE_ARCHITECTURE
        if phase not in PHASE_NAMES:
            phase = PHASE_ARCHITECTURE
        role = str(data.get("active_agent") or "architect")
        return cls(
            id=str(data.get("project_id") or uuid.uuid4().hex[:12]),
            brief=str(data.get("brief") or ""),
            workspace=workspace,
            status=status,
            phase=phase,
            active_role=role if role in ROLES else "architect",
            tasks=tasks,
            build_status=str(data.get("build_status") or "untested"),
            last_execution_error=str(data.get("last_execution_error") or ""),
            continuation_token_needed=bool(data.get("continuation_token_needed")),
            last_run_timestamp=str(data.get("last_run_timestamp") or ""),
            created_at=float(data.get("created_at") or time.time()),
            steps_used=int(data.get("steps_used") or 0),
            fix_attempts=int(data.get("fix_attempts") or 0),
            expansions_remaining=int(data.get("expansions_remaining") or 0),
            error=str(data.get("error") or ""),
            note=str(data.get("note") or ""),
        )


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------


class ProjectEngine:
    """Drives one project through the phase cycle on a worker thread.

    ``busy_check`` is called before a project starts and must raise if
    something else - a council run, a chat - is already working the tree. The
    engine does not reach into the pipeline to find that out: the two are
    peers, and the thing that owns both is what knows they conflict.
    """

    def __init__(
        self,
        store: cfg.ConfigStore,
        bus: EventBus,
        busy_check: Optional[Callable[[], None]] = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self._busy_check = busy_check
        self._lock = threading.RLock()
        self._project: Optional[Project] = None
        self._thread: Optional[threading.Thread] = None
        self._runner: Optional[ProviderRunner] = None
        self._stop = threading.Event()
        # Cleared while paused, set while running: the loop waits on it between
        # steps, which is the only place a pause can take effect without
        # killing an agent in the middle of writing a file.
        self._resume = threading.Event()
        self._resume.set()
        self._handoff_role = ""

    # -- introspection -----------------------------------------------------

    @property
    def project(self) -> Optional[Project]:
        with self._lock:
            return self._project

    def is_running(self) -> bool:
        with self._lock:
            return (
                self._project is not None
                and self._project.status not in TERMINAL_STATUSES
                and self._thread is not None
                and self._thread.is_alive()
            )

    def snapshot_state(self, workspace: str = "") -> Dict[str, Any]:
        """What the Projects tab renders.

        With no project in memory it looks on disk: a `.theseus/STATE.json` in
        the chosen folder is a project this app was running before it was
        closed, and offering to resume it beats presenting an empty form over
        the top of a half-built codebase.

        A finished project stays in memory, but it does not follow the operator
        to another folder: asked about a different one, this reports what is in
        *that* folder. A run still in flight is the exception and always wins -
        hiding three agents mid-build because the folder picker moved is worse
        than showing a project the status bar does not currently name.
        """
        folder = (workspace or "").strip()
        with self._lock:
            project = self._project
            running = self.is_running()

        if project is not None and not running and folder and folder != project.workspace:
            project = None

        if project is not None:
            ws = Workspace(project.workspace)
            return {
                "running": running,
                "resumable": not running and project.status not in TERMINAL_STATUSES,
                "project": project.to_dict(
                    roadmap=ws.roadmap(MAX_UI_TEXT),
                    state_json=json.dumps(project.state_document(), indent=2),
                ),
            }

        folder = folder or (self.store.get("workspace") or "").strip()
        if folder:
            ws = Workspace(folder)
            data = ws.read_state()
            if data.get("project_id"):
                found = Project.from_state(folder, data)
                found.config = self.store.all()
                return {
                    "running": False,
                    "resumable": found.status not in TERMINAL_STATUSES,
                    "found_on_disk": True,
                    "project": found.to_dict(
                        roadmap=ws.roadmap(MAX_UI_TEXT),
                        state_json=json.dumps(data, indent=2),
                    ),
                }
        return {"running": False, "resumable": False, "project": None}

    # -- control -----------------------------------------------------------

    def start(self, brief: str, workspace: str = "", resume: bool = False) -> Project:
        """Begin, or pick up, an autonomous build. Returns once the loop is up.

        Everything that can refuse the run is checked here, before a single
        agent is launched: the brief, the folder, the three CLIs. A project
        that discovers in phase 3 that its QA binary was never installed has
        already spent two rounds of quota finding out.
        """
        brief = (brief or "").strip()
        chosen = (workspace or "").strip()
        root = self._resolve_root(chosen)

        with self._lock:
            if self.is_running():
                raise ProjectBusy("A project is already running.")
        if self._busy_check is not None:
            self._busy_check()

        conf = self.store.all()
        settings = conf.get("project") or {}
        ws = Workspace(root)

        if resume:
            data = ws.read_state()
            if not data.get("project_id"):
                raise ValueError(
                    f"There is no project to resume in {root} - no "
                    f"{THESEUS_DIR}/{STATE_FILE} was found there."
                )
            project = Project.from_state(root, data)
            if project.status in TERMINAL_STATUSES:
                raise ValueError(
                    f"That project already finished ({project.status.title()}). "
                    f"Start a new one, or move {THESEUS_DIR}/ aside to build "
                    f"here again."
                )
            if brief:
                project.brief = brief
            if not project.brief:
                raise ValueError(
                    "That project's state file carries no brief, so there is "
                    "nothing to continue from. Type one to resume it."
                )
        else:
            if not brief:
                raise ValueError("Describe what you want built.")
            existing = ws.read_state()
            if existing.get("project_id") and str(
                existing.get("status")
            ) not in TERMINAL_STATUSES:
                raise ValueError(
                    f"{root} already has a project in progress "
                    f"({existing.get('status')}). Resume it, or move "
                    f"{THESEUS_DIR}/ aside to start over."
                )
            project = Project(
                id=uuid.uuid4().hex[:12], brief=brief, workspace=root
            )
            project.expansions_remaining = _int_setting(settings, "expansion_rounds", 1)

        project.config = conf
        project.paused = False
        project.error = ""

        missing = self._unavailable_roles(conf)
        if missing:
            raise ValueError(self._missing_message(missing))

        ws.ensure()
        self._write_state(project)

        with self._lock:
            self._project = project
            self._stop.clear()
            self._resume.set()
            self._handoff_role = ""

        self.store.remember_workspace(root)
        self.bus.publish("project_started", project=self._payload(project))
        self._log(
            "info",
            f"Project {project.id} started in {root}. Every step runs with its "
            f"agent's auto-approve flags - files will change without asking.",
        )

        thread = threading.Thread(
            target=self._execute, args=(project,), name=f"project-{project.id}",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
        thread.start()
        return project

    def pause(self) -> None:
        """Halt after the running agent finishes its step.

        Deliberately not mid-step. A CLI killed between two `write` calls
        leaves a half-written file that nothing in the run knows is
        half-written, and the next agent builds on it. Waiting for the step
        costs a few minutes and leaves the tree consistent - and **Stop** is
        there for the operator who wants it to end now.
        """
        with self._lock:
            project = self._project
            if project is None or project.status in TERMINAL_STATUSES:
                raise ValueError("No project is running.")
            project.paused = True
        self._resume.clear()
        self._log("warn", "Pausing after the current agent finishes its step.")
        self._publish_state()

    def resume(self) -> None:
        """Release a pause. The loop re-reads its state and carries on."""
        with self._lock:
            project = self._project
            if project is None:
                raise ValueError("No project to resume.")
            project.paused = False
        self._resume.set()
        self._log("info", "Resumed.")
        self._publish_state()

    def handoff(self, role: str) -> None:
        """Force the next step onto a different agent.

        For the case the phase cycle cannot detect: an agent that is answering,
        exiting cleanly, and going in circles. The engine cannot tell that from
        progress, so the operator can.
        """
        role = (role or "").strip().lower()
        if role not in ROLES:
            raise ValueError(f"Not a project role: {role!r}")
        with self._lock:
            if self._project is None or self._project.status in TERMINAL_STATUSES:
                raise ValueError("No project is running.")
            self._handoff_role = role
        self._log(
            "warn",
            f"Hand-off requested: the next step runs on the "
            f"{ROLE_LABELS[role]} agent.",
        )
        self._publish_state()

    def stop(self) -> None:
        """End the run now, killing whatever is executing."""
        with self._lock:
            self._stop.set()
            runner = self._runner
            project = self._project
        self._resume.set()  # a paused loop has to wake up to notice it is over
        if runner is not None:
            runner.cancel()
        if project is not None:
            self._log("warn", "Stop requested.")

    def wait_for_worker(self, timeout: float = 60.0) -> bool:
        """Block until the worker thread has wound down. False if it has not."""
        with self._lock:
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # -- preflight ---------------------------------------------------------

    def _resolve_root(self, folder: str) -> str:
        """The absolute directory a project builds in.

        Unlike a council run this does *not* collapse to the repository root. A
        project is asked to lay out its own tree, and silently redirecting it
        from `~/code/repo/subdir` to `~/code/repo` would have it build over a
        codebase the operator never pointed at.
        """
        folder = (folder or "").strip()
        if not folder:
            return str(cfg.workspace_dir())
        path = Path(folder).expanduser()
        if not path.is_dir():
            raise ValueError(
                f"{folder!r} is not a folder that exists. Pick one, or clear "
                f"it to build in the scratch workspace."
            )
        return str(path.resolve())

    def _unavailable_roles(self, conf: Dict[str, Any]) -> List[str]:
        """Which of the three chairs is pointed at a binary that is not there."""
        providers = conf.get("providers") or {}
        missing = []
        for role in ROLES:
            provider = providers.get(role) or {}
            if not probe(provider).get("available"):
                missing.append(role)
        return missing

    def _missing_message(self, missing: List[str]) -> str:
        providers = self.store.get("providers", {}) or {}
        lines = []
        for role in missing:
            command = (providers.get(role) or {}).get("command") or ["?"]
            lines.append(f"{ROLE_LABELS[role]} is set to `{command[0]}`")
        return (
            f"{'; '.join(lines)}, which is not installed or not on PATH. "
            f"Install it, or assign that chair to another CLI in the agent "
            f"matrix. A project runs unattended, so it will not start with a "
            f"seat it cannot fill."
        )

    # -- the loop ----------------------------------------------------------

    def _execute(self, project: Project) -> None:
        """Worker-thread body: run phases until the project settles."""
        try:
            self._take_snapshot(project)
            max_steps = _int_setting(
                project.config.get("project") or {}, "max_steps", 40
            )

            while True:
                # Terminal first, and before the stop check: a phase that has
                # just finished the project and a stop that arrives in the same
                # moment would otherwise rewrite a COMPLETED run as "stopped by
                # the operator", which is both wrong and unrecoverable.
                if project.status in TERMINAL_STATUSES:
                    return

                if self._stop.is_set():
                    self._finish(project, FAILED, "Stopped by the operator.")
                    return

                # The pause gate. Between steps only - see `pause`.
                if not self._resume.is_set():
                    self._publish_state()
                    self._resume.wait()
                    if self._stop.is_set():
                        self._finish(project, FAILED, "Stopped by the operator.")
                        return

                if project.steps_used >= max_steps:
                    self._finish(
                        project,
                        FAILED,
                        f"Reached the step limit ({max_steps} agent turns) "
                        f"without finishing. Raise it in Settings, or resume "
                        f"the project - everything built so far is on disk and "
                        f"the roadmap says where it got to.",
                    )
                    return

                handler = {
                    PHASE_ARCHITECTURE: self._phase_architecture,
                    PHASE_IMPLEMENTATION: self._phase_implementation,
                    PHASE_QA: self._phase_qa,
                    PHASE_EXPANSION: self._phase_expansion,
                    PHASE_FINALISATION: self._phase_finalisation,
                }[project.phase]
                handler(project)
        except Exception as exc:  # a crash here must not wedge the engine
            self._finish(project, FAILED, f"{type(exc).__name__}: {exc}")

    # -- phases ------------------------------------------------------------

    def _phase_architecture(self, project: Project) -> None:
        self._enter(project, PHASE_ARCHITECTURE, PLANNING, "architect")
        step, report = self._run_role(
            project, "architect", "Design the system",
            lambda ctx: prompts.build_architecture_prompt(project.brief, ctx),
        )
        if step is None:
            return

        project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")
        spec_written = Workspace(project.workspace).spec_path.exists()

        # Judged on what is on disk, not on the exit status. An architect that
        # wrote the spec and the roadmap and *then* fell over has done phase 1;
        # failing the project on the exit code would throw that away and make
        # the operator start again from a folder that already has the answer.
        if not project.tasks and not spec_written:
            self._fail_step(
                project, step,
                "The architect produced neither a task list nor a SPEC.md, so "
                "there is nothing for the developer to build.",
            )
            return

        if not step.ok:
            self._log(
                "warn",
                f"The architect's step failed ({step.error}), but it left a "
                f"plan behind. Continuing from what is on disk.",
            )
        if not project.tasks:
            self._log(
                "warn",
                "The architect returned no task list. The developer will work "
                "from SPEC.md and the roadmap instead.",
            )
        if not spec_written:
            self._log(
                "warn",
                "No SPEC.md was written. The developer and QA will work from "
                "the roadmap and the brief alone, which is thinner than "
                "intended - and QA has no build command to run.",
            )
        self._advance(project, PHASE_IMPLEMENTATION)

    def _phase_implementation(self, project: Project) -> None:
        fixing = project.build_status == "failing"
        self._enter(project, PHASE_IMPLEMENTATION, IMPLEMENTING, "coder")
        pending = [t for t in project.tasks if t["status"] != "completed"]
        step, report = self._run_role(
            project,
            "coder",
            "Fix the build" if fixing else "Implement the pending tasks",
            lambda ctx: prompts.build_implementation_prompt(
                project.brief, ctx, _render_tasks(pending), fixing=fixing
            ),
        )
        if step is None:
            return
        project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")
        if not step.ok:
            # Not fatal on its own: QA runs next either way, and what is on
            # disk is what matters. A developer that keeps failing is caught by
            # the fix-attempt budget instead.
            self._log(
                "warn",
                f"The developer's step failed ({step.error}). Handing to QA "
                f"anyway - what is on disk is what gets verified.",
            )
        self._advance(project, PHASE_QA)

    def _phase_qa(self, project: Project) -> None:
        self._enter(project, PHASE_QA, TESTING, "qa")
        step, report = self._run_role(
            project, "qa", "Build and verify",
            lambda ctx: prompts.build_qa_prompt(project.brief, ctx),
        )
        if step is None:
            return

        build = str(report.get("build_status") or "").strip().lower()
        error = str(report.get("last_execution_error") or "").strip()
        if build not in ("passing", "failing"):
            # No usable verdict. Treated as failing, never as passing: the one
            # thing that must not happen is a silent QA turning into a green
            # build the loop then builds on top of.
            build = "failing"
            error = error or (
                step.error
                or "QA did not report a build status, so the build is "
                "unverified. Run the build and the tests, and report "
                "`build_status` explicitly."
            )
            self._log("warn", "QA reported no build status. Treating it as failing.")

        project.build_status = build
        project.last_execution_error = error if build == "failing" else ""
        Workspace(project.workspace).append_critique(
            f"## {_stamp()} - QA, phase {project.phase}, build {build}\n\n"
            f"{step.reasoning or '(no summary reported)'}\n"
            + (f"\n```\n{prompts.clip(error, 4000)}\n```\n" if error else "")
        )

        if build == "failing":
            budget = _int_setting(
                project.config.get("project") or {}, "max_fix_attempts", 3
            )
            project.fix_attempts += 1
            if project.fix_attempts > budget:
                self._finish(
                    project,
                    FAILED,
                    f"The build still fails after {budget} fix attempts. The "
                    f"last failure is in {THESEUS_DIR}/{CRITIQUE_FILE}; the "
                    f"work so far is on disk and the project can be resumed "
                    f"once you have unstuck it.",
                )
                return
            self._log(
                "warn",
                f"Build failing (attempt {project.fix_attempts} of {budget}). "
                f"Back to the developer with the trace.",
            )
            self._advance(project, PHASE_IMPLEMENTATION)
            return

        project.fix_attempts = 0
        self._log("info", "Build passing.")
        if project.expansions_remaining > 0:
            self._advance(project, PHASE_EXPANSION)
        else:
            self._advance(project, PHASE_FINALISATION)

    def _phase_expansion(self, project: Project) -> None:
        self._enter(project, PHASE_EXPANSION, EXPANDING, "architect")
        project.expansions_remaining = max(0, project.expansions_remaining - 1)
        before = {t["id"] for t in project.tasks}
        step, report = self._run_role(
            project, "architect", "Propose the next version",
            lambda ctx: prompts.build_expansion_prompt(project.brief, ctx),
        )
        if step is None:
            return

        project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")
        added = [t for t in project.tasks if t["id"] not in before]
        if not added:
            # A real answer, and the architect is told so: nothing worth adding
            # means the project is done, not that the phase failed.
            self._log(
                "info",
                "The architect proposed nothing further. Going straight to "
                "finalisation.",
            )
            self._advance(project, PHASE_FINALISATION)
            return

        self._log(
            "info",
            f"Expansion: {len(added)} new task(s) - "
            + "; ".join(t["description"][:70] or t["id"] for t in added),
        )
        Workspace(project.workspace).append_critique(
            f"## {_stamp()} - Expansion round\n\n"
            + "\n".join(f"- {t['id']}: {t['description']}" for t in added)
            + "\n"
        )
        self._advance(project, PHASE_IMPLEMENTATION)

    def _phase_finalisation(self, project: Project) -> None:
        """Verify the whole thing, then write the README, then stop."""
        self._enter(project, PHASE_FINALISATION, TESTING, "qa")
        step, report = self._run_role(
            project, "qa", "Final integrity check",
            lambda ctx: prompts.build_integrity_prompt(project.brief, ctx),
        )
        if step is None:
            return

        build = str(report.get("build_status") or "").strip().lower()
        error = str(report.get("last_execution_error") or "").strip()
        Workspace(project.workspace).append_critique(
            f"## {_stamp()} - Final integrity check, build {build or 'unreported'}\n\n"
            f"{step.reasoning or '(no summary reported)'}\n"
            + (f"\n```\n{prompts.clip(error, 4000)}\n```\n" if error else "")
        )

        if build == "failing":
            budget = _int_setting(
                project.config.get("project") or {}, "max_fix_attempts", 3
            )
            project.build_status = "failing"
            project.last_execution_error = error
            project.fix_attempts += 1
            if project.fix_attempts <= budget:
                self._log(
                    "warn",
                    "The final check failed. Back to the developer rather than "
                    "handing over something that does not build.",
                )
                self._advance(project, PHASE_IMPLEMENTATION)
                return
            # Out of budget, but the code is real and the log says what is
            # wrong. Finish the hand-off and say plainly that it is broken -
            # that is more use than stopping with no README at all.
            self._log(
                "warn",
                "The final check still fails and the fix budget is spent. "
                "Writing the hand-off anyway, with the failure recorded.",
            )
        else:
            project.build_status = "passing" if build == "passing" else project.build_status
            project.last_execution_error = ""

        # -- the write-up ---------------------------------------------------
        self._enter(project, PHASE_FINALISATION, IMPLEMENTING, "architect")
        step, _ = self._run_role(
            project, "architect", "Write the hand-off",
            lambda ctx: prompts.build_handoff_prompt(project.brief, ctx),
        )
        if step is None:
            return

        done = sum(1 for t in project.tasks if t["status"] == "completed")
        # Completed, and honest about what it is: the build is what the project
        # is, and the write-up is the last thing on top of it. Saying "finished"
        # while quietly omitting that the README step failed, or that the final
        # check never went green, is exactly the reassurance that stops the
        # operator opening CRITIQUE.log.
        caveats = []
        if not step.ok:
            caveats.append(f"the README step failed ({step.error})")
        if project.build_status != "passing":
            caveats.append(f"the build is {project.build_status}")
        self._finish(
            project,
            COMPLETED,
            "",
            note=(
                f"Finished: {done} of {len(project.tasks)} task(s) completed, "
                f"build {project.build_status}, in {project.steps_used} agent "
                f"turns."
                + (f" Note: {'; '.join(caveats)}." if caveats else "")
            ),
        )

    # -- running one agent -------------------------------------------------

    def _run_role(
        self,
        project: Project,
        role: str,
        heading: str,
        build_prompt: Callable[[str], str],
    ) -> tuple[Optional[StepRecord], Dict[str, Any]]:
        """Run one step, handing off to another agent if this one is exhausted.

        Returns ``(None, {})`` when the run was stopped mid-step, which is the
        caller's signal to return without advancing anything.

        The hand-off is the whole reason a project survives a context limit.
        The prompt is rebuilt for the replacement rather than replayed, because
        every project prompt is built from files on disk - so the second agent
        starts from the same state, not from the first one's transcript.
        """
        forced = ""
        with self._lock:
            if self._handoff_role:
                forced, self._handoff_role = self._handoff_role, ""

        attempted: List[str] = []
        runner_role = forced or role
        handoff_from = role if forced and forced != role else ""

        while True:
            attempted.append(runner_role)
            provider = self._provider(project, runner_role)
            step = StepRecord(
                index=project.steps_used + 1,
                phase=project.phase,
                role=runner_role,
                label=str(provider.get("label") or runner_role),
                heading=heading,
                handoff_from=handoff_from,
            )
            project.steps_used += 1
            project.steps.append(step)
            del project.steps[:-MAX_STEP_RECORDS]

            prompt = build_prompt(self._context(project, runner_role))
            self.bus.publish(
                "project_step",
                project=self._payload(project),
                step=step.to_dict(),
            )

            result = self._invoke(project, runner_role, provider, prompt)
            report = parse_report(result.stdout)

            step.ended_at = time.time()
            step.ok = result.ok
            step.error = result.error
            step.command = result.command
            step.reported_status = str(report.get("status") or "")
            step.reasoning = str(report.get("reasoning") or "").strip()[:2000]
            step.files_modified = [
                str(p) for p in (report.get("files_modified") or [])
                if isinstance(p, (str, int))
            ][:200]
            step.state = "done" if result.ok else "failed"
            project.last_run_timestamp = _stamp()

            if result.cancelled or self._stop.is_set():
                step.state = "skipped"
                self._write_state(project)
                self.bus.publish(
                    "project_step_done",
                    project=self._payload(project),
                    step=step.to_dict(),
                )
                self._finish(project, FAILED, "Stopped by the operator.")
                return None, {}

            self.bus.publish(
                "project_step_done",
                project=self._payload(project),
                step=step.to_dict(),
            )

            # Did this agent run out of room rather than run out of ideas?
            spare = [r for r in ROLES if r not in attempted]
            if result.ok or not self._looks_exhausted(result) or not spare:
                project.continuation_token_needed = False
                self._write_state(project)
                return step, report

            project.continuation_token_needed = True
            self._write_state(project)
            handoff_from = runner_role
            runner_role = spare[0]
            self._log(
                "warn",
                f"The {ROLE_LABELS[handoff_from]} agent ran out of room "
                f"({result.error or 'context or quota limit'}). Handing this "
                f"step to the {ROLE_LABELS[runner_role]} agent with the state "
                f"file as its context.",
            )

    def _invoke(
        self, project: Project, role: str, provider: Dict[str, Any], prompt: str
    ) -> ProviderResult:
        """Launch one CLI. Always with write permission - see the module docs."""
        runner = ProviderRunner(provider, self._output_cb(role))
        with self._lock:
            self._runner = runner
            if self._stop.is_set():
                runner.cancel()
        try:
            return runner.run(
                prompt, cwd=project.workspace, auto_approve=True, read_only=False
            )
        finally:
            with self._lock:
                self._runner = None

    def _output_cb(self, role: str) -> Callable[[str, str], None]:
        def cb(stream: str, line: str) -> None:
            self.bus.publish("project_output", role=role, stream=stream, line=line)

        return cb

    def _looks_exhausted(self, result: ProviderResult) -> bool:
        """Whether a failure reads as "no room left" rather than "no".

        A timeout counts: the CLIs do not distinguish a model grinding through
        an oversized context from one that is merely slow, and re-running the
        same oversized prompt on the same agent would only time out again.
        """
        if result.timed_out:
            return True
        haystack = f"{result.error}\n{result.stderr}\n{result.stdout[-4000:]}"
        return bool(EXHAUSTION_RE.search(haystack))

    def _provider(self, project: Project, role: str) -> Dict[str, Any]:
        """The frozen provider config for one chair."""
        provider = dict((project.config.get("providers") or {}).get(role) or {})
        provider.setdefault("id", role)
        return provider

    def _context(self, project: Project, role: str) -> str:
        """Build the shared preamble for one turn, from the files on disk."""
        ws = Workspace(project.workspace)
        diff = ""
        try:
            if gitutil.repo_root(project.workspace):
                diff = gitutil.working_diff(project.workspace)
        except (gitutil.GitError, OSError):
            diff = ""
        return prompts.project_context_block(
            project_id=project.id,
            root=project.workspace,
            state_json=json.dumps(project.state_document(), indent=2),
            roadmap=ws.roadmap(),
            spec=ws.spec(),
            last_error=project.last_execution_error,
            diff=diff,
            house_rules=str(project.config.get("house_rules") or ""),
        )

    # -- state transitions -------------------------------------------------

    def _enter(self, project: Project, phase: int, status: str, role: str) -> None:
        project.phase = phase
        project.status = status
        project.active_role = role
        project.last_run_timestamp = _stamp()
        self._write_state(project)
        self._publish_state()

    def _advance(self, project: Project, phase: int) -> None:
        project.phase = phase
        project.status = {
            PHASE_ARCHITECTURE: PLANNING,
            PHASE_IMPLEMENTATION: IMPLEMENTING,
            PHASE_QA: TESTING,
            PHASE_EXPANSION: EXPANDING,
            PHASE_FINALISATION: TESTING,
        }[phase]
        self._write_state(project)
        self._publish_state()

    def _fail_step(self, project: Project, step: StepRecord, why: str) -> None:
        self._finish(project, FAILED, f"{why} {step.error}".strip())

    def _finish(
        self, project: Project, status: str, error: str, note: str = ""
    ) -> None:
        project.status = status
        project.error = error
        project.note = note
        project.ended_at = time.time()
        project.last_run_timestamp = _stamp()
        self._write_state(project)
        Workspace(project.workspace).append_critique(
            f"## {_stamp()} - Project {status.lower()}\n\n{note or error or '(no note)'}\n"
        )
        self._log("error" if status == FAILED else "info", note or error or status)
        self._publish_state()

    def _take_snapshot(self, project: Project) -> None:
        """Anchor the tree before the first agent writes to it, where we can."""
        if not project.config.get("safety_snapshot", True):
            return
        try:
            project.snapshot = gitutil.take_snapshot(project.workspace)
        except (gitutil.GitError, OSError) as exc:
            project.snapshot = None
            self._log("warn", f"Could not take a safety snapshot ({exc}).")
            return
        if project.snapshot:
            self._log(
                "info", f"Safety snapshot taken at {project.snapshot.head[:8]}."
            )
        else:
            why = (
                "this repository has no commits yet"
                if gitutil.repo_root(project.workspace)
                else "this folder is not a git repository"
            )
            self._log(
                "warn",
                f"No safety snapshot: {why}. Everything the agents write here "
                f"is unprotected - there is no state to restore.",
            )

    # -- plumbing ----------------------------------------------------------

    def _write_state(self, project: Project) -> None:
        try:
            Workspace(project.workspace).write_state(project.state_document())
        except OSError as exc:
            # Losing the state file costs resumability, not the run in flight.
            self._log("warn", f"Could not write {THESEUS_DIR}/{STATE_FILE}: {exc}")

    def _payload(self, project: Project) -> Dict[str, Any]:
        ws = Workspace(project.workspace)
        return project.to_dict(
            roadmap=ws.roadmap(MAX_UI_TEXT),
            state_json=json.dumps(project.state_document(), indent=2),
        )

    def _publish_state(self) -> None:
        with self._lock:
            project = self._project
        if project is not None:
            self.bus.publish("project_state", project=self._payload(project))

    def _log(self, level: str, message: str) -> None:
        self.bus.publish("project_log", level=level, message=message)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _stamp() -> str:
    """An ISO-8601 timestamp, which is what the schema asks for."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _int_setting(settings: Dict[str, Any], key: str, fallback: int) -> int:
    """One positive integer out of the project settings, however mangled."""
    try:
        value = int(settings.get(key, fallback))
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _render_tasks(tasks: List[Dict[str, Any]]) -> str:
    """The task list as the developer reads it."""
    if not tasks:
        return ""
    return "\n".join(
        f"- `{t['id']}` [{t['status']}] {t['description'] or '(no description)'}"
        for t in tasks
    )
