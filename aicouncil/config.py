"""Persistent configuration for Theseus AI.

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
* ``incognito_args`` are appended only when Incognito is on, and an agent that
  declares none is not run incognito at all - see ``providers.build_argv``.
* Nothing here ever holds an API key. The CLIs carry their own subscription
  auth; we only shell out to them - see ``AGENT_SETUP`` for the install and
  sign-in commands Settings offers, all of which hand the credential to the
  vendor's own CLI and none of which pass through this app.
* No agent is required and none is preferred. ``agent_settings.<cli>.selected``
  is the operator's answer to "use this one", it starts false, and it is what
  decides whether a CLI is seated - not whether its binary happens to exist.
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

# Owner-only, stated rather than inherited. What lands under these paths is the
# task, the model's output and the full diff of a private repository, plus the
# command templates a run executes - under the usual 022 umask every one of
# those would be world-readable on a shared machine.
DIR_MODE = 0o700
FILE_MODE = 0o600


def private_dir(path: Path) -> Path:
    """Create ``path`` owner-only, tightening it if it already exists.

    The tighten pass is for directories an earlier version created with the
    ambient umask: ``mkdir(mode=...)`` only applies to a directory it actually
    creates, so without it an upgrade would leave the old mode in place
    forever.
    """
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(DIR_MODE)
    except OSError:
        # Filesystems without POSIX modes (Windows, some network mounts) are a
        # supported place to keep a config; they are simply not protected here.
        pass
    return path


def write_private_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as an owner-only file.

    ``Path.write_text`` creates at 0666 & ~umask - typically 0644 - and leaves
    an existing file's mode alone, so both halves are stated here.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


def config_dir() -> Path:
    """Return the XDG config directory for the app, creating it if needed."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return private_dir(Path(base) / "ai-council")


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
    return private_dir(config_dir() / "runs")


def workspace_dir() -> Path:
    """The folder a run executes in when no working folder has been chosen.

    A directory of the app's own rather than ``$HOME``: an agent asked to write
    something with nowhere in particular to put it has to put it *somewhere*,
    and a named, contained folder is one the operator can find afterwards. It
    is deliberately not a git repository - there is nothing here to snapshot,
    and the UI says so rather than offering a rollback that cannot work.
    """
    return private_dir(config_dir() / "workspace")


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
        # Incognito's half of the bargain on the CLI's side: `--ephemeral` runs
        # without persisting session files to disk, so the turn leaves nothing
        # in `~/.codex` for `codex resume` to find. Read off the installed
        # binary's own help rather than assumed - a flag a CLI does not know
        # fails the run instead of protecting it.
        "incognito_args": ["--ephemeral"],
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
        # Claude's own no-save flag. Its help says it only works with
        # `--print`, which the command above already is - the same reason
        # `stream_args` can ask for stream-json here and nowhere else.
        "incognito_args": ["--no-session-persistence"],
    },
    # Google's Antigravity CLI, which replaced Gemini CLI for personal accounts
    # on 18 June 2026. Its binary is `agy`, and the key has to stay a substring
    # of that: `agent_for` identifies an agent by looking for its name inside
    # the executable's, so that a hand-edited absolute path still resolves.
    "agy": {
        "label": "Antigravity",
        # The prompt is the *value* of `--print`, not a positional argument -
        # the one structural difference from the other two. A bare `{prompt}`
        # token would let `build_argv` splice the model and permission flags in
        # ahead of it, directly between `--print` and its own value, and
        # `--print` would take `--model` as the thing to answer. Attaching the
        # placeholder to the flag removes the gap entirely.
        #
        # `--print-timeout` is pinned well past every timeout in this app
        # because `agy` defaults to five minutes and gives up on its own: a
        # council stage allowed thirty would be cut off at five with no
        # explanation. Set high, the app's own timeout is always the one that
        # decides.
        "command": ["agy", "--print-timeout", "60m", "--prompt={prompt}"],
        "auto_approve_args": ["--dangerously-skip-permissions"],
        "model_args": ["--model", "{model}"],
        # low | medium | high, per the CLI's own validation message. Narrower
        # than Claude's five levels, and it refuses a value it does not know
        # rather than warning and carrying on - so the picker asks rather than
        # offering a list this file would have to keep in step.
        "effort_args": ["--effort", "{effort}"],
        # No streaming format to ask for: `agy` has no `--output-format`, so
        # its print mode is read as the plain text it is. Present and empty for
        # the same reason as Codex's - assigning this agent to a job must clear
        # the other's streaming flags rather than leave them on a binary that
        # would reject them.
        "stream_args": [],
        # `--mode plan` is Antigravity's read-only mode: it reads the
        # repository and answers, and will not edit.
        #
        # The second flag looks like the opposite of read-only and is not.
        # `--mode plan` is what withholds the write; the permission prompt is a
        # separate thing, and in `--print` mode `agy` cannot ask it - it
        # auto-*denies* instead. So a plan-mode run without this flag has every
        # `read_file` denied, produces no output at all, and still exits 0:
        #
        #   jetski: no output produced - a tool required the "read_file"
        #   permission that headless mode cannot prompt for, so it was
        #   auto-denied.
        #
        # which is exactly what made every Antigravity seat fail its position
        # stage. Verified against agy 1.1.10: with both flags a write, whether
        # by edit tool or by a shell redirect, is still refused by plan mode.
        "read_only_args": ["--mode", "plan", "--dangerously-skip-permissions"],
        # Empty, and not an omission. `agy --help` on 1.1.10 offers
        # `--continue` and `--conversation` to *resume* a saved conversation
        # and nothing at all to stop it being saved, so there is no flag here
        # that would make the promise true. An agent with no incognito
        # arguments is left off an incognito run rather than run and quietly
        # recorded - see `providers.supports_incognito`.
        "incognito_args": [],
    },
}

# Reported for a command that matches no catalogued agent: a hand-written
# template, or the bundled mock agent.
CUSTOM_AGENT = "custom"

# How each catalogued CLI is installed and signed in, so Settings can offer
# both without the operator leaving the app.
#
# Deliberately *not* part of `AGENTS`. Those entries are copied wholesale onto
# every provider record (see `_council_seat`), which means they are saved to
# config.json, editable by hand in the Command line disclosure, and spliced
# into argv by `build_argv`. A login command sitting there is one hand edit
# away from being launched with a prompt appended to it. Here it is reachable
# only by agent id, and every argv is fixed at import time - the setup
# endpoints take an id and an action and never an executable.
#
# `status_command` must be non-interactive and must not open a browser: it runs
# on an ordinary state refresh. `login_command` is the opposite - it is
# expected to want a terminal, and `login_tui` says it wants a *whole* one, in
# which case Settings hands the command over to be pasted rather than pretends
# a scrollback pane is a terminal emulator.
AGENT_SETUP: Dict[str, Dict[str, Any]] = {
    "codex": {
        # Prints a URL and waits on the browser callback, so it reads fine as
        # streamed lines.
        "login_command": ["codex", "login"],
        "login_tui": False,
        "status_command": ["codex", "login", "status"],
        "docs_url": "https://chatgpt.com/codex",
        "account": "a ChatGPT Plus, Pro or Business subscription",
    },
    "claude": {
        # `--claudeai` is the subscription flow and is not a default worth
        # leaving to the CLI: the alternative it offers is `--console`, which
        # is API-key billing, and picking that by accident is the one outcome
        # this app exists to avoid.
        "login_command": ["claude", "auth", "login", "--claudeai"],
        "login_tui": False,
        # Answers with a JSON object carrying `loggedIn`, so the reply is read
        # rather than pattern-matched.
        "status_command": ["claude", "auth", "status"],
        "docs_url": "https://claude.ai/download",
        "account": "a Claude Pro or Max subscription",
    },
    "agy": {
        # Antigravity 1.1.12 has no auth subcommand at all: signing in happens
        # inside the interactive session, which is a full-screen TUI. Said so
        # here rather than discovered by an operator watching a pane fill with
        # escape sequences.
        "login_command": ["agy"],
        "login_tui": True,
        "status_command": [],
        "docs_url": "https://antigravity.google/cli",
        "account": "a Google account",
    },
}


def install_command(agent: str) -> List[str]:
    """The argv that installs one CLI, or an empty list for an unknown agent.

    Routed through the bundled script rather than naming a vendor URL here:
    those URLs are piped to `bash`, and one file that names them is one file to
    read before trusting them. The script is also what a terminal user runs, so
    the button and the documented command cannot drift apart.
    """
    if agent not in AGENT_SETUP:
        return []
    return [str(REPO_ROOT / "scripts" / "install-deps.sh"), "--agent", agent]


# How the GitHub connection is made, checked and broken. Catalogued here for
# the same reason `AGENT_SETUP` is: the endpoints take an action name and never
# an argv, so nothing the browser sends can become a command.
#
# The important part of this design is what is *absent*. There is no config key
# for a token and no file this app writes one to. A token pasted into Settings
# is piped to `gh auth login --with-token` on stdin and then dropped; from that
# point `gh` owns the credential and this app can only ask `gh` about it. That
# is what lets the agent CLIs use GitHub too - they shell out to the same `gh`
# and inherit the same login - without a secret ever appearing in an argv, an
# environment variable or a config file belonging to this app.
#
# Honesty about where it does land: `gh` stores the token in its own config
# (`~/.config/gh/hosts.yml`, mode 0600) unless a system keyring is available,
# in which case it uses that. Neither is this app's to promise, so the UI says
# which one happened rather than claiming the token is encrypted.
GITHUB_SETUP: Dict[str, Any] = {
    # Non-interactive and safe to run on an ordinary refresh.
    "status_command": ["gh", "auth", "status", "--hostname", "github.com"],
    # `--with-token` reads stdin. The token never becomes an argument, because
    # arguments are world-readable in `ps` for as long as the process lives.
    "login_command": ["gh", "auth", "login", "--hostname", "github.com", "--with-token"],
    # Teaches git to authenticate as `gh` over HTTPS, so a push from a run - or
    # from an agent working in the folder - uses the same login.
    "setup_git_command": ["gh", "auth", "setup-git"],
    "logout_command": ["gh", "auth", "logout", "--hostname", "github.com"],
    "docs_url": "https://github.com/settings/tokens",
    # What a token has to carry for pull-request mode to work end to end.
    # `repo` alone is enough to push and open a PR; `workflow` is only needed if
    # a run touches `.github/workflows`, and is requested because a run that
    # edits CI and then cannot push is a confusing way to find out.
    "scopes": ("repo", "workflow", "read:org"),
}


def github_install_command() -> List[str]:
    """The argv that installs the GitHub CLI, rootless, into ``~/.local/bin``.

    Same reasoning as `install_command`: one script names the download so there
    is one file to read before trusting it, and the button cannot drift away
    from the documented command.
    """
    return [str(REPO_ROOT / "scripts" / "install-deps.sh"), "--gh"]


# The council seats are configured per *CLI* rather than per chair, because
# which chair a CLI takes is decided per run by the router - there is no
# standing "seat 2" to configure. What an operator sets here is how Codex
# behaves whenever Codex is seated, which is the thing that stays true across
# runs.
def council_provider_id(agent: str) -> str:
    """The provider id holding one CLI's council configuration."""
    return f"council_{agent}"


COUNCIL_PROVIDERS = tuple(council_provider_id(a) for a in AGENTS)

# The order every list of chairs is presented in: the council's per-CLI seats,
# the legacy two-stage pair, the chat assistant, then Projects Mode's three
# roles. Here rather than repeated at each call site, so adding another cannot
# leave one screen showing the rest.
#
# `drafter` and `polisher` are kept after the rewrite of Council into a
# deliberating bench. They are no longer dispatched on, but transcripts written
# before it name them, and dropping them from the order would render an
# archived run with two unlabelled stages.
PROVIDER_ORDER = (
    *COUNCIL_PROVIDERS,
    "drafter",
    "polisher",
    "solo",
    "architect",
    "coder",
    "qa",
)


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
    catalog = []
    for name, preset in AGENTS.items():
        setup = AGENT_SETUP.get(name) or {}
        catalog.append(dict(
            copy.deepcopy(preset),
            id=name,
            # What the connection card needs to describe this CLI, and no argv:
            # the browser never sends a command back, so it has no reason to
            # hold one. It asks for an agent id and an action instead.
            setup={
                "login_tui": bool(setup.get("login_tui")),
                "can_check": bool(setup.get("status_command")),
                "docs_url": str(setup.get("docs_url") or ""),
                "account": str(setup.get("account") or ""),
                "login_hint": " ".join(setup.get("login_command") or []),
                "install_hint": " ".join(install_command(name)),
            },
        ))
    catalog.append({
        "id": CUSTOM_AGENT,
        "label": "Custom command",
        "command": [],
        "auto_approve_args": [],
        "model_args": [],
        "effort_args": [],
        "stream_args": [],
        "read_only_args": [],
        # Blank like the rest, and load-bearing: a hand-written command is
        # nobody's catalogued agent, so this app cannot know whether it saves
        # its conversations. It is left off incognito runs until the operator
        # fills this in with whatever their wrapper takes.
        "incognito_args": [],
    })
    return catalog


# --------------------------------------------------------------------------
# Per-agent settings
# --------------------------------------------------------------------------
#
# Which model a CLI runs, and how hard it is asked to think, belong to the CLI
# and not to the place it happens to be sitting. Antigravity is one login with
# one catalogue: choosing `gemini-3.6-flash` for it on the Projects tab and
# finding Council still on the old one is a setting that did not take, not a
# per-tab preference anybody asked for.
#
# So they live here, once per agent, and are *projected* onto every provider
# that runs that agent on load and after every save. The copies on the provider
# records are derived - everything that reads a model reads it off a provider
# (`build_argv`, the project engine's frozen config, the UI's chips), and one
# place to write beats rewriting all of them. Editing a provider's model by
# hand in config.json is therefore overwritten at the next load; edit
# ``agent_settings`` instead, which is what the pickers write.
#
# A provider running a hand-written command is nobody's agent and keeps its own.
AGENT_SETTING_KEYS = ("model", "models", "effort")

# `selected` sits beside them and is not one of them: the three above are
# lifted *off* a provider the pickers patched, and this one is never on a
# provider in the first place. What it produces there is `enabled`, written by
# `_apply_agent_settings` in the same sweep.
#
# False by default, for every agent, and that is the design rather than a
# cautious default. This app has no opinion about which vendor an operator
# should use and no way to know which they pay for, so a fresh install seats
# nobody until asked. A CLI found on PATH is evidence of nothing except that it
# is installed - plenty of machines carry all three and a subscription for one.
AGENT_SELECTED_KEY = "selected"


def _default_agent_settings() -> Dict[str, Any]:
    return {
        agent: {"selected": False, "model": "", "models": [], "effort": ""}
        for agent in AGENTS
    }


def selected_agents(conf: Dict[str, Any]) -> List[str]:
    """The catalogued CLIs the operator has added, in catalogue order."""
    settings = conf.get("agent_settings") or {}
    out: List[str] = []
    for agent in AGENTS:
        entry = settings.get(agent)
        if isinstance(entry, dict) and entry.get(AGENT_SELECTED_KEY):
            out.append(agent)
    return out


def provider_enabled(conf: Dict[str, Any], provider: Dict[str, Any]) -> bool:
    """Whether a chair may run at all - the one question every caller asks.

    Read here rather than projected onto the provider records the way the model
    and the reasoning level are. Those are settings of a CLI that a chair
    displays; this is a *fact about two things at once*, and writing it down
    would put a copy of the answer on a record whose command can change under
    it. A chair repointed at a hand-written command would keep the derived
    "off" of whichever CLI used to hold it, and read back as a wrapper the
    operator had disabled - which nobody did.

    So: a catalogued CLI runs where it was added in Settings, and a
    hand-written command answers to nobody's selection and keeps its own flag.
    The second half is what lets the bundled mock agent exercise the whole
    pipeline with no vendor CLI added at all.
    """
    if not provider.get("enabled", True):
        return False
    agent = agent_for(provider)
    return agent not in AGENTS or agent in selected_agents(conf)


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

# The retired Stage 1 - the "junior" that drafted for the two-stage council.
#
# Nothing dispatches on it since Council became a deliberating bench. It
# survives for the reason given at PROVIDER_ORDER: transcripts written before
# the rewrite name this stage, and it must still have a provider to render
# against. What it no longer carries is a role. `role_template`, `role_system`
# and the `role` label all described a behaviour resolved per stage, by a
# `prompts.resolve_system` that no longer exists; a seat's behaviour now comes
# from the persona the router assigns it. Kept, they would read as settings
# that decide something, which none of them has since the rewrite.
DEFAULT_DRAFTER = {
    "id": "drafter",
    "enabled": True,
    # When true the prompt is piped on stdin and `{prompt}` is dropped from
    # argv. Useful for very long prompts that would blow the ARG_MAX limit.
    "prompt_on_stdin": False,
    "timeout_seconds": 900,
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
    # Codex drafted by default because its quota was the generous one.
    **copy.deepcopy(AGENTS["codex"]),
}

# The retired Stage 2 - the "senior" that reviewed and applied the draft. Kept
# for the same reason as the drafter above, and roleless for the same reason.
DEFAULT_POLISHER = {
    "id": "polisher",
    "enabled": True,
    "prompt_on_stdin": False,
    "timeout_seconds": 1800,
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
    # quietly enrol the agent in a persona nobody asked for. Blank means blank
    # here, and there is no catalogue behind it to fall through to - unlike a
    # council seat, whose lens the router picks when nobody has pinned one.
    "behavior": "",
    "prompt_on_stdin": False,
    "timeout_seconds": 1800,
    # --- Model selection ----------------------------------------------------
    "model": "",
    "models": [],
    # --- Reasoning effort ---------------------------------------------------
    "effort": "",
    # --- Agent --------------------------------------------------------------
    **copy.deepcopy(AGENTS["claude"]),
}

# --------------------------------------------------------------------------
# Projects Mode's three chairs
# --------------------------------------------------------------------------
#
# Configured as ordinary providers, under the role ids the engine dispatches
# on, so every picker the council already has - agent, model, reasoning effort,
# availability probe, quota chip - works on these with no new code. What none
# of them takes is a behaviour: a project chair is told what to do by the
# phase it is in rather than by a role assigned once, so the Roles catalogue
# has nothing to say about these three.
#
# The default pairing follows the work rather than the vendor: design and
# review to the model with the best judgement, bulk implementation to the one
# with the most generous quota, and build-and-verify to the one that is
# happiest running commands and reading what came back.


def _project_role(role: str, agent: str, timeout: int) -> Dict[str, Any]:
    return {
        "id": role,
        "enabled": True,
        "prompt_on_stdin": False,
        # Longer than a council stage's by default. A project phase reads a
        # tree, runs a build and writes several files in one turn, and a
        # timeout mid-phase costs the whole step - which the engine then has to
        # hand to another agent that starts from the same place.
        "timeout_seconds": timeout,
        "model": "",
        "models": [],
        "effort": "",
        **copy.deepcopy(AGENTS[agent]),
    }


DEFAULT_ARCHITECT = _project_role("architect", "claude", 1800)
DEFAULT_CODER = _project_role("coder", "codex", 2700)
DEFAULT_QA = _project_role("qa", "agy", 1800)

# What each chair is for, and which CLI the app would put in it. Kept beside
# the defaults rather than in the front end because the same three facts are
# wanted in two places - the chair cards in the matrix and the README - and a
# seat whose description lives only in the UI drifts from the one the engine
# dispatches on.
#
# `recommended_agent` draws a hint, never a rule: reassigning a chair is one
# click and stays one click. The reasoning is the capability profile in
# `router.py` rather than the vendor - judgement and review score highest for
# Claude, implementation for Codex, and analysis, which is what an independent
# check of somebody else's build actually is, for Antigravity. Deliberately no
# `label`: the probe result these are merged into already carries the
# provider's own, and the seat names live with the rest of the chrome.
PROJECT_SEATS = {
    "architect": {
        "recommended_agent": "claude",
        "summary": (
            "Turns the goal into cards with acceptance criteria, then judges "
            "every diff against them rather than against itself."
        ),
    },
    "coder": {
        "recommended_agent": "codex",
        "summary": (
            "Builds one card per turn, tests included, and clears failing "
            "builds. The seat that spends the most turns and the most quota."
        ),
    },
    "qa": {
        "recommended_agent": "agy",
        "summary": (
            "Audits the folder, runs the real build and tests, and decides "
            "at the end whether the thing that was built actually runs."
        ),
    },
}


def _council_seat(agent: str) -> Dict[str, Any]:
    """One CLI's standing council configuration.

    Carries no behaviour of its own. A seat's persona is decided by the router
    per run, from what the prompt is about, so a persona stored against the CLI
    would defeat the routing - and would follow that CLI into whichever seat it
    landed in, which is not what "this seat is the sceptic" means. Pin it in
    `council.personas` instead, keyed by seat, where it sits beside the agent
    pin that has the same effect on the same chair.

    The timeout is a ceiling rather than an expectation. A member is read-only
    and usually quick; the chairman writes, and takes its own longer ceiling
    from `council.chair_timeout_seconds` because the same CLI does both jobs
    and only one of them applies a patch.
    """
    return {
        "id": council_provider_id(agent),
        "enabled": True,
        "prompt_on_stdin": False,
        "timeout_seconds": 1200,
        "model": "",
        "models": [],
        "effort": "",
        **copy.deepcopy(AGENTS[agent]),
    }


DEFAULT_COUNCIL_PROVIDERS = {
    council_provider_id(agent): _council_seat(agent) for agent in AGENTS
}


DEFAULTS: Dict[str, Any] = {
    "version": 1,
    # --- Mode ---------------------------------------------------------------
    # Which of the two products the next message starts. Council is the
    # Junior Draft / Senior Polish pipeline; Solo is one assistant answering
    # directly. Not a modifier on the pipeline - Solo has no draft, no gate,
    # no delivery and no diff, and the two share only the working folder and
    # the conversation store.
    "mode": "council",
    # What the dashboard calls you. Blank falls back to the OS user the app was
    # launched as, which is the only name it can know without being told - and
    # a login name is rarely the one a person answers to.
    "display_name": "",
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
    # --- Where a run works --------------------------------------------------
    # The folder both modes run their agents in. Optional, and blank by
    # default: neither a conversation nor a council run needs a repository to
    # be worth starting, and demanding one before the first message turns a
    # coding assistant into a thing you have to configure first. Blank means
    # the scratch workspace (see ``workspace_dir``).
    #
    # A folder that is not a git repository is equally legitimate. What it
    # costs is the git-backed half of the safety model - snapshot, rollback,
    # diff and pull-request delivery - and the UI says which of those are off
    # rather than letting a run discover it.
    "workspace": "",
    "recent_workspaces": [],
    # --- Providers ----------------------------------------------------------
    "providers": {
        # One entry per CLI that can be seated on the council. Ordinary
        # providers, so the agent swap, model picker, effort picker,
        # availability probe and quota chip all work on them unchanged.
        **DEFAULT_COUNCIL_PROVIDERS,
        # The pre-deliberation council's two fixed chairs. No longer dispatched
        # on; kept so archived transcripts still render with named stages.
        "drafter": DEFAULT_DRAFTER,
        "polisher": DEFAULT_POLISHER,
        "solo": DEFAULT_SOLO,
        # Projects Mode. Three more chairs in the same map rather than a
        # namespace of their own, because everything that operates on a
        # provider - the agent swap, the model picker, the availability probe -
        # looks it up by id in exactly this dict.
        "architect": DEFAULT_ARCHITECT,
        "coder": DEFAULT_CODER,
        "qa": DEFAULT_QA,
    },
    # --- What each CLI runs -------------------------------------------------
    # One model and one reasoning level per agent, applied wherever that agent
    # sits - council seat, chat assistant or project chair. See the section
    # above for why the provider records carry copies of them.
    "agent_settings": _default_agent_settings(),
    # --- Council ------------------------------------------------------------
    # The deliberating bench: who sits, how hard they push, and how much of
    # that the operator fixes by hand.
    "council": {
        # How many members answer the task independently in Stage 1. Two is the
        # floor the router enforces - one member has no peer to review and the
        # critique stage would have nothing to do.
        "seat_count": 3,
        # Whether the chairman may also hold a member seat. On, because with
        # three CLIs installed the alternative benches the strongest one for
        # the whole of Stage 1. The cost - a chairman weighing a position it
        # wrote - is real, and the seating discloses it rather than hiding it.
        "chair_deliberates": True,
        # Reasoning effort for Stages 1 and 2, overriding whatever each seat's
        # CLI is set to. Empty - the default - leaves every seat alone.
        #
        # Why it exists: effort belongs to the CLI, so the seat Claude holds
        # and the chair Claude runs share one setting, and there was no way to
        # buy a cheaper deliberation without also demoting the only stage that
        # writes. Reading and re-reading the folder is where a council spends
        # its quota, and the chairman verifies that half anyway.
        "deliberation_effort": "",
        # How hard the critique stage pushes and how much agreement the chair
        # requires, 0-5. A prompt knob, not a sampling one: none of these CLIs
        # expose temperature, so a temperature slider here would move nothing.
        "strictness": 2,
        # "auto" routes the bench from the prompt. "manual" freezes it to the
        # pins below, which is what to use when the routing keeps choosing
        # differently to you and you would rather it stopped.
        "routing": "auto",
        # seat id -> agent, e.g. {"chair": "claude", "seat1": "codex"}. A
        # pinned seat is still profiled and still explains itself; it simply
        # cannot be moved. Unpinned seats route around whatever is pinned.
        "pins": {},
        # seat id -> role template id, fixing a seat's lens the same way. The
        # chair is not listed: its behaviour is the chairman's, which is what
        # the stage *is* rather than a lens over it.
        "personas": {},
        # Whether the bench is drawn above the thread. On, because the seats
        # are the control surface - clicking one pins it - and a strip that
        # only appeared once a run started could not be used to set one up.
        # Off is for when the routing is trusted and the row is just noise.
        "show_seats": True,
        # The chair applies the patch, so it gets a longer ceiling than the
        # read-only members even though it is usually the same CLI.
        "chair_timeout_seconds": 1800,
        # How long a Zero-Touch run waits at the gate its own failure forced
        # on. Zero-Touch means nobody is watching, so an unanswerable gate has
        # to end the run rather than park the engine on it forever.
        "gate_timeout_seconds": 3600,
        # agent -> axis -> 0.0-1.0. What each CLI is taken to be good at, which
        # is half of what the router scores against. Empty means "use the
        # shipped profiles"; see router.DEFAULT_CAPABILITIES for those and for
        # why they are starting positions rather than measurements.
        "capabilities": {},
        # The router's feedback loop, written by the pipeline rather than by
        # the operator: per agent, per axis, how its seats have actually gone.
        # A run the operator rolled back counts against the seat that wrote it.
        "stats": {},
    },
    # --- Projects -----------------------------------------------------------
    # The bounds on an autonomous build. All three exist because the loop has
    # no human in it: without them a project that cannot make progress will
    # keep spending quota on the same failure until someone notices.
    "project": {
        # Total agent turns before the run stops and says so. Roughly: one to
        # audit the folder, one to plan, one or two per card, and one QA round
        # per batch of cards.
        "max_steps": 40,
        # Consecutive failing builds before the project gives up rather than
        # handing the same trace back to the developer again.
        "max_fix_attempts": 3,
        # How many rounds of "what did we miss?" the architect gets once the
        # board is clear and the build is green. Zero builds what was asked for
        # and stops; this is what the Innovation slider sets.
        "innovation_rounds": 2,
    },
    # --- Prompting ----------------------------------------------------------
    # Extra standing instructions appended to both council stages. Solo does
    # not get them: it is a conversation, not a run against this project.
    "house_rules": "",
    # Caveman Mode: telegraphic answers, with code, paths, commands and error
    # text left byte-exact. Per mode rather than global, because the three want
    # different things from it - a Chat answer is read off the screen and
    # shortening it costs nothing, while a council deliberation is also the
    # record of *why* a change was made. Off everywhere by default: it changes
    # how every answer reads, which is not something to inherit unasked.
    "caveman": {
        "council": False,
        "chat": False,
        "project": False,
    },
    # Multi-agent answer: a Chat message goes to every installed CLI at once
    # and each answers on its own, one under the other. Chat only - Council
    # already asks several agents and then does something with the spread,
    # which is a different question from "what would each of you say".
    #
    # Each CLI answers with the model and reasoning depth set against it in
    # Settings > Agents. Those are the same per-CLI cards the council seats
    # read, because they describe the CLI rather than the job it is doing.
    #
    # Always read-only, whatever Zero-Touch says: three agents editing one
    # folder at the same time with nothing arbitrating between them is not a
    # feature, it is a race. See `_execute_chat_bench`.
    "multi_agent": False,
    # Efficiency Mode: concise normal prose without Caveman Mode's deliberately
    # telegraphic grammar. It is independent so either style, both, or neither
    # can be selected for each kind of run.
    "efficiency": {
        "council": False,
        "chat": False,
        "project": False,
    },
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


# The two writing-style switches, and the three kinds of run that each hold
# their own copy of both. Named here so the gear, the prompt builders and the
# three run loops are all talking about the same set.
WRITING_STYLES = ("caveman", "efficiency")
RUN_MODES = ("council", "chat", "project")


def writing_styles(conf: Dict[str, Any], mode: str) -> Dict[str, bool]:
    """The style switches one kind of run should answer under.

    One reader for all three: Council, Chat and Projects each pull the same two
    keys out of the same nested shape, and the result is splatted straight into
    the prompt builders, so a style added to `WRITING_STYLES` and to the
    builders reaches every mode without a fourth copy of this.

    Defensive rather than strict, like every other read of a persisted config
    here: a key hand-edited to a string or a null is off, not a crash mid-run.
    """
    styles: Dict[str, bool] = {}
    for style in WRITING_STYLES:
        modes = conf.get(style)
        styles[style] = bool(modes.get(mode)) if isinstance(modes, dict) else False
    return styles


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
    """Carry a config written before ``mode`` or ``workspace`` existed forward.

    Solo used to be a toggle that skipped Stage 1 and borrowed one council
    stage's configuration. What is worth keeping out of that is the operator's
    choice of CLI, model and reasoning level, so the stage they pointed it at
    is copied onto the new ``solo`` provider - but deliberately not its council
    role, which is precisely the part Solo no longer has.

    The working folder used to be a *target repository*: mandatory, and
    refused unless it contained a .git directory. The folder itself carries
    over unchanged - it is still where runs happen - but the name no longer
    claims it is a repository, because it no longer has to be one.

    ``raw`` is the file as written rather than the merged result: after
    ``_deep_merge`` every new default is present, and a missing legacy key
    would be indistinguishable from one the operator set to the same value.
    """
    if "workspace" not in raw:
        merged["workspace"] = str(raw.get("target_repo") or "")
        recent = raw.get("recent_repos")
        if isinstance(recent, list):
            merged["recent_workspaces"] = [str(r) for r in recent]

    if "mode" not in raw:
        merged["mode"] = "solo" if raw.get("solo_mode") else "council"
        source = (merged.get("providers") or {}).get(str(raw.get("solo_stage") or ""))
        if raw.get("solo_mode") and isinstance(source, dict):
            carried = copy.deepcopy(source)
            # The role keys are swept from every provider below, so listing
            # them here is belt and braces - but this is the one place where
            # dropping them is the *point* rather than tidying: what Solo
            # borrows from the stage is the operator's choice of CLI, model
            # and effort, never the council behaviour it was carrying.
            for key in ("id", "role", "role_template", "role_system", "enabled"):
                carried.pop(key, None)
            merged["providers"]["solo"] = _deep_merge(DEFAULT_SOLO, carried)

    # Dropped rather than left to rot: a stale key that no longer decides
    # anything is worse than an absent one, because it still reads as a setting.
    merged.pop("solo_mode", None)
    merged.pop("solo_stage", None)
    merged.pop("target_repo", None)
    merged.pop("recent_repos", None)
    # `cwd_mode` never decided anything - every stage has always run in the
    # working folder - and now that the folder need not be a repository, the
    # one value it ever held ("repo") does not even describe the default.
    #
    # The role keys stopped deciding anything when Council became a
    # deliberating bench: a seat's behaviour is the persona the router assigns
    # it, and no code reads a behaviour off a provider any more. Left in a
    # config the operator opens, `"role_system": "Be terse."` reads like an
    # instruction still being followed. Personas pinned in `council.personas`
    # are untouched - that is where a behaviour lives now.
    for provider in (merged.get("providers") or {}).values():
        if isinstance(provider, dict):
            provider.pop("cwd_mode", None)
            provider.pop("role", None)
            provider.pop("role_template", None)
            provider.pop("role_system", None)
    if merged.get("mode") not in ("council", "solo"):
        merged["mode"] = "council"
    _repair_agy_read_only(merged)
    _repair_incognito_args(merged, raw)
    _adopt_agent_settings(merged, raw)
    _adopt_agent_selection(merged, raw)
    _apply_agent_settings(merged)
    return merged


def _adopt_agent_selection(merged: Dict[str, Any], raw: Dict[str, Any]) -> None:
    """Carry a config written before agents were opt-in forward unchanged.

    Selection defaults to off, and applying that default to an existing
    installation would empty its bench at the next launch - the operator would
    open a working app and find nobody seated and nothing saying why. So a
    stored config that never had the key answers it from what it was already
    doing: an agent whose council seat was enabled was in use, and stays.

    Only agents the file is silent about are decided here. An explicit
    `"selected": false` written by the panel is an answer, not an absence, and
    survives a reload.
    """
    stored = raw.get("agent_settings")
    stored = stored if isinstance(stored, dict) else {}
    providers = raw.get("providers") or {}
    settings = merged.setdefault("agent_settings", _default_agent_settings())
    for agent in AGENTS:
        entry = stored.get(agent)
        if isinstance(entry, dict) and AGENT_SELECTED_KEY in entry:
            continue
        seat = providers.get(council_provider_id(agent))
        was_on = bool((seat or {}).get("enabled", True)) if isinstance(seat, dict) else True
        settings.setdefault(agent, {})[AGENT_SELECTED_KEY] = was_on


def _adopt_agent_settings(merged: Dict[str, Any], raw: Dict[str, Any]) -> None:
    """Take each CLI's global model and level from wherever it already sat.

    Before this, the same CLI carried its own model and level in every place it
    was seated, and a config in that shape holds up to five answers for one
    agent. The first non-empty one wins, council seat first, because that is
    the one the operator sees on the bench and is most likely to have set
    deliberately. The remembered *lists* are unioned instead: they are only the
    hand-typed names the picker offers next time, and losing one to a
    precedence rule would be a name the operator typed disappearing.
    """
    if isinstance(raw.get("agent_settings"), dict):
        return
    providers = merged.get("providers") or {}
    ordered = [pid for pid in PROVIDER_ORDER if pid in providers]
    ordered += [pid for pid in providers if pid not in ordered]

    settings = merged.setdefault("agent_settings", _default_agent_settings())
    for agent in AGENTS:
        entry = settings.setdefault(agent, {"model": "", "models": [], "effort": ""})
        for pid in ordered:
            provider = providers.get(pid)
            if not isinstance(provider, dict) or agent_for(provider) != agent:
                continue
            for key in ("model", "effort"):
                if not entry.get(key) and provider.get(key):
                    entry[key] = provider[key]
            for model in provider.get("models") or []:
                if model not in entry["models"]:
                    entry["models"].append(model)


def _apply_agent_settings(merged: Dict[str, Any]) -> None:
    """Give every provider the settings of the agent it runs."""
    settings = merged.get("agent_settings") or {}
    for provider in (merged.get("providers") or {}).values():
        if not isinstance(provider, dict):
            continue
        chosen = settings.get(agent_for(provider))
        if not isinstance(chosen, dict):
            continue  # a hand-written command answers to nobody's settings
        provider["model"] = str(chosen.get("model") or "")
        provider["models"] = [str(m) for m in (chosen.get("models") or [])]
        provider["effort"] = str(chosen.get("effort") or "")


def _lift_agent_settings(
    current: Dict[str, Any], patch: Dict[str, Any]
) -> Dict[str, Any]:
    """Rewrite a per-provider model or effort change as a per-agent one.

    The pickers patch the provider they were opened on, which is the only id
    they have; this is where that becomes a setting for the CLI itself. The
    provider keys are removed from the patch rather than left beside the lifted
    copy, so the two cannot disagree - ``_apply_agent_settings`` writes them
    back from the one source afterwards.

    A value equal to what the agent already has is dropped rather than lifted.
    That is what makes the Settings form safe: it posts every card at once, so
    a save that edits Claude's model on the council card also carries the chat
    card's unedited copy of it, and taking the last one read would undo the
    edit. Two cards edited to different values in one save is still last-wins,
    and there is no way to tell those apart.
    """
    if not isinstance(patch.get("providers"), dict):
        return patch
    out = copy.deepcopy(patch)
    settings = current.get("agent_settings") or {}
    lifted: Dict[str, Dict[str, Any]] = {}
    for pid, changes in out["providers"].items():
        if not isinstance(changes, dict):
            continue
        # An agent swap clears the model and level of the CLI leaving the
        # chair (see `_resolve_agent_choices`); lifting those clears would wipe
        # the settings of the CLI arriving in it.
        if changes.get("agent"):
            continue
        agent = agent_for((current.get("providers") or {}).get(pid) or {})
        if agent not in AGENTS:
            continue
        for key in AGENT_SETTING_KEYS:
            if key not in changes:
                continue
            value = changes.pop(key)
            if value != (settings.get(agent) or {}).get(key):
                lifted.setdefault(agent, {})[key] = value
    if lifted:
        out["agent_settings"] = _deep_merge(out.get("agent_settings") or {}, lifted)
    return out


# What `agy`'s read-only grant used to be, before the auto-deny above was
# understood. A provider still carrying exactly this is carrying a bug rather
# than a preference, so it is repaired in place.
_BROKEN_AGY_READ_ONLY = ["--mode", "plan"]


def _repair_agy_read_only(merged: Dict[str, Any]) -> None:
    """Give stored Antigravity providers the read grant they were missing.

    ``_deep_merge`` cannot do this: a list is replaced wholesale, so a config
    written before the fix keeps its own two-element list forever and every
    read-only stage it runs keeps producing nothing.

    Matched exactly against the old default, never merely by agent. An operator
    who has hand-written their own flags has said something, and this is not
    entitled to overrule it.
    """
    for provider in (merged.get("providers") or {}).values():
        if not isinstance(provider, dict):
            continue
        if agent_for(provider) != "agy":
            continue
        if list(provider.get("read_only_args") or []) == _BROKEN_AGY_READ_ONLY:
            provider["read_only_args"] = list(AGENTS["agy"]["read_only_args"])


def _repair_incognito_args(merged: Dict[str, Any], raw: Dict[str, Any]) -> None:
    """Give a provider written before Incognito the flags of its own CLI.

    ``_deep_merge`` fills the missing key from the default for that *provider
    id*, which names the right CLI only where the operator never swapped one
    in. A chat assistant pointed at Antigravity would otherwise inherit
    Claude's ``--no-session-persistence`` - the exact mixing of one agent's
    flags into another's binary that ``agent_for`` exists to prevent. Here it
    is worse than a failed launch: ``providers.supports_incognito`` would read
    that inherited flag and seat a CLI that cannot honour it.

    Only providers the stored file wrote without the key are repaired, so a
    hand-written wrapper's own arguments are the operator's and stay.
    """
    stored = raw.get("providers")
    stored = stored if isinstance(stored, dict) else {}
    for pid, provider in (merged.get("providers") or {}).items():
        if not isinstance(provider, dict):
            continue
        before = stored.get(pid)
        if isinstance(before, dict) and "incognito_args" in before:
            continue
        preset = AGENTS.get(agent_for(provider)) or {}
        provider["incognito_args"] = list(preset.get("incognito_args") or [])


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
        # Owner-only, and set on the temporary file so the config is never
        # world-readable even for the instant before the rename.
        write_private_text(tmp, json.dumps(self._data, indent=2) + "\n")
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
        flags as one unit. A model or reasoning level is lifted onto the agent
        itself before the merge, and projected back onto every provider running
        that agent after it - one CLI, one setting, wherever it sits.
        """
        with self._lock:
            current = self._load()
            resolved = _resolve_agent_choices(
                current, _lift_agent_settings(current, patch)
            )
            self._data = _deep_merge(current, resolved)
            _apply_agent_settings(self._data)
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

    def remember_workspace(self, folder: str, limit: int = 8) -> None:
        """Push ``folder`` to the front of the most-recently-used list.

        Re-reads first for the reason ``update`` does: this runs at the start
        of every run with a chosen folder, and writing the whole file from a
        stale in-memory copy would revert whatever another window - or a hand
        edit - had changed since startup, ``zero_touch`` included.
        """
        if not folder:
            return
        with self._lock:
            self._data = self._load()
            recent = [r for r in self._data.get("recent_workspaces", []) if r != folder]
            recent.insert(0, folder)
            self._data["recent_workspaces"] = recent[:limit]
            self._data["workspace"] = folder
            self._write()
