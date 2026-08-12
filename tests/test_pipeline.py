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
from aicouncil import router  # noqa: E402
from aicouncil.config import ConfigStore  # noqa: E402
from aicouncil.events import EventBus  # noqa: E402
from aicouncil.pipeline import (  # noqa: E402
    Pipeline,
    PipelineBusy,
    Run,
    VOLATILE_RUN_LIMIT,
)

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


def mock_council(agent: str, extra=None):
    """One CLI's council seat, pointed at the mock agent.

    No `--role`: a council turn is identified by what the prompt asks for, not
    by the chair it was sent to - the same CLI holds a member seat, critiques
    its peers and may chair, and which of those it is doing is decided per run
    by the router.
    """
    return {
        "id": cfg.council_provider_id(agent),
        "label": f"Mock {agent}",
        "command": [sys.executable, MOCK, *(extra or []), "{prompt}"],
        "auto_approve_args": ["--dangerously-skip-permissions"],
        "read_only_args": ["--read-only"],
        # Cleared for the same reason every real agent declares its own: these
        # merge onto the CLI preset, and Claude's `--output-format stream-json`
        # left on a command that is not Claude puts `stream-json` where the
        # prompt should be. The seat then runs on an 11-character task and
        # still exits zero, which is the worst kind of green.
        "stream_args": [],
        "prompt_on_stdin": False,
        "timeout_seconds": 60,
    }


def council_providers(extra_for=None):
    """The whole bench on the mock, optionally breaking one agent."""
    extra_for = extra_for or {}
    return {
        cfg.council_provider_id(a): mock_council(a, extra_for.get(a))
        for a in cfg.AGENTS
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
        # Anything else in the app that derives a path from the environment
        # stays pointed at the fixture too. Transcripts do not rely on this:
        # the pipeline below is handed its runs directory outright, so a run
        # winding down on its worker thread after this variable is restored
        # still cannot reach the developer's real history.
        previous_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "xdg")
        # The mock paces its output so the UI's streaming is visible when it is
        # driven by hand. A council run is seven stages rather than two, so that
        # pacing now dominates the suite's runtime. Nothing here is testing the
        # delay itself.
        previous_delay = os.environ.get("MOCK_AGENT_DELAY")
        os.environ["MOCK_AGENT_DELAY"] = "0"

        def restore_delay():
            if previous_delay is None:
                os.environ.pop("MOCK_AGENT_DELAY", None)
            else:
                os.environ["MOCK_AGENT_DELAY"] = previous_delay

        self.addCleanup(restore_delay)

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
            "workspace": str(self.repo),
            "safety_snapshot": True,
            "providers": council_providers(),
        })
        self.bus = EventBus()
        self.runs_dir = self.tmp / "xdg" / "ai-council" / "runs"
        self.pipeline = Pipeline(self.store, self.bus, runs_dir=self.runs_dir)

    # -- council helpers ---------------------------------------------------
    #
    # The bench is routed per run, so a test cannot name "seat2" and know which
    # CLI is in it. These ask the run what it seated instead, which is also the
    # only way an assertion stays true when the routing changes.

    def member_ids(self, run):
        """The Stage 1 stage ids of this run, in seating order."""
        return [s.id for s in run.seating.members]

    def member_stages(self, run):
        return [run.stages[s.id] for s in run.seating.members]

    def critique_stages(self, run):
        return [run.stages[f"{s.id}_critique"] for s in run.seating.members]

    def pin_chair(self, agent, provider=None):
        """Fix the chair to one CLI, and optionally break that CLI.

        The bench is routed, so "break the stage that writes" is no longer a
        thing a test can say by naming a provider - any of the three could be
        chairing. Pinning makes the intent precise, and sitting the chair out
        of the deliberation keeps the breakage on the chair alone rather than
        also taking out a member seat.
        """
        patch = {"council": {"pins": {"chair": agent}, "chair_deliberates": False}}
        if provider is not None:
            patch["providers"] = {cfg.council_provider_id(agent): provider}
        self.store.update(patch)

    def tearDown(self):
        self.pipeline.cancel()
        # Wait for the worker to actually exit, not just for the run to reach a
        # terminal state - it writes the transcript after that. A test that
        # returned mid-run would otherwise race its own fixture teardown.
        self.assertTrue(
            self.pipeline.wait_for_worker(),
            "the run worker thread did not wind down",
        )
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
        # The worker writes the transcript *after* the run reaches a terminal
        # state, so a test that reads one back - by name, or by continuing it -
        # is racing the thread unless it waits for the thread itself.
        self.assertTrue(
            self.pipeline.wait_for_worker(timeout),
            "the run worker thread did not wind down",
        )


class TestZeroTouch(PipelineTestBase):
    def test_full_run_applies_changes_without_a_gate(self):
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        for stage in self.member_stages(run):
            self.assertEqual(stage.state, "done", stage.error)
        for stage in self.critique_stages(run):
            self.assertEqual(stage.state, "done", stage.error)
        self.assertEqual(run.stages["chair"].state, "done")
        # Zero-Touch means the file really was written.
        self.assertTrue((self.repo / "AI_COUNCIL_DEMO.md").exists())
        self.assertIn("AI_COUNCIL_DEMO.md", run.diff)
        self.assertGreaterEqual(run.diff_stat["files"], 1)

    def test_only_the_chairman_is_granted_permission_to_write(self):
        # The permission model is positional: every deliberating seat is
        # read-only by contract *and* by flag, and the chairman is the only
        # thing that can change a file - even under Zero-Touch.
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("anything", str(self.repo))
        self.wait_terminal()

        for stage in self.member_stages(run) + self.critique_stages(run):
            self.assertNotIn(
                "--dangerously-skip-permissions", stage.command,
                f"{stage.id} was handed write permission",
            )
            self.assertIn(
                "--read-only", stage.command,
                f"{stage.id} was not invoked read-only",
            )
        self.assertIn(
            "--dangerously-skip-permissions", run.stages["chair"].command
        )
        self.assertNotIn("--read-only", run.stages["chair"].command)

    def test_a_critic_reviews_its_peers_and_never_itself(self):
        # Asserted on what each critic actually reported rather than on the
        # prompt it was given - the prompt is redacted out of `command`, and
        # the mock names every peer it was shown under its own heading. A
        # member reviewing itself under an alias, believing it a colleague, is
        # worse than not running the stage at all.
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()

        for seat in run.seating.members:
            reported = run.stages[f"{seat.id}_critique"].output
            self.assertNotIn(
                f"### {seat.alias}", reported,
                f"{seat.id} was handed its own answer to review",
            )
            for other in run.seating.members:
                if other.id != seat.id:
                    self.assertIn(f"### {other.alias}", reported)

    def test_the_chairman_reports_its_own_confidence(self):
        # Parsed off the reply, never computed here. A run whose chairman said
        # nothing reports None rather than a number this app invented.
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.confidence, 75)
        self.assertEqual(run.consensus, 80)
        # Both figures also ride on the chair's own stage record, which is what
        # the verdict card renders from - the run-level properties are a
        # convenience, not the source the UI reads.
        chair = run.stages["chair"]
        self.assertEqual(chair.confidence, 75)
        self.assertEqual(chair.consensus, 80)
        self.assertTrue(chair.because)
        # And a member states a confidence but never a consensus: it has not
        # seen the other members, so it has no view on how far they agreed.
        for stage in self.member_stages(run):
            self.assertIsNotNone(stage.confidence)
            self.assertIsNone(stage.consensus)

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
    """Solo is its own product, not the council with a stage switched off."""

    def setUp(self):
        super().setUp()
        assistant = mock_provider("solo", "Assistant")
        assistant["read_only_args"] = ["--read-only"]
        assistant["behavior"] = ""
        self.store.update({"mode": "solo", "providers": {"solo": assistant}})

    def test_solo_runs_its_own_provider_and_nothing_else(self):
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(list(run.stages), ["solo"])
        self.assertEqual(run.mode, "solo")
        self.assertTrue(run.stages["solo"].output.strip())

    def test_the_assistant_is_read_only_without_zero_touch(self):
        # The default, and what makes "what does this repo do?" safe to ask:
        # there is no gate to approve a write at, so nothing grants one.
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertFalse(run.zero_touch)
        self.assertNotIn(
            "--dangerously-skip-permissions", run.stages["solo"].command
        )
        self.assertIn("--read-only", run.stages["solo"].command)
        self.assertTrue(gitutil.status(self.repo).clean)

    def test_zero_touch_lets_the_assistant_write(self):
        # Chat has no gate, so Zero-Touch is the only thing that can grant it -
        # and when it does, the grant is the same one the council's writing
        # stage gets, not a weaker variant.
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertTrue(run.zero_touch)
        self.assertIn(
            "--dangerously-skip-permissions", run.stages["solo"].command
        )
        self.assertNotIn("--read-only", run.stages["solo"].command)
        # The two grants are opposites and must never both be sent.
        self.assertFalse(gitutil.status(self.repo).clean)

    def test_a_writing_conversation_is_protected_like_a_council_run(self):
        # Whatever writes gets the snapshot before it and the diff after it.
        # Offering one without the other would leave a run that changed files
        # with no way back and nothing to show for it.
        self.store.update({"zero_touch": True, "safety_snapshot": True})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertIsNotNone(run.snapshot)
        self.assertTrue(run.to_dict()["can_rollback"])
        self.assertTrue(run.diff.strip())
        self.assertTrue(run.diff_stat.get("files"))

    def test_no_gate_is_reached(self):
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)

    def test_no_delivery_state_is_collected(self):
        # Nothing was written, so a diff, a snapshot or a rollback offer would
        # all be attributing the operator's own tree to the assistant.
        self.store.update({"zero_touch": False})
        (self.repo / "scratch.txt").write_text("mine, not the assistant's\n")
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.diff, "")
        self.assertEqual(run.diff_stat, {})
        self.assertIsNone(run.snapshot)
        self.assertFalse(run.to_dict()["can_rollback"])

    def test_delivery_settings_do_not_block_a_read_only_conversation(self):
        # Refusing to answer a question because the tree is dirty would be
        # absurd, and a chat that cannot write has no branch to deliver.
        self.store.update({
            "zero_touch": False,
            "require_clean_worktree": True, "pull_request_mode": True,
        })
        (self.repo / "scratch.txt").write_text("uncommitted\n")
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertFalse(run.pull_request_mode)
        self.assertEqual(run.work_branch, "")

    def test_a_plain_message_reaches_the_agent_exactly_as_typed(self):
        # The command echo records the prompt's length, so this pins that no
        # persona, house rules or repository preamble were wrapped around it.
        message = "MAGIC-PLAIN-MESSAGE"
        self.store.update({"house_rules": "always use tabs"})
        run = self.pipeline.start(message, str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertIn(f"<prompt: {len(message)} chars>", run.stages["solo"].command)

    def test_a_conversation_cannot_change_mode_mid_thread(self):
        first = self.pipeline.start("hello", str(self.repo))
        self.wait_terminal()
        self.store.update({"mode": "council"})

        with self.assertRaisesRegex(ValueError, "solo conversation"):
            self.pipeline.start(
                "and now?", str(self.repo), continue_from=first.transcript_name
            )

    def test_a_council_run_cannot_be_continued_as_a_conversation(self):
        self.store.update({"mode": "council", "zero_touch": True})
        first = self.pipeline.start("do a thing", str(self.repo))
        self.wait_terminal()
        self.store.update({"mode": "solo"})

        with self.assertRaisesRegex(ValueError, "council conversation"):
            self.pipeline.start(
                "and now?", str(self.repo), continue_from=first.transcript_name
            )


class TestMultiAgentChat(PipelineTestBase):
    """One message, every installed CLI, three answers left as three answers."""

    def setUp(self):
        super().setUp()
        assistant = mock_provider("solo", "Assistant")
        assistant["read_only_args"] = ["--read-only"]
        assistant["behavior"] = ""
        # Both halves of the config: the per-CLI cards the bench reads, and the
        # single assistant it falls back to when only one CLI is installed.
        self.store.update({
            "mode": "solo",
            "multi_agent": True,
            "providers": {"solo": assistant, **council_providers()},
        })

    def test_every_installed_agent_answers_the_same_message(self):
        run = self.pipeline.start("what is this repo?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(
            sorted(run.stages),
            sorted(f"chat_{agent}" for agent in cfg.AGENTS),
        )
        self.assertNotIn("solo", run.stages)
        for stage in run.stages.values():
            self.assertTrue(stage.output.strip(), stage.id)
            self.assertEqual(stage.kind, "chat")

    def test_the_bench_is_read_only_even_with_zero_touch_on(self):
        # Three agents editing one folder at once, with nothing arbitrating
        # between them, is a race rather than a feature - so the grant Chat
        # would otherwise get is refused for this shape of turn.
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        for stage in run.stages.values():
            self.assertIn("--read-only", stage.command)
            self.assertNotIn("--dangerously-skip-permissions", stage.command)
        self.assertTrue(gitutil.status(self.repo).clean)
        self.assertIsNone(run.snapshot)
        self.assertEqual(run.diff, "")

    def test_one_agent_installed_falls_back_to_the_single_assistant(self):
        # A bench of one is the ordinary Chat assistant. Refusing to answer
        # because two CLIs are missing would be worse than answering.
        only = council_providers()[cfg.council_provider_id("codex")]
        self.store.update({
            "providers": {
                cfg.council_provider_id(a): {
                    **council_providers()[cfg.council_provider_id(a)],
                    "enabled": a == "codex",
                }
                for a in cfg.AGENTS
            },
        })
        self.assertTrue(only["command"])
        run = self.pipeline.start("hello", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(list(run.stages), ["solo"])

    def test_all_three_answers_carry_into_the_next_turn(self):
        first = self.pipeline.start("first question", str(self.repo))
        self.wait_terminal()
        second = self.pipeline.start(
            "and now?", str(self.repo), continue_from=first.transcript_name
        )
        self.wait_terminal()

        self.assertEqual(second.state, "complete", second.error)
        self.assertEqual(len(second.conversation), 1)
        replies = second.conversation[0]["replies"]
        self.assertEqual(
            sorted(r["stage"] for r in replies),
            sorted(f"chat_{agent}" for agent in cfg.AGENTS),
        )
        # Labelled, not merged: a follow-up has to be able to say "Codex was
        # right" and have that mean something.
        for reply in replies:
            self.assertTrue(reply["label"].strip())
            self.assertTrue(reply["output"].strip())

    def test_the_toggle_off_is_the_ordinary_single_assistant(self):
        self.store.update({"multi_agent": False})
        run = self.pipeline.start("hello", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(list(run.stages), ["solo"])


class TestAgentsAreOptIn(PipelineTestBase):
    """An installed CLI is not a seated one.

    Every other test in this file drives hand-written commands, which answer to
    nobody's selection by design - that is what keeps the mock agent usable
    with no vendor CLI added at all. These point the bench at the shipped
    catalogued commands instead, over stand-in binaries, because the question
    here is exactly whether being on PATH is enough. It is not: the operator
    saying so in Settings is.
    """

    def setUp(self):
        super().setUp()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for agent in cfg.AGENTS:
            fake = bindir / agent
            fake.write_text(f"#!{sys.executable}\nprint('ok')\n", encoding="utf-8")
            fake.chmod(0o755)
        previous = os.environ["PATH"]
        self.addCleanup(lambda: os.environ.__setitem__("PATH", previous))
        os.environ["PATH"] = f"{bindir}{os.pathsep}{previous}"
        # Back to the shipped commands: `agent_for` reads the CLI off the
        # command, and the mock's `python3` is nobody's.
        self.store.update({
            "providers": {
                pid: {"command": list(seat["command"])}
                for pid, seat in cfg.DEFAULT_COUNCIL_PROVIDERS.items()
            },
        })

    def test_an_installed_agent_nobody_added_is_not_seated(self):
        self.assertEqual(self.pipeline.available_agents(self.store.all()), [])

    def test_one_added_agent_is_the_whole_bench(self):
        conf = self.store.update({"agent_settings": {"claude": {"selected": True}}})
        self.assertEqual(self.pipeline.available_agents(conf), ["claude"])

    def test_a_council_with_nobody_added_says_what_to_do_about_it(self):
        with self.assertRaises(ValueError) as caught:
            self.pipeline.seat_council("what is a monad?", self.store.all())
        self.assertIn("Settings", str(caught.exception))

    def test_chat_refuses_before_launching_an_agent_nobody_added(self):
        # Said up front rather than after the CLI fails: "not connected" is a
        # different problem from "not installed", and only one of them is
        # fixed by installing something.
        self.store.update({"mode": "solo"})
        with self.assertRaises(ValueError) as caught:
            self.pipeline.start("hello", str(self.repo))
        self.assertIn("not connected", str(caught.exception))


class TestWorkingFolderIsOptional(PipelineTestBase):
    """A repository is what makes a run reviewable, not what makes it possible.

    Both modes have to start with no folder chosen and in a folder that has no
    git in it. What those runs lose is the git-backed half of the safety model
    - diff, snapshot, rollback, pull-request delivery - and the engine has to
    say so rather than fail, half-apply, or offer a rollback that cannot work.
    """

    def setUp(self):
        super().setUp()
        self.plain = self.tmp / "just-a-folder"
        self.plain.mkdir()
        assistant = mock_provider("solo", "Assistant")
        assistant["read_only_args"] = ["--read-only"]
        self.store.update({"providers": {"solo": assistant}})

    def test_a_council_run_works_in_a_folder_with_no_git(self):
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("add a demo artifact", str(self.plain))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(run.workspace, str(self.plain.resolve()))
        self.assertTrue((self.plain / "AI_COUNCIL_DEMO.md").exists())
        # Nothing to diff against and nothing to restore, and neither is
        # reported as an error - they are simply absent.
        self.assertEqual(run.diff, "")
        self.assertIsNone(run.snapshot)
        self.assertFalse(run.snapshot_planned)
        self.assertFalse(run.to_dict()["can_rollback"])

    def test_a_council_run_works_with_no_folder_chosen_at_all(self):
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("add a demo artifact")
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(run.workspace, str(cfg.workspace_dir()))
        self.assertTrue(run.stages["chair"].output.strip())

    def test_no_folder_chosen_means_nothing_is_written(self):
        # The choice the operator actually made: ask the council a question,
        # get an answer. Zero-Touch grants permission to write in the folder
        # they picked, and they picked none - so there is nothing to grant.
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("add a demo artifact")
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertTrue(run.read_only)
        self.assertFalse((cfg.workspace_dir() / "AI_COUNCIL_DEMO.md").exists())
        self.assertIn("--read-only", run.stages["chair"].command)
        self.assertNotIn(
            "--dangerously-skip-permissions", run.stages["chair"].command
        )
        # No permission means no delivery state either: a diff, a snapshot or a
        # rollback offer would all describe work that did not happen.
        self.assertEqual(run.diff, "")
        self.assertIsNone(run.snapshot)
        self.assertFalse(run.snapshot_planned)
        self.assertFalse(run.to_dict()["can_rollback"])

    def test_a_no_folder_run_is_not_stopped_at_the_gate(self):
        # The gate exists to stand between the deliberation and the only stage
        # that writes. Nothing writes here, so asking would be asking the
        # operator to approve nothing.
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("add a demo artifact")
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertFalse(run.approved)
        self.assertFalse((cfg.workspace_dir() / "AI_COUNCIL_DEMO.md").exists())

    def test_a_conversation_writes_nothing_with_no_folder_and_zero_touch(self):
        # Chat's only grant is Zero-Touch, and it is a grant over a folder.
        # Without one there is nothing it can be a grant over.
        self.store.update({"mode": "solo", "zero_touch": True})
        run = self.pipeline.start("add a demo artifact")
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertTrue(run.read_only)
        self.assertIn("--read-only", run.stages["solo"].command)
        self.assertNotIn(
            "--dangerously-skip-permissions", run.stages["solo"].command
        )
        self.assertFalse((cfg.workspace_dir() / "AI_COUNCIL_DEMO.md").exists())

    def test_continuing_a_no_folder_run_after_a_restart_still_writes_nothing(self):
        # The run is rebuilt from its transcript, and a rebuilt run that came
        # back with write permission would apply, after a restart, work the
        # operator never asked to have applied anywhere.
        self.store.update({"zero_touch": True})
        self.pin_chair("claude", mock_council("claude", ["--fail"]))
        run = self.pipeline.start("add a demo artifact")
        self.wait_terminal()
        self.assertEqual(run.state, "failed")
        self.store.update({
            "providers": {cfg.council_provider_id("claude"): mock_council("claude")},
        })

        restarted = Pipeline(self.store, self.bus, runs_dir=self.runs_dir)
        self.addCleanup(restarted.wait_for_worker)
        revived = restarted.revive(run.transcript_name)
        self.wait_for(
            lambda: not restarted.is_busy(), what="the revived run to finish"
        )
        restarted.wait_for_worker()

        self.assertEqual(revived.state, "complete", revived.error)
        self.assertTrue(revived.read_only)
        self.assertIn("--read-only", revived.stages["chair"].command)
        self.assertFalse((cfg.workspace_dir() / "AI_COUNCIL_DEMO.md").exists())

    def test_a_transcript_written_before_the_flag_is_still_read_only(self):
        # Transcripts on disk predate the rule. The scratch workspace stands in
        # for the missing flag: it is the folder nobody chose.
        self.store.update({"zero_touch": True})
        self.pin_chair("claude", mock_council("claude", ["--fail"]))
        run = self.pipeline.start("add a demo artifact")
        self.wait_terminal()
        path = self.runs_dir / run.transcript_name
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("read_only", None)
        path.write_text(json.dumps(data), encoding="utf-8")

        restarted = Pipeline(self.store, self.bus, runs_dir=self.runs_dir)
        self.addCleanup(restarted.wait_for_worker)
        revived = restarted.revive(run.transcript_name, start=False)

        self.assertTrue(revived.read_only)

    def test_a_conversation_needs_no_folder(self):
        self.store.update({"mode": "solo"})
        run = self.pipeline.start("what is a monad?")
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(run.workspace, str(cfg.workspace_dir()))
        self.assertTrue(run.stages["solo"].output.strip())

    def test_the_scratch_workspace_is_not_remembered_as_a_choice(self):
        # Storing the folder a blank setting resolved to would pin a folder
        # nobody picked, and the next run would open with it selected.
        self.store.update({"mode": "solo", "workspace": ""})
        self.pipeline.start("hello", "")
        self.wait_terminal()

        self.assertEqual(self.store.get("workspace"), "")
        self.assertEqual(self.store.get("recent_workspaces"), [])

    def test_a_chosen_folder_is_remembered(self):
        self.store.update({"zero_touch": True})
        self.pipeline.start("add a demo artifact", str(self.plain))
        self.wait_terminal()

        self.assertEqual(self.store.get("workspace"), str(self.plain.resolve()))
        self.assertIn(str(self.plain.resolve()), self.store.get("recent_workspaces"))

    def test_pull_request_mode_refuses_a_folder_with_no_git(self):
        # Refused before either agent spends quota, and with a reason that
        # names the toggle rather than reporting a bare git error.
        self.store.update({"pull_request_mode": True})
        with self.assertRaises(ValueError) as ctx:
            self.pipeline.start("go", str(self.plain))
        self.assertIn("not a git repository", str(ctx.exception))

    def test_the_prompt_tells_the_agents_there_is_no_repository(self):
        # An agent that assumes a git history will go looking for one, and
        # will assume its work is about to be reviewed as a diff.
        from aicouncil import prompts

        prompt = prompts.build_draft_prompt(
            "add a demo artifact",
            str(self.plain),
            gitutil.status(self.plain).to_dict(),
        )
        self.assertIn(f"Working folder: {self.plain}", prompt)
        self.assertIn("not a git repository", prompt)


class TestIncognito(PipelineTestBase):
    """A private conversation leaves no record in either place it could.

    Two records exist: this app's own transcripts, and the history each CLI
    keeps of its own sessions. A mode that suppressed one and not the other
    would be worth less than nothing, because the operator would believe it had
    suppressed both.
    """

    def setUp(self):
        super().setUp()
        # The mock stands in for a CLI that can be told not to save. It ignores
        # the flag itself; what is under test is that the flag is passed, and
        # that an agent without one is left out rather than run anyway.
        self.store.update({
            "providers": {
                cfg.council_provider_id(a): dict(
                    mock_council(a), incognito_args=["--incognito"]
                )
                for a in cfg.AGENTS
            },
        })

    def transcripts(self):
        return sorted(p.name for p in self.runs_dir.glob("*.json"))

    def test_a_private_run_writes_no_transcript(self):
        run = self.pipeline.start("what is a monad?", incognito=True)
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertTrue(run.incognito)
        self.assertEqual(self.transcripts(), [])

    def test_an_ordinary_run_still_writes_one(self):
        # The control. Without it "no transcript" could mean the engine had
        # stopped writing them at all.
        run = self.pipeline.start("what is a monad?")
        self.wait_terminal()

        self.assertEqual(self.transcripts(), [run.transcript_name])

    def test_every_stage_is_told_not_to_save(self):
        run = self.pipeline.start("what is a monad?", incognito=True)
        self.wait_terminal()

        for stage in run.stages.values():
            self.assertIn(
                "--incognito", stage.command,
                f"{stage.id} was run without its no-save flag",
            )

    def test_a_private_conversation_is_not_listed(self):
        self.pipeline.start("what is a monad?", incognito=True)
        self.wait_terminal()

        self.assertEqual(self.pipeline.history(), [])

    def test_a_private_conversation_can_still_be_continued(self):
        # Continuing is how a conversation works at all: the browser attaches
        # the last run by the name its transcript would have had. Held in
        # memory rather than on disk, so it survives the session and nothing
        # else.
        first = self.pipeline.start("what is a monad?", incognito=True)
        self.wait_terminal()
        second = self.pipeline.start(
            "and a functor?", continue_from=first.transcript_name
        )
        self.wait_terminal()

        self.assertEqual(second.state, "complete", second.error)
        self.assertEqual(len(second.conversation), 1)
        self.assertEqual(second.conversation[0]["task"], "what is a monad?")
        # The follow-up asked for no privacy of its own and gets it anyway: it
        # carries the earlier turns of a conversation that was promised none.
        self.assertTrue(second.incognito)
        self.assertEqual(self.transcripts(), [])

    def test_the_router_learns_nothing_from_a_private_run(self):
        # The seating history is a persistent record of who sat and how it
        # went, which is the record this run was told not to leave.
        before = self.store.get("council", {}).get("stats", {})
        self.pipeline.start("what is a monad?", incognito=True)
        self.wait_terminal()

        self.assertEqual(self.store.get("council", {}).get("stats", {}), before)

    def test_an_ordinary_run_still_teaches_the_router(self):
        self.pipeline.start("what is a monad?")
        self.wait_terminal()

        self.assertTrue(self.store.get("council", {}).get("stats"))

    def test_an_agent_that_cannot_run_incognito_is_not_seated(self):
        # Left out for this run only, and for a reason of the run rather than
        # of the agent: its binary is installed, enabled and working.
        self.store.update({
            "providers": {
                cfg.council_provider_id("agy"): dict(
                    mock_council("agy"), incognito_args=[]
                ),
            },
            "council": {"seat_count": 3, "chair_deliberates": True},
        })
        seating = self.pipeline.seat_council(
            "what is a monad?", self.store.all(), incognito=True
        )

        seated = {s.agent for s in seating.seats}
        self.assertNotIn("agy", seated)
        self.assertTrue(seated)

    def test_the_same_agent_is_seated_when_the_run_is_not_private(self):
        self.store.update({
            "providers": {
                cfg.council_provider_id("agy"): dict(
                    mock_council("agy"), incognito_args=[]
                ),
            },
        })
        seating = self.pipeline.seat_council("what is a monad?", self.store.all())

        self.assertIn("agy", {s.agent for s in seating.seats})

    def test_a_bench_with_nobody_left_says_why(self):
        # Rather than the router's "check the agents in Settings", which would
        # send the operator looking for a CLI that is working perfectly well.
        self.store.update({
            "providers": {
                cfg.council_provider_id(a): dict(mock_council(a), incognito_args=[])
                for a in cfg.AGENTS
            },
        })
        with self.assertRaises(ValueError) as ctx:
            self.pipeline.start("what is a monad?", incognito=True)
        self.assertIn("incognito", str(ctx.exception))

    def test_a_private_chat_turn_is_private_too(self):
        assistant = mock_provider("solo", "Assistant")
        assistant["read_only_args"] = ["--read-only"]
        assistant["incognito_args"] = ["--incognito"]
        self.store.update({"mode": "solo", "providers": {"solo": assistant}})

        run = self.pipeline.start("what is a monad?", incognito=True)
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertIn("--incognito", run.stages["solo"].command)
        self.assertEqual(self.transcripts(), [])

    def test_only_the_newest_private_runs_are_held_in_memory(self):
        # Nothing on disk exists to clean these up later, so the map is bounded
        # rather than left to grow for as long as the app is open.
        made = []
        for i in range(VOLATILE_RUN_LIMIT + 3):
            run = Run(
                id=f"{i:04d}", task="private", workspace=str(self.repo),
                zero_touch=False, incognito=True,
            )
            made.append(run.transcript_name)
            self.pipeline._persist(run)

        held = list(self.pipeline._volatile_runs)
        self.assertEqual(len(held), VOLATILE_RUN_LIMIT)
        self.assertEqual(held, made[-VOLATILE_RUN_LIMIT:])
        self.assertIsNone(self.pipeline.load_run(made[0]))
        self.assertIsNotNone(self.pipeline.load_run(made[-1]))


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

    def test_an_enormous_diff_is_capped_rather_than_read_whole(self):
        # The point of the cap is memory, not display: a staged 500 MB asset
        # used to be a 500 MB string before anything clipped it. Asserting the
        # returned length proves git was stopped at the cap.
        big = self.repo / "big.txt"
        big.write_text("original\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-qm", "add big"], self.repo)
        big.write_text("".join(f"line {i}\n" for i in range(200_000)))

        diff = gitutil.working_diff(self.repo, max_bytes=20_000)
        self.assertIn("diff truncated for display", diff)
        self.assertLess(len(diff), 21_000)


class TestPrivateOnDisk(PipelineTestBase):
    """What this app writes is private: the task, every stage's output and the
    full diff of a repository that is not ours to leak to the next login."""

    @unittest.skipUnless(os.name == "posix", "file modes are a POSIX concept")
    def test_transcripts_and_config_are_owner_only(self):
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()

        transcript = self.runs_dir / run.transcript_name
        self.assertEqual(transcript.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.runs_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "posix", "file modes are a POSIX concept")
    def test_a_directory_an_earlier_version_left_open_is_tightened(self):
        loose = self.tmp / "loose-runs"
        loose.mkdir(mode=0o755)
        cfg.private_dir(loose)
        self.assertEqual(loose.stat().st_mode & 0o777, 0o700)


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
        # The whole council has spoken and none of it touched a file.
        for stage in self.member_stages(run):
            self.assertTrue(stage.output.strip(), f"{stage.id} said nothing")
        self.assertEqual(run.stages["chair"].state, "pending")

        self.pipeline.approve("also mention the reviewer note")
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        self.assertTrue((self.repo / "AI_COUNCIL_DEMO.md").exists())

    def test_approval_grants_execute_permission_to_the_chairman(self):
        # With Zero-Touch off, the flag must still be passed *after* the human
        # approves - otherwise the CLI would block on an interactive prompt.
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("do the thing", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")
        self.pipeline.approve()
        self.wait_terminal()
        self.assertIn(
            "--dangerously-skip-permissions", run.stages["chair"].command
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
        self.assertEqual(run.stages["chair"].state, "skipped")

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

        # Swap the command under every seat the chair could be sitting in.
        self.store.update({
            "providers": {
                cfg.council_provider_id(a):
                    {"command": ["definitely-not-the-approved-command"]}
                for a in cfg.AGENTS
            },
        })
        self.pipeline.approve()
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertNotIn(
            "definitely-not-the-approved-command", run.stages["chair"].command
        )


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

    def test_a_chairman_that_changes_nothing_opens_no_pull_request(self):
        self.pin_chair("claude", mock_council("claude", ["--fail"]))
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
    def test_failing_chairman_marks_the_run_failed(self):
        self.store.update({"zero_touch": True})
        self.pin_chair("claude", mock_council("claude", ["--fail"]))
        run = self.pipeline.start("this will fail", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.stages["chair"].state, "failed")
        self.assertTrue(run.error)

    def test_one_failing_member_does_not_abort_the_council(self):
        # A seat that dies is simply not at the table for the rest of the run.
        # It is not replaced: a substitute would not have deliberated
        # independently with the others, which is the only thing Stage 1 buys.
        self.store.update({
            "zero_touch": True,
            "providers": {
                cfg.council_provider_id("codex"): mock_council("codex", ["--fail"]),
            },
        })
        run = self.pipeline.start("carry on regardless", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        states = {s.agent: s.state for s in self.member_stages(run)}
        self.assertEqual(states.get("codex"), "failed")
        self.assertIn("done", states.values())
        self.assertTrue((self.repo / "AI_COUNCIL_DEMO.md").exists())

    def test_a_council_with_no_surviving_member_degrades_to_the_gate(self):
        # Zero-Touch assumes a deliberation to synthesise. With none, the run
        # pauses for a human rather than letting one agent write to the repo
        # with nobody watching - a combination nobody selected.
        self.store.update({
            "zero_touch": True,
            "providers": {
                cfg.council_provider_id(a): mock_council(a, ["--fail"])
                for a in cfg.AGENTS
            },
        })
        self.pin_chair("claude")
        run = self.pipeline.start("everyone fails", str(self.repo))
        self.wait_for(
            lambda: run.state == "awaiting_approval", what="the degraded gate"
        )
        self.assertFalse((self.repo / "AI_COUNCIL_DEMO.md").exists())
        self.pipeline.reject()
        self.wait_terminal()

    def test_a_lone_position_skips_the_critique_stage_and_stops_at_the_gate(self):
        # One member handed its own answer back under an alias would be
        # reviewing itself while believing it a colleague. Skipping is right.
        # Chair sits out, so the bench is codex and agy - and only agy answers.
        # One voice is not a quorum, so Zero-Touch pauses rather than letting a
        # single unreviewed opinion be written unattended.
        self.store.update({
            "zero_touch": True,
            "providers": {
                cfg.council_provider_id("codex"): mock_council("codex", ["--fail"]),
            },
        })
        self.pin_chair("claude")
        run = self.pipeline.start("only one survives", str(self.repo))
        self.wait_for(
            lambda: run.state == "awaiting_approval", what="the quorum gate"
        )
        self.assertFalse((self.repo / "AI_COUNCIL_DEMO.md").exists())
        self.pipeline.approve()
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        for stage in self.critique_stages(run):
            self.assertIn(stage.state, ("skipped", "failed"))

    def test_two_seats_on_one_cli_are_not_a_quorum(self):
        # A machine with one CLI installed gets two seats on it, which is two
        # correlated answers rather than two votes. Zero-Touch stops for a
        # human rather than writing on a council that never disagreed.
        self.store.update({
            "zero_touch": True,
            "providers": {
                cfg.council_provider_id(a): {
                    "command": ["definitely-not-a-real-binary-xyz", "{prompt}"],
                }
                for a in cfg.AGENTS
                if a != "codex"
            },
        })
        run = self.pipeline.start("one voice only", str(self.repo))
        self.wait_for(
            lambda: run.state == "awaiting_approval", what="the quorum gate"
        )
        self.assertEqual({s.agent for s in run.seating.members}, {"codex"})
        self.assertFalse((self.repo / "AI_COUNCIL_DEMO.md").exists())
        self.pipeline.reject()
        self.wait_terminal()

    def test_a_silent_member_is_failed_not_counted_as_an_answer(self):
        # Exit 0 with an empty stdout is what a CLI that hit its own quota
        # wall does. Counted as success it costs twice: the seat is dropped
        # from the deliberation anyway, while the transcript shows it green
        # and its critique card is left pending with no explanation.
        self.store.update({
            "zero_touch": True,
            "providers": {
                cfg.council_provider_id("agy"): mock_council("agy", ["--silent"]),
            },
        })
        run = self.pipeline.start("one seat says nothing", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        states = {s.agent: s.state for s in self.member_stages(run)}
        self.assertEqual(states.get("agy"), "failed")
        silent = [s for s in run.seating.members if s.agent == "agy"][0]
        critique = run.stages[f"{silent.id}_critique"]
        self.assertEqual(critique.state, "skipped")
        self.assertIn("no peers", critique.error)
        # The seats that did answer still cross-evaluated.
        answered = [
            run.stages[f"{s.id}_critique"]
            for s in run.seating.members
            if s.agent != "agy"
        ]
        self.assertTrue(all(s.state == "done" for s in answered))

    def test_a_silent_member_that_said_why_has_its_reason_carried(self):
        # The half of "printed nothing" that is actually actionable. A CLI
        # that names the permission it was denied has told the operator how to
        # fix it; replacing that with a generic sentence about empty output
        # turns a two-minute settings change into a mystery.
        reason = 'a tool required the "read_file" permission and was auto-denied'
        self.store.update({
            "zero_touch": True,
            "providers": {
                cfg.council_provider_id("agy"): mock_council(
                    "agy", ["--silent-reason", reason]
                ),
            },
        })
        run = self.pipeline.start("one seat is denied its tools", str(self.repo))
        self.wait_terminal()

        silent = [s for s in self.member_stages(run) if s.agent == "agy"][0]
        self.assertEqual(silent.state, "failed")
        self.assertIn("read_file", silent.error)
        self.assertIn("printed nothing", silent.error)

    def test_no_installed_cli_refuses_to_start(self):
        # The router seats only CLIs that actually resolve, so a machine with
        # none of them says so before a run begins rather than failing at the
        # first seat. This is stricter than the two-stage council was: it used
        # to start, run, and report the failure per stage.
        self.store.update({
            "zero_touch": True,
            "providers": {
                cfg.council_provider_id(a): {
                    "command": ["definitely-not-a-real-binary-xyz", "{prompt}"],
                }
                for a in cfg.AGENTS
            },
        })
        with self.assertRaises(ValueError) as ctx:
            self.pipeline.start("go", str(self.repo))
        self.assertIn("nobody to seat", str(ctx.exception))

    def test_unusable_provider_config_finishes_the_stage(self):
        # A stage that cannot be launched must still end. It used to be left
        # marked "running" forever, with no end time and no error on it.
        #
        # Asserted on a member rather than the chair: the binary has to resolve
        # or the router would not seat it at all, and the chair's timeout is
        # replaced by `council.chair_timeout_seconds` on the way in - so the
        # chair cannot carry a broken one.
        self.store.update({
            "zero_touch": True,
            "providers": {
                cfg.council_provider_id("codex"): dict(
                    mock_council("codex"), timeout_seconds="not a number"
                ),
            },
        })
        run = self.pipeline.start("go", str(self.repo))
        self.wait_terminal()

        broken = [s for s in self.member_stages(run) if s.agent == "codex"]
        self.assertTrue(broken, "codex was not seated")
        for stage in broken:
            self.assertEqual(stage.state, "failed")
            self.assertTrue(stage.ended_at, "a stage that failed to launch never ended")
            self.assertIn("misconfigured", stage.error)

    def test_a_folder_that_does_not_exist_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.pipeline.start("go", str(self.tmp / "nowhere"))
        self.assertIn("not a folder that exists", str(ctx.exception))

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


class TestContinuingAFailedRun(PipelineTestBase):
    """Continue: run what failed again, and nothing that did not.

    The reason it exists is money. A council that loses only its chairman - to
    a quota wall, a timeout, a CLI that fell over - has already paid for every
    member position and every peer critique, and starting again spends all of
    it a second time to get back to where it stopped.
    """

    def fail_the_chair(self):
        self.store.update({"zero_touch": True})
        self.pin_chair("claude", mock_council("claude", ["--fail"]))
        run = self.pipeline.start("write the greeting", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "failed")
        return run

    def mend_the_chair(self):
        self.store.update({
            "providers": {cfg.council_provider_id("claude"): mock_council("claude")},
        })

    def test_only_the_failed_stage_is_offered(self):
        run = self.fail_the_chair()
        self.assertTrue(run.can_resume)
        self.assertEqual(run.unfinished_stages, ["chair"])

    def test_a_finished_run_has_nothing_to_continue(self):
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("finish cleanly", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        self.assertFalse(run.can_resume)
        with self.assertRaises(ValueError):
            self.pipeline.resume()

    def test_the_members_are_not_asked_again(self):
        # The whole saving, asserted on the clock: a reused stage keeps the
        # timestamps of the attempt that actually ran it.
        run = self.fail_the_chair()
        before = {s.id: (s.started_at, s.output) for s in self.member_stages(run)}
        self.mend_the_chair()

        self.pipeline.resume()
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        after = {s.id: (s.started_at, s.output) for s in self.member_stages(run)}
        self.assertEqual(before, after)
        self.assertEqual(run.stages["chair"].state, "done")
        self.assertTrue(run.stages["chair"].output.strip())

    def test_a_continued_run_keeps_its_identity(self):
        # One run, one id, one transcript. Two half-runs the operator has to
        # line up by hand would lose the thing the transcript is for.
        run = self.fail_the_chair()
        run_id = run.id
        self.mend_the_chair()

        resumed = self.pipeline.resume()
        self.wait_terminal()

        self.assertIs(resumed, run)
        self.assertEqual(resumed.id, run_id)
        self.assertEqual(resumed.resumed, 1)

    def test_continuing_picks_up_a_seat_pointed_at_a_working_command(self):
        # Why providers are re-read on continue and nothing else is: the fix
        # for "this CLI hit its quota" is to change what that seat runs, and a
        # frozen command would continue straight back into the same wall.
        run = self.fail_the_chair()
        self.assertIn("chair", run.unfinished_stages)
        self.mend_the_chair()

        self.pipeline.resume()
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertTrue((self.repo / "AI_COUNCIL_DEMO.md").exists())

    def test_a_failed_chair_that_fails_again_can_be_continued_again(self):
        run = self.fail_the_chair()
        self.pipeline.resume()
        self.wait_terminal()

        self.assertEqual(run.state, "failed")
        self.assertEqual(run.resumed, 1)
        self.assertTrue(run.can_resume)

        self.mend_the_chair()
        self.pipeline.resume()
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(run.resumed, 2)

    def test_the_gate_is_not_asked_twice(self):
        # A human approved this bench and these positions once. Continuing
        # reuses both unchanged, so asking again would be asking about
        # something that has not changed.
        self.pin_chair("claude", mock_council("claude", ["--fail"]))
        run = self.pipeline.start("needs approval", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval", what="the gate")
        self.pipeline.approve("go ahead")
        self.wait_terminal()
        self.assertEqual(run.state, "failed")
        self.assertTrue(run.approved)

        self.mend_the_chair()
        self.pipeline.resume()
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        # And the grant survived with it: the chairman was invoked with the
        # auto-approve flag the human granted, not read-only.
        self.assertIn(
            "--dangerously-skip-permissions", run.stages["chair"].command
        )

    def test_the_first_attempts_snapshot_is_kept(self):
        # Rollback after a continuation must undo the whole run, including
        # whatever the first, failed chairman had already written.
        run = self.fail_the_chair()
        snapshot = run.snapshot
        self.assertIsNotNone(snapshot)
        self.mend_the_chair()

        self.pipeline.resume()
        self.wait_terminal()

        self.assertIs(run.snapshot, snapshot)

    def test_nothing_to_continue_without_a_run(self):
        with self.assertRaises(ValueError):
            self.pipeline.resume()

    def test_a_failed_run_can_be_continued_after_a_restart(self):
        # The case in-memory continuation cannot cover: the app was closed
        # between the failure and the decision to continue. A fresh Pipeline
        # standing in for the restart - same runs directory, no memory of the
        # run - still finishes it without asking the members again.
        run = self.fail_the_chair()
        name = run.transcript_name
        members = {s.id: s.output for s in self.member_stages(run)}
        self.mend_the_chair()

        restarted = Pipeline(self.store, self.bus, runs_dir=self.runs_dir)
        self.addCleanup(restarted.wait_for_worker)
        revived = restarted.revive(name)
        self.wait_for(
            lambda: not restarted.is_busy(), what="the revived run to finish"
        )
        restarted.wait_for_worker()

        self.assertEqual(revived.state, "complete", revived.error)
        self.assertEqual(revived.id, run.id)
        self.assertEqual(
            {s.id: s.output for s in self.member_stages(revived)}, members
        )
        self.assertTrue(revived.stages["chair"].output.strip())

    def test_a_finished_transcript_is_not_revived(self):
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("finish cleanly", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)

        with self.assertRaises(ValueError):
            self.pipeline.revive(run.transcript_name)

    def test_reviving_a_transcript_that_is_gone_says_so(self):
        with self.assertRaises(ValueError):
            self.pipeline.revive("1700000000-nosuchrun.json")

    def test_a_transcript_written_before_continue_existed_can_still_continue(self):
        # The runs that need this most are the ones already on disk: a council
        # that paid for six answers and lost its chairman to a quota wall,
        # written by a version that had no `can_resume` to record. Answering
        # from the stored key would call those unusable, which is backwards.
        run = self.fail_the_chair()
        path = self.runs_dir / run.transcript_name
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("can_resume", None)
        data.pop("unfinished_stages", None)
        path.write_text(json.dumps(data), encoding="utf-8")

        loaded = self.pipeline.load_run(run.transcript_name)
        self.assertTrue(loaded["can_resume"])
        self.assertEqual(loaded["unfinished_stages"], ["chair"])

        # And it is not merely labelled continuable - it continues.
        self.mend_the_chair()
        restarted = Pipeline(self.store, self.bus, runs_dir=self.runs_dir)
        self.addCleanup(restarted.wait_for_worker)
        revived = restarted.revive(run.transcript_name)
        self.wait_for(lambda: not restarted.is_busy(), what="the revived run")
        restarted.wait_for_worker()
        self.assertEqual(revived.state, "complete", revived.error)

    def test_the_conversation_list_says_which_rows_can_continue(self):
        # Discoverability: whoever needs this is scanning a list of
        # conversations for the one they gave up on, not opening each in turn.
        run = self.fail_the_chair()
        rows = self.pipeline.history(mode="council")
        row = next(r for r in rows if r["file"] == run.transcript_name)
        self.assertTrue(row["can_resume"])

    def test_a_completed_run_is_not_offered_in_the_list(self):
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("finish cleanly", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)

        rows = self.pipeline.history(mode="council")
        row = next(r for r in rows if r["file"] == run.transcript_name)
        self.assertFalse(row["can_resume"])

    def test_a_pull_request_run_is_not_revived_after_a_restart(self):
        # Its branch, its commits and possibly a published PR are all outside
        # the transcript. Half-reconstructing that would be a guess about the
        # repository, so it is refused in words the operator can act on.
        run = self.fail_the_chair()
        path = self.runs_dir / run.transcript_name
        data = json.loads(path.read_text(encoding="utf-8"))
        data["pull_request_mode"] = True
        data["work_branch"] = "council/whatever"
        path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            self.pipeline.revive(run.transcript_name)
        self.assertIn("pull request", str(caught.exception).lower())

    def test_a_failed_chat_turn_can_be_continued_too(self):
        # Chat has one stage, so there is nothing to reuse - but the message,
        # the thread and the folder are all still here, and retyping them is
        # the thing continuing is meant to save.
        self.store.update({
            "mode": "solo",
            "providers": {"solo": mock_provider("solo", "Assistant", ["--fail"])},
        })
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "failed")
        self.assertTrue(run.can_resume)

        self.store.update({"providers": {"solo": mock_provider("solo", "Assistant")}})
        self.pipeline.resume()
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(run.task, "what does this repo do?")


class TestRunningOneSeatAgain(PipelineTestBase):
    """Retry: replace one answer without throwing away the rest.

    The rule that makes it honest is what follows a re-run. A position is
    quoted into every peer's critique and into the chairman's prompt, so a seat
    that answers again invalidates the reviews of it - keeping them would leave
    a transcript whose critiques discuss an answer no longer in it.
    """

    def council(self):
        self.store.update({"mode": "council", "zero_touch": True})
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        return run

    def test_a_finished_run_offers_every_seat(self):
        run = self.council()
        plan = run.to_dict()["retry_plan"]
        self.assertEqual(sorted(plan), sorted(run.stage_order))

    def test_re_running_a_position_takes_the_reviews_of_it_with_it(self):
        run = self.council()
        seat = run.seating.members[0].id
        plan = run.to_dict()["retry_plan"][seat]

        self.assertEqual(plan[0], seat)
        for member in run.seating.members:
            self.assertIn(f"{member.id}_critique", plan)
        self.assertIn("chair", plan)
        # Not the other members' own positions: they were written blind, at the
        # same time, and are not downstream of this one.
        for other in run.seating.members[1:]:
            self.assertNotIn(other.id, plan)

    def test_re_running_a_critique_only_takes_the_verdict(self):
        run = self.council()
        critique = f"{run.seating.members[0].id}_critique"
        self.assertEqual(run.to_dict()["retry_plan"][critique], [critique, "chair"])

    def test_re_running_the_chair_takes_nothing_else(self):
        run = self.council()
        self.assertEqual(run.to_dict()["retry_plan"]["chair"], ["chair"])

    def test_the_kept_answers_are_not_asked_again(self):
        run = self.council()
        seat = run.seating.members[0].id
        untouched = [
            s for s in run.seating.members[1:]
        ]
        before = {s.id: run.stages[s.id].started_at for s in untouched}

        self.pipeline.retry(seat)
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        after = {s.id: run.stages[s.id].started_at for s in untouched}
        self.assertEqual(before, after)
        # And the stage asked for really did run again.
        self.assertEqual(run.stages[seat].state, "done")
        self.assertEqual(run.resumed, 1)

    def test_the_reviews_of_a_re_run_position_are_written_fresh(self):
        run = self.council()
        seat = run.seating.members[0].id
        before = {
            f"{m.id}_critique": run.stages[f"{m.id}_critique"].started_at
            for m in run.seating.members
        }

        self.pipeline.retry(seat)
        self.wait_terminal()

        for stage_id, started in before.items():
            self.assertNotEqual(run.stages[stage_id].started_at, started, stage_id)

    def test_a_chat_turn_has_nothing_downstream(self):
        self.store.update({
            "mode": "solo",
            "providers": {"solo": mock_provider("solo", "Assistant")},
        })
        run = self.pipeline.start("what is this?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.to_dict()["retry_plan"], {"solo": ["solo"]})

    def test_a_seat_that_died_can_be_replaced_on_a_run_that_finished(self):
        # The case the operator actually hits: the council carried on with two
        # of three, the chairman answered, and the run reads `complete` - so
        # Continue is not offered and the dead seat is nobody's problem. Here
        # it is one click, and the reviews and verdict are rewritten around the
        # answer that was missing when they were written.
        self.store.update({
            "zero_touch": True,
            "council": {"chair_deliberates": True},
            "providers": {
                cfg.council_provider_id("codex"): mock_council("codex", ["--fail"]),
            },
        })
        run = self.pipeline.start("carry on regardless", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)

        dead = next(
            s for s in self.member_stages(run) if s.agent == "codex"
        )
        self.assertEqual(dead.state, "failed")
        survivors = {
            s.id: s.started_at for s in self.member_stages(run) if s.state == "done"
        }
        reviews = {
            s.id: s.started_at for s in self.critique_stages(run)
            if s.state == "done"
        }
        self.assertTrue(survivors and reviews)

        self.store.update({
            "providers": {cfg.council_provider_id("codex"): mock_council("codex")},
        })
        self.pipeline.retry(dead.id)
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(run.stages[dead.id].state, "done")
        # The positions that answered the first time are untouched...
        for stage_id, started in survivors.items():
            self.assertEqual(run.stages[stage_id].started_at, started, stage_id)
        # ...and the reviews of them are not, because the bench they reviewed
        # has a member in it that was not there before.
        for stage_id, started in reviews.items():
            self.assertNotEqual(run.stages[stage_id].started_at, started, stage_id)

    def test_an_archived_run_is_offered_the_same_plan(self):
        # Asked of a transcript, not of the engine: the runs worth re-seating a
        # dead member on are usually yesterday's, and a file written before the
        # question existed has to answer it the same way.
        run = self.council()
        path = self.runs_dir / run.transcript_name
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("retry_plan", None)
        path.write_text(json.dumps(data), encoding="utf-8")

        loaded = self.pipeline.load_run(run.transcript_name)
        self.assertEqual(loaded["retry_plan"], run.to_dict()["retry_plan"])

    def test_a_seat_can_be_re_run_after_a_restart(self):
        run = self.council()
        seat = run.seating.members[0].id
        kept = {
            s.id: s.started_at for s in self.member_stages(run) if s.id != seat
        }

        restarted = Pipeline(self.store, self.bus, runs_dir=self.runs_dir)
        self.addCleanup(restarted.wait_for_worker)
        restarted.revive(run.transcript_name, start=False)
        revived = restarted.retry(seat)
        self.wait_for(lambda: not restarted.is_busy(), what="the retried run")
        restarted.wait_for_worker()

        self.assertEqual(revived.state, "complete", revived.error)
        self.assertEqual(revived.id, run.id)
        for stage_id, started in kept.items():
            self.assertEqual(revived.stages[stage_id].started_at, started, stage_id)

    def test_a_stage_this_run_never_had_is_refused(self):
        self.council()
        with self.assertRaises(ValueError):
            self.pipeline.retry("seat9")

    def test_nothing_is_offered_while_a_run_is_in_flight(self):
        # The plan is a question about a finished run. Offered mid-run it would
        # invite a click that the engine would have to refuse anyway.
        self.store.update({"mode": "council", "zero_touch": True})
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_for(
            lambda: run.state not in ("idle", "queued"), what="the run to start"
        )
        self.assertEqual(run.to_dict()["retry_plan"], {})
        self.wait_terminal()


class TestCancellation(PipelineTestBase):
    def test_cancel_at_the_gate_ends_the_run(self):
        self.store.update({"zero_touch": False})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")
        self.pipeline.cancel()
        self.wait_terminal()
        self.assertEqual(run.state, "cancelled")
        self.assertFalse((self.repo / "AI_COUNCIL_DEMO.md").exists())


class TestTranscriptIsolation(PipelineTestBase):
    """A run writes where it was told to, not where the environment points.

    The transcript is written by the worker thread *after* the run reaches a
    terminal state, so it can land arbitrarily late - after the caller that
    started it has moved on and put the environment back. Re-reading
    XDG_CONFIG_HOME on every write made that late transcript escape into the
    operator's real history; the directory is bound once instead.
    """

    def test_a_late_transcript_ignores_a_changed_environment(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()

        self.store.update({"zero_touch": False})
        run = self.pipeline.start("do the thing", str(self.repo))
        self.wait_for(lambda: run.state == "awaiting_approval")

        # The worker is parked at the gate. Move the environment out from under
        # it - what a cleanup does - and only then let it wind down.
        os.environ["XDG_CONFIG_HOME"] = str(elsewhere)
        self.pipeline.cancel()
        self.assertTrue(self.pipeline.wait_for_worker(), "worker did not exit")

        self.assertEqual(run.state, "cancelled")
        self.assertTrue(
            (self.runs_dir / run.transcript_name).exists(),
            "the transcript did not reach the directory the run was given",
        )
        self.assertEqual(
            sorted(elsewhere.rglob("*.json")), [],
            "the run wrote a transcript outside the directory it was given",
        )

    def test_the_worker_has_finished_writing_once_it_is_joined(self):
        # `is_busy()` going false is not the same as the worker being done: the
        # transcript is written after the terminal state is published.
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("add a demo artifact", str(self.repo))
        self.wait_terminal()
        self.assertTrue(self.pipeline.wait_for_worker(), "worker did not exit")
        self.assertTrue((self.runs_dir / run.transcript_name).exists())

    def test_the_default_runs_directory_still_follows_the_config_dir(self):
        # Not injecting one has to keep the production behaviour.
        self.assertEqual(Pipeline(self.store, EventBus()).runs_dir, cfg.runs_dir())


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

        # And it reached the CLIs, not just the Run object. Compared per seat
        # id rather than per CLI: the bench is routed, so seat 1 of the
        # follow-up need not be the same agent as seat 1 of the first run -
        # what has to hold is that the seat was told what came before.
        #
        # Only the stages that carry the thread are checked. A critique is
        # given the peers' answers rather than the history, so its prompt does
        # not grow with the conversation and asserting it would be wrong.
        for stage in [*self.member_ids(second), "chair"]:
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

    def test_a_run_cannot_be_continued_in_another_folder(self):
        first = self.first_run()
        other = self.tmp / "other"
        other.mkdir()
        git(["init", "-q"], other)

        with self.assertRaisesRegex(ValueError, "folder it started in"):
            self.pipeline.start(
                "follow up", str(other), continue_from=first.transcript_name
            )

    def test_a_transcript_from_before_this_feature_can_still_be_continued(self):
        # Older transcripts have no `conversation` or `parent_run_id` key. They
        # are still one turn of a conversation - the first one.
        legacy = self.pipeline.runs_dir / "1700000000-legacyrun.json"
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

    def test_many_chat_follow_ups_remain_one_conversation(self):
        self.store.update({
            "mode": "solo",
            "providers": {"solo": mock_provider("solo", "Assistant")},
        })
        previous = ""
        runs = []
        for number in range(1, 9):
            run = self.run_once(
                f"chat message {number}",
                continue_from=previous,
            )
            runs.append(run)
            previous = run.transcript_name

        listed = self.pipeline.history(mode="solo")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["file"], runs[-1].transcript_name)
        self.assertEqual(listed[0]["messages"], 8)
        self.assertEqual(listed[0]["title"], "chat message 1")
        self.assertEqual(runs[-1].parent_run_id, runs[-2].id)
        self.assertEqual(
            [turn["task"] for turn in runs[-1].conversation],
            [f"chat message {number}" for number in range(1, 8)],
        )

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
        legacy = self.pipeline.runs_dir / "1700000000-legacyrun.json"
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
        (self.pipeline.runs_dir / "1700000001-broken.json").write_text("{oh no", encoding="utf-8")
        self.assertEqual([c["file"] for c in self.pipeline.history()],
                         [run.transcript_name])

    def test_an_edited_transcript_is_read_again(self):
        # The rows are cached against each file's mtime and size, so listing a
        # year of runs does not reparse every diff in them. A transcript that
        # changed on disk has to defeat that, or the sidebar would keep showing
        # a title the file no longer carries.
        run = self.run_once("the first title")
        self.assertEqual(self.pipeline.history()[0]["title"], "the first title")

        path = self.pipeline.runs_dir / run.transcript_name
        data = json.loads(path.read_text(encoding="utf-8"))
        data["task"] = "an entirely different title"
        path.write_text(json.dumps(data), encoding="utf-8")

        self.assertEqual(
            self.pipeline.history()[0]["title"], "an entirely different title"
        )

    def test_a_deleted_transcript_is_forgotten_rather_than_cached(self):
        run = self.run_once("do the thing")
        self.pipeline.history()
        self.pipeline.delete_run(run.transcript_name)

        self.assertEqual(self.pipeline.history(), [])
        self.assertEqual(self.pipeline._summary_cache, {})


class TestEventStream(PipelineTestBase):
    def test_terminal_event_is_published_after_transcript_exists(self):
        q = self.bus.subscribe()
        self.store.update({"zero_touch": True})
        run = self.pipeline.start("persist before terminal event", str(self.repo))

        while True:
            event = q.get(timeout=5)
            if event["kind"] == "state" and event["state"] in (
                "complete", "failed", "cancelled"
            ):
                break

        self.assertTrue((self.pipeline.runs_dir / run.transcript_name).is_file())

    def test_the_transcript_is_written_once_rather_than_twice(self):
        # The worker used to persist again on its way out, after `_set_state`
        # had already written the same file on the way into the terminal state.
        # A transcript carries every stage's output and the whole diff, so the
        # second dump was the largest write in the run and changed nothing.
        writes = []
        real = self.pipeline._persist

        def counted(run):
            writes.append(run.state)
            real(run)

        self.pipeline._persist = counted
        self.addCleanup(setattr, self.pipeline, "_persist", real)

        self.store.update({"zero_touch": True})
        run = self.pipeline.start("write me once", str(self.repo))
        self.wait_terminal()

        self.assertEqual(writes, ["complete"], run.error)
        self.assertTrue((self.pipeline.runs_dir / run.transcript_name).is_file())

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
    """A dead Stage 1 must not turn Zero-Touch into an unattended solo run.

    Zero-Touch means "you may skip my approval because a council deliberated
    and a chairman is synthesising what it decided". With no positions at all
    that premise is gone — proceeding anyway silently grants one agent
    unattended write access under a setting the operator chose for a different
    situation.

    One member dying is a different case and does not degrade: the council
    continues with whoever is left. It takes losing all of them.
    """

    def setUp(self):
        super().setUp()
        self.store.update({
            "zero_touch": True,
            "providers": {
                cfg.council_provider_id(a): mock_council(a, ["--fail"])
                for a in cfg.AGENTS
            },
        })
        # The chair has to survive to be approvable, so it is pinned to a CLI
        # that is put back in working order.
        self.pin_chair("claude", mock_council("claude"))

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

    def test_a_healthy_council_still_skips_the_gate_under_zero_touch(self):
        # The fallback must not become a gate on every Zero-Touch run.
        self.store.update({"providers": council_providers()})
        run = self.pipeline.start("do the thing", str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        self.assertNotEqual(run.state, "awaiting_approval")


def seat(persona="", seat_id="seat1", agent="codex"):
    """One routed seat, as the seating hands it to the pipeline."""
    return router.Seat(
        id=seat_id,
        agent=agent,
        provider_id=cfg.council_provider_id(agent),
        alias="Agent A",
        persona=persona,
    )


class TestRoleTemplates(PipelineTestBase):
    """Role behaviour is a setting, not a constant.

    The shipped prompts are defaults the operator can replace; what must not
    change is that the resolved text actually reaches the CLI. Which role a
    seat gets is no longer a per-stage setting at all - it is the persona the
    router assigned, resolved by `Pipeline._persona_system` against the same
    catalogue - so that is what these drive.
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

    def test_a_seat_gets_the_persona_it_was_routed(self):
        text = self.pipeline._persona_system(seat("security_review"), {})
        self.assertIn("SECURITY REVIEWER", text)

    def test_an_edited_persona_beats_the_shipped_text(self):
        text = self.pipeline._persona_system(
            seat("security_review"), {"security_review": {"system": "Be a poet."}}
        )
        self.assertEqual(text, "Be a poet.")
        self.assertNotIn("SECURITY REVIEWER", text)

    def test_an_unrouted_seat_gets_no_lens_rather_than_a_default(self):
        # There is no per-stage fallback any more, and inventing one would put
        # a behaviour nobody chose in front of a seat. Blank is the neutral
        # council member: the stage contract and nothing else.
        self.assertEqual(self.pipeline._persona_system(seat(""), {}), "")

    def test_a_persona_that_no_longer_exists_adds_no_lens(self):
        # Deleting a role the Council panel still pins must not resurrect some
        # other role's wording under that seat's name.
        self.assertEqual(self.pipeline._persona_system(seat("gone"), {}), "")

    def test_an_emptied_persona_adds_no_lens(self):
        # Blanking a role's text in the editor is the operator saying "no
        # lens", which composes onto the stage contract as nothing at all.
        text = self.pipeline._persona_system(
            seat("pragmatist"), {"pragmatist": {"system": "   "}}
        )
        self.assertEqual(text.strip(), "")

    def test_the_resolved_persona_reaches_the_agent(self):
        from aicouncil import prompts

        prompt = prompts.build_member_prompt(
            "add a feature", "/tmp/r", None, "",
            persona_system=self.pipeline._persona_system(
                seat("adversarial_review"), {}
            ),
        )
        self.assertIn("ADVERSARIAL REVIEWER", prompt)
        self.assertIn("add a feature", prompt)
        # Composed onto the stage contract, not in place of it: a persona must
        # not be able to talk a read-only seat out of being read-only.
        self.assertIn("# Your lens", prompt)

    def test_a_seat_with_no_persona_leaves_the_contract_alone(self):
        from aicouncil import prompts

        prompt = prompts.build_member_prompt(
            "add a feature", "/tmp/r", None, "",
            persona_system=self.pipeline._persona_system(seat(""), {}),
        )
        self.assertNotIn("# Your lens", prompt)
        self.assertIn("add a feature", prompt)

    def test_the_persona_also_reaches_the_critique_turn(self):
        # The lens is the seat's, not the stage's: a Pragmatist that turned
        # neutral the moment it started reviewing peers would be half a seat.
        from aicouncil import prompts

        prompt = prompts.build_critique_prompt(
            "task", [{"alias": "Agent B", "output": "their answer"}],
            "/tmp/r", None, "",
            persona_system=self.pipeline._persona_system(seat("pragmatist"), {}),
        )
        self.assertIn("PRAGMATISM", prompt.upper())

    def test_house_rules_still_apply_over_a_custom_role(self):
        from aicouncil import prompts

        prompt = prompts.build_draft_prompt(
            "task", "/tmp/r", None, "ALWAYS USE TABS",
            system="Custom behaviour.",
        )
        self.assertIn("Custom behaviour.", prompt)
        self.assertIn("ALWAYS USE TABS", prompt)


class TestHistoryScopeAndDeletion(PipelineTestBase):
    """Two lists, and the ways to empty them."""

    def setUp(self):
        super().setUp()
        self.store.update({
            "zero_touch": True,
            "providers": {"solo": mock_provider("solo", "Assistant")},
        })

    def run_in(self, mode, task):
        self.store.update({"mode": mode})
        run = self.pipeline.start(task, str(self.repo))
        self.wait_terminal()
        self.assertEqual(run.state, "complete", run.error)
        return run

    def test_each_mode_lists_only_its_own_conversations(self):
        # They cannot be continued in each other, so offering both in one list
        # would show rows that clicking cannot do what the click promises.
        council = self.run_in("council", "a council run")
        chat = self.run_in("solo", "a chat")

        self.assertEqual(
            [r["file"] for r in self.pipeline.history(mode="council")],
            [council.transcript_name],
        )
        self.assertEqual(
            [r["file"] for r in self.pipeline.history(mode="solo")],
            [chat.transcript_name],
        )
        # No filter still means everything, which is what the API default is.
        self.assertEqual(len(self.pipeline.history()), 2)

    def test_deleting_one_conversation_leaves_the_rest(self):
        keep = self.run_in("council", "keep this one")
        drop = self.run_in("council", "delete this one")

        self.assertTrue(self.pipeline.delete_run(drop.transcript_name))
        self.assertEqual(
            [r["file"] for r in self.pipeline.history()], [keep.transcript_name]
        )
        # Gone from disk, not merely hidden from the listing.
        self.assertIsNone(self.pipeline.load_run(drop.transcript_name))
        # And deleting it again is a plain False, not an exception.
        self.assertFalse(self.pipeline.delete_run(drop.transcript_name))

    def test_clearing_one_mode_leaves_the_other_untouched(self):
        council = self.run_in("council", "a council run")
        self.run_in("solo", "a chat")
        self.run_in("solo", "another chat")

        self.assertEqual(self.pipeline.clear_history("solo"), 2)
        self.assertEqual(self.pipeline.history(mode="solo"), [])
        self.assertEqual(
            [r["file"] for r in self.pipeline.history(mode="council")],
            [council.transcript_name],
        )

    def test_clearing_without_a_mode_removes_everything(self):
        self.run_in("council", "a council run")
        self.run_in("solo", "a chat")

        self.assertEqual(self.pipeline.clear_history(), 2)
        self.assertEqual(self.pipeline.history(), [])

    def test_a_transcript_name_cannot_escape_the_runs_directory(self):
        # The name arrives from a request body, and this is the only thing
        # between a client and `unlink`.
        outside = self.tmp / "not-a-transcript.json"
        outside.write_text("{}", encoding="utf-8")

        for evil in (
            "../not-a-transcript.json",
            "../../etc/passwd",
            "subdir/run.json",
            "..\\run.json",
            "",
            "run",           # no .json suffix
        ):
            self.assertFalse(
                self.pipeline.delete_run(evil), f"{evil!r} should be refused"
            )
            self.assertIsNone(self.pipeline.load_run(evil))
        self.assertTrue(outside.exists(), "a file outside the runs dir was deleted")


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

        draft = prompts.build_draft_prompt("do it", "/tmp/r", None, "", self.TURNS)
        polish = prompts.build_polish_prompt(
            "do it", "a draft", "/tmp/r", None, "", "", self.TURNS
        )
        chat = prompts.build_chat_prompt("do it", self.TURNS)
        for prompt in (draft, polish, chat):
            self.assertIn("add a login route", prompt)

    def test_the_working_folder_is_named_as_the_authority_over_the_transcript(self):
        # A remembered run may have been rolled back or edited over since.
        from aicouncil import prompts

        prompt = prompts.build_polish_prompt(
            "do it", "a draft", "/tmp/r", None, "", "", self.TURNS
        )
        self.assertIn("working folder as it stands now", prompt)

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


class TestSoloPrompt(unittest.TestCase):
    """Solo Mode adds nothing the operator did not put there."""

    def test_a_plain_message_is_sent_exactly_as_typed(self):
        from aicouncil import prompts

        self.assertEqual(prompts.build_chat_prompt("  what is this repo?  "),
                         "what is this repo?")

    def test_no_council_persona_arrives_uninvited(self):
        from aicouncil import prompts

        prompt = prompts.build_chat_prompt("what is this repo?")
        self.assertNotIn("ARCHITECT", prompt)
        self.assertNotIn("Target repository", prompt)
        self.assertNotIn("Standing project rules", prompt)

    def test_a_behaviour_is_added_only_when_one_is_set(self):
        from aicouncil import prompts

        prompt = prompts.build_chat_prompt("hello", behavior="  Answer briefly.  ")
        self.assertIn("Answer briefly.", prompt)
        self.assertLess(prompt.index("Answer briefly."), prompt.index("hello"))

    def test_the_thread_precedes_the_message(self):
        from aicouncil import prompts

        prompt = prompts.build_chat_prompt("and now?", TestConversationPrompt.TURNS)
        self.assertIn("add a login route", prompt)
        self.assertLess(prompt.index("add a login route"), prompt.index("and now?"))


class TestReadOnlyChairmanPrompt(unittest.TestCase):
    """A run with no folder asks the chairman for the answer, not for edits.

    The shipped chairman text is a write-mode contract - apply the outcome,
    then report what changed - and it arrives from the role catalogue rather
    than from a default, so the swap has to be made on the text that is handed
    in.
    """

    def chairman(self, **kwargs):
        from aicouncil import prompts

        return prompts.build_chairman_prompt(
            "add a demo artifact", [{"alias": "Agent A", "output": "do it"}],
            [], "/tmp", **kwargs,
        )

    def test_the_write_mode_contract_is_the_default(self):
        from aicouncil import prompts

        prompt = self.chairman(system=prompts.CHAIRMAN_SYSTEM)
        self.assertIn("Apply the edits to the working tree", prompt)

    def test_a_read_only_run_swaps_the_shipped_chairman(self):
        from aicouncil import prompts

        prompt = self.chairman(system=prompts.CHAIRMAN_SYSTEM, read_only=True)
        self.assertNotIn("Apply the edits to the working tree", prompt)
        self.assertIn("Nothing on this run writes to disk", prompt)
        self.assertIn("a fenced block per file", prompt)

    def test_an_edited_chairman_is_kept_and_overruled(self):
        # The operator's wording is theirs. What it says about applying the
        # outcome cannot stand, because there is nothing to apply it to.
        prompt = self.chairman(
            system="You are the CHAIRMAN. Apply the edits yourself.",
            read_only=True,
        )
        self.assertIn("Apply the edits yourself.", prompt)
        self.assertIn("# This run writes nothing", prompt)

    def test_the_deliberation_still_reaches_the_chairman(self):
        # The read-only chairman is the same stage with a different
        # deliverable, and the mock council fixture identifies it by this
        # heading - a swap that lost it would silently reseat the stage.
        prompt = self.chairman(read_only=True)
        self.assertIn("# Stage 1 - independent positions", prompt)
        self.assertIn("CONSENSUS: <integer 0-100>", prompt)


class TestCavemanPrompt(unittest.TestCase):
    """The style switch: telegraphic prose, byte-exact code.

    Each mode owns its own switch, so every test here is also a test that the
    other two modes were left alone.
    """

    MARK = "ULTRA-LOW TOKEN EFFICIENCY MODE"

    def test_nothing_is_added_when_it_is_off(self):
        from aicouncil import prompts

        self.assertEqual(prompts.build_chat_prompt("what is this?"),
                         "what is this?")
        self.assertNotIn(self.MARK, prompts.build_member_prompt("t", "/tmp"))

    def test_chat_gets_it_before_the_message(self):
        from aicouncil import prompts

        prompt = prompts.build_chat_prompt("what is this?", caveman=True)
        self.assertIn(self.MARK, prompt)
        self.assertLess(prompt.index(self.MARK), prompt.index("what is this?"))

    def test_a_typed_behaviour_still_arrives_alongside_it(self):
        # The switch is a style, not a replacement for what the operator wrote.
        from aicouncil import prompts

        prompt = prompts.build_chat_prompt(
            "hello", behavior="Always show the code.", caveman=True
        )
        self.assertIn(self.MARK, prompt)
        self.assertIn("Always show the code.", prompt)

    def test_every_council_stage_gets_the_same_instruction(self):
        # A chairman writing tersely over members who wrote at length would
        # read as three different voices in one transcript.
        from aicouncil import prompts

        member = prompts.build_member_prompt("t", "/tmp", caveman=True)
        critique = prompts.build_critique_prompt(
            "t", [{"alias": "Agent B", "output": "x"}], "/tmp", caveman=True
        )
        chair = prompts.build_chairman_prompt(
            "t", [{"alias": "Agent A", "output": "x"}], [], "/tmp", caveman=True
        )
        for prompt in (member, critique, chair):
            self.assertIn(self.MARK, prompt)

    def test_code_and_paths_are_carved_out_of_the_compression(self):
        # The whole reason this is safe to send: what the operator has to use
        # out of an answer is exempt from being shortened.
        from aicouncil import prompts

        prompt = prompts.build_member_prompt("t", "/tmp", caveman=True)
        self.assertIn("byte-exact", prompt)
        self.assertIn("Preservation Exception", prompt)

    def test_a_project_turn_carries_it_only_when_it_is_on(self):
        from aicouncil import prompts

        off = prompts.project_context_block("p1", "/tmp", "{}")
        on = prompts.project_context_block("p1", "/tmp", "{}", caveman=True)
        self.assertNotIn(self.MARK, off)
        self.assertIn(self.MARK, on)


class TestEfficiencyPrompt(unittest.TestCase):
    """Concise normal prose remains independent from Caveman Mode."""

    MARK = "[SYSTEM INSTRUCTION: EFFICIENCY MODE]"

    def test_nothing_is_added_when_it_is_off(self):
        from aicouncil import prompts

        self.assertEqual(
            prompts.build_chat_prompt("what is this?"),
            "what is this?",
        )
        self.assertNotIn(self.MARK, prompts.build_member_prompt("t", "/tmp"))

    def test_chat_gets_it_before_the_message(self):
        from aicouncil import prompts

        prompt = prompts.build_chat_prompt("what is this?", efficiency=True)
        self.assertIn(self.MARK, prompt)
        self.assertLess(prompt.index(self.MARK), prompt.index("what is this?"))

    def test_every_council_stage_gets_the_same_instruction(self):
        from aicouncil import prompts

        member = prompts.build_member_prompt("t", "/tmp", efficiency=True)
        critique = prompts.build_critique_prompt(
            "t",
            [{"alias": "Agent B", "output": "x"}],
            "/tmp",
            efficiency=True,
        )
        chair = prompts.build_chairman_prompt(
            "t",
            [{"alias": "Agent A", "output": "x"}],
            [],
            "/tmp",
            efficiency=True,
        )
        for prompt in (member, critique, chair):
            self.assertIn(self.MARK, prompt)

    def test_it_preserves_technical_content_and_necessary_reasoning(self):
        from aicouncil import prompts

        prompt = prompts.build_member_prompt("t", "/tmp", efficiency=True)
        self.assertIn("Keep code blocks, shell commands, file paths", prompt)
        self.assertIn("Preserve essential reasoning", prompt)

    def test_it_can_coexist_with_caveman_mode(self):
        from aicouncil import prompts

        prompt = prompts.build_chat_prompt(
            "hello",
            caveman=True,
            efficiency=True,
        )
        self.assertIn(self.MARK, prompt)
        self.assertIn("ULTRA-LOW TOKEN EFFICIENCY MODE", prompt)

    def test_both_styles_arrive_under_one_heading_everywhere(self):
        # Two identically-titled sections is the agent being told twice, in
        # different words, how to write. Every builder composes them the same
        # way, so every builder is checked.
        from aicouncil import prompts

        built = (
            prompts.build_chat_prompt("hello", caveman=True, efficiency=True),
            prompts.build_member_prompt(
                "t", "/tmp", caveman=True, efficiency=True
            ),
            prompts.build_critique_prompt(
                "t",
                [{"alias": "Agent B", "output": "x"}],
                "/tmp",
                caveman=True,
                efficiency=True,
            ),
            prompts.build_chairman_prompt(
                "t",
                [{"alias": "Agent A", "output": "x"}],
                [],
                "/tmp",
                caveman=True,
                efficiency=True,
            ),
            prompts.project_context_block(
                "p1", "/tmp", "{}", caveman=True, efficiency=True
            ),
        )
        for prompt in built:
            self.assertEqual(prompt.count("# How to write your answer"), 1)
            self.assertIn(self.MARK, prompt)
            self.assertIn("ULTRA-LOW TOKEN EFFICIENCY MODE", prompt)
            # Caveman is the claim on the voice; Efficiency applies inside it.
            self.assertLess(
                prompt.index("ULTRA-LOW TOKEN EFFICIENCY MODE"),
                prompt.index(self.MARK),
            )

    def test_a_project_turn_carries_it_only_when_it_is_on(self):
        from aicouncil import prompts

        off = prompts.project_context_block("p1", "/tmp", "{}")
        on = prompts.project_context_block(
            "p1",
            "/tmp",
            "{}",
            efficiency=True,
        )
        self.assertNotIn(self.MARK, off)
        self.assertIn(self.MARK, on)


class TestStyleDoesNotEatTheContract(unittest.TestCase):
    """A shortened answer must still be a parseable one.

    Both switches tell an agent to cut what is not needed, and both name code
    as the thing that may not be cut - which leaves the machine-read part of a
    reply unmentioned. A seat that pruned its confidence trailer, or a project
    turn that dropped its fenced report, would be obeying the style and
    breaking the engine.
    """

    GUARD = "This changes your voice, not the contract."

    def _prompts(self, **styles):
        from aicouncil import prompts

        return {
            "chat": prompts.build_chat_prompt("hello", **styles),
            "member": prompts.build_member_prompt("t", "/tmp", **styles),
            "critique": prompts.build_critique_prompt(
                "t", [{"alias": "Agent B", "output": "x"}], "/tmp", **styles
            ),
            "chair": prompts.build_chairman_prompt(
                "t", [{"alias": "Agent A", "output": "x"}], [], "/tmp", **styles
            ),
            "project": prompts.project_context_block(
                "p1", "/tmp", "{}", **styles
            ),
        }

    def test_every_styled_prompt_says_the_contract_still_stands(self):
        for style in cfg.WRITING_STYLES:
            for name, prompt in self._prompts(**{style: True}).items():
                with self.subTest(style=style, prompt=name):
                    self.assertIn(self.GUARD, prompt)

    def test_it_is_said_once_when_both_are_on(self):
        # Same reason the heading is one heading: an agent told the same thing
        # twice in different words has nothing to go on when they differ.
        for name, prompt in self._prompts(caveman=True, efficiency=True).items():
            with self.subTest(prompt=name):
                self.assertEqual(prompt.count(self.GUARD), 1)

    def test_an_unstyled_prompt_is_left_alone(self):
        # It is part of the style block, not a new standing instruction: a run
        # with no style switched on is byte-for-byte what it always was.
        for name, prompt in self._prompts().items():
            with self.subTest(prompt=name):
                self.assertNotIn(self.GUARD, prompt)

    def test_the_thing_it_protects_is_actually_in_the_prompt(self):
        # The guard would be words about nothing if the trailer it defends had
        # been dropped from the stage contract.
        from aicouncil import prompts

        for name, prompt in self._prompts(caveman=True).items():
            if name in ("chat", "project"):
                continue
            with self.subTest(prompt=name):
                self.assertIn(prompts.CONFIDENCE_CONTRACT.strip()[:40], prompt)


class TestTheTranscriptRecordsItsStyles(PipelineTestBase):
    """An archived answer has to be able to say why it reads as it does."""

    def test_a_run_records_the_switches_it_answered_under(self):
        self.store.update({
            "mode": "solo",
            "efficiency": {"chat": True},
            "providers": {"solo": mock_provider("solo", "Assistant")},
        })
        run = self.pipeline.start("what is this?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(
            run.to_dict()["styles"], {"caveman": False, "efficiency": True}
        )

    def test_turning_the_switch_off_afterwards_does_not_rewrite_history(self):
        # The gear is a live setting; the transcript is a record. A run written
        # under Caveman still says so once Caveman is off, or the record
        # explains the wrong answer.
        self.store.update({
            "mode": "solo",
            "caveman": {"chat": True},
            "providers": {"solo": mock_provider("solo", "Assistant")},
        })
        run = self.pipeline.start("what is this?", str(self.repo))
        self.wait_terminal()
        self.store.update({"caveman": {"chat": False}})

        self.assertTrue(run.to_dict()["styles"]["caveman"])
        persisted = self.pipeline.load_run(run.transcript_name)
        self.assertTrue(persisted["styles"]["caveman"])

    def test_a_council_run_records_the_councils_switches(self):
        # Not Chat's. The two are independent, and a transcript that quoted the
        # wrong one would be worse than quoting none.
        self.store.update({
            "mode": "council",
            "zero_touch": True,
            "caveman": {"council": True, "chat": False},
        })
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertTrue(run.to_dict()["styles"]["caveman"])


class TestWritingStyleReading(unittest.TestCase):
    """The one reader all three run loops pull their style switches through."""

    def test_each_mode_sees_only_its_own_switches(self):
        conf = {
            "caveman": {"council": True, "chat": False, "project": True},
            "efficiency": {"council": False, "chat": True, "project": True},
        }
        self.assertEqual(
            cfg.writing_styles(conf, "council"),
            {"caveman": True, "efficiency": False},
        )
        self.assertEqual(
            cfg.writing_styles(conf, "chat"),
            {"caveman": False, "efficiency": True},
        )
        self.assertEqual(
            cfg.writing_styles(conf, "project"),
            {"caveman": True, "efficiency": True},
        )

    def test_a_hand_edited_config_is_off_rather_than_a_crash(self):
        # These are read inside a running loop, so a config someone typed into
        # by hand has to degrade to "off", not take the run down with it.
        for broken in ({}, {"efficiency": None}, {"efficiency": "yes"}):
            with self.subTest(broken=broken):
                self.assertEqual(
                    cfg.writing_styles(broken, "chat"),
                    {"caveman": False, "efficiency": False},
                )

    def test_the_keys_it_returns_are_what_the_builders_take(self):
        # It is splatted straight into the prompt builders; a style added to
        # one and not the other is a TypeError at run time, not import time.
        from aicouncil import prompts

        prompts.build_chat_prompt("hi", **cfg.writing_styles({}, "chat"))
        self.assertEqual(
            sorted(cfg.writing_styles({}, "council")),
            sorted(cfg.WRITING_STYLES),
        )


class TestCavemanReachesTheRun(PipelineTestBase):
    """End to end: the switch a mode owns is the one that reaches its CLI."""

    # The mock echoes this when the instruction reached it. Asserting on the
    # recorded command would prove nothing: it redacts the prompt to a length.
    MARK = "caveman mode requested"

    def setUp(self):
        super().setUp()
        assistant = mock_provider("solo", "Assistant")
        # The shipped solo provider's streaming flags survive the deep merge,
        # and `--output-format stream-json` leaves the mock's own parser
        # reading "stream-json" as the prompt. Cleared so the assertion below
        # is about what the pipeline sent and not about argparse.
        assistant["stream_args"] = []
        assistant["read_only_args"] = []
        self.store.update({"mode": "solo", "providers": {"solo": assistant}})

    def _sent(self, run):
        return run.stages["solo"].output

    def test_chat_answers_telegraphically_once_it_is_switched_on(self):
        self.store.update({"caveman": {"chat": True}})
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertIn(self.MARK, self._sent(run))

    def test_switching_it_on_for_the_council_leaves_chat_alone(self):
        # Three switches, not one with three labels. Turning the council terse
        # must not quietly change how a conversation answers.
        self.store.update({"caveman": {"council": True, "chat": False}})
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertNotIn(self.MARK, self._sent(run))

    def test_it_can_be_switched_off_again(self):
        # `ConfigStore.update` deep-merges, so a switch is only really a switch
        # if writing False over True survives the merge.
        self.store.update({"caveman": {"chat": True}})
        self.store.update({"caveman": {"chat": False}})
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertNotIn(self.MARK, self._sent(run))


class TestCavemanReachesCouncilRun(PipelineTestBase):
    """The Council setting reaches members, critiques, and the chair."""

    MARK = "caveman mode requested"

    def test_every_council_invocation_gets_the_instruction(self):
        self.store.update({
            "mode": "council",
            "zero_touch": True,
            "caveman": {"council": True},
        })
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        stages = self.member_stages(run) + self.critique_stages(run)
        stages.append(run.stages["chair"])
        for stage in stages:
            self.assertIn(self.MARK, stage.output, stage.id)


class TestEfficiencyReachesTheRun(PipelineTestBase):
    MARK = "efficiency mode requested"

    def setUp(self):
        super().setUp()
        assistant = mock_provider("solo", "Assistant")
        assistant["stream_args"] = []
        assistant["read_only_args"] = []
        self.store.update({"mode": "solo", "providers": {"solo": assistant}})

    def test_chat_receives_only_its_own_efficiency_switch(self):
        self.store.update({
            "efficiency": {"chat": True, "council": False},
        })
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertIn(self.MARK, run.stages["solo"].output)

    def test_council_switch_does_not_change_chat(self):
        self.store.update({
            "efficiency": {"council": True, "chat": False},
        })
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertNotIn(self.MARK, run.stages["solo"].output)

    def test_it_can_be_switched_off_again(self):
        # `ConfigStore.update` deep-merges, so a switch is only really a switch
        # if writing False over True survives the merge.
        self.store.update({"efficiency": {"chat": True}})
        self.store.update({"efficiency": {"chat": False}})
        run = self.pipeline.start("what does this repo do?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertNotIn(self.MARK, run.stages["solo"].output)

    def test_a_multi_agent_answer_carries_it_to_every_cli(self):
        # The bench shares `_chat_prompt` with the single-agent turn, which is
        # what makes the two answers comparable. Pinned so a future prompt
        # built separately for the bench cannot quietly drop the style.
        self.store.update({
            "multi_agent": True,
            "efficiency": {"chat": True},
            "providers": council_providers(),
        })
        run = self.pipeline.start("what is this repo?", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        self.assertEqual(len(run.stages), len(cfg.AGENTS))
        for stage in run.stages.values():
            self.assertIn(self.MARK, stage.output, stage.id)


class TestEfficiencyReachesCouncilRun(PipelineTestBase):
    MARK = "efficiency mode requested"

    def test_every_council_invocation_gets_the_instruction(self):
        self.store.update({
            "mode": "council",
            "zero_touch": True,
            "efficiency": {"council": True},
        })
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        stages = self.member_stages(run) + self.critique_stages(run)
        stages.append(run.stages["chair"])
        for stage in stages:
            self.assertIn(self.MARK, stage.output, stage.id)


class TestDeliberationEffort(PipelineTestBase):
    """Stages 1 and 2 can be told to think less hard than the one that writes.

    Worth an end-to-end run rather than a unit test on the helper: the point of
    the setting is that one agent's member seat and its chair stop sharing a
    provider, and only a routed run puts the same CLI in both.
    """

    def graded_providers(self):
        # `effort_args` cleared for the reason `mock_council` clears
        # `stream_args`: these merge onto the CLI preset, and Claude's
        # `--effort {effort}` on a command that is not Claude would hand the
        # mock a flag it has never heard of.
        return {
            pid: dict(provider, effort="high", effort_args=[])
            for pid, provider in council_providers().items()
        }

    def test_it_is_unset_by_default(self):
        # Off unless asked for: a council that quietly thought less hard than
        # the CLIs were configured to would be a worse council for no stated
        # reason.
        self.assertEqual(cfg.DEFAULTS["council"]["deliberation_effort"], "")

    def test_it_demotes_the_members_and_leaves_the_chair(self):
        self.store.update({
            "zero_touch": True,
            "providers": self.graded_providers(),
            "council": {"deliberation_effort": "low"},
        })
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        for stage in self.member_stages(run) + self.critique_stages(run):
            self.assertEqual(stage.effort, "low", stage.id)
        self.assertEqual(run.stages["chair"].effort, "high")

    def test_blank_leaves_every_seat_on_its_own_setting(self):
        self.store.update({
            "zero_touch": True,
            "providers": self.graded_providers(),
        })
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        stages = self.member_stages(run) + self.critique_stages(run)
        stages.append(run.stages["chair"])
        for stage in stages:
            self.assertEqual(stage.effort, "high", stage.id)


class TestRoleReachesTheRun(PipelineTestBase):
    def test_a_configured_persona_is_used_by_a_real_run(self):
        # End to end: pin a seat's lens, run, and confirm the agent answered
        # as that persona rather than as the neutral member. The mock varies
        # its position by the lens it was given, so its own output is the
        # evidence that the persona text actually reached the CLI.
        self.store.update({
            "zero_touch": True,
            "council": {
                "personas": {"seat1": "pragmatist"},
                "chair_deliberates": False,
            },
        })
        run = self.pipeline.start("do a thing", str(self.repo))
        self.wait_terminal()

        self.assertEqual(run.state, "complete", run.error)
        seat1 = run.stages["seat1"]
        self.assertEqual(seat1.persona, "pragmatist")
        self.assertIn("not paid for by this task", seat1.output)


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


class TestWorkspaceMigration(unittest.TestCase):
    """A config written when the folder was a mandatory repository still works.

    The folder itself carries over - it is still where runs happen - under a
    name that no longer claims it has to be a repository.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-migrate-"))
        self.path = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return ConfigStore(self.path)

    def test_the_old_target_repository_becomes_the_working_folder(self):
        store = self.write({
            "target_repo": "/home/me/project",
            "recent_repos": ["/home/me/project", "/home/me/other"],
        })
        self.assertEqual(store.get("workspace"), "/home/me/project")
        self.assertEqual(
            store.get("recent_workspaces"), ["/home/me/project", "/home/me/other"]
        )

    def test_the_old_keys_do_not_survive_the_migration(self):
        # A stale key that no longer decides anything still reads as a setting.
        store = self.write({"target_repo": "/home/me/project"})
        self.assertNotIn("target_repo", store.all())
        self.assertNotIn("recent_repos", store.all())

    def test_a_config_that_already_uses_the_new_name_is_left_alone(self):
        store = self.write({"workspace": "/home/me/new", "target_repo": "/old"})
        self.assertEqual(store.get("workspace"), "/home/me/new")

    def test_the_dead_cwd_mode_key_is_dropped_from_every_provider(self):
        store = self.write({"providers": {"drafter": {"cwd_mode": "repo"}}})
        self.assertNotIn("cwd_mode", store.get("providers")["drafter"])

    def test_a_seat_can_be_unpinned_again(self):
        # `update` deep-merges, so a save that dropped the key would leave the
        # old pin in place and the panel would appear to set the seat back to
        # Auto without doing it. The UI writes a blank instead, which
        # overwrites - and the router reads a blank as unpinned.
        store = self.write({"council": {"pins": {"chair": "claude"}}})
        store.update({"council": {"pins": {"chair": ""}}})
        self.assertEqual(store.get("council")["pins"]["chair"], "")

        store.update({"council": {"personas": {"seat1": "visionary"}}})
        store.update({"council": {"personas": {"seat1": ""}}})
        self.assertEqual(store.get("council")["personas"]["seat1"], "")

    def test_the_bench_is_shown_unless_it_has_been_switched_off(self):
        # A display toggle, but one that decides whether the only place to pin
        # a seat by hand is on screen at all.
        self.assertTrue(cfg.DEFAULTS["council"]["show_seats"])
        store = self.write({})
        self.assertTrue(store.get("council")["show_seats"])
        store.update({"council": {"show_seats": False}})
        self.assertFalse(store.get("council")["show_seats"])


class TestEditableRoles(PipelineTestBase):
    """Built-ins are defaults, not laws — and a role you add is not a lesser one.

    On the base fixture rather than a bare ConfigStore, because what a role
    edit has to survive is the trip through `Pipeline._persona_system` onto a
    seat - the only path that puts one in front of a CLI.
    """

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
        # A role the operator wrote is seatable on exactly the same terms as a
        # built-in: pin it to a seat and its text is the lens that seat brings.
        stored = {"perf": {"name": "Perf", "system": "PROFILE-FIRST-SENTINEL"}}
        self.assertEqual(
            self.pipeline._persona_system(seat("perf"), stored),
            "PROFILE-FIRST-SENTINEL",
        )

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

    def test_a_seat_pinned_to_a_deleted_role_runs_neutral(self):
        # Deleting a role the Council panel is still pinned to must not take
        # the seat down with it, and must not seat some other role's wording
        # under that seat's name. It loses its lens and nothing else.
        self.store.update({
            "roles": {"perf": {"name": "Perf", "system": "PROFILE-FIRST."}},
            "council": {"personas": {"seat1": "perf"}},
        })
        self.assertEqual(
            self.pipeline._persona_system(seat("perf"), self.store.get("roles")),
            "PROFILE-FIRST.",
        )

        self.store.replace_roles({})
        # The pin outlives the role - nothing rewrites it - so this is what a
        # seating built from that config actually hands the pipeline.
        self.assertEqual(self.store.get("council")["personas"]["seat1"], "perf")
        self.assertEqual(
            self.pipeline._persona_system(seat("perf"), self.store.get("roles")), ""
        )

    def test_an_empty_role_never_produces_an_empty_prompt(self):
        # A role blanked in the editor contributes no lens. The stage contract
        # is not the role's to erase, though, so the prompt is still whole.
        from aicouncil import prompts

        stored = {"hollow": {"name": "Hollow", "system": "   "}}
        prompt = prompts.build_member_prompt(
            "task", "/tmp/r", None, "",
            persona_system=self.pipeline._persona_system(seat("hollow"), stored),
        )
        self.assertNotIn("# Your lens", prompt)
        self.assertIn("one member of an AI council", prompt)
