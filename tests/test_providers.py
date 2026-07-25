"""Tests for CLI argv construction - the security-critical part of the app.

The invariant under test: auto-approve flags reach the child process if and
only if the pipeline explicitly granted permission.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil.providers import (  # noqa: E402
    ARGV_PROMPT_LIMIT,
    discover_models,
    ProviderRunner,
    ProviderUnavailable,
    build_argv,
    redact_argv,
    resolve_binary,
)

CLAUDE = {
    "id": "polisher",
    "command": ["claude", "-p", "{prompt}"],
    "auto_approve_args": ["--dangerously-skip-permissions"],
}
CODEX = {
    "id": "drafter",
    "command": ["codex", "exec", "{prompt}"],
    "auto_approve_args": ["--dangerously-bypass-approvals-and-sandbox"],
}


class TestBuildArgv(unittest.TestCase):
    def test_prompt_is_substituted(self):
        argv, stdin = build_argv(CLAUDE, "fix the bug", auto_approve=False)
        self.assertEqual(argv, ["claude", "-p", "fix the bug"])
        self.assertIsNone(stdin)

    def test_auto_approve_flags_absent_by_default(self):
        argv, _ = build_argv(CLAUDE, "hi", auto_approve=False)
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_auto_approve_flags_present_when_granted(self):
        argv, _ = build_argv(CLAUDE, "hi", auto_approve=True)
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_flags_land_before_the_prompt_not_after(self):
        # A trailing positional prompt must stay last, or the CLI would read
        # the flag as the prompt and the prompt as an unknown argument.
        argv, _ = build_argv(CODEX, "do a thing", auto_approve=True)
        self.assertEqual(argv[0], "codex")
        self.assertEqual(argv[1], "exec")
        self.assertEqual(argv[2], "--dangerously-bypass-approvals-and-sandbox")
        self.assertEqual(argv[-1], "do a thing")

    def test_flags_go_immediately_before_the_prompt(self):
        argv, _ = build_argv(CLAUDE, "hi", auto_approve=True)
        self.assertEqual(argv, ["claude", "-p", "--dangerously-skip-permissions", "hi"])

    def test_flags_survive_an_interpreter_style_template(self):
        # `python3 script.py --role drafter {prompt}` - inserting after the
        # "subcommand" would hand the flag to python itself, which rejects it.
        provider = {
            "id": "x",
            "command": ["python3", "/long/path/to/some/agent-script.py",
                        "--role", "drafter", "{prompt}"],
            "auto_approve_args": ["--yes"],
        }
        argv, _ = build_argv(provider, "task text", auto_approve=True)
        self.assertEqual(argv, [
            "python3", "/long/path/to/some/agent-script.py",
            "--role", "drafter", "--yes", "task text",
        ])

    def test_flags_go_last_when_the_prompt_is_on_stdin(self):
        provider = dict(CLAUDE, prompt_on_stdin=True)
        argv, stdin = build_argv(provider, "hi", auto_approve=True)
        self.assertEqual(argv, ["claude", "-p", "--dangerously-skip-permissions"])
        self.assertEqual(stdin, "hi")

    def test_prompt_on_stdin_drops_the_placeholder(self):
        provider = dict(CLAUDE, prompt_on_stdin=True)
        argv, stdin = build_argv(provider, "long prompt", auto_approve=False)
        self.assertEqual(argv, ["claude", "-p"])
        self.assertEqual(stdin, "long prompt")

    def test_oversized_prompt_switches_to_stdin_automatically(self):
        big = "x" * (ARGV_PROMPT_LIMIT + 1)
        argv, stdin = build_argv(CLAUDE, big, auto_approve=False)
        self.assertEqual(stdin, big)
        self.assertNotIn(big, argv)

    def test_oversized_prompt_is_piped_when_stdin_was_configured(self):
        # An explicit choice is honoured even for a decorated placeholder.
        provider = {"id": "x", "command": ["tool", "--message={prompt}"],
                    "prompt_on_stdin": True}
        big = "x" * (ARGV_PROMPT_LIMIT + 1)
        argv, stdin = build_argv(provider, big, auto_approve=False)
        self.assertEqual(argv, ["tool", "--message="])
        self.assertEqual(stdin, big)

    def test_oversized_prompt_in_a_decorated_placeholder_is_refused(self):
        # Emptying `--message=` and piping the prompt instead would hand the
        # CLI an empty task and let it report success.
        provider = {"id": "x", "command": ["tool", "--message={prompt}"]}
        with self.assertRaises(ProviderUnavailable) as ctx:
            build_argv(provider, "x" * (ARGV_PROMPT_LIMIT + 1), auto_approve=False)
        self.assertIn("stdin", str(ctx.exception))

    def test_flags_precede_the_first_prompt_placeholder(self):
        # Anchoring on a later placeholder would put the flags behind a
        # positional prompt, where most CLIs have stopped reading options.
        provider = {
            "id": "x",
            "command": ["tool", "{prompt}", "--also={prompt}"],
            "auto_approve_args": ["--yes"],
        }
        argv, _ = build_argv(provider, "task", auto_approve=True)
        self.assertEqual(argv, ["tool", "--yes", "task", "--also=task"])

    def test_embedded_placeholder_is_substituted_in_place(self):
        provider = {"id": "x", "command": ["tool", "--message={prompt}"]}
        argv, _ = build_argv(provider, "hello", auto_approve=False)
        self.assertEqual(argv, ["tool", "--message=hello"])

    def test_template_without_placeholder_appends_the_prompt(self):
        provider = {"id": "x", "command": ["tool", "run"]}
        argv, _ = build_argv(provider, "hello", auto_approve=False)
        self.assertEqual(argv, ["tool", "run", "hello"])

    def test_empty_command_is_rejected(self):
        with self.assertRaises(ProviderUnavailable):
            build_argv({"id": "x", "command": []}, "hi", auto_approve=False)

    def test_shell_metacharacters_stay_inert(self):
        # argv is passed with shell=False, so these are a single literal
        # argument rather than anything the shell would expand.
        nasty = "hi; rm -rf / $(whoami) `id` && echo pwned"
        argv, _ = build_argv(CLAUDE, nasty, auto_approve=False)
        self.assertEqual(argv[-1], nasty)
        self.assertEqual(len(argv), 3)

    def test_blank_auto_approve_entries_are_dropped(self):
        provider = dict(CLAUDE, auto_approve_args=["", "  ", "--yes"])
        argv, _ = build_argv(provider, "hi", auto_approve=True)
        self.assertIn("--yes", argv)
        self.assertNotIn("", argv)


class TestModelSelection(unittest.TestCase):
    def test_no_model_means_no_model_flag(self):
        # Blank must mean "let the CLI decide", not "--model ''".
        argv, _ = build_argv(CLAUDE, "hi", auto_approve=False)
        self.assertNotIn("--model", argv)

    def test_model_is_passed_before_the_prompt(self):
        provider = dict(CLAUDE, model="opus")
        argv, _ = build_argv(provider, "hi", auto_approve=False)
        self.assertEqual(argv, ["claude", "-p", "--model", "opus", "hi"])

    def test_model_and_auto_approve_both_precede_the_prompt(self):
        provider = dict(CLAUDE, model="claude-opus-4-8")
        argv, _ = build_argv(provider, "hi", auto_approve=True)
        self.assertEqual(argv, [
            "claude", "-p",
            "--model", "claude-opus-4-8",
            "--dangerously-skip-permissions",
            "hi",
        ])

    def test_whitespace_only_model_is_ignored(self):
        provider = dict(CLAUDE, model="   ")
        argv, _ = build_argv(provider, "hi", auto_approve=False)
        self.assertNotIn("--model", argv)

    def test_custom_model_flag_shape_is_honoured(self):
        # Some CLIs want `-m X`, others `--model=X`; both must work.
        short = dict(CODEX, model="o3", model_args=["-m", "{model}"])
        argv, _ = build_argv(short, "task", auto_approve=False)
        self.assertEqual(argv, ["codex", "exec", "-m", "o3", "task"])

        joined = dict(CODEX, model="o3", model_args=["--model={model}"])
        argv, _ = build_argv(joined, "task", auto_approve=False)
        self.assertEqual(argv, ["codex", "exec", "--model=o3", "task"])

    def test_model_goes_last_when_prompt_is_on_stdin(self):
        provider = dict(CLAUDE, model="sonnet", prompt_on_stdin=True)
        argv, stdin = build_argv(provider, "hi", auto_approve=False)
        self.assertEqual(argv, ["claude", "-p", "--model", "sonnet"])
        self.assertEqual(stdin, "hi")

    def test_model_value_is_never_shell_interpreted(self):
        provider = dict(CLAUDE, model="a; rm -rf /")
        argv, _ = build_argv(provider, "hi", auto_approve=False)
        self.assertIn("a; rm -rf /", argv)  # one literal argv entry


class TestRedaction(unittest.TestCase):
    def test_prompt_is_replaced_with_a_placeholder(self):
        argv = ["claude", "-p", "a very long prompt"]
        out = redact_argv(argv, "a very long prompt")
        self.assertEqual(out[:2], ["claude", "-p"])
        self.assertIn("chars", out[2])
        self.assertNotIn("a very long prompt", out[2])

    def test_empty_prompt_is_a_no_op(self):
        self.assertEqual(redact_argv(["a", "b"], ""), ["a", "b"])


class TestRunnerConfiguration(unittest.TestCase):
    """A provider that cannot even be assembled must come back as a result.

    The caller has already marked the stage running by this point, and only a
    ProviderResult carries the reason back to the UI - an exception here left
    the stage running forever with no error and no end time on it.
    """

    def run_with(self, provider):
        return ProviderRunner(provider, lambda *_: None).run(
            "hi", cwd=str(Path(__file__).parent), auto_approve=False
        )

    def test_empty_command_returns_a_failed_result(self):
        result = self.run_with({"id": "polisher", "command": []})
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 126)
        self.assertIn("misconfigured", result.error)

    def test_unparseable_timeout_returns_a_failed_result(self):
        result = self.run_with(
            {"id": "polisher", "command": ["sh", "-c", "true"],
             "timeout_seconds": "half an hour"}
        )
        self.assertFalse(result.ok)
        self.assertIn("misconfigured", result.error)

    def test_negative_timeout_returns_a_failed_result(self):
        result = self.run_with(
            {"id": "polisher", "command": ["sh", "-c", "true"], "timeout_seconds": -5}
        )
        self.assertFalse(result.ok)
        self.assertIn("positive", result.error)


class TestResolveBinary(unittest.TestCase):
    def test_finds_an_executable_on_path(self):
        self.assertIsNotNone(resolve_binary(["sh"]))

    def test_missing_executable_returns_none(self):
        self.assertIsNone(resolve_binary(["definitely-not-a-real-binary-xyz"]))

    def test_empty_command_returns_none(self):
        self.assertIsNone(resolve_binary([]))


if __name__ == "__main__":
    unittest.main()


class TestModelDiscovery(unittest.TestCase):
    """Models come from the CLI, never from a list shipped in this app.

    A seeded list was wrong in practice: `gpt-5.1-codex` was rejected at run
    time with "not supported when using Codex with a ChatGPT account" — the
    entitlements differ per account, so only the CLI knows.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-models-"))
        self._old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.tmp)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_cache(self, payload):
        (self.tmp / "models_cache.json").write_text(json.dumps(payload))

    def test_reads_slugs_from_the_codex_cache(self):
        self._write_cache({"fetched_at": "2026-07-25T01:03:30Z", "models": [
            {"slug": "gpt-5.6-sol", "visibility": "list"},
            {"slug": "gpt-5.5", "visibility": "list"},
        ]})
        r = discover_models({"command": ["codex", "exec", "{prompt}"]})
        self.assertEqual(r["models"], ["gpt-5.6-sol", "gpt-5.5"])
        self.assertFalse(r["error"])

    def test_hidden_models_are_excluded(self):
        # codex-auto-review is marked hide; it is not a selectable session model.
        self._write_cache({"models": [
            {"slug": "gpt-5.6-sol", "visibility": "list"},
            {"slug": "codex-auto-review", "visibility": "hide"},
        ]})
        r = discover_models({"command": ["codex", "exec", "{prompt}"]})
        self.assertEqual(r["models"], ["gpt-5.6-sol"])

    def test_missing_cache_explains_itself(self):
        r = discover_models({"command": ["codex", "exec", "{prompt}"]})
        self.assertEqual(r["models"], [])
        self.assertIn("models_cache.json", r["error"])

    def test_corrupt_cache_does_not_raise(self):
        (self.tmp / "models_cache.json").write_text("{not json")
        r = discover_models({"command": ["codex", "exec", "{prompt}"]})
        self.assertEqual(r["models"], [])
        self.assertTrue(r["error"])

    def test_claude_offers_aliases_not_pinned_ids(self):
        r = discover_models({"command": ["claude", "-p", "{prompt}"]})
        self.assertIn("opus", r["models"])
        # A pinned ID would go stale and silently keep running an old model.
        self.assertFalse([m for m in r["models"] if m.startswith("claude-")])

    def test_unknown_cli_reports_no_discovery(self):
        r = discover_models({"command": ["some-other-agent", "{prompt}"]})
        self.assertEqual(r["models"], [])
        self.assertTrue(r["error"])
