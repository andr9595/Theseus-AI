"""Tests for the Projects engine.

Two kinds here, deliberately separated:

* Unit tests over the parts that decide things - report parsing, task merging,
  the state file, exhaustion detection. These run in milliseconds and are where
  the edge cases live.

* One end-to-end test that drives the whole five-phase loop against a real
  directory using ``scripts/mock-agent.py``. It writes real files, runs a real
  test suite that really fails, gets handed the real trace, and finishes with a
  README. That one is slow and worth it: every unit test above it would still
  pass if the phases never actually connected.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil.config import ConfigStore  # noqa: E402
from aicouncil.events import EventBus  # noqa: E402
from aicouncil.projects import (  # noqa: E402
    COMPLETED,
    FAILED,
    IMPLEMENTING,
    PHASE_ARCHITECTURE,
    PHASE_FINALISATION,
    PHASE_IMPLEMENTATION,
    PHASE_QA,
    PLANNING,
    ROLES,
    Project,
    ProjectEngine,
    StepRecord,
    Workspace,
    merge_tasks,
    parse_report,
)
from aicouncil.providers import ProviderResult  # noqa: E402

MOCK = str(Path(__file__).resolve().parent.parent / "scripts" / "mock-agent.py")


def mock_provider(role: str, timeout: int = 180) -> dict:
    """A project chair pointed at the bundled mock agent."""
    return {
        "id": role,
        "label": f"Mock {role}",
        "command": [sys.executable, MOCK, "--role", role, "{prompt}"],
        "auto_approve_args": ["--dangerously-skip-permissions"],
        "model_args": [],
        "effort_args": [],
        "stream_args": [],
        "read_only_args": [],
        "prompt_on_stdin": True,  # the project prompt is far past ARG_MAX
        "timeout_seconds": timeout,
        "model": "",
        "effort": "",
    }


class TestReportParsing(unittest.TestCase):
    """The one channel the engine reads structured answers through."""

    def test_a_fenced_block_is_read(self):
        report = parse_report(
            'Did the work.\n\n```json\n{"status": "ok", '
            '"files_modified": ["a.py"]}\n```\n'
        )
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["files_modified"], ["a.py"])

    def test_the_last_block_wins(self):
        # An agent that quotes the contract back while explaining itself puts
        # an example block earlier in the reply. The report is what it ends on.
        report = parse_report(
            'I was asked for:\n\n```json\n{"status": "example"}\n```\n\n'
            'Here is mine:\n\n```json\n{"status": "ok"}\n```\n'
        )
        self.assertEqual(report["status"], "ok")

    def test_an_unfenced_trailing_object_is_read(self):
        report = parse_report('Done.\n{"status": "ok", "reasoning": "wrote it"}')
        self.assertEqual(report["reasoning"], "wrote it")

    def test_junk_is_not_a_report(self):
        # Never raises, and never invents. An unparseable reply means "it did
        # not tell me", which the engine treats very differently from failure.
        for text in ("", "no json here", "```json\n{not json}\n```", "```\n[]\n```"):
            self.assertEqual(parse_report(text), {})

    def test_a_bare_list_is_not_a_report(self):
        self.assertEqual(parse_report('```json\n["a", "b"]\n```'), {})


class TestTaskMerging(unittest.TestCase):
    def test_a_new_id_is_appended(self):
        out = merge_tasks([], [{"id": "task_1", "description": "do it"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "pending")
        self.assertEqual(out[0]["assigned_to"], "coder")

    def test_a_status_only_report_keeps_the_description(self):
        existing = [
            {"id": "task_1", "description": "write it", "status": "pending",
             "assigned_to": "coder"}
        ]
        out = merge_tasks(existing, [{"id": "task_1", "status": "completed"}])
        self.assertEqual(out[0]["description"], "write it")
        self.assertEqual(out[0]["status"], "completed")

    def test_omission_is_not_deletion(self):
        # Silence about a task says nothing about it. Reading it as removal
        # would let one forgetful reply erase the roadmap.
        existing = [
            {"id": "task_1", "description": "a", "status": "pending",
             "assigned_to": "coder"},
            {"id": "task_2", "description": "b", "status": "pending",
             "assigned_to": "coder"},
        ]
        out = merge_tasks(existing, [{"id": "task_1", "status": "completed"}])
        self.assertEqual([t["id"] for t in out], ["task_1", "task_2"])
        self.assertEqual(out[1]["status"], "pending")

    def test_an_unknown_status_falls_back_to_pending(self):
        out = merge_tasks([], [{"id": "task_1", "status": "nearly"}])
        self.assertEqual(out[0]["status"], "pending")

    def test_entries_without_an_id_are_dropped(self):
        out = merge_tasks([], [{"description": "no id"}, "not a dict", None])
        self.assertEqual(out, [])

    def test_an_unknown_role_is_not_stored(self):
        # `assigned_to` is a role, not an agent. A task that named a binary
        # would be wrong the moment the operator reassigned that chair.
        out = merge_tasks([], [{"id": "t", "assigned_to": "claude"}])
        self.assertEqual(out[0]["assigned_to"], "coder")


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-proj-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ensure_seeds_the_agent_owned_files(self):
        ws = Workspace(self.tmp)
        ws.ensure()
        self.assertTrue(ws.roadmap_path.exists())
        self.assertTrue(ws.critique_path.exists())

    def test_ensure_does_not_overwrite(self):
        ws = Workspace(self.tmp)
        ws.ensure()
        ws.roadmap_path.write_text("mine", encoding="utf-8")
        ws.ensure()
        self.assertEqual(ws.roadmap_path.read_text(encoding="utf-8"), "mine")

    def test_the_schema_keys_are_all_written(self):
        project = Project(id="abc", brief="build a thing", workspace=str(self.tmp))
        doc = project.state_document()
        for key in (
            "project_id", "status", "current_phase", "active_agent",
            "last_run_timestamp", "continuation_token_needed", "tasks",
            "last_execution_error", "build_status",
        ):
            self.assertIn(key, doc)

    def test_a_corrupt_state_file_reads_as_absent(self):
        # It sits in a folder three coding agents can write to. A truncated
        # file has to degrade to "keep what the engine had", not take the run
        # down with a JSONDecodeError on a worker thread.
        ws = Workspace(self.tmp)
        ws.ensure()
        ws.state_path.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(ws.read_state(), {})

    def test_a_project_round_trips_through_disk(self):
        ws = Workspace(self.tmp)
        ws.ensure()
        before = Project(id="abc", brief="a brief", workspace=str(self.tmp))
        before.status = IMPLEMENTING
        before.phase = PHASE_IMPLEMENTATION
        before.tasks = merge_tasks([], [{"id": "task_1", "description": "x"}])
        ws.write_state(before.state_document())

        after = Project.from_state(str(self.tmp), ws.read_state())
        self.assertEqual(after.id, "abc")
        self.assertEqual(after.brief, "a brief")
        self.assertEqual(after.status, IMPLEMENTING)
        self.assertEqual(after.phase, PHASE_IMPLEMENTATION)
        self.assertEqual([t["id"] for t in after.tasks], ["task_1"])

    def test_a_hand_mangled_state_resumes_at_the_start(self):
        # A phase of 99 or a status of "nearly" must not be dispatched on.
        after = Project.from_state(
            str(self.tmp),
            {"project_id": "x", "status": "nearly", "current_phase": 99,
             "active_agent": "nobody"},
        )
        self.assertEqual(after.status, PLANNING)
        self.assertEqual(after.phase, PHASE_ARCHITECTURE)
        self.assertIn(after.active_role, ROLES)

    def test_critique_is_append_only(self):
        ws = Workspace(self.tmp)
        ws.ensure()
        ws.append_critique("first")
        ws.append_critique("second")
        text = ws.critique_path.read_text(encoding="utf-8")
        self.assertIn("first", text)
        self.assertIn("second", text)


class TestExhaustionDetection(unittest.TestCase):
    """What decides whether a failure gets handed to another agent."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-proj-"))
        self.engine = ProjectEngine(ConfigStore(self.tmp / "config.json"), EventBus())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def result(self, **kw) -> ProviderResult:
        base = dict(
            provider_id="coder", ok=False, exit_code=1, stdout="", stderr="",
            duration=1.0, command=[],
        )
        base.update(kw)
        return ProviderResult(**base)

    def test_a_context_limit_is_exhaustion(self):
        for text in (
            "Error: prompt is too long: 210000 tokens > 200000 maximum",
            "context window exceeded",
            "You have hit your usage limit reached for this week",
            "rate limit exceeded, please try again later",
        ):
            self.assertTrue(
                self.engine._looks_exhausted(self.result(error=text)), text
            )

    def test_a_timeout_is_exhaustion(self):
        # The CLIs do not distinguish a model grinding through an oversized
        # context from one that is merely slow, and re-running the same prompt
        # on the same agent would only time out again.
        self.assertTrue(
            self.engine._looks_exhausted(self.result(timed_out=True, error="Timed out"))
        )

    def test_an_ordinary_failure_is_not(self):
        self.assertFalse(
            self.engine._looks_exhausted(
                self.result(error="SyntaxError: invalid syntax in greeter.py")
            )
        )


class TestPreflight(unittest.TestCase):
    """Everything that can refuse a project, before any quota is spent."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-proj-"))
        self.store = ConfigStore(self.tmp / "config.json")
        self.root = self.tmp / "build"
        self.root.mkdir()
        self.store.update(
            {"providers": {r: mock_provider(r) for r in ROLES},
             "workspace": str(self.root)}
        )
        self.engine = ProjectEngine(self.store, EventBus())

    def tearDown(self):
        self.engine.stop()
        self.engine.wait_for_worker(30)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_empty_brief_is_refused(self):
        with self.assertRaises(ValueError):
            self.engine.start("   ", str(self.root))

    def test_a_folder_that_does_not_exist_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.engine.start("build it", str(self.tmp / "nowhere"))
        self.assertIn("not a folder that exists", str(ctx.exception))

    def test_a_missing_cli_is_refused_by_name(self):
        # A project runs unattended. Discovering in phase 3 that the QA binary
        # was never installed costs two rounds of everyone's quota.
        self.store.update({"providers": {"qa": {"command": ["definitely-not-installed"]}}})
        with self.assertRaises(ValueError) as ctx:
            self.engine.start("build it", str(self.root))
        message = str(ctx.exception)
        self.assertIn("QA", message)
        self.assertIn("definitely-not-installed", message)

    def test_a_council_run_blocks_a_project(self):
        def busy():
            raise RuntimeError("A run is in progress.")

        engine = ProjectEngine(self.store, EventBus(), busy_check=busy)
        with self.assertRaises(RuntimeError):
            engine.start("build it", str(self.root))

    def test_resuming_nothing_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.engine.start("", str(self.root), resume=True)
        self.assertIn("no project to resume", str(ctx.exception).lower())

    def test_a_second_project_in_the_same_folder_is_refused(self):
        ws = Workspace(self.root)
        ws.ensure()
        ws.write_state({"project_id": "earlier", "status": IMPLEMENTING})
        with self.assertRaises(ValueError) as ctx:
            self.engine.start("build something else", str(self.root))
        self.assertIn("already has a project in progress", str(ctx.exception))

    def test_a_finished_project_does_not_block_a_new_one(self):
        ws = Workspace(self.root)
        ws.ensure()
        ws.write_state({"project_id": "earlier", "status": COMPLETED})
        project = self.engine.start("build something else", str(self.root))
        self.assertNotEqual(project.id, "earlier")

    def test_the_root_is_not_collapsed_to_the_repository(self):
        # A council run resolves into a repo root; a project must not. It lays
        # out its own tree, and silently redirecting it up a level would have
        # it build over a codebase nobody pointed at.
        sub = self.root / "nested"
        sub.mkdir()
        self.assertEqual(self.engine._resolve_root(str(sub)), str(sub.resolve()))


class TestTheLoop(unittest.TestCase):
    """The whole five-phase cycle, against a real directory.

    Slow, and the only test here that would notice if the phases stopped
    connecting to each other.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-proj-e2e-"))
        self.store = ConfigStore(self.tmp / "config.json")
        self.root = self.tmp / "build"
        self.root.mkdir()
        self.store.update({
            "providers": {r: mock_provider(r) for r in ROLES},
            "workspace": str(self.root),
            "project": {"max_steps": 20, "max_fix_attempts": 3,
                        "expansion_rounds": 1},
        })
        self.bus = EventBus()
        self.engine = ProjectEngine(self.store, self.bus)

    def tearDown(self):
        self.engine.stop()
        self.engine.wait_for_worker(60)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def drain(self, timeout: float = 300.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.engine.is_running():
                return
            time.sleep(0.25)
        self.fail("The project never finished.")

    def test_a_project_runs_from_brief_to_readme(self):
        self.engine.start("A greeting module in Python.", str(self.root))
        self.drain()

        project = self.engine.project
        self.assertEqual(project.status, COMPLETED, project.error)

        # Every phase left its artefact behind, and they are real files.
        self.assertTrue((self.root / "SPEC.md").exists())
        self.assertTrue((self.root / "greeter.py").exists())
        self.assertTrue((self.root / "test_greeter.py").exists())
        self.assertTrue((self.root / "README.md").exists())

        # The build genuinely failed once and was genuinely fixed. The mock's
        # first implementation drops the name from the greeting, so its own
        # test suite really fails; QA really runs it, really captures the
        # trace, and the developer is really handed it back.
        self.assertEqual(project.build_status, "passing")
        self.assertIn(
            "{name}", (self.root / "greeter.py").read_text(encoding="utf-8")
        )
        headings = [s.heading for s in project.steps]
        self.assertIn("Fix the build", headings)
        # And the counter was reset once it passed, so the next failure starts
        # from a full budget rather than from a spent one.
        self.assertEqual(project.fix_attempts, 0)

        # The expansion round happened and its task is on the list.
        self.assertIn("task_3", {t["id"] for t in project.tasks})

        # All five phases were visited.
        self.assertEqual(
            {s.phase for s in project.steps},
            {1, 2, 3, 4, 5},
        )

    def test_the_state_file_tracks_the_run(self):
        self.engine.start("A greeting module in Python.", str(self.root))
        self.drain()

        state = Workspace(self.root).read_state()
        self.assertEqual(state["status"], COMPLETED)
        self.assertEqual(state["build_status"], "passing")
        self.assertFalse(state["continuation_token_needed"])
        self.assertTrue(state["tasks"])
        # Written as ISO-8601, which is what the schema asks for.
        self.assertRegex(state["last_run_timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:")

    def test_the_critique_log_accumulates(self):
        self.engine.start("A greeting module in Python.", str(self.root))
        self.drain()
        log = Workspace(self.root).critique_path.read_text(encoding="utf-8")
        self.assertIn("QA", log)
        self.assertIn("Project completed", log)

    def test_events_narrate_the_run(self):
        seen = []
        q = self.bus.subscribe()
        self.engine.start("A greeting module in Python.", str(self.root))
        self.drain()
        while not q.empty():
            seen.append(q.get_nowait()["kind"])
        for kind in ("project_started", "project_step", "project_step_done",
                     "project_state", "project_log"):
            self.assertIn(kind, seen)

    def test_stopping_ends_the_run(self):
        self.engine.start("A greeting module in Python.", str(self.root))
        # Let the first agent actually get going, so this exercises killing a
        # child process rather than a race with the thread start.
        time.sleep(1.0)
        self.engine.stop()
        self.assertTrue(self.engine.wait_for_worker(60))
        self.assertEqual(self.engine.project.status, FAILED)
        self.assertIn("Stopped", self.engine.project.error)

    def test_a_stopped_project_can_be_resumed(self):
        self.engine.start("A greeting module in Python.", str(self.root))
        time.sleep(1.0)
        self.engine.stop()
        self.engine.wait_for_worker(60)

        # A stopped project is FAILED, which is terminal - resuming it is a new
        # decision, so the operator starts it again rather than continuing a
        # run the engine had already written off.
        Workspace(self.root).write_state({
            **Workspace(self.root).read_state(), "status": PLANNING,
        })
        project = self.engine.start("", str(self.root), resume=True)
        self.assertEqual(project.brief, "A greeting module in Python.")
        self.engine.stop()
        self.engine.wait_for_worker(60)

    def test_pausing_halts_between_steps(self):
        self.engine.start("A greeting module in Python.", str(self.root))
        self.engine.pause()
        self.assertTrue(self.engine.project.paused)
        # The step in flight is allowed to finish - killing a CLI between two
        # file writes leaves a tree nothing in the run knows is half-written.
        deadline = time.time() + 180
        while time.time() < deadline:
            if self.engine.project.steps and all(
                s.state != "running" for s in self.engine.project.steps
            ):
                break
            time.sleep(0.25)
        self.assertTrue(self.engine.project.paused)
        self.assertNotIn(self.engine.project.status, (COMPLETED, FAILED))
        self.engine.stop()
        self.engine.wait_for_worker(60)

    def test_a_hand_off_moves_the_next_step(self):
        self.engine.start("A greeting module in Python.", str(self.root))
        self.engine.handoff("qa")
        self.drain()
        # Somewhere in the run a step ran on the QA chair having been forced
        # there, rather than because its phase called for it.
        self.assertTrue(
            any(s.role == "qa" and s.handoff_from for s in self.engine.project.steps)
            or any(s.phase == PHASE_ARCHITECTURE and s.role == "qa"
                   for s in self.engine.project.steps)
        )

    def test_the_step_limit_stops_a_project_that_cannot_finish(self):
        self.store.update({"project": {"max_steps": 2}})
        self.engine.start("A greeting module in Python.", str(self.root))
        self.drain()
        project = self.engine.project
        self.assertEqual(project.status, FAILED)
        self.assertIn("step limit", project.error)
        self.assertLessEqual(project.steps_used, 3)


class TestPhaseOne(unittest.TestCase):
    """Phase 1 is judged on what is on disk, not on an exit status."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-proj-"))
        self.store = ConfigStore(self.tmp / "config.json")
        self.root = self.tmp / "build"
        self.root.mkdir()
        self.engine = ProjectEngine(self.store, EventBus())
        self.project = Project(
            id="p1", brief="build a thing", workspace=str(self.root)
        )
        self.project.config = self.store.all()
        Workspace(self.root).ensure()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub_step(self, ok, report):
        """Stand in for one agent turn, without launching one."""
        step = StepRecord(index=1, phase=1, role="architect", label="stub",
                          heading="Design the system")
        step.ok = ok
        step.error = "" if ok else "Exited with status 1."
        step.state = "done" if ok else "failed"
        self.engine._run_role = lambda *a, **k: (step, report)
        return step

    def test_a_failed_architect_that_left_a_plan_carries_on(self):
        # A CLI that wrote the spec and the roadmap and *then* fell over has
        # done phase 1. Failing on the exit code would throw that away and make
        # the operator start again from a folder that already has the answer.
        Workspace(self.root).spec_path.write_text("# Spec\n", encoding="utf-8")
        self.stub_step(False, {"tasks": [{"id": "task_1", "description": "go"}]})
        self.engine._phase_architecture(self.project)
        self.assertEqual(self.project.phase, PHASE_IMPLEMENTATION)
        self.assertEqual(self.project.status, IMPLEMENTING)
        self.assertEqual([t["id"] for t in self.project.tasks], ["task_1"])

    def test_an_architect_that_left_nothing_ends_the_project(self):
        self.stub_step(False, {})
        self.engine._phase_architecture(self.project)
        self.assertEqual(self.project.status, FAILED)
        self.assertIn("nothing for the developer to build", self.project.error)

    def test_a_spec_with_no_task_list_is_enough_to_continue(self):
        # The developer can work from SPEC.md; an empty task list is thin, not
        # fatal.
        Workspace(self.root).spec_path.write_text("# Spec\n", encoding="utf-8")
        self.stub_step(True, {})
        self.engine._phase_architecture(self.project)
        self.assertEqual(self.project.phase, PHASE_IMPLEMENTATION)


class TestSnapshotState(unittest.TestCase):
    """What the Projects tab is served."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-proj-"))
        self.store = ConfigStore(self.tmp / "config.json")
        self.root = self.tmp / "build"
        self.root.mkdir()
        self.engine = ProjectEngine(self.store, EventBus())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_empty_folder_offers_nothing(self):
        state = self.engine.snapshot_state(str(self.root))
        self.assertIsNone(state["project"])
        self.assertFalse(state["resumable"])

    def test_a_project_left_on_disk_is_found(self):
        # The app was closed mid-build. Presenting an empty form over the top
        # of a half-built codebase would be the wrong answer.
        ws = Workspace(self.root)
        ws.ensure()
        ws.write_state({
            "project_id": "abc", "status": IMPLEMENTING, "current_phase": 2,
            "brief": "a half-built thing", "tasks": [],
        })
        state = self.engine.snapshot_state(str(self.root))
        self.assertTrue(state["found_on_disk"])
        self.assertTrue(state["resumable"])
        self.assertEqual(state["project"]["brief"], "a half-built thing")

    def test_a_finished_project_is_not_resumable(self):
        ws = Workspace(self.root)
        ws.ensure()
        ws.write_state({"project_id": "abc", "status": COMPLETED, "tasks": []})
        state = self.engine.snapshot_state(str(self.root))
        self.assertFalse(state["resumable"])

    def test_a_finished_project_does_not_follow_you_to_another_folder(self):
        # The tab reports on the folder it is asked about. A project left in
        # memory from the last build would otherwise be shown over the top of
        # a folder that has nothing to do with it.
        other = self.tmp / "elsewhere"
        other.mkdir()
        self.engine._project = Project(
            id="abc", brief="an earlier build", workspace=str(self.root),
            status=COMPLETED,
        )
        here = self.engine.snapshot_state(str(self.root))
        self.assertEqual(here["project"]["id"], "abc")
        there = self.engine.snapshot_state(str(other))
        self.assertIsNone(there["project"])


if __name__ == "__main__":
    unittest.main()
