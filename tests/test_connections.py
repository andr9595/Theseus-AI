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


# A stand-in for the GitHub CLI that behaves like the real one in the two ways
# that matter here: it takes the token on *stdin* rather than in an argument,
# and it is the thing that remembers the login afterwards. It records every
# argv it is called with so a test can prove the token never appeared in one.
FAKE_GH = '''#!/usr/bin/env python3
import os
import sys

state = os.environ["FAKE_GH_STATE"]
with open(state + ".argv", "a", encoding="utf-8") as fh:
    fh.write(repr(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
if args[:2] == ["auth", "login"]:
    token = sys.stdin.read().strip()
    if token != "ghp_valid0000000000000000000000000000":
        sys.stderr.write(f"error validating token: bad credentials ({token})\\n")
        sys.exit(1)
    with open(state, "w", encoding="utf-8") as fh:
        fh.write(token)
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    if not os.path.exists(state):
        sys.stderr.write("You are not logged into any GitHub hosts.\\n")
        sys.exit(1)
    sys.stderr.write(
        "github.com\\n"
        "  \\u2713 Logged in to github.com account octocat (keyring)\\n"
        "  - Token: gho_************************************\\n"
        "  - Token scopes: 'read:org', 'repo'\\n"
    )
    sys.exit(0)
if args[:2] == ["auth", "setup-git"]:
    sys.exit(0)
if args[:2] == ["auth", "logout"]:
    if not os.path.exists(state):
        sys.stderr.write("not logged in to any hosts\\n")
        sys.exit(1)
    os.remove(state)
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


VALID_TOKEN = "ghp_valid0000000000000000000000000000"


class TestGitHubConnection(ConnectionsTestBase):
    """The one credential path in the app, against a fake `gh`.

    The behaviour under test is not "does it call gh" but the three promises
    the UI makes about the token: it never reaches a command line, this app
    never writes it anywhere, and it is scrubbed out of anything shown back.
    """

    def setUp(self):
        super().setUp()
        write_fake(self.bin, "gh", FAKE_GH)
        self.state = self.tmp / "gh-state"
        os.environ["FAKE_GH_STATE"] = str(self.state)

    def tearDown(self):
        os.environ.pop("FAKE_GH_STATE", None)
        super().tearDown()

    def argv_log(self):
        path = Path(str(self.state) + ".argv")
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_it_reports_not_connected_before_anything_happens(self):
        found = connections.github_status()
        self.assertTrue(found["installed"])
        self.assertFalse(found["connected"])

    def test_a_missing_gh_is_not_reported_as_signed_out(self):
        # Same three-valued honesty the agent cards have: "cannot ask" and
        # "asked, and the answer was no" are different states.
        os.environ["PATH"] = str(self.tmp / "empty")
        found = connections.github_status()
        self.assertFalse(found["installed"])
        self.assertFalse(found["connected"])
        self.assertIn("not installed", found["detail"])

    def test_connecting_reports_the_account_and_scopes(self):
        found = connections.github_connect(VALID_TOKEN)
        self.assertTrue(found["connected"])
        self.assertEqual(found["account"], "octocat")
        self.assertEqual(found["scopes"], ["read:org", "repo"])
        self.assertEqual(found["storage"], "keyring")

    def test_the_token_never_reaches_a_command_line(self):
        # The whole reason `--with-token` reads stdin: an argument is readable
        # in `ps` by any process on the box for as long as the child lives.
        connections.github_connect(VALID_TOKEN)
        self.assertNotIn(VALID_TOKEN, self.argv_log())
        self.assertIn("'--with-token'", self.argv_log())

    def test_nothing_in_this_app_keeps_the_token(self):
        connections.github_connect(VALID_TOKEN)
        found = connections.github_status()
        # Not in what the browser is handed...
        self.assertNotIn(VALID_TOKEN, repr(found))
        # ...and not in this app's config, which is the file a "save the token"
        # design would have put it in.
        store = cfg.ConfigStore(self.tmp / "config.json")
        conf = store.all()
        self.assertNotIn(VALID_TOKEN, repr(conf))
        # No key was added to hold one either. Checked by name rather than by
        # searching for "token", which legitimately appears in
        # `context_window_tokens`.
        self.assertNotIn("github", conf)
        self.assertNotIn("github_token", conf)
        # ...and nothing was dropped beside the config file for it.
        self.assertEqual(
            [p.name for p in (self.tmp).glob("*token*")], [],
        )

    def test_a_rejected_token_is_not_echoed_back_in_the_error(self):
        # The fake spits the token back the way a real CLI can when it quotes
        # what it was given. The most likely home for that message is a
        # screenshot, so it is scrubbed on the way out.
        bad = "ghp_wrong000000000000000000000000000000"
        with self.assertRaises(ValueError) as caught:
            connections.github_connect(bad)
        self.assertNotIn(bad, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))

    def test_setup_git_runs_so_a_plain_push_uses_the_same_login(self):
        connections.github_connect(VALID_TOKEN)
        self.assertIn("'setup-git'", self.argv_log())

    def test_a_token_with_whitespace_is_refused_before_gh_is_run(self):
        for bad in ("", "   ", "ghp_one two", "ghp_one\nghp_two"):
            with self.assertRaises(ValueError):
                connections.github_connect(bad)
        self.assertEqual(self.argv_log(), "")

    def test_disconnecting_asks_gh_to_forget_it(self):
        connections.github_connect(VALID_TOKEN)
        found = connections.github_disconnect()
        self.assertFalse(found["connected"])
        self.assertFalse(self.state.exists())

    def test_disconnecting_when_never_connected_is_not_an_error(self):
        # The caller asked for "not logged in" and that is the state they get.
        found = connections.github_disconnect()
        self.assertFalse(found["connected"])

    def test_missing_scopes_are_named_rather_than_left_to_a_failed_push(self):
        found = connections.github_connect(VALID_TOKEN)
        # The fake grants read:org and repo but not workflow.
        self.assertEqual(found["missing_scopes"], ["workflow"])

    def test_an_ambient_gh_token_cannot_masquerade_as_the_stored_login(self):
        # A GH_TOKEN in the environment silently outranks the stored login, so
        # a card reading "connected" would be describing a credential this app
        # neither set nor can clear.
        os.environ["GH_TOKEN"] = "ghp_ambient00000000000000000000000000"
        try:
            self.assertFalse(connections.github_status()["connected"])
        finally:
            os.environ.pop("GH_TOKEN", None)

    def test_redact_covers_both_token_shapes(self):
        text = (
            "classic ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa and "
            "fine-grained github_pat_bbbbbbbbbbbbbbbbbbbbbbbb here"
        )
        cleaned = connections.redact(text)
        self.assertNotIn("ghp_a", cleaned)
        self.assertNotIn("github_pat_b", cleaned)
        self.assertEqual(cleaned.count("[redacted]"), 2)


class TestGitHubSetupRouting(ConnectionsTestBase):
    def setUp(self):
        super().setUp()
        self.setup = connections.SetupManager()

    def test_github_offers_an_install_but_not_a_terminal_login(self):
        # Installing `gh` is a long download worth watching in the pane; the
        # token is one request and has no business on a pseudo-terminal.
        with self.assertRaises(ValueError) as caught:
            self.setup.start("github", "login")
        self.assertIn("token", str(caught.exception))

    def test_the_github_install_is_routed_through_the_bundled_script(self):
        argv = connections.SetupManager._argv("github", "install")
        self.assertEqual(argv[0], "bash")
        self.assertIn("install-deps.sh", argv[1])
        self.assertIn("--gh", argv)


if __name__ == "__main__":
    unittest.main()
