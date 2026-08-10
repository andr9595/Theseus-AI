"""HTTP layer tests, exercised against a real socket.

The auth and Origin checks here are the boundary between "a local dev tool"
and "any web page you visit can run a coding agent on your source tree", so
they get direct coverage.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil import config as cfg  # noqa: E402
from aicouncil.config import ConfigStore, agent_catalog, agent_for  # noqa: E402
from aicouncil.server import make_server, serve_forever_in_thread  # noqa: E402


class ServerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-http-"))
        # Set before the server is built, because that is when its pipeline
        # binds the directory it will read and write transcripts in. No test
        # here starts a run that reaches disk, but the server is a real one -
        # nothing about it should be able to reach the operator's own history.
        cls._previous_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(cls.tmp / "xdg")
        cls.store = ConfigStore(cls.tmp / "config.json")
        cls.server, cls.state, cls.url = make_server(cls.store, port=0)
        cls.thread = serve_forever_in_thread(cls.server)
        host, port = cls.server.server_address[:2]
        cls.base = f"http://{host}:{port}"
        cls.token = cls.state.token

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        if cls._previous_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = cls._previous_xdg
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def request(self, path, method="GET", body=None, token=None, headers=None):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        tok = self.token if token is None else token
        if tok:
            req.add_header("X-AC-Token", tok)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}


class TestAuth(ServerTestBase):
    def test_valid_token_is_accepted(self):
        status, data = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_missing_token_is_rejected(self):
        status, _ = self.request("/api/state", token="")
        self.assertEqual(status, 401)

    def test_wrong_token_is_rejected(self):
        status, _ = self.request("/api/state", token="not-the-token")
        self.assertEqual(status, 401)

    def test_token_may_be_passed_as_a_query_parameter(self):
        status, data = self.request(f"/api/state?token={self.token}", token="")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_cross_origin_request_is_rejected(self):
        # The drive-by CSRF case: a page on the open web POSTing to localhost.
        status, _ = self.request(
            "/api/state", headers={"Origin": "https://evil.example.com"}
        )
        self.assertEqual(status, 403)

    def test_same_origin_request_is_allowed(self):
        status, _ = self.request(
            "/api/state", headers={"Origin": f"http://127.0.0.1:{self.server.server_address[1]}"}
        )
        self.assertEqual(status, 200)

    def test_rebinding_host_header_is_rejected(self):
        status, _ = self.request("/api/state", headers={"Host": "attacker.example.com"})
        self.assertEqual(status, 403)


class TestLaunchTicket(ServerTestBase):
    """A session token on a browser's command line is readable by every other
    user on the machine for as long as the window is open, so the launch URL
    carries a ticket that buys the token once instead."""

    def test_session_endpoint_rejects_get(self):
        status, _ = self.request("/api/session", token="")
        self.assertEqual(status, 405)

    def test_the_launch_url_does_not_carry_the_session_token(self):
        self.assertIn("ticket=", self.url)
        self.assertNotIn(self.state.token, self.url)

    def test_ticket_buys_the_token_exactly_once(self):
        ticket = self.state.ticket
        status, data = self.request(
            "/api/session", method="POST", body={"ticket": ticket}, token=""
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["token"], self.token)

        # Replay: whoever read the ticket out of the process table arrives
        # second, and second gets nothing.
        status, _ = self.request(
            "/api/session", method="POST", body={"ticket": ticket}, token=""
        )
        self.assertEqual(status, 401)

    def test_unknown_ticket_is_rejected(self):
        status, _ = self.request(
            "/api/session", method="POST", body={"ticket": "not-the-ticket"}, token=""
        )
        self.assertEqual(status, 401)


class TestStaticFiles(ServerTestBase):
    def test_index_is_served_without_a_token(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=15) as res:
            self.assertEqual(res.status, 200)
            body = res.read().decode()
        self.assertIn("Theseus AI", body)

    def test_caveman_settings_live_with_the_modes_that_use_them(self):
        # All three modes toggle it from their composer gear, so Settings
        # carries no checkbox for any of them - including Project, which used
        # to own one and could overwrite the gear's value on save.
        with urllib.request.urlopen(f"{self.base}/", timeout=15) as res:
            body = res.read().decode()
        self.assertNotIn('id="caveman-council"', body)
        self.assertNotIn('id="caveman-chat"', body)
        self.assertNotIn('id="caveman-project"', body)

        with urllib.request.urlopen(f"{self.base}/app.js", timeout=15) as res:
            script = res.read().decode()
        self.assertIn(
            "const cavemanMode = project ? 'project' : (chat ? 'chat' : 'council');",
            script,
        )
        self.assertIn("row('caveman', 'Caveman mode'", script)
        self.assertIn("patchConfig({ caveman: { [cavemanMode]:", script)

    def test_efficiency_settings_live_with_the_modes_that_use_them(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=15) as res:
            body = res.read().decode()
        self.assertNotIn('id="efficiency-council"', body)
        self.assertNotIn('id="efficiency-chat"', body)
        self.assertNotIn('id="efficiency-project"', body)

        with urllib.request.urlopen(f"{self.base}/app.js", timeout=15) as res:
            script = res.read().decode()
        self.assertIn(
            "const efficiencyMode = project ? 'project' : "
            "(chat ? 'chat' : 'council');",
            script,
        )
        self.assertIn("row('efficiency', 'Efficiency mode'", script)
        self.assertIn("patchConfig({", script)
        self.assertIn("efficiency: {", script)

    def test_deliberation_effort_is_settable_and_saved(self):
        # A knob only reachable by hand-editing the config file is half a
        # setting, so this checks both ends: the control is served, and the
        # save collects it rather than leaving the stored value behind.
        with urllib.request.urlopen(f"{self.base}/", timeout=15) as res:
            body = res.read().decode()
        self.assertIn('id="council-deliberation-effort"', body)

        with urllib.request.urlopen(f"{self.base}/app.js", timeout=15) as res:
            script = res.read().decode()
        self.assertIn(
            "$('#council-deliberation-effort').value = "
            "council.deliberation_effort || '';",
            script,
        )
        self.assertIn(
            "deliberation_effort: $('#council-deliberation-effort').value,",
            script,
        )

    def test_project_has_its_own_run_options_cogwheel(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=15) as res:
            body = res.read().decode()
        self.assertEqual(body.count('class="project-gear-btn icon-round"'), 2)
        self.assertIn('aria-label="Project options"', body)
        goal_box = body.split('class="project-goal-box"', 1)[1].split(
            'class="field-hint"', 1
        )[0]
        self.assertIn('id="project-goal"', goal_box)
        self.assertIn('class="project-gear-btn icon-round"', goal_box)

        with urllib.request.urlopen(f"{self.base}/app.js", timeout=15) as res:
            script = res.read().decode()
        self.assertIn("const project = mode === 'project';", script)
        self.assertIn("project ? 'project' : (chat ? 'chat' : 'council')", script)
        self.assertIn("$$('.project-gear-btn').forEach", script)
        # 'run' and 'agents' are the live tab ids in index.html; the pair this
        # once asserted ('project'/'stages') was renamed out of the dialog.
        self.assertIn("openSettings(project ? 'run' : 'agents')", script)

    def test_completed_run_is_only_rendered_in_its_own_mode(self):
        with urllib.request.urlopen(f"{self.base}/app.js", timeout=15) as res:
            script = res.read().decode()
        helper = script.split("function runOnScreen()", 1)[1].split(
            "/** The folder", 1
        )[0]
        self.assertIn("mode === uiMode() ? run : null", helper)
        thread = script.split("function renderThread()", 1)[1].split(
            "/* ---- Projects", 1
        )[0]
        self.assertIn("const run = runOnScreen();", thread)

    def test_accepted_chat_submit_clears_only_the_submitted_message(self):
        with urllib.request.urlopen(f"{self.base}/app.js", timeout=15) as res:
            script = res.read().decode()
        self.assertIn("const submittedValue = input.value;", script)
        self.assertIn("if (input.value === submittedValue) {", script)
        self.assertIn("input.value = '';", script)

    def test_completed_chat_stays_attached_for_the_next_message(self):
        with urllib.request.urlopen(f"{self.base}/continuation.js", timeout=15) as res:
            continuation = res.read().decode()
        with urllib.request.urlopen(f"{self.base}/app.js", timeout=15) as res:
            script = res.read().decode()

        # The pure decision covers every terminal transcript, and refuses all
        # states in which silently attaching would be surprising or invalid.
        for terminal in ("complete", "failed", "cancelled"):
            self.assertIn(f"'{terminal}'", continuation)
        for guard in (
            "options.busy",
            "options.fresh",
            "options.openChat",
            "mode !== options.mode",
            "runWorkspace !== options.workspace",
        ):
            self.assertIn(guard, continuation)

        handler = script.split("on('state', (d) => {", 1)[1].split(
            "on('stage_started'", 1
        )[0]
        terminal = handler.split("if (!state.busy) {", 1)[1]
        self.assertIn("restoreContinuation();", terminal)
        load_state = script.split("async function loadState()", 1)[1].split(
            "async function startRun()", 1
        )[0]
        # This is the missed-event/reload/commit-refresh case that produced
        # two newest root chats in the operator's actual history.
        self.assertIn("restoreContinuation();", load_state)
        self.assertIn("state.freshChat = false;", script)
        self.assertIn("function startFreshChat()", script)
        self.assertIn("if (!alreadyAttachedLatest) clearContinuation();", script)

        with urllib.request.urlopen(f"{self.base}/", timeout=15) as res:
            html = res.read().decode()
        self.assertLess(
            html.index('<script src="/continuation.js"></script>'),
            html.index('<script src="/app.js"></script>'),
        )

    def test_chat_hides_intermediate_agent_output(self):
        with urllib.request.urlopen(f"{self.base}/app.js", timeout=15) as res:
            script = res.read().decode()
        handler = script.split("on('stage_output', (d) => {", 1)[1].split(
            "on('stage_finished'", 1
        )[0]
        self.assertIn("if (state.run && state.run.solo)", handler)
        self.assertNotIn("live.textContent", handler)

    def test_security_headers_are_present(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=15) as res:
            csp = res.headers.get("Content-Security-Policy")
            self.assertIn("default-src 'none'", csp)
            self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")

    def test_path_traversal_is_blocked(self):
        try:
            with urllib.request.urlopen(f"{self.base}/../../etc/passwd", timeout=15) as res:
                body = res.read().decode()
                self.assertNotIn("root:", body)
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, (400, 403, 404))


class TestConcurrentConfigWriters(unittest.TestCase):
    """Two app instances sharing one config file must not clobber each other.

    Running a second window (or leaving an old server up) previously reverted
    the first one's settings — including the approval gate and rollback
    protection — to whatever the second instance loaded at startup.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-cfg-"))
        self.path = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_instance_does_not_revert_the_first(self):
        a = ConfigStore(self.path)
        b = ConfigStore(self.path)  # started before A's change, stale copy

        a.update({"zero_touch": True})
        b.update({"port": 9999})  # unrelated key

        self.assertTrue(
            ConfigStore(self.path).get("zero_touch"),
            "an unrelated write from a second instance reverted zero_touch",
        )
        self.assertEqual(ConfigStore(self.path).get("port"), 9999)

    def test_safety_toggles_survive_a_stale_writer(self):
        a = ConfigStore(self.path)
        b = ConfigStore(self.path)

        a.update({"safety_snapshot": False, "zero_touch": True})
        b.update({"house_rules": "use tabs"})

        fresh = ConfigStore(self.path).all()
        self.assertFalse(fresh["safety_snapshot"])
        self.assertTrue(fresh["zero_touch"])
        self.assertEqual(fresh["house_rules"], "use tabs")

    def test_remembering_a_workspace_does_not_revert_the_other_instance(self):
        # This one runs at the start of every run with a chosen folder, which
        # made it the likeliest write to be holding a stale copy - and it was
        # the only writer that did not re-read first.
        a = ConfigStore(self.path)
        b = ConfigStore(self.path)

        b.update({"zero_touch": True, "house_rules": "use tabs"})
        a.remember_workspace("/tmp/some-project")

        fresh = ConfigStore(self.path).all()
        self.assertEqual(fresh["workspace"], "/tmp/some-project")
        self.assertTrue(fresh["zero_touch"], "starting a run reverted zero_touch")
        self.assertEqual(fresh["house_rules"], "use tabs")

    def test_same_key_written_twice_takes_the_later_value(self):
        a = ConfigStore(self.path)
        b = ConfigStore(self.path)
        a.update({"zero_touch": True})
        b.update({"zero_touch": False})
        self.assertFalse(ConfigStore(self.path).get("zero_touch"))


class TestAgentAssignment(unittest.TestCase):
    """Either agent may be assigned to either job.

    The invariant: a command and its auto-approve flag move together. Claude's
    flag on `codex` is rejected outright; the reverse is worse, because the CLI
    starts, finds no permission grant and blocks on a prompt forever.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-agents-"))
        self.store = ConfigStore(self.tmp / "config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_pair_each_job_with_an_agent(self):
        conf = self.store.all()
        self.assertEqual(agent_for(conf["providers"]["drafter"]), "codex")
        self.assertEqual(agent_for(conf["providers"]["polisher"]), "claude")

    def test_assigning_an_agent_swaps_the_command_and_its_flags(self):
        conf = self.store.update({"providers": {"polisher": {"agent": "codex"}}})
        polisher = conf["providers"]["polisher"]
        self.assertEqual(polisher["command"], ["codex", "exec", "{prompt}"])
        self.assertEqual(
            polisher["auto_approve_args"],
            ["--dangerously-bypass-approvals-and-sandbox"],
        )
        # Everything that is not the CLI is untouched: only the agent changed.
        self.assertEqual(polisher["id"], "polisher")
        self.assertEqual(polisher["timeout_seconds"], 1800)

    def test_the_same_agent_may_hold_both_jobs(self):
        conf = self.store.update({"providers": {"drafter": {"agent": "claude"}}})
        self.assertEqual(agent_for(conf["providers"]["drafter"]), "claude")
        self.assertEqual(agent_for(conf["providers"]["polisher"]), "claude")

    def test_the_agent_key_is_never_persisted(self):
        self.store.update({"providers": {"polisher": {"agent": "codex"}}})
        raw = json.loads((self.tmp / "config.json").read_text())
        self.assertNotIn("agent", raw["providers"]["polisher"])

    def test_switching_agents_clears_the_model(self):
        # A Codex slug handed to `claude --model` fails at launch.
        self.store.update({"providers": {"polisher": {"model": "gpt-5.5"}}})
        conf = self.store.update({"providers": {"polisher": {"agent": "codex"}}})
        self.assertEqual(conf["providers"]["polisher"]["model"], "")

    def test_switching_agents_clears_the_reasoning_effort(self):
        # `ultra` exists only on some Codex models and not on Claude at all.
        # Carried over it would be silently ignored, and the chip would go on
        # claiming a depth the run never used.
        self.store.update({"providers": {"drafter": {"effort": "ultra"}}})
        conf = self.store.update({"providers": {"drafter": {"agent": "claude"}}})
        self.assertEqual(conf["providers"]["drafter"]["effort"], "")

    def test_switching_agents_swaps_the_effort_flag(self):
        # Claude takes `--effort high`; Codex takes a config override. Neither
        # spelling means anything to the other binary.
        conf = self.store.update({"providers": {"polisher": {"agent": "codex"}}})
        self.assertEqual(
            conf["providers"]["polisher"]["effort_args"],
            ["-c", "model_reasoning_effort={effort}"],
        )
        conf = self.store.update({"providers": {"drafter": {"agent": "claude"}}})
        self.assertEqual(
            conf["providers"]["drafter"]["effort_args"], ["--effort", "{effort}"]
        )

    def test_a_changed_agent_beats_a_stale_command_in_the_same_patch(self):
        # The Settings form submits both. Letting the textarea win would pair
        # Claude's binary with Codex's permission flag.
        conf = self.store.update({
            "providers": {
                "polisher": {
                    "agent": "codex",
                    "command": ["claude", "-p", "{prompt}"],
                    "auto_approve_args": ["--dangerously-skip-permissions"],
                }
            }
        })
        self.assertEqual(conf["providers"]["polisher"]["command"][0], "codex")
        self.assertEqual(
            conf["providers"]["polisher"]["auto_approve_args"],
            ["--dangerously-bypass-approvals-and-sandbox"],
        )

    def test_a_hand_edited_command_survives_an_unchanged_agent(self):
        conf = self.store.update({
            "providers": {
                "polisher": {"agent": "claude", "command": ["claude", "--print", "{prompt}"]}
            }
        })
        self.assertEqual(
            conf["providers"]["polisher"]["command"], ["claude", "--print", "{prompt}"]
        )

    def test_switching_agents_swaps_the_streaming_flags_too(self):
        # `--output-format stream-json` is Claude's way of narrating its work;
        # left behind on `codex exec` it is simply an unknown flag.
        conf = self.store.update({"providers": {"polisher": {"agent": "codex"}}})
        self.assertEqual(conf["providers"]["polisher"]["stream_args"], [])

        conf = self.store.update({"providers": {"drafter": {"agent": "claude"}}})
        self.assertIn("stream-json", conf["providers"]["drafter"]["stream_args"])

    def test_an_existing_config_picks_up_the_streaming_flags(self):
        # A config written before this field existed must gain it, or Claude
        # goes on printing nothing until the run is over.
        (self.tmp / "config.json").write_text(json.dumps({
            "providers": {"polisher": {
                "command": ["claude", "-p", "{prompt}"],
                "auto_approve_args": ["--dangerously-skip-permissions"],
            }},
        }))
        conf = ConfigStore(self.tmp / "config.json").all()
        self.assertIn("stream-json", conf["providers"]["polisher"]["stream_args"])

    def test_an_uncatalogued_command_reads_as_custom(self):
        conf = self.store.update({
            "providers": {"drafter": {"command": ["python3", "mock-agent.py", "{prompt}"]}}
        })
        self.assertEqual(agent_for(conf["providers"]["drafter"]), "custom")

    def test_switching_agents_swaps_the_read_only_flags_too(self):
        # Codex's `--sandbox read-only` handed to `claude` is an unknown flag,
        # which is the pairing this swap exists to make impossible.
        conf = self.store.update({"providers": {"solo": {"agent": "codex"}}})
        self.assertEqual(
            conf["providers"]["solo"]["read_only_args"],
            ["--sandbox", "read-only"],
        )
        conf = self.store.update({"providers": {"solo": {"agent": "claude"}}})
        self.assertEqual(
            conf["providers"]["solo"]["read_only_args"],
            ["--permission-mode", "plan"],
        )


class TestGlobalAgentSettings(unittest.TestCase):
    """One CLI, one model, one reasoning level - wherever it is sitting.

    Antigravity is a single login with a single catalogue. Choosing a model for
    it on the Projects tab and finding the council still on the old one is a
    setting that did not take, not a per-tab preference: the CLI is the thing
    being configured, and every chair it holds runs the same binary.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-global-"))
        self.path = self.tmp / "config.json"
        self.store = ConfigStore(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _agy(self, conf):
        """Every provider that runs `agy`: the council seat and the QA chair."""
        return [
            p for p in conf["providers"].values()
            if isinstance(p, dict) and agent_for(p) == "agy"
        ]

    def test_a_model_chosen_on_one_chair_is_the_clis_everywhere(self):
        conf = self.store.update({"providers": {"qa": {"model": "gemini-3.6-flash"}}})
        self.assertTrue(len(self._agy(conf)) > 1)
        for provider in self._agy(conf):
            self.assertEqual(provider["model"], "gemini-3.6-flash")
        self.assertEqual(conf["agent_settings"]["agy"]["model"], "gemini-3.6-flash")

    def test_an_effort_chosen_on_one_chair_is_the_clis_everywhere(self):
        conf = self.store.update({"providers": {"council_agy": {"effort": "high"}}})
        for provider in self._agy(conf):
            self.assertEqual(provider["effort"], "high")

    def test_a_hand_typed_model_is_remembered_for_every_chair(self):
        # The picker saves what was typed so it is offered next time. Offered
        # on one card and missing on another would be the same list problem in
        # a new place.
        conf = self.store.update({
            "providers": {"qa": {"model": "new-agy-model", "models": ["new-agy-model"]}}
        })
        for provider in self._agy(conf):
            self.assertEqual(provider["models"], ["new-agy-model"])

    def test_the_setting_is_stored_against_the_cli(self):
        # The provider copies on disk are derived from this one, projected on
        # every load. Written the other way round, five chairs would be five
        # answers to the same question.
        self.store.update({"providers": {"qa": {"model": "gemini-3.6-flash"}}})
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["agent_settings"]["agy"]["model"], "gemini-3.6-flash")

    def test_a_stale_copy_in_the_same_save_does_not_undo_an_edit(self):
        # Settings posts every card at once, so a save that changes Claude's
        # model on the council card also carries the chat card's copy of the
        # old one. Taking the last one read would discard the edit.
        self.store.update({"providers": {"council_claude": {"model": "opus"}}})
        conf = self.store.update({
            "providers": {
                "council_claude": {"model": "sonnet"},
                "solo": {"model": "opus"},
            }
        })
        self.assertEqual(conf["providers"]["solo"]["model"], "sonnet")

    def test_a_hand_written_command_keeps_its_own(self):
        # A custom template is nobody's catalogued agent; there is no CLI for
        # it to share a setting with.
        self.store.update({
            "providers": {"drafter": {"command": ["python3", "mock-agent.py", "{prompt}"]}}
        })
        self.store.update({"providers": {"drafter": {"model": "mock-1"}}})
        conf = self.store.update({"providers": {"council_claude": {"model": "opus"}}})
        self.assertEqual(conf["providers"]["drafter"]["model"], "mock-1")

    def test_a_swapped_chair_arrives_on_the_new_clis_settings(self):
        # Not the departing CLI's model, which would be rejected at launch,
        # and not a blank either: the arriving CLI already has one.
        self.store.update({"providers": {"council_codex": {"model": "gpt-5.5"}}})
        conf = self.store.update({"providers": {"qa": {"agent": "codex"}}})
        self.assertEqual(conf["providers"]["qa"]["model"], "gpt-5.5")
        # And the CLI that left is untouched by having been swapped out.
        self.assertEqual(conf["agent_settings"]["agy"]["model"], "")

    def test_an_existing_config_adopts_what_each_cli_was_already_set_to(self):
        # Written before the setting was global: the same CLI carried its own
        # model in every chair. The council seat is the one the operator sees
        # on the bench, so it is the one that wins.
        self.path.write_text(json.dumps({
            "providers": {
                "council_agy": {
                    "command": ["agy", "--prompt={prompt}"],
                    "model": "gemini-3.6-flash",
                    "models": ["gemini-3.6-flash"],
                    "effort": "high",
                },
                "qa": {
                    "command": ["agy", "--prompt={prompt}"],
                    "model": "gemini-3.6-pro",
                    "models": ["gemini-3.6-pro"],
                },
            },
        }))
        conf = ConfigStore(self.path).all()
        self.assertEqual(conf["agent_settings"]["agy"]["model"], "gemini-3.6-flash")
        self.assertEqual(conf["providers"]["qa"]["model"], "gemini-3.6-flash")
        self.assertEqual(conf["providers"]["qa"]["effort"], "high")
        # The remembered lists are unioned rather than picked between: they are
        # only names typed into the picker, and a precedence rule would lose
        # one of them for no reason.
        self.assertEqual(
            conf["agent_settings"]["agy"]["models"],
            ["gemini-3.6-flash", "gemini-3.6-pro"],
        )

    def test_an_adopted_config_is_not_re_adopted_afterwards(self):
        # Once the settings exist they are the source; re-reading the provider
        # copies would resurrect a value the operator had since cleared.
        self.store.update({"providers": {"council_agy": {"model": "gemini-3.6-flash"}}})
        self.store.update({"providers": {"council_agy": {"model": ""}}})
        conf = ConfigStore(self.path).all()
        self.assertEqual(conf["agent_settings"]["agy"]["model"], "")
        self.assertEqual(conf["providers"]["qa"]["model"], "")


class TestSoloConfigMigration(unittest.TestCase):
    """A config written when Solo was a toggle over one council stage."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-migrate-"))
        self.path = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self, raw):
        self.path.write_text(json.dumps(raw))
        return ConfigStore(self.path).all()

    def test_the_old_toggle_becomes_the_mode(self):
        self.assertEqual(self.load({"solo_mode": True})["mode"], "solo")
        self.assertEqual(self.load({"solo_mode": False})["mode"], "council")

    def test_a_config_that_never_saw_solo_defaults_to_council(self):
        self.assertEqual(self.load({"port": 9000})["mode"], "council")

    def test_the_chosen_stage_becomes_the_assistant_without_its_role(self):
        conf = self.load({
            "solo_mode": True,
            "solo_stage": "drafter",
            "providers": {"drafter": {
                "command": ["codex", "exec", "{prompt}"],
                "model": "chosen-model",
                "role": "Junior Draft",
                "role_template": "junior_draft",
            }},
        })
        solo = conf["providers"]["solo"]
        self.assertEqual(solo["model"], "chosen-model")
        self.assertEqual(solo["command"][0], "codex")
        self.assertEqual(solo["id"], "solo")
        # Blank means blank here, and the council role does not come with it.
        self.assertEqual(solo["behavior"], "")
        self.assertNotIn("role", solo)
        self.assertNotIn("role_template", solo)

    def test_the_dead_keys_do_not_survive(self):
        conf = self.load({"solo_mode": True, "solo_stage": "polisher"})
        self.assertNotIn("solo_mode", conf)
        self.assertNotIn("solo_stage", conf)

    def test_a_providers_council_role_is_swept_wherever_it_sits(self):
        # A config written before Council became a deliberating bench carries
        # a behaviour on the provider. Nothing reads one now - a seat's lens is
        # the persona the router assigns it - so leaving the key would show the
        # operator an instruction that has not been followed since the rewrite.
        conf = self.load({"providers": {
            "drafter": {"role": "Junior Draft",
                        "role_template": "junior_draft",
                        "role_system": "Be terse."},
            "council_codex": {"role_template": "security_review"},
        }})
        for pid in ("drafter", "council_codex", "polisher", "solo"):
            provider = conf["providers"][pid]
            self.assertFalse(
                {"role", "role_template", "role_system"} & set(provider),
                f"{pid} still carries a council role: {sorted(provider)}",
            )
        # Swept, not reset: the rest of the provider is the operator's.
        self.assertEqual(conf["providers"]["drafter"]["timeout_seconds"], 900)

    def test_a_pinned_persona_is_not_swept_with_the_dead_role_keys(self):
        # `council.personas` is where a behaviour lives now. The sweep above
        # must not take the live setting out with the dead ones.
        conf = self.load({"council": {"personas": {"seat1": "security_review"}}})
        self.assertEqual(conf["council"]["personas"]["seat1"], "security_review")

    def test_an_unknown_mode_falls_back_to_council(self):
        self.assertEqual(self.load({"mode": "committee"})["mode"], "council")

    def test_a_stored_antigravity_seat_gets_the_missing_read_grant(self):
        # `--mode plan` alone made every Antigravity read-only stage produce
        # nothing: headless `agy` auto-denies the `read_file` it cannot prompt
        # for. The merge cannot repair this on its own - a list is replaced
        # wholesale, so a config written before the fix keeps the broken pair
        # forever.
        conf = self.load({"providers": {
            "council_agy": {
                "command": ["agy", "--print-timeout", "60m", "--prompt={prompt}"],
                "read_only_args": ["--mode", "plan"],
            },
            "qa": {
                "command": ["/home/x/.local/bin/agy", "--prompt={prompt}"],
                "read_only_args": ["--mode", "plan"],
            },
        }})
        for pid in ("council_agy", "qa"):
            self.assertEqual(
                conf["providers"][pid]["read_only_args"],
                ["--mode", "plan", "--dangerously-skip-permissions"],
                f"{pid} still cannot read anything in a read-only stage",
            )

    def test_the_repair_leaves_hand_written_flags_alone(self):
        # Only the exact broken default is replaced. Anything else is a choice
        # somebody made, and this is not entitled to overrule it.
        conf = self.load({"providers": {"council_agy": {
            "command": ["agy", "--prompt={prompt}"],
            "read_only_args": ["--mode", "plan", "--sandbox"],
        }}})
        self.assertEqual(
            conf["providers"]["council_agy"]["read_only_args"],
            ["--mode", "plan", "--sandbox"],
        )

    def test_the_repair_does_not_reach_the_other_clis(self):
        conf = self.load({"providers": {"council_claude": {
            "command": ["claude", "-p", "{prompt}"],
            "read_only_args": ["--mode", "plan"],
        }}})
        self.assertEqual(
            conf["providers"]["council_claude"]["read_only_args"], ["--mode", "plan"]
        )


class TestApi(ServerTestBase):
    def test_the_page_explains_an_engine_too_old_to_offer_continue(self):
        # The browser reloads app.js by itself; the engine only reloads when
        # the app is restarted. A page newer than the server it is talking to
        # would otherwise draw neither button and give no reason.
        with urllib.request.urlopen(f"{self.base}/app.js", timeout=15) as res:
            script = res.read().decode()
        self.assertIn("run.can_resume === undefined", script)
        self.assertIn("started before Continue", script)

    def test_retry_needs_to_be_told_which_stage(self):
        status, data = self.request("/api/retry", method="POST", body={})
        self.assertEqual(status, 400)
        self.assertIn("which stage", data["error"].lower())

    def test_retry_says_why_when_there_is_no_run(self):
        status, data = self.request(
            "/api/retry", method="POST", body={"stage": "chair"}
        )
        self.assertEqual(status, 400)
        self.assertIn("no run", data["error"].lower())

    def test_resume_says_why_when_there_is_nothing_to_continue(self):
        # The button is only offered on a failed run, but the endpoint is
        # reachable regardless - and whoever reaches it should be told what is
        # wrong rather than handed a stack trace.
        status, data = self.request("/api/resume", method="POST")
        self.assertEqual(status, 400)
        self.assertIn("no run to continue", data["error"].lower())

    def test_config_round_trips(self):
        status, data = self.request(
            "/api/config", method="POST", body={"house_rules": "use tabs"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["config"]["house_rules"], "use tabs")

        status, data = self.request("/api/config")
        self.assertEqual(data["config"]["house_rules"], "use tabs")

    def test_caveman_modes_round_trip_independently(self):
        modes = {"council": True, "chat": False, "project": True}
        status, data = self.request(
            "/api/config", method="POST", body={"caveman": modes}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["config"]["caveman"], modes)

        _, data = self.request("/api/config")
        self.assertEqual(data["config"]["caveman"], modes)
        self.request(
            "/api/config",
            method="POST",
            body={"caveman": {mode: False for mode in modes}},
        )

    def test_efficiency_modes_round_trip_independently(self):
        modes = {"council": False, "chat": True, "project": True}
        status, data = self.request(
            "/api/config",
            method="POST",
            body={"efficiency": modes},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["config"]["efficiency"], modes)

        _, data = self.request("/api/config")
        self.assertEqual(data["config"]["efficiency"], modes)
        self.request(
            "/api/config",
            method="POST",
            body={"efficiency": {mode: False for mode in modes}},
        )

    def test_zero_touch_toggle_persists(self):
        self.request("/api/config", method="POST", body={"zero_touch": True})
        _, data = self.request("/api/state")
        self.assertTrue(data["config"]["zero_touch"])
        self.request("/api/config", method="POST", body={"zero_touch": False})

    def test_mode_round_trips(self):
        _, data = self.request("/api/config", method="POST", body={"mode": "solo"})
        self.assertEqual(data["config"]["mode"], "solo")
        self.request("/api/config", method="POST", body={"mode": "council"})

    def test_an_unknown_mode_is_refused(self):
        status, data = self.request(
            "/api/config", method="POST", body={"mode": "committee"}
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_state_serves_the_agent_catalog(self):
        # The browser renders the Agent dropdown from this; it never carries
        # its own copy of a command or a permission flag.
        _, data = self.request("/api/state")
        ids = {a["id"] for a in data["agents"]}
        # Against the catalogue itself, not a list spelled out here: the point
        # is that the endpoint serves all of it, and a literal set turns every
        # new agent into a test failure that says nothing about the change.
        self.assertEqual(ids, {a["id"] for a in agent_catalog()})
        # The ones that must not quietly disappear. Removing an agent should
        # take a deliberate edit here, unlike adding one.
        self.assertLessEqual({"codex", "claude", "agy", "custom"}, ids)

    def test_doctor_reports_provider_availability(self):
        status, data = self.request("/api/doctor")
        self.assertEqual(status, 200)
        ids = {p["id"] for p in data["providers"]}
        # Against the configured chairs rather than a literal, for the same
        # reason as the agent catalogue above. What must not silently vanish is
        # named separately: every council seat - Settings draws one card per
        # seat provider and reads its availability from here - plus chat and
        # the three Projects roles.
        self.assertEqual(ids, set(cfg.DEFAULTS["providers"]))
        self.assertLessEqual(set(cfg.COUNCIL_PROVIDERS), ids)
        self.assertLessEqual({"solo"}, ids)
        self.assertLessEqual({"architect", "coder", "qa"}, ids)
        for p in data["providers"]:
            self.assertIn("available", p)

    def test_filesystem_listing_returns_directories(self):
        status, data = self.request(f"/api/fs?path={Path.home()}")
        self.assertEqual(status, 200)
        self.assertIn("entries", data)
        for entry in data["entries"]:
            self.assertIn("is_repo", entry)

    def test_repo_endpoint_flags_a_non_repository(self):
        status, data = self.request(f"/api/fs?path=/definitely/not/here")
        self.assertEqual(status, 200)
        self.assertTrue(data["error"])

    def test_unknown_endpoint_is_404(self):
        status, _ = self.request("/api/nope")
        self.assertEqual(status, 404)

    def test_start_without_a_task_is_a_400(self):
        self.request("/api/config", method="POST",
                     body={"workspace": str(Path(__file__).parent.parent)})
        status, data = self.request("/api/start", method="POST", body={"task": ""})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_state_names_the_folder_a_run_lands_in_with_none_chosen(self):
        # The browser has no way to work out an XDG path, and "no folder"
        # must not read as "nowhere".
        self.request("/api/config", method="POST", body={"workspace": ""})
        _, data = self.request("/api/state")
        self.assertTrue(data["scratch_workspace"])
        self.assertIsNone(data["workspace_status"])

    def test_start_needs_no_working_folder(self):
        # The whole point: a fresh install can answer a question before it has
        # been pointed at anything. Only the task is required.
        self.request("/api/config", method="POST", body={"workspace": ""})
        status, data = self.request(
            "/api/start", method="POST",
            body={"task": "hello", "continue_from": "1700000000-nope.json"},
        )
        # Refused for the transcript, not for the missing folder - which is
        # what proves the folder check is gone rather than merely reordered.
        self.assertEqual(status, 400)
        self.assertIn("no longer exists", data["error"])

    def test_approve_without_a_gate_is_a_400(self):
        status, _ = self.request("/api/approve", method="POST", body={})
        self.assertEqual(status, 400)

    def test_start_rejects_a_continue_from_that_is_not_a_filename(self):
        status, data = self.request(
            "/api/start", method="POST",
            body={"task": "follow up", "workspace": str(Path(__file__).parent.parent),
                  "continue_from": ["not", "a", "filename"]},
        )
        self.assertEqual(status, 400)
        self.assertIn("continue_from", data["error"])

    def test_start_rejects_an_unknown_transcript(self):
        status, data = self.request(
            "/api/start", method="POST",
            body={"task": "follow up", "workspace": str(Path(__file__).parent.parent),
                  "continue_from": "1700000000-nosuchrun.json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("no longer exists", data["error"])

    def test_start_rejects_a_compaction_flag_that_is_not_a_boolean(self):
        status, data = self.request(
            "/api/start", method="POST",
            body={"task": "follow up", "workspace": str(Path(__file__).parent.parent),
                  "compact_context": "yes"},
        )
        self.assertEqual(status, 400)
        self.assertIn("compact_context", data["error"])

    def test_context_needs_a_file_to_measure(self):
        status, data = self.request("/api/context")
        self.assertEqual(status, 400)
        self.assertIn("file", data["error"])

    def test_context_rejects_an_unknown_transcript(self):
        status, data = self.request("/api/context?file=1700000000-nosuchrun.json")
        self.assertEqual(status, 400)
        self.assertIn("No such run transcript", data["error"])


class TestPickerEndpoints(ServerTestBase):
    """What the model and effort menus are served, and how often it costs a
    process launch. Opening a picker used to run `agy models` every time."""

    STUB = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "open(sys.argv[0] + '.calls', 'a').write('x')\n"
        "print('gemini-3.6-flash-high')\n"
    )

    def setUp(self):
        self.stub = self.tmp / "agy"
        self.stub.write_text(self.STUB)
        self.stub.chmod(0o755)
        # Rewriting it dates it, so each test starts with the catalogue empty
        # for this binary; the launch count has to start from zero with it.
        Path(str(self.stub) + ".calls").unlink(missing_ok=True)
        self.store.update({
            "providers": {
                "council_agy": {"command": [str(self.stub), "--prompt={prompt}"]}
            }
        })

    def _calls(self):
        marker = Path(str(self.stub) + ".calls")
        return len(marker.read_text()) if marker.exists() else 0

    def test_reopening_the_menu_does_not_relaunch_the_cli(self):
        _, first = self.request("/api/models?provider=council_agy")
        _, second = self.request("/api/models?provider=council_agy")
        self.assertEqual(first["models"], ["gemini-3.6-flash-high"])
        self.assertEqual(second["models"], ["gemini-3.6-flash-high"])
        self.assertTrue(second["cached"])
        self.assertEqual(self._calls(), 1)

    def test_refresh_asks_the_cli_again(self):
        self.request("/api/models?provider=council_agy")
        _, fresh = self.request("/api/models?provider=council_agy&refresh=1")
        self.assertFalse(fresh["cached"])
        self.assertEqual(self._calls(), 2)


class TestProjectRoutes(ServerTestBase):
    """The Projects tab's endpoints, over the same real socket."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="aicouncil-proj-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def test_an_empty_folder_reports_no_project(self):
        status, data = self.request(
            f"/api/project?workspace={urllib.parse.quote(str(self.folder))}"
        )
        self.assertEqual(status, 200)
        self.assertIsNone(data["project"])
        self.assertFalse(data["running"])
        # The matrix draws its availability dots from this.
        self.assertEqual({r["id"] for r in data["roles"]}, {"architect", "coder", "qa"})
        self.assertIn("max_steps", data["settings"])

    def test_a_project_on_disk_is_reported(self):
        theseus = self.folder / ".theseus"
        theseus.mkdir()
        (theseus / "BOARD.json").write_text(
            json.dumps({
                "project_id": "abc123", "status": "IMPLEMENTING",
                "goal": "half a thing",
                "columns": {"backlog": [{"id": "t1", "title": "finish it"}]},
            }),
            encoding="utf-8",
        )
        status, data = self.request(
            f"/api/project?workspace={urllib.parse.quote(str(self.folder))}"
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["resumable"])
        self.assertEqual(data["project"]["goal"], "half a thing")
        self.assertEqual(data["project"]["counts"]["backlog"], 1)

    def test_starting_without_a_goal_is_refused(self):
        status, data = self.request(
            "/api/project/start", method="POST",
            body={"goal": "", "workspace": str(self.folder)},
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_an_innovation_of_zero_is_not_read_as_unset(self):
        # The falsy-zero trap, pinned. `0 or ""` is `""`, so the obvious
        # one-liner turns "build what I asked for and stop" into "use the saved
        # default" — which on a stock config means three rounds of work nobody
        # asked for, in somebody's repository.
        from aicouncil.server import _opt_int

        self.assertEqual(_opt_int(0), 0)
        self.assertEqual(_opt_int("0"), 0)
        self.assertIsNone(_opt_int(None))
        self.assertIsNone(_opt_int(""))
        self.assertIsNone(_opt_int("three"))
        # A JSON `true` is not a count, and int(True) == 1 would be a silent
        # round of invented work.
        self.assertIsNone(_opt_int(True))

    def test_starting_with_innovation_off_reaches_the_engine(self):
        seen = {}

        def fake_start(goal, workspace="", resume=False, innovation=None):
            seen["innovation"] = innovation
            raise ValueError("stop here — the argument is what is under test")

        self.state.projects.start = fake_start
        self.request(
            "/api/project/start", method="POST",
            body={"goal": "build it", "workspace": str(self.folder), "innovation": 0},
        )
        self.assertEqual(seen["innovation"], 0)

    def test_controls_refuse_when_nothing_is_running(self):
        for path in ("/api/project/pause", "/api/project/resume"):
            status, data = self.request(path, method="POST")
            self.assertEqual(status, 400, path)
            self.assertFalse(data["ok"])

    def test_dismissing_closes_the_board_so_the_tab_offers_a_new_one(self):
        # Clearing it in the browser alone lasts until the next reload: the
        # engine finds the same board again on disk.
        theseus = self.folder / ".theseus"
        theseus.mkdir()
        (theseus / "BOARD.json").write_text(
            json.dumps({"project_id": "abc123", "status": "COMPLETED",
                        "goal": "a thing"}),
            encoding="utf-8",
        )
        status, data = self.request(
            "/api/project/dismiss", method="POST",
            body={"workspace": str(self.folder)},
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        _, after = self.request(
            f"/api/project?workspace={urllib.parse.quote(str(self.folder))}"
        )
        self.assertIsNone(after["project"])
        # Closing the report is not deleting the build.
        self.assertTrue((theseus / "BOARD.json").exists())

    def test_an_unknown_handoff_role_is_refused(self):
        status, data = self.request(
            "/api/project/handoff", method="POST", body={"role": "janitor"}
        )
        self.assertEqual(status, 400)
        self.assertIn("role", data["error"])

    def test_the_file_reader_takes_a_name_not_a_path(self):
        # This endpoint reads a folder the operator chose, over a port any
        # local process can reach. Taking a filename would turn "show me the
        # board" into an arbitrary-file read.
        for name in ("../../etc/passwd", "/etc/passwd", "id_rsa"):
            status, data = self.request(
                f"/api/project/file?name={urllib.parse.quote(name)}"
                f"&workspace={urllib.parse.quote(str(self.folder))}"
            )
            self.assertEqual(status, 400, name)
            self.assertIn("Not a project file", data["error"])

    def test_the_file_reader_serves_the_three_it_knows(self):
        theseus = self.folder / ".theseus"
        theseus.mkdir()
        (theseus / "CRITIQUE.log").write_text("# Critique log\n\nfound one\n",
                                              encoding="utf-8")
        status, data = self.request(
            f"/api/project/file?name=critique"
            f"&workspace={urllib.parse.quote(str(self.folder))}"
        )
        self.assertEqual(status, 200)
        self.assertIn("found one", data["text"])

    def test_a_project_and_a_run_refuse_each_other(self):
        # Both drive coding agents against the same folder. Two of them editing
        # one tree with no idea the other exists is how a build ends up with
        # half of each.
        engine = self.state.projects

        class Pretend:
            def is_running(self_inner):
                return True

        real = self.state.projects
        try:
            self.state.projects = Pretend()
            status, data = self.request(
                "/api/start", method="POST", body={"task": "do something"}
            )
            self.assertEqual(status, 409)
            self.assertIn("project is running", data["error"])
        finally:
            self.state.projects = real
        self.assertIs(self.state.projects, engine)


if __name__ == "__main__":
    unittest.main()
