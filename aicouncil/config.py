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
* The *agent* (which CLI) and the *job* (Junior Draft / Senior Polish) are
  separate concerns. ``AGENTS`` holds the CLI-specific half of a provider and
  either agent can be assigned to either job; the defaults below are a
  starting pairing, not a rule.
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
from typing import Any, Dict, List

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
# Agents
# --------------------------------------------------------------------------

# The CLI-specific half of a provider: which binary to launch, which flag
# grants it permission to write, how it takes a model name, and how it is asked
# to narrate its work.
#
# These fields travel together and must never be mixed between agents: Claude's
# `--dangerously-skip-permissions` handed to `codex` is rejected outright, and
# the reverse is worse - the CLI starts, finds no permission grant, and blocks
# on a prompt nothing in this pipeline can answer.
AGENTS: Dict[str, Dict[str, Any]] = {
    # `codex exec` is the non-interactive entry point of the Codex CLI. The
    # prompt is a trailing argument; the CLI carries its own subscription auth.
    "codex": {
        "label": "Codex",
        "command": ["codex", "exec", "{prompt}"],
        "auto_approve_args": ["--dangerously-bypass-approvals-and-sandbox"],
        # argv fragment used to pass the model. `{model}` is substituted.
        "model_args": ["--model", "{model}"],
        # How hard the model is asked to think. Codex has no `--effort` flag:
        # reasoning depth is the `model_reasoning_effort` key in config.toml,
        # and `-c` overrides it for one run without editing that file. Which
        # levels are legal depends on the model, so the picker reads them from
        # the same cache the model list comes from.
        "effort_args": ["-c", "model_reasoning_effort={effort}"],
        # Empty on purpose: `codex exec` already narrates its work on stdout as
        # it goes. Present all the same, so assigning this agent to a job
        # clears the other's streaming flags instead of leaving them behind on
        # a binary that does not accept them.
        "stream_args": [],
        # The mirror image of `auto_approve_args`, appended only in Solo Mode.
        # A solo conversation has no approval gate, no snapshot and no diff, so
        # it is never granted write permission - and a CLI that discovers that
        # halfway through a task stalls on a prompt nothing here can answer.
        # Saying so up front turns that into an answer instead.
        "read_only_args": ["--sandbox", "read-only"],
    },
    "claude": {
        "label": "Claude",
        "command": ["claude", "-p", "{prompt}"],
        "auto_approve_args": ["--dangerously-skip-permissions"],
        "model_args": ["--model", "{model}"],
        # Claude Code takes the level as a first-class flag. Unlike Codex it
        # warns and falls back to its default on a value it does not know, so a
        # level that a future release drops degrades rather than failing a run.
        "effort_args": ["--effort", "{effort}"],
        # `claude -p` in its default text mode prints nothing at all until the
        # run is over, so the live stream sat empty for minutes and then filled
        # with the conclusion in one go. This asks for one JSON event per step
        # instead; `providers.py` translates them back into readable lines.
        # `--verbose` is not decoration - the CLI refuses stream-json without it.
        "stream_args": ["--output-format", "stream-json", "--verbose"],
        # Claude's read-only mode. It still reads the repository and still
        # answers; what it will not do is edit, which is the whole difference
        # between a conversation and a run.
        "read_only_args": ["--permission-mode", "plan"],
    },
}

# Reported for a command that matches no catalogued agent: a hand-written
# template, or the bundled mock agent.
CUSTOM_AGENT = "custom"


def agent_for(provider: Dict[str, Any]) -> str:
    """Which catalogued agent a provider runs, judged by its executable.

    Derived from the command rather than stored beside it. A stored field can
    disagree with the command after a hand edit in Settings, and that
    disagreement is invisible right up until a run hands one CLI's permission
    flag to the other CLI's binary.
    """
    command = provider.get("command") or []
    exe = os.path.basename(str(command[0])) if command else ""
    for name in AGENTS:
        if name in exe:
            return name
    return CUSTOM_AGENT


def agent_catalog() -> List[Dict[str, Any]]:
    """The selectable agents, for the Settings dropdown.

    Served to the UI so the browser never carries its own copy of a command
    or a permission flag - one source of truth, in Python.
    """
    catalog = [dict(copy.deepcopy(preset), id=name) for name, preset in AGENTS.items()]
    catalog.append({
        "id": CUSTOM_AGENT,
        "label": "Custom command",
        "command": [],
        "auto_approve_args": [],
        "model_args": [],
        "effort_args": [],
        "stream_args": [],
        "read_only_args": [],
    })
    return catalog


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

# Stage 1 - the "junior". Cheap, fast, generous quota. Drafts the solution.
DEFAULT_DRAFTER = {
    "id": "drafter",
    # --- Role -------------------------------------------------------------
    # Which behaviour this stage performs. The catalogue lives in prompts.py;
    # `role_system` overrides it with edited text, and blank means "use the
    # template", so clearing the box in Settings restores the default.
    "role_template": "junior_draft",
    "role_system": "",
    "role": "Junior Draft",
    "enabled": True,
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
    # --- Reasoning effort ---------------------------------------------------
    # How hard the model is asked to think, the same knob `/effort` sets in
    # Claude Code and the reasoning selector sets in Codex. Empty means the
    # CLI's own default, which is what the vendor tuned for that model. Not
    # enumerated here for the same reason `models` is not: the legal levels
    # are per-model and the picker reads them at open time.
    "effort": "",
    # --- Agent -------------------------------------------------------------
    # Codex drafts by default because its quota is the generous one. Reassign
    # it in Settings; nothing else about this stage has to change.
    **copy.deepcopy(AGENTS["codex"]),
}

# Stage 2 - the "senior". Expensive, rationed. Reviews and applies.
DEFAULT_POLISHER = {
    "id": "polisher",
    # --- Role -------------------------------------------------------------
    # Which behaviour this stage performs. The catalogue lives in prompts.py;
    # `role_system` overrides it with edited text, and blank means "use the
    # template", so clearing the box in Settings restores the default.
    "role_template": "senior_polish",
    "role_system": "",
    "role": "Senior Polish",
    "enabled": True,
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
    # --- Reasoning effort ---------------------------------------------------
    # Empty for the same reason as the drafter: the CLI's default is the tuned
    # one, and the legal levels depend on the model in use.
    "effort": "",
    # --- Agent -------------------------------------------------------------
    **copy.deepcopy(AGENTS["claude"]),
}

# Solo Mode's single assistant. Not a third council stage: it has no draft to
# receive, nothing to hand on to, and therefore no role in the pipeline sense.
DEFAULT_SOLO = {
    "id": "solo",
    # --- Behaviour ----------------------------------------------------------
    # Free text put in front of the message, and the only instruction this
    # assistant ever carries. Empty by default and left empty until the
    # operator writes something: opening a plain conversation should not
    # quietly enrol the agent in a persona nobody asked for. That is the
    # difference between this and a stage's `role_system`, where blank means
    # "fall back to the template".
    "behavior": "",
    "prompt_on_stdin": False,
    "timeout_seconds": 1800,
    "cwd_mode": "repo",
    # --- Model selection ----------------------------------------------------
    "model": "",
    "models": [],
    # --- Reasoning effort ---------------------------------------------------
    "effort": "",
    # --- Agent --------------------------------------------------------------
    **copy.deepcopy(AGENTS["claude"]),
}

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    # --- Mode ---------------------------------------------------------------
    # Which of the two products the next message starts. Council is the
    # Junior Draft / Senior Polish pipeline; Solo is one assistant answering
    # directly. Not a modifier on the pipeline - Solo has no draft, no gate,
    # no delivery and no diff, and the two share only the target repository
    # and the conversation store.
    "mode": "council",
    # --- Pipeline behaviour (Council only) ----------------------------------
    # Zero-Touch: run start-to-finish with no human gate, passing the CLIs'
    # auto-approve flags. OFF by default - opting in to autonomous file
    # modification should always be a deliberate act.
    "zero_touch": False,
    # Take a git snapshot ref before Stage 2 writes anything, so any run can be
    # rolled back with one click even in Zero-Touch Mode.
    "safety_snapshot": True,
    # Refuse to run against a repo with uncommitted changes unless overridden.
    "require_clean_worktree": False,
    # Deliver the run on a branch of its own and open a GitHub pull request,
    # instead of leaving the changes uncommitted on the checked-out branch. The
    # base branch is then only ever changed by a human merging that PR. Implies
    # a clean starting tree regardless of the toggle above - see gitutil.
    "pull_request_mode": False,
    # --- Target -------------------------------------------------------------
    "target_repo": "",
    "recent_repos": [],
    # --- Providers ----------------------------------------------------------
    "providers": {
        "drafter": DEFAULT_DRAFTER,
        "polisher": DEFAULT_POLISHER,
        "solo": DEFAULT_SOLO,
    },
    # --- Prompting ----------------------------------------------------------
    # Extra standing instructions appended to both council stages. Solo does
    # not get them: it is a conversation, not a run against this project.
    "house_rules": "",
    # What the context meter's percentage is measured against. A figure to edit
    # rather than one to trust: no CLI reports the window its model was given,
    # and the aliases in Settings resolve to whatever the vendor ships today.
    # 200k is the common case for both Claude and Codex models. Set it to 0 to
    # show the token estimate on its own with no percentage.
    "context_window_tokens": 200_000,
    # --- Roles ------------------------------------------------------------
    # Edits to the built-in roles and any roles the operator added, keyed by
    # id. Built-ins are merged over their shipped definition rather than
    # copied into here wholesale, so an untouched role keeps tracking the
    # shipped text when this app updates, and "reset" is a delete.
    "roles": {},

    # --- Quota ---------------------------------------------------------------
    # Poll the CLI's own /usage on launch and on this interval. Claude answers
    # locally, so this costs no tokens; agents that cannot report say so.
    "usage_polling": True,
    "usage_poll_seconds": 300,
    # Warn before starting a run once the worst reported limit is at or above
    # this. A warning only - the operator can always force the run through,
    # because the reading is a snapshot and only they know what the work is
    # worth.
    "usage_warn_percent": 85,

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


def _migrate(merged: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    """Carry a config written before ``mode`` existed forward.

    Solo used to be a toggle that skipped Stage 1 and borrowed one council
    stage's configuration. What is worth keeping out of that is the operator's
    choice of CLI, model and reasoning level, so the stage they pointed it at
    is copied onto the new ``solo`` provider - but deliberately not its council
    role, which is precisely the part Solo no longer has.

    ``raw`` is the file as written rather than the merged result: after
    ``_deep_merge`` every new default is present, and a missing legacy key
    would be indistinguishable from one the operator set to the same value.
    """
    if "mode" not in raw:
        merged["mode"] = "solo" if raw.get("solo_mode") else "council"
        source = (merged.get("providers") or {}).get(str(raw.get("solo_stage") or ""))
        if raw.get("solo_mode") and isinstance(source, dict):
            carried = copy.deepcopy(source)
            for key in ("id", "role", "role_template", "role_system", "enabled"):
                carried.pop(key, None)
            merged["providers"]["solo"] = _deep_merge(DEFAULT_SOLO, carried)

    # Dropped rather than left to rot: a stale key that no longer decides
    # anything is worse than an absent one, because it still reads as a setting.
    merged.pop("solo_mode", None)
    merged.pop("solo_stage", None)
    if merged.get("mode") not in ("council", "solo"):
        merged["mode"] = "council"
    return merged


def _resolve_agent_choices(
    current: Dict[str, Any], patch: Dict[str, Any]
) -> Dict[str, Any]:
    """Expand any ``providers.<job>.agent`` key into the real provider fields.

    The UI sends an agent id when the operator picks one from the dropdown.
    Expanding it here rather than in the browser keeps every command and
    permission flag in one place, and makes the swap atomic: a stale command
    submitted by the same form cannot re-pair one CLI's binary with the
    other's auto-approve flag.

    ``agent`` is never persisted - it is derived from the command by
    ``agent_for()``, so there is nothing to keep in sync.
    """
    if not isinstance(patch.get("providers"), dict):
        return patch
    out = copy.deepcopy(patch)
    for pid, changes in out["providers"].items():
        if not isinstance(changes, dict):
            continue
        chosen = changes.pop("agent", None)
        preset = AGENTS.get(str(chosen or ""))
        if preset is None:
            continue
        if chosen == agent_for((current.get("providers") or {}).get(pid) or {}):
            continue  # unchanged: whatever else this patch carries wins
        changes.update(copy.deepcopy(preset))
        # Model names are not interchangeable between CLIs - a Codex slug
        # handed to `claude --model` fails at launch - so the swap clears them.
        changes["model"] = ""
        changes["models"] = []
        # Nor are reasoning levels: `ultra` exists only on some Codex models,
        # and Claude's set is its own. Carrying one over would either be
        # rejected outright or silently ignored, and silently ignored is worse
        # - the chip would keep claiming a depth the run never used.
        changes["effort"] = ""
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
        return _migrate(_deep_merge(DEFAULTS, raw), raw)

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

        A ``providers.<job>.agent`` key is resolved against that same freshly
        read copy, so assigning an agent swaps its command and permission
        flags as one unit.
        """
        with self._lock:
            current = self._load()
            self._data = _deep_merge(current, _resolve_agent_choices(current, patch))
            self._write()
            return copy.deepcopy(self._data)

    def reload(self) -> Dict[str, Any]:
        """Discard the in-memory copy and re-read from disk."""
        with self._lock:
            self._data = self._load()
            return copy.deepcopy(self._data)

    def replace_roles(self, roles: Dict[str, Any]) -> Dict[str, Any]:
        """Set the roles map wholesale. ``update`` deep-merges, so a deleted
        role would come straight back; deletion needs replacement."""
        with self._lock:
            self._data = _deep_merge(self._load(), {})
            self._data["roles"] = copy.deepcopy(roles)
            self._write()
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
