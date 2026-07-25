"""End-to-end pipeline tests against a real git repository and the mock agent.

These drive the actual state machine, subprocesses and git plumbing rather
than mocking them out - the parts most likely to break are exactly the ones a
mock would paper over.
"""

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
from aicouncil.pipeline import Pipeline, PipelineBusy  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK = str(REPO_ROOT / "scripts" / "mock-agent.py")


def mock_provider(pid: str, role: str, extra=None):
    return {
        "id": pid,
        "label": f"Mock {role}",
        "role": role,
        "command": [sys.executable, MOCK, "--role", pid, *(extra or []), "{prompt}"],
        "auto_approve_args": ["--dangerously-skip-permissions"],
        "prompt_on_stdin": False,
        "timeout_seconds": 60,
    }


def git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        capture_output=True, text=True,
    )


class PipelineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-test-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(["init", "-q"], self.repo)
        git(["config", "user.email", "test@example.com"], self.repo)
        git(["config", "user.name", "Test"], self.repo)
        (self.repo / "README.md").write_text("# fixture\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-qm", "initial"], self.repo)

        self.config_path = self.tmp / "config.json"
        self.store = ConfigStore(self.config_path)
        self.store.update({
            "target_repo": str(self.repo),
            "safety_snapshot": True,
            "providers": {
                "drafter": mock_provider("drafter", "Junior Draft"),
                "polisher": mock_provider("polisher", "Senior Polish"),
            },
        })
        self.bus = EventBus()
        self.pipeline = Pipeline(self.store, self.bus)

    def tearDown(self):
        self.pipeline.cancel()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_for(self, predicate, timeout=45, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        run = self.pipeline.run
        self.fail(f"Timed out waiting for {what}. state={run.state if run else None}")

    def wait_terminal(self, timeout=60):
        self.wait_for(lambda: not self.pipeline.is_busy(), timeout, "a terminal state")


class TestZeroTouch(PipelineTestBase):
    def test_full_run_applies_changes_without_a_gate(self):
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(run.stages["drafter"].state, "done")
        self.assertEqual(run.stages["polisher"].state, "done")
        # Zero-Touch means the file really was written.
        self.assertTrue((self.repo / "AI_COUNCIL_DEMO.md").exists())
        self.assertIn("AI_COUNCIL_DEMO.md", run.diff)
        self.assertGreaterEqual(run.diff_stat["files"], 1)

    def test_stage_one_never_receives_auto_approve_flags(self):
        # The junior is read-only by contract; the flag must not reach it even
        # when Zero-Touch is on.
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("anything", str(self.repo))
        self.wait_terminal()
        self.assertNotIn(
            "--dangerously-skip-permissions", run.stages["drafter"].command
        )
        self.assertIn(
            "--dangerously-skip-permissions", run.stages["polisher"].command
        )

    def test_rollback_restores_the_tree(self):
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("write the artifact", str(self.repo))
        self.wait_terminal()
        artifact = self.repo / "AI_COUNCIL_DEMO.md"
        self.assertTrue(artifact.exists())

        self.pipeline.rollback()
        self.assertFalse(artifact.exists(), "rollback should remove the new file")
        self.assertTrue(gitutil.status(self.repo).clean)

    def test_rollback_preserves_pre_existing_uncommitted_work(self):
        # A rollback must not eat changes the user already had in flight.
        scratch = self.repo / "my_work_in_progress.txt"
        scratch.write_text("do not lose me\n")
        (self.repo / "README.md").write_text("# fixture\nedited by hand\n")

        self.store.update({"zero_touch": True})
        self.pipeline.start("write the artifact", str(self.repo))
        self.wait_terminal()
        self.pipeline.rollback()

        self.assertFalse((self.repo / "AI_COUNCIL_DEMO.md").exists())
        self.assertTrue(scratch.exists(), "untracked user file was destroyed")
        self.assertEqual(scratch.read_text(), "do not lose me\n")
        self.assertIn("edited by hand", (self.repo / "README.md").read_text())


class TestSnapshots(PipelineTestBase):
    def test_an_incomplete_snapshot_refuses_to_reset_the_tree(self):
        # A snapshot that captured nothing used to be indistinguishable from
        # "the tree was clean at HEAD", and restoring it reset and cleaned the
        # user's own uncommitted work away - the exact loss it exists to stop.
        scratch = self.repo / "my_work_in_progress.txt"
        scratch.write_text("do not lose me\n")
        snap = gitutil.Snapshot(root=str(self.repo), head=gitutil.status(self.repo).head)

        with self.assertRaises(gitutil.GitError):
            gitutil.restore_snapshot(snap)
        self.assertTrue(scratch.exists(), "an incomplete snapshot destroyed work")

    def test_each_snapshot_anchors_its_own_ref(self):
        # One shared ref stops protecting the first run's commit as soon as a
        # second run overwrites it.
        first = gitutil.take_snapshot(self.repo)
        (self.repo / "later.txt").write_text("more work\n")
        second = gitutil.take_snapshot(self.repo)

        self.assertNotEqual(first.ref, second.ref)
        for snap in (first, second):
            resolved = subprocess.run(
                ["git", "rev-parse", snap.ref], cwd=self.repo,
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(resolved.stdout.strip(), snap.commit)

    def test_a_failed_snapshot_leaves_no_rollback_point(self):
        scratch = self.repo / "my_work_in_progress.txt"
        scratch.write_text("do not lose me\n")

        def unwritable(path):
            raise gitutil.GitError("git add -A failed (1): scratch index is read-only")

        original = gitutil.take_snapshot
        gitutil.take_snapshot = unwritable
        self.addCleanup(setattr, gitutil, "take_snapshot", original)

        self.store.update({"zero_touch": True})
        run = self.pipeline.start("write the artifact", str(self.repo))
        self.wait_terminal()

        self.assertIsNone(run.snapshot)
        self.assertFalse(run.to_dict()["can_rollback"])
        with self.assertRaises(ValueError):
            self.pipeline.rollback()
        self.assertEqual(scratch.read_text(), "do not lose me\n")


class TestSoloMode(PipelineTestBase):
    def test_solo_mode_runs_the_stage_it_was_pointed_at(self):
        # Either slot can hold either agent, so Solo Mode must be able to run
        # the drafter slot - with the write permission the lone stage needs.
        writer = mock_provider("drafter", "Junior Draft")
        writer["command"] = [sys.executable, MOCK, "--role", "polisher", "{prompt}"]
        self.store.update({
            "zero_touch": True,
            "solo_mode": True,
            "solo_stage": "drafter",
            "providers": {"drafter": writer},
        })
        run = self.pipeline.start("write the artifact", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertNotIn("polisher", run.stages)
        self.assertEqual(run.stages["drafter"].state, "done")
        self.assertIn("--dangerously-skip-permissions", run.stages["drafter"].command)
        self.assertTrue((self.repo / "AI_COUNCIL_DEMO.md").exists())

    def test_an_unknown_solo_stage_falls_back_to_the_polisher(self):
        self.store.update({
            "zero_touch": True, "solo_mode": True, "solo_stage": "nonesuch",
        })
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(list(run.stages), ["polisher"])


class TestDiffStat(PipelineTestBase):
    def test_binary_untracked_files_are_counted_but_not_line_counted(self):
        # Regression: an agent that imports a module to verify its work leaves
        # a .pyc behind. Reading it as text invented ~9 "insertions" and
        # inflated the +N in the UI summary.
        (self.repo / "notes.txt").write_text("one\ntwo\nthree\n")
        (self.repo / "blob.bin").write_bytes(b"\x00\x01\x02" * 500)

        stat = gitutil.diff_stat(self.repo)
        self.assertEqual(stat["files"], 2, "both files should be counted")
        self.assertEqual(
            stat["insertions"], 3,
            "only the text file's lines should count toward insertions",
        )

    def test_binary_files_still_appear_in_the_diff(self):
        # Skipping the line count must not skip the file itself.
        (self.repo / "blob.bin").write_bytes(b"\x00\xff" * 100)
        diff = gitutil.working_diff(self.repo)
        self.assertIn("blob.bin", diff)


class TestApprovalGate(PipelineTestBase):
    def test_run_pauses_and_writes_nothing_before_approval(self):
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_for(
            lambda: run.state == "awaiting_approval", what="the approval gate"
        )
        # The critical guarantee: the gate is reached with a pristine tree.
        self.assertFalse((self.repo / "AI_COUNCIL_DEMO.md").exists())
        self.assertTrue(gitutil.status(self.repo).clean)
        self.assertTrue(run.stages["drafter"].output.strip())
        self.assertEqual(run.stages["polisher"].state, "pending")

        self.pipeline.approve("also mention the reviewer note")
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        self.assertTrue((self.repo / "AI_COUNCIL_DEMO.md").exists())

    def test_approval_grants_execute_permission_to_stage_two(self):
        # With Zero-Touch off, the flag must still be passed *after* the human
        # approves - otherwise the CLI would block on an interactive prompt.
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("do the thing", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")
        self.pipeline.approve()
        self.wait_terminal()
        self.assertIn(
            "--dangerously-skip-permissions", run.stages["polisher"].command
        )

    def test_reject_abandons_the_run_without_touching_files(self):
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")
        self.pipeline.reject("not what I asked for")
        self.wait_terminal()

        self.assertEqual(run.state, "cancelled")
        self.assertFalse((self.repo / "AI_COUNCIL_DEMO.md").exists())
        self.assertTrue(gitutil.status(self.repo).clean)
        self.assertEqual(run.stages["polisher"].state, "skipped")

    def test_reviewer_note_reaches_the_senior_prompt(self):
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")
        self.pipeline.approve("MAGIC-REVIEWER-TOKEN")
        self.wait_terminal()
        self.assertEqual(run.reviewer_note, "MAGIC-REVIEWER-TOKEN")

    def test_settings_changed_at_the_gate_do_not_reach_the_run(self):
        # What the operator approved is what runs. A run reads its
        # configuration once, so a settings change - from this window or a
        # second instance - cannot swap the command about to be granted write
        # permission.
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("do the thing", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")

        self.store.update({
            "providers": {
                "polisher": {"command": ["definitely-not-the-approved-command"]},
            },
        })
        self.pipeline.approve()
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertNotIn(
            "definitely-not-the-approved-command", run.stages["polisher"].command
        )

    def test_solo_mode_still_gates(self):
        # No draft to review, but the operator must still authorise writes.
        self.store.update({"zero_touch": False, "solo_mode": True})
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")
        self.assertNotIn("drafter", run.stages)
        self.pipeline.approve()
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)


class TestFailureHandling(PipelineTestBase):
    def test_failing_senior_stage_marks_the_run_failed(self):
        self.store.update({
            "zero_touch": True,
            "providers": {"polisher": mock_provider("polisher", "Senior", ["--fail"])},
        })
        run = self.pipeline.start("this will fail", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.stages["polisher"].state, "failed")
        self.assertTrue(run.error)

    def test_failing_junior_stage_does_not_abort_the_run(self):
        # A dead junior is recoverable — the senior can work from the task
        # alone — but recovery is the operator's call, not the pipeline's.
        # Under Zero-Touch the run degrades to the approval gate rather than
        # proceeding unattended; see TestDraftFailureDoesNotEscalate.
        self.store.update({
            "zero_touch": False,
            "providers": {"drafter": mock_provider("drafter", "Junior", ["--fail"])},
        })
        run = self.pipeline.start("carry on regardless", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")
        self.assertEqual(run.stages["drafter"].state, "failed")

        self.pipeline.approve()
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)

    def test_missing_executable_is_reported_not_raised(self):
        self.store.update({
            "zero_touch": True,
            "providers": {
                "polisher": {
                    "id": "polisher",
                    "label": "Ghost",
                    "role": "Senior",
                    "command": ["definitely-not-a-real-binary-xyz", "{prompt}"],
                    "auto_approve_args": [],
                    "timeout_seconds": 30,
                }
            },
        })
        run = self.pipeline.start("go", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "failed")
        self.assertIn("not installed", run.stages["polisher"].error)

    def test_unusable_provider_config_finishes_the_stage(self):
        # A stage that cannot be launched must still end. It used to be left
        # marked "running" forever, with no end time and no error on it.
        self.store.update({
            "zero_touch": True,
            "providers": {
                "polisher": {
                    "id": "polisher",
                    "label": "Broken",
                    "role": "Senior",
                    "command": [],
                    "timeout_seconds": "not a number",
                }
            },
        })
        run = self.pipeline.start("go", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "failed")
        self.assertEqual(run.stages["polisher"].state, "failed")
        self.assertTrue(run.stages["polisher"].ended_at)
        self.assertIn("misconfigured", run.stages["polisher"].error)

    def test_non_repository_target_is_rejected(self):
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        with self.assertRaises(ValueError) as ctx:
            self.pipeline.start("go", str(plain))
        self.assertIn("not a git repository", str(ctx.exception))

    def test_empty_task_is_rejected(self):
        with self.assertRaises(ValueError):
            self.pipeline.start("   ", str(self.repo))

    def test_concurrent_runs_are_refused(self):
        self.store.update({"zero_touch": False})
        self.pipeline.start("first", str(self.repo))
        with self.assertRaises(PipelineBusy):
            self.pipeline.start("second", str(self.repo))

    def test_require_clean_worktree_blocks_a_dirty_repo(self):
        (self.repo / "dirty.txt").write_text("uncommitted\n")
        self.store.update({"require_clean_worktree": True})
        with self.assertRaises(ValueError) as ctx:
            self.pipeline.start("go", str(self.repo))
        self.assertIn("uncommitted", str(ctx.exception))


class TestCancellation(PipelineTestBase):
    def test_cancel_at_the_gate_ends_the_run(self):
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")
        self.pipeline.cancel()
        self.wait_terminal()
        self.assertEqual(run.state, "cancelled")
        self.assertFalse((self.repo / "AI_COUNCIL_DEMO.md").exists())


class TestEventStream(PipelineTestBase):
    def test_subscribers_observe_the_whole_run(self):
        q = self.bus.subscribe()
        self.store.update({"zero_touch": True})
        self.pipeline.start("stream me", str(self.repo))
        self.wait_terminal()

        kinds = []
        while not q.empty():
            kinds.append(q.get_nowait()["kind"])
        for expected in ("run_started", "stage_started", "stage_output",
                         "stage_finished", "state"):
            self.assertIn(expected, kinds, f"missing {expected} event")

    def test_history_replays_to_a_late_subscriber(self):
        self.bus.publish("log", message="one")
        self.bus.publish("log", message="two")
        q = self.bus.subscribe(replay_from=0)
        self.assertEqual(q.qsize(), 2)

    def test_replay_from_skips_already_seen_events(self):
        first = self.bus.publish("log", message="one")
        self.bus.publish("log", message="two")
        q = self.bus.subscribe(replay_from=first["id"])
        self.assertEqual(q.qsize(), 1)

    def test_slow_subscriber_does_not_block_the_publisher(self):
        bus = EventBus()
        q = bus.subscribe()
        # Overfill well past the queue's 4096 maxsize.
        done = threading.Event()

        def flood():
            for i in range(5000):
                bus.publish("log", message=str(i))
            done.set()

        t = threading.Thread(target=flood, daemon=True)
        t.start()
        t.join(timeout=30)
        self.assertTrue(done.is_set(), "publish() blocked on a full subscriber queue")
        self.assertLessEqual(q.qsize(), 4096)


if __name__ == "__main__":
    unittest.main()


class TestDraftFailureDoesNotEscalate(PipelineTestBase):
    """A failed Stage 1 must not turn Zero-Touch into an unattended solo run.

    Zero-Touch means "you may skip my approval because a junior drafted it and
    a senior is verifying that draft". With no draft, that premise is gone —
    proceeding anyway silently grants an agent unattended write access under a
    setting the operator chose for a different situation.
    """

    def setUp(self):
        super().setUp()
        self.store.update({
            "zero_touch": True,
            "providers": {"drafter": mock_provider("drafter", "Junior", ["--fail"])},
        })

    def test_zero_touch_falls_back_to_the_gate(self):
        run = self.pipeline.start("do the thing", str(self.repo))
        self.wait_for(
            lambda: run.state == "awaiting_approval", what="the fallback gate"
        )
        # And crucially: nothing written while it waits.
        self.assertFalse((self.repo / "AI_COUNCIL_DEMO.md").exists())
        self.assertTrue(gitutil.status(self.repo).clean)

    def test_operator_can_still_approve_the_solo_continuation(self):
        run = self.pipeline.start("do the thing", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")
        self.pipeline.approve()
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        self.assertTrue((self.repo / "AI_COUNCIL_DEMO.md").exists())

    def test_rejecting_writes_nothing(self):
        run = self.pipeline.start("do the thing", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")
        self.pipeline.reject()
        self.wait_terminal()
        self.assertEqual(run.state, "cancelled")
        self.assertTrue(gitutil.status(self.repo).clean)

    def test_a_healthy_draft_still_skips_the_gate_under_zero_touch(self):
        # The fallback must not become a gate on every Zero-Touch run.
        self.store.update({
            "providers": {"drafter": mock_provider("drafter", "Junior Draft")},
        })
        run = self.pipeline.start("do the thing", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        self.assertNotEqual(run.state, "awaiting_approval")
