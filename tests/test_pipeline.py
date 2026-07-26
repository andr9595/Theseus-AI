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

# A stand-in for the GitHub CLI, written to a temporary PATH entry by the
# pull-request tests. It records what it was asked to do, and prints the URL
# the way gh does - after an unrelated line, so the code that has to pick the
# URL out of the output is exercised rather than assumed.
FAKE_GH = '''"""Stand-in `gh` for the pull-request tests."""
import os
import sys

with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")

if sys.argv[1:3] == ["auth", "status"]:
    if os.environ.get("FAKE_GH_UNAUTHENTICATED"):
        sys.stderr.write("You are not logged into any GitHub hosts.\\n")
        sys.exit(1)
elif sys.argv[1:3] == ["pr", "create"]:
    if os.environ.get("FAKE_GH_FAIL"):
        sys.stderr.write("pull request create failed: HTTP 403\\n")
        sys.exit(1)
    print("Warning: 1 uncommitted change")
    print("https://github.com/example/project/pull/7")
sys.exit(0)
'''


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


def git_out(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


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
        # -b: the initial branch name is a host git setting, and the
        # pull-request tests assert on which branch a run targeted.
        git(["init", "-q", "-b", "main"], self.repo)
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


class TestPullRequestMode(PipelineTestBase):
    """The base branch must survive a run untouched.

    The remote is a real bare repository and the push is a real push. Only the
    GitHub CLI is a stand-in - it is the one part of this that cannot be run in
    a temporary directory - and it records its argv so the tests can check what
    it was actually asked to open.
    """

    def setUp(self):
        super().setUp()

        self.origin = self.tmp / "origin.git"
        self.origin.mkdir()
        git(["init", "--bare", "-q"], self.origin)
        git(["remote", "add", "origin", str(self.origin)], self.repo)

        bindir = self.tmp / "bin"
        bindir.mkdir()
        fake_gh = bindir / "gh"
        fake_gh.write_text(f"#!{sys.executable}\n{FAKE_GH}", encoding="utf-8")
        fake_gh.chmod(0o755)

        self.gh_log = self.tmp / "gh.log"
        self.set_env("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        self.set_env("FAKE_GH_LOG", str(self.gh_log))

        self.store.update({"zero_touch": True, "pull_request_mode": True})

    def set_env(self, name, value):
        previous = os.environ.get(name)

        def restore():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

        self.addCleanup(restore)
        os.environ[name] = value

    def gh_calls(self):
        return self.gh_log.read_text().splitlines() if self.gh_log.exists() else []

    def branches(self):
        return git_out(["branch", "--format=%(refname:short)"], self.repo).split()

    # -- the guarantee -----------------------------------------------------

    def test_the_base_branch_is_left_exactly_as_it_was(self):
        head_before = git_out(["rev-parse", "main"], self.repo)
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(run.base_branch, "main")
        # Back where the operator started, with nothing of the run on it -
        # otherwise the *next* run would silently branch from this one's work.
        self.assertEqual(gitutil.status(self.repo).branch, "main")
        self.assertEqual(git_out(["rev-parse", "main"], self.repo), head_before)
        self.assertTrue(gitutil.status(self.repo).clean)
        self.assertFalse((self.repo / "AI_COUNCIL_DEMO.md").exists())

    def test_the_work_reaches_the_branch_and_the_remote(self):
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)

        self.assertIn(run.work_branch, self.branches())
        self.assertIn(
            "AI_COUNCIL_DEMO.md",
            git_out(["ls-tree", "-r", "--name-only", run.work_branch], self.repo),
        )
        # Pushed for real, to a real remote.
        self.assertEqual(
            git_out(["rev-parse", f"refs/heads/{run.work_branch}"], self.origin),
            run.pull_request.commit,
        )

    def test_the_pull_request_targets_the_branch_that_was_checked_out(self):
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)

        created = [c for c in self.gh_calls() if c.startswith("pr create")]
        self.assertEqual(len(created), 1, self.gh_calls())
        self.assertIn(f"--base main --head {run.work_branch}", created[0])
        self.assertEqual(
            run.pull_request.url, "https://github.com/example/project/pull/7"
        )
        self.assertEqual(
            run.to_dict()["pull_request"]["url"], run.pull_request.url
        )

    def test_the_diff_shows_what_the_pull_request_contains(self):
        # The worktree diff is empty once the work is committed; what the tab
        # must show from then on is the branch against its base.
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        self.assertIn("AI_COUNCIL_DEMO.md", run.diff)
        self.assertGreaterEqual(run.diff_stat["files"], 1)

    def test_rollback_is_not_offered_once_a_pull_request_exists(self):
        # Restoring the worktree would not close the PR or unpush the branch.
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()
        self.assertIsNotNone(run.snapshot)
        self.assertFalse(run.to_dict()["can_rollback"])

    # -- preconditions -----------------------------------------------------

    def test_a_dirty_tree_is_refused_even_with_the_clean_toggle_off(self):
        # Whatever is in the tree at the start would be swept into the commit.
        (self.repo / "dirty.txt").write_text("uncommitted\n")
        self.store.update({"require_clean_worktree": False})
        with self.assertRaises(ValueError) as ctx:
            self.pipeline.start("add a demo artifact", str(self.repo))
        self.assertIn("clean tree", str(ctx.exception))

    def test_a_repository_with_no_origin_is_refused(self):
        git(["remote", "remove", "origin"], self.repo)
        with self.assertRaisesRegex(ValueError, "origin"):
            self.pipeline.start("add a demo artifact", str(self.repo))

    def test_an_unauthenticated_cli_is_caught_before_any_agent_runs(self):
        self.set_env("FAKE_GH_UNAUTHENTICATED", "1")
        with self.assertRaisesRegex(ValueError, "GitHub CLI is not ready"):
            self.pipeline.start("add a demo artifact", str(self.repo))
        self.assertEqual(self.branches(), ["main"])

    def test_rejecting_at_the_gate_creates_no_branch(self):
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")
        self.pipeline.reject()
        self.wait_terminal()

        self.assertEqual(run.state, "cancelled")
        self.assertEqual(self.branches(), ["main"])
        self.assertEqual(self.gh_calls(), ["auth status"])

    # -- failure -----------------------------------------------------------

    def test_a_failed_publish_says_where_the_work_is(self):
        self.set_env("FAKE_GH_FAIL", "1")
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "failed")
        self.assertIsNone(run.pull_request)
        self.assertIn(run.work_branch, run.error)
        self.assertIn("main is unchanged", run.error)
        # The work is still there to recover, on the branch and in the tree.
        self.assertEqual(gitutil.status(self.repo).branch, run.work_branch)
        self.assertTrue((self.repo / "AI_COUNCIL_DEMO.md").exists())
        self.assertTrue(run.to_dict()["can_rollback"])

    def test_a_senior_stage_that_changes_nothing_opens_no_pull_request(self):
        self.store.update({
            "providers": {"polisher": mock_provider("polisher", "Senior", ["--fail"])},
        })
        run = self.pipeline.start("do nothing at all", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "failed")
        self.assertIsNone(run.pull_request)
        self.assertNotIn("pr create", " ".join(self.gh_calls()))

    # -- naming ------------------------------------------------------------

    def test_branch_names_carry_the_task_and_the_run_id(self):
        self.assertEqual(
            gitutil.branch_for("abc123", "Add rate limiting to /api/login!"),
            "ai-council/add-rate-limiting-to-api-login-abc123",
        )
        # A task with nothing nameable in it still yields a valid ref.
        self.assertEqual(gitutil.branch_for("abc123", "!!!"), "ai-council/abc123")


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

    def test_a_follow_up_records_what_its_thread_cost(self):
        first = self.first_run()
        second = self.pipeline.start(
            "second message", str(self.repo), continue_from=first.transcript_name
        )
        self.wait_terminal()

        self.assertEqual(second.context["stored_turns"], 1)
        self.assertEqual(second.context["compacted_turns"], 0)
        self.assertGreater(second.context["estimated_tokens"], 0)
        # The percentage is measured against the configured window, not a
        # figure any CLI reported.
        self.assertEqual(second.context["window_tokens"], 200_000)
        self.assertIsNotNone(second.context["percent"])

    def test_compacting_on_request_summarises_the_earlier_turns(self):
        first = self.first_run()
        second = self.pipeline.start(
            "second message", str(self.repo), continue_from=first.transcript_name
        )
        self.wait_terminal()
        third = self.pipeline.start(
            "third message", str(self.repo),
            continue_from=second.transcript_name, compact_context=True,
        )
        self.wait_terminal()

        # Nothing is lost from the thread; the oldest turns are summarised and
        # the newest is kept whole.
        self.assertEqual([t["task"] for t in third.conversation],
                         ["remember MAGIC-THREAD-TOKEN", "second message"])
        self.assertTrue(third.conversation[0]["compacted"])
        self.assertFalse(third.conversation[-1].get("compacted"))
        self.assertEqual(third.context["compacted_turns"], 1)

    def test_a_window_of_zero_reports_tokens_without_a_percentage(self):
        # An operator who does not know their model's window can say so rather
        # than being shown a percentage of a number this app made up.
        self.store.update({"context_window_tokens": 0})
        first = self.first_run()
        second = self.pipeline.start(
            "second message", str(self.repo), continue_from=first.transcript_name
        )
        self.wait_terminal()

        self.assertIsNone(second.context["percent"])
        self.assertGreater(second.context["estimated_tokens"], 0)

    def test_the_context_preview_prices_compaction_before_the_run(self):
        first = self.first_run()
        second = self.pipeline.start(
            "second message", str(self.repo), continue_from=first.transcript_name
        )
        self.wait_terminal()

        preview = self.pipeline.context_preview(second.transcript_name)
        # Two turns would be replayed: the first run, and the one being
        # continued - which is not yet a turn of any stored conversation.
        self.assertEqual(preview["stored_turns"], 2)
        self.assertEqual(preview["compacted_turns"], 0)
        self.assertEqual(preview["compacted"]["compacted_turns"], 1)
        self.assertLess(
            preview["compacted"]["characters"], preview["characters"]
        )

    def test_the_context_preview_refuses_a_missing_transcript(self):
        with self.assertRaisesRegex(ValueError, "No such run transcript"):
            self.pipeline.context_preview("1700000000-nope.json")

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


class TestConversationList(PipelineTestBase):
    """What the sidebar lists: conversations, not individual runs."""

    def setUp(self):
        super().setUp()
        self.store.update({"zero_touch": True})

    def run_once(self, task, continue_from=""):
        run = self.pipeline.start(task, str(self.repo), continue_from=continue_from)
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        return run

    def test_one_run_is_one_conversation(self):
        run = self.run_once("do the thing")
        listed = self.pipeline.history()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["file"], run.transcript_name)
        self.assertEqual(listed[0]["messages"], 1)

    def test_a_follow_up_replaces_its_parent_rather_than_joining_it(self):
        first = self.run_once("the original request")
        second = self.run_once("a follow-up", continue_from=first.transcript_name)
        third = self.run_once("another follow-up", continue_from=second.transcript_name)

        listed = self.pipeline.history()
        self.assertEqual(len(listed), 1, "a thread is one conversation, not three")
        self.assertEqual(listed[0]["file"], third.transcript_name)
        self.assertEqual(listed[0]["messages"], 3)
        # Named for what it was opened about, so the row does not rename itself
        # every time the operator sends another message.
        self.assertEqual(listed[0]["title"], "the original request")

    def test_continuing_the_same_run_twice_lists_both_branches(self):
        first = self.run_once("the original request")
        left = self.run_once("down one path", continue_from=first.transcript_name)
        right = self.run_once("down another", continue_from=first.transcript_name)

        files = {c["file"] for c in self.pipeline.history()}
        self.assertEqual(files, {left.transcript_name, right.transcript_name})

    def test_unrelated_runs_stay_separate_conversations(self):
        self.run_once("one thing")
        self.run_once("a different thing")
        self.assertEqual(len(self.pipeline.history()), 2)

    def test_a_transcript_from_before_this_feature_is_its_own_conversation(self):
        # No `parent_run_id` and no `conversation`: one message, listed as one
        # conversation rather than guessed into somebody else's thread.
        legacy = cfg.runs_dir() / "1700000000-legacyrun.json"
        legacy.write_text(json.dumps({
            "id": "legacyrun",
            "task": "the original request",
            "repo": str(self.repo),
            "state": "complete",
        }), encoding="utf-8")

        listed = self.pipeline.history()
        self.assertEqual([c["file"] for c in listed], [legacy.name])
        self.assertEqual(listed[0]["messages"], 1)
        self.assertEqual(listed[0]["title"], "the original request")

    def test_an_unreadable_transcript_does_not_break_the_list(self):
        run = self.run_once("do the thing")
        (cfg.runs_dir() / "1700000001-broken.json").write_text("{oh no", encoding="utf-8")
        self.assertEqual([c["file"] for c in self.pipeline.history()],
                         [run.transcript_name])


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

    def test_an_over_long_thread_compacts_its_oldest_turns(self):
        from aicouncil import prompts

        turns = [
            {"task": "ancient history", "replies": [
                {"label": "Claude", "output": "x" * prompts.MAX_HISTORY_CHARS}]},
            {"task": "what I just asked", "replies": [
                {"label": "Claude", "output": "the answer that matters"}]},
        ]
        prompt = prompts.build_draft_prompt("do it", "/tmp/r", None, "", turns)

        self.assertIn("the answer that matters", prompt)
        # The old turn is summarised, not dropped: what was asked survives even
        # when the answer no longer fits, which is what a follow-up refers to.
        self.assertIn("ancient history", prompt)
        self.assertIn("compacted summary", prompt)
        self.assertLess(len(prompt), prompts.MAX_HISTORY_CHARS + 5_000)

    def test_the_newest_turn_is_never_compacted(self):
        from aicouncil import prompts

        turns = [
            {"task": "old", "replies": [
                {"label": "Claude", "output": "y" * prompts.MAX_HISTORY_CHARS}]},
            {"task": "new", "replies": [
                {"label": "Claude", "output": "SENTINEL-" + "z" * 5_000}]},
        ]
        context = prompts.conversation_context(turns)

        self.assertEqual(context.compacted_turns, 1)
        self.assertEqual(context.stored_turns, 2)
        self.assertFalse(context.conversation[1].get("compacted"))
        self.assertIn("SENTINEL-" + "z" * 5_000, context.rendered)

    def test_compacting_by_hand_needs_no_over_long_thread(self):
        from aicouncil import prompts

        # Well inside the budget, so nothing is compacted unless asked.
        turns = [
            {"task": "first", "replies": [{"label": "Claude", "output": "c" * 4_000}]},
            {"task": "second", "replies": [{"label": "Claude", "output": "another one"}]},
        ]
        relaxed = prompts.conversation_context(turns)
        forced = prompts.conversation_context(turns, force=True)

        self.assertEqual(relaxed.compacted_turns, 0)
        self.assertEqual(forced.compacted_turns, 1)
        self.assertLess(forced.characters, relaxed.characters)
        # Compaction rewrites turns; it must not mutate the caller's list.
        self.assertNotIn("compacted", turns[0])

    def test_an_already_compacted_turn_is_not_compacted_again(self):
        from aicouncil import prompts

        turns = [
            prompts._compact_turn(
                {"task": "first", "replies": [{"label": "Claude", "output": "b" * 40_000}]}
            ),
            {"task": "second", "replies": [{"label": "Claude", "output": "fresh"}]},
        ]
        context = prompts.conversation_context(turns, force=True)

        # Counted once, for the state it is already in - not once per pass.
        self.assertEqual(context.compacted_turns, 1)

    def test_the_token_figure_is_derived_from_the_rendered_text(self):
        # It is an estimate, and the only honest thing to pin is that it is
        # computed from what the agents are actually given.
        from aicouncil import prompts

        context = prompts.conversation_context(self.TURNS)
        self.assertEqual(context.characters, len(context.rendered))
        self.assertEqual(
            context.estimated_tokens,
            -(-context.characters // prompts.ESTIMATED_CHARS_PER_TOKEN),
        )


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


class TestCommitFromTheApp(PipelineTestBase):
    """Committing is the operator's act; the app only has to do it faithfully."""

    def test_commits_the_whole_reviewed_tree(self):
        (self.repo / "new.txt").write_text("added\n")
        (self.repo / "README.md").write_text("# fixture\nedited\n")

        result = gitutil.commit_all(self.repo, "do the thing")
        self.assertEqual(result["message"], "do the thing")
        self.assertEqual(result["files"], 2)
        self.assertTrue(gitutil.status(self.repo).clean)

        log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=self.repo,
                             capture_output=True, text=True, check=True)
        self.assertEqual(log.stdout.strip(), "do the thing")

    def test_untracked_files_are_included(self):
        # The diff the operator reviewed shows new files as additions, so a
        # commit that skipped them would not be what they approved.
        (self.repo / "brand_new.py").write_text("x = 1\n")
        gitutil.commit_all(self.repo, "add it")
        tracked = subprocess.run(["git", "ls-files"], cwd=self.repo,
                                 capture_output=True, text=True, check=True)
        self.assertIn("brand_new.py", tracked.stdout)

    def test_an_empty_message_is_refused(self):
        (self.repo / "x.txt").write_text("y\n")
        for blank in ("", "   ", "\n"):
            with self.assertRaises(gitutil.GitError):
                gitutil.commit_all(self.repo, blank)

    def test_a_clean_tree_is_refused_rather_than_committed_empty(self):
        with self.assertRaises(gitutil.GitError) as ctx:
            gitutil.commit_all(self.repo, "nothing to say")
        self.assertIn("clean", str(ctx.exception).lower())

    def test_ignored_files_alone_do_not_make_a_commit(self):
        (self.repo / ".gitignore").write_text("build/\n")
        gitutil.commit_all(self.repo, "add ignore rules")
        (self.repo / "build").mkdir()
        (self.repo / "build" / "out.o").write_text("binary\n")
        with self.assertRaises(gitutil.GitError):
            gitutil.commit_all(self.repo, "should not commit build output")

    def test_a_missing_identity_is_explained_not_dumped(self):
        subprocess.run(["git", "config", "--unset", "user.email"], cwd=self.repo,
                       capture_output=True)
        subprocess.run(["git", "config", "--unset", "user.name"], cwd=self.repo,
                       capture_output=True)
        # A repo with no identity and no global fallback; skip where a global
        # identity exists, since git would then succeed.
        probe = subprocess.run(["git", "var", "GIT_AUTHOR_IDENT"], cwd=self.repo,
                               capture_output=True, text=True)
        if probe.returncode == 0:
            self.skipTest("a global git identity is configured on this machine")
        (self.repo / "x.txt").write_text("y\n")
        with self.assertRaises(gitutil.GitError) as ctx:
            gitutil.commit_all(self.repo, "try it")
        self.assertIn("identity", str(ctx.exception).lower())


class TestEditableRoles(unittest.TestCase):
    """Built-ins are defaults, not laws — and a role you add is not a lesser one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-roles-"))
        self.store = ConfigStore(self.tmp / "config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_untouched_builtin_reports_the_shipped_text(self):
        from aicouncil import prompts

        role = prompts.role_by_id("junior_draft", {})
        self.assertTrue(role["builtin"])
        self.assertFalse(role["edited"])
        self.assertIn("JUNIOR ENGINEER", role["system"])

    def test_editing_a_builtin_overrides_it_and_is_flagged(self):
        from aicouncil import prompts

        stored = {"junior_draft": {"name": "Junior Draft", "system": "Be brief."}}
        role = prompts.role_by_id("junior_draft", stored)
        self.assertEqual(role["system"], "Be brief.")
        self.assertTrue(role["builtin"])
        self.assertTrue(role["edited"])

    def test_an_edit_that_matches_the_shipped_text_is_not_an_edit(self):
        from aicouncil import prompts

        shipped = prompts.ROLE_TEMPLATES["junior_draft"]["system"]
        role = prompts.role_by_id("junior_draft", {"junior_draft": {"system": shipped}})
        self.assertFalse(role["edited"])

    def test_a_custom_role_joins_the_same_list(self):
        from aicouncil import prompts

        stored = {"perf": {"name": "Perf Reviewer", "summary": "Finds slow paths",
                           "system": "Profile first.", "writes": False}}
        catalog = prompts.role_catalog(stored)
        ids = [r["id"] for r in catalog]
        self.assertIn("perf", ids)
        self.assertIn("junior_draft", ids)
        perf = next(r for r in catalog if r["id"] == "perf")
        self.assertFalse(perf["builtin"])
        # Same shape as a built-in: the UI renders one list, not two.
        self.assertEqual(set(perf), set(catalog[0]))

    def test_a_custom_role_reaches_the_prompt(self):
        from aicouncil import prompts

        stored = {"perf": {"name": "Perf", "system": "PROFILE-FIRST-SENTINEL"}}
        text = prompts.resolve_system("drafter", {"role_template": "perf"}, stored)
        self.assertEqual(text, "PROFILE-FIRST-SENTINEL")

    def test_deleting_a_builtin_edit_restores_the_shipped_text(self):
        from aicouncil import prompts

        self.store.update({"roles": {"junior_draft": {"system": "Overridden."}}})
        self.assertEqual(
            prompts.role_by_id("junior_draft", self.store.get("roles"))["system"],
            "Overridden.",
        )
        # Deletion must *replace* the map: update() deep-merges and would
        # resurrect the key.
        self.store.replace_roles({})
        role = prompts.role_by_id("junior_draft", self.store.get("roles"))
        self.assertIn("JUNIOR ENGINEER", role["system"])
        self.assertFalse(role["edited"])

    def test_a_stage_pointing_at_a_deleted_role_falls_back(self):
        from aicouncil import prompts

        text = prompts.resolve_system("polisher", {"role_template": "gone"}, {})
        self.assertIn("SENIOR STAFF ARCHITECT", text)

    def test_an_empty_role_never_produces_an_empty_prompt(self):
        from aicouncil import prompts

        stored = {"hollow": {"name": "Hollow", "system": "   "}}
        text = prompts.resolve_system("drafter", {"role_template": "hollow"}, stored)
        self.assertTrue(text.strip())
        self.assertIn("JUNIOR ENGINEER", text)
