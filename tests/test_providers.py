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
    ClaudeStreamReader,
    discover_models,
    ProviderRunner,
    ProviderUnavailable,
    build_argv,
    redact_argv,
    resolve_binary,
    stream_reader_for,
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


class TestStreamingArgs(unittest.TestCase):
    """The flags that make a CLI narrate its work, rather than only its result.

    Unlike the auto-approve flags these are unconditional: they change how the
    CLI reports, never what it may do.
    """

    STREAMING = dict(CLAUDE, stream_args=["--output-format", "stream-json", "--verbose"])

    def test_streaming_flags_are_passed_without_any_grant(self):
        argv, _ = build_argv(self.STREAMING, "hi", auto_approve=False)
        self.assertEqual(argv, [
            "claude", "-p",
            "--output-format", "stream-json", "--verbose",
            "hi",
        ])

    def test_streaming_flags_precede_the_model_and_the_grant(self):
        provider = dict(self.STREAMING, model="opus")
        argv, _ = build_argv(provider, "hi", auto_approve=True)
        self.assertEqual(argv, [
            "claude", "-p",
            "--output-format", "stream-json", "--verbose",
            "--model", "opus",
            "--dangerously-skip-permissions",
            "hi",
        ])

    def test_no_streaming_flags_configured_changes_nothing(self):
        argv, _ = build_argv(CODEX, "task", auto_approve=False)
        self.assertEqual(argv, ["codex", "exec", "task"])

    def test_blank_streaming_entries_are_dropped(self):
        provider = dict(CLAUDE, stream_args=["", "--verbose"])
        argv, _ = build_argv(provider, "hi", auto_approve=False)
        self.assertEqual(argv, ["claude", "-p", "--verbose", "hi"])

    def test_streaming_flags_go_last_when_the_prompt_is_on_stdin(self):
        provider = dict(self.STREAMING, prompt_on_stdin=True)
        argv, stdin = build_argv(provider, "hi", auto_approve=False)
        self.assertEqual(argv, [
            "claude", "-p", "--output-format", "stream-json", "--verbose",
        ])
        self.assertEqual(stdin, "hi")


class TestStreamReaderSelection(unittest.TestCase):
    """Which output format to expect is decided by the argv actually run.

    Reading it from the configured template instead would misjudge a
    hand-edited command, and parsing plain prose as JSON mangles it.
    """

    def test_claude_asked_for_events_gets_a_reader(self):
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "hi"]
        self.assertIsInstance(stream_reader_for(argv), ClaudeStreamReader)

    def test_claude_in_plain_text_mode_gets_none(self):
        self.assertIsNone(stream_reader_for(["claude", "-p", "hi"]))

    def test_an_absolute_path_is_still_recognised(self):
        argv = ["/home/someone/.local/bin/claude", "-p", "--output-format=stream-json", "hi"]
        self.assertIsInstance(stream_reader_for(argv), ClaudeStreamReader)

    def test_another_cli_is_left_alone(self):
        argv = ["codex", "exec", "--output-format", "stream-json", "task"]
        self.assertIsNone(stream_reader_for(argv))

    def test_empty_argv_gets_none(self):
        self.assertIsNone(stream_reader_for([]))

    def test_a_prompt_mentioning_the_format_does_not_enable_a_reader(self):
        # The prompt is data. Only a flag turns the format on, and the flag is
        # never the last positional argument.
        self.assertIsNone(
            stream_reader_for(["claude", "-p", "explain stream-json to me"])
        )


class TestClaudeStreamReader(unittest.TestCase):
    """Events in, readable lines out - and the final answer kept separately."""

    def setUp(self):
        self.reader = ClaudeStreamReader()

    def feed(self, event):
        return self.reader.feed(json.dumps(event))

    def test_thinking_is_shown_as_it_arrives(self):
        # The whole point of the change: the reasoning must reach the stream
        # during the run, not after it.
        lines = self.feed({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "first thought\nsecond thought"},
        ]}})
        self.assertEqual(lines, ["· first thought", "· second thought"])

    def test_assistant_text_passes_through_unmarked(self):
        lines = self.feed({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I will edit two files."},
        ]}})
        self.assertEqual(lines, ["I will edit two files."])

    def test_tool_calls_name_their_main_argument(self):
        lines = self.feed({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "a/b.py"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
        ]}})
        self.assertEqual(lines, ["→ Read a/b.py", "→ Bash pytest -q"])

    def test_a_tool_with_no_recognised_field_still_says_something(self):
        lines = self.feed({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Odd", "input": {"unheard_of": "value"}},
        ]}})
        self.assertEqual(len(lines), 1)
        self.assertIn("unheard_of", lines[0])

    def test_tool_results_are_summarised_not_dumped(self):
        # A file read would otherwise bury the agent's own words.
        body = "\n".join(f"line {i}" for i in range(400))
        lines = self.feed({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": body},
        ]}})
        self.assertEqual(lines, ["← line 0 (+399 lines)"])

    def test_a_failed_tool_result_is_marked(self):
        lines = self.feed({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "No such file", "is_error": True},
        ]}})
        self.assertEqual(lines, ["✗ No such file"])

    def test_block_shaped_tool_result_content_is_handled(self):
        lines = self.feed({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "ok"}]},
        ]}})
        self.assertEqual(lines, ["← ok"])

    def test_long_lines_are_bounded(self):
        lines = self.feed({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "x" * 5000}},
        ]}})
        self.assertLess(len(lines[0]), 300)

    def test_the_session_model_is_reported_once(self):
        # The only place it appears when the stage runs the CLI's own default.
        lines = self.feed({"type": "system", "subtype": "init", "model": "opus-x"})
        self.assertEqual(lines, ["· model opus-x"])

    def test_the_final_result_is_collected_not_streamed(self):
        self.assertEqual(self.feed({"type": "result", "result": "all done"}), [])
        self.assertEqual(self.reader.result, "all done")
        self.assertEqual(self.reader.error, "")

    def test_a_failed_result_becomes_an_error_and_a_line(self):
        lines = self.feed(
            {"type": "result", "is_error": True, "result": "usage limit reached"}
        )
        self.assertEqual(lines, ["✗ usage limit reached"])
        self.assertEqual(self.reader.error, "usage limit reached")

    def test_noise_events_produce_nothing(self):
        self.assertEqual(self.feed({"type": "rate_limit_event"}), [])
        self.assertEqual(self.feed({"type": "some_future_event", "x": 1}), [])

    def test_non_json_output_passes_through(self):
        # A banner or a deprecation warning is still worth seeing.
        self.assertEqual(self.reader.feed("npm notice: update available"),
                         ["npm notice: update available"])
        self.assertEqual(self.reader.feed("{not json after all"),
                         ["{not json after all"])

    def test_malformed_events_do_not_raise(self):
        # An exception here would kill the pump thread and silently truncate
        # the rest of the run's output.
        for event in (
            {"type": "assistant", "message": "not a dict"},
            {"type": "assistant", "message": {"content": "plain string"}},
            {"type": "assistant", "message": {"content": [None, 7]}},
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            {"type": "result"},
            [1, 2, 3],
        ):
            self.reader.feed(json.dumps(event))


class TestStreamingRunnerIntegration(unittest.TestCase):
    """The pump translates as it goes, and the result replaces the transcript.

    Driven through a real subprocess named so the reader recognises it, because
    the wiring between pump, translator and ProviderResult is the part a unit
    test on the translator alone would miss.
    """

    SHIM = """#!/usr/bin/env python3
import json, sys
def emit(obj):
    print(json.dumps(obj), flush=True)
emit({"type": "system", "subtype": "init", "model": "test-model"})
emit({"type": "assistant", "message": {"content": [
    {"type": "thinking", "thinking": "considering"},
    {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}}]}})
emit({"type": "user", "message": {"content": [
    {"type": "tool_result", "content": "one\\ntwo"}]}})
emit({"type": "result", "is_error": False, "result": "THE ANSWER"})
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-stream-"))
        # The basename decides whether events are expected, so it has to read
        # as a claude binary.
        self.exe = self.tmp / "claude-shim"
        self.exe.write_text(self.SHIM)
        self.exe.chmod(0o755)
        self.lines = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_shim(self, stream_args):
        provider = {
            "id": "polisher",
            "command": [str(self.exe), "{prompt}"],
            "stream_args": stream_args,
            "timeout_seconds": 60,
        }
        runner = ProviderRunner(provider, lambda s, ln: self.lines.append(ln))
        return runner.run("hi", cwd=str(self.tmp), auto_approve=False)

    def test_events_reach_the_callback_as_readable_lines(self):
        result = self.run_shim(["--output-format", "stream-json", "--verbose"])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.lines, [
            "· model test-model",
            "· considering",
            "→ Read x.py",
            "← one (+1 lines)",
        ])

    def test_the_final_answer_becomes_the_stage_output(self):
        # Not the transcript: this text is what the next stage's prompt and the
        # Draft/Final panes are built from.
        result = self.run_shim(["--output-format", "stream-json"])
        self.assertEqual(result.stdout, "THE ANSWER")

    def test_without_the_flags_the_raw_output_is_untouched(self):
        result = self.run_shim([])
        self.assertIn('"type": "result"', result.stdout)
        self.assertEqual(len(self.lines), 4)


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
