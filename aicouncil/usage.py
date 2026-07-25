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

Codex has no equivalent. ``codex exec "/status"`` does not run a slash command
- it sends the text to the model as a prompt, which costs ~16k tokens and
returns the model's apology for not knowing. Polling that would burn the very
quota it claims to measure, so this module refuses to try and says why. An
agent with no data shows "no data", never an invented percentage.
"""

from __future__ import annotations

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

    def to_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "percent": self.percent, "resets": self.resets}


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
        """The limit closest to exhaustion - the one worth showing on a chip."""
        return max(self.limits, key=lambda l: l.percent) if self.limits else None

    def to_dict(self) -> Dict[str, Any]:
        worst = self.worst
        return {
            "provider_id": self.provider_id,
            "supported": self.supported,
            "limits": [l.to_dict() for l in self.limits],
            "worst": worst.to_dict() if worst else None,
            "checked_at": self.checked_at,
            "error": self.error,
            "note": self.note,
        }


def parse_usage(text: str) -> List[Limit]:
    """Pull every reported limit out of ``claude -p "/usage"`` output."""
    limits: List[Limit] = []
    for m in LIMIT_RE.finditer(text or ""):
        try:
            pct = float(m.group("pct"))
        except (TypeError, ValueError):
            continue
        resets = (m.group("resets") or "").strip()
        limits.append(
            Limit(label=m.group("label").strip(), percent=pct, resets=resets)
        )
    return limits


def supports_usage(provider: Dict[str, Any]) -> bool:
    """Whether this agent can be asked for its quota at all."""
    exe = (provider.get("command") or [""])[0]
    return "claude" in os.path.basename(str(exe))


def read_usage(provider: Dict[str, Any], cwd: Optional[str] = None) -> UsageReading:
    """Ask one agent for its quota. Never raises."""
    pid = str(provider.get("id", "provider"))
    command = list(provider.get("command") or [])

    if not supports_usage(provider):
        return UsageReading(
            provider_id=pid,
            supported=False,
            note=(
                f"{(command or ['this CLI'])[0]} does not report quota "
                f"non-interactively. Its slash commands are sent to the model "
                f"as prompts, which would spend quota rather than measure it."
            ),
        )

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
