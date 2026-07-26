"""Local HTTP + SSE server built on ``http.server``.

Security posture
----------------
The server binds to 127.0.0.1 only, but "localhost" is not a security boundary
on a multi-user or browser-hosting machine: any page you visit can issue
requests to http://127.0.0.1:8760, and any local process can too. Since this
app's API can execute a coding agent with ``--dangerously-skip-permissions``,
three defences are layered on:

1. **Session token.** Generated per launch, never persisted. Every ``/api/``
   request must present it. The launcher puts it in the URL fragment/query.
2. **Origin / Host validation.** Requests whose ``Origin`` is a real remote
   site are rejected, which blocks the drive-by CSRF case, and a non-loopback
   ``Host`` header is rejected, which blocks DNS rebinding.
3. **No shell.** See ``providers.py`` - argv lists, never a shell string.
"""

from __future__ import annotations

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
from . import gitutil
from .events import EventBus, drain, sse_comment, sse_format
from .pipeline import Pipeline, PipelineBusy
from . import prompts
from .providers import discover_efforts, discover_models, probe
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
        self.token = secrets.token_urlsafe(24)
        self.started_at = time.time()
        # Publishing on the bus means every open tab updates together, and a
        # tab opened later replays the last reading instead of showing blank.
        self.usage = UsagePoller(
            store, on_update=lambda snap: self.bus.publish("usage", usage=snap)
        )


class Handler(BaseHTTPRequestHandler):
    """Routes requests to the API or the static bundle."""

    server_version = f"AICouncil/{__version__}"
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
            ("POST", "/api/rollback"): self._api_rollback,
            ("POST", "/api/commit"): self._api_commit,
            ("GET", "/api/roles"): lambda p: {
                "ok": True,
                "roles": prompts.role_catalog(self.app.store.get("roles", {})),
            },
            ("POST", "/api/roles"): self._api_save_role,
            ("POST", "/api/roles/delete"): self._api_delete_role,
        }

        handler = routes.get((method, path))
        if handler is None:
            self._error(HTTPStatus.NOT_FOUND, f"No such endpoint: {path}")
            return

        try:
            result = handler(params)
        except PipelineBusy as exc:
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
        repo = state["config"].get("target_repo") or ""
        state["repo_status"] = gitutil.status(repo).to_dict() if repo else None
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
        return {"ok": True, "status": gitutil.status(path).to_dict()}

    def _api_history(self, params: Dict[str, list]) -> Dict[str, Any]:
        return {"ok": True, "runs": self.app.pipeline.history()}

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
        providers = self.app.store.get("providers", {})
        return {
            "ok": True,
            "version": __version__,
            "config_path": str(self.app.store.path),
            "runs_path": str(self.app.pipeline.runs_dir),
            "uptime": round(time.time() - self.app.started_at, 1),
            "providers": [
                probe(providers[k])
                for k in ("drafter", "polisher", "solo")
                if k in providers
            ],
        }

    def _api_models(self, params: Dict[str, list]) -> Dict[str, Any]:
        """What models the configured CLI reports it can actually run."""
        pid = (params.get("provider") or [""])[0]
        provider = self.app.store.get("providers", {}).get(pid)
        if not provider:
            raise ValueError(f"No such provider: {pid!r}")
        result = discover_models(provider)
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
        result = discover_efforts(provider)
        result["ok"] = True
        result["provider"] = pid
        result["current"] = provider.get("effort", "")
        # Named so the menu can say which model the levels belong to - they
        # differ between them, and a list with no model attached invites the
        # assumption that it is universal.
        result["model"] = provider.get("model", "")
        return result

    def _api_usage_refresh(self, params: Dict[str, list]) -> Dict[str, Any]:
        self.app.usage.poll_once()
        return {"ok": True, "usage": self.app.usage.snapshot()}

    def _api_start(self, params: Dict[str, list]) -> Dict[str, Any]:
        body = self._read_body()
        task = body.get("task") or ""
        repo = body.get("repo") or self.app.store.get("target_repo") or ""
        continue_from = body.get("continue_from") or ""
        compact_context = body.get("compact_context", False)
        if not isinstance(continue_from, str):
            raise ValueError("`continue_from` must be a transcript filename.")
        if not isinstance(compact_context, bool):
            raise ValueError("`compact_context` must be true or false.")
        if not repo:
            raise ValueError("No target repository selected.")
        run = self.app.pipeline.start(
            task,
            repo,
            continue_from=continue_from,
            compact_context=compact_context,
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

    def _api_commit(self, params: Dict[str, list]) -> Dict[str, Any]:
        """Commit the working tree of the selected repository."""
        body = self._read_body()
        repo = str(body.get("repo") or self.app.store.get("target_repo") or "")
        if not repo:
            raise ValueError("No target repository selected.")
        if self.app.pipeline.is_busy():
            # Committing underneath a running agent would capture a tree it is
            # still editing, and the resulting commit would match neither the
            # diff that was reviewed nor the one the run ends with.
            raise ValueError("A run is in progress. Wait for it to finish.")
        result = gitutil.commit_all(repo, str(body.get("message") or ""))
        self.app.bus.publish("committed", commit=result, repo=repo)
        return {"ok": True, "commit": result}

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
    url = f"http://{host}:{chosen}/?token={urllib.parse.quote(state.token)}"
    return server, state, url


def serve_forever_in_thread(server: Server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    return thread
