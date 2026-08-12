"""Adding, installing and signing in to the agent CLIs, from Settings.

What this module does *not* do is hold a credential. There is no field
anywhere in this app that takes an API key, and there is no OAuth flow
implemented here. What it does is run the vendor's own commands - `codex
login`, `claude auth login --claudeai`, the bundled installer - on the
operator's behalf and show their output, so that adding an agent is a button
rather than a paragraph in a README. The token that comes out the far end is
written by the vendor's CLI into the vendor's own config directory, exactly as
it would be if the same command were typed in a terminal, and this app never
sees it.

That boundary is what keeps a run free at the point of use: a subscription
login costs nothing per token, and an API key would put every stage on metered
billing. It is also why the sign-in commands are catalogued in `config.py`
rather than accepted from the browser - see `AGENT_SETUP`. An endpoint that
took an argv would be a local HTTP endpoint that runs anything.

One session runs at a time. These are interactive processes attached to a
pseudo-terminal, and two of them racing for the same vendor config directory is
a way to end up signed in as neither.
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
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config as cfg
from .providers import resolve_binary

try:  # POSIX only, which is what this app supports
    import pty
except ImportError:  # pragma: no cover - Windows
    pty = None  # type: ignore[assignment]

# How long a non-interactive status check may take before it is treated as no
# answer. Generous: `agy` in particular is a large binary and a cold start on a
# spinning disk is not a failure.
STATUS_TIMEOUT = 25.0

# How much of a setup session's output is kept. A login prints a URL and a
# handful of lines; an install prints a progress bar. Neither needs scrollback,
# and an unbounded buffer is a memory leak with a `curl` in front of it.
OUTPUT_LIMIT = 40_000

# A session left open by a closed tab is still a process holding a terminal.
SESSION_MAX_SECONDS = 15 * 60

# Enough of an ANSI escape vocabulary to make a progress bar readable. The
# vendors' login flows are line-oriented once colour is gone; the one that is
# not says so in `AGENT_SETUP` and is never run here.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|\r(?!\n)")
_URL = re.compile(r"https?://[^\s\"'<>]+")


def _sanitize(text: str) -> str:
    """Readable text from a terminal stream, for a pane that is not one."""
    return _ANSI.sub("", text).replace("\x00", "")


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


def auth_status(agent: str) -> Dict[str, Any]:
    """Ask one CLI whether it is signed in, without opening anything.

    Three answers, not two. ``True`` and ``False`` come from a CLI that has a
    status command; ``None`` means the question could not be asked - the binary
    is missing, or the CLI has no such command - and the UI says so rather than
    reporting a red cross that reads as "signed out". Antigravity is the second
    case: its 1.1.12 release has no auth subcommand at all.
    """
    setup = cfg.AGENT_SETUP.get(agent) or {}
    command = list(setup.get("status_command") or [])
    out: Dict[str, Any] = {"signed_in": None, "detail": ""}
    if not command:
        out["detail"] = "This CLI has no sign-in check; open it to see."
        return out
    path = resolve_binary(command)
    if not path:
        return out

    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    try:
        proc = subprocess.run(
            [path, *command[1:]],
            capture_output=True, text=True, env=env,
            timeout=STATUS_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        out["detail"] = f"Could not ask {command[0]}: {exc}"
        return out

    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    # Claude answers with a JSON object; read it rather than pattern-matching
    # prose that is free to be reworded in the next release.
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and "loggedIn" in parsed:
        out["signed_in"] = bool(parsed["loggedIn"])
        who = str(parsed.get("email") or parsed.get("authMethod") or "")
        plan = str(parsed.get("subscriptionType") or "")
        out["detail"] = " · ".join(p for p in (who, plan) if p)
        return out

    out["signed_in"] = proc.returncode == 0
    out["detail"] = _sanitize(text).splitlines()[0][:160] if text else ""
    return out


def agent_status(conf: Dict[str, Any], agent: str) -> Dict[str, Any]:
    """One connection card's worth of truth about a catalogued CLI.

    Three independent facts, kept apart on purpose. *Added* is what the
    operator asked for and the only one that decides whether the agent is
    seated. *Installed* is whether the binary resolves. *Signed in* is whether
    the vendor says the CLI has an account behind it. Collapsing them is how a
    setup screen ends up claiming an agent is ready because `--version` worked.
    """
    preset = cfg.AGENTS.get(agent) or {}
    setup = cfg.AGENT_SETUP.get(agent) or {}
    command = list(preset.get("command") or [])
    path = resolve_binary(command)
    status = auth_status(agent) if path else {
        "signed_in": None,
        "detail": "" if command else "No command configured.",
    }
    return {
        "id": agent,
        "label": str(preset.get("label") or agent),
        "selected": agent in cfg.selected_agents(conf),
        "installed": path is not None,
        "path": path or "",
        "executable": command[0] if command else "",
        "login_tui": bool(setup.get("login_tui")),
        "docs_url": str(setup.get("docs_url") or ""),
        "account": str(setup.get("account") or ""),
        "login_hint": " ".join(setup.get("login_command") or []),
        "install_hint": " ".join(cfg.install_command(agent)),
        **status,
    }


def agent_statuses(conf: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every catalogued CLI, probed in parallel.

    Three status commands run one after another is three cold starts in series,
    and this is what the Agents panel waits on before it can be used.
    """
    agents = list(cfg.AGENTS)
    out: Dict[str, Dict[str, Any]] = {}
    lock = threading.Lock()

    def probe(agent: str) -> None:
        try:
            found = agent_status(conf, agent)
        except Exception as exc:  # noqa: BLE001 - a card, not a run
            found = {"id": agent, "error": f"{type(exc).__name__}: {exc}"}
        with lock:
            out[agent] = found

    threads = [
        threading.Thread(target=probe, args=(a,), name=f"status-{a}", daemon=True)
        for a in agents
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=STATUS_TIMEOUT + 5)
    return [out[a] for a in agents if a in out]


# --------------------------------------------------------------------------
# Setup sessions
# --------------------------------------------------------------------------


class SetupSession:
    """One install or sign-in, running on a pseudo-terminal.

    A pipe is not good enough here. Both vendors' login commands check whether
    they are talking to a terminal and take the quiet, non-interactive path
    when they are not - which for a sign-in means refusing rather than printing
    the URL that is the whole point. So the child gets a real pty, and the
    output is stripped of its escape sequences on the way back up.

    What is streamed back is therefore *readable* rather than *rendered*. That
    is fine for a flow that prints a URL and waits, and it is not fine for one
    that draws a menu - which is why `AGENT_SETUP` marks the second kind and
    Settings offers those as a command to run in a terminal instead.
    """

    def __init__(self, agent: str, action: str, argv: List[str]) -> None:
        self.agent = agent
        self.action = action
        self.command = list(argv)
        self.started_at = time.time()
        self.finished_at = 0.0
        self.exit_code: Optional[int] = None
        self._buffer = ""
        self._lock = threading.Lock()
        self._master = -1
        self._proc: Optional[subprocess.Popen] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if pty is None:  # pragma: no cover - Windows
            raise ValueError("Interactive setup needs a POSIX terminal.")
        master, slave = pty.openpty()
        env = dict(os.environ)
        # The opposite of the run path's `TERM=dumb`: these commands are meant
        # to be interactive, and one told it has no terminal declines to be.
        env["TERM"] = "xterm-256color"
        env.setdefault("PATH", os.defpath)
        # The vendors' installers drop their binary here and the shell only
        # learns about it at the next login, so a sign-in immediately after an
        # install would not find what was just installed.
        local_bin = str(Path.home() / ".local" / "bin")
        if local_bin not in env["PATH"].split(os.pathsep):
            env["PATH"] = os.pathsep.join([local_bin, env["PATH"]])
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=slave, stdout=slave, stderr=slave,
                env=env, cwd=str(cfg.REPO_ROOT), close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(slave)
        self._master = master
        threading.Thread(
            target=self._pump, name=f"setup-{self.agent}", daemon=True
        ).start()

    def _pump(self) -> None:
        while True:
            try:
                chunk = os.read(self._master, 4096)
            except OSError:
                break  # the child exited and the pty closed under us
            if not chunk:
                break
            self._append(_sanitize(chunk.decode("utf-8", "replace")))
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            self.exit_code = proc.returncode
        self.finished_at = time.time()
        try:
            os.close(self._master)
        except OSError:
            pass

    def _append(self, text: str) -> None:
        with self._lock:
            self._buffer = (self._buffer + text)[-OUTPUT_LIMIT:]

    # -- interaction -------------------------------------------------------

    def write(self, text: str) -> None:
        """Answer a prompt. One line, as typed, with the newline supplied."""
        if not self.running:
            raise ValueError("That setup session has already finished.")
        try:
            os.write(self._master, (text + "\n").encode("utf-8"))
        except OSError as exc:
            raise ValueError(f"Could not reach the setup session: {exc}") from None

    def cancel(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        # The whole group: the installers pipe curl into bash, and killing the
        # bash alone leaves the download running with nothing reading it.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            proc.terminate()
        self._append("\n[cancelled]\n")

    # -- reporting ---------------------------------------------------------

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            output = self._buffer
        urls = _URL.findall(output)
        return {
            "agent": self.agent,
            "action": self.action,
            "command": " ".join(self.command),
            "running": self.running,
            "exit_code": self.exit_code,
            "output": output,
            # Surfaced separately because it is the one line of a login that
            # the operator has to act on, and it is usually not the last one.
            "url": urls[-1] if urls else "",
            "elapsed": round(time.time() - self.started_at, 1),
        }


class SetupManager:
    """The one setup session that may be running, and how it is reached."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: Optional[SetupSession] = None

    def start(self, agent: str, action: str) -> Dict[str, Any]:
        argv = self._argv(agent, action)
        with self._lock:
            current = self._session
            if current is not None and current.running:
                if time.time() - current.started_at < SESSION_MAX_SECONDS:
                    raise ValueError(
                        f"{current.command[0]} is still running. Finish or "
                        f"cancel it before starting another."
                    )
                current.cancel()
            session = SetupSession(agent, action, argv)
            session.start()
            self._session = session
            return session.to_dict()

    @staticmethod
    def _argv(agent: str, action: str) -> List[str]:
        """The catalogued argv for one action, or a refusal.

        Nothing from the request reaches a command line: the agent id selects a
        row of `AGENT_SETUP`, the action selects a column, and an id or action
        that is not in the table is an error rather than a fallback.
        """
        if agent not in cfg.AGENT_SETUP:
            raise ValueError(f"No such agent: {agent!r}")
        if action == "install":
            argv = cfg.install_command(agent)
            if not shutil.which("bash"):
                raise ValueError(
                    "The installer needs bash. Install this CLI with the "
                    "vendor's own command instead."
                )
            return ["bash", *argv]
        if action == "login":
            setup = cfg.AGENT_SETUP[agent]
            if setup.get("login_tui"):
                raise ValueError(
                    f"{agent} signs in inside its own full-screen session, "
                    f"which this pane cannot draw. Run `"
                    f"{' '.join(setup.get('login_command') or [])}` in a "
                    f"terminal instead."
                )
            argv = list(setup.get("login_command") or [])
            path = resolve_binary(argv)
            if not path:
                raise ValueError(
                    f"`{argv[0] if argv else agent}` is not installed yet. "
                    f"Install it first, then sign in."
                )
            return [path, *argv[1:]]
        raise ValueError(f"Unknown setup action: {action!r}")

    def current(self) -> Optional[SetupSession]:
        with self._lock:
            return self._session

    def status(self) -> Dict[str, Any]:
        session = self.current()
        return session.to_dict() if session else {}

    def write(self, text: str) -> Dict[str, Any]:
        session = self.current()
        if session is None:
            raise ValueError("No setup session is open.")
        session.write(text)
        return session.to_dict()

    def cancel(self) -> Dict[str, Any]:
        session = self.current()
        if session is None:
            return {}
        session.cancel()
        return session.to_dict()
