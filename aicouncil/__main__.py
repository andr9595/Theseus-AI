"""Entry point: ``python3 -m aicouncil``.

Starts the local server and, unless told otherwise, opens the dashboard in a
dedicated browser window. Chromium-family browsers get ``--app=`` which strips
the tab strip and address bar so it reads as a desktop application; Firefox
gets a plain new window, since it removed ``-app`` support.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from typing import List, Optional

from . import APP_NAME, __version__
from . import config as cfg
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
    _    ___    ____                       _ _
   / \  |_ _|  / ___|___  _   _ _ __   ___(_) |
  / _ \  | |  | |   / _ \| | | | '_ \ / __| | |
 / ___ \ | |  | |__| (_) | |_| | | | | (__| | |
/_/   \_\___|  \____\___/ \__,_|_| |_|\___|_|_|
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
    print(f"  zero-touch  : {'ON' if store.get('zero_touch') else 'off'}")
    print(f"  target repo : {store.get('target_repo') or '(none selected)'}")
    print("\nProviders:")
    missing = []
    for key in ("drafter", "polisher"):
        provider = store.get("providers", {}).get(key)
        if not provider:
            continue
        info = probe(provider)
        mark = "OK  " if info["available"] else "MISS"
        version = f"  [{info['version']}]" if info["version"] else ""
        location = info["path"] or "not found on PATH"
        # The job comes first: either agent can be assigned to either job, so
        # which CLI is doing what is the part worth reading at a glance.
        print(
            f"  [{mark}] {provider.get('role', key):<14} {info['label']:<8} "
            f"{info['executable']:<10} -> {location}{version}"
        )
        if not info["available"]:
            missing.append(info["executable"])
    if missing:
        print(
            f"\n  {len(missing)} CLI(s) missing: {', '.join(missing)}."
            f"\n  Run scripts/install-deps.sh, or repoint the command in Settings."
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aicouncil",
        description=f"{APP_NAME} - a local Junior Draft / Senior Polish coding pipeline.",
    )
    parser.add_argument("--port", type=int, default=None, help="preferred port")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (loopback only)")
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

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"Refusing to bind to {args.host!r}. This app can execute a coding "
            f"agent with auto-approve flags and must stay on the loopback "
            f"interface.",
            file=sys.stderr,
        )
        return 2

    try:
        server, state, url = make_server(store, args.port, args.host)
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
