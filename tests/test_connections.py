"""Adding, installing and signing in to an agent, against fake CLIs.

Fake rather than mocked: the point of this module is that it drives somebody
else's program on a pseudo-terminal, and a patched `subprocess` would prove
nothing about the part most likely to break. What the fakes stand in for is the
vendor, not the plumbing - the pty, the argv and the exit code are all real.

Nothing here installs software or starts a real sign-in.
"""

import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil import config as cfg  # noqa: E402
from aicouncil import connections  # noqa: E402

# `codex login status` answers in prose and by exit code; `codex login` prints
# the URL, waits for the browser, and is the one flow that has to survive being
# read through a terminal this app owns.
FAKE_CODEX = '''#!/usr/bin/env python3
import sys

if sys.argv[1:] == ["login", "status"]:
    import os
    if os.environ.get("FAKE_SIGNED_OUT"):
        sys.stderr.write("Not logged in\\n")
        sys.exit(1)
    print("Logged in using ChatGPT")
    sys.exit(0)
if sys.argv[1:] == ["login"]:
    print("\\x1b[1mSign in\\x1b[0m at https://example.test/auth?code=42")
    answer = sys.stdin.readline().strip()
    print(f"got {answer}")
    sys.exit(0)
sys.exit(2)
'''

# Claude answers with JSON, which is read rather than pattern-matched.
FAKE_CLAUDE = '''#!/usr/bin/env python3
import json
import sys

if sys.argv[1:] == ["auth", "status"]:
    print(json.dumps({
        "loggedIn": True, "email": "you@example.test", "subscriptionType": "pro",
    }))
    sys.exit(0)
sys.exit(2)
'''


def write_fake(directory, name, body):
    path = Path(directory) / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class ConnectionsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-conn-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        write_fake(self.bin, "codex", FAKE_CODEX)
        write_fake(self.bin, "claude", FAKE_CLAUDE)
        self._previous_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{self._previous_path}"

    def tearDown(self):
        os.environ["PATH"] = self._previous_path
        os.environ.pop("FAKE_SIGNED_OUT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAuthStatus(ConnectionsTestBase):
    def test_an_exit_code_answers_for_a_cli_that_prints_prose(self):
        self.assertIs(connections.auth_status("codex")["signed_in"], True)
        os.environ["FAKE_SIGNED_OUT"] = "1"
        self.assertIs(connections.auth_status("codex")["signed_in"], False)

    def test_a_json_answer_is_read_rather_than_matched(self):
        # Prose is free to be reworded in the next release; `loggedIn` is not.
        found = connections.auth_status("claude")
        self.assertIs(found["signed_in"], True)
        self.assertIn("you@example.test", found["detail"])

    def test_a_cli_with_no_status_command_answers_neither(self):
        # Antigravity 1.1.12 has no auth subcommand at all. Reporting that as
        # "signed out" would claim something nothing here knows.
        found = connections.auth_status("agy")
        self.assertIsNone(found["signed_in"])
        self.assertTrue(found["detail"])

    def test_a_missing_binary_answers_neither(self):
        os.environ["PATH"] = str(self.tmp / "empty")
        self.assertIsNone(connections.auth_status("codex")["signed_in"])


class TestAgentStatus(ConnectionsTestBase):
    def test_added_installed_and_signed_in_are_three_separate_facts(self):
        conf = {"agent_settings": {"codex": {"selected": False}}}
        found = connections.agent_status(conf, "codex")
        self.assertFalse(found["selected"])
        self.assertTrue(found["installed"])
        self.assertIs(found["signed_in"], True)

    def test_every_catalogued_agent_gets_a_card(self):
        found = connections.agent_statuses({})
        self.assertEqual([a["id"] for a in found], list(cfg.AGENTS))


class TestSetupSessions(ConnectionsTestBase):
    def setUp(self):
        super().setUp()
        self.setup = connections.SetupManager()

    def tearDown(self):
        self.setup.cancel()
        super().tearDown()

    def drain(self, timeout=15):
        """Wait for the session to exit, then return its final report."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            session = self.setup.status()
            if not session.get("running"):
                return session
            time.sleep(0.05)
        self.fail("the setup session never finished")

    def test_a_sign_in_streams_its_output_and_takes_an_answer(self):
        self.setup.start("codex", "login")
        deadline = time.monotonic() + 15
        while "example.test" not in self.setup.status().get("output", ""):
            self.assertLess(time.monotonic(), deadline, "no URL was printed")
            time.sleep(0.05)
        # Lifted out of the scrollback because it is the one line that has to
        # be acted on, and it is rarely the last one printed.
        self.assertEqual(
            self.setup.status()["url"], "https://example.test/auth?code=42"
        )
        # The colour a real CLI emits is stripped on the way up: this pane
        # renders text, and a raw escape sequence in it is not text.
        self.assertNotIn("\x1b", self.setup.status()["output"])

        self.setup.write("ok")
        session = self.drain()
        self.assertEqual(session["exit_code"], 0)
        self.assertIn("got ok", session["output"])

    def test_only_one_session_runs_at_a_time(self):
        self.setup.start("codex", "login")
        with self.assertRaises(ValueError):
            self.setup.start("codex", "login")

    def test_a_cancelled_session_stops(self):
        self.setup.start("codex", "login")
        self.setup.cancel()
        self.assertFalse(self.drain()["running"])

    def test_an_unknown_agent_or_action_never_reaches_a_process(self):
        for agent, action in (
            ("codex", "uninstall"), ("bash", "login"), ("codex", ""),
        ):
            with self.assertRaises(ValueError):
                self.setup.start(agent, action)

    def test_signing_in_to_a_cli_that_is_not_installed_says_so(self):
        os.environ["PATH"] = str(self.tmp / "empty")
        with self.assertRaises(ValueError) as caught:
            self.setup.start("codex", "login")
        self.assertIn("not installed", str(caught.exception))

    def test_a_full_screen_sign_in_is_refused_with_the_command_to_type(self):
        with self.assertRaises(ValueError) as caught:
            self.setup.start("agy", "login")
        self.assertIn("agy", str(caught.exception))
        self.assertIn("terminal", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
