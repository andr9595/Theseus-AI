"""Subscription-quota polling.

The council spends a rationed resource, so "how much is left" belongs on the
dashboard. Neither CLI reports quota through its normal non-interactive
output, but Claude Code answers its own ``/usage`` slash command through
``claude -p``:

    Current session: 20% used · resets Jul 25, 10:49pm (Europe/Copenhagen)
    Current week (all models): 21% used · resets Jul 31, 1:59am (Europe/Copenhagen)

That is the vendor's own number, which is the only trustworthy source: the
denominator of a Pro plan is not published, so anything computed locally would
be a guess dressed up as a measurement.

Codex answers differently. ``codex exec "/status"`` does not run a slash
command - it sends the text to the model as a prompt, which costs ~16k tokens
and returns the model's apology for not knowing. But the CLI records the
server's rate-limit headers into its session rollout logs under
``$CODEX_HOME/sessions/``:

    {"limit_id": "codex", "plan_type": "plus",
     "primary": {"used_percent": 1.0, "window_minutes": 10080,
                 "resets_at": 1785544255}}

Reading the newest of those is free and needs no subprocess at all. The
tradeoff is freshness: it is the figure from the last Codex run rather than
this instant, so readings carry the time they were captured and the UI says
"as of" rather than implying live. A slightly stale real number beats both an
invented one and a blank.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .providers import resolve_binary

# `/usage` is answered locally by the CLI, so this is a short wait, not a
# model call. If it ever starts taking longer than this, something has
# changed and a stale reading is better than a hung poller.
USAGE_TIMEOUT = 45

# Matches every "Current <label>: <n>% used · resets <when>" line rather than
# the two known ones, so a plan with an extra limit (a per-model weekly cap,
# say) shows up without a code change.
LIMIT_RE = re.compile(
    r"^Current\s+(?P<label>[^:]+):\s*(?P<pct>\d+(?:\.\d+)?)%\s*used"
    r"(?:\s*[·.-]\s*resets\s+(?P<resets>.+?))?\s*$",
    re.MULTILINE,
)


@dataclass
class Limit:
    """One quota window as the CLI reported it."""

    label: str
    percent: float
    resets: str = ""
    # Length of the quota window in minutes, where known. Codex reports it
    # directly; for Claude it is inferred from the label. Used to decide which
    # limit the chip leads with - see UsageReading.primary.
    window_minutes: Optional[float] = None
    # When the figure was measured. Claude's is read live; Codex's comes from
    # its last run's log, and conflating the two would overstate the second.
    as_of: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "percent": self.percent,
            "resets": self.resets,
            "as_of": self.as_of,
            "window_minutes": self.window_minutes,
        }


@dataclass
class UsageReading:
    """The latest quota answer for one agent."""

    provider_id: str
    supported: bool = True
    limits: List[Limit] = field(default_factory=list)
    checked_at: float = 0.0
    error: str = ""
    note: str = ""

    @property
    def worst(self) -> Optional[Limit]:
        """The limit closest to exhaustion."""
        return max(self.limits, key=lambda l: l.percent) if self.limits else None

    @property
    def primary(self) -> Optional[Limit]:
        """The limit to lead with: the shortest window that is known.

        For Claude that is the 5-hour session, which is what actually stops
        work mid-afternoon; the weekly moves slowly and is the wrong number to
        read at a glance. Codex reports only a weekly, so it leads with that.
        Ranking by window length rather than matching the word "session" means
        a plan that gains a new short window is handled without a change here.

        The chip shows this figure but takes its colour from ``worst``, so a
        nearly-exhausted weekly still turns it red rather than hiding behind a
        comfortable session number.
        """
        if not self.limits:
            return None
        known = [l for l in self.limits if l.window_minutes]
        if known:
            return min(known, key=lambda l: l.window_minutes)
        return self.worst

    def to_dict(self) -> Dict[str, Any]:
        worst = self.worst
        primary = self.primary
        return {
            "provider_id": self.provider_id,
            "supported": self.supported,
            "limits": [l.to_dict() for l in self.limits],
            "worst": worst.to_dict() if worst else None,
            "primary": primary.to_dict() if primary else None,
            "checked_at": self.checked_at,
            "error": self.error,
            "note": self.note,
        }


# Claude reports a label, not a duration. These are the windows those labels
# correspond to, so the shortest-window rule below can rank them against
# Codex's numeric ones.
CLAUDE_WINDOWS = (
    ("session", 300.0),   # the 5-hour rolling window
    ("day", 1440.0),
    ("week", 10080.0),
    ("month", 43200.0),
)


def window_for_label(label: str) -> Optional[float]:
    """Best guess at a window length from a Claude usage label."""
    low = (label or "").lower()
    for token, minutes in CLAUDE_WINDOWS:
        if token in low:
            return minutes
    return None


def parse_usage(text: str) -> List[Limit]:
    """Pull every reported limit out of ``claude -p "/usage"`` output."""
    limits: List[Limit] = []
    for m in LIMIT_RE.finditer(text or ""):
        try:
            pct = float(m.group("pct"))
        except (TypeError, ValueError):
            continue
        resets = (m.group("resets") or "").strip()
        label = m.group("label").strip()
        limits.append(
            Limit(
                label=label,
                percent=pct,
                resets=resets,
                window_minutes=window_for_label(label),
            )
        )
    return limits


def agent_kind(provider: Dict[str, Any]) -> str:
    """Which quota source applies to this agent: 'claude', 'codex' or ''."""
    exe = os.path.basename(str((provider.get("command") or [""])[0]))
    if "claude" in exe:
        return "claude"
    if "codex" in exe:
        return "codex"
    return ""


def supports_usage(provider: Dict[str, Any]) -> bool:
    """Whether this agent's quota can be read at all."""
    return bool(agent_kind(provider))


def read_usage(provider: Dict[str, Any], cwd: Optional[str] = None) -> UsageReading:
    """Ask one agent for its quota. Never raises."""
    pid = str(provider.get("id", "provider"))
    command = list(provider.get("command") or [])

    kind = agent_kind(provider)
    if not kind:
        return UsageReading(
            provider_id=pid,
            supported=False,
            note=(
                f"No quota source known for "
                f"`{(command or ['this command'])[0]}`."
            ),
        )

    if kind == "codex":
        return read_codex_usage(pid)

    exe = resolve_binary(command)
    if exe is None:
        return UsageReading(
            provider_id=pid, error=f"`{(command or ['?'])[0]}` is not on PATH."
        )

    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"

    try:
        proc = subprocess.run(
            [exe, "-p", "/usage"],
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            stdin=subprocess.DEVNULL,  # or the CLI waits for more input
            capture_output=True,
            text=True,
            timeout=USAGE_TIMEOUT,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return UsageReading(provider_id=pid, error=f"Timed out after {USAGE_TIMEOUT}s.")
    except (OSError, ValueError) as exc:
        return UsageReading(provider_id=pid, error=f"Could not run: {exc}")

    limits = parse_usage(proc.stdout)
    if not limits:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return UsageReading(
            provider_id=pid,
            checked_at=time.time(),
            error=(
                detail[-1][:200] if detail else
                "No usage lines in the reply. The CLI's /usage output may have "
                "changed shape."
            ),
        )
    return UsageReading(provider_id=pid, limits=limits, checked_at=time.time())


def _window_label(minutes: Optional[float]) -> str:
    """Name a rate-limit window from its length in minutes."""
    try:
        mins = float(minutes)
    except (TypeError, ValueError):
        return "limit"
    if mins >= 10000:  # 10080 = 7 days
        return "week"
    if mins >= 1400:  # 1440 = 1 day
        return "day"
    if mins >= 60:
        hours = mins / 60
        return f"{hours:g}-hour window"
    return f"{mins:g}-minute window"


def _codex_session_files(home: str, limit: int = 40) -> List[str]:
    """Newest rollout logs first, capped so a long history is not walked."""
    root = os.path.join(home, "sessions")
    found: List[tuple] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            try:
                found.append((os.path.getmtime(path), path))
            except OSError:
                continue
    found.sort(reverse=True)
    return [p for _mtime, p in found[:limit]]


def _parse_codex_rate_limits(payload: Dict[str, Any], as_of: float) -> List[Limit]:
    """Turn one ``rate_limits`` payload into the limits worth showing."""
    limits: List[Limit] = []
    for slot in ("primary", "secondary"):
        entry = payload.get(slot)
        if not isinstance(entry, dict):
            continue
        pct = entry.get("used_percent")
        if pct is None:
            continue
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            continue
        resets = ""
        stamp = entry.get("resets_at")
        if stamp:
            try:
                resets = time.strftime("%b %-d, %-I:%M%p", time.localtime(float(stamp)))
            except (TypeError, ValueError, OSError):
                resets = ""
        try:
            window = float(entry.get("window_minutes"))
        except (TypeError, ValueError):
            window = None
        limits.append(
            Limit(
                label=_window_label(entry.get("window_minutes")),
                percent=pct,
                resets=resets,
                as_of=as_of,
                window_minutes=window,
            )
        )
    return limits


def read_codex_usage(provider_id: str, home: Optional[str] = None) -> UsageReading:
    """Read Codex's quota from the rate-limit headers it logs per run.

    Free and instant - no subprocess, no tokens. The figure is as of the last
    Codex run, which the caller surfaces rather than hides.
    """
    home = home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    files = _codex_session_files(home)
    if not files:
        return UsageReading(
            provider_id=provider_id,
            note=(
                "No Codex session logs yet. Quota appears here after the first "
                "Codex run, which is when the CLI records the server's limits."
            ),
        )

    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                # Last occurrence in the file is the most recent in that run.
                newest: Optional[Dict[str, Any]] = None
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = (record.get("payload") or {}).get("rate_limits")
                    if isinstance(payload, dict):
                        newest = payload
        except OSError:
            continue

        if newest:
            as_of = os.path.getmtime(path)
            limits = _parse_codex_rate_limits(newest, as_of)
            if limits:
                plan = newest.get("plan_type")
                return UsageReading(
                    provider_id=provider_id,
                    limits=limits,
                    checked_at=time.time(),
                    note=(
                        f"From the last Codex run"
                        + (f" · {plan} plan" if plan else "")
                    ),
                )

    return UsageReading(
        provider_id=provider_id,
        note=(
            "Codex session logs contain no rate-limit record yet. It appears "
            "after a run that reaches the server."
        ),
    )


class UsagePoller:
    """Background thread that refreshes quota on an interval.

    Polls once at startup and then every ``interval`` seconds while the app is
    running. It is not tied to runs: quota moves because of work done anywhere,
    including other machines and claude.ai, so a reading taken only around runs
    would drift.
    """

    def __init__(
        self,
        store,
        on_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.store = store
        self.on_update = on_update
        self._lock = threading.RLock()
        self._readings: Dict[str, UsageReading] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="usage-poll", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def refresh_now(self) -> None:
        """Ask for an immediate poll (e.g. straight after a run finishes)."""
        self._wake.set()

    # -- data --------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {pid: r.to_dict() for pid, r in self._readings.items()}

    def worst_percent(self, provider_id: str) -> Optional[float]:
        """Highest reported percentage for one agent, or None if unknown."""
        with self._lock:
            reading = self._readings.get(provider_id)
        if not reading or not reading.limits:
            return None
        worst = reading.worst
        return worst.percent if worst else None

    # -- internals ---------------------------------------------------------

    def _interval(self) -> float:
        try:
            value = float(self.store.get("usage_poll_seconds", 300) or 300)
        except (TypeError, ValueError):
            value = 300.0
        # A floor keeps a mistyped setting from hammering the CLI; polling is
        # cheap but not free of process spawns.
        return max(60.0, value)

    def poll_once(self) -> Dict[str, Any]:
        providers = self.store.get("providers", {}) or {}
        repo = self.store.get("target_repo") or None
        for pid, provider in providers.items():
            if self._stop.is_set():
                break
            reading = read_usage(provider, cwd=repo)
            with self._lock:
                # Keep the previous numbers when a poll fails, so a transient
                # error blanks the chip's freshness rather than its content.
                prior = self._readings.get(pid)
                if reading.error and prior and prior.limits:
                    prior.error = reading.error
                    self._readings[pid] = prior
                else:
                    self._readings[pid] = reading
        snap = self.snapshot()
        if self.on_update:
            try:
                self.on_update(snap)
            except Exception:  # a UI callback must not kill the poller
                pass
        return snap

    def _loop(self) -> None:
        if not self.store.get("usage_polling", True):
            return
        self.poll_once()  # once at launch, as soon as the app is up
        while not self._stop.is_set():
            self._wake.wait(timeout=self._interval())
            self._wake.clear()
            if self._stop.is_set():
                break
            if self.store.get("usage_polling", True):
                self.poll_once()
