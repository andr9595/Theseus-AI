"""Adding, installing and signing in to the agent CLIs, from Settings.

What this module does *not* do is hold a credential. **No agent LLM API key is
asked for, stored or sent** - and that is the boundary that matters, because it
is what keeps a run free at the point of use: a subscription login costs
nothing per token, and an API key would put every stage on metered billing.
Signing an agent in runs the vendor's own commands - `codex login`, `claude
auth login --claudeai`, the bundled installer - on the operator's behalf and
shows their output, so that adding an agent is a button rather than a paragraph
in a README. The token that comes out the far end is written by the vendor's
CLI into the vendor's own config directory, exactly as it would be if the same
command were typed in a terminal, and this app never sees it.

There is exactly one credential this app will accept, and it is not an LLM key:
a GitHub token, pasted in Settings so that pull-request mode and the agent CLIs
can work with GitHub. It is handled by handing it straight to `gh` on stdin and
forgetting it - see the GitHub section below for why that is the shape, and for
the one thing this app cannot promise about where `gh` then puts it.

Sign-in commands are catalogued in `config.py` rather than accepted from the
browser - see `AGENT_SETUP` and `GITHUB_SETUP`. An endpoint that took an argv
would be a local HTTP endpoint that runs anything.

One session runs at a time. These are interactive processes attached to a
pseudo-terminal, and two of them racing for the same vendor config directory is
a way to end up signed in as neither.
"""

from __future__ import annotations

import codecs
import fcntl
import itertools
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config as cfg
from . import gitutil
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
# GitHub
# --------------------------------------------------------------------------
#
# The one credential this app will carry, and it carries it for milliseconds:
# `github_connect` pipes the token to `gh` on stdin and returns. Nothing here
# writes it down, echoes it, or puts it in an argv. Everything afterwards is a
# question asked of `gh`.
#
# Why that also serves the agents: once `gh` holds the login, a CLI agent that
# runs `gh pr create` in the working folder is authenticated, and `gh auth
# setup-git` means a plain `git push` over HTTPS is too. Handing every agent a
# `GH_TOKEN` environment variable would have achieved the same thing while
# leaving the secret readable in `/proc/<pid>/environ` for the life of every
# run, and recoverable from any transcript that captured an environment dump.

# Anything shaped like a GitHub credential, so it can be scrubbed from text on
# its way to a browser or a log. `gh` already redacts its own output; this is
# the belt to that pair of braces, and it also covers a token pasted into the
# wrong box and echoed back inside an error message.
_TOKEN_LIKE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})\b"
)

# `gh auth status` phrases the account line differently across releases -
# "account NAME" since 2.40ish, "as NAME" before it - so both are read.
_GH_ACCOUNT = re.compile(r"Logged in to \S+ (?:account|as) (\S+)")
_GH_SCOPES = re.compile(r"Token scopes:\s*(.+)")
# The parenthetical on the account line says where the token actually lives:
# "(keyring)" when a secret store was available, otherwise a path.
_GH_STORE = re.compile(r"Logged in to \S+ (?:account|as) \S+\s*\(([^)]+)\)")


def redact(text: str) -> str:
    """Text with anything token-shaped replaced, for display or logging."""
    return _TOKEN_LIKE.sub("[redacted]", text or "")


# GitHub's OAuth scopes are a hierarchy, not a set of flags: granting `repo`
# grants `public_repo`, and granting `admin:org` grants `write:org` and with it
# `read:org`. A token is never issued carrying the implied children - `gh auth
# status` lists only what was actually checked - so comparing the wanted list
# against that raw list reports scopes as missing that the token demonstrably
# has. Only the parents this app might ask for are listed; the map does not
# need to be GitHub's whole catalogue to be correct about them.
_SCOPE_IMPLIES: Dict[str, tuple] = {
    "repo": (
        "repo:status", "repo_deployment", "public_repo", "repo:invite",
        "security_events",
    ),
    "admin:org": ("write:org",),
    "write:org": ("read:org",),
    "admin:public_key": ("write:public_key",),
    "write:public_key": ("read:public_key",),
    "admin:repo_hook": ("write:repo_hook",),
    "write:repo_hook": ("read:repo_hook",),
    "admin:gpg_key": ("write:gpg_key",),
    "write:gpg_key": ("read:gpg_key",),
    "user": ("read:user", "user:email", "user:follow"),
    "project": ("read:project",),
    "write:packages": ("read:packages",),
    "write:discussion": ("read:discussion",),
    "admin:enterprise": ("manage_billing:enterprise", "read:enterprise"),
}


def expand_scopes(granted) -> set:
    """Every scope a token holds, including the ones it implies.

    Transitive, so `admin:org` reaches `read:org` through `write:org` without
    the map having to state that edge itself.
    """
    seen = set()
    queue = list(granted or ())
    while queue:
        scope = queue.pop()
        if scope in seen:
            continue
        seen.add(scope)
        queue.extend(_SCOPE_IMPLIES.get(scope, ()))
    return seen


def missing_scopes(wanted, granted) -> List[str]:
    """Which of ``wanted`` the token genuinely lacks.

    Empty when nothing is granted, which is not the same claim: a fine-grained
    token reports no classic scopes at all, and listing every wanted scope as
    "missing" for one would be telling the operator to fix a token that is
    probably fine. Its permissions are simply not knowable from here.
    """
    if not granted:
        return []
    held = expand_scopes(granted)
    return [s for s in wanted if s not in held]


def _gh_binary() -> Optional[str]:
    return resolve_binary(["gh"])


def _run_gh(
    args: List[str],
    stdin_text: Optional[str] = None,
    timeout: float = STATUS_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run `gh` non-interactively, optionally feeding it stdin.

    ``stdin_text`` is the token path. It is passed through ``input=`` so it
    reaches the child on a pipe and never touches the command line or the disk.
    """
    path = _gh_binary()
    if not path:
        raise ValueError(
            "The GitHub CLI (`gh`) is not installed. Install it first - the "
            "button above does it without sudo."
        )
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["GH_PAGER"] = "cat"
    env["GH_PROMPT_DISABLED"] = "1"
    # A stale GH_TOKEN in the environment silently outranks the stored login,
    # which would make a "connected" card describe a credential this app did
    # not set and cannot clear.
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return subprocess.run(
        [path, *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def github_status() -> Dict[str, Any]:
    """Whether `gh` has a GitHub login, and what it is good for.

    Shaped like `agent_status`: *installed* and *connected* are separate facts,
    and "could not ask" is not reported as "signed out".
    """
    out: Dict[str, Any] = {
        "installed": False,
        "connected": False,
        "account": "",
        "scopes": [],
        "storage": "",
        "detail": "",
        "docs_url": str(cfg.GITHUB_SETUP.get("docs_url") or ""),
        "wanted_scopes": list(cfg.GITHUB_SETUP.get("scopes") or ()),
        "install_hint": " ".join(cfg.github_install_command()),
    }
    path = _gh_binary()
    if not path:
        out["detail"] = "The GitHub CLI (`gh`) is not installed."
        return out
    out["installed"] = True
    out["path"] = path

    command = list(cfg.GITHUB_SETUP["status_command"])[1:]
    try:
        proc = _run_gh(command)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        out["detail"] = redact(f"Could not ask gh: {exc}")
        return out

    # gh writes the status report to stderr and has done for several majors,
    # but not consistently enough to read only one of the two.
    text = _sanitize(f"{proc.stdout or ''}\n{proc.stderr or ''}").strip()
    out["connected"] = proc.returncode == 0
    account = _GH_ACCOUNT.search(text)
    if account:
        out["account"] = account.group(1)
    store = _GH_STORE.search(text)
    if store:
        out["storage"] = store.group(1).strip()
    scopes = _GH_SCOPES.search(text)
    if scopes:
        out["scopes"] = [
            s.strip().strip("'\"") for s in scopes.group(1).split(",") if s.strip()
        ]
    if not out["connected"]:
        out["detail"] = redact(text.splitlines()[0][:160]) if text else "Not connected."
        return out

    out["missing_scopes"] = missing_scopes(out["wanted_scopes"], out["scopes"])
    return out


def github_connect(token: str) -> Dict[str, Any]:
    """Hand one token to `gh`, then forget it.

    The token is not returned, stored, logged or echoed. If `gh` rejects it the
    reason is passed back with anything token-shaped scrubbed, because the most
    likely reason is that the wrong string was pasted and the most likely place
    for it to end up is a screenshot of the error.
    """
    token = (token or "").strip()
    if not token:
        raise ValueError("Paste a GitHub token first.")
    # Checked before spending a round trip, and phrased as a hint rather than a
    # hard rule: GitHub has changed its token prefixes before and an enterprise
    # host may not use them at all.
    if "\n" in token or "\r" in token or " " in token:
        raise ValueError(
            "That does not look like a token - it has whitespace in it. Copy "
            "just the token, with no surrounding quotes or line breaks."
        )

    login = list(cfg.GITHUB_SETUP["login_command"])[1:]
    try:
        proc = _run_gh(login, stdin_text=token + "\n", timeout=STATUS_TIMEOUT * 2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(redact(f"Could not run gh: {exc}")) from exc
    finally:
        # Not security theatre against a memory dump - CPython interns and
        # copies strings and this cannot be guaranteed - but it does stop the
        # value being reachable from a traceback frame if what follows raises.
        token = ""

    if proc.returncode != 0:
        detail = _sanitize(
            (proc.stderr or "").strip() or (proc.stdout or "").strip()
        )
        raise ValueError(
            redact(detail) or "GitHub rejected that token."
        )

    # Best effort, and deliberately not fatal: the login is what the card
    # reports, and a working login with git left unconfigured is still better
    # than reporting failure and leaving the token installed anyway.
    git_note = ""
    try:
        setup = _run_gh(list(cfg.GITHUB_SETUP["setup_git_command"])[1:])
        if setup.returncode != 0:
            git_note = _sanitize((setup.stderr or "").strip())[:160]
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        git_note = f"{type(exc).__name__}: {exc}"

    # `setup-git` teaches git how to authenticate a push; it says nothing
    # about who the commit is from. A container built fresh for this app has
    # never had `git config user.name`/`user.email` run in it, so without
    # this the very first commit - the one Projects' "Commit & push" or a
    # Zero-Touch run tries to make - fails on exactly that. Filled in from
    # this account (its display name, or its login if that is not public,
    # and its GitHub-issued noreply address - no extra scope needed), and
    # only ever filled in: `ensure_global_identity` leaves an identity the
    # operator already set alone.
    try:
        who = _run_gh(["api", "user"])
        if who.returncode == 0 and who.stdout.strip():
            account = json.loads(who.stdout)
            login = str(account.get("login") or "").strip()
            account_id = account.get("id")
            name = str(account.get("name") or "").strip() or login
            if name and login and account_id is not None:
                gitutil.ensure_global_identity(
                    name, f"{account_id}+{login}@users.noreply.github.com"
                )
    except (OSError, subprocess.TimeoutExpired, ValueError,
            json.JSONDecodeError, gitutil.GitError):
        # Same shape as the setup-git failure above: the login still
        # succeeded, and the identity gap is one `git config` away from being
        # fixed by hand, same as it always was.
        pass

    status = github_status()
    if git_note:
        status["git_warning"] = redact(git_note)
    return status


def github_disconnect() -> Dict[str, Any]:
    """Ask `gh` to forget the login. This app has nothing of its own to clear."""
    try:
        proc = _run_gh(list(cfg.GITHUB_SETUP["logout_command"])[1:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(redact(f"Could not run gh: {exc}")) from exc
    if proc.returncode != 0:
        detail = _sanitize((proc.stderr or "").strip() or (proc.stdout or "").strip())
        # gh exits non-zero when there was nothing to log out of, which is the
        # state the caller asked for rather than a failure.
        if "not logged" not in detail.lower():
            raise ValueError(redact(detail) or "gh could not log out.")
    return github_status()


# How many repositories the picker's GitHub tab asks for in one call. One
# request, filtered afterwards in the browser as the operator types - not a
# `gh` call per keystroke.
REPO_LIST_LIMIT = 100
REPO_LIST_TIMEOUT = 20.0


def github_repos() -> Dict[str, Any]:
    """Repositories the connected login can see - for the "GitHub repo" half
    of the working-folder picker, the alternative to browsing to a local one.

    `gh repo list` with no owner argument lists the authenticated user's own
    repositories, which is exactly "somewhere I could point the council at",
    not every repository on GitHub. Connection status is reported the same
    three-valued way as everything else GitHub-shaped in this module: not
    installed, not connected, or here is what was found.
    """
    if not _gh_binary():
        return {
            "connected": False, "repos": [],
            "error": "The GitHub CLI (`gh`) is not installed.",
        }
    try:
        proc = _run_gh(
            [
                "repo", "list", "--limit", str(REPO_LIST_LIMIT), "--json",
                "nameWithOwner,description,updatedAt,isPrivate,defaultBranchRef",
            ],
            timeout=REPO_LIST_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"connected": False, "repos": [], "error": redact(f"Could not ask gh: {exc}")}
    if proc.returncode != 0:
        detail = redact((proc.stderr or proc.stdout or "").strip())
        return {
            "connected": False, "repos": [],
            "error": detail.splitlines()[-1] if detail else "gh repo list failed.",
        }
    try:
        raw = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        raw = []
    repos = [
        {
            "repo": r.get("nameWithOwner", ""),
            "description": r.get("description") or "",
            "updated_at": r.get("updatedAt", ""),
            "private": bool(r.get("isPrivate")),
            "default_branch": (r.get("defaultBranchRef") or {}).get("name", ""),
        }
        for r in raw
        if isinstance(r, dict) and r.get("nameWithOwner")
    ]
    return {"connected": True, "repos": repos, "error": ""}


# --------------------------------------------------------------------------
# Setup sessions
# --------------------------------------------------------------------------


class SetupSession:
    """One install or sign-in, running on a pseudo-terminal.

    A pipe is not good enough here. Both vendors' login commands check whether
    they are talking to a terminal and take the quiet, non-interactive path
    when they are not - which for a sign-in means refusing rather than printing
    the URL that is the whole point. So the child gets a real pty.

    Two views of that pty are kept side by side. `output` is stripped of its
    escape sequences - readable rather than rendered, which is fine for a flow
    that prints a URL and waits and is the one every non-terminal caller still
    polls. `raw_output` keeps them, for the one case that needs to actually
    render a screen rather than read lines - see `on_raw` and `openTuiTerminal`
    in app.js, which feeds it to a real terminal emulator. Sequence-numbered so
    a browser that missed some of it (a closed tab, a reconnect) can ask for
    exactly the bytes it does not have rather than replaying blind.
    """

    def __init__(
        self,
        agent: str,
        action: str,
        argv: List[str],
        id: int,
        on_raw: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self.id = id
        self.agent = agent
        self.action = action
        self.command = list(argv)
        self.started_at = time.time()
        self.finished_at = 0.0
        self.exit_code: Optional[int] = None
        self._buffer = ""
        self._raw_buffer = ""
        self._raw_seq = 0
        self._on_raw = on_raw
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
        # One incremental decoder for the life of the session: a pty read can
        # split a multi-byte UTF-8 character across two chunks (a box-drawing
        # glyph, an emoji in a spinner), and decoding each chunk on its own -
        # the previous behaviour - would corrupt exactly that character rather
        # than carrying its other half over to the next read.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            try:
                chunk = os.read(self._master, 4096)
            except OSError:
                break  # the child exited and the pty closed under us
            if not chunk:
                break
            text = decoder.decode(chunk)
            if not text:
                continue
            self._append(_sanitize(text))
            self._append_raw(text)
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

    def _append_raw(self, text: str) -> None:
        with self._lock:
            self._raw_buffer = (self._raw_buffer + text)[-OUTPUT_LIMIT:]
            self._raw_seq += 1
            seq = self._raw_seq
        if self._on_raw is not None:
            self._on_raw(text, seq)

    # -- interaction -------------------------------------------------------

    def write(self, text: str) -> None:
        """Answer a prompt. One line, as typed, with the newline supplied."""
        if not self.running:
            raise ValueError("That setup session has already finished.")
        try:
            os.write(self._master, (text + "\n").encode("utf-8"))
        except OSError as exc:
            raise ValueError(f"Could not reach the setup session: {exc}") from None

    def write_raw(self, data: str) -> None:
        """Send bytes exactly as given - a keystroke, not a line.

        The terminal's counterpart to `write`: that one is for the
        line-oriented "type an answer" box and always appends a newline. A
        real terminal decides for itself when a carriage return means
        anything - arrow keys, Ctrl+C and every other control sequence are
        not lines and must not become one by having a newline forced onto
        them.
        """
        if not self.running:
            raise ValueError("That setup session has already finished.")
        try:
            os.write(self._master, data.encode("utf-8"))
        except OSError as exc:
            raise ValueError(f"Could not reach the setup session: {exc}") from None

    def resize(self, rows: int, cols: int) -> None:
        """Tell the pty its new size. The kernel delivers SIGWINCH for us -
        nothing here has to signal the child directly."""
        if not self.running or self._master < 0:
            return
        try:
            fcntl.ioctl(
                self._master, termios.TIOCSWINSZ,
                struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0),
            )
        except OSError:
            pass

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
            raw_output = self._raw_buffer
            raw_seq = self._raw_seq
        urls = _URL.findall(output)
        return {
            "id": self.id,
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
            # For a terminal that just opened or reattached: one write of
            # this brings its screen to the current state, then live "pty"
            # events (tagged with a seq past this one) carry the rest - see
            # `raw_seq` and openTuiTerminal in app.js.
            "raw_output": raw_output,
            "raw_seq": raw_seq,
        }


class SetupManager:
    """The one setup session that may be running, and how it is reached."""

    def __init__(self, bus: Optional[Any] = None) -> None:
        self._lock = threading.Lock()
        self._session: Optional[SetupSession] = None
        # Optional: an EventBus to publish raw pty bytes on, for the one kind
        # of session (a full-screen sign-in) a browser has to actually render
        # rather than read. None is a legitimate value - `SetupManager()` on
        # its own still runs every session, it just cannot offer a live
        # terminal for the TUI ones, which is exactly the situation the
        # non-terminal test fakes in test_connections.py want.
        self._bus = bus
        self._next_id = itertools.count(1)

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
            sid = next(self._next_id)
            bus = self._bus
            on_raw = (
                (lambda text, seq, _sid=sid: bus.publish(
                    "pty", session=_sid, seq=seq, data=text
                ))
                if bus is not None else None
            )
            session = SetupSession(agent, action, argv, id=sid, on_raw=on_raw)
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
        # GitHub is not an agent and has no login *session*: its token arrives
        # through its own endpoint, in one request, and never on a terminal.
        # Installing `gh` is the one thing it shares with the agents, because
        # that is a long download worth watching.
        if agent == "github":
            if action != "install":
                raise ValueError(
                    "GitHub connects with a token rather than a terminal "
                    "session. Use the Connect button on its card."
                )
            if not shutil.which("bash"):
                raise ValueError(
                    "The installer needs bash. Install `gh` with your package "
                    "manager instead."
                )
            return ["bash", *cfg.github_install_command()]
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
            # `login_tui` used to be a refusal here - "this pane cannot draw a
            # full-screen session, run it in a terminal yourself" - because the
            # scrollback pane genuinely could not. It is not a refusal any
            # more: the frontend opens a real terminal emulator instead of the
            # plain-text pane for exactly these sessions (see `openTuiTerminal`
            # in app.js), fed from the same pty this starts either way. The
            # flag now only tells the browser which view to draw.
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

    def write_raw(self, data: str) -> Dict[str, Any]:
        session = self.current()
        if session is None:
            raise ValueError("No setup session is open.")
        session.write_raw(data)
        return session.to_dict()

    def resize(self, rows: int, cols: int) -> Dict[str, Any]:
        session = self.current()
        if session is None:
            return {}
        session.resize(rows, cols)
        return session.to_dict()

    def cancel(self) -> Dict[str, Any]:
        session = self.current()
        if session is None:
            return {}
        session.cancel()
        return session.to_dict()
