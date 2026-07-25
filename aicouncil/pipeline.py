"""The Junior Draft / Senior Polish orchestration engine.

State machine
-------------

    idle
      |  start()
      v
    drafting                (skipped in Solo Mode)
      |
      v
    awaiting_approval       (skipped in Zero-Touch Mode)
      |  approve()      \\  reject() / cancel()
      v                  v
    polishing --------> cancelled
      |
      +--> complete  (exit 0)
      +--> failed    (non-zero exit, timeout, or an unhandled error)

Permission to write to disk is carried by ``execute_approved``. It is true
only when Zero-Touch Mode granted it up front or a human granted it at the
gate, and it is what decides whether the Stage 2 CLI receives its
auto-approve flags. Stage 1 is read-only and never receives them.

Only one run executes at a time. The engine owns a worker thread; every public
method is safe to call from the HTTP handler threads.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import config as cfg
from . import gitutil, prompts
from .events import EventBus
from .providers import ProviderResult, ProviderRunner, probe

# Pipeline states
IDLE = "idle"
DRAFTING = "drafting"
AWAITING_APPROVAL = "awaiting_approval"
POLISHING = "polishing"
COMPLETE = "complete"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = {COMPLETE, FAILED, CANCELLED}


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
        }


@dataclass
class Run:
    """A single end-to-end pipeline execution."""

    id: str
    task: str
    repo: str
    zero_touch: bool
    solo: bool
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "repo": self.repo,
            "zero_touch": self.zero_touch,
            "solo": self.solo,
            "state": self.state,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
            "stage_order": [s for s in ("drafter", "polisher") if s in self.stages],
            "diff": self.diff,
            "diff_stat": self.diff_stat,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "can_rollback": bool(self.snapshot) and self.state in TERMINAL_STATES,
            "reviewer_note": self.reviewer_note,
            "error": self.error,
            "rollback_note": self.rollback_note,
        }


class PipelineBusy(RuntimeError):
    """A run is already in flight."""


class Pipeline:
    """Owns the current run and drives it on a background thread."""

    def __init__(self, store: cfg.ConfigStore, bus: EventBus) -> None:
        self.store = store
        self.bus = bus
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
            "providers_status": [
                probe(providers[k]) for k in ("drafter", "polisher") if k in providers
            ],
        }

    def is_busy(self) -> bool:
        with self._lock:
            return self._run is not None and self._run.state not in TERMINAL_STATES

    # -- control -----------------------------------------------------------

    def start(self, task: str, repo: str) -> Run:
        """Kick off a new run. Raises PipelineBusy if one is already active."""
        task = (task or "").strip()
        if not task:
            raise ValueError("Task description is empty.")

        root = gitutil.repo_root(repo)
        if root is None:
            raise ValueError(
                f"{repo!r} is not a git repository. Pick a directory that "
                f"contains a .git folder, or run `git init` there first."
            )

        conf = self.store.all()
        if conf.get("require_clean_worktree"):
            st = gitutil.status(root)
            if not st.clean:
                raise ValueError(
                    f"Working tree has {len(st.staged) + len(st.modified) + len(st.untracked)} "
                    f"uncommitted change(s) and 'require clean worktree' is on. "
                    f"Commit or stash them first."
                )

        with self._lock:
            if self.is_busy():
                raise PipelineBusy("A run is already in progress.")

            solo = bool(conf.get("solo_mode"))
            zero_touch = bool(conf.get("zero_touch"))
            providers = conf.get("providers", {})

            run = Run(
                id=uuid.uuid4().hex[:12],
                task=task,
                repo=root,
                zero_touch=zero_touch,
                solo=solo,
            )
            if not solo:
                d = providers.get("drafter", {})
                run.stages["drafter"] = StageRecord(
                    id="drafter",
                    label=d.get("label", "Codex"),
                    role=d.get("role", "Junior Draft"),
                    model=str(d.get("model") or ""),
                )
            p = providers.get("polisher", {})
            run.stages["polisher"] = StageRecord(
                id="polisher",
                label=p.get("label", "Claude"),
                role=p.get("role", "Senior Polish"),
                model=str(p.get("model") or ""),
            )

            self._run = run
            self._cancel_requested = False
            self._gate.clear()
            self._gate_decision = ""

        self.store.remember_repo(root)
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
                "restore. Enable 'Safety snapshot' in Settings."
            )

        note = gitutil.restore_snapshot(snap)
        with self._lock:
            run.rollback_note = note
            run.diff = gitutil.working_diff(run.repo)
            run.diff_stat = gitutil.diff_stat(run.repo)
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
        prompt: str,
        auto_approve: bool,
    ) -> ProviderResult:
        provider = self.store.get("providers", {}).get(stage_id, {})
        stage = run.stages[stage_id]
        stage.model = str(provider.get("model") or "")
        stage.state = "running"
        stage.started_at = time.time()
        self.bus.publish("stage_started", stage=stage_id, run=run.to_dict())

        runner = ProviderRunner(provider, self._stage_output_cb(stage_id))
        with self._lock:
            self._runner = runner
            if self._cancel_requested:
                runner.cancel()

        result = runner.run(prompt, cwd=run.repo, auto_approve=auto_approve)

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
        """Worker-thread body for one run."""
        conf = self.store.all()
        house_rules = conf.get("house_rules", "")
        repo_status = gitutil.status(run.repo).to_dict()

        try:
            draft_text = ""
            # Whether Stage 2 may modify files. Zero-Touch grants this up
            # front; otherwise it is granted by the human at the approval
            # gate. Reaching Stage 2 without it would hand the CLI a task it
            # cannot complete: it would block on an interactive permission
            # prompt that nothing in this pipeline can answer.
            execute_approved = run.zero_touch

            # ---- Stage 1: Junior Draft ----------------------------------
            if not run.solo:
                self._set_state(run, DRAFTING)
                draft_prompt = prompts.build_draft_prompt(
                    run.task, run.repo, repo_status, house_rules
                )
                # Stage 1 is read-only by instruction, so it never receives the
                # auto-approve flags regardless of the Zero-Touch setting.
                result = self._run_stage(run, "drafter", draft_prompt, auto_approve=False)

                if self._is_cancelled():
                    self._finish_cancelled(run)
                    return

                if not result.ok:
                    # A failed junior is recoverable: the senior can work from
                    # the task alone. Warn and continue rather than aborting.
                    self.bus.publish(
                        "log",
                        level="warn",
                        message=(
                            f"Draft stage failed ({result.error}). "
                            f"Continuing with the senior stage alone."
                        ),
                    )
                draft_text = result.stdout

            # ---- Approval gate ----------------------------------------------
            # Shown whenever Zero-Touch is off, including in Solo Mode where
            # there is no draft to read - the operator is still approving that
            # an agent may write to their repository.
            if not run.zero_touch:
                self._set_state(run, AWAITING_APPROVAL, draft=draft_text)
                self.bus.publish(
                    "log",
                    level="info",
                    message="Paused for review. Nothing has been written to disk yet.",
                )
                self._gate.wait()
                with self._lock:
                    decision = self._gate_decision
                if decision != "approve" or self._is_cancelled():
                    self._finish_cancelled(run, "Rejected at the approval gate.")
                    return
                execute_approved = True

            # ---- Safety snapshot ----------------------------------------
            # Taken immediately before the only stage that writes to disk.
            if conf.get("safety_snapshot", True):
                try:
                    run.snapshot = gitutil.take_snapshot(run.repo)
                    if run.snapshot:
                        self.bus.publish(
                            "log",
                            level="info",
                            message=f"Safety snapshot taken at {run.snapshot.head[:8]}.",
                        )
                except (gitutil.GitError, OSError) as exc:
                    self.bus.publish(
                        "log",
                        level="warn",
                        message=f"Could not take a safety snapshot: {exc}",
                    )

            if self._is_cancelled():
                self._finish_cancelled(run)
                return

            # ---- Stage 2: Senior Polish ---------------------------------
            self._set_state(run, POLISHING)
            if run.solo:
                polish_prompt = prompts.build_solo_prompt(
                    run.task, run.repo, repo_status, house_rules
                )
            else:
                polish_prompt = prompts.build_polish_prompt(
                    run.task,
                    draft_text,
                    run.repo,
                    repo_status,
                    house_rules,
                    run.reviewer_note,
                )

            result = self._run_stage(
                run, "polisher", polish_prompt, auto_approve=execute_approved
            )

            # ---- Collect the diff ---------------------------------------
            try:
                run.diff = gitutil.working_diff(run.repo)
                run.diff_stat = gitutil.diff_stat(run.repo)
            except (gitutil.GitError, OSError) as exc:
                self.bus.publish(
                    "log", level="warn", message=f"Could not read the diff: {exc}"
                )

            if self._is_cancelled():
                self._finish_cancelled(run)
                return

            if result.ok:
                self._set_state(run, COMPLETE)
            else:
                run.error = result.error or "The senior stage failed."
                self._set_state(run, FAILED)

        except Exception as exc:  # a crash here must not wedge the engine
            run.error = f"{type(exc).__name__}: {exc}"
            self._set_state(run, FAILED)
        finally:
            self._persist(run)

    # -- helpers -----------------------------------------------------------

    def _is_cancelled(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def _finish_cancelled(self, run: Run, message: str = "Run cancelled.") -> None:
        for stage in run.stages.values():
            if stage.state in ("pending", "running"):
                stage.state = "skipped"
        run.error = message
        try:
            run.diff = gitutil.working_diff(run.repo)
            run.diff_stat = gitutil.diff_stat(run.repo)
        except (gitutil.GitError, OSError):
            pass
        self._set_state(run, CANCELLED)
        self._persist(run)

    def _persist(self, run: Run) -> None:
        """Write the run transcript to disk for later inspection."""
        try:
            path = cfg.runs_dir() / f"{int(run.created_at)}-{run.id}.json"
            path.write_text(
                json.dumps(run.to_dict(), indent=2, default=str), encoding="utf-8"
            )
        except OSError:
            pass  # a transcript we cannot save is not worth failing a run over

    def history(self, limit: int = 25) -> List[Dict[str, Any]]:
        """Summaries of recent runs, newest first."""
        out: List[Dict[str, Any]] = []
        try:
            files = sorted(cfg.runs_dir().glob("*.json"), reverse=True)[:limit]
        except OSError:
            return out
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            out.append(
                {
                    "id": data.get("id"),
                    "task": (data.get("task") or "")[:200],
                    "repo": data.get("repo"),
                    "state": data.get("state"),
                    "created_at": data.get("created_at"),
                    "zero_touch": data.get("zero_touch"),
                    "diff_stat": data.get("diff_stat") or {},
                    "file": f.name,
                }
            )
        return out

    def load_run(self, name: str) -> Optional[Dict[str, Any]]:
        """Load a persisted transcript by filename."""
        # Reject any path separator: this value comes from a query string.
        if "/" in name or "\\" in name or ".." in name:
            return None
        path = cfg.runs_dir() / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
