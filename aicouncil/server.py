"""Local HTTP + SSE server built on ``http.server``.

Security posture
----------------
The server binds to 127.0.0.1 only, but "localhost" is not a security boundary
on a multi-user or browser-hosting machine: any page you visit can issue
requests to http://127.0.0.1:8760, and any local process can too. Since this
app's API can execute a coding agent with ``--dangerously-skip-permissions``,
three defences are layered on:

1. **Session token.** Generated per launch, never persisted. Every ``/api/``
   request must present it. The token never reaches a command line: the
   launcher's URL carries a single-use *ticket*, which the page trades for the
   token through ``POST /api/session`` before its first real request.
2. **Origin / Host validation.** Requests whose ``Origin`` is a real remote
   site are rejected, which blocks the drive-by CSRF case, and a non-loopback
   ``Host`` header is rejected, which blocks DNS rebinding.
3. **No shell.** See ``providers.py`` - argv lists, never a shell string.
"""

from __future__ import annotations

import getpass
import json
import mimetypes
import os
import queue
import re
import secrets
import socket
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import APP_NAME, __version__
from . import config as cfg
from . import connections
from . import gitutil
from .events import EventBus, drain, sse_comment, sse_format
from .pipeline import Pipeline, PipelineBusy
from . import prompts
from . import projects
from .projects import ProjectBusy, ProjectEngine
from .providers import discover_efforts, discover_models, probe_all, resolve_binary
from .usage import UsagePoller

WEB_ROOT = Path(__file__).resolve().parent / "web"
HEARTBEAT_SECONDS = 15.0
MAX_BODY_BYTES = 4 * 1024 * 1024

ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


class AppState:
    """Container for the objects every request handler needs."""

    def __init__(self, store: cfg.ConfigStore) -> None:
        self.store = store
        self.bus = EventBus()
        self.pipeline = Pipeline(store, self.bus)
        # Projects and the pipeline are peers, and they conflict: both drive
        # coding agents against the same working folder, and two agents editing
        # one tree with no idea the other exists is how a build ends up with
        # half of each. Neither reaches into the other to find that out - this
        # object owns both, so it is what knows, and each is handed a callable
        # that refuses on the other's behalf.
        self.projects = ProjectEngine(store, self.bus, busy_check=self._refuse_if_busy)
        # Installs and sign-ins started from Settings. One at a time, and owned
        # here rather than by a request handler: the browser polls a session it
        # started on an earlier request, and a handler-scoped one would be a
        # process nothing could reach a second later.
        self.setup = connections.SetupManager()
        self.token = secrets.token_urlsafe(24)
        # The launcher has no way to hand a browser a secret except on its
        # command line, where every other user on the machine can read it out
        # of the process table for as long as the window stays open. So the
        # URL carries a *ticket* rather than the token: the first page to
        # present it is given the token in a response body and the ticket dies
        # on the spot, which narrows the exposure from the whole session to
        # however long the browser takes to start.
        self._ticket = secrets.token_urlsafe(24)
        self._ticket_lock = threading.Lock()
        self.started_at = time.time()
        # Publishing on the bus means every open tab updates together, and a
        # tab opened later replays the last reading instead of showing blank.
        self.usage = UsagePoller(
            store, on_update=lambda snap: self.bus.publish("usage", usage=snap)
        )
        # Let the seating router see how much quota each CLI has left, so it can
        # route around one that is nearly out rather than seating it and having
        # the run die at the first stage. The poller is owned here, not by the
        # pipeline, so it is handed over as a callable - and the router works
        # without it, which is why this is wiring rather than a constructor
        # argument. Readings are the vendor's own; nothing here computes one.
        self.pipeline.quota_source = self._agent_quota

    @property
    def ticket(self) -> str:
        """The unredeemed launch ticket, or "" once it has been spent."""
        with self._ticket_lock:
            return self._ticket

    def redeem_ticket(self, supplied: str) -> Optional[str]:
        """Trade a valid, unspent ticket for the session token.

        Under the lock, so two windows racing to open the same printed URL
        cannot both win - exactly one gets the token and the other is told to
        relaunch.
        """
        with self._ticket_lock:
            if not supplied or not self._ticket:
                return None
            if not secrets.compare_digest(supplied, self._ticket):
                return None
            self._ticket = ""
            return self.token

    def _agent_quota(self) -> Dict[str, Optional[float]]:
        """Percent of its window each council CLI has consumed, where known.

        Keyed by agent rather than by provider id, because that is what the
        router seats. A CLI the poller has no reading for is absent from the
        map, and the router treats absent as "no signal" rather than as zero.
        """
        out: Dict[str, Optional[float]] = {}
        for agent in cfg.AGENTS:
            percent = self.usage.worst_percent(cfg.council_provider_id(agent))
            if percent is not None:
                out[agent] = percent
        return out

    def _refuse_if_busy(self) -> None:
        """Raise if a council or chat run is working the tree right now."""
        if self.pipeline.is_busy():
            raise PipelineBusy(
                "A run is in progress. A project drives the same folder, so "
                "wait for it to finish or cancel it first."
            )

    def refuse_if_project_running(self) -> None:
        """The mirror image, for the paths that start a council or chat run."""
        if self.projects.is_running():
            raise PipelineBusy(
                "A project is running in this folder. Pause or stop it before "
                "starting a run - both drive agents against the same files."
            )


class Handler(BaseHTTPRequestHandler):
    """Routes requests to the API or the static bundle."""

    server_version = f"TheseusAI/{__version__}"
    protocol_version = "HTTP/1.1"
    app: AppState  # injected by make_server

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        # BaseHTTPRequestHandler logs every request to stderr by default,
        # which would drown the launcher's own output. Errors still surface
        # via log_error, which routes here too - keep those.
        if args and str(args[0]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _origin_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip()
        if host and host not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urllib.parse.urlparse(origin)
            except ValueError:
                return False
            if parsed.hostname not in ALLOWED_HOSTS:
                return False
        return True

    def _authorized(self, params: Dict[str, list]) -> bool:
        supplied = ""
        header = self.headers.get("X-AC-Token")
        if header:
            supplied = header.strip()
        elif "token" in params:
            supplied = params["token"][0]
        return bool(supplied) and secrets.compare_digest(supplied, self.app.token)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json",
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # The UI ships entirely from disk with no remote assets; a strict CSP
        # means an agent's Markdown output can never pull in a third party.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'",
        )
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"))

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _read_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("Request body too large.")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Malformed JSON body: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _workspace_from(self, body: Dict[str, Any]) -> str:
        """The folder a request names, with an empty one taken at its word.

        Blank is an answer - the scratch workspace - and not the same as not
        asking, so the saved folder only fills in when the key is absent
        altogether. Read with ``or`` instead, an explicit "no folder" would be
        replaced by whatever the picker was last pointed at, and a run the
        operator confirmed for scratch would write into that repository.
        """
        if "workspace" not in body:
            return str(self.app.store.get("workspace") or "")
        value = body["workspace"]
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("`workspace` must be a folder path.")
        return value

    def _incognito_from(self, body: Dict[str, Any]) -> bool:
        """Whether this request asked for a private run.

        Typed strictly rather than read for truthiness: the string ``"false"``
        is true in Python, and a client that sent one would be told its run was
        private while every stage was recorded as usual.
        """
        value = body.get("incognito", False)
        if not isinstance(value, bool):
            raise ValueError("`incognito` must be true or false.")
        return value

    # -- dispatch ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - name fixed by the base class
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._origin_ok():
            self._error(HTTPStatus.FORBIDDEN, "Rejected: untrusted Host or Origin.")
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(parsed.query)

        if not path.startswith("/api"):
            if method != "GET":
                self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed.")
            else:
                self._serve_static(path)
            return

        # The one API call that cannot present a token: it is how the page
        # gets one. The Origin and Host checks above still apply to it.
        if path == "/api/session":
            self._api_session(method)
            return

        if not self._authorized(params):
            self._error(
                HTTPStatus.UNAUTHORIZED,
                "Missing or invalid session token. Reopen the app from the "
                "URL the launcher printed.",
            )
            return

        if path == "/api/events":
            self._serve_events(params)
            return

        routes: Dict[Tuple[str, str], Callable[[Dict[str, list]], Any]] = {
            ("GET", "/api/state"): self._api_state,
            ("GET", "/api/config"): lambda p: {"ok": True, "config": self.app.store.all()},
            ("GET", "/api/fs"): self._api_fs,
            ("GET", "/api/repo"): self._api_repo,
            ("GET", "/api/history"): self._api_history,
            ("POST", "/api/history/delete"): self._api_history_delete,
            ("GET", "/api/run"): self._api_run,
            ("GET", "/api/context"): self._api_context,
            ("GET", "/api/doctor"): self._api_doctor,
            ("GET", "/api/models"): self._api_models,
            ("GET", "/api/efforts"): self._api_efforts,
            ("GET", "/api/usage"): lambda p: {
                "ok": True, "usage": self.app.usage.snapshot()
            },
            ("POST", "/api/usage/refresh"): self._api_usage_refresh,
            ("POST", "/api/config"): self._api_set_config,
            ("POST", "/api/config/reset"): lambda p: {
                "ok": True, "config": self.app.store.reset()
            },
            ("POST", "/api/start"): self._api_start,
            ("POST", "/api/approve"): self._api_approve,
            ("POST", "/api/reject"): self._api_reject,
            ("POST", "/api/cancel"): self._api_cancel,
            ("POST", "/api/resume"): self._api_resume,
            ("POST", "/api/retry"): self._api_retry,
            ("POST", "/api/rollback"): self._api_rollback,
            ("POST", "/api/commit"): self._api_commit,
            ("GET", "/api/project"): self._api_project,
            ("POST", "/api/project/start"): self._api_project_start,
            ("POST", "/api/project/pause"): self._api_project_pause,
            ("POST", "/api/project/resume"): self._api_project_resume,
            ("POST", "/api/project/stop"): self._api_project_stop,
            ("POST", "/api/project/handoff"): self._api_project_handoff,
            ("POST", "/api/project/dismiss"): self._api_project_dismiss,
            ("GET", "/api/project/file"): self._api_project_file,
            ("POST", "/api/council/route"): self._api_council_route,
            ("GET", "/api/roles"): lambda p: {
                "ok": True,
                "roles": prompts.role_catalog(self.app.store.get("roles", {})),
            },
            ("POST", "/api/roles"): self._api_save_role,
            ("POST", "/api/roles/delete"): self._api_delete_role,
            ("GET", "/api/agents"): self._api_agents,
            ("POST", "/api/agents/select"): self._api_agents_select,
            ("POST", "/api/agents/setup"): self._api_agent_setup,
            ("GET", "/api/agents/setup"): lambda p: {
                "ok": True, "session": self.app.setup.status()
            },
            ("POST", "/api/agents/setup/input"): self._api_agent_setup_input,
            ("POST", "/api/agents/setup/cancel"): lambda p: {
                "ok": True, "session": self.app.setup.cancel()
            },
        }

        handler = routes.get((method, path))
        if handler is None:
            self._error(HTTPStatus.NOT_FOUND, f"No such endpoint: {path}")
            return

        try:
            result = handler(params)
        except (PipelineBusy, ProjectBusy) as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except (gitutil.GitError, OSError) as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        except Exception as exc:  # never leak a traceback to the browser
            self.log_error("Unhandled error on %s: %r", path, exc)
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            self._json(HTTPStatus.OK, result)

    # -- session -----------------------------------------------------------

    def _api_session(self, method: str) -> None:
        """Exchange the launcher's one-time ticket for the session token."""
        if method != "POST":
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed.")
            return
        try:
            body = self._read_body()
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        # Body only, never the query string: a ticket in a URL is a ticket in
        # a referrer, a history entry and a server log.
        token = self.app.redeem_ticket(str(body.get("ticket") or "").strip())
        if token is None:
            self._error(
                HTTPStatus.UNAUTHORIZED,
                "That launch ticket is not valid, or has already been used. "
                "Restart the app to get a fresh URL.",
            )
            return
        self._json(HTTPStatus.OK, {"ok": True, "token": token})

    # -- static ------------------------------------------------------------

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = (WEB_ROOT / rel).resolve()
        try:
            # Containment check: reject anything that escapes the web root.
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, "Forbidden.")
            return
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return

        ctype, _ = mimetypes.guess_type(str(target))
        try:
            body = target.read_bytes()
        except OSError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        self._send(
            HTTPStatus.OK,
            body,
            ctype or "application/octet-stream",
            {"Cache-Control": "no-store"},
        )

    # -- SSE ---------------------------------------------------------------

    def _serve_events(self, params: Dict[str, list]) -> None:
        """Hold an open response and stream pipeline events to the browser."""
        last_id = 0
        header_id = self.headers.get("Last-Event-ID")
        raw_id = header_id or (params.get("last_id") or ["0"])[0]
        try:
            last_id = int(raw_id)
        except (TypeError, ValueError):
            last_id = 0

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = self.app.bus.subscribe(replay_from=last_id)
        try:
            # Prime the stream so EventSource fires `open` immediately.
            self.wfile.write(sse_comment(f"{APP_NAME} stream open"))
            self.wfile.flush()
            while True:
                wrote = False
                for event in drain(q, timeout=HEARTBEAT_SECONDS):
                    self.wfile.write(sse_format(event))
                    wrote = True
                if not wrote:
                    # Heartbeat: proves the socket is alive and keeps any
                    # intermediary from timing the connection out.
                    self.wfile.write(sse_comment())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the tab closed; this is the normal exit path
        finally:
            self.app.bus.unsubscribe(q)

    # -- API handlers ------------------------------------------------------

    def _api_state(self, params: Dict[str, list]) -> Dict[str, Any]:
        state = self.app.pipeline.snapshot_state()
        state["ok"] = True
        state["version"] = __version__
        state["usage"] = self.app.usage.snapshot()
        # Whose desktop this is, for the sidebar footer. There are no accounts
        # here - the app is a local tool bound to loopback - so the only true
        # answer is the OS user it was launched as.
        try:
            state["user"] = getpass.getuser()
        except (KeyError, OSError):
            state["user"] = ""
        workspace = state["config"].get("workspace") or ""
        # Reported whether or not the folder is a repository: "not a git
        # repository" is a state the UI shows rather than an error, since it
        # only decides which of the git-backed features are on offer.
        state["workspace_status"] = (
            gitutil.status(workspace).to_dict() if workspace else None
        )
        # Where a run lands when no folder is chosen. Served rather than
        # guessed in the browser, which knows nothing about XDG paths.
        state["scratch_workspace"] = str(cfg.workspace_dir())
        return state

    def _api_set_config(self, params: Dict[str, list]) -> Dict[str, Any]:
        patch = self._read_body()
        # These are derived/runtime values; refuse to let the client set them.
        for protected in ("version",):
            patch.pop(protected, None)
        # Mode decides which pipeline a run takes and which controls the UI
        # shows. An unknown value would be stored, read back as "not council"
        # by nothing and as council by everything, so refuse it here.
        if "mode" in patch and patch["mode"] not in ("council", "solo"):
            raise ValueError("Mode must be either 'council' or 'solo'.")
        conf = self.app.store.update(patch)
        self.app.bus.publish("config", config=conf)
        return {"ok": True, "config": conf}

    def _api_fs(self, params: Dict[str, list]) -> Dict[str, Any]:
        path = (params.get("path") or [str(Path.home())])[0]
        listing = gitutil.list_directory(path)
        listing["ok"] = not listing.get("error")
        listing["home"] = str(Path.home())
        return listing

    def _api_repo(self, params: Dict[str, list]) -> Dict[str, Any]:
        path = (params.get("path") or [""])[0]
        if not path:
            raise ValueError("A `path` query parameter is required.")
        out: Dict[str, Any] = {"ok": True, "status": gitutil.status(path).to_dict()}
        # `?diff=1` is what the status bar's uncommitted chip asks for when it
        # is opened. Status and patch are read in one request on purpose: two
        # requests would be two different moments, and a file could be saved
        # between them, leaving a list and a patch that disagree about what is
        # in the tree the operator is about to commit.
        if (params.get("diff") or [""])[0] not in ("", "0", "false"):
            out["diff"] = gitutil.working_diff(path)
            out["stat"] = gitutil.diff_stat(path)
        return out

    def _api_history(self, params: Dict[str, list]) -> Dict[str, Any]:
        # Council and Chat conversations are not interchangeable - continuing
        # one in the other mode is refused - so the sidebar asks for the mode
        # it is showing rather than filtering a mixed list in the browser.
        mode = (params.get("mode") or [""])[0]
        if mode not in ("council", "solo", ""):
            raise ValueError("Mode must be either 'council' or 'solo'.")
        return {"ok": True, "runs": self.app.pipeline.history(mode=mode)}

    def _api_history_delete(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Delete one conversation, or every conversation of a mode.

        Deliberately two explicit shapes rather than one with an optional
        filter: a bug that dropped the filter from a request would otherwise
        turn "delete this one" into "delete everything".
        """
        body = self._read_body()
        name = str(body.get("file") or "")
        if name:
            if not self.app.pipeline.delete_run(name):
                raise ValueError("No such run transcript.")
            return {"ok": True, "deleted": 1}

        if not body.get("all"):
            raise ValueError(
                "Send a transcript filename, or `all: true` to clear the list."
            )
        mode = str(body.get("mode") or "")
        if mode not in ("council", "solo", ""):
            raise ValueError("Mode must be either 'council' or 'solo'.")
        return {"ok": True, "deleted": self.app.pipeline.clear_history(mode)}

    def _api_run(self, params: Dict[str, list]) -> Dict[str, Any]:
        name = (params.get("file") or [""])[0]
        data = self.app.pipeline.load_run(name)
        if data is None:
            raise ValueError("No such run transcript.")
        return {"ok": True, "run": data}

    def _api_context(self, params: Dict[str, list]) -> Dict[str, Any]:
        """How much context continuing this transcript would replay."""
        name = (params.get("file") or [""])[0]
        if not name:
            raise ValueError("A `file` query parameter is required.")
        return {"ok": True, "context": self.app.pipeline.context_preview(name)}

    def _api_doctor(self, params: Dict[str, list]) -> Dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "config_path": str(self.app.store.path),
            "runs_path": str(self.app.pipeline.runs_dir),
            "uptime": round(time.time() - self.app.started_at, 1),
            "providers": probe_all(self.app.store.all(), cfg.PROVIDER_ORDER),
        }

    @staticmethod
    def _refresh_asked(params: Dict[str, list]) -> bool:
        """Whether the menu asked for a fresh probe rather than the catalogue.

        Sent only by its Refresh button. Everything else - opening a picker,
        the alias prefetch at boot - takes the stored answer, which is the
        difference between a dropdown that paints and one that waits on `agy`.
        """
        return (params.get("refresh") or [""])[0] in ("1", "true")

    def _api_models(self, params: Dict[str, list]) -> Dict[str, Any]:
        """What models the configured CLI reports it can actually run."""
        pid = (params.get("provider") or [""])[0]
        provider = self.app.store.get("providers", {}).get(pid)
        if not provider:
            raise ValueError(f"No such provider: {pid!r}")
        result = discover_models(provider, refresh=self._refresh_asked(params))
        result["ok"] = True
        result["provider"] = pid
        result["current"] = provider.get("model", "")
        return result

    def _api_efforts(self, params: Dict[str, list]) -> Dict[str, Any]:
        """What reasoning levels the configured CLI accepts for its model."""
        pid = (params.get("provider") or [""])[0]
        provider = self.app.store.get("providers", {}).get(pid)
        if not provider:
            raise ValueError(f"No such provider: {pid!r}")
        result = discover_efforts(provider, refresh=self._refresh_asked(params))
        result["ok"] = True
        result["provider"] = pid
        result["current"] = provider.get("effort", "")
        # Named so the menu can say which model the levels belong to - they
        # differ between them, and a list with no model attached invites the
        # assumption that it is universal.
        result["model"] = provider.get("model", "")
        return result

    # -- agents ------------------------------------------------------------

    def _api_agents(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Which CLIs are added, which are installed, which are signed in.

        Its own endpoint rather than part of `/api/state` for the reason the
        doctor report is: answering it means shelling out to every catalogued
        CLI twice, and the dashboard must paint before that finishes. The
        Agents panel opens on what `/api/state` already knows - which agents
        the operator added - and fills the rest in when this lands.
        """
        conf = self.app.store.all()
        return {
            "ok": True,
            "agents": connections.agent_statuses(conf),
            "session": self.app.setup.status(),
        }

    def _api_agents_select(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Add or remove one CLI. The only thing that decides who is seated.

        Removing the CLI a chair was holding moves that chair to one still
        added, rather than leaving Chat or a project pointed at an agent the
        operator has just said they do not use. Council seats are not touched:
        there is one per CLI and `enabled` is what takes them off the bench.
        """
        body = self._read_body()
        agent = str(body.get("agent") or "")
        if agent not in cfg.AGENTS:
            raise ValueError(f"No such agent: {agent!r}")
        selected = bool(body.get("selected"))

        conf = self.app.store.all()
        patch: Dict[str, Any] = {"agent_settings": {agent: {"selected": selected}}}
        moved: Dict[str, str] = {}
        if not selected:
            remaining = [a for a in cfg.selected_agents(conf) if a != agent]
            successor = next(
                (a for a in remaining if resolve_binary(
                    (cfg.AGENTS[a].get("command") or [])
                )),
                remaining[0] if remaining else "",
            )
            if successor:
                providers = conf.get("providers") or {}
                for pid in ("solo", *projects.ROLES):
                    if cfg.agent_for(providers.get(pid) or {}) != agent:
                        continue
                    patch.setdefault("providers", {})[pid] = {"agent": successor}
                    moved[pid] = successor

        conf = self.app.store.update(patch)
        self.app.bus.publish("config", config=conf)
        return {"ok": True, "config": conf, "moved": moved}

    def _api_agent_setup(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Start an install or a sign-in. Never given a command, only an id."""
        body = self._read_body()
        session = self.app.setup.start(
            str(body.get("agent") or ""), str(body.get("action") or "")
        )
        return {"ok": True, "session": session}

    def _api_agent_setup_input(self, params: Dict[str, list]) -> Dict[str, Any]:
        body = self._read_body()
        return {"ok": True, "session": self.app.setup.write(str(body.get("text") or ""))}

    def _api_usage_refresh(self, params: Dict[str, list]) -> Dict[str, Any]:
        self.app.usage.poll_once()
        return {"ok": True, "usage": self.app.usage.snapshot()}

    def _api_start(self, params: Dict[str, list]) -> Dict[str, Any]:
        body = self._read_body()
        task = body.get("task") or ""
        # No folder is a legitimate answer, in either mode: the pipeline runs
        # in the scratch workspace instead of refusing to start.
        workspace = self._workspace_from(body)
        continue_from = body.get("continue_from") or ""
        compact_context = body.get("compact_context", False)
        if not isinstance(continue_from, str):
            raise ValueError("`continue_from` must be a transcript filename.")
        if not isinstance(compact_context, bool):
            raise ValueError("`compact_context` must be true or false.")
        # Checked here rather than inside the pipeline: the engine and the
        # pipeline are peers that know nothing of each other, and this is the
        # object that owns both.
        self.app.refuse_if_project_running()
        run = self.app.pipeline.start(
            task,
            workspace,
            continue_from=continue_from,
            compact_context=compact_context,
            incognito=self._incognito_from(body),
        )
        return {"ok": True, "run": run.to_dict()}

    def _api_approve(self, params: Dict[str, list]) -> Dict[str, Any]:
        self.app.pipeline.approve(self._read_body().get("note", ""))
        return {"ok": True}

    def _api_reject(self, params: Dict[str, list]) -> Dict[str, Any]:
        self.app.pipeline.reject(self._read_body().get("note", ""))
        return {"ok": True}

    def _api_cancel(self, params: Dict[str, list]) -> Dict[str, Any]:
        self.app.pipeline.cancel()
        return {"ok": True}

    def _api_resume(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Run the failed run's unfinished stages again, reusing the rest.

        With no ``file`` this continues the run the engine is still holding.
        With one it loads that transcript back first, which is the path after
        the app has been restarted - the answers already paid for are on disk
        either way.
        """
        name = str(self._read_body().get("file") or "")
        pipeline = self.app.pipeline
        run = pipeline.revive(name) if name else pipeline.resume()
        return {"ok": True, "run": run.to_dict()}

    def _api_retry(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Run one seat again, with whatever quoted it.

        ``file`` loads a transcript back first, exactly as `/api/resume` does:
        a seat whose answer is not good enough is worth replacing whether or
        not the app has been restarted since it gave it.
        """
        body = self._read_body()
        stage = str(body.get("stage") or "")
        if not stage:
            raise ValueError("Which stage should run again?")
        name = str(body.get("file") or "")
        pipeline = self.app.pipeline
        if name:
            pipeline.revive(name, start=False)
        return {"ok": True, "run": pipeline.retry(stage).to_dict()}

    def _api_commit(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Commit the working tree of the selected folder."""
        body = self._read_body()
        workspace = self._workspace_from(body)
        if not workspace:
            raise ValueError(
                "No working folder is selected, so there is nothing to commit."
            )
        if self.app.pipeline.is_busy() or self.app.projects.is_running():
            # Committing underneath a running agent would capture a tree it is
            # still editing, and the resulting commit would match neither the
            # diff that was reviewed nor the one the run ends with. A project
            # is the same hazard for longer.
            raise ValueError(
                "A run or project is in progress. Wait for it to finish."
            )
        result = gitutil.commit_all(workspace, str(body.get("message") or ""))
        self.app.bus.publish("committed", commit=result, workspace=workspace)
        return {"ok": True, "commit": result}

    # -- Projects ----------------------------------------------------------

    def _api_project(self, params: Dict[str, list]) -> Dict[str, Any]:
        """The Projects tab's whole view: the run, or what is on disk.

        Takes the folder as a parameter rather than reading it from config so
        that opening the tab while looking at a folder reports *that* folder's
        project, even before the operator has committed to it.
        """
        workspace = (params.get("workspace") or [""])[0]
        state = self.app.projects.snapshot_state(workspace)
        state["ok"] = True
        # Which chairs can actually be filled. The matrix draws its dots from
        # this, so a chair the operator has not connected - or whose CLI is
        # missing - is visible before the start button is pressed rather than
        # in the error that refuses it.
        # Carrying what each seat is for and which CLI belongs in it, so the
        # matrix can say why a chair exists rather than only which binary is
        # currently sitting in it.
        state["roles"] = [
            {**role, **cfg.PROJECT_SEATS.get(role["id"], {})}
            for role in probe_all(self.app.store.all(), projects.ROLES)
        ]
        state["settings"] = self.app.store.get("project", {})
        return state

    def _api_project_start(self, params: Dict[str, list]) -> Dict[str, Any]:
        body = self._read_body()
        if self._incognito_from(body):
            # Refused rather than ignored. A build's board, spec and critique
            # log are the state it pauses, resumes and is audited from, so a
            # project that kept none of them would not be a project - and a
            # request quietly answered with the opposite of what it asked for
            # is the one failure mode a privacy control cannot have.
            raise ValueError(
                "A project keeps its board and spec on disk, so it cannot run "
                "incognito."
            )
        # `innovation` is per-run, not a saved setting: it is the slider on the
        # initializer, and the answer to "how much may it invent this time" is
        # different for a throwaway prototype and somebody's production repo.
        project = self.app.projects.start(
            str(body.get("goal") or body.get("brief") or ""),
            self._workspace_from(body),
            resume=bool(body.get("resume")),
            innovation=_opt_int(body.get("innovation")),
        )
        return {"ok": True, "project": project.to_dict()}

    def _api_project_pause(self, params: Dict[str, list]) -> Dict[str, Any]:
        self.app.projects.pause()
        return {"ok": True}

    def _api_project_resume(self, params: Dict[str, list]) -> Dict[str, Any]:
        self.app.projects.resume()
        return {"ok": True}

    def _api_project_stop(self, params: Dict[str, list]) -> Dict[str, Any]:
        self.app.projects.stop()
        return {"ok": True}

    def _api_project_handoff(self, params: Dict[str, list]) -> Dict[str, Any]:
        self.app.projects.handoff(str(self._read_body().get("role") or ""))
        return {"ok": True}

    def _api_project_dismiss(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Close the report on a finished project so the tab offers a new one.

        Server-side because the engine holds the project in memory and finds it
        again in ``.theseus/BOARD.json``: clearing it in the browser alone lasts
        until the next reload. Nothing on disk is deleted.
        """
        body = self._read_body()
        self.app.projects.dismiss(str(body.get("workspace") or ""))
        return {"ok": True}

    def _api_project_file(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Read one of the project's own files, for the tracker's viewer.

        A fixed set of names, never a path from the client: this endpoint reads
        a folder the operator chose, and taking a filename would turn "show me
        the board" into an arbitrary-file read over an authenticated but
        drive-by-reachable local port.
        """
        name = (params.get("name") or ["board"])[0]
        workspace = (params.get("workspace") or [""])[0].strip()
        if not workspace:
            # Nothing asked for: the running project's folder, then the chosen
            # one. A folder that *was* asked for is never overridden - the tab
            # can be looking at a different one from the project in memory.
            project = self.app.projects.project
            workspace = (
                project.workspace if project is not None
                else (self.app.store.get("workspace") or "")
            )
        if not workspace:
            raise ValueError("No project folder is selected.")

        ws = projects.Workspace(workspace)
        readable = {
            "board": ws.board_path,
            "critique": ws.critique_path,
            "spec": ws.spec_path,
        }
        if name not in readable:
            raise ValueError(
                f"Not a project file: {name!r}. "
                f"Expected one of {', '.join(sorted(readable))}."
            )
        return {
            "ok": True,
            "name": name,
            "path": str(readable[name]),
            "text": ws.read_text(readable[name], projects.MAX_UI_TEXT),
        }

    def _api_council_route(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Who would be seated for this prompt, without spending a token on it.

        The composer calls this as the operator types so the strip shows the
        bench that would actually run. It is the same call `start()` makes, on
        the same config, so what is previewed is what is seated - a preview
        computed a second way would eventually disagree with the run.

        The run id is deliberately not passed: it is what shuffles the
        anonymous aliases, and a preview that reshuffled the letters on every
        keystroke would be unreadable. The letters are assigned for real when
        the run starts.
        """
        body = self._read_body()
        task = str(body.get("task") or "")
        # Incognito narrows the field to the CLIs that can be told not to save
        # the conversation, so a preview that ignored it would show a bench the
        # run would not seat.
        seating = self.app.pipeline.seat_council(
            task, self.app.store.all(), incognito=self._incognito_from(body)
        )
        return {"ok": True, "seating": seating.to_dict()}

    def _api_save_role(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Create a role, or edit one - including a built-in."""
        body = self._read_body()
        role_id = str(body.get("id") or "").strip().lower()
        role_id = re.sub(r"[^a-z0-9_]+", "_", role_id).strip("_")
        if not role_id:
            raise ValueError("A role needs an id.")
        if not str(body.get("name") or "").strip():
            raise ValueError("A role needs a name.")
        if not str(body.get("system") or "").strip():
            raise ValueError("A role needs prompt text.")

        roles = self.app.store.get("roles", {}) or {}
        roles[role_id] = {
            "name": str(body["name"]).strip(),
            "summary": str(body.get("summary") or "").strip(),
            "system": str(body["system"]),
            "writes": bool(body.get("writes")),
        }
        conf = self.app.store.update({"roles": roles})
        self.app.bus.publish("config", config=conf)
        return {
            "ok": True,
            "roles": prompts.role_catalog(conf.get("roles", {})),
            "saved": role_id,
        }

    def _api_delete_role(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Delete a custom role, or restore a built-in to its shipped text.

        The same call for both: a built-in has no stored entry once its edit is
        removed, which *is* the shipped definition. Nothing special-cases it.
        """
        role_id = str(self._read_body().get("id") or "").strip()
        roles = self.app.store.get("roles", {}) or {}
        roles.pop(role_id, None)
        # `update` deep-merges, so a popped key would survive it.
        conf = self.app.store.replace_roles(roles)
        self.app.bus.publish("config", config=conf)
        return {
            "ok": True,
            "roles": prompts.role_catalog(conf.get("roles", {})),
            "builtin": role_id in prompts.ROLE_TEMPLATES,
        }

    def _api_rollback(self, params: Dict[str, list]) -> Dict[str, Any]:
        return {"ok": True, "message": self.app.pipeline.rollback()}


class Server(ThreadingHTTPServer):
    """Threaded so an open SSE stream cannot block ordinary API calls."""

    daemon_threads = True
    allow_reuse_address = True
    # SSE holds a socket open indefinitely; the default request queue would
    # otherwise fill up with parked connections from multiple tabs.
    request_queue_size = 32


def _opt_int(value: Any) -> Optional[int]:
    """An optional integer from a JSON body: the number, or None if absent.

    Written out rather than done inline because the obvious one-liner
    (``int(v) if str(v or "").strip().isdigit() else None``) turns a deliberate
    **0** into None - ``0 or ""`` is ``""`` - and silently falls back to the
    saved default. For the innovation slider that is the difference between
    "build what I asked for and stop" and "invent three more rounds of work in
    my repository", so zero has to survive the trip.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def find_free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """Return ``preferred`` if bindable, otherwise an OS-assigned free port."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
            except OSError:
                continue
            return s.getsockname()[1]
    raise OSError("Could not bind to any port on the loopback interface.")


def make_server(
    store: Optional[cfg.ConfigStore] = None,
    port: Optional[int] = None,
    host: str = "127.0.0.1",
) -> Tuple[Server, AppState, str]:
    """Build a ready-to-serve instance. Returns ``(server, state, url)``."""
    store = store or cfg.ConfigStore()
    state = AppState(store)
    # `port is None` means "unset, use the configured default". Port 0 is a
    # real request for an OS-assigned ephemeral port, so `port or default`
    # would be wrong - it treats 0 as unset.
    preferred = port if port is not None else int(store.get("port", 8760))
    chosen = find_free_port(preferred, host)

    handler = type("BoundHandler", (Handler,), {"app": state})
    server = Server((host, chosen), handler)
    url = f"http://{host}:{chosen}/?ticket={urllib.parse.quote(state.ticket)}"
    return server, state, url


def serve_forever_in_thread(server: Server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    return thread
