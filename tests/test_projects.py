"""Tests for the Projects engine.

Three kinds here, deliberately separated:

* Unit tests over the parts that decide things - report parsing, card merging,
  tooling detection, the board file. These run in milliseconds and are where
  the edge cases live.

* Tests of the decision engine itself, driven by handing it a board and asking
  what it would do next. No agent is launched, so the ordering policy - a
  failing build outranks a review, a review outranks new work - is pinned
  directly rather than inferred from a run.

* One end-to-end test that drives the whole loop against a real directory using
  ``scripts/mock-agent.py``. It writes real files, runs a real test suite that
  really fails, and gets handed the real trace. That one is slow and worth it:
  every unit test above it would still pass if the turns never actually
  connected to each other.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil import gitutil  # noqa: E402
from aicouncil.config import ConfigStore  # noqa: E402
from aicouncil.events import EventBus  # noqa: E402
from aicouncil.projects import (  # noqa: E402
    COL_BACKLOG,
    COL_DONE,
    COL_IN_PROGRESS,
    COL_IN_REVIEW,
    COMPLETED,
    FAILED,
    HEALTH_FAILING,
    HEALTH_PASSING,
    HEALTH_UNKNOWN,
    KIND_BUG,
    ORIGIN_INNOVATION,
    ROLES,
    STALL_LIMIT,
    Project,
    ProjectEngine,
    StepRecord,
    Workspace,
    detect_tooling,
    ensure_gitignored,
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


def card(cid, column=COL_BACKLOG, kind="task", **extra) -> dict:
    base = {
        "id": cid, "title": cid, "detail": "", "column": column, "kind": kind,
        "assigned_to": "coder", "origin": "goal", "note": "",
    }
    base.update(extra)
    return base


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

    def test_a_reply_that_is_only_the_object_is_read(self):
        # An agent told to end on a JSON block and nothing else sometimes
        # sends exactly that, with no prose in front of it.
        self.assertEqual(parse_report('{"status": "blocked"}')["status"], "blocked")

    def test_a_board_only_report_is_read(self):
        # A QA turn reports a build and nothing else; a reviewer reports
        # verdicts and nothing else. Neither carries `status`, and both are
        # real reports.
        self.assertIn("build", parse_report('```json\n{"build": {"health": "PASSING"}}\n```'))
        self.assertIn("reviews", parse_report('```json\n{"reviews": []}\n```'))

    def test_junk_is_not_a_report(self):
        # Never raises, and never invents. An unparseable reply means "it did
        # not tell me", which the engine treats very differently from failure.
        for text in ("", "no json here", "```json\n{not json}\n```", "```\n[]\n```"):
            self.assertEqual(parse_report(text), {})

    def test_a_bare_list_is_not_a_report(self):
        self.assertEqual(parse_report('```json\n["a", "b"]\n```'), {})


class TestCardMerging(unittest.TestCase):
    def test_a_new_id_is_appended(self):
        out = merge_tasks([], [{"id": "t1", "title": "do it"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["column"], COL_BACKLOG)
        self.assertEqual(out[0]["assigned_to"], "coder")

    def test_a_move_only_report_keeps_the_title(self):
        out = merge_tasks(
            [card("t1", title="write the parser", detail="in parse.py")],
            [{"id": "t1", "column": COL_DONE}],
        )
        self.assertEqual(out[0]["column"], COL_DONE)
        self.assertEqual(out[0]["title"], "write the parser")
        self.assertEqual(out[0]["detail"], "in parse.py")

    def test_omission_is_not_deletion(self):
        # An agent that forgets a card has said nothing about it. Reading
        # silence as a delete would let one careless reply wipe the board.
        out = merge_tasks(
            [card("t1"), card("t2"), card("t3")],
            [{"id": "t2", "column": COL_DONE}],
        )
        self.assertEqual([c["id"] for c in out], ["t1", "t2", "t3"])
        self.assertEqual([c["column"] for c in out],
                         [COL_BACKLOG, COL_DONE, COL_BACKLOG])

    def test_an_unknown_column_falls_back_to_backlog(self):
        out = merge_tasks([], [{"id": "t1", "column": "shipped"}])
        self.assertEqual(out[0]["column"], COL_BACKLOG)

    def test_entries_without_an_id_are_dropped(self):
        self.assertEqual(merge_tasks([], [{"title": "no id"}, "nonsense", 7]), [])

    def test_a_bug_keeps_its_kind(self):
        out = merge_tasks([], [{"id": "b1", "title": "npe", "kind": "bug"}])
        self.assertEqual(out[0]["kind"], KIND_BUG)
        self.assertEqual(out[0]["origin"], "bug")


class TestToolingDetection(unittest.TestCase):
    """Adopting the project's own commands instead of inventing a build."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-tool-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_empty_folder_has_no_tooling(self):
        found = detect_tooling(self.tmp)
        self.assertEqual(found["commands"], [])
        self.assertEqual(found["stack"], [])

    def test_go_is_detected_from_go_mod(self):
        (self.tmp / "go.mod").write_text("module x\n", encoding="utf-8")
        found = detect_tooling(self.tmp)
        self.assertEqual(found["stack"], ["Go"])
        self.assertIn("go test ./...", found["commands"])

    def test_node_scripts_are_read_not_guessed(self):
        # `npm test` against a package with no test script fails in a way that
        # looks exactly like a broken build, so only declared scripts are used.
        (self.tmp / "package.json").write_text(
            json.dumps({"scripts": {"build": "tsc", "lint": "eslint ."}}),
            encoding="utf-8",
        )
        found = detect_tooling(self.tmp)
        self.assertIn("npm run build", found["commands"])
        self.assertIn("npm run lint", found["commands"])
        self.assertNotIn("npm test", found["commands"])

    def test_a_corrupt_package_json_does_not_raise(self):
        (self.tmp / "package.json").write_text("{not json", encoding="utf-8")
        found = detect_tooling(self.tmp)
        self.assertEqual(found["stack"], ["Node"])
        self.assertEqual(found["commands"], [])

    def test_several_markers_all_contribute(self):
        (self.tmp / "go.mod").write_text("module x\n", encoding="utf-8")
        (self.tmp / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
        found = detect_tooling(self.tmp)
        self.assertEqual(found["stack"], ["Go", "Make"])
        self.assertIn("make", found["commands"])


class TestGitignoreSafety(unittest.TestCase):
    """`.theseus/` must not turn up in somebody's pull request."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-ignore-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git_init(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)

    def test_a_non_repository_is_left_alone(self):
        self.assertEqual(ensure_gitignored(self.tmp), "")
        self.assertFalse((self.tmp / ".gitignore").exists())

    def test_it_is_added_to_a_repository(self):
        self.git_init()
        self.assertIn(".theseus", ensure_gitignored(self.tmp))
        self.assertIn("/.theseus/", (self.tmp / ".gitignore").read_text())

    def test_an_existing_gitignore_is_appended_to_not_replaced(self):
        self.git_init()
        (self.tmp / ".gitignore").write_text("*.pyc\nnode_modules/\n", encoding="utf-8")
        ensure_gitignored(self.tmp)
        text = (self.tmp / ".gitignore").read_text()
        self.assertIn("*.pyc", text)
        self.assertIn("node_modules/", text)
        self.assertIn("/.theseus/", text)

    def test_it_is_not_added_twice(self):
        self.git_init()
        ensure_gitignored(self.tmp)
        self.assertEqual(ensure_gitignored(self.tmp), "")
        text = (self.tmp / ".gitignore").read_text()
        self.assertEqual(text.count(".theseus"), 1)

    def test_an_already_ignored_project_is_recognised(self):
        self.git_init()
        (self.tmp / ".gitignore").write_text(".theseus\n", encoding="utf-8")
        self.assertEqual(ensure_gitignored(self.tmp), "")


class TestBoardFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-board-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ensure_seeds_the_log(self):
        Workspace(self.tmp).ensure()
        self.assertTrue((self.tmp / ".theseus" / "CRITIQUE.log").exists())

    def test_ensure_does_not_overwrite(self):
        ws = Workspace(self.tmp)
        ws.ensure()
        ws.critique_path.write_text("mine\n", encoding="utf-8")
        ws.ensure()
        self.assertEqual(ws.critique_path.read_text(), "mine\n")

    def test_the_columns_are_all_written(self):
        project = Project(id="p1", goal="g", workspace=str(self.tmp))
        project.tasks = [card("t1", COL_DONE), card("t2", COL_BACKLOG)]
        doc = project.board_document()
        self.assertEqual(
            sorted(doc["columns"]),
            ["backlog", "done", "in_progress", "in_review"],
        )
        self.assertEqual([c["id"] for c in doc["columns"]["done"]], ["t1"])
        # The column is the key, so carrying it inside the card too would give
        # the file two places to disagree with itself.
        self.assertNotIn("column", doc["columns"]["done"][0])

    def test_a_corrupt_board_reads_as_absent(self):
        ws = Workspace(self.tmp)
        ws.ensure()
        ws.board_path.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(ws.read_board(), {})

    def test_a_project_round_trips_through_disk(self):
        ws = Workspace(self.tmp)
        ws.ensure()
        original = Project(id="p1", goal="build it", workspace=str(self.tmp))
        original.tasks = [card("t1", COL_IN_REVIEW), card("b1", COL_BACKLOG, KIND_BUG)]
        original.build_health = HEALTH_FAILING
        original.last_build_log = "boom"
        original.innovation_rounds = 3
        ws.write_board(original.board_document())

        back = Project.from_board(str(self.tmp), ws.read_board())
        self.assertEqual(back.goal, "build it")
        self.assertEqual(back.build_health, HEALTH_FAILING)
        self.assertEqual(back.last_build_log, "boom")
        self.assertEqual(back.innovation_rounds, 3)
        # Cards come back grouped by column, because that is how the file
        # stores them - insertion order across columns is not preserved, and
        # order *within* a column, which is what claiming reads, is.
        self.assertEqual(sorted(c["id"] for c in back.tasks), ["b1", "t1"])
        self.assertEqual(back.column(COL_IN_REVIEW)[0]["id"], "t1")
        self.assertEqual(back.column(COL_BACKLOG)[0]["kind"], KIND_BUG)

    def test_the_safety_snapshot_comes_back_with_the_board(self):
        # The one piece of state whose loss cannot be repaired by running the
        # project again: it points at the tree as it was before the first agent
        # wrote anything.
        ws = Workspace(self.tmp)
        ws.ensure()
        original = Project(id="p1", goal="build it", workspace=str(self.tmp))
        original.snapshot = gitutil.Snapshot(
            root=str(self.tmp), head="a" * 40, commit="b" * 40,
            had_changes=True, ref="refs/ai-council/snapshots/p1",
        )
        ws.write_board(original.board_document())

        back = Project.from_board(str(self.tmp), ws.read_board())
        self.assertEqual(back.snapshot.commit, "b" * 40)
        self.assertEqual(back.snapshot.ref, "refs/ai-council/snapshots/p1")
        self.assertTrue(back.snapshot.had_changes)

    def test_a_snapshot_with_no_commit_restores_nothing_and_is_read_as_absent(self):
        back = Project.from_board(
            str(self.tmp), {"snapshot": {"root": "/x", "head": "a" * 40}}
        )
        self.assertIsNone(back.snapshot)

    def test_a_mangled_health_resumes_as_unknown_never_passing(self):
        # The one field that must never fail open. A board somebody hand-edited
        # into nonsense resumes as unverified, not as green.
        back = Project.from_board(str(self.tmp), {"build_health": "probably fine"})
        self.assertEqual(back.build_health, HEALTH_UNKNOWN)

    def test_critique_is_append_only(self):
        ws = Workspace(self.tmp)
        ws.ensure()
        ws.append_critique("first")
        ws.append_critique("second")
        text = ws.critique_path.read_text()
        self.assertLess(text.index("first"), text.index("second"))


class TestExhaustionDetection(unittest.TestCase):
    """Deciding a failure was "no room left" rather than "no"."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-exh-"))
        self.engine = ProjectEngine(ConfigStore(self.tmp / "c.json"), EventBus())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def result(self, **kw) -> ProviderResult:
        base = dict(
            provider_id="qa", ok=False, stdout="", stderr="", error="",
            command=[], duration=1.0, timed_out=False, cancelled=False,
            exit_code=1,
        )
        base.update(kw)
        return ProviderResult(**base)

    def test_a_context_limit_is_exhaustion(self):
        for text in (
            "Error: prompt is too long",
            "context window exceeded",
            "usage limit reached, try again at 4pm",
            "quota exhausted",
        ):
            self.assertTrue(
                self.engine._looks_exhausted(self.result(stderr=text)), text
            )

    def test_a_timeout_is_exhaustion(self):
        self.assertTrue(self.engine._looks_exhausted(self.result(timed_out=True)))

    def test_an_ordinary_failure_is_not(self):
        for text in ("SyntaxError: invalid syntax", "test failed", ""):
            self.assertFalse(
                self.engine._looks_exhausted(self.result(stderr=text)), text
            )


class TestDecisionEngine(unittest.TestCase):
    """What the board says to do next. No agents are launched here.

    This is the policy, stated once: a failing build outranks everything, an
    unverified one outranks a review, a review outranks starting more work, and
    only an empty board with a green build gets to invent anything.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-decide-"))
        self.store = ConfigStore(self.tmp / "config.json")
        self.root = self.tmp / "build"
        self.root.mkdir()
        Workspace(self.root).ensure()
        self.engine = ProjectEngine(self.store, EventBus())
        self.project = Project(id="p1", goal="build it", workspace=str(self.root))
        self.project.config = self.store.all()
        self.project.audited = True

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def decide(self):
        return self.engine._decide(self.project)

    def test_the_first_turn_is_a_read_only_audit(self):
        self.project.audited = False
        d = self.decide()
        self.assertEqual((d.role, d.kind), ("qa", "audit"))
        # The whole point of it being first: nothing has written yet, and this
        # turn is not allowed to either.
        self.assertTrue(d.read_only)

    def test_an_empty_board_is_planned(self):
        d = self.decide()
        self.assertEqual((d.role, d.kind), ("architect", "plan"))

    def test_a_failing_build_outranks_everything(self):
        self.project.tasks = [card("t1", COL_IN_REVIEW), card("t2", COL_BACKLOG)]
        self.project.build_health = HEALTH_FAILING
        d = self.decide()
        self.assertEqual((d.role, d.kind), ("coder", "fix"))

    def test_an_unverified_build_outranks_a_review(self):
        # Reviewing code nobody has compiled wastes the reviewer's turn and
        # half the time reviews something that does not build.
        self.project.tasks = [card("t1", COL_IN_REVIEW)]
        self.project.needs_verification = True
        d = self.decide()
        self.assertEqual((d.role, d.kind), ("qa", "verify"))

    def test_a_review_outranks_new_work(self):
        self.project.tasks = [card("t1", COL_IN_REVIEW), card("t2", COL_BACKLOG)]
        self.project.build_health = HEALTH_PASSING
        d = self.decide()
        self.assertEqual((d.role, d.kind), ("architect", "review"))
        self.assertEqual([c["id"] for c in d.tasks], ["t1"])

    def test_the_backlog_is_worked_when_nothing_else_is_pending(self):
        self.project.tasks = [card("t1"), card("t2")]
        self.project.build_health = HEALTH_PASSING
        d = self.decide()
        self.assertEqual((d.role, d.kind), ("coder", "implement"))
        self.assertEqual(d.tasks[0]["id"], "t1")

    def test_a_bug_jumps_the_queue(self):
        self.project.tasks = [card("t1"), card("b1", kind=KIND_BUG)]
        self.project.build_health = HEALTH_PASSING
        d = self.decide()
        self.assertEqual(d.tasks[0]["id"], "b1")

    def test_a_card_left_in_progress_is_finished_before_a_new_one(self):
        # A turn that died holding a card must not have that card duplicated by
        # the next developer turn.
        self.project.tasks = [card("t1"), card("t2", COL_IN_PROGRESS)]
        self.project.build_health = HEALTH_PASSING
        d = self.decide()
        self.assertEqual(d.tasks[0]["id"], "t2")

    def test_a_clear_green_board_innovates(self):
        self.project.tasks = [card("t1", COL_DONE)]
        self.project.build_health = HEALTH_PASSING
        self.project.innovation_rounds = 1
        d = self.decide()
        self.assertEqual((d.role, d.kind), ("architect", "innovate"))

    def test_a_clear_green_board_with_no_budget_is_finished(self):
        self.project.tasks = [card("t1", COL_DONE)]
        self.project.build_health = HEALTH_PASSING
        self.project.innovation_rounds = 0
        self.assertIsNone(self.decide())

    def test_endless_fixing_is_bounded(self):
        self.project.tasks = [card("t1")]
        self.project.build_health = HEALTH_FAILING
        self.project.fix_attempts = 3  # the configured max
        self.assertIsNone(self.decide())
        self.assertEqual(self.project.status, FAILED)
        self.assertIn("failed 3 times", self.project.error)

    def test_giving_up_is_not_relabelled_as_finishing(self):
        # Every "nothing left to schedule" looks the same to the loop, which
        # reads it as success - so the run that gave up on a red build used to
        # be overwritten by COMPLETED one line later. The first ending wins.
        self.project.tasks = [card("t1")]
        self.project.build_health = HEALTH_FAILING
        self.project.fix_attempts = 3
        self.assertIsNone(self.decide())
        self.engine._complete(self.project)
        self.assertEqual(self.project.status, FAILED)
        self.assertIn("failed 3 times", self.project.error)
        self.assertEqual(self.project.note, "")


class TestCavemanModeInProjects(unittest.TestCase):
    """Projects has its own switch, read live on every turn."""

    MARK = "ULTRA-LOW TOKEN EFFICIENCY MODE"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-caveman-"))
        self.store = ConfigStore(self.tmp / "config.json")
        self.root = self.tmp / "build"
        self.root.mkdir()
        Workspace(self.root).ensure()
        self.engine = ProjectEngine(self.store, EventBus())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def context(self):
        project = Project(id="p1", goal="build it", workspace=str(self.root))
        project.config = self.store.all()
        return self.engine._context(project, "coder")

    def test_a_turn_says_nothing_about_style_by_default(self):
        self.assertNotIn(self.MARK, self.context())

    def test_switching_it_on_reaches_every_project_turn(self):
        self.store.update({"caveman": {"project": True}})
        self.assertIn(self.MARK, self.context())

    def test_the_councils_switch_does_not_move_projects(self):
        # Three switches, not one with three labels.
        self.store.update({"caveman": {"council": True, "project": False}})
        self.assertNotIn(self.MARK, self.context())


class TestEfficiencyModeInProjects(unittest.TestCase):
    """Projects reads its independent Efficiency switch, live, on every turn."""

    MARK = "[SYSTEM INSTRUCTION: EFFICIENCY MODE]"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-efficiency-"))
        self.store = ConfigStore(self.tmp / "config.json")
        self.root = self.tmp / "build"
        self.root.mkdir()
        Workspace(self.root).ensure()
        self.engine = ProjectEngine(self.store, EventBus())

    def tearDown(self):
        self.engine.stop()
        self.engine.wait_for_worker(30)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def context(self):
        project = Project(id="p1", goal="build it", workspace=str(self.root))
        project.config = self.store.all()
        return self.engine._context(project, "coder")

    def test_a_turn_says_nothing_about_efficiency_by_default(self):
        self.assertNotIn(self.MARK, self.context())

    def test_switching_it_on_reaches_every_project_turn(self):
        self.store.update({"efficiency": {"project": True}})
        for role in ROLES:
            with self.subTest(role=role):
                project = Project(id="p1", goal="g", workspace=str(self.root))
                project.config = self.store.all()
                self.assertIn(self.MARK, self.engine._context(project, role))

    def test_chat_switch_does_not_change_projects(self):
        self.store.update({
            "efficiency": {"chat": True, "project": False},
        })
        self.assertNotIn(self.MARK, self.context())

    def test_switching_it_on_mid_project_reaches_the_next_turn(self):
        # The gear that sets this sits in the tracker header of a *running*
        # project, and `start()` pins a deep copy of the config onto it. Read
        # off that snapshot the tick came on and nothing else did, for every
        # remaining turn of a run that can last hours.
        self.store.update({"providers": {r: mock_provider(r) for r in ROLES}})
        project = self.engine.start("build it", str(self.root))
        self.engine.stop()
        self.engine.wait_for_worker(30)

        self.assertNotIn(self.MARK, self.engine._context(project, "coder"))
        self.store.update({"efficiency": {"project": True}})
        self.assertIn(self.MARK, self.engine._context(project, "coder"))

    def test_the_rest_of_the_config_stays_frozen_at_start(self):
        # Only the style switches are live. Changing the CLI or the house rules
        # halfway through is a different run, not a restyled one.
        self.store.update({
            "providers": {r: mock_provider(r) for r in ROLES},
            "house_rules": "Original rules.",
        })
        project = self.engine.start("build it", str(self.root))
        self.engine.stop()
        self.engine.wait_for_worker(30)

        self.store.update({"house_rules": "Replaced mid-run."})
        context = self.engine._context(project, "coder")
        self.assertIn("Original rules.", context)
        self.assertNotIn("Replaced mid-run.", context)


class TestApplyingReports(unittest.TestCase):
    """Folding one agent's answer back onto the board."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-apply-"))
        self.store = ConfigStore(self.tmp / "config.json")
        self.root = self.tmp / "build"
        self.root.mkdir()
        Workspace(self.root).ensure()
        self.engine = ProjectEngine(self.store, EventBus())
        self.project = Project(id="p1", goal="build it", workspace=str(self.root))
        self.project.config = self.store.all()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def step(self, role="qa", ok=True, reported="", files=()) -> StepRecord:
        s = StepRecord(index=1, role=role, label="stub", heading="h", trigger="t")
        s.ok = ok
        s.error = "" if ok else "Exited with status 1."
        s.reported_status = reported
        s.files_modified = list(files)
        return s

    # -- verification is fail-closed ---------------------------------------

    def test_a_passing_verdict_is_taken(self):
        self.engine._apply_verify(
            self.project, None, self.step(),
            {"build": {"health": "PASSING", "log": ""}},
        )
        self.assertEqual(self.project.build_health, HEALTH_PASSING)
        self.assertFalse(self.project.needs_verification)

    def test_no_verdict_is_failing_never_passing(self):
        # The one place the engine must not give an agent the benefit of the
        # doubt: everything downstream reads PASSING as "somebody ran the
        # tests", so a silent QA turn cannot be allowed to mean that.
        self.engine._apply_verify(self.project, None, self.step(), {})
        self.assertEqual(self.project.build_health, HEALTH_FAILING)
        self.assertIn("without reporting a build status", self.project.last_build_log)

    def test_a_verification_that_never_ran_is_not_a_failing_build(self):
        # The other half of fail-closed, and the half that used to be wrong: a
        # QA turn that crashed has tested nothing. Recording it as FAILING
        # hands the developer the app's own error message to fix, which it
        # cannot, and spends the whole fix budget finding that out.
        self.project.needs_verification = True
        self.engine._apply_verify(self.project, None, self.step(ok=False), {})
        self.assertEqual(self.project.build_health, HEALTH_UNKNOWN)
        self.assertTrue(self.project.needs_verification)
        self.assertEqual(self.project.last_build_log, "")

    def test_a_crashed_verification_that_still_reported_is_taken_at_its_word(self):
        # Partial output is output. If the trace made it out before the CLI
        # fell over, that trace is a real build failure.
        self.engine._apply_verify(
            self.project, None, self.step(ok=False),
            {"build": {"health": "FAILING", "log": "3 tests failed"}},
        )
        self.assertEqual(self.project.build_health, HEALTH_FAILING)
        self.assertIn("3 tests failed", self.project.last_build_log)

    def test_an_unrecognised_verdict_is_failing(self):
        self.engine._apply_verify(
            self.project, None, self.step(), {"build": {"health": "probably fine"}}
        )
        self.assertEqual(self.project.build_health, HEALTH_FAILING)

    def test_a_passing_verdict_resets_the_fix_budget(self):
        self.project.fix_attempts = 2
        self.engine._apply_verify(
            self.project, None, self.step(), {"build": {"health": "PASSING"}}
        )
        self.assertEqual(self.project.fix_attempts, 0)

    def test_qa_may_raise_bugs(self):
        self.engine._apply_verify(
            self.project, None, self.step(),
            {"build": {"health": "FAILING", "log": "npe"},
             "tasks": [{"id": "b1", "title": "null deref", "kind": "bug"}]},
        )
        self.assertEqual(self.project.column(COL_BACKLOG)[0]["kind"], KIND_BUG)

    # -- writing code invalidates a green build ----------------------------

    def test_writing_code_makes_the_build_unverified_again(self):
        self.project.build_health = HEALTH_PASSING
        self.project.tasks = [card("t1", COL_IN_PROGRESS)]
        self.engine._apply_implement(
            self.project, _decision([card("t1", COL_IN_PROGRESS)]),
            self.step("coder"), {"files_modified": ["a.py"]},
        )
        self.assertEqual(self.project.build_health, HEALTH_UNKNOWN)
        self.assertTrue(self.project.needs_verification)

    def test_a_clean_implement_turn_puts_its_card_up_for_review(self):
        self.project.tasks = [card("t1", COL_IN_PROGRESS)]
        self.engine._apply_implement(
            self.project, _decision([card("t1", COL_IN_PROGRESS)]),
            self.step("coder"), {},
        )
        self.assertEqual(self.project.column(COL_IN_REVIEW)[0]["id"], "t1")

    def test_a_failed_implement_turn_leaves_its_card_in_progress(self):
        self.project.tasks = [card("t1", COL_IN_PROGRESS)]
        self.engine._apply_implement(
            self.project, _decision([card("t1", COL_IN_PROGRESS)]),
            self.step("coder", ok=False), {},
        )
        self.assertEqual(self.project.column(COL_IN_PROGRESS)[0]["id"], "t1")

    def test_a_developer_that_says_it_is_blocked_keeps_its_card(self):
        # Exit zero is not the same as done. The contract asks for `blocked`
        # when an agent could not proceed, and sending that card to review
        # asks the architect to read a diff that was never written.
        self.project.build_health = HEALTH_PASSING
        self.project.tasks = [card("t1", COL_IN_PROGRESS)]
        self.engine._apply_implement(
            self.project, _decision([card("t1", COL_IN_PROGRESS)]),
            self.step("coder", reported="blocked"), {"status": "blocked"},
        )
        self.assertEqual(self.project.column(COL_IN_PROGRESS)[0]["id"], "t1")
        # Nothing was written, so nothing needs retesting either.
        self.assertEqual(self.project.build_health, HEALTH_PASSING)
        self.assertFalse(self.project.needs_verification)

    def test_a_blocked_turn_that_still_wrote_files_invalidates_the_build(self):
        self.project.build_health = HEALTH_PASSING
        self.project.tasks = [card("t1", COL_IN_PROGRESS)]
        self.engine._apply_implement(
            self.project, _decision([card("t1", COL_IN_PROGRESS)]),
            self.step("coder", reported="blocked", files=["a.py"]),
            {"status": "blocked"},
        )
        self.assertEqual(self.project.build_health, HEALTH_UNKNOWN)
        self.assertTrue(self.project.needs_verification)

    # -- review --------------------------------------------------------------

    def test_approval_moves_a_card_to_done(self):
        self.project.tasks = [card("t1", COL_IN_REVIEW)]
        self.engine._apply_review(
            self.project, _decision([card("t1", COL_IN_REVIEW)]),
            self.step("architect"),
            {"reviews": [{"id": "t1", "verdict": "approve"}]},
        )
        self.assertEqual(self.project.column(COL_DONE)[0]["id"], "t1")

    def test_changes_send_a_card_back_carrying_the_note(self):
        self.project.tasks = [card("t1", COL_IN_REVIEW)]
        self.engine._apply_review(
            self.project, _decision([card("t1", COL_IN_REVIEW)]),
            self.step("architect"),
            {"reviews": [{"id": "t1", "verdict": "changes", "note": "stubbed out"}]},
        )
        back = self.project.column(COL_BACKLOG)[0]
        self.assertEqual(back["id"], "t1")
        self.assertEqual(back["note"], "stubbed out")

    def test_bouncing_a_card_does_not_retest_an_untouched_tree(self):
        # A review moves cards; it does not write code. Marking the build
        # unverified here spends a QA turn rebuilding a tree nothing has
        # touched since it last passed - once per bounced card, before the
        # developer has changed a line.
        self.project.build_health = HEALTH_PASSING
        self.project.tasks = [card("t1", COL_IN_REVIEW)]
        self.engine._apply_review(
            self.project, _decision([card("t1", COL_IN_REVIEW)]),
            self.step("architect"),
            {"reviews": [{"id": "t1", "verdict": "changes", "note": "no tests"}]},
        )
        self.assertEqual(self.project.column(COL_BACKLOG)[0]["id"], "t1")
        self.assertEqual(self.project.build_health, HEALTH_PASSING)
        self.assertFalse(self.project.needs_verification)

    def test_a_reviewer_that_edited_something_does_invalidate_the_build(self):
        # It can: every turn after the audit carries its CLI's write grant.
        self.project.build_health = HEALTH_PASSING
        self.project.tasks = [card("t1", COL_IN_REVIEW)]
        self.engine._apply_review(
            self.project, _decision([card("t1", COL_IN_REVIEW)]),
            self.step("architect", files=["greeter.py"]),
            {"reviews": [{"id": "t1", "verdict": "approve"}]},
        )
        self.assertEqual(self.project.build_health, HEALTH_UNKNOWN)
        self.assertTrue(self.project.needs_verification)

    def test_a_card_with_no_verdict_stays_in_review(self):
        # Not approved by default. An unreviewed card sitting in review shows
        # up as a stall, which is more honest than passing it silently.
        self.project.tasks = [card("t1", COL_IN_REVIEW)]
        self.engine._apply_review(
            self.project, _decision([card("t1", COL_IN_REVIEW)]),
            self.step("architect"), {},
        )
        self.assertEqual(self.project.column(COL_IN_REVIEW)[0]["id"], "t1")

    # -- innovation ----------------------------------------------------------

    def test_ideas_become_backlog_cards_and_spend_a_round(self):
        self.project.innovation_rounds = 2
        self.engine._apply_innovate(
            self.project, None, self.step("architect"),
            {"ideas": [{"title": "add telemetry", "detail": "in main.go"}]},
        )
        self.assertEqual(self.project.innovation_rounds, 1)
        added = self.project.column(COL_BACKLOG)[0]
        self.assertEqual(added["title"], "add telemetry")
        self.assertEqual(added["origin"], ORIGIN_INNOVATION)

    def test_proposing_nothing_ends_the_innovation_budget(self):
        # Out of ideas is a finished project, not a reason to ask again next
        # turn and every turn after it.
        self.project.innovation_rounds = 3
        self.engine._apply_innovate(self.project, None, self.step("architect"), {})
        self.assertEqual(self.project.innovation_rounds, 0)

    # -- the stall guard -----------------------------------------------------

    def test_a_board_that_stops_moving_ends_the_run(self):
        self.project.tasks = [card("t1", COL_IN_REVIEW)]
        self.project.last_fingerprint = self.project.fingerprint()
        for _ in range(STALL_LIMIT):
            self.engine._note_progress(self.project)
        self.assertEqual(self.project.status, FAILED)
        self.assertIn("has not moved", self.project.error)

    def test_progress_resets_the_stall_counter(self):
        self.project.tasks = [card("t1", COL_IN_REVIEW)]
        self.project.last_fingerprint = self.project.fingerprint()
        self.engine._note_progress(self.project)
        self.assertEqual(self.project.stall_count, 1)
        self.engine._move(self.project, "t1", COL_DONE)
        self.engine._note_progress(self.project)
        self.assertEqual(self.project.stall_count, 0)
        self.assertNotEqual(self.project.status, FAILED)


def _decision(cards):
    """A stand-in Decision carrying only what the appliers read off it."""
    from aicouncil.projects import Decision

    return Decision(
        role="coder", kind="implement", heading="h", trigger="t",
        status="IMPLEMENTING", build=lambda ctx: "", tasks=cards,
    )


class TestHandOff(unittest.TestCase):
    """Moving a turn to a different chair - the reason there are three.

    Driven with a stubbed CLI rather than through a live run: which agent gets
    turn seven depends on what turn six did, and a test that has to guess that
    is testing the mock's script rather than the hand-off.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-handoff-"))
        self.store = ConfigStore(self.tmp / "config.json")
        self.root = self.tmp / "build"
        self.root.mkdir()
        Workspace(self.root).ensure()
        self.store.update({"providers": {r: mock_provider(r) for r in ROLES}})
        self.engine = ProjectEngine(self.store, EventBus())
        self.project = Project(id="p1", goal="build it", workspace=str(self.root))
        self.project.config = self.store.all()
        self.project.status = "IMPLEMENTING"
        self.engine._project = self.project

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def result(self, ok=True, **kw) -> ProviderResult:
        base = dict(
            provider_id="x", ok=ok, exit_code=0 if ok else 1,
            stdout='```json\n{"status": "ok"}\n```', stderr="", duration=0.1,
            command=["stub"], error="" if ok else "boom",
        )
        base.update(kw)
        return ProviderResult(**base)

    def stub_invokes(self, results):
        """Return canned results in order, recording which chair each ran on."""
        self.ran = []

        def fake(project, provider, prompt, read_only):
            self.ran.append(provider.get("id"))
            return results[len(self.ran) - 1]

        self.engine._invoke = fake

    def decision(self, role="architect"):
        from aicouncil.projects import Decision

        return Decision(role=role, kind="plan", heading="h", trigger="t",
                        status="PLANNING", build=lambda ctx: "prompt")

    def test_an_operator_hand_off_forces_the_next_chair(self):
        self.stub_invokes([self.result()])
        self.engine.handoff("coder")
        step, _ = self.engine._run_role(self.project, self.decision("architect"))
        self.assertEqual(self.ran, ["coder"])
        self.assertEqual(step.role, "coder")
        self.assertEqual(step.handoff_from, "architect")

    def test_a_hand_off_is_spent_once(self):
        self.stub_invokes([self.result(), self.result()])
        self.engine.handoff("qa")
        self.engine._run_role(self.project, self.decision("architect"))
        step, _ = self.engine._run_role(self.project, self.decision("architect"))
        self.assertEqual(self.ran, ["qa", "architect"])
        self.assertEqual(step.handoff_from, "")

    def test_exhaustion_moves_the_turn_to_another_agent(self):
        # The whole reason the three chairs are three independent providers:
        # the same turn is retried on a different CLI, rebuilt from the board
        # rather than replayed from the first one's transcript.
        self.stub_invokes([
            self.result(ok=False, stderr="Error: prompt is too long"),
            self.result(),
        ])
        step, _ = self.engine._run_role(self.project, self.decision("architect"))
        self.assertEqual(len(self.ran), 2)
        self.assertNotEqual(self.ran[0], self.ran[1])
        self.assertEqual(step.handoff_from, "architect")
        self.assertEqual(self.project.steps_used, 2)
        # Cleared once somebody completed the turn, so a later resume does not
        # think it is still mid-continuation.
        self.assertFalse(self.project.continuation_needed)

    def test_an_ordinary_failure_is_not_handed_off(self):
        # A compile error is the agent's answer, not its exhaustion. Retrying
        # it on a different CLI would just spend a second agent's quota.
        self.stub_invokes([self.result(ok=False, stderr="SyntaxError")])
        self.engine._run_role(self.project, self.decision("architect"))
        self.assertEqual(self.ran, ["architect"])

    def test_exhaustion_gives_up_after_every_chair_has_tried(self):
        self.stub_invokes([
            self.result(ok=False, stderr="context window exceeded"),
            self.result(ok=False, stderr="context window exceeded"),
            self.result(ok=False, stderr="context window exceeded"),
        ])
        step, _ = self.engine._run_role(self.project, self.decision("architect"))
        self.assertEqual(len(self.ran), 3)
        self.assertEqual(sorted(self.ran), sorted(ROLES))
        self.assertFalse(step.ok)

    def test_a_hand_off_to_a_role_that_is_not_one_is_refused(self):
        with self.assertRaises(ValueError):
            self.engine.handoff("manager")


class TestPreflight(unittest.TestCase):
    """Everything that can refuse a run, before any quota is spent."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-pre-"))
        self.store = ConfigStore(self.tmp / "config.json")
        self.root = self.tmp / "build"
        self.root.mkdir()
        self.store.update({
            "providers": {r: mock_provider(r) for r in ROLES},
            "workspace": str(self.root),
        })
        self.engine = ProjectEngine(self.store, EventBus())

    def tearDown(self):
        self.engine.stop()
        self.engine.wait_for_worker(30)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_empty_goal_is_refused(self):
        with self.assertRaises(ValueError):
            self.engine.start("", str(self.root))

    def test_a_folder_that_does_not_exist_is_refused(self):
        with self.assertRaises(ValueError):
            self.engine.start("build it", str(self.root / "nope"))

    def test_a_missing_cli_is_refused_by_name(self):
        self.store.update({"providers": {"qa": {"command": ["definitely-not-installed"]}}})
        with self.assertRaises(ValueError) as ctx:
            self.engine.start("build it", str(self.root))
        self.assertIn("QA", str(ctx.exception))
        self.assertIn("definitely-not-installed", str(ctx.exception))

    def test_a_council_run_blocks_a_project(self):
        def busy():
            raise RuntimeError("A run is in progress.")

        engine = ProjectEngine(self.store, EventBus(), busy_check=busy)
        with self.assertRaises(RuntimeError):
            engine.start("build it", str(self.root))

    def test_resuming_nothing_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.engine.start("", str(self.root), resume=True)
        self.assertIn("BOARD.json", str(ctx.exception))

    def test_a_second_project_in_the_same_folder_is_refused(self):
        ws = Workspace(self.root)
        ws.ensure()
        ws.write_board({"project_id": "abc", "status": "IMPLEMENTING"})
        with self.assertRaises(ValueError) as ctx:
            self.engine.start("build it", str(self.root))
        self.assertIn("already has a project", str(ctx.exception))

    def test_a_finished_project_does_not_block_a_new_one(self):
        ws = Workspace(self.root)
        ws.ensure()
        ws.write_board({"project_id": "abc", "status": COMPLETED})
        project = self.engine.start("build something else", str(self.root))
        self.assertNotEqual(project.id, "abc")
        self.engine.stop()

    def test_a_run_that_hit_a_limit_resumes_with_its_budget_cleared(self):
        # The bounds exist so an unattended loop stops; resuming is what makes
        # them cheap. Leaving the counter that ended the run at its limit would
        # end the resumed run on its first decision, for a reason it has
        # already reported.
        ws = Workspace(self.root)
        ws.ensure()
        ws.write_board({
            "project_id": "abc", "goal": "build it", "status": FAILED,
            "fix_attempts": 3, "build_health": HEALTH_FAILING,
            "error": "The build has failed 3 times in a row",
            "columns": {COL_BACKLOG: [{"id": "t1", "title": "finish it"}]},
        })
        project = self.engine.start("", str(self.root), resume=True)
        self.assertEqual(project.id, "abc")
        self.assertEqual(project.fix_attempts, 0)
        self.assertEqual(project.error, "")
        self.assertNotIn(project.status, (COMPLETED, FAILED))
        self.engine.stop()

    def test_a_completed_project_is_not_resumed(self):
        ws = Workspace(self.root)
        ws.ensure()
        ws.write_board({"project_id": "abc", "goal": "g", "status": COMPLETED})
        with self.assertRaises(ValueError) as ctx:
            self.engine.start("", str(self.root), resume=True)
        self.assertIn("already finished", str(ctx.exception))

    def test_a_resumed_project_keeps_the_snapshot_it_started_with(self):
        # Retaking it here would anchor the half-built codebase and quietly
        # replace the only thing a rollback could restore.
        taken = []
        self.engine._take_snapshot = lambda p: taken.append(p)
        Workspace(self.root).ensure()
        project = Project(id="abc", goal="g", workspace=str(self.root))
        project.config = self.store.all()
        project.snapshot = gitutil.Snapshot(
            root=str(self.root), head="a" * 40, commit="b" * 40,
        )
        self.engine._stop.set()
        self.engine._execute(project)
        self.assertEqual(taken, [])
        self.assertEqual(project.snapshot.commit, "b" * 40)

    def test_a_fresh_project_takes_one(self):
        taken = []
        self.engine._take_snapshot = lambda p: taken.append(p)
        Workspace(self.root).ensure()
        project = Project(id="abc", goal="g", workspace=str(self.root))
        project.config = self.store.all()
        self.engine._stop.set()
        self.engine._execute(project)
        self.assertEqual(len(taken), 1)

    def test_the_innovation_slider_overrides_the_setting(self):
        self.store.update({"project": {"innovation_rounds": 2}})
        project = self.engine.start("build it", str(self.root), innovation=0)
        self.assertEqual(project.innovation_rounds, 0)
        self.engine.stop()

    def test_an_existing_codebase_gets_a_baseline_verification(self):
        # Being handed a repository that was already red matters: without this
        # the first failure looks like the developer broke it.
        (self.root / "go.mod").write_text("module x\n", encoding="utf-8")
        project = self.engine.start("improve it", str(self.root))
        self.assertTrue(project.needs_verification)
        self.assertIn("go test ./...", project.tooling["commands"])
        self.engine.stop()

    def test_an_empty_folder_is_not_verified_before_anything_exists(self):
        # Verifying an empty directory reports a failing build ("no tests ran")
        # and sends the developer off to fix a project that does not exist yet.
        project = self.engine.start("build it", str(self.root))
        self.assertFalse(project.needs_verification)
        self.engine.stop()

    def test_the_root_is_not_collapsed_to_the_repository(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        nested = self.root / "service"
        nested.mkdir()
        project = self.engine.start("build it", str(nested))
        self.assertEqual(project.workspace, str(nested.resolve()))
        self.engine.stop()


class TestTheLoop(unittest.TestCase):
    """The whole decision loop, against a real directory.

    Slow, and the only test here that would notice if the turns stopped
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
            "project": {"max_steps": 24, "max_fix_attempts": 3,
                        "innovation_rounds": 1},
        })
        self.bus = EventBus()
        self.engine = ProjectEngine(self.store, self.bus)

    def tearDown(self):
        self.engine.stop()
        self.engine.wait_for_worker(60)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def drain(self, timeout: float = 400.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.engine.is_running():
                return
            time.sleep(0.25)
        self.fail("The project never finished.")

    def test_a_project_runs_from_goal_to_a_clear_board(self):
        self.engine.start("A greeting module in Python.", str(self.root))
        self.drain()

        project = self.engine.project
        self.assertEqual(project.status, COMPLETED, project.error)

        # Real files, written by real turns.
        self.assertTrue((self.root / "greeter.py").exists())
        self.assertTrue((self.root / "test_greeter.py").exists())

        # The build genuinely failed once and was genuinely fixed. The mock's
        # first implementation drops the name from the greeting, so its own
        # test suite really fails; QA really runs it, really captures the
        # trace, and the developer is really handed it back.
        self.assertEqual(project.build_health, HEALTH_PASSING)
        self.assertIn("{name}", (self.root / "greeter.py").read_text(encoding="utf-8"))
        triggers = [s.trigger for s in project.steps]
        self.assertIn("build FAILING", triggers)
        # And the counter was reset once it passed, so the next failure starts
        # from a full budget rather than from a spent one.
        self.assertEqual(project.fix_attempts, 0)

        # Every card reached Done, including the one nobody asked for.
        self.assertEqual(project.column(COL_BACKLOG), [])
        self.assertEqual(project.column(COL_IN_REVIEW), [])
        self.assertTrue(
            any(c["origin"] == ORIGIN_INNOVATION for c in project.column(COL_DONE))
        )

    def test_the_first_turn_writes_nothing(self):
        # The read-only audit is the promise that pointing this at somebody's
        # repository looks before it touches. It is checked here against a real
        # invocation rather than trusted from the decision table.
        self.engine.start("A greeting module in Python.", str(self.root))
        deadline = time.time() + 120
        while time.time() < deadline:
            project = self.engine.project
            if project and project.steps and project.steps[0].ended_at:
                break
            time.sleep(0.1)
        first = self.engine.project.steps[0]
        self.assertTrue(first.read_only)
        self.assertEqual(first.role, "qa")
        self.assertEqual(first.files_modified, [])

    def test_the_board_tracks_the_run(self):
        self.engine.start("A greeting module in Python.", str(self.root))
        self.drain()
        board = Workspace(self.root).read_board()
        self.assertEqual(board["status"], COMPLETED)
        self.assertEqual(board["build_health"], HEALTH_PASSING)
        self.assertGreater(board["steps_used"], 5)
        self.assertEqual(board["columns"]["backlog"], [])

    def test_the_critique_log_accumulates(self):
        self.engine.start("A greeting module in Python.", str(self.root))
        self.drain()
        text = Workspace(self.root).critique()
        self.assertIn("Verification", text)
        self.assertIn("FAILING", text)

    def test_events_narrate_the_run(self):
        q = self.bus.subscribe()
        self.engine.start("A greeting module in Python.", str(self.root))
        self.drain()
        seen = set()
        while not q.empty():
            seen.add(q.get_nowait().get("kind"))
        for name in ("project_started", "project_step", "project_step_done",
                     "project_state", "project_log"):
            self.assertIn(name, seen, name)

    def test_stopping_ends_the_run(self):
        self.engine.start("A greeting module in Python.", str(self.root))
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

        board = Workspace(self.root).read_board()
        board["status"] = "IMPLEMENTING"  # a crash leaves it mid-run, not FAILED
        Workspace(self.root).write_board(board)

        resumed = self.engine.start("", str(self.root), resume=True)
        self.assertEqual(resumed.goal, "A greeting module in Python.")
        self.drain()
        self.assertEqual(self.engine.project.status, COMPLETED)

    def test_pausing_halts_between_turns(self):
        self.engine.start("A greeting module in Python.", str(self.root))
        time.sleep(1.0)
        self.engine.pause()
        deadline = time.time() + 200
        while time.time() < deadline and not self.engine.project.paused:
            time.sleep(0.2)
        used = self.engine.project.steps_used
        time.sleep(2.0)
        # Paused means paused: no further turn may start.
        self.assertEqual(self.engine.project.steps_used, used)
        self.engine.resume()
        self.drain()
        self.assertEqual(self.engine.project.status, COMPLETED)

    def test_the_turn_limit_stops_a_project_that_cannot_finish(self):
        self.store.update({"project": {"max_steps": 2, "max_fix_attempts": 3,
                                       "innovation_rounds": 0}})
        self.engine.start("A greeting module in Python.", str(self.root))
        self.drain()
        self.assertEqual(self.engine.project.status, FAILED)
        self.assertIn("turn limit", self.engine.project.error)


class TestSnapshotState(unittest.TestCase):
    """What the tab renders when nothing is running."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-snap-"))
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
        ws = Workspace(self.root)
        ws.ensure()
        project = Project(id="abc", goal="build it", workspace=str(self.root))
        project.status = "IMPLEMENTING"
        project.tasks = [card("t1", COL_BACKLOG)]
        ws.write_board(project.board_document())

        state = self.engine.snapshot_state(str(self.root))
        self.assertTrue(state["found_on_disk"])
        self.assertTrue(state["resumable"])
        self.assertEqual(state["project"]["goal"], "build it")
        self.assertEqual(state["project"]["counts"]["backlog"], 1)

    def test_a_finished_project_is_not_resumable(self):
        ws = Workspace(self.root)
        ws.ensure()
        ws.write_board({"project_id": "abc", "goal": "g", "status": COMPLETED})
        state = self.engine.snapshot_state(str(self.root))
        self.assertFalse(state["resumable"])

    def test_a_dismissed_project_is_not_offered_again(self):
        ws = Workspace(self.root)
        ws.ensure()
        project = Project(id="abc", goal="build it", workspace=str(self.root))
        project.status = COMPLETED
        self.engine._project = project

        self.engine.dismiss(str(self.root))
        # Both copies: the one in memory, and the board it would be found in
        # again on the next reload.
        self.assertIsNone(self.engine.snapshot_state(str(self.root))["project"])
        self.assertTrue(ws.read_board()["dismissed"])

    def test_a_board_left_on_disk_can_be_dismissed_without_being_in_memory(self):
        ws = Workspace(self.root)
        ws.ensure()
        ws.write_board({"project_id": "abc", "goal": "g", "status": COMPLETED})
        self.engine.dismiss(str(self.root))
        self.assertIsNone(self.engine.snapshot_state(str(self.root))["project"])

    def test_a_run_that_hit_a_limit_is_still_resumable(self):
        # Everything it built is on disk and the board says where it got to.
        # Only COMPLETED means there is nothing left to schedule.
        ws = Workspace(self.root)
        ws.ensure()
        ws.write_board({"project_id": "abc", "goal": "g", "status": FAILED})
        self.assertTrue(self.engine.snapshot_state(str(self.root))["resumable"])

    def test_a_finished_project_does_not_follow_you_to_another_folder(self):
        # A completed run stays in memory, but asked about a different folder
        # this must report *that* folder rather than the last thing it built.
        other = self.tmp / "elsewhere"
        other.mkdir()
        finished = Project(id="abc", goal="g", workspace=str(self.root))
        finished.status = COMPLETED
        self.engine._project = finished

        state = self.engine.snapshot_state(str(other))
        self.assertIsNone(state["project"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
