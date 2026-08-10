"""The Projects engine: three agents around one board, nobody watching.

Council is one task, two stages and a human at the gate. Projects is the other
end of that: a goal goes in, and an architect, a developer and a QA agent work
a shared Kanban board against the same directory until the thing builds,
passes its own tests, and nobody has anything left to add.

Not a pipeline
--------------

There is no step 1, step 2, step 3. Every turn the engine reads the board and
asks one question - *what does this project need next?* - and the answer picks
the agent::

    build FAILING ......... developer fixes it
    build UNKNOWN ......... QA builds and tests it
    tasks in review ....... architect reviews the diff
    tasks in backlog ...... developer claims the top one
    board empty, green .... architect proposes what is missing
    nothing left .......... done

That ordering is the whole design. A failing build outranks new features; an
unverified build outranks a code review, because reviewing code that does not
compile is wasted; and *only* a QA turn can move the build to PASSING. The
engine sets it back to UNKNOWN the moment anyone writes code, so a green board
always means somebody ran the tests after the last edit.

Who owns what
-------------

The engine owns ``.theseus/BOARD.json`` and is the only writer. Agents read it
and report *deltas* - this task moved, the build failed, here are three ideas -
in a fenced JSON block the engine parses and applies. Agents own the prose:
``.theseus/CRITIQUE.log`` (append-only findings) and ``.theseus/SPEC.md``
(design notes), plus the project's own source. That split is deliberate: prose
files tolerate three writers, a state machine does not.

Everything the engine creates lives in ``.theseus/`` and that directory is
added to ``.gitignore`` on the way in, so pointing this at a real repository
does not litter its diff.

Working on somebody else's code
-------------------------------

Most projects are not empty folders. The first turn of every run is a
*read-only* audit: the QA agent gets no write grant at all and is asked what
is already here - the layout, the configs, the build tooling. The engine reads
the tooling too (``go.mod``, ``package.json``, ``Cargo.toml``, ...) and puts
the real commands on the board, so QA runs ``go test ./...`` because that is
what this project uses, not because an agent invented a script. Everything
after that turn is asked for targeted edits against named files, never
whole-file rewrites.

Surviving a context limit
-------------------------

No turn depends on the previous turn's conversation. Every prompt is rebuilt
from the board and the diff on disk, never from a transcript, so an agent that
dies mid-turn costs one turn, not the run. When a turn fails in a way that
reads as exhaustion - a token or quota limit, a timeout - the engine marks
``continuation_needed`` and re-runs that same turn on a *different* agent,
which is why the three chairs are three independent providers.

Permission to write
-------------------

A project writes. Every turn after the audit is invoked with its agent's
auto-approve flags, and there is no setting that changes that. Starting a
project *is* the grant - which is why it takes a deliberate press of a button,
names the folder it will write to, and takes a git snapshot first where there
is one to take.

Only one project runs at a time, and never alongside a council or chat run:
they would be two agents editing the same tree with no idea the other existed.
"""

from __future__ import annotations

import copy
import hashlib
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

# -- The board -------------------------------------------------------------

# The four columns, in the order they are read and rendered. These strings are
# the ones agents see in BOARD.json and report back, so they are lower-case
# and boring on purpose.
COL_BACKLOG = "backlog"
COL_IN_PROGRESS = "in_progress"
COL_IN_REVIEW = "in_review"
COL_DONE = "done"

COLUMNS = (COL_BACKLOG, COL_IN_PROGRESS, COL_IN_REVIEW, COL_DONE)
COLUMN_LABELS = {
    COL_BACKLOG: "Backlog",
    COL_IN_PROGRESS: "In progress",
    COL_IN_REVIEW: "Review",
    COL_DONE: "Done",
}

# Build health. Only a QA turn may report PASSING; the engine writes UNKNOWN
# itself whenever code changes, so "green" cannot survive an edit.
HEALTH_PASSING = "PASSING"
HEALTH_FAILING = "FAILING"
HEALTH_UNKNOWN = "UNKNOWN"
HEALTH_VALUES = (HEALTH_PASSING, HEALTH_FAILING, HEALTH_UNKNOWN)

# Project status. Derived from whichever decision the loop just took - it is
# what the banner says, never what the engine dispatches on.
AUDITING = "AUDITING"
PLANNING = "PLANNING"
IMPLEMENTING = "IMPLEMENTING"
TESTING = "TESTING"
REVIEWING = "REVIEWING"
ACCEPTING = "ACCEPTING"
INNOVATING = "INNOVATING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

TERMINAL_STATUSES = {COMPLETED, FAILED}

# The three chairs, and the provider each is configured under. The ids are the
# provider keys in config, so every picker the council already has - agent,
# model, effort, availability, quota - works on these unchanged.
ROLES = ("architect", "coder", "qa")
ROLE_LABELS = {
    "architect": "Architect",
    "coder": "Developer",
    "qa": "QA",
}

# Task kinds. A bug is a task that jumps the queue: QA raises them off a build
# failure, and the developer claims them before anything in the backlog.
KIND_TASK = "task"
KIND_BUG = "bug"
KINDS = (KIND_TASK, KIND_BUG)

# Where a task came from. Only used to tell the operator which items the
# council invented on its own once the brief was satisfied.
ORIGIN_GOAL = "goal"
ORIGIN_INNOVATION = "innovation"
ORIGIN_BUG = "bug"

# Files the engine manages, all relative to the project root.
THESEUS_DIR = ".theseus"
BOARD_FILE = "BOARD.json"
CRITIQUE_FILE = "CRITIQUE.log"
SPEC_FILE = "SPEC.md"

# How much of the board and the log the browser is sent. A project that has
# run for an hour has a critique log far larger than anything worth pushing
# down an SSE stream on every turn.
MAX_UI_TEXT = 24_000
# Turn records held for the UI. The whole history is in CRITIQUE.log.
MAX_STEP_RECORDS = 200
# How much of a build failure is kept on the board and replayed to the agent
# that has to fix it. Enough for a stack trace, not a whole test run.
MAX_BUILD_LOG = 6_000

# A reply that ends in a fenced JSON block, which is the contract in
# ``prompts.REPORT_CONTRACT``. Matched from the *last* fence backwards, since
# an agent explaining the contract back to us puts an example block earlier.
FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.DOTALL)

# What exhaustion looks like from outside the process. None of the three CLIs
# has a distinct exit code for it, so this reads their prose - which makes it a
# heuristic, and it is treated as one: a false positive costs one hand-off to
# another agent, and a false negative just looks like an ordinary failed turn.
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

# What an agent says about its own turn, when what it says is "no". The
# contract in ``prompts.REPORT_CONTRACT`` documents three values - `ok`,
# `blocked`, `failed` - and says in as many words that a false `ok` "sends the
# next agent off to review something that was never written". The engine reads
# the honest ones: a CLI that exits zero having got nowhere has not done the
# work, whatever its exit status says.
UNSUCCESSFUL_STATUSES = frozenset(
    {"blocked", "failed", "error", "aborted", "cancelled", "canceled"}
)

# How many consecutive turns may leave the board completely unchanged before
# the run is called stalled. Three agents that all decline to move a card are
# not making progress, and the step limit is too blunt an instrument to notice
# that an hour early.
STALL_LIMIT = 3


class ProjectBusy(RuntimeError):
    """A project is already running."""


# --------------------------------------------------------------------------
# Native build tooling
# --------------------------------------------------------------------------

# Marker file -> (stack name, the commands that stack actually uses). Ordered:
# the first marker found names the stack, and every marker found contributes
# its commands, because a Go service with a Makefile has both.
#
# The point of this table is that QA is *told* the project's own commands
# rather than inventing a build script. Adopting `go test ./...` because
# go.mod is present is the difference between verifying this project and
# verifying a scaffold an agent wrote to make itself pass.
TOOLING_MARKERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("go.mod", "Go", ("gofmt -l .", "go vet ./...", "go build ./...", "go test ./...")),
    ("Cargo.toml", "Rust", ("cargo fmt --check", "cargo clippy", "cargo test")),
    ("pyproject.toml", "Python", ("python3 -m pytest",)),
    ("pytest.ini", "Python", ("python3 -m pytest",)),
    ("tox.ini", "Python", ("python3 -m pytest",)),
    ("setup.py", "Python", ("python3 -m pytest",)),
    ("Gemfile", "Ruby", ("bundle exec rake test",)),
    ("pom.xml", "Java (Maven)", ("mvn -q verify",)),
    ("build.gradle", "Java (Gradle)", ("./gradlew build",)),
    ("CMakeLists.txt", "CMake", ("cmake -B build", "cmake --build build")),
    ("Makefile", "Make", ("make",)),
)

# package.json scripts worth running, in the order they should run.
NPM_SCRIPTS = ("lint", "build", "test")


def detect_tooling(root: str | Path) -> Dict[str, Any]:
    """Read the project's own build tooling off disk. Never raises, never writes.

    Returns ``{"stack": [...], "commands": [...], "markers": [...]}``. An empty
    result is normal and honest - an empty folder has no tooling yet, and the
    agents are told to say so rather than to guess.
    """
    path = Path(root)
    stack: List[str] = []
    commands: List[str] = []
    markers: List[str] = []

    for marker, name, cmds in TOOLING_MARKERS:
        if not (path / marker).is_file():
            continue
        markers.append(marker)
        if name not in stack:
            stack.append(name)
        for cmd in cmds:
            if cmd not in commands:
                commands.append(cmd)

    # package.json is handled separately: its commands are whatever the
    # project declared, not a guess. `npm test` on a package with no test
    # script fails in a way that looks like a broken build.
    pkg = path / "package.json"
    if pkg.is_file():
        markers.append("package.json")
        if "Node" not in stack:
            stack.append("Node")
        scripts: Dict[str, Any] = {}
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict) and isinstance(data.get("scripts"), dict):
                scripts = data["scripts"]
        except (OSError, json.JSONDecodeError, ValueError):
            scripts = {}
        for name in NPM_SCRIPTS:
            if scripts.get(name):
                cmd = f"npm run {name}" if name != "test" else "npm test"
                if cmd not in commands:
                    commands.append(cmd)

    return {"stack": stack, "commands": commands, "markers": markers}


def ensure_gitignored(root: str | Path) -> str:
    """Add ``.theseus/`` to the project's .gitignore. Returns what it did.

    Only inside a git repository, and only ever by appending: an existing
    .gitignore is somebody's file, and a project engine that rewrites it has
    already broken the promise that it edits surgically.
    """
    path = Path(root)
    try:
        if not gitutil.repo_root(path):
            return ""
    except (gitutil.GitError, OSError):
        return ""

    ignore = path / ".gitignore"
    try:
        current = ignore.read_text(encoding="utf-8", errors="replace") if ignore.is_file() else ""
    except OSError:
        return ""

    for line in current.splitlines():
        if line.strip().strip("/") == THESEUS_DIR:
            return ""

    block = (
        f"\n# Working state for the Theseus AI project engine (not part of the app):\n"
        f"/{THESEUS_DIR}/\n"
    )
    try:
        with ignore.open("a", encoding="utf-8") as fh:
            if current and not current.endswith("\n"):
                fh.write("\n")
            fh.write(block)
    except OSError:
        return ""
    return f"Added /{THESEUS_DIR}/ to .gitignore."


# --------------------------------------------------------------------------
# Reading what an agent reported
# --------------------------------------------------------------------------


def parse_report(text: str) -> Dict[str, Any]:
    """Pull the JSON block out of an agent's reply. Never raises.

    Returns ``{}`` when there is nothing parseable, which the caller treats as
    "it did the work but did not tell me about it" rather than as a failure.
    That distinction matters: a turn failed on a missing report would burn a
    retry on an agent whose only mistake was formatting, and whatever it wrote
    is on disk either way for QA to find.
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
        elif tail.startswith("{"):
            # A reply that is nothing but the object, which is what an agent
            # told to end on a JSON block and nothing else sometimes sends.
            candidates.append(tail)

    keys = {"status", "files_modified", "reasoning", "tasks", "build", "reviews", "ideas"}
    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and keys & set(data):
            return data
    return {}


def _as_task(entry: Any, fallback_role: str = "coder") -> Optional[Dict[str, Any]]:
    """Normalise one reported card, or None if it is not one."""
    if not isinstance(entry, dict):
        return None
    task_id = str(entry.get("id") or "").strip()
    if not task_id:
        return None

    column = str(entry.get("column") or COL_BACKLOG).strip().lower()
    if column not in COLUMNS:
        column = COL_BACKLOG
    kind = str(entry.get("kind") or KIND_TASK).strip().lower()
    if kind not in KINDS:
        kind = KIND_TASK
    assigned = str(entry.get("assigned_to") or "").strip().lower()
    origin = str(entry.get("origin") or "").strip().lower()

    return {
        "id": task_id,
        "title": str(entry.get("title") or entry.get("description") or "").strip(),
        "detail": str(entry.get("detail") or "").strip(),
        "column": column,
        "kind": kind,
        "assigned_to": assigned if assigned in ROLES else fallback_role,
        "origin": origin if origin in (ORIGIN_GOAL, ORIGIN_INNOVATION, ORIGIN_BUG)
        else (ORIGIN_BUG if kind == KIND_BUG else ORIGIN_GOAL),
        "note": str(entry.get("note") or "").strip(),
    }


def merge_tasks(
    existing: List[Dict[str, Any]], reported: Any, fallback_role: str = "coder"
) -> List[Dict[str, Any]]:
    """Apply an agent's reported cards onto the board the engine is holding.

    A reported id already on the board moves that card; a new id is appended.
    Nothing is ever removed - an agent that omits a card has said nothing about
    it, and reading silence as deletion would let one forgetful reply wipe the
    board.

    A move-only report (``{"id": "t3", "column": "done"}``) keeps the title and
    detail the card already had, which is what the contract asks for once the
    board exists.
    """
    out = [dict(t) for t in existing]
    index = {t["id"]: i for i, t in enumerate(out)}
    for entry in reported if isinstance(reported, list) else []:
        task = _as_task(entry, fallback_role)
        if task is None:
            continue
        if task["id"] in index:
            current = out[index[task["id"]]]
            current["column"] = task["column"]
            for key in ("title", "detail", "note"):
                if task[key]:
                    current[key] = task[key]
            if isinstance(entry, dict):
                if entry.get("assigned_to"):
                    current["assigned_to"] = task["assigned_to"]
                if entry.get("kind"):
                    current["kind"] = task["kind"]
        else:
            index[task["id"]] = len(out)
            out.append(task)
    return out


def _as_health(value: Any) -> str:
    """One of the three health values, or UNKNOWN. Never guesses PASSING."""
    text = str(value or "").strip().upper()
    if text in HEALTH_VALUES:
        return text
    # Accept the obvious synonyms an agent might reach for, but only for the
    # two that are safe to infer. Anything unrecognised stays UNKNOWN.
    if text in ("PASS", "PASSED", "GREEN", "OK", "SUCCESS"):
        return HEALTH_PASSING
    if text in ("FAIL", "FAILED", "RED", "BROKEN", "ERROR"):
        return HEALTH_FAILING
    return HEALTH_UNKNOWN


def _cli_identity(provider: Dict[str, Any]) -> tuple:
    """Which binary a chair really runs, for telling two chairs apart.

    The whole command rather than the agent id, because two custom chairs are
    two different agents and both report the same one.
    """
    return tuple(str(part) for part in (provider.get("command") or ()))


def _as_snapshot(value: Any) -> Optional[gitutil.Snapshot]:
    """The safety snapshot back off the board, or None if it is not one.

    A snapshot without a commit records no tree and would restore nothing, so
    it is read as absent rather than as protection that is not there.
    """
    if not isinstance(value, dict):
        return None
    root = str(value.get("root") or "")
    head = str(value.get("head") or "")
    commit = str(value.get("commit") or "")
    if not (root and head and commit):
        return None
    return gitutil.Snapshot(
        root=root,
        head=head,
        commit=commit,
        had_changes=bool(value.get("had_changes")),
        ref=str(value.get("ref") or ""),
    )


# --------------------------------------------------------------------------
# The .theseus directory
# --------------------------------------------------------------------------


class Workspace:
    """The ``.theseus/`` directory inside a project root.

    Every read here is forgiving and every write is atomic. The files sit in a
    folder three coding agents have write access to, so a truncated or
    hand-mangled BOARD.json has to degrade to "the engine keeps what it had"
    rather than take the run down.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @property
    def dir(self) -> Path:
        return self.root / THESEUS_DIR

    @property
    def board_path(self) -> Path:
        return self.dir / BOARD_FILE

    @property
    def critique_path(self) -> Path:
        return self.dir / CRITIQUE_FILE

    @property
    def spec_path(self) -> Path:
        return self.dir / SPEC_FILE

    def ensure(self) -> None:
        """Create the directory and seed the log.

        Seeded rather than left absent so the first turn has something to
        append to, and so a ``.theseus`` directory somebody finds in a checkout
        explains itself.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.critique_path.exists():
            self.critique_path.write_text(
                "# Critique log\n\n"
                "Append-only. Every build failure, review finding and "
                "verification result, oldest first. The board itself is in "
                "BOARD.json.\n",
                encoding="utf-8",
            )

    # -- reads (never raise) ----------------------------------------------

    def read_text(self, path: Path, limit: int = 0) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return prompts.clip(text, limit) if limit and len(text) > limit else text

    def read_board(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.board_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def critique(self, limit: int = 0) -> str:
        return self.read_text(self.critique_path, limit)

    def spec(self, limit: int = 0) -> str:
        return self.read_text(self.spec_path, limit)

    # -- writes ------------------------------------------------------------

    def write_board(self, board: Dict[str, Any]) -> None:
        """Persist BOARD.json atomically. Failure is reported, not fatal."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.board_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(board, indent=2, default=str) + "\n", encoding="utf-8")
        tmp.replace(self.board_path)

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
    role: str
    label: str  # which CLI ran it, e.g. "Claude"
    heading: str  # what it was asked to do, e.g. "Fix the failing build"
    trigger: str  # why the loop chose it, e.g. "build FAILING"
    state: str = "running"  # running | done | failed | skipped
    read_only: bool = False
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
            "role": self.role,
            "role_label": ROLE_LABELS.get(self.role, self.role),
            "label": self.label,
            "heading": self.heading,
            "trigger": self.trigger,
            "state": self.state,
            "read_only": self.read_only,
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
class Decision:
    """What the board says should happen next, and who does it."""

    role: str
    kind: str  # audit | plan | implement | fix | verify | review | innovate
    heading: str
    trigger: str
    status: str
    build: Callable[[str], str]
    read_only: bool = False
    tasks: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class Project:
    """One autonomous build, from goal to hand-off."""

    id: str
    goal: str
    workspace: str
    status: str = AUDITING
    active_role: str = "qa"
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    build_health: str = HEALTH_UNKNOWN
    # Whether there is anything worth building right now. Distinct from
    # ``build_health == UNKNOWN``, which is also true of a project nobody has
    # written a line of code for yet: verifying an empty folder produces a
    # failing build ("no tests ran") and sends the developer off to fix a
    # project that does not exist. Set when an agent writes code, and at start
    # for a workspace that arrived with build tooling already in it - there,
    # "was this repository already broken before we touched it?" is worth one
    # turn to find out.
    needs_verification: bool = False
    last_build_log: str = ""
    # The other verdict, and the one a card-by-card build cannot reach on its
    # own: whether the *application* does what the goal asked for. A green
    # build says every command exited zero, which a project can manage while
    # never starting, never serving a page and never satisfying a single one of
    # the architect's acceptance criteria. Same shape as the build health and
    # the same invariant behind it - only an acceptance turn may set PASSING,
    # and writing code clears it.
    release_health: str = HEALTH_UNKNOWN
    needs_release: bool = False
    last_release_log: str = ""
    ideas: List[Dict[str, Any]] = field(default_factory=list)
    tooling: Dict[str, Any] = field(default_factory=dict)
    audited: bool = False
    continuation_needed: bool = False
    last_run_timestamp: str = ""
    created_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    steps_used: int = 0
    fix_attempts: int = 0
    innovation_rounds: int = 0
    stall_count: int = 0
    last_fingerprint: str = ""
    error: str = ""
    # How it ended, when it ended well. Kept apart from `error` rather than
    # reusing it: a summary in the error field reads as a failure everywhere
    # that checks whether one is set, starting with the banner.
    note: str = ""
    paused: bool = False
    # Set when the operator closes the report on a finished project. The build
    # stays exactly where it is; this is what stops the tab handing the same
    # finished board back the moment it reloads.
    dismissed: bool = False
    snapshot: Optional[gitutil.Snapshot] = None
    steps: List[StepRecord] = field(default_factory=list)
    # Frozen at start, exactly as a council run freezes its own: a project runs
    # for an hour and the operator will reasonably change settings during it.
    # Which CLI holds which chair must not change under a running turn.
    config: Dict[str, Any] = field(default_factory=dict, repr=False)

    # -- the board ---------------------------------------------------------

    def column(self, name: str) -> List[Dict[str, Any]]:
        """Every card currently in one column, in board order."""
        return [t for t in self.tasks if t.get("column") == name]

    def open_tasks(self) -> List[Dict[str, Any]]:
        """Everything not yet done."""
        return [t for t in self.tasks if t.get("column") != COL_DONE]

    def next_card(self) -> Optional[Dict[str, Any]]:
        """The card the developer should claim: bugs first, then board order.

        Anything already in progress outranks the backlog - a card left in
        progress by a turn that died is the thing to finish, not to duplicate.
        """
        for column in (COL_IN_PROGRESS, COL_BACKLOG):
            cards = self.column(column)
            bugs = [c for c in cards if c.get("kind") == KIND_BUG]
            if bugs:
                return bugs[0]
            if cards:
                return cards[0]
        return None

    def fingerprint(self) -> str:
        """A hash of everything a turn is supposed to be able to change.

        Used only to notice a stalled swarm. Deliberately excludes step counts
        and timestamps, which move whether or not any work happened.
        """
        payload = json.dumps(
            {
                "tasks": [
                    [t.get("id"), t.get("column"), t.get("kind")] for t in self.tasks
                ],
                "health": self.build_health,
                "release": self.release_health,
                "ideas": len(self.ideas),
                "audited": self.audited,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def board_document(self) -> Dict[str, Any]:
        """``.theseus/BOARD.json`` as the engine writes it.

        Cards are grouped into columns here rather than carrying a ``column``
        field, because this file is read by agents and a Kanban board with
        columns in it is the thing they were told to expect. The engine holds
        them flat internally, where moving one is an assignment rather than
        two list operations.
        """
        return {
            "project_id": self.id,
            "goal": self.goal,
            "status": self.status,
            "build_health": self.build_health,
            "needs_verification": self.needs_verification,
            "last_build_log": prompts.clip(self.last_build_log, MAX_BUILD_LOG),
            "release_health": self.release_health,
            "needs_release": self.needs_release,
            "last_release_log": prompts.clip(self.last_release_log, MAX_BUILD_LOG),
            "active_agent": self.active_role,
            "last_run_timestamp": self.last_run_timestamp,
            "continuation_needed": self.continuation_needed,
            "columns": {
                name: [
                    {k: v for k, v in card.items() if k != "column"}
                    for card in self.column(name)
                ]
                for name in COLUMNS
            },
            "ideas": copy.deepcopy(self.ideas),
            "tooling": copy.deepcopy(self.tooling),
            # -- engine bookkeeping --------------------------------------
            "audited": self.audited,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "steps_used": self.steps_used,
            "fix_attempts": self.fix_attempts,
            "innovation_rounds": self.innovation_rounds,
            "error": self.error,
            "note": self.note,
            "dismissed": self.dismissed,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }

    def to_dict(
        self, critique: str = "", board_json: str = "", pausing: bool = False
    ) -> Dict[str, Any]:
        doc = self.board_document()
        counts = {name: len(self.column(name)) for name in COLUMNS}
        return {
            **doc,
            "id": self.id,
            "workspace": self.workspace,
            "columns_order": list(COLUMNS),
            "column_labels": COLUMN_LABELS,
            "role_labels": ROLE_LABELS,
            "counts": counts,
            "paused": self.paused,
            # A pause is a request until the agent that was mid-turn exits. The
            # two states read very differently to somebody deciding whether to
            # open the folder, so the banner is told them apart.
            "pausing": pausing,
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
            "critique": critique,
            "board_json": board_json,
        }

    # -- restoring from disk ----------------------------------------------

    @classmethod
    def from_board(cls, workspace: str, data: Dict[str, Any]) -> "Project":
        """Rebuild a project from a BOARD.json written by an earlier session.

        Anything missing or mangled falls back to a value that makes the run
        finishable rather than one that makes it look finished - which is why
        an unreadable ``build_health`` lands on UNKNOWN, not PASSING.

        The snapshot comes back with it. It is the pointer to the tree as it
        was before the first agent wrote anything, and a resumed project that
        forgets it takes a fresh one over the half-built codebase - which is
        the one piece of state whose loss cannot be repaired by running the
        project again.
        """
        tasks: List[Dict[str, Any]] = []
        columns = data.get("columns")
        if isinstance(columns, dict):
            for name in COLUMNS:
                for entry in columns.get(name) or []:
                    card = _as_task(entry)
                    if card is not None:
                        card["column"] = name
                        tasks.append(card)

        status = str(data.get("status") or PLANNING).upper()
        if status not in {
            AUDITING, PLANNING, IMPLEMENTING, TESTING, REVIEWING, ACCEPTING,
            INNOVATING, COMPLETED, FAILED,
        }:
            status = PLANNING
        role = str(data.get("active_agent") or "qa")

        ideas = [i for i in (data.get("ideas") or []) if isinstance(i, dict)]
        tooling = data.get("tooling")

        return cls(
            id=str(data.get("project_id") or uuid.uuid4().hex[:12]),
            goal=str(data.get("goal") or ""),
            workspace=workspace,
            status=status,
            active_role=role if role in ROLES else "qa",
            tasks=tasks,
            build_health=_as_health(data.get("build_health")),
            needs_verification=bool(data.get("needs_verification")),
            last_build_log=str(data.get("last_build_log") or ""),
            release_health=_as_health(data.get("release_health")),
            needs_release=bool(data.get("needs_release")),
            last_release_log=str(data.get("last_release_log") or ""),
            ideas=ideas,
            tooling=tooling if isinstance(tooling, dict) else {},
            audited=bool(data.get("audited")),
            continuation_needed=bool(data.get("continuation_needed")),
            last_run_timestamp=str(data.get("last_run_timestamp") or ""),
            created_at=_as_float(data.get("created_at"), time.time()),
            ended_at=_as_float(data.get("ended_at"), 0.0),
            steps_used=int(_as_float(data.get("steps_used"), 0)),
            fix_attempts=int(_as_float(data.get("fix_attempts"), 0)),
            innovation_rounds=int(_as_float(data.get("innovation_rounds"), 0)),
            error=str(data.get("error") or ""),
            note=str(data.get("note") or ""),
            dismissed=bool(data.get("dismissed")),
            snapshot=_as_snapshot(data.get("snapshot")),
        )


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------


class ProjectEngine:
    """Drives one project by reading the board and picking an agent.

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
        # turns, which is the only place a pause can take effect without
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

        With no project in memory it looks on disk: a ``.theseus/BOARD.json``
        in the chosen folder is a project this app was running before it was
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
            return {
                "running": running,
                "resumable": not running and _resumable(project),
                "project": self._payload(project),
            }

        folder = folder or (self.store.get("workspace") or "").strip()
        if folder:
            ws = Workspace(folder)
            data = ws.read_board()
            if data.get("project_id") and not data.get("dismissed"):
                found = Project.from_board(folder, data)
                found.config = self.store.all()
                return {
                    "running": False,
                    "resumable": _resumable(found),
                    "found_on_disk": True,
                    "project": found.to_dict(
                        critique=ws.critique(MAX_UI_TEXT),
                        board_json=json.dumps(data, indent=2),
                    ),
                }
        return {"running": False, "resumable": False, "project": None}

    # -- control -----------------------------------------------------------

    def start(
        self,
        goal: str,
        workspace: str = "",
        resume: bool = False,
        innovation: Optional[int] = None,
    ) -> Project:
        """Begin, or pick up, an autonomous build. Returns once the loop is up.

        Everything that can refuse the run is checked here, before a single
        agent is launched: the goal, the folder, the three CLIs. A project that
        discovers on its fourth turn that its QA binary was never installed has
        already spent three rounds of quota finding out.
        """
        goal = (goal or "").strip()
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
            data = ws.read_board()
            if not data.get("project_id"):
                raise ValueError(
                    f"There is no project to resume in {root} - no "
                    f"{THESEUS_DIR}/{BOARD_FILE} was found there."
                )
            project = Project.from_board(root, data)
            if project.status == COMPLETED:
                raise ValueError(
                    f"That project already finished ({project.status.title()}). "
                    f"Start a new one, or move {THESEUS_DIR}/ aside to build "
                    f"here again."
                )
            if project.status == FAILED:
                # A run that hit a limit is exactly the case the limits exist
                # for, and the bounds documentation promises it can be picked
                # up once the operator has unstuck it. The two counters that
                # ended it are the two that have to be cleared, or the first
                # decision of the resumed run ends it again for the same
                # reason - having spent nothing and learned nothing.
                project.status = PLANNING
                project.fix_attempts = 0
                project.stall_count = 0
                project.ended_at = 0.0
                project.note = ""
            if goal:
                project.goal = goal
            if not project.goal:
                raise ValueError(
                    "That project's board carries no goal, so there is nothing "
                    "to continue from. Type one to resume it."
                )
        else:
            if not goal:
                raise ValueError("Describe what you want built.")
            existing = ws.read_board()
            if existing.get("project_id") and str(
                existing.get("status")
            ) not in TERMINAL_STATUSES:
                raise ValueError(
                    f"{root} already has a project in progress "
                    f"({existing.get('status')}). Resume it, or move "
                    f"{THESEUS_DIR}/ aside to start over."
                )
            project = Project(id=uuid.uuid4().hex[:12], goal=goal, workspace=root)

        if innovation is None:
            project.innovation_rounds = _int_setting(settings, "innovation_rounds", 2, floor=0)
        else:
            project.innovation_rounds = max(0, min(int(innovation), 10))

        project.config = conf
        project.paused = False
        project.error = ""
        project.dismissed = False

        missing = self._unavailable_roles(conf)
        if missing:
            raise ValueError(self._missing_message(missing))

        # Read-only, and before anything is written: what this project already
        # builds with is a fact about the folder, not something to ask an agent
        # to guess at.
        project.tooling = detect_tooling(root)
        # A workspace that arrived with real build tooling gets one baseline
        # verification, so a repository that was already red when we were
        # pointed at it is known to be our starting position rather than
        # discovered three cards later and blamed on the developer.
        if not resume and project.tooling.get("commands"):
            project.needs_verification = True

        ws.ensure()
        self._write_board(project)

        with self._lock:
            self._project = project
            self._stop.clear()
            self._resume.set()
            self._handoff_role = ""

        self.store.remember_workspace(root)
        self.bus.publish("project_started", project=self._payload(project))

        ignored = ensure_gitignored(root)
        if ignored:
            self._log("info", ignored)
        if project.tooling.get("commands"):
            self._log(
                "info",
                f"Adopting this project's own tooling: "
                f"{', '.join(project.tooling['commands'][:4])}.",
            )
        self._log(
            "info",
            f"Project {project.id} started in {root}. The first turn is a "
            f"read-only audit; every turn after it runs with its agent's "
            f"auto-approve flags, so files will change without asking.",
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
        """Halt after the running agent finishes its turn.

        Deliberately not mid-turn. A CLI killed between two writes leaves a
        half-written file that nothing in the run knows is half-written, and
        the next agent builds on it. Waiting costs a few minutes and leaves the
        tree consistent - and **Stop** is there for the operator who wants it
        to end now.
        """
        with self._lock:
            project = self._project
            if project is None or project.status in TERMINAL_STATUSES:
                raise ValueError("No project is running.")
            project.paused = True
        self._resume.clear()
        self._log("warn", "Pausing after the current agent finishes its turn.")
        self._publish_state()

    def resume(self) -> None:
        """Release a pause. The loop re-reads the board and carries on."""
        with self._lock:
            project = self._project
            if project is None:
                raise ValueError("No project to resume.")
            project.paused = False
        self._resume.set()
        self._log("info", "Resumed.")
        self._publish_state()

    def handoff(self, role: str) -> None:
        """Force the next turn onto a different agent.

        For the case the board cannot detect: an agent that is answering,
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
            f"Hand-off requested: the next turn runs on the "
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

    def dismiss(self, workspace: str = "") -> None:
        """Close the report on a project, so the tab offers a fresh one.

        This is *closing the report*, not deleting the build: everything the
        agents wrote stays where it is and the board keeps saying what happened.
        What it stops is the tab handing the same finished project straight back
        - the engine holds it in memory and finds it again on disk, so clearing
        it in the browser alone lasts exactly until the next reload.

        A run in flight cannot be dismissed. Hiding three agents mid-build
        behind an initializer is the wrong thing to show.
        """
        folder = (workspace or "").strip()
        with self._lock:
            if self.is_running():
                raise ValueError(
                    "That project is still running. Pause or stop it first."
                )
            project = self._project
            if project is not None and (not folder or folder == project.workspace):
                project.dismissed = True
                self._project = None
            else:
                project = None

        if project is not None:
            self._write_board(project)
            return
        if not folder:
            return
        ws = Workspace(folder)
        data = ws.read_board()
        if not data.get("project_id"):
            return
        data["dismissed"] = True
        try:
            ws.write_board(data)
        except OSError as exc:
            raise ValueError(
                f"Could not close that project's board: {exc}"
            ) from exc

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
        project may be pointed at one service inside a monorepo, and silently
        redirecting it from ``~/code/repo/subdir`` to ``~/code/repo`` would have
        it build over a codebase the operator never pointed at.
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
        """Worker-thread body: read the board, pick an agent, repeat."""
        try:
            # Only where there is not one already. A resumed project carries
            # the snapshot of the tree as it was before the first agent wrote
            # anything; retaking it here would anchor the half-built codebase
            # instead and quietly replace the only thing a rollback could
            # restore.
            if project.snapshot is None:
                self._take_snapshot(project)
            settings = project.config.get("project") or {}
            max_steps = _int_setting(settings, "max_steps", 40)
            project.last_fingerprint = project.fingerprint()

            while True:
                # Terminal first, and before the stop check: a turn that has
                # just finished the project and a stop that arrives in the same
                # moment would otherwise rewrite a COMPLETED run as "stopped by
                # the operator", which is both wrong and unrecoverable.
                if project.status in TERMINAL_STATUSES:
                    return

                if self._stop.is_set():
                    self._finish(project, FAILED, "Stopped by the operator.")
                    return

                # The pause gate. Between turns only - see `pause`.
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
                        f"Reached the turn limit ({max_steps} agent turns) "
                        f"without finishing. Raise it in Settings, or resume "
                        f"the project - everything built so far is on disk and "
                        f"the board says where it got to.",
                    )
                    return

                decision = self._decide(project)
                if decision is None:
                    self._complete(project)
                    return

                self._run_decision(project, decision)
        except Exception as exc:  # a crash here must not wedge the engine
            self._finish(project, FAILED, f"{type(exc).__name__}: {exc}")

    # -- the decision engine -----------------------------------------------

    def _decide(self, project: Project) -> Optional[Decision]:
        """What this project needs next. ``None`` means it is finished.

        The order is the policy, and it is the part of this engine worth
        arguing about:

        1. Nothing has looked at the folder yet - audit it, read-only.
        2. The board is empty - the goal has not been decomposed.
        3. The build is failing - nothing else matters until it is not.
        4. The build is unverified - somebody wrote code and nobody ran it.
        5. Cards are waiting on review - close the loop before opening another.
        6. Cards are waiting to be built - do the work.
        7. The board is clear but nobody has used what was built - accept it.
        8. Everything is done, green and accepted - propose what is missing.
        """
        settings = project.config.get("project") or {}
        goal = project.goal

        # 1. Startup: a read-only look at what is already here.
        if not project.audited:
            return Decision(
                role="qa",
                kind="audit",
                heading="Audit the workspace",
                trigger="project start",
                status=AUDITING,
                read_only=True,
                build=lambda ctx: prompts.build_audit_prompt(goal, ctx),
            )

        # 2. A goal nobody has broken down yet.
        if not project.tasks:
            return Decision(
                role="architect",
                kind="plan",
                heading="Break the goal into tasks",
                trigger="board empty",
                status=PLANNING,
                build=lambda ctx: prompts.build_plan_prompt(goal, ctx),
            )

        # 3. A failing build outranks everything. Bounded, because an agent
        #    that cannot fix a build in three tries will not fix it in thirty.
        if project.build_health == HEALTH_FAILING:
            max_fix = _int_setting(settings, "max_fix_attempts", 3)
            if project.fix_attempts >= max_fix:
                self._finish(
                    project,
                    FAILED,
                    f"The build has failed {project.fix_attempts} times in a "
                    f"row and the developer could not clear it. The last "
                    f"failure is on the board and in "
                    f"{THESEUS_DIR}/{CRITIQUE_FILE}; everything written so far "
                    f"is still on disk.",
                )
                return None
            log = project.last_build_log
            return Decision(
                role="coder",
                kind="fix",
                heading="Fix the failing build",
                trigger="build FAILING",
                status=IMPLEMENTING,
                build=lambda ctx: prompts.build_fix_prompt(goal, ctx, log),
            )

        # 4. Somebody wrote code and nobody has run it since.
        if project.needs_verification:
            return Decision(
                role="qa",
                kind="verify",
                heading="Build and test",
                trigger="unverified changes",
                status=TESTING,
                build=lambda ctx: prompts.build_verify_prompt(goal, ctx),
            )

        # 5. Review before starting more work, so the queue cannot run away.
        in_review = project.column(COL_IN_REVIEW)
        if in_review:
            return Decision(
                role="architect",
                kind="review",
                heading=f"Review {len(in_review)} change(s)",
                trigger=f"{len(in_review)} in review",
                status=REVIEWING,
                tasks=in_review,
                build=lambda ctx: prompts.build_review_prompt(goal, ctx, in_review),
            )

        # 6. Work waiting to be done.
        card = project.next_card()
        if card is not None:
            label = card.get("title") or card["id"]
            return Decision(
                role="coder",
                kind="implement",
                heading=prompts.clip(label, 80),
                trigger=(
                    "bug on the board" if card.get("kind") == KIND_BUG else "backlog"
                ),
                status=IMPLEMENTING,
                tasks=[card],
                build=lambda ctx: prompts.build_implement_prompt(goal, ctx, card),
            )

        # 7. The board is clear and the build is green. Nobody has run the
        #    thing. A build assembled one card at a time can pass every test
        #    it wrote for itself and still not start, not serve a page, and
        #    not do what the goal asked for - which is the only question the
        #    operator was ever asking. This turn is where that is answered,
        #    and it is the last gate before a project may call itself done.
        if project.needs_release:
            return Decision(
                role="qa",
                kind="release",
                heading="Use what was built",
                trigger="board clear, unaccepted",
                status=ACCEPTING,
                build=lambda ctx: prompts.build_release_prompt(goal, ctx),
            )

        # 8. Everything green and accepted, nothing left - what did we miss?
        if project.innovation_rounds > 0:
            rounds = project.innovation_rounds
            return Decision(
                role="architect",
                kind="innovate",
                heading="Propose enhancements",
                trigger="board clear, build green",
                status=INNOVATING,
                build=lambda ctx: prompts.build_innovate_prompt(goal, ctx, rounds),
            )

        return None

    # -- applying one turn's report ----------------------------------------

    def _run_decision(self, project: Project, decision: Decision) -> None:
        """Run the chosen agent and fold its report back into the board."""
        project.status = decision.status
        project.active_role = decision.role
        project.last_run_timestamp = _stamp()

        # A claimed card is in progress before the agent starts, not after: if
        # this turn dies, the board must already say who was holding it.
        if decision.kind == "implement" and decision.tasks:
            self._move(project, decision.tasks[0]["id"], COL_IN_PROGRESS)

        self._write_board(project)
        self._publish_state()

        step, report = self._run_role(project, decision)
        if step is None:
            return  # stopped mid-turn; _run_role has already finished the run

        applier = {
            "audit": self._apply_audit,
            "plan": self._apply_plan,
            "implement": self._apply_implement,
            "fix": self._apply_fix,
            "verify": self._apply_verify,
            "release": self._apply_release,
            "review": self._apply_review,
            "innovate": self._apply_innovate,
        }[decision.kind]
        applier(project, decision, step, report)

        self._note_progress(project)
        self._write_board(project)
        self._publish_state()

    def _apply_audit(
        self, project: Project, decision: Decision, step: StepRecord, report: Dict[str, Any]
    ) -> None:
        """The read-only look at the folder. Cannot fail the run.

        A failed audit is worth a warning and nothing more: the engine already
        read the tooling off disk itself, so the run has what it needs even if
        the agent said nothing useful.
        """
        project.audited = True
        if not step.ok:
            self._log(
                "warn",
                f"The workspace audit failed ({step.error}). Continuing from "
                f"what the engine could read off disk itself.",
            )
        notes = str(report.get("reasoning") or "").strip()
        if notes:
            Workspace(project.workspace).append_critique(
                f"## {_stamp()} - Workspace audit ({ROLE_LABELS[step.role]})\n\n{notes}\n"
            )
        # An audit reads; it does not verify. Whatever it says about the build,
        # the board's health stays unknown until QA has actually run one - a
        # green CI badge in somebody's README is not a build.
        project.build_health = HEALTH_UNKNOWN

    def _apply_plan(
        self, project: Project, decision: Decision, step: StepRecord, report: Dict[str, Any]
    ) -> None:
        project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")
        # Judged on what landed on the board, not on the exit status: an
        # architect that wrote a plan and *then* fell over has done the job.
        if not project.tasks:
            self._finish(
                project,
                FAILED,
                f"The architect produced no tasks, so there is nothing for the "
                f"developer to build. {step.error}".strip(),
            )
            return
        self._log(
            "info",
            f"{len(project.tasks)} task(s) on the board.",
        )

    def _apply_implement(
        self, project: Project, decision: Decision, step: StepRecord, report: Dict[str, Any]
    ) -> None:
        card_id = decision.tasks[0]["id"] if decision.tasks else ""
        project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")
        worked = self._turn_succeeded(step)

        # If the developer did not say where the card went, the engine decides
        # from the outcome. A turn that ran cleanly puts its work up for
        # review; one that failed - or that said so itself - leaves the card in
        # progress for the next attempt rather than quietly parking it.
        if card_id and not self._reported(report, card_id):
            self._move(project, card_id, COL_IN_REVIEW if worked else COL_IN_PROGRESS)

        if step.files_modified or worked:
            self._mark_dirty(project, f"the {ROLE_LABELS[step.role]} agent wrote code")

    def _apply_fix(
        self, project: Project, decision: Decision, step: StepRecord, report: Dict[str, Any]
    ) -> None:
        project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")
        project.fix_attempts += 1
        self._mark_dirty(project, "the developer attempted a fix")

    def _apply_verify(
        self, project: Project, decision: Decision, step: StepRecord, report: Dict[str, Any]
    ) -> None:
        """Fold a QA verdict into the board.

        Fail-closed on purpose. QA that *ran* and returned nothing usable is
        recorded as FAILING, never as passing: a silent verification turning
        the board green is the one outcome the rest of the loop must not build
        on, since every downstream decision - review, innovation, completion -
        reads PASSING as "somebody ran the tests".

        A QA turn that never ran is a different thing and is not written down
        as a red build. The trace would be the CLI's own crash, and the next
        turn hands it to the developer as something to fix - which it cannot,
        so it spends the whole fix budget on an error message about the app.
        The truthful state is the one before the turn: nobody has tested this
        tree. The stall guard is what ends a run whose QA chair keeps falling
        over, in three turns rather than in the fix budget plus three.
        """
        build = report.get("build")
        health = HEALTH_UNKNOWN
        log = ""
        if isinstance(build, dict):
            health = _as_health(build.get("health") or build.get("status"))
            log = str(build.get("log") or build.get("error") or "").strip()
        elif build is not None:
            health = _as_health(build)

        if health == HEALTH_UNKNOWN and not step.ok:
            project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")
            self._log(
                "warn",
                f"The verification turn did not run ({step.error or 'no detail'}). "
                f"The build stays unverified rather than being recorded as "
                f"failing - nothing about this tree has been tested.",
            )
            return

        if health == HEALTH_UNKNOWN:
            health = HEALTH_FAILING
            log = log or (
                f"QA ({ROLE_LABELS[step.role]}) finished without reporting a "
                f"build status, so the build is treated as failing. Run the "
                f"project's build and tests and report `build.health` "
                f"explicitly."
            )

        project.build_health = health
        project.needs_verification = False
        project.last_build_log = prompts.clip(log, MAX_BUILD_LOG)

        # QA may raise bugs off a failure. They are cards like any other, and
        # they jump the developer's queue.
        project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")

        if health == HEALTH_PASSING:
            project.fix_attempts = 0
            self._log("info", "Build passing.")
        else:
            self._log("warn", f"Build failing. {prompts.clip(log, 300)}")

        Workspace(project.workspace).append_critique(
            f"## {_stamp()} - Verification by {ROLE_LABELS[step.role]}: {health}\n\n"
            f"{prompts.clip(log, MAX_BUILD_LOG) or '(no detail reported)'}\n"
        )

    def _apply_release(
        self, project: Project, decision: Decision, step: StepRecord, report: Dict[str, Any]
    ) -> None:
        """Fold the acceptance verdict - did the application actually work?

        Fail-closed exactly as verification is, and for a sharper reason: this
        is the last thing that happens before COMPLETED, so an acceptance turn
        that said nothing usable must not be the one that lets a project call
        itself finished.

        Whatever it found that the tests did not comes back as bug cards, which
        jump the developer's queue like any other. Those reopen the loop -
        implement, verify, review - and writing code clears acceptance again,
        so the gate is passed only against the tree as it finally stands.
        """
        acceptance = report.get("acceptance")
        health = HEALTH_UNKNOWN
        log = ""
        if isinstance(acceptance, dict):
            health = _as_health(acceptance.get("health") or acceptance.get("status"))
            log = str(acceptance.get("log") or acceptance.get("notes") or "").strip()
        elif acceptance is not None:
            health = _as_health(acceptance)

        if health == HEALTH_UNKNOWN and not step.ok:
            project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")
            self._log(
                "warn",
                f"The acceptance turn did not run ({step.error or 'no detail'}). "
                f"Nobody has used this build, so it stays unaccepted rather "
                f"than being written down as broken.",
            )
            return

        if health == HEALTH_UNKNOWN:
            health = HEALTH_FAILING
            log = log or (
                f"QA ({ROLE_LABELS[step.role]}) finished the acceptance turn "
                f"without saying whether the application works, so it is "
                f"treated as not working. Run it and report "
                f"`acceptance.health` explicitly."
            )

        project.release_health = health
        project.needs_release = False
        project.last_release_log = prompts.clip(log, MAX_BUILD_LOG)

        before = len(project.tasks)
        project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")
        raised = len(project.tasks) - before

        if health == HEALTH_PASSING:
            self._log("info", "The application does what the goal asked for.")
        else:
            self._log(
                "warn",
                "The application does not yet do what the goal asked for"
                + (f", and {raised} card(s) say why." if raised > 0 else ".")
                + f" {prompts.clip(log, 300)}",
            )

        Workspace(project.workspace).append_critique(
            f"## {_stamp()} - Acceptance by {ROLE_LABELS[step.role]}: {health}\n\n"
            f"{prompts.clip(log, MAX_BUILD_LOG) or '(no detail reported)'}\n"
        )

        # The same grant every non-audit turn carries. If QA wrote an
        # end-to-end test to exercise the thing with, the tree it just accepted
        # is not the tree on disk any more.
        if step.files_modified:
            self._mark_dirty(project, f"the {ROLE_LABELS[step.role]} agent wrote code")

    def _apply_review(
        self, project: Project, decision: Decision, step: StepRecord, report: Dict[str, Any]
    ) -> None:
        """Approve or bounce the cards that were in review.

        A card the reviewer said nothing about stays in review. That looks like
        a stall, and it is meant to: the stall guard will end the run and say
        so, which is more honest than approving unreviewed work by default.
        """
        reviews = report.get("reviews")
        approved, bounced = 0, 0
        seen: set[str] = set()

        for entry in reviews if isinstance(reviews, list) else []:
            if not isinstance(entry, dict):
                continue
            card_id = str(entry.get("id") or "").strip()
            if not card_id:
                continue
            verdict = str(entry.get("verdict") or "").strip().lower()
            note = str(entry.get("note") or "").strip()
            seen.add(card_id)
            if verdict in ("approve", "approved", "accept", "pass", "ok"):
                self._move(project, card_id, COL_DONE, note=note)
                approved += 1
            else:
                self._move(project, card_id, COL_BACKLOG, note=note)
                bounced += 1
                if note:
                    Workspace(project.workspace).append_critique(
                        f"## {_stamp()} - Review of {card_id}: changes requested\n\n{note}\n"
                    )

        # The contract also allows a plain task move, for a reviewer that
        # reports through `tasks` instead of `reviews`.
        project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")

        if approved or bounced:
            self._log(
                "info",
                f"Review: {approved} approved, {bounced} sent back.",
            )
        else:
            self._log(
                "warn",
                f"The {ROLE_LABELS[step.role]} agent returned no review verdict, "
                f"so nothing moved out of review.",
            )
        # A review moves cards; it does not write code. Sending work back does
        # not make a verified tree unverified, and marking it so would spend a
        # QA turn rebuilding a tree nothing has touched since it last passed -
        # once per bounced card, before the developer has changed a line. What
        # *does* invalidate it is the reviewer editing something, which it can:
        # every turn after the audit carries its CLI's write grant.
        if step.files_modified:
            self._mark_dirty(project, f"the {ROLE_LABELS[step.role]} agent wrote code")

    def _apply_innovate(
        self, project: Project, decision: Decision, step: StepRecord, report: Dict[str, Any]
    ) -> None:
        """Turn proposed ideas into backlog cards, spending one round."""
        project.innovation_rounds = max(0, project.innovation_rounds - 1)

        ideas = [i for i in (report.get("ideas") or []) if isinstance(i, dict)]
        before = len(project.tasks)
        project.tasks = merge_tasks(project.tasks, report.get("tasks"), "coder")

        # Ideas reported as ideas rather than as cards still become cards -
        # otherwise a well-behaved architect that filled in the documented
        # `ideas` field would have its proposals silently dropped.
        for n, idea in enumerate(ideas, start=1):
            title = str(idea.get("title") or idea.get("description") or "").strip()
            if not title:
                continue
            card_id = str(idea.get("id") or "").strip() or f"idea_{len(project.tasks) + n}"
            if any(t["id"] == card_id for t in project.tasks):
                continue
            project.tasks.append(
                {
                    "id": card_id,
                    "title": title,
                    "detail": str(idea.get("detail") or idea.get("why") or "").strip(),
                    "column": COL_BACKLOG,
                    "kind": KIND_TASK,
                    "assigned_to": "coder",
                    "origin": ORIGIN_INNOVATION,
                    "note": "",
                }
            )

        project.ideas = (project.ideas + ideas)[-20:]
        added = len(project.tasks) - before
        if added > 0:
            self._log("info", f"{added} proposed enhancement(s) added to the backlog.")
        else:
            # Nothing proposed means the architect is out of ideas, which is a
            # finished project - not a reason to ask it again next turn.
            project.innovation_rounds = 0
            self._log("info", "No further enhancements proposed.")

    # -- board mutations ---------------------------------------------------

    def _move(
        self, project: Project, card_id: str, column: str, note: str = ""
    ) -> None:
        for card in project.tasks:
            if card["id"] == card_id:
                card["column"] = column
                if note:
                    card["note"] = prompts.clip(note, 600)
                return

    def _turn_succeeded(self, step: StepRecord) -> bool:
        """Whether a turn did its job, not merely whether the CLI exited zero.

        The report contract asks for `ok`, `blocked` or `failed` and says what
        a false `ok` costs. The reverse is worth the same care: an agent that
        exits cleanly and reports `blocked` has told us it got nowhere, and
        sending its card to review anyway asks the architect to read a diff
        that was never written.
        """
        if not step.ok:
            return False
        reported = step.reported_status.strip().lower()
        if reported in UNSUCCESSFUL_STATUSES:
            self._log(
                "warn",
                f"The {ROLE_LABELS[step.role]} agent exited cleanly but "
                f"reported `{reported}`, so its work is not treated as done.",
            )
            return False
        return True

    def _reported(self, report: Dict[str, Any], card_id: str) -> bool:
        """Whether the agent's report says anything about one card."""
        for entry in report.get("tasks") or []:
            if isinstance(entry, dict) and str(entry.get("id") or "").strip() == card_id:
                return True
        return False

    def _mark_dirty(self, project: Project, why: str) -> None:
        """Code changed, so the build is unverified until QA says otherwise.

        This is the invariant that makes PASSING mean something. Only a QA turn
        can set it, and any write clears it - so the board can never claim a
        green build for a tree nobody has tested in its current state.

        Acceptance goes with it, and for the same reason: an application that
        was used and worked two cards ago has not been used since.
        """
        if project.build_health == HEALTH_PASSING:
            self._log("info", f"Build health back to UNKNOWN: {why}.")
        project.build_health = HEALTH_UNKNOWN
        project.needs_verification = True
        project.release_health = HEALTH_UNKNOWN
        project.needs_release = True

    def _note_progress(self, project: Project) -> None:
        """Count consecutive turns that changed nothing on the board.

        Three agents can all run cleanly, report nothing actionable, and leave
        the board exactly as they found it - a reviewer with no verdict, a
        developer that claimed a card and wrote nothing. The turn limit would
        eventually catch that, thirty turns and an hour of quota later.
        """
        current = project.fingerprint()
        if current == project.last_fingerprint:
            project.stall_count += 1
        else:
            project.stall_count = 0
            project.last_fingerprint = current

        if project.stall_count >= STALL_LIMIT:
            self._finish(
                project,
                FAILED,
                f"The board has not moved in {project.stall_count} turns - the "
                f"agents are answering but nothing is changing. Everything "
                f"built so far is on disk; {THESEUS_DIR}/{CRITIQUE_FILE} and "
                f"the turn list say what they were doing. Try a hand-off to a "
                f"different agent, or a sharper goal.",
            )

    def _complete(self, project: Project) -> None:
        """Nothing left to do. Say how it went, honestly."""
        done = len(project.column(COL_DONE))
        total = len(project.tasks)
        caveats = []
        if project.build_health != HEALTH_PASSING:
            caveats.append(f"the build is {project.build_health}")
        if project.release_health != HEALTH_PASSING:
            caveats.append(
                "nobody got the application itself to work"
                if project.release_health == HEALTH_FAILING
                else "nobody ran the application itself"
            )
        stuck = [t for t in project.open_tasks()]
        if stuck:
            caveats.append(f"{len(stuck)} card(s) never reached Done")
        invented = len(
            [t for t in project.tasks if t.get("origin") == ORIGIN_INNOVATION]
        )
        self._finish(
            project,
            COMPLETED,
            "",
            note=(
                f"Finished: {done} of {total} card{_s(total)} done, build "
                f"{project.build_health.lower()}, in {project.steps_used} agent "
                f"turns."
                + (
                    f" {invented} of those {'was' if invented == 1 else 'were'} "
                    f"proposed by the council itself, not by you."
                    if invented else ""
                )
                + (f" Note: {'; '.join(caveats)}." if caveats else "")
            ),
        )

    # -- running one agent -------------------------------------------------

    def _run_role(
        self, project: Project, decision: Decision
    ) -> tuple[Optional[StepRecord], Dict[str, Any]]:
        """Run one turn, handing off to another agent if this one is exhausted.

        Returns ``(None, {})`` when the run was stopped mid-turn, which is the
        caller's signal to return without applying anything.

        The hand-off is the whole reason a project survives a context limit.
        The prompt is rebuilt for the replacement rather than replayed, because
        every project prompt is built from the board and the diff on disk - so
        the second agent starts from the same state, not from the first one's
        transcript.
        """
        forced = ""
        with self._lock:
            if self._handoff_role:
                forced, self._handoff_role = self._handoff_role, ""

        attempted: List[str] = []
        # And the binaries behind them. A hand-off exists to reach a CLI with
        # room left in it, so a spare chair pointed at the CLI that just ran
        # out is not a spare chair - it is the same agent under another name,
        # handed the same oversized prompt to fail on again. Nothing stops an
        # operator putting one agent in two seats; this stops that costing a
        # turn to discover.
        attempted_clis: set = set()
        runner_role = forced or decision.role
        handoff_from = decision.role if forced and forced != decision.role else ""

        while True:
            attempted.append(runner_role)
            provider = self._provider(project, runner_role)
            attempted_clis.add(_cli_identity(provider))
            step = StepRecord(
                index=project.steps_used + 1,
                role=runner_role,
                label=str(provider.get("label") or runner_role),
                heading=decision.heading,
                trigger=decision.trigger,
                read_only=decision.read_only,
                handoff_from=handoff_from,
            )
            project.steps_used += 1
            project.steps.append(step)
            del project.steps[:-MAX_STEP_RECORDS]

            prompt = decision.build(self._context(project, runner_role))
            self.bus.publish(
                "project_step",
                project=self._payload(project),
                step=step.to_dict(),
            )

            result = self._invoke(project, provider, prompt, decision.read_only)
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
                self._write_board(project)
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
            spare = [
                r for r in ROLES
                if r not in attempted
                and _cli_identity(self._provider(project, r)) not in attempted_clis
            ]
            if result.ok or not self._looks_exhausted(result) or not spare:
                project.continuation_needed = False
                self._write_board(project)
                return step, report

            project.continuation_needed = True
            self._write_board(project)
            handoff_from = runner_role
            runner_role = spare[0]
            self._log(
                "warn",
                f"The {ROLE_LABELS[handoff_from]} agent ran out of room "
                f"({result.error or 'context or quota limit'}). Handing this "
                f"turn to the {ROLE_LABELS[runner_role]} agent with the board "
                f"as its context.",
            )

    def _invoke(
        self,
        project: Project,
        provider: Dict[str, Any],
        prompt: str,
        read_only: bool,
    ) -> ProviderResult:
        """Launch one CLI.

        ``read_only`` is the audit turn and only the audit turn: the two grants
        are opposites and never both sent, so an audit gets the CLI's read-only
        flags and no auto-approve at all. Everything else writes - see the
        module docs.
        """
        runner = ProviderRunner(provider, self._output_cb(str(provider.get("id") or "")))
        with self._lock:
            self._runner = runner
            if self._stop.is_set():
                runner.cancel()
        try:
            return runner.run(
                prompt,
                cwd=project.workspace,
                auto_approve=not read_only,
                read_only=read_only,
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
        """Build the shared preamble for one turn, from the board and the tree.

        Deliberately not the transcript. What an agent gets is the board, the
        working diff, the last build failure and the house rules - never the
        previous turns' terminal output, which is the single largest thing that
        could be sent and the least useful per token.

        The two writing-style switches are the one thing read live rather than
        off `project.config`, which is a deep copy frozen at `start()`. A
        council run is minutes and has one voice to keep consistent; a project
        is dozens of independent turns across three CLIs over hours, and the
        gear that sets these sits in the tracker header of a *running* project.
        Reading them off the snapshot made that gear tick and change nothing.
        Only these two keys - providers, safety and house rules stay frozen,
        because a turn that changed CLI or permissions halfway is a different
        run, not a restyled one.
        """
        diff = ""
        try:
            if gitutil.repo_root(project.workspace):
                diff = gitutil.working_diff(project.workspace)
        except (gitutil.GitError, OSError):
            diff = ""
        return prompts.project_context_block(
            project_id=project.id,
            root=project.workspace,
            board_json=json.dumps(project.board_document(), indent=2),
            tooling=project.tooling,
            build_health=project.build_health,
            build_log=project.last_build_log,
            diff=diff,
            house_rules=str(project.config.get("house_rules") or ""),
            **cfg.writing_styles(self.store.all(), "project"),
        )

    # -- state transitions -------------------------------------------------

    def _finish(
        self, project: Project, status: str, error: str, note: str = ""
    ) -> None:
        """End the run. The first ending wins.

        A decision that ends the project has to report it *and* return - and
        every "nothing more to schedule" return looks the same to the loop,
        which reads it as success. Without this guard, giving up on a build
        that has failed its way through the fix budget is immediately
        overwritten by COMPLETED, and the run that gave up reports as the run
        that finished.
        """
        if project.status in TERMINAL_STATUSES:
            return
        project.status = status
        project.error = error
        project.note = note
        project.ended_at = time.time()
        project.last_run_timestamp = _stamp()
        self._write_board(project)
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

    def _write_board(self, project: Project) -> None:
        try:
            Workspace(project.workspace).write_board(project.board_document())
        except OSError as exc:
            # Losing the board costs resumability, not the run in flight.
            self._log("warn", f"Could not write {THESEUS_DIR}/{BOARD_FILE}: {exc}")

    def _payload(self, project: Project) -> Dict[str, Any]:
        ws = Workspace(project.workspace)
        with self._lock:
            # A live runner while paused means the pause has been asked for and
            # the agent it interrupts has not exited yet. Only once that is
            # gone is the claim the banner wants to make - nothing was
            # interrupted mid-write - actually true.
            pausing = project.paused and self._runner is not None
        return project.to_dict(
            critique=ws.critique(MAX_UI_TEXT),
            board_json=json.dumps(project.board_document(), indent=2),
            pausing=pausing,
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


def _resumable(project: Project) -> bool:
    """Whether this project can be picked up again.

    A run that hit a limit is the case the limits exist for: everything built
    is on disk, the board says where it got to, and the operator is meant to
    unstick it and carry on. COMPLETED is the only genuine end - there is
    nothing left to schedule - and a dismissed report is one the operator has
    already closed.
    """
    return project.status != COMPLETED and not project.dismissed


def _stamp() -> str:
    """An ISO-8601 timestamp, which is what the board asks for."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _s(n: int) -> str:
    """Plural suffix. These strings end up in the banner the operator reads."""
    return "" if n == 1 else "s"


def _as_float(value: Any, fallback: float) -> float:
    """One number out of a board field, however mangled."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _int_setting(
    settings: Dict[str, Any], key: str, fallback: int, floor: int = 1
) -> int:
    """One integer out of the project settings, however mangled.

    ``floor`` is 1 for the bounds that make no sense at zero (a run with no
    turns) and 0 for the innovation budget, where zero is a real choice: ship
    what I asked for and stop.
    """
    try:
        value = int(settings.get(key, fallback))
    except (TypeError, ValueError):
        return fallback
    return value if value >= floor else fallback
