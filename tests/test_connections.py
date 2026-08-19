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
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil import config as cfg  # noqa: E402
from aicouncil import connections  # noqa: E402

# `codex login status` answers in prose and by exit code; `codex login
# --device-auth` prints a URL and a short code and is the one flow that has to
# survive being read through a terminal this app owns. `--device-auth` is what
# the configured login_command actually passes - see AGENT_SETUP in config.py
# - because plain `codex login` waits on a local browser callback that a
# browser-hosted or Docker deployment can never receive.
FAKE_CODEX = '''#!/usr/bin/env python3
import sys

if sys.argv[1:] == ["login", "status"]:
    import os
    if os.environ.get("FAKE_SIGNED_OUT"):
        sys.stderr.write("Not logged in\\n")
        sys.exit(1)
    print("Logged in using ChatGPT")
    sys.exit(0)
if sys.argv[1:] == ["login", "--device-auth"]:
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

# A stand-in for a full-screen TUI sign-in: colour codes it would draw a menu
# with, and a single keystroke read raw rather than as a line. `setcbreak` is
# what a real TUI does on its own stdin so a keypress arrives immediately
# instead of waiting on the pty's line discipline for Enter - without it this
# fake could not tell a real single-key send from `write` forcing a newline.
FAKE_AGY = '''#!/usr/bin/env python3
import sys
import tty

sys.stdout.write("\\x1b[2J\\x1b[1;1H\\x1b[36mSign in with Google\\x1b[0m\\r\\n")
sys.stdout.flush()
tty.setcbreak(sys.stdin.fileno())
key = sys.stdin.read(1)
sys.stdout.write(f"\\r\\ngot key: {key!r}\\r\\n")
sys.exit(0)
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
if args[:2] == ["repo", "list"]:
    import json
    if os.environ.get("FAKE_GH_REPO_LIST_FAIL"):
        sys.stderr.write("error connecting to api.github.com\\n")
        sys.exit(1)
    print(json.dumps([
        {
            "nameWithOwner": "octocat/Hello-World",
            "description": "My first repository",
            "updatedAt": "2026-01-01T00:00:00Z",
            "isPrivate": False,
            "defaultBranchRef": {"name": "main"},
        },
        {
            "nameWithOwner": "octocat/Secret-Project",
            "description": None,
            "updatedAt": "2026-02-02T00:00:00Z",
            "isPrivate": True,
            "defaultBranchRef": {"name": "main"},
        },
    ]))
    sys.exit(0)
if args[:2] == ["api", "user"]:
    import json
    print(json.dumps({"login": "octocat", "id": 583231, "name": "The Octocat"}))
    sys.exit(0)
sys.exit(2)
'''


def write_fake(directory, name, body):
    path = Path(directory) / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _FakeBus:
    """Stands in for EventBus.publish - records calls, does not fan them out.

    SetupManager only ever calls the one method a real EventBus offers here;
    testing against a fake rather than a real EventBus keeps this module free
    of a dependency on aicouncil.events for what is a one-line contract.
    """

    def __init__(self, sink):
        self._sink = sink

    def publish(self, kind, **payload):
        event = {"kind": kind, **payload}
        self._sink.append(event)
        return event


class ConnectionsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-conn-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        write_fake(self.bin, "codex", FAKE_CODEX)
        write_fake(self.bin, "claude", FAKE_CLAUDE)
        write_fake(self.bin, "agy", FAKE_AGY)
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

    def test_codex_signs_in_with_device_auth_not_a_local_callback(self):
        # Plain `codex login` redirects an OAuth provider to a server on this
        # host's loopback interface - fine when the browser sharing that
        # loopback is on the same machine, broken for every browser-hosted or
        # Docker deployment. --device-auth is what makes signing in from
        # anywhere possible, so it has to be what actually gets run, not just
        # what the docs recommend.
        self.assertEqual(
            cfg.AGENT_SETUP["codex"]["login_command"],
            ["codex", "login", "--device-auth"],
        )

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

    def test_a_full_screen_sign_in_runs_like_any_other(self):
        # No more refusal: the browser draws a real terminal for this one
        # instead of the plain-text pane, but the session itself starts the
        # same way regardless of what login_tui says.
        self.setup.start("agy", "login")
        deadline = time.monotonic() + 5
        while "Sign in with Google" not in self.setup.status().get("raw_output", ""):
            self.assertLess(time.monotonic(), deadline, "no TUI output arrived")
            time.sleep(0.02)

    def test_write_raw_sends_no_trailing_newline(self):
        # The whole reason it exists rather than reusing `write`: a single
        # keystroke - an arrow key, Ctrl+C - is not a line and must not be
        # turned into one.
        self.setup.start("agy", "login")
        deadline = time.monotonic() + 5
        while "Sign in with Google" not in self.setup.status().get("raw_output", ""):
            self.assertLess(time.monotonic(), deadline, "no TUI output arrived")
            time.sleep(0.02)
        self.setup.write_raw("q")
        session = self.drain()
        self.assertIn("got key: 'q'", session["raw_output"])

    def test_raw_output_keeps_escape_codes_the_sanitized_output_strips(self):
        self.setup.start("agy", "login")
        deadline = time.monotonic() + 5
        while not self.setup.status().get("raw_output"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        session = self.setup.status()
        self.assertIn("\x1b[36m", session["raw_output"])
        self.assertNotIn("\x1b[36m", session["output"])

    def test_resize_does_not_raise_even_without_a_running_session(self):
        self.setup.resize(24, 80)  # nothing running; must be a no-op, not a crash

    def test_every_session_gets_a_distinct_id(self):
        first = self.setup.start("codex", "login")
        self.setup.cancel()
        self.drain()
        second = self.setup.start("codex", "login")
        self.assertNotEqual(first["id"], second["id"])

    def test_a_bus_is_told_about_raw_bytes_as_they_arrive(self):
        received = []
        manager = connections.SetupManager(bus=_FakeBus(received))
        session = manager.start("agy", "login")
        deadline = time.monotonic() + 5
        while not received:
            self.assertLess(time.monotonic(), deadline, "no pty event was published")
            time.sleep(0.02)
        self.assertEqual(received[0]["session"], session["id"])
        self.assertEqual(received[0]["seq"], 1)
        self.assertIn("Sign in with Google", received[0]["data"])
        manager.cancel()
        deadline = time.monotonic() + 5
        while manager.status().get("running"):
            self.assertLess(time.monotonic(), deadline, "the fake never exited")
            time.sleep(0.02)


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
        # `github_connect` now also fills in a missing global git identity -
        # a real `git config --global`, against a home directory of its own
        # rather than the one running this suite.
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._previous_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._previous_home
        os.environ.pop("FAKE_GH_STATE", None)
        super().tearDown()

    def global_git_config(self, key):
        proc = subprocess.run(
            ["git", "config", "--global", "--get", key],
            capture_output=True, text=True, check=False,
        )
        return proc.stdout.strip()

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

    def test_connecting_fills_in_a_missing_git_identity(self):
        # `setup-git` alone leaves a fresh container able to authenticate a
        # push but with no one to attribute the commit to - this is what
        # makes the very first commit not fail on that.
        self.assertEqual(self.global_git_config("user.name"), "")
        connections.github_connect(VALID_TOKEN)
        self.assertEqual(self.global_git_config("user.name"), "The Octocat")
        self.assertEqual(
            self.global_git_config("user.email"),
            "583231+octocat@users.noreply.github.com",
        )

    def test_connecting_does_not_overwrite_an_existing_git_identity(self):
        subprocess.run(
            ["git", "config", "--global", "user.name", "Someone Else"],
            check=True,
        )
        connections.github_connect(VALID_TOKEN)
        self.assertEqual(self.global_git_config("user.name"), "Someone Else")
        # The other half of the pair was still genuinely missing, and still
        # gets filled in - this is "leave what is set alone", not "leave the
        # pair alone once either half exists".
        self.assertEqual(
            self.global_git_config("user.email"),
            "583231+octocat@users.noreply.github.com",
        )

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

    def test_a_parent_scope_satisfies_the_child_it_implies(self):
        # GitHub never issues a token carrying the implied children, so a flat
        # membership test reports `read:org` missing from a token holding
        # `admin:org` - which is a scope it demonstrably has.
        wanted = ["repo", "workflow", "read:org"]
        self.assertEqual(
            connections.missing_scopes(wanted, ["repo", "workflow", "admin:org"]),
            [],
        )
        self.assertEqual(
            connections.missing_scopes(wanted, ["repo", "workflow", "write:org"]),
            [],
        )

    def test_implication_is_transitive(self):
        # admin:org -> write:org -> read:org, without the map stating that edge.
        self.assertIn("read:org", connections.expand_scopes(["admin:org"]))
        self.assertIn("public_repo", connections.expand_scopes(["repo"]))

    def test_a_genuinely_absent_scope_is_still_reported(self):
        # The fix must not turn the check into one that never fires.
        self.assertEqual(
            connections.missing_scopes(
                ["repo", "workflow", "read:org"], ["repo", "read:org"],
            ),
            ["workflow"],
        )

    def test_a_token_reporting_no_scopes_is_not_called_deficient(self):
        # A fine-grained token lists no classic scopes at all. Naming every
        # wanted scope as missing would send the operator to fix a token that
        # is most likely correct.
        self.assertEqual(
            connections.missing_scopes(["repo", "workflow"], []), [],
        )

    def test_redact_covers_both_token_shapes(self):
        text = (
            "classic ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa and "
            "fine-grained github_pat_bbbbbbbbbbbbbbbbbbbbbbbb here"
        )
        cleaned = connections.redact(text)
        self.assertNotIn("ghp_a", cleaned)
        self.assertNotIn("github_pat_b", cleaned)
        self.assertEqual(cleaned.count("[redacted]"), 2)


class TestGitHubRepoListing(ConnectionsTestBase):
    """The "GitHub repo" half of the working-folder picker - what it has to
    show before anything is cloned."""

    def setUp(self):
        super().setUp()
        write_fake(self.bin, "gh", FAKE_GH)
        # FAKE_GH logs every call's argv to this path regardless of which
        # subcommand it is - unset, it KeyErrors before reaching `repo list`.
        os.environ["FAKE_GH_STATE"] = str(self.tmp / "gh-state")

    def tearDown(self):
        os.environ.pop("FAKE_GH_STATE", None)
        super().tearDown()

    def test_lists_what_gh_reports(self):
        found = connections.github_repos()
        self.assertTrue(found["connected"])
        self.assertEqual(found["error"], "")
        repos = {r["repo"]: r for r in found["repos"]}
        self.assertEqual(
            repos["octocat/Hello-World"]["description"], "My first repository"
        )
        self.assertFalse(repos["octocat/Hello-World"]["private"])
        self.assertTrue(repos["octocat/Secret-Project"]["private"])
        # `gh` reports null, not "", for a repo with no description - the UI
        # needs a string to render, not a value it has to guard against.
        self.assertEqual(repos["octocat/Secret-Project"]["description"], "")

    def test_a_missing_gh_is_not_reported_as_connected(self):
        os.environ["PATH"] = str(self.tmp / "empty")
        found = connections.github_repos()
        self.assertFalse(found["connected"])
        self.assertEqual(found["repos"], [])
        self.assertIn("not installed", found["error"])

    def test_a_failed_call_reports_the_error_not_an_empty_list(self):
        self.addCleanup(os.environ.pop, "FAKE_GH_REPO_LIST_FAIL", None)
        os.environ["FAKE_GH_REPO_LIST_FAIL"] = "1"
        found = connections.github_repos()
        self.assertFalse(found["connected"])
        self.assertEqual(found["repos"], [])
        self.assertTrue(found["error"])


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
