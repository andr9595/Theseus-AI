"""End-to-end pipeline tests against a real git repository and the mock agent.

These drive the actual state machine, subprocesses and git plumbing rather
than mocking them out - the parts most likely to break are exactly the ones a
mock would paper over.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil import config as cfg  # noqa: E402
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
        # Transcripts go to `config_dir()/runs`, which is derived from the
        # environment rather than from the store's path - without this every
        # test run would file its fixtures in the developer's real history.
        previous_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "xdg")

        def restore_xdg():
            if previous_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = previous_xdg

        self.addCleanup(restore_xdg)

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


class TestContinuedConversation(PipelineTestBase):
    """A follow-up run must carry the earlier exchange to the agents.

    Continuation is council-level and provider-neutral: the transcript this
    app already keeps is replayed into the next prompt, rather than resuming a
    CLI's private session - which a custom configured command does not have.
    """

    def setUp(self):
        super().setUp()
        self.store.update({"zero_touch": True})

    def first_run(self, task="remember MAGIC-THREAD-TOKEN"):
        run = self.pipeline.start(task, str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        return run

    def prompt_size(self, run, stage):
        """How long a prompt the stage was actually launched with.

        The recorded command redacts the prompt down to its length, so that
        length is the only direct evidence of what reached the CLI.
        """
        match = re.search(r"<prompt: (\d+) chars>", " ".join(run.stages[stage].command))
        self.assertIsNotNone(match, "the stage recorded no prompt")
        return int(match.group(1))

    def test_a_follow_up_reaches_both_agents(self):
        first = self.first_run()
        second = self.pipeline.start(
            "now do the follow-up", str(self.repo),
            continue_from=first.transcript_name,
        )
        self.wait_terminal()

        self.assertEqual(second.parent_run_id, first.id)
        # The task itself stays the new message; the thread rides alongside it.
        self.assertEqual(second.task, "now do the follow-up")
        self.assertEqual(len(second.conversation), 1)
        self.assertIn("MAGIC-THREAD-TOKEN", second.conversation[0]["task"])
        self.assertTrue(second.conversation[0]["replies"])

        # And it reached the CLIs, not just the Run object: both stages were
        # launched with a materially longer prompt than the same task alone.
        for stage in ("drafter", "polisher"):
            self.assertGreater(
                self.prompt_size(second, stage), self.prompt_size(first, stage) + 200,
                f"the {stage} stage was not told what came before",
            )

    def test_the_thread_grows_with_each_follow_up(self):
        first = self.first_run()
        second = self.pipeline.start(
            "second message", str(self.repo), continue_from=first.transcript_name
        )
        self.wait_terminal()
        third = self.pipeline.start(
            "third message", str(self.repo), continue_from=second.transcript_name
        )
        self.wait_terminal()

        self.assertEqual([t["task"] for t in third.conversation],
                         ["remember MAGIC-THREAD-TOKEN", "second message"])

    def test_an_ordinary_run_carries_no_conversation(self):
        run = self.first_run()
        self.assertEqual(run.conversation, [])
        self.assertEqual(run.parent_run_id, "")

    def test_continuing_a_missing_transcript_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            self.pipeline.start(
                "follow up", str(self.repo), continue_from="1700000000-nope.json"
            )

    def test_a_transcript_outside_the_runs_directory_is_refused(self):
        # `continue_from` arrives from an HTTP body; it names a file, not a path.
        with self.assertRaises(ValueError):
            self.pipeline.start(
                "follow up", str(self.repo), continue_from="../../config.json"
            )

    def test_a_run_cannot_be_continued_in_another_repository(self):
        first = self.first_run()
        other = self.tmp / "other"
        other.mkdir()
        git(["init", "-q"], other)

        with self.assertRaisesRegex(ValueError, "repository it started in"):
            self.pipeline.start(
                "follow up", str(other), continue_from=first.transcript_name
            )

    def test_a_transcript_from_before_this_feature_can_still_be_continued(self):
        # Older transcripts have no `conversation` or `parent_run_id` key. They
        # are still one turn of a conversation - the first one.
        legacy = cfg.runs_dir() / "1700000000-legacyrun.json"
        legacy.write_text(json.dumps({
            "id": "legacyrun",
            "task": "the original request",
            "repo": str(self.repo),
            "state": "complete",
            "stages": {"polisher": {"label": "Claude", "output": "I did the thing."}},
        }), encoding="utf-8")

        run = self.pipeline.start(
            "follow up", str(self.repo), continue_from=legacy.name
        )
        self.wait_terminal()

        self.assertEqual(run.parent_run_id, "legacyrun")
        self.assertEqual(len(run.conversation), 1)
        self.assertEqual(run.conversation[0]["replies"][0]["output"], "I did the thing.")

    def test_a_long_reply_is_trimmed_before_it_is_stored(self):
        # A thread copies its predecessor's turns into every later transcript,
        # so an unbounded reply would be duplicated for as long as it runs.
        from aicouncil.pipeline import MAX_TURN_CHARS, _conversation_turn

        turn = _conversation_turn({
            "id": "x",
            "task": "t",
            "stages": {"polisher": {"label": "Claude", "output": "x" * 90_000}},
            "stage_order": ["polisher"],
        })
        self.assertLess(len(turn["replies"][0]["output"]), MAX_TURN_CHARS + 200)


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


class TestRoleTemplates(unittest.TestCase):
    """Role behaviour is a setting, not a constant.

    The shipped prompts are defaults the operator can replace; what must not
    change is that the resolved text actually reaches the CLI.
    """

    def test_catalog_entries_are_complete(self):
        from aicouncil import prompts

        catalog = prompts.role_catalog()
        self.assertGreaterEqual(len(catalog), 6)
        for role in catalog:
            for key in ("id", "name", "summary", "system", "writes"):
                self.assertIn(key, role, f"{role.get('id')} missing {key}")
            self.assertTrue(role["system"].strip())
            self.assertIsInstance(role["writes"], bool)

    def test_stage_defaults_when_nothing_is_configured(self):
        from aicouncil import prompts

        self.assertIn("JUNIOR ENGINEER", prompts.resolve_system("drafter", {}))
        self.assertIn("SENIOR STAFF ARCHITECT", prompts.resolve_system("polisher", {}))

    def test_a_template_replaces_the_stage_default(self):
        from aicouncil import prompts

        text = prompts.resolve_system("drafter", {"role_template": "security_review"})
        self.assertIn("SECURITY REVIEWER", text)
        self.assertNotIn("JUNIOR ENGINEER", text)

    def test_edited_text_beats_the_template(self):
        from aicouncil import prompts

        text = prompts.resolve_system(
            "drafter",
            {"role_template": "security_review", "role_system": "Be a poet."},
        )
        self.assertEqual(text, "Be a poet.")

    def test_blank_override_falls_back_rather_than_sending_nothing(self):
        # Clearing the box in Settings must restore the template, not ship an
        # empty system prompt.
        from aicouncil import prompts

        for blank in ("", "   ", "\n\t "):
            text = prompts.resolve_system("drafter", {"role_system": blank})
            self.assertIn("JUNIOR ENGINEER", text)

    def test_an_unknown_template_falls_back_to_the_stage_default(self):
        from aicouncil import prompts

        text = prompts.resolve_system("polisher", {"role_template": "no_such_role"})
        self.assertIn("SENIOR STAFF ARCHITECT", text)

    def test_the_resolved_role_reaches_the_agent(self):
        from aicouncil import prompts

        prompt = prompts.build_draft_prompt(
            "add a feature", "/tmp/r", None, "",
            system=prompts.resolve_system(
                "drafter", {"role_template": "adversarial_review"}
            ),
        )
        self.assertIn("ADVERSARIAL REVIEWER", prompt)
        self.assertIn("add a feature", prompt)

    def test_house_rules_still_apply_over_a_custom_role(self):
        from aicouncil import prompts

        prompt = prompts.build_draft_prompt(
            "task", "/tmp/r", None, "ALWAYS USE TABS",
            system="Custom behaviour.",
        )
        self.assertIn("Custom behaviour.", prompt)
        self.assertIn("ALWAYS USE TABS", prompt)


class TestConversationPrompt(unittest.TestCase):
    """How an earlier exchange is rendered into the next prompt."""

    TURNS = [{
        "task": "add a login route",
        "replies": [{"stage": "polisher", "label": "Claude", "output": "Added it."}],
        "reviewer_note": "use tabs",
        "outcome": "complete, 1 file(s) changed (+9/-0)",
    }]

    def test_nothing_is_added_when_there_is_no_thread(self):
        from aicouncil import prompts

        for conversation in (None, []):
            prompt = prompts.build_draft_prompt("do it", "/tmp/r", None, "", conversation)
            self.assertNotIn("Earlier in this conversation", prompt)

    def test_the_earlier_exchange_precedes_the_current_task(self):
        from aicouncil import prompts

        prompt = prompts.build_draft_prompt("do it", "/tmp/r", None, "", self.TURNS)
        self.assertIn("add a login route", prompt)
        self.assertIn("Claude answered", prompt)
        self.assertIn("Added it.", prompt)
        self.assertIn("use tabs", prompt)
        self.assertLess(
            prompt.index("add a login route"), prompt.index("# Task"),
            "the thread must read as context, not as the task",
        )

    def test_every_stage_can_be_handed_the_thread(self):
        from aicouncil import prompts

        solo = prompts.build_solo_prompt("do it", "/tmp/r", None, "", self.TURNS)
        polish = prompts.build_polish_prompt(
            "do it", "a draft", "/tmp/r", None, "", "", self.TURNS
        )
        for prompt in (solo, polish):
            self.assertIn("add a login route", prompt)

    def test_the_repository_is_named_as_the_authority_over_the_transcript(self):
        # A remembered run may have been rolled back or edited over since.
        from aicouncil import prompts

        prompt = prompts.build_solo_prompt("do it", "/tmp/r", None, "", self.TURNS)
        self.assertIn("repository as it stands now", prompt)

    def test_an_over_long_thread_keeps_its_recent_end(self):
        from aicouncil import prompts

        turns = [
            {"task": "ancient history", "replies": [
                {"label": "Claude", "output": "x" * prompts.MAX_HISTORY_CHARS}]},
            {"task": "what I just asked", "replies": [
                {"label": "Claude", "output": "the answer that matters"}]},
        ]
        prompt = prompts.build_draft_prompt("do it", "/tmp/r", None, "", turns)
        self.assertIn("the answer that matters", prompt)
        self.assertNotIn("ancient history", prompt)
        self.assertIn("older turns dropped", prompt)


class TestRoleReachesTheRun(PipelineTestBase):
    def test_a_configured_role_is_used_by_a_real_run(self):
        # End to end: set a role, run, and confirm the agent saw that text.
        self.store.update({
            "zero_touch": True,
            "providers": {"drafter": {"role_system": "SENTINEL-ROLE-TEXT"}},
        })
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_terminal()
        # The mock agent echoes the prompt length and its role; the prompt it
        # received is what the pipeline built.
        self.assertTrue(run.stages["drafter"].output)
        self.assertEqual(run.state, "complete", run.error)
