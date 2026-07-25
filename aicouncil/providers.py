"""Adapters that shell out to locally installed, subscription-backed CLIs.

This module is the reason the app costs nothing per token. It never speaks
HTTP to a model endpoint and never reads an API key from the environment. It
launches the `codex` and `claude` binaries that are already authenticated
against the user's ChatGPT Plus/Pro and Claude Pro subscriptions, and streams
their stdout back to the UI line by line.

Two behaviours are worth calling out:

* **Auto-approve flags are injected, never hardcoded into the template.** The
  dangerous flags (``--dangerously-skip-permissions``,
  ``--dangerously-bypass-approvals-and-sandbox``) live in a separate config
  field and are appended only when ``auto_approve`` is set. The pipeline sets
  it in exactly two cases: Zero-Touch Mode is on, or a human clicked "Approve
  & execute" at the gate. Stage 1 never receives them - it is read-only.

* **No shell.** Commands are executed as argv lists with ``shell=False``, so a
  prompt containing backticks, ``$(...)`` or a semicolon is inert data rather
  than something the shell will interpret.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

PROMPT_TOKEN = "{prompt}"
MODEL_TOKEN = "{model}"

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
) -> tuple[List[str], Optional[str]]:
    """Assemble the argv for a provider invocation.

    Returns ``(argv, stdin_text)``. ``stdin_text`` is None when the prompt is
    passed as an argument instead.

    The ``{prompt}`` token may appear anywhere in the template, including
    embedded in a larger string (e.g. ``--message={prompt}``).
    """
    template: List[str] = list(provider.get("command") or [])
    if not template:
        raise ProviderUnavailable("No command configured for this provider")

    use_stdin = bool(provider.get("prompt_on_stdin")) or len(prompt) > ARGV_PROMPT_LIMIT

    argv: List[str] = []
    # Where the auto-approve flags belong: immediately before the prompt
    # argument. Guessing at "the end of the subcommand chain" instead is
    # unreliable - a template like `python3 script.py --role drafter {prompt}`
    # has no way to be distinguished from its own executable by inspection,
    # and misplacing a flag hands it to the wrong program entirely.
    prompt_index: Optional[int] = None

    for token in template:
        if PROMPT_TOKEN in token:
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

    # Flags to splice in ahead of the prompt. Model first so the final argv
    # reads `cli --model X --auto-approve <prompt>` rather than interleaved.
    extra: List[str] = []

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
    ) -> ProviderResult:
        pid = str(self.provider.get("id", "provider"))
        argv, stdin_text = build_argv(self.provider, prompt, auto_approve)
        echo = redact_argv(argv, prompt)

        if resolve_binary(argv) is None:
            return ProviderResult(
                provider_id=pid,
                ok=False,
                exit_code=127,
                stdout="",
                stderr="",
                duration=0.0,
                command=echo,
                error=(
                    f"`{argv[0]}` is not installed or not on PATH. "
                    f"Install it, or point this provider at a different "
                    f"command in Settings."
                ),
            )

        timeout = int(self.provider.get("timeout_seconds") or 900)
        env = dict(os.environ)
        # Ask the CLIs for plain, unstyled output: ANSI escapes would have to
        # be stripped again before rendering as Markdown in the browser.
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        env["CI"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        started = time.monotonic()
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
            try:
                for line in iter(stream.readline, ""):
                    line = line.rstrip("\n")
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

        error = ""
        if timed_out:
            error = f"Timed out after {timeout}s. Raise the limit in Settings."
        elif cancelled:
            error = "Cancelled by user."
        elif exit_code != 0:
            error = stderr.strip().splitlines()[-1] if stderr.strip() else (
                f"Exited with status {exit_code}."
            )

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
