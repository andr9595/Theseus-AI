"""HTTP layer tests, exercised against a real socket.

The auth and Origin checks here are the boundary between "a local dev tool"
and "any web page you visit can run a coding agent on your source tree", so
they get direct coverage.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil.config import ConfigStore, agent_for  # noqa: E402
from aicouncil.server import make_server, serve_forever_in_thread  # noqa: E402


class ServerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-http-"))
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


class TestStaticFiles(ServerTestBase):
    def test_index_is_served_without_a_token(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=15) as res:
            self.assertEqual(res.status, 200)
            body = res.read().decode()
        self.assertIn("AI Council", body)

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
        # The job itself is untouched: only the agent doing it changed.
        self.assertEqual(polisher["role"], "Senior Polish")
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


class TestApi(ServerTestBase):
    def test_config_round_trips(self):
        status, data = self.request(
            "/api/config", method="POST", body={"house_rules": "use tabs"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["config"]["house_rules"], "use tabs")

        status, data = self.request("/api/config")
        self.assertEqual(data["config"]["house_rules"], "use tabs")

    def test_zero_touch_toggle_persists(self):
        self.request("/api/config", method="POST", body={"zero_touch": True})
        _, data = self.request("/api/state")
        self.assertTrue(data["config"]["zero_touch"])
        self.request("/api/config", method="POST", body={"zero_touch": False})

    def test_state_serves_the_agent_catalog(self):
        # The browser renders the Agent dropdown from this; it never carries
        # its own copy of a command or a permission flag.
        _, data = self.request("/api/state")
        ids = {a["id"] for a in data["agents"]}
        self.assertEqual(ids, {"codex", "claude", "custom"})

    def test_doctor_reports_provider_availability(self):
        status, data = self.request("/api/doctor")
        self.assertEqual(status, 200)
        ids = {p["id"] for p in data["providers"]}
        self.assertEqual(ids, {"drafter", "polisher"})
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
                     body={"target_repo": str(Path(__file__).parent.parent)})
        status, data = self.request("/api/start", method="POST", body={"task": ""})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_approve_without_a_gate_is_a_400(self):
        status, _ = self.request("/api/approve", method="POST", body={})
        self.assertEqual(status, 400)

    def test_start_rejects_a_continue_from_that_is_not_a_filename(self):
        status, data = self.request(
            "/api/start", method="POST",
            body={"task": "follow up", "repo": str(Path(__file__).parent.parent),
                  "continue_from": ["not", "a", "filename"]},
        )
        self.assertEqual(status, 400)
        self.assertIn("continue_from", data["error"])

    def test_start_rejects_an_unknown_transcript(self):
        status, data = self.request(
            "/api/start", method="POST",
            body={"task": "follow up", "repo": str(Path(__file__).parent.parent),
                  "continue_from": "1700000000-nosuchrun.json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("no longer exists", data["error"])


if __name__ == "__main__":
    unittest.main()
