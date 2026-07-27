"""Adapters that shell out to locally installed, subscription-backed CLIs.

This module is the reason the app costs nothing per token. It never speaks
HTTP to a model endpoint and never reads an API key from the environment. It
launches the `codex` and `claude` binaries that are already authenticated
against the user's ChatGPT Plus/Pro and Claude Pro subscriptions, and streams
their stdout back to the UI line by line.

Three behaviours are worth calling out:

* **Auto-approve flags are injected, never hardcoded into the template.** The
  dangerous flags (``--dangerously-skip-permissions``,
  ``--dangerously-bypass-approvals-and-sandbox``) live in a separate config
  field and are appended only when ``auto_approve`` is set. The pipeline sets
  it in exactly two cases: Zero-Touch Mode is on, or a human clicked "Approve
  & execute" at the gate. Stage 1 never receives them - it is read-only - and
  neither does Chat until Zero-Touch is switched on, being invoked with
  ``read_only_args`` instead.

* **No shell.** Commands are executed as argv lists with ``shell=False``, so a
  prompt containing backticks, ``$(...)`` or a semicolon is inert data rather
  than something the shell will interpret.

* **"Line by line" needs the CLI's cooperation.** ``claude -p`` buffers its
  entire answer and prints it once, at the end. Its ``stream_args`` turn that
  into one JSON event per step, and ``ClaudeStreamReader`` turns those back
  into text so the stream stays readable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PROMPT_TOKEN = "{prompt}"
MODEL_TOKEN = "{model}"
EFFORT_TOKEN = "{effort}"

# A prompt longer than this is piped on stdin regardless of the configured
# mode. Linux caps a single argv entry at MAX_ARG_STRLEN (128 KB), and the CLI
# would fail with a confusing E2BIG rather than a useful message.
ARGV_PROMPT_LIMIT = 96_000


@dataclass
class ProviderResult:
    """Outcome of one CLI invocation."""

    provider_id: str
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    command: List[str]
    error: str = ""
    timed_out: bool = False
    cancelled: bool = False

    def to_dict(self) -> Dict:
        return {
            "provider_id": self.provider_id,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": round(self.duration, 2),
            # Redact nothing, but show the prompt as a placeholder so the UI's
            # command echo stays readable.
            "command": self.command,
            "error": self.error,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
        }


class ProviderUnavailable(RuntimeError):
    """The configured executable is not installed or not on PATH."""


def resolve_binary(command: List[str]) -> Optional[str]:
    """Return the absolute path of a command's executable, or None."""
    if not command:
        return None
    exe = command[0]
    if os.path.sep in exe:
        p = Path(exe).expanduser()
        return str(p) if p.exists() and os.access(p, os.X_OK) else None
    return shutil.which(exe)


def build_argv(
    provider: Dict,
    prompt: str,
    auto_approve: bool,
    read_only: bool = False,
) -> tuple[List[str], Optional[str]]:
    """Assemble the argv for a provider invocation.

    Returns ``(argv, stdin_text)``. ``stdin_text`` is None when the prompt is
    passed as an argument instead.

    ``auto_approve`` and ``read_only`` are opposite grants and never both set:
    the first is what the pipeline hands a stage the human approved, the second
    is what Solo Mode hands an assistant that will never be approved at all.

    The ``{prompt}`` token may appear anywhere in the template, including
    embedded in a larger string (e.g. ``--message={prompt}``) - the one
    exception being a prompt too large for argv, which has to move to stdin
    and cannot take a decorated placeholder with it.
    """
    template: List[str] = list(provider.get("command") or [])
    if not template:
        raise ProviderUnavailable("No command configured for this provider")

    placeholders = [t for t in template if PROMPT_TOKEN in t]
    configured_stdin = bool(provider.get("prompt_on_stdin"))
    oversized = len(prompt) > ARGV_PROMPT_LIMIT
    if (
        oversized
        and not configured_stdin
        and placeholders
        and placeholders[0].strip() != PROMPT_TOKEN
    ):
        # Emptying out `--message={prompt}` and piping the prompt instead is a
        # silent lie: a CLI that takes the prompt as an option value is not
        # necessarily reading stdin at all, so it would run on an empty task
        # and report success. Fail loudly rather than do nothing convincingly.
        raise ProviderUnavailable(
            f"The prompt is {len(prompt):,} characters, past the {ARGV_PROMPT_LIMIT:,} "
            f"argv limit, and `{placeholders[0]}` cannot be moved to stdin on its "
            f"own. Use a bare {PROMPT_TOKEN} argument, or enable 'Pipe the prompt "
            f"on stdin' in Settings."
        )
    use_stdin = configured_stdin or oversized

    argv: List[str] = []
    # Where the auto-approve flags belong: immediately before the prompt
    # argument. Guessing at "the end of the subcommand chain" instead is
    # unreliable - a template like `python3 script.py --role drafter {prompt}`
    # has no way to be distinguished from its own executable by inspection,
    # and misplacing a flag hands it to the wrong program entirely.
    prompt_index: Optional[int] = None

    for token in template:
        if PROMPT_TOKEN in token:
            # The *first* placeholder: flags belong ahead of every prompt
            # argument, and anchoring on a later one would leave them behind
            # the first - where a CLI reading a positional prompt has already
            # stopped looking for options.
            if prompt_index is None:
                prompt_index = len(argv)
            if use_stdin:
                # Drop a bare placeholder; keep a decorated one with the token
                # emptied out so flags like `--message=` are not malformed.
                if token.strip() == PROMPT_TOKEN:
                    continue
                argv.append(token.replace(PROMPT_TOKEN, ""))
            else:
                argv.append(token.replace(PROMPT_TOKEN, prompt))
        else:
            argv.append(token)

    if prompt_index is None and not use_stdin:
        # Template has no placeholder: append the prompt as a trailing arg.
        prompt_index = len(argv)
        argv.append(prompt)

    # Flags to splice in ahead of the prompt, grouped so the final argv reads
    # `cli --output-format X --model Y --auto-approve <prompt>` rather than
    # interleaved.
    extra: List[str] = []

    # Unconditional, unlike the auto-approve flags: these only change how the
    # CLI reports its progress, never what it is allowed to do. Without them
    # `claude -p` prints one block at the very end of the run and the live
    # stream sits empty until then. ProviderRunner translates whatever they
    # turn on back into readable lines.
    extra.extend(a for a in (provider.get("stream_args") or []) if a)

    model = str(provider.get("model") or "").strip()
    if model:
        # Fall back to the flag form, never to a bare `{model}`: a config
        # missing `model_args` would otherwise inject the model as a stray
        # positional argument, which most CLIs read as the prompt.
        template_args = provider.get("model_args") or ["--model", MODEL_TOKEN]
        for token in template_args:
            if not token:
                continue
            extra.append(token.replace(MODEL_TOKEN, model))

    effort = str(provider.get("effort") or "").strip()
    if effort:
        # No fallback here, unlike the model above. There is no spelling this
        # is safe to guess at: Claude takes `--effort high` and Codex takes
        # `-c model_reasoning_effort=high`, and either one handed to the other
        # binary is an error. A provider with no `effort_args` simply has no
        # effort knob, and the picker does not offer one.
        for token in provider.get("effort_args") or []:
            if not token:
                continue
            extra.append(token.replace(EFFORT_TOKEN, effort))

    if read_only:
        # Stated rather than assumed. Withholding the auto-approve flags is
        # enough to stop a CLI writing, but not enough to stop it *trying* -
        # and a solo conversation has no gate to answer the resulting prompt.
        extra.extend(a for a in (provider.get("read_only_args") or []) if a)

    if auto_approve:
        extra.extend(a for a in (provider.get("auto_approve_args") or []) if a)

    if extra:
        # With the prompt on stdin there is no positional to stay ahead of, so
        # the flags simply go last.
        insert_at = len(argv) if prompt_index is None else prompt_index
        argv[insert_at:insert_at] = extra

    return argv, (prompt if use_stdin else None)


def redact_argv(argv: List[str], prompt: str) -> List[str]:
    """Replace the full prompt in an argv echo with a short placeholder."""
    if not prompt:
        return list(argv)
    out = []
    for token in argv:
        if prompt and prompt in token:
            out.append(token.replace(prompt, f"<prompt: {len(prompt)} chars>"))
        else:
            out.append(token)
    return out


# --------------------------------------------------------------------------
# Streaming output
# --------------------------------------------------------------------------

# Longest line the translator will emit. Tool inputs and results are arbitrary
# blobs; a 40 KB file read has no business becoming one line in the browser.
STREAM_LINE_LIMIT = 400

# How much of a tool call or its result to show. Shorter than a line of prose:
# these are context for the agent's own words, not the point of the stream.
TOOL_DETAIL_LIMIT = 200

# The field of a tool's input worth putting on screen, most specific first.
# Whatever a tool calls its principal argument, one of these is usually it.
TOOL_SUMMARY_KEYS = (
    "command",
    "file_path",
    "path",
    "pattern",
    "url",
    "query",
    "description",
    "prompt",
)


def _one_line(value: Any, limit: int = STREAM_LINE_LIMIT) -> str:
    """Collapse a value into a single bounded line of text."""
    flat = " ".join(str(value).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class ClaudeStreamReader:
    """Turns ``claude --output-format stream-json`` events into readable lines.

    Why this exists: ``claude -p`` in its default text mode emits nothing until
    the run has finished - measured here at 8.7s into a 9.3s run - so the live
    stream stayed blank and then filled with the conclusion all at once. The
    streaming format emits one JSON object per step as it happens, which is what
    the UI needs; raw JSON on screen is no improvement over nothing, so this is
    the cost of getting it.

    The reader also keeps the run's final answer to one side. That, and not the
    transcript, is what the next stage's prompt and the Draft/Final panes want.

    Every accessor is defensive about shapes. A malformed or newly-invented
    event must not raise: the exception would kill the pump thread and take the
    rest of the run's output with it.
    """

    def __init__(self) -> None:
        self.result = ""  # the final assistant message, once it arrives
        self.error = ""  # set when the CLI reports a failed result

    def feed(self, line: str) -> List[str]:
        """Translate one line of CLI output into zero or more display lines."""
        stripped = line.strip()
        if not stripped:
            return []
        if not stripped.startswith("{"):
            # Not an event - a banner or a warning the CLI wrote to stdout
            # anyway. Pass it through rather than swallowing it.
            return [line]
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return [line]
        if not isinstance(event, dict):
            return [line]

        kind = event.get("type")
        if kind == "system":
            # The only detail here a human needs, and the only place it appears
            # when the stage is configured to use the CLI's default model.
            model = event.get("model")
            return [f"· model {model}"] if model else []
        if kind == "assistant":
            return self._assistant_lines(event.get("message"))
        if kind == "user":
            return self._tool_result_lines(event.get("message"))
        if kind == "result":
            text = str(event.get("result") or "")
            if event.get("is_error"):
                self.error = _one_line(text) or "The CLI reported a failed result."
                return [f"✗ {self.error}"]
            self.result = text
            return []
        # `rate_limit_event`, partial-message chunks, and whatever a future
        # version adds: nothing anyone needs to watch mid-run.
        return []

    # -- event shapes ------------------------------------------------------

    def _assistant_lines(self, message: Any) -> List[str]:
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        if isinstance(content, str):
            return content.splitlines()
        if not isinstance(content, list):
            return []

        out: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                out.extend(str(block.get("text") or "").splitlines())
            elif kind == "thinking":
                # The reasoning is the half of the run the operator most wants
                # to watch, so it goes through in full, marked as thinking.
                out.extend(
                    f"· {ln}" for ln in str(block.get("thinking") or "").splitlines()
                )
            elif kind == "tool_use":
                name = block.get("name") or "tool"
                out.append(f"→ {name}{self._tool_detail(block.get('input'))}")
        return out

    @staticmethod
    def _tool_detail(payload: Any) -> str:
        if not isinstance(payload, dict) or not payload:
            return ""
        for key in TOOL_SUMMARY_KEYS:
            if payload.get(key):
                return " " + _one_line(payload[key], TOOL_DETAIL_LIMIT)
        return " " + _one_line(json.dumps(payload, default=str), TOOL_DETAIL_LIMIT)

    @staticmethod
    def _tool_result_lines(message: Any) -> List[str]:
        """One summary line per tool result: its first line and how much more.

        Tool output is the bulkiest thing in the stream and the least worth
        reading in full - a single file read would bury the agent's own words.
        """
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        if not isinstance(content, list):
            return []

        out: List[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            body = block.get("content")
            if isinstance(body, list):
                body = "\n".join(
                    str(part.get("text") or "")
                    for part in body
                    if isinstance(part, dict)
                )
            lines = str(body or "").splitlines()
            head = _one_line(lines[0], TOOL_DETAIL_LIMIT) if lines else ""
            more = f" (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
            out.append(f"{'✗' if block.get('is_error') else '←'} {head}{more}".rstrip())
        return out


def _asks_for_stream_json(argv: List[str]) -> bool:
    """Whether this argv turns on the streaming event format.

    Only flag-shaped tokens are considered. The prompt is an argument in this
    same list, and a task that happens to mention stream-json must not switch a
    JSON parser on over what is still plain prose.
    """
    for index, token in enumerate(argv):
        if not token.startswith("-"):
            continue
        if "stream-json" in token:  # --output-format=stream-json
            return True
        if token == "--output-format" and argv[index + 1 : index + 2] == ["stream-json"]:
            return True
    return False


def stream_reader_for(argv: List[str]) -> Optional[ClaudeStreamReader]:
    """The translator for a command's output format, or None if it needs none.

    Keyed on the argv actually being run rather than on the configured
    template: a hand-edited command that drops the streaming flags produces
    plain text again, and parsing that as JSON would mangle it.
    """
    if not argv:
        return None
    exe = os.path.basename(str(argv[0]))
    if "claude" in exe and _asks_for_stream_json(argv):
        return ClaudeStreamReader()
    return None


class ProviderRunner:
    """Runs one CLI invocation, streaming output through a callback."""

    def __init__(
        self,
        provider: Dict,
        on_output: Callable[[str, str], None],
    ) -> None:
        """``on_output(stream_name, line)`` is called for each line produced."""
        self.provider = provider
        self.on_output = on_output
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._cancelled = False

    # -- lifecycle ---------------------------------------------------------

    def cancel(self) -> None:
        """Terminate the child process group, escalating to SIGKILL."""
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            # The CLIs spawn helper children; signal the whole group.
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                return
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    # -- execution ---------------------------------------------------------

    def run(
        self,
        prompt: str,
        cwd: str,
        auto_approve: bool,
        read_only: bool = False,
    ) -> ProviderResult:
        pid = str(self.provider.get("id", "provider"))
        started = time.monotonic()

        # Everything derived from configuration is fallible, and it all has to
        # come back as a *result*: the caller has already marked the stage
        # running, and only a result carries the reason back to the UI. An
        # exception here leaves the stage running forever with no error on it.
        try:
            argv, stdin_text = build_argv(
                self.provider, prompt, auto_approve, read_only=read_only
            )
            timeout = int(self.provider.get("timeout_seconds") or 900)
            if timeout <= 0:
                raise ValueError(f"timeout_seconds must be positive, got {timeout!r}")
        except (ProviderUnavailable, TypeError, ValueError) as exc:
            return ProviderResult(
                provider_id=pid,
                ok=False,
                exit_code=126,
                stdout="",
                stderr="",
                duration=time.monotonic() - started,
                command=[],
                error=f"This provider is misconfigured: {exc}",
            )
        echo = redact_argv(argv, prompt)

        if resolve_binary(argv) is None:
            return ProviderResult(
                provider_id=pid,
                ok=False,
                exit_code=127,
                stdout="",
                stderr="",
                duration=time.monotonic() - started,
                command=echo,
                error=(
                    f"`{argv[0]}` is not installed or not on PATH. "
                    f"Install it, or point this provider at a different "
                    f"command in Settings."
                ),
            )

        env = dict(os.environ)
        # Ask the CLIs for plain, unstyled output: ANSI escapes would have to
        # be stripped again before rendering as Markdown in the browser.
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        env["CI"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        # Non-None when this CLI reports structured events instead of prose.
        reader = stream_reader_for(argv)

        stdout_lines: List[str] = []
        stderr_lines: List[str] = []

        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line buffered, so the UI updates as output arrives
                errors="replace",
                start_new_session=True,  # own process group, for clean cancel
            )
        except (OSError, ValueError) as exc:
            return ProviderResult(
                provider_id=pid,
                ok=False,
                exit_code=126,
                stdout="",
                stderr="",
                duration=time.monotonic() - started,
                command=echo,
                error=f"Could not launch `{argv[0]}`: {exc}",
            )

        with self._lock:
            self._proc = proc
            was_cancelled = self._cancelled
        if was_cancelled:
            # cancel() landed between the check and the assignment.
            self.cancel()

        # stdin must be closed even when nothing is piped: these CLIs will
        # otherwise sit waiting for interactive input and hit the timeout.
        def feed_stdin() -> None:
            try:
                if stdin_text is not None and proc.stdin:
                    proc.stdin.write(stdin_text)
                if proc.stdin:
                    proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        def pump(stream, name: str, sink: List[str]) -> None:
            # Only stdout carries structured events; stderr is always prose.
            translate = reader.feed if reader and name == "stdout" else None
            try:
                for raw in iter(stream.readline, ""):
                    raw = raw.rstrip("\n")
                    for line in translate(raw) if translate else (raw,):
                        sink.append(line)
                        try:
                            self.on_output(name, line)
                        except Exception:  # a UI callback must never kill the pump
                            pass
            except (ValueError, OSError):
                pass
            finally:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

        threads = [
            threading.Thread(target=feed_stdin, name=f"{pid}-stdin", daemon=True),
            threading.Thread(
                target=pump, args=(proc.stdout, "stdout", stdout_lines),
                name=f"{pid}-out", daemon=True,
            ),
            threading.Thread(
                target=pump, args=(proc.stderr, "stderr", stderr_lines),
                name=f"{pid}-err", daemon=True,
            ),
        ]
        for t in threads:
            t.start()

        timed_out = False
        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.cancel()
            exit_code = proc.poll() if proc.poll() is not None else -1

        # Give the pumps a moment to flush whatever is still buffered.
        for t in threads:
            t.join(timeout=5)

        with self._lock:
            cancelled = self._cancelled

        duration = time.monotonic() - started
        stdout = "\n".join(stdout_lines)
        stderr = "\n".join(stderr_lines)
        if reader is not None and reader.result:
            # What was pumped is a transcript of the run; the next stage's
            # prompt and the Draft/Final panes want the answer itself. Falls
            # back to the transcript when no final answer arrived, which is the
            # only evidence a crashed or killed run leaves behind.
            stdout = reader.result

        error = ""
        if timed_out:
            error = f"Timed out after {timeout}s. Raise the limit in Settings."
        elif cancelled:
            error = "Cancelled by user."
        elif exit_code != 0:
            last_stderr = stderr.strip().splitlines()[-1] if stderr.strip() else ""
            # A CLI reporting its own failure in the event stream can leave
            # stderr empty, and that reason beats a bare exit status.
            reported = reader.error if reader is not None else ""
            error = last_stderr or reported or f"Exited with status {exit_code}."

        return ProviderResult(
            provider_id=pid,
            ok=(exit_code == 0 and not timed_out and not cancelled),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            command=echo,
            error=error,
            timed_out=timed_out,
            cancelled=cancelled,
        )


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------


def discover_models(provider: Dict) -> Dict:
    """Ask the CLI what models it can actually run, rather than guessing.

    A hand-written list is worse than no list: it looks authoritative and is
    wrong the moment a vendor renames a model or an account tier changes what
    it may use. Codex maintains ``models_cache.json`` in ``$CODEX_HOME``, keyed
    to the logged-in account and refreshed from the server, so that file is the
    real answer for the machine it is read on.

    Claude Code ships no equivalent cache; its ``--model`` help documents the
    alias form instead, and an alias always resolves to the current model in
    that family, so aliases are what we offer - each one asked what it points
    at right now, so the menu can say `opus -> claude-opus-5` instead of
    leaving the reader to guess which generation they just picked.

    Returns ``{"models": [...], "resolved": {alias: id}, "source": "...",
    "error": "..."}``. Callers fall back to the configured list when ``models``
    is empty, and must treat ``resolved`` as optional - it is empty whenever
    the CLI could not be asked.
    """
    exe = (provider.get("command") or [""])[0]
    base = os.path.basename(str(exe))

    if "codex" in base:
        return _discover_codex_models()
    if "claude" in base:
        return _discover_claude_models(provider)
    if "agy" in base:
        return _discover_agy_models(provider)
    return {"models": [], "source": "", "error": "No discovery available for this CLI."}


def _discover_agy_models(provider: Dict) -> Dict:
    """Ask `agy models`, which prints one model per line and nothing else.

    Antigravity has a subcommand for this, so there is no guessing to do at
    all. It needs the CLI to be signed in; when it is not, the CLI says so in
    a sentence worth passing straight through rather than paraphrasing.
    """
    path = resolve_binary(list(provider.get("command") or []))
    if not path:
        return {
            "models": [], "source": "",
            "error": f"`{(provider.get('command') or ['agy'])[0]}` is not on PATH.",
        }
    try:
        proc = subprocess.run(
            [path, "models"], capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"models": [], "source": "", "error": f"Could not run `agy models`: {exc}"}

    # A model id never contains a space, so this drops the "Available models:"
    # style heading and the human-readable "Gemini 3.6 Flash (High)" form the
    # CLI uses in error messages, without needing to know either is coming.
    models = [
        word for word in (line.strip() for line in (proc.stdout or "").splitlines())
        if word and not word.endswith(":") and len(word.split()) == 1
    ]
    error = ""
    if not models:
        error = _one_line((proc.stderr or proc.stdout or "").strip()) or (
            "`agy models` returned nothing."
        )
    return {
        "models": models,
        "source": "agy models (asked just now)" if models else "",
        "error": error,
    }


# The families `claude --model` documents as aliases. Pinned IDs are
# deliberately not enumerated alongside them: this app has no way to know which
# ones the account may use, and a stale pinned ID is the failure mode the
# aliases exist to avoid.
CLAUDE_ALIASES = ["opus", "sonnet", "haiku", "fable"]

# Resolutions live for the process, keyed by binary path. They only change when
# the CLI is upgraded, and a menu that reopens often should not pay four
# process launches every time.
_ALIAS_CACHE: Dict[str, Dict[str, str]] = {}
_ALIAS_LOCK = threading.Lock()

# Long enough for a cold CLI start on a loaded machine, short enough that a
# hung binary cannot hold a dropdown open indefinitely.
ALIAS_PROBE_TIMEOUT = 20.0


def _discover_claude_models(provider: Dict) -> Dict:
    """Offer the aliases, and name the model each one currently points at."""
    path = resolve_binary(list(provider.get("command") or []))
    resolved = _resolve_claude_aliases(path) if path else {}
    return {
        "models": list(CLAUDE_ALIASES),
        "resolved": resolved,
        "source": (
            "claude --model aliases, resolved by asking the CLI just now"
            if resolved else
            # Not an error: the aliases are still the right thing to offer, and
            # a red banner over a list that works would be a lie about it.
            "claude --model aliases (the CLI could not be asked which model "
            "each one points at)"
        ),
        "error": "",
    }


def _resolve_claude_aliases(path: str) -> Dict[str, str]:
    """Map every alias to the concrete model id it stands for, in parallel.

    Four sequential probes would put four seconds in front of a dropdown. Run
    together they finish in about one, and the answer is then cached for the
    life of the process.
    """
    with _ALIAS_LOCK:
        cached = _ALIAS_CACHE.get(path)
    if cached is not None:
        return dict(cached)

    with ThreadPoolExecutor(max_workers=len(CLAUDE_ALIASES)) as pool:
        pairs = pool.map(lambda a: (a, _resolve_claude_alias(path, a)), CLAUDE_ALIASES)
        resolved = {alias: model for alias, model in pairs if model}

    # Only a complete answer is cached. A partial one usually means the machine
    # was busy or offline for a moment, and caching it would freeze a blank
    # entry in the menu until the app restarts.
    if len(resolved) == len(CLAUDE_ALIASES):
        with _ALIAS_LOCK:
            _ALIAS_CACHE[path] = dict(resolved)
    return resolved


def _resolve_claude_alias(path: str, alias: str) -> str:
    """Ask `claude` what one alias resolves to, without spending anything.

    The CLI expands the alias itself, before it opens a connection: its `init`
    event names the resolved model, and that event still arrives when the API
    endpoint is pointed at a dead port. So this starts a session, reads the
    first line, and kills it - the prompt is never sent and no tokens are used.

    ``--session-id`` is fixed per alias so that repeated probing reuses the
    same slot instead of filling the user's `/resume` list with one dead
    session per dropdown they open.
    """
    session = str(uuid.uuid5(uuid.NAMESPACE_URL, f"theseus-ai/model-probe/{alias}"))
    proc = None
    watchdog = None
    try:
        proc = subprocess.Popen(
            [
                path, "-p", "--model", alias, "--session-id", session,
                "--output-format", "stream-json", "--verbose",
                # A prompt is required to get past argument validation. It is
                # never delivered - see above.
                "hi",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # Reading a pipe blocks, so the time limit has to come from outside it:
        # the kill closes stdout, the loop below ends, and the probe gives up.
        watchdog = threading.Timer(ALIAS_PROBE_TIMEOUT, proc.kill)
        watchdog.start()
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                return str(event.get("model") or "")
    except (OSError, ValueError):
        pass
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if proc is not None:
            proc.kill()
            # The pipe is still open on the return path, and this runs inside a
            # long-lived server: leaking one descriptor per probe would add up.
            if proc.stdout is not None:
                proc.stdout.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    return ""


def _discover_codex_models() -> Dict:
    """Read Codex's own account-scoped model cache."""
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    cache = Path(home) / "models_cache.json"
    if not cache.exists():
        return {
            "models": [],
            "source": "",
            "error": (
                f"{cache} not found. Run `codex` once so it fetches the model "
                f"list for your account."
            ),
        }
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"models": [], "source": str(cache), "error": f"Unreadable: {exc}"}

    models = []
    for entry in data.get("models") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        # `visibility: hide` marks internal models (e.g. codex-auto-review)
        # that are not meant to be selected as the session model.
        if slug and entry.get("visibility") == "list":
            models.append(str(slug))

    return {
        "models": models,
        "source": f"{cache} (fetched {data.get('fetched_at', '?')})",
        "error": "" if models else "Cache contained no selectable models.",
    }


# Fallback for `claude --effort`, used only when the probe below cannot run.
# Transcribed from the CLI's own validation message (claude 2.1.219) rather
# than invented, and ordered shallowest-first as the CLI lists them.
CLAUDE_EFFORT_FALLBACK = ["low", "medium", "high", "xhigh", "max"]

# The same, for `agy --effort`. Transcribed from its validation message
# ("valid: low, medium, high") on Antigravity CLI 1.1.7, not invented.
AGY_EFFORT_FALLBACK = ["low", "medium", "high"]

# What each level means. Codex publishes its own wording per model and that is
# preferred where it exists; this covers Claude, which publishes none.
EFFORT_NOTES = {
    "low": "fastest, least thinking",
    "medium": "balanced",
    "high": "deeper thinking, slower",
    "xhigh": "very deep, noticeably slower",
    "max": "maximum depth",
    "ultra": "maximum depth, delegates subtasks",
}


def discover_efforts(provider: Dict) -> Dict:
    """What reasoning levels the configured CLI will accept.

    The legal set is not universal and not static: Codex varies it per model
    (only some models offer ``ultra``) and Claude keeps its own. Both are asked
    rather than assumed, for the same reason the model list is.

    Returns ``{"levels": [{"effort", "description"}], "default": str,
    "source": str, "error": str}``. An empty ``levels`` means this command has
    no effort knob, which is the honest answer for a hand-written template.
    """
    if not (provider.get("effort_args") or []):
        return {
            "levels": [], "default": "", "source": "",
            "error": (
                "This command has no reasoning-effort setting. Add one as "
                "`effort_args` in Settings if the CLI accepts a level."
            ),
        }

    exe = (provider.get("command") or [""])[0]
    base = os.path.basename(str(exe))
    if "codex" in base:
        return _discover_codex_efforts(str(provider.get("model") or "").strip())
    if "claude" in base:
        return _discover_claude_efforts(provider)
    if "agy" in base:
        return _discover_agy_efforts(provider)
    return {
        "levels": [], "default": "", "source": "",
        "error": "No effort discovery available for this CLI.",
    }


def _discover_agy_efforts(provider: Dict) -> Dict:
    """Ask `agy` which levels it takes, and whether this model allows one.

    Two different "no" answers, deliberately kept apart. Antigravity refuses
    `--effort` beside any model `agy models` lists, because every name on that
    list is already a complete selection - `gemini-3.6-flash-high` has the
    level in it, and `claude-sonnet-4-6` has no such knob at all. The levels
    are for the base names it does *not* list, like `gemini-3.6-flash`.

    The list is what decides, never the shape of the name: reading the `-high`
    suffix looks right, and gets `claude-sonnet-4-6` wrong. Reporting this as
    an ordinary empty list would leave a stale level set and a run that dies at
    launch, so it is flagged as the definite refusal it is.
    """
    model = str(provider.get("model") or "").strip()
    if model and model in set(_discover_agy_models(provider).get("models") or []):
        return {
            "levels": [], "default": "", "source": "",
            "conflicts_with_model": True,
            "error": (
                f"{model} is one of the complete selections `agy models` lists, "
                f"so it takes no separate effort. To choose one, use a base "
                f"model name it does not list - gemini-3.6-flash rather than "
                f"gemini-3.6-flash-high."
            ),
        }

    path = resolve_binary(list(provider.get("command") or []))
    levels: List[str] = []
    source = ""
    if path:
        try:
            proc = subprocess.run(
                # Same trick as the Claude probe: a value no real level will
                # ever be makes the CLI print the legal set and exit. It is
                # rejected during argument validation, so no request is made
                # and no quota is spent - and no model is named, because
                # naming one here would risk the conflict above.
                [path, "--effort", "?ask", "--prompt=?"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            text = (proc.stdout or "") + (proc.stderr or "")
            match = re.search(r"valid:\s*([a-zA-Z0-9_, -]+?)\)", text)
            if match:
                levels = [v.strip() for v in match.group(1).split(",") if v.strip()]
                source = "agy --effort validation (asked just now)"
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass

    if not levels:
        levels = list(AGY_EFFORT_FALLBACK)
        source = "known values for agy --effort (the CLI could not be asked)"

    return {
        "levels": [
            {"effort": name, "description": EFFORT_NOTES.get(name, "")}
            for name in levels
        ],
        # Antigravity does not say which level it defaults to, and it varies by
        # model. Saying nothing beats naming one and being wrong.
        "default": "",
        "source": source,
        "error": "",
    }


def _discover_claude_efforts(provider: Dict) -> Dict:
    """Ask `claude` itself which levels it accepts.

    Handing it a value it cannot know makes it print the whole legal set and
    exit. With `-p ""` it never reaches the model, so this costs no quota and
    no tokens - it is a pure argument-validation path.
    """
    path = resolve_binary(list(provider.get("command") or []))
    levels: List[str] = []
    source = ""
    if path:
        try:
            proc = subprocess.run(
                # A value no real level will ever be. Not a NUL sentinel:
                # argv cannot carry one and subprocess rejects it outright.
                [path, "--effort", "?ask", "-p", ""],
                capture_output=True, text=True, timeout=30, check=False,
            )
            text = (proc.stdout or "") + (proc.stderr or "")
            match = re.search(r"Valid values:\s*([a-zA-Z0-9_, -]+)", text)
            if match:
                levels = [v.strip() for v in match.group(1).split(",") if v.strip()]
                source = "claude --effort validation (asked just now)"
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass

    if not levels:
        levels = list(CLAUDE_EFFORT_FALLBACK)
        source = "known values for claude --effort (the CLI could not be asked)"

    return {
        "levels": [
            {"effort": name, "description": EFFORT_NOTES.get(name, "")}
            for name in levels
        ],
        # Claude does not report which level it defaults to, and it varies by
        # model. Saying nothing beats naming one and being wrong.
        "default": "",
        "source": source,
        "error": "",
    }


def _discover_codex_efforts(model: str) -> Dict:
    """Read the levels Codex publishes for the selected model.

    Each entry in the model cache carries its own ``supported_reasoning_levels``
    and ``default_reasoning_level``, so a model that offers ``ultra`` and one
    that stops at ``xhigh`` are told apart rather than averaged.

    With no model pinned the CLI picks, and this cannot know which - so it
    offers only the levels *every* listed model accepts. That set is safe
    whatever the CLI chooses; picking a model first unlocks the rest.
    """
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    cache = Path(home) / "models_cache.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "levels": [], "default": "", "source": str(cache),
            "error": (
                f"Could not read {cache}: {exc}. Run `codex` once so it "
                f"fetches the catalogue for your account."
            ),
        }

    entries = [e for e in (data.get("models") or []) if isinstance(e, dict)]
    fetched = data.get("fetched_at", "?")

    def levels_of(entry: Dict) -> List[Dict[str, str]]:
        out = []
        for level in entry.get("supported_reasoning_levels") or []:
            if isinstance(level, dict) and level.get("effort"):
                out.append(
                    {
                        "effort": str(level["effort"]),
                        "description": str(level.get("description") or ""),
                    }
                )
        return out

    if model:
        for entry in entries:
            if str(entry.get("slug") or "") == model:
                return {
                    "levels": levels_of(entry),
                    "default": str(entry.get("default_reasoning_level") or ""),
                    "source": f"{cache} · {model} (fetched {fetched})",
                    "error": "",
                }
        return {
            "levels": [], "default": "", "source": str(cache),
            "error": (
                f"{model!r} is not in the model catalogue, so its reasoning "
                f"levels are unknown."
            ),
        }

    listed = [e for e in entries if e.get("visibility") == "list"]
    if not listed:
        return {
            "levels": [], "default": "", "source": str(cache),
            "error": "The model catalogue lists no selectable models.",
        }

    shared = set.intersection(*({l["effort"] for l in levels_of(e)} for e in listed))
    # Ordered by the first model that lists them, which is the vendor's own
    # shallow-to-deep ordering rather than anything alphabetical.
    ordered = [l for l in levels_of(listed[0]) if l["effort"] in shared]
    return {
        "levels": ordered,
        "default": "",
        "source": f"{cache} · levels common to every model (fetched {fetched})",
        "error": (
            "" if ordered else
            "No reasoning level is accepted by every model in the catalogue."
        ),
    }


def probe_all(providers: Dict[str, Dict], order: tuple) -> List[Dict]:
    """Probe several chairs, launching each distinct binary once.

    Six providers are usually two or three binaries: the council's two stages,
    the chat assistant and the three project roles all draw from the same small
    set of installed CLIs. `probe` spawns `<exe> --version` and waits up to
    fifteen seconds for it, and this runs on every `/api/state`, so probing per
    provider would have the dashboard launch six processes to learn three
    things - and would stall the first paint behind all of them.
    """
    cache: Dict[str, Dict] = {}
    out: List[Dict] = []
    for pid in order:
        provider = providers.get(pid)
        if not provider:
            continue
        command = list(provider.get("command") or [])
        # Keyed on the whole command word, not the resolved path: an
        # unresolvable command has no path, and every one of those would
        # otherwise share a single "" entry and report the first one's name.
        key = str(command[0]) if command else ""
        found = cache.get(key)
        if found is None:
            found = probe(provider)
            cache[key] = found
        out.append({**found, "id": pid, "label": provider.get("label")})
    return out


def probe(provider: Dict) -> Dict:
    """Check whether a provider's executable exists, for the UI status dots."""
    command = list(provider.get("command") or [])
    exe = command[0] if command else ""
    path = resolve_binary(command)
    info: Dict = {
        "id": provider.get("id"),
        "label": provider.get("label"),
        "executable": exe,
        "path": path or "",
        "available": path is not None,
        "version": "",
    }
    if not path:
        return info

    # Most CLIs answer --version quickly; treat any failure as non-fatal since
    # availability, not version, is what gates the pipeline.
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        out = (proc.stdout or proc.stderr or "").strip().splitlines()
        if out:
            info["version"] = out[0][:120]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return info
