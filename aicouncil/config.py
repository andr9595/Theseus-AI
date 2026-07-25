"""Persistent configuration for AI Council.

Config lives at ``~/.config/ai-council/config.json`` (XDG-aware). A repo-local
``config.local.json`` overrides it when present, which makes development and
testing easy without touching the user's real settings.

Design notes
------------
* Every provider is described by a *command template*: an argv list where the
  token ``{prompt}`` is substituted with the rendered prompt. This keeps the
  app agnostic to CLI flag churn - if the `claude` or `codex` CLI changes its
  interface, the user edits the template in Settings instead of the source.
* ``auto_approve_args`` are appended only when Zero-Touch Mode is enabled, so
  the dangerous flags are impossible to pass by accident.
* Nothing here ever holds an API key. The CLIs carry their own subscription
  auth; we only shell out to them.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

PKG_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PKG_ROOT.parent


def config_dir() -> Path:
    """Return the XDG config directory for the app, creating it if needed."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    d = Path(base) / "ai-council"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    """Active config file. A repo-local override wins when it exists."""
    override = os.environ.get("AI_COUNCIL_CONFIG")
    if override:
        return Path(override).expanduser()
    local = REPO_ROOT / "config.local.json"
    if local.exists():
        return local
    return config_dir() / "config.json"


def runs_dir() -> Path:
    """Directory holding the JSON transcript of every pipeline run."""
    d = config_dir() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

# Stage 1 - the "junior". Cheap, fast, generous quota. Drafts the solution.
#
# `codex exec` is the non-interactive entry point of the Codex CLI. We pass the
# prompt as a trailing argument and rely on the CLI's own subscription auth.
DEFAULT_DRAFTER = {
    "id": "drafter",
    "label": "Codex",
    "role": "Junior Draft",
    "enabled": True,
    "command": ["codex", "exec", "{prompt}"],
    # Appended only in Zero-Touch Mode.
    "auto_approve_args": ["--dangerously-bypass-approvals-and-sandbox"],
    # When true the prompt is piped on stdin and `{prompt}` is dropped from
    # argv. Useful for very long prompts that would blow the ARG_MAX limit.
    "prompt_on_stdin": False,
    "timeout_seconds": 900,
    "cwd_mode": "repo",  # run inside the selected target repository
    # --- Model selection ---------------------------------------------------
    # Empty means "whatever the CLI is configured to use by default", which is
    # the safest choice: it keeps working when a vendor ships a new model.
    "model": "",
    # Deliberately empty. Codex publishes an account-scoped model list in
    # $CODEX_HOME/models_cache.json, and the picker reads it - a shipped list
    # would be both stale and wrong for accounts with different entitlements
    # (a ChatGPT-account login cannot run every model an API key can).
    "models": [],
    # argv fragment used to pass the model. `{model}` is substituted.
    "model_args": ["--model", "{model}"],
}

# Stage 2 - the "senior". Expensive, rationed. Reviews and applies.
DEFAULT_POLISHER = {
    "id": "polisher",
    "label": "Claude",
    "role": "Senior Polish",
    "enabled": True,
    "command": ["claude", "-p", "{prompt}"],
    "auto_approve_args": ["--dangerously-skip-permissions"],
    "prompt_on_stdin": False,
    "timeout_seconds": 1800,
    "cwd_mode": "repo",
    # --- Model selection ---------------------------------------------------
    "model": "",
    # Empty for the same reason as the drafter: the picker asks the CLI. For
    # Claude that means the documented `--model` aliases, which always resolve
    # to the current model in each family. Pinned IDs are not enumerated -
    # this app cannot know which ones an account may use, and a stale pinned
    # ID is exactly the failure being avoided. Type one to use it anyway.
    "models": [],
    "model_args": ["--model", "{model}"],
}

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    # --- Pipeline behaviour -------------------------------------------------
    # Zero-Touch: run start-to-finish with no human gate, passing the CLIs'
    # auto-approve flags. OFF by default - opting in to autonomous file
    # modification should always be a deliberate act.
    "zero_touch": False,
    # Take a git snapshot ref before Stage 2 writes anything, so any run can be
    # rolled back with one click even in Zero-Touch Mode.
    "safety_snapshot": True,
    # Refuse to run against a repo with uncommitted changes unless overridden.
    "require_clean_worktree": False,
    # Skip Stage 1 and send the task straight to Claude.
    "solo_mode": False,
    # --- Target -------------------------------------------------------------
    "target_repo": "",
    "recent_repos": [],
    # --- Providers ----------------------------------------------------------
    "providers": {
        "drafter": DEFAULT_DRAFTER,
        "polisher": DEFAULT_POLISHER,
    },
    # --- Prompting ----------------------------------------------------------
    # Extra standing instructions appended to both stages.
    "house_rules": "",
    # --- Server -------------------------------------------------------------
    "port": 8760,
    "open_browser": True,
    "theme": "midnight",
}


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``overlay`` onto a copy of ``base``.

    Dict values merge key-by-key; every other type is replaced wholesale. This
    lets a config file written by an older version pick up newly added default
    keys instead of silently missing them.
    """
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class ConfigStore:
    """Thread-safe read/modify/write wrapper around the JSON config file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else config_path()
        self._lock = threading.RLock()
        self._data = self._load()

    # -- io ----------------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        if not self._path.exists():
            return copy.deepcopy(DEFAULTS)
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt config must never brick the app. Fall back to defaults
            # and preserve the bad file for inspection.
            try:
                self._path.rename(self._path.with_suffix(".json.corrupt"))
            except OSError:
                pass
            return copy.deepcopy(DEFAULTS)
        if not isinstance(raw, dict):
            return copy.deepcopy(DEFAULTS)
        return _deep_merge(DEFAULTS, raw)

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._path)  # atomic on POSIX

    # -- api ---------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def all(self) -> Dict[str, Any]:
        """Return a deep copy of the whole config."""
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge ``patch`` into the config, persist, return the result.

        Re-reads from disk first. Without that, two running instances each hold
        their own in-memory copy and every write ships the whole file, so the
        last saver silently reverts the other's settings to whatever it loaded
        at startup. That is bad for a port number and genuinely unsafe for
        ``zero_touch`` and ``safety_snapshot`` - a second window could turn the
        approval gate off, or rollback protection off, without anyone touching
        that toggle. Re-reading narrows last-write-wins from the whole file to
        the individual keys actually being changed.
        """
        with self._lock:
            self._data = _deep_merge(self._load(), patch)
            self._write()
            return copy.deepcopy(self._data)

    def reload(self) -> Dict[str, Any]:
        """Discard the in-memory copy and re-read from disk."""
        with self._lock:
            self._data = self._load()
            return copy.deepcopy(self._data)

    def reset(self) -> Dict[str, Any]:
        with self._lock:
            self._data = copy.deepcopy(DEFAULTS)
            self._write()
            return copy.deepcopy(self._data)

    def remember_repo(self, repo: str, limit: int = 8) -> None:
        """Push ``repo`` to the front of the most-recently-used list."""
        if not repo:
            return
        with self._lock:
            recent = [r for r in self._data.get("recent_repos", []) if r != repo]
            recent.insert(0, repo)
            self._data["recent_repos"] = recent[:limit]
            self._data["target_repo"] = repo
            self._write()
