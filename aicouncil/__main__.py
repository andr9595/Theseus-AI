"""Entry point: ``python3 -m aicouncil``.

Starts the local server and, unless told otherwise, opens the dashboard in a
dedicated browser window. Chromium-family browsers get ``--app=`` which strips
the tab strip and address bar so it reads as a desktop application; Firefox
gets a plain new window, since it removed ``-app`` support.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from typing import List, Optional

from . import APP_NAME, __version__
from . import config as cfg
from . import gitutil
from .providers import probe
from .server import make_server

# Ordered by how close the result looks to a native window.
APP_MODE_BROWSERS = [
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge",
    "vivaldi",
]
WINDOW_BROWSERS = ["firefox", "librewolf"]

BANNER = r"""
 _____  _                                        _     ___
|_   _|| |__    ___  ___   ___  _   _  ___      / \   |_ _|
  | |  | '_ \  / _ \/ __| / _ \| | | |/ __|    / _ \   | |
  | |  | | | ||  __/\__ \|  __/| |_| |\__ \   / ___ \  | |
  |_|  |_| |_| \___||___/ \___| \__,_||___/  /_/   \_\|___|
"""


def _spawn(argv: List[str]) -> bool:
    """Launch a detached GUI process, swallowing its output."""
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except (OSError, ValueError):
        return False


def open_ui(url: str, prefer_app_window: bool = True) -> str:
    """Open ``url`` in the best available browser. Returns what was used."""
    if prefer_app_window:
        for name in APP_MODE_BROWSERS:
            path = shutil.which(name)
            if path and _spawn([path, f"--app={url}", "--new-window"]):
                return f"{name} (app window)"

        for name in WINDOW_BROWSERS:
            path = shutil.which(name)
            if path and _spawn([path, "--new-window", url]):
                return f"{name} (new window)"

    try:
        if webbrowser.open(url, new=1):
            return "default browser"
    except webbrowser.Error:
        pass
    return ""


def _print_doctor(store: cfg.ConfigStore) -> int:
    """Report on the environment. Exit status is 0 even when CLIs are absent -
    a missing CLI is a configuration state, not a crash."""
    print(f"{APP_NAME} v{__version__}")
    print(f"  python      : {sys.version.split()[0]} ({sys.executable})")
    print(f"  config      : {store.path}")
    print(f"  runs        : {cfg.runs_dir()}")
    print(f"  mode        : {store.get('mode', 'council')}")
    print(f"  zero-touch  : {'ON' if store.get('zero_touch') else 'off'}")
    workspace = store.get("workspace") or ""
    print(f"  workspace   : {workspace or f'{cfg.workspace_dir()} (scratch)'}")
    if store.get("pull_request_mode"):
        if store.get("zero_touch"):
            # Never actually reached: Zero-Touch ignores the toggle and pushes
            # directly instead. Worth saying here, since the setting still
            # reads as ON everywhere else in this printout.
            print("  pull request: ON, but ignored - Zero-Touch pushes directly")
        else:
            # PR mode's dependencies (a remote, `gh`, a git identity) are
            # exactly the kind of thing this command exists to find before a
            # run does.
            blocker = (
                gitutil.pull_request_blocker(workspace)
                if workspace
                else "the scratch workspace is not a git repository"
            )
            print(f"  pull request: ON - {blocker or 'ready'}")
    else:
        print("  pull request: off")
    print("\nProviders:")
    missing = []
    # Named for what each chair does rather than for its config key: "coder"
    # on its own says nothing about which of the three tabs it belongs to.
    # `drafter` and `polisher` are named for what they are now - entries kept
    # so archived transcripts render, dispatched on by nothing - because a
    # doctor line that reads like a live job invites someone to configure one.
    jobs = {
        "drafter": "(retired)",
        "polisher": "(retired)",
        "solo": "Chat assistant",
        "architect": "Project: arch",
        "coder": "Project: dev",
        "qa": "Project: QA",
    }
    for key in cfg.PROVIDER_ORDER:
        provider = store.get("providers", {}).get(key)
        if not provider:
            continue
        info = probe(provider)
        # Three marks rather than two. A CLI that is installed but not added is
        # not a problem to fix by installing something, and reporting it as
        # missing would send the operator looking for a binary that is there.
        if not cfg.provider_enabled(store.all(), provider):
            mark = "OFF "
        else:
            mark = "OK  " if info["available"] else "MISS"
        version = f"  [{info['version']}]" if info["version"] else ""
        location = info["path"] or "not found on PATH"
        # The job comes first: either agent can be assigned to either job, so
        # which CLI is doing what is the part worth reading at a glance.
        job = jobs.get(key) or key
        print(
            f"  [{mark}] {job:<14} {info['label']:<8} "
            f"{info['executable']:<10} -> {location}{version}"
        )
        if mark == "MISS":
            missing.append(info["executable"])
    added = cfg.selected_agents(store.all())
    print(
        f"\n  Agents added: {', '.join(added) if added else 'none yet'}."
        f"\n  Add, install or sign in to one in Settings -> Agents."
    )
    if missing:
        print(
            f"\n  {len(missing)} added CLI(s) missing: {', '.join(missing)}."
            f"\n  Install it from Settings -> Agents, run"
            f" scripts/install-deps.sh --agent <name>, or repoint the command."
        )
    return 0


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _bind_refusal(host: str, allow_lan: bool) -> Optional[str]:
    """Why ``host`` should not be bound, or None if it is fine to.

    A pure check so the container and the desktop path share one rule rather
    than one refusing on faith that the other agrees. This app can execute a
    coding agent with auto-approve flags, so leaving loopback is a real change
    in exposure - the flag is the operator saying that is intended, not a
    default anything can flip on for them.
    """
    if host in LOOPBACK_HOSTS:
        return None
    if not allow_lan:
        return (
            f"Refusing to bind to {host!r}. This app can execute a coding agent "
            f"with auto-approve flags and stays on the loopback interface "
            f"unless told otherwise. Pass --allow-lan (or set "
            f"AI_COUNCIL_ALLOW_LAN=1) to bind elsewhere - meant for a container "
            f"whose network Docker already isolates, not for a bare host."
        )
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aicouncil",
        description=f"{APP_NAME} - a local, deliberating multi-agent coding council.",
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ["AI_COUNCIL_PORT"]) if os.environ.get("AI_COUNCIL_PORT") else None,
        help="preferred port (env: AI_COUNCIL_PORT)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("AI_COUNCIL_HOST", "127.0.0.1"),
        help="bind address - loopback unless --allow-lan (env: AI_COUNCIL_HOST)",
    )
    parser.add_argument(
        "--allow-lan", action="store_true",
        default=_truthy_env("AI_COUNCIL_ALLOW_LAN"),
        help="permit a non-loopback --host (env: AI_COUNCIL_ALLOW_LAN=1)",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a window")
    parser.add_argument(
        "--print-url", action="store_true", help="print only the URL, then serve"
    )
    parser.add_argument("--doctor", action="store_true", help="check the environment and exit")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    store = cfg.ConfigStore()

    if args.doctor:
        return _print_doctor(store)

    refusal = _bind_refusal(args.host, args.allow_lan)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    # A token supplied this way survives a restart with the same URL, which is
    # what makes a container's WebUI link stable - see AppState in server.py
    # for what "persistent" changes about the ticket. Never a CLI flag: argv
    # is world-readable via `ps` for as long as the process runs.
    token = os.environ.get("AI_COUNCIL_TOKEN") or None

    try:
        server, state, url = make_server(store, args.port, args.host, token=token)
    except OSError as exc:
        print(f"Could not start the server: {exc}", file=sys.stderr)
        return 1

    if args.print_url:
        print(url, flush=True)
    else:
        print(BANNER)
        print(f"  {APP_NAME} v{__version__}")
        print(f"  Dashboard : {url}")
        print(f"  Config    : {store.path}")
        print(f"  Zero-Touch: {'ON' if store.get('zero_touch') else 'off'}")
        if args.host not in LOOPBACK_HOSTS:
            allowed = os.environ.get("AI_COUNCIL_ALLOWED_HOSTS", "")
            print(f"  Bound     : {args.host} (LAN-reachable)")
            print(
                f"  Reachable as: {allowed}" if allowed else
                "  AI_COUNCIL_ALLOWED_HOSTS is not set - only loopback names "
                "will be accepted even though the socket is open. Set it to "
                "the hostname or IP you will actually browse to."
            )
        print("\n  Press Ctrl+C to stop.\n")

    if not args.no_browser and store.get("open_browser", True):
        # Give the accept loop a beat before the browser races to connect.
        def launch() -> None:
            time.sleep(0.4)
            used = open_ui(url)
            if not used and not args.print_url:
                print("  Could not open a browser. Paste the URL above instead.\n")

        threading.Thread(target=launch, name="open-ui", daemon=True).start()

    # Poll once now and on the configured interval, so the first thing the
    # dashboard paints is a real number rather than a dash.
    state.usage.start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        state.usage.stop()
        state.pipeline.cancel()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
