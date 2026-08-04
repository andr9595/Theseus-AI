"""Which agents sit on the council for *this* prompt, and in which chair.

The council used to have two fixed chairs: Codex drafted, Claude polished.
That is the right pairing for "add a flag to this CLI" and the wrong one for
"is this JWT check exploitable" - and nothing about the prompt was allowed to
change it. This module decides the seating per run instead.

The shape is borrowed from LLMRouter (ulab-uiuc): a query is turned into a
profile, every candidate model carries a profile of its own, and the router
scores one against the other. What is *not* borrowed is how those profiles are
obtained. LLMRouter embeds the query with Longformer and learns the model
vectors from eleven benchmark datasets; that needs torch, a training run and
per-model benchmark scores. This machine has no pip (see the project notes), so
none of it is available and pretending otherwise would ship a router that
cannot run.

So both halves are explicit and editable instead of learned:

* the **task profile** comes from lexical feature extraction - keyword
  lexicons plus structural signals like fenced code, file paths and
  tracebacks;
* the **capability profile** is a per-agent vector the operator can edit in
  Settings, seeded with defaults that describe what each CLI is actually good
  at.

The one part of LLMRouter's training loop that does port is the feedback: past
runs are scored and fed back as a bounded adjustment, so a seat that keeps
failing on debugging tasks slowly stops being chosen for them.

Every number here is computed locally and is a statement about *this router's
preference*, never about a model's quality. Nothing in this module produces a
figure that may be shown as a model's confidence or a vendor's percentage -
those come from the agents themselves, or are absent. The UI is given the
reasons, not the arithmetic.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Axes
# --------------------------------------------------------------------------

# The dimensions a task and an agent are both described in. Six, chosen because
# each one changes who should answer: they are not a taxonomy of software work,
# they are the axes on which these particular CLIs actually differ.
DIMENSIONS: Tuple[str, ...] = (
    "implementation",  # write the code, build the thing
    "debugging",       # something is broken and the cause is unknown
    "review",          # judge existing code: correctness, defects, quality
    "security",        # attacker present, trust boundary involved
    "architecture",    # structure, trade-offs, "how should this be shaped"
    "analysis",        # explain, compare, decide - prose, not a patch
)

# Keyword -> weight, per axis. Weights are deliberately coarse: a two-point
# spread between "this word is the whole task" and "this word merely hints at
# it" is as much resolution as single-word matching can honestly support.
#
# Matched on word stems against a lowercased, word-split prompt, so "refactor"
# also catches "refactoring" and "refactored" without listing every ending.
LEXICONS: Dict[str, Dict[str, float]] = {
    "implementation": {
        "implement": 3, "add": 2, "build": 3, "write": 2, "creat": 2,
        "feature": 2, "support": 1, "port": 2, "migrat": 2, "rename": 2,
        "wire": 2, "endpoint": 2, "function": 1, "script": 2, "cli": 1,
        "generat": 2, "scaffold": 2,
    },
    "debugging": {
        "bug": 3, "fix": 3, "broken": 3, "fail": 3, "error": 3, "crash": 3,
        "traceback": 4, "exception": 3, "regress": 3, "hang": 2, "leak": 2,
        "wrong": 2, "unexpect": 2, "reproduc": 3, "flaky": 3, "stack": 1,
        "debug": 4, "why": 1,
    },
    "review": {
        "review": 4, "critique": 3, "audit": 3, "check": 2, "verify": 2,
        "correct": 2, "quality": 2, "smell": 2, "lint": 2, "improv": 1,
        "cleanup": 2, "simplif": 2, "readab": 2, "maintain": 2, "test": 1,
        "coverage": 2, "assert": 1,
    },
    "security": {
        "secur": 4, "vulnerab": 4, "exploit": 4, "attack": 3, "auth": 3,
        "token": 2, "password": 3, "credential": 3, "secret": 3, "crypt": 3,
        "injection": 4, "xss": 4, "csrf": 4, "sanitis": 3, "sanitiz": 3,
        "escap": 2, "permission": 2, "privileg": 3, "sandbox": 2, "cve": 4,
        "hardening": 3, "selinux": 2,
    },
    "architecture": {
        "architect": 4, "design": 3, "structur": 3, "refactor": 3,
        "pattern": 2, "abstract": 2, "coupl": 2, "modul": 2, "layer": 2,
        "scale": 2, "tradeoff": 3, "trade-off": 3, "approach": 2,
        "should": 1, "boundary": 2, "interface": 2, "schema": 2,
    },
    "analysis": {
        "explain": 3, "compar": 3, "why": 2, "how": 1, "what": 1,
        "understand": 2, "research": 3, "evaluat": 3, "recommend": 3,
        "option": 2, "decide": 2, "docum": 2, "summar": 3, "overview": 2,
        "difference": 2, "best": 1, "worth": 2,
    },
}

# Structural signals. Cheaper and far more reliable than keywords - a
# traceback in the prompt is near-proof of a debugging task in a way that the
# word "error" is not, because the word appears in every request to *add* error
# handling too.
_CODE_FENCE = re.compile(r"```")
_FILE_PATH = re.compile(r"\b[\w./-]+\.(?:py|js|ts|tsx|go|rs|java|rb|c|h|cpp|sh|css|html|json|ya?ml|toml|sql)\b")
_TRACEBACK = re.compile(r"traceback \(most recent call last\)|^\s+at .+\(.+:\d+\)|panic:|segmentation fault", re.I | re.M)
_LINE_REF = re.compile(r"\b\w+\.\w+:\d+\b")
_QUESTION = re.compile(r"\?\s*$|^\s*(?:what|why|how|which|should|is|are|does|can|would)\b", re.I | re.M)

# What each structural hit adds, per axis. Sized against the lexicon weights
# above: a traceback outweighs any single keyword, which is the intent.
STRUCTURAL: Tuple[Tuple[Any, Dict[str, float]], ...] = (
    (_TRACEBACK, {"debugging": 6, "implementation": 1}),
    (_LINE_REF, {"debugging": 2, "review": 1}),
    (_CODE_FENCE, {"implementation": 2, "review": 2}),
    (_FILE_PATH, {"implementation": 2, "review": 1}),
    (_QUESTION, {"analysis": 3, "architecture": 1}),
)

_WORDS = re.compile(r"[a-z0-9_-]+")


# --------------------------------------------------------------------------
# Capability profiles
# --------------------------------------------------------------------------

# What each CLI is taken to be good at, on the axes above, 0.0 - 1.0.
#
# These are starting positions, not measurements, and the Settings panel says
# so. They encode the reason each agent is in the app at all: Codex is the fast
# generous-quota implementer, Claude is the strong reviewer that also writes,
# Antigravity is the one reached for to think a question through rather than
# to land a patch. An operator who disagrees edits them, and the feedback loop
# below moves them anyway.
DEFAULT_CAPABILITIES: Dict[str, Dict[str, float]] = {
    "codex": {
        "implementation": 0.90, "debugging": 0.80, "review": 0.55,
        "security": 0.50, "architecture": 0.55, "analysis": 0.50,
    },
    "claude": {
        "implementation": 0.85, "debugging": 0.85, "review": 0.95,
        "security": 0.90, "architecture": 0.90, "analysis": 0.85,
    },
    "agy": {
        "implementation": 0.60, "debugging": 0.60, "review": 0.70,
        "security": 0.60, "architecture": 0.75, "analysis": 0.90,
    },
    # An operator's own command. Deliberately flat: the router has no idea what
    # it is, so it must neither favour nor bury it. A flat profile scores the
    # mean on every task, which is what "no information" should look like.
    "custom": {d: 0.60 for d in DIMENSIONS},
}

# How much a seat's own history may move its score, either way. Bounded hard:
# the feedback signal here is a handful of runs, not a benchmark sweep, and an
# unbounded loop would let two bad afternoons permanently bench an agent.
HISTORY_WEIGHT = 0.15
# Below this many recorded runs the history term is scaled down proportionally,
# so one unlucky run cannot swing a seat.
HISTORY_CONFIDENCE_RUNS = 5

# How much a quota reading may move a score. Smaller than history on purpose:
# a nearly-exhausted agent should be passed over for a *marginal* seat, not
# dropped from a task it is uniquely suited to.
QUOTA_WEIGHT = 0.20
# Above this percentage of its window consumed, an agent starts being avoided.
QUOTA_PRESSURE_PERCENT = 75.0


def default_capabilities() -> Dict[str, Dict[str, float]]:
    """A fresh copy of the shipped capability table."""
    return {agent: dict(axes) for agent, axes in DEFAULT_CAPABILITIES.items()}


def capability_for(agent: str, stored: Optional[Dict[str, Dict]] = None) -> Dict[str, float]:
    """One agent's capability vector, operator edits applied.

    Unknown agents fall back to the flat ``custom`` profile rather than to
    zeros: an agent the router knows nothing about should rank in the middle,
    not last. Missing individual axes fall back the same way, so a partially
    edited profile stays usable.
    """
    base = dict(DEFAULT_CAPABILITIES.get(agent) or DEFAULT_CAPABILITIES["custom"])
    saved = ((stored or {}).get(agent) or {})
    for axis in DIMENSIONS:
        value = saved.get(axis)
        if isinstance(value, (int, float)):
            # Clamped rather than rejected: a hand-edited config file is a
            # supported way to set these, and 1.4 clearly means "as high as
            # it goes" rather than "discard my edit".
            base[axis] = max(0.0, min(1.0, float(value)))
        elif axis not in base:
            base[axis] = 0.60
    return base


# --------------------------------------------------------------------------
# Task profile
# --------------------------------------------------------------------------


@dataclass
class TaskProfile:
    """What kind of work a prompt is asking for, on the six axes.

    ``weights`` sum to 1.0 across the axes, so a score is a weighted average of
    an agent's capabilities and is directly comparable between tasks.
    ``dominant`` is what the UI names as the reason.
    """

    weights: Dict[str, float]
    signals: List[str] = field(default_factory=list)
    length: int = 0

    @property
    def dominant(self) -> List[str]:
        """The axes carrying this task, strongest first.

        Only those meaningfully above an even split are returned - a prompt
        that scores flat has no dominant axis, and inventing one would put a
        confident-sounding but arbitrary reason on screen.
        """
        even = 1.0 / len(DIMENSIONS)
        ranked = sorted(self.weights.items(), key=lambda kv: -kv[1])
        return [axis for axis, w in ranked if w > even * 1.25]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "dominant": self.dominant,
            "signals": self.signals,
            "length": self.length,
        }


def profile_task(task: str) -> TaskProfile:
    """Turn a prompt into a task profile by lexical and structural extraction.

    A prompt that matches nothing - "have a look at this" - yields an even
    spread, which makes every agent score its own mean and lets the tie-break
    (declared order, then history) decide. That is the correct behaviour for a
    prompt carrying no information: it should not be routed confidently.
    """
    text = (task or "").strip()
    lowered = text.lower()
    words = set(_WORDS.findall(lowered))

    raw: Dict[str, float] = {d: 0.0 for d in DIMENSIONS}
    signals: List[str] = []

    # Keywords. Stem-matched: a lexicon entry hits if any word in the prompt
    # starts with it, which is why the entries are stems and not whole words.
    for axis, lexicon in LEXICONS.items():
        for stem, weight in lexicon.items():
            if any(word.startswith(stem) for word in words):
                raw[axis] += weight

    # Structure.
    for pattern, contribution in STRUCTURAL:
        if pattern.search(text):
            for axis, weight in contribution.items():
                raw[axis] += weight
            signals.append(_SIGNAL_NAMES.get(pattern, "structure"))

    total = sum(raw.values())
    if total <= 0:
        weights = {d: 1.0 / len(DIMENSIONS) for d in DIMENSIONS}
    else:
        # Smoothed towards even before normalising. Without this a prompt whose
        # single matching word is "fix" would score 1.0 debugging and 0.0
        # everywhere else, and a seat good at everything *except* debugging
        # would score zero - which is never what one keyword should mean.
        floor = total * 0.08
        smoothed = {d: raw[d] + floor for d in DIMENSIONS}
        grand = sum(smoothed.values())
        weights = {d: smoothed[d] / grand for d in DIMENSIONS}

    return TaskProfile(weights=weights, signals=signals, length=len(text))


# Named here rather than inline so the tuple above stays readable.
_SIGNAL_NAMES = {
    _TRACEBACK: "traceback",
    _LINE_REF: "file:line reference",
    _CODE_FENCE: "fenced code",
    _FILE_PATH: "file path",
    _QUESTION: "open question",
}


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def score_history(stats: Optional[Dict[str, Any]], agent: str, profile: TaskProfile) -> Tuple[float, Optional[str]]:
    """A bounded adjustment from how this agent has done on tasks like this.

    Returns ``(adjustment, reason)`` where the adjustment is within
    +/- ``HISTORY_WEIGHT`` and the reason is None when there is nothing worth
    saying. Recorded per axis, so "Codex keeps failing at security work" does
    not also penalise it for implementation work.

    A run that was rolled back counts as a failure even though it exited zero:
    the operator undoing the work is the strongest signal available that the
    council got it wrong, and it is the only one that does not depend on an
    agent's own account of itself.
    """
    entry = ((stats or {}).get(agent) or {})
    if not entry:
        return 0.0, None

    # Weight each axis's record by how much this task is about that axis, so
    # the adjustment is specific to the work in hand.
    total_weight = 0.0
    total_rate = 0.0
    runs_seen = 0
    for axis in DIMENSIONS:
        axis_stats = entry.get(axis) or {}
        runs = int(axis_stats.get("runs") or 0)
        if runs <= 0:
            continue
        good = int(axis_stats.get("ok") or 0) - int(axis_stats.get("rolled_back") or 0)
        rate = max(0.0, min(1.0, good / runs))
        weight = profile.weights[axis] * min(1.0, runs / HISTORY_CONFIDENCE_RUNS)
        total_rate += rate * weight
        total_weight += weight
        runs_seen += runs

    if total_weight <= 0 or runs_seen == 0:
        return 0.0, None

    # Centred on 0.5: a 50% success rate is neutral, not a penalty.
    mean_rate = total_rate / total_weight
    adjustment = (mean_rate - 0.5) * 2.0 * HISTORY_WEIGHT * min(1.0, total_weight)

    if abs(adjustment) < 0.01:
        return adjustment, None
    verdict = "a good record here" if adjustment > 0 else "a poor record here"
    return adjustment, f"{verdict} over {runs_seen} run(s)"


def record_outcome(
    stats: Dict[str, Any],
    agent: str,
    profile_weights: Dict[str, float],
    ok: bool,
    rolled_back: bool = False,
) -> Dict[str, Any]:
    """Fold one finished seat into the history table. Mutates and returns it.

    Attributed across axes by the task's own weights rather than to a single
    "task type", because a prompt is rarely one thing and forcing it into one
    bucket throws away most of what was learned.
    """
    entry = stats.setdefault(agent, {})
    for axis in DIMENSIONS:
        weight = float(profile_weights.get(axis) or 0.0)
        # Only axes this task was substantially about. Crediting an agent for
        # security work because a task was 4% security would drown the signal.
        if weight < 0.10:
            continue
        axis_stats = entry.setdefault(axis, {"runs": 0, "ok": 0, "rolled_back": 0})
        axis_stats["runs"] = int(axis_stats.get("runs") or 0) + 1
        if ok:
            axis_stats["ok"] = int(axis_stats.get("ok") or 0) + 1
        if rolled_back:
            axis_stats["rolled_back"] = int(axis_stats.get("rolled_back") or 0) + 1
    return stats


# --------------------------------------------------------------------------
# Seating
# --------------------------------------------------------------------------


@dataclass
class Seat:
    """One chair on the council, as decided for a single run."""

    id: str            # stage id: "seat1".."seatN", or "chair"
    agent: str         # catalogued agent: codex | claude | agy | custom
    provider_id: str   # which configured provider supplies the command
    chairman: bool = False
    # The anonymous name this seat's work appears under during peer critique.
    # Assigned per run so "Agent A" is not always the same CLI.
    alias: str = ""
    persona: str = ""       # role template id, e.g. "security_review"
    persona_name: str = ""  # its display name
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    pinned: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "provider_id": self.provider_id,
            "chairman": self.chairman,
            "alias": self.alias,
            "persona": self.persona,
            "persona_name": self.persona_name,
            # Rounded, and never shown as a percentage. It orders the seats and
            # explains a choice; it is not a claim about the model.
            "score": round(self.score, 3),
            "reasons": self.reasons,
            "pinned": self.pinned,
        }


@dataclass
class Seating:
    """The full bench for one run: who deliberates, and who chairs."""

    members: List[Seat]
    chair: Seat
    profile: TaskProfile
    # Set when the router could not seat what was asked for and had to settle -
    # one CLI installed, everything pinned to the same agent, and so on.
    notes: List[str] = field(default_factory=list)

    @property
    def seats(self) -> List[Seat]:
        """Every seat, members first, chairman last."""
        return [*self.members, self.chair]

    def alias_map(self) -> Dict[str, str]:
        """Seat id -> anonymous alias, for the critique stage."""
        return {s.id: s.alias for s in self.members}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "members": [s.to_dict() for s in self.members],
            "chair": self.chair.to_dict(),
            "profile": self.profile.to_dict(),
            "notes": self.notes,
        }


# The anonymous names peers are shown under. Latin letters because the critique
# prompt refers to them in prose and "Agent A" reads as a participant in a way
# that "Model 1" does not.
ALIASES = tuple(chr(ord("A") + i) for i in range(26))

# Which persona a seat is given when the router picks one for it. Ordered by
# axis, and consulted only for axes this task actually loads - a task with no
# security content should not seat a Security Reviewer just to fill a chair.
PERSONA_FOR_AXIS: Dict[str, str] = {
    "security": "security_review",
    "review": "adversarial_review",
    "debugging": "adversarial_review",
    "implementation": "council_member",
    "architecture": "council_member",
    "analysis": "council_member",
}


def score_seat(
    agent: str,
    profile: TaskProfile,
    capabilities: Optional[Dict[str, Dict]] = None,
    stats: Optional[Dict[str, Any]] = None,
    quota_percent: Optional[float] = None,
) -> Tuple[float, List[str]]:
    """How well one agent fits this task, and why, in words.

    The score is the weighted average of the agent's capabilities under the
    task's weights, then adjusted by history and by quota pressure. The reasons
    are what the UI shows; the number only orders the list.
    """
    caps = capability_for(agent, capabilities)
    base = sum(profile.weights[axis] * caps[axis] for axis in DIMENSIONS)
    reasons: List[str] = []

    # Name the axes this agent is actually being chosen *for*: the ones the
    # task loads heavily and this agent happens to be strong on.
    strengths = [
        axis for axis in profile.dominant
        if caps[axis] >= 0.75
    ]
    if strengths:
        reasons.append("strong on " + ", ".join(strengths[:3]))

    adjustment, history_reason = score_history(stats, agent, profile)
    if history_reason:
        reasons.append(history_reason)

    quota_adjustment = 0.0
    if quota_percent is not None and quota_percent >= QUOTA_PRESSURE_PERCENT:
        # Linear from the pressure threshold to exhausted. A real reading from
        # the vendor, quoted as such - this app never computes a usage figure.
        span = max(1.0, 100.0 - QUOTA_PRESSURE_PERCENT)
        severity = min(1.0, (quota_percent - QUOTA_PRESSURE_PERCENT) / span)
        quota_adjustment = -QUOTA_WEIGHT * severity
        reasons.append(f"{quota_percent:.0f}% of its quota window used")

    return base + adjustment + quota_adjustment, reasons


def _persona_for(profile: TaskProfile, taken: Iterable[str]) -> Tuple[str, str]:
    """Pick a persona for a seat, preferring one this task calls for.

    ``taken`` is the personas already seated, so a three-agent council does not
    end up as three Adversarial Reviewers. Falls back to the plain council
    member, which is the neutral behaviour and always available.
    """
    taken = set(taken)
    for axis in profile.dominant:
        persona = PERSONA_FOR_AXIS.get(axis)
        if persona and persona not in taken and persona != "council_member":
            return persona, axis
    return "council_member", ""


def route(
    task: str,
    available: Sequence[str],
    *,
    seat_count: int = 3,
    chair_deliberates: bool = True,
    pins: Optional[Dict[str, str]] = None,
    provider_ids: Optional[Dict[str, str]] = None,
    capabilities: Optional[Dict[str, Dict]] = None,
    stats: Optional[Dict[str, Any]] = None,
    quota: Optional[Dict[str, Optional[float]]] = None,
    personas: Optional[Dict[str, str]] = None,
    run_id: str = "",
) -> Seating:
    """Seat the council for one prompt.

    ``available`` is the agents whose CLI actually resolves on this machine, in
    the order they should be tried when scores tie. Routing around an agent
    that is not installed is the difference between a council of two and a run
    that dies at the first seat.

    ``pins`` fixes a seat to an agent - ``{"chair": "claude"}`` - and the router
    fills what is left. A pinned seat is still profiled and still explains
    itself; it simply cannot be moved.

    ``personas`` likewise fixes a seat's behaviour; unpinned seats get one
    chosen from what the task loads.

    The chairman is picked first and from the whole field. It is the only seat
    that writes, so it must be the best available judge of the work rather than
    whoever is left over after the members are seated.

    ``chair_deliberates`` decides whether the chairman may also hold a member
    seat. With three CLIs installed it defaults to yes, because three
    independent positions are worth more than two and the alternative benches
    the strongest model for the whole of stage 1. The cost is real and is
    disclosed in ``notes``: the chairman wrote one of the answers it is
    weighing, and will recognise it however well anonymised it is. Turn it off
    to buy clean separation at the price of one fewer position.
    """
    pins = dict(pins or {})
    personas = dict(personas or {})
    provider_ids = dict(provider_ids or {})
    quota = dict(quota or {})
    profile = profile_task(task)
    notes: List[str] = []

    field_agents = [a for a in available if a]
    if not field_agents:
        raise ValueError(
            "No agent CLI is available, so there is nobody to seat. Check the "
            "agents in Settings."
        )

    scored: Dict[str, Tuple[float, List[str]]] = {
        agent: score_seat(agent, profile, capabilities, stats, quota.get(agent))
        for agent in field_agents
    }

    def rank(agents: Iterable[str]) -> List[str]:
        # Ties break on the declared order of `available`, which keeps the
        # seating stable for a prompt that carries no routing signal.
        return sorted(agents, key=lambda a: (-scored[a][0], field_agents.index(a)))

    # ---- the chairman ----------------------------------------------------
    chair_pin = pins.get("chair") or ""
    if chair_pin and chair_pin in scored:
        chair_agent, chair_pinned = chair_pin, True
    else:
        if chair_pin:
            notes.append(
                f"{chair_pin} is pinned to the chair but its CLI is not "
                f"available, so the chair was routed instead."
            )
        # Judging the work is what the chair does, whatever the task is about,
        # so its score is blended with a standing review/architecture bias.
        chair_profile = TaskProfile(
            weights={
                axis: 0.5 * profile.weights[axis]
                + 0.5 * (0.4 if axis in ("review", "architecture") else 0.05)
                for axis in DIMENSIONS
            },
            signals=profile.signals,
            length=profile.length,
        )
        chair_scores = {
            a: score_seat(a, chair_profile, capabilities, stats, quota.get(a))
            for a in field_agents
        }
        chair_agent = sorted(
            field_agents,
            key=lambda a: (-chair_scores[a][0], field_agents.index(a)),
        )[0]
        chair_pinned = False

    chair_score, chair_reasons = scored[chair_agent]
    chair = Seat(
        id="chair",
        agent=chair_agent,
        provider_id=provider_ids.get("chair", "chair"),
        chairman=True,
        persona="chairman",
        persona_name="Chairman",
        score=chair_score,
        reasons=(["pinned to the chair"] if chair_pinned else ["best placed to judge the work"]) + chair_reasons,
        pinned=chair_pinned,
    )

    # ---- the members -----------------------------------------------------
    # At least two: one agent deliberating alone and then critiquing itself is
    # not a council, and the peer-review stage would have no peers.
    wanted = max(2, int(seat_count or 3))

    # The pool the members are drawn from. Excluding the chair costs a position
    # and buys separation; the caller chose which, and either way the seating
    # says what it did.
    pool = list(field_agents)
    if not chair_deliberates:
        without_chair = [a for a in pool if a != chair_agent]
        if len(without_chair) >= 2:
            pool = without_chair
        else:
            notes.append(
                f"The chair was asked to sit out the deliberation, but that "
                f"would leave {len(without_chair)} member(s). It is holding a "
                f"seat as well so the council has peers to review."
            )
    # The ceiling is the bench that exists. Asking for six seats on a machine
    # with three CLIs installed buys three correlated answers at six times the
    # quota, so the request is reduced - and said out loud, because a Members
    # setting that quietly means something else is a setting nobody can trust.
    seated = min(wanted, max(2, len(pool)))
    if seated < wanted:
        notes.append(
            f"{wanted} members were asked for, but only {len(pool)} CLI(s) are "
            f"available to seat. The council is {seated}; installing another "
            f"agent is the only thing that widens it."
        )
    wanted = seated

    members: List[Seat] = []
    used: List[str] = []

    for index in range(wanted):
        seat_id = f"seat{index + 1}"
        pin = pins.get(seat_id) or ""
        if pin and pin in scored:
            agent, pinned = pin, True
        else:
            if pin:
                notes.append(
                    f"{pin} is pinned to seat {index + 1} but its CLI is not "
                    f"available, so that seat was routed instead."
                )
            # Prefer an agent not already deliberating: a council's value is
            # the spread of its opinions, and two seats on the same CLI with
            # the same model produce two correlated answers, not two votes.
            fresh = [a for a in pool if a not in used]
            agent = rank(fresh or pool)[0]
            pinned = False

        score, reasons = scored[agent]
        if agent in used:
            reasons = [*reasons, "seated twice - no other CLI is available"]
        used.append(agent)

        persona_id = personas.get(seat_id) or ""
        persona_axis = ""
        if not persona_id:
            persona_id, persona_axis = _persona_for(
                profile, [m.persona for m in members]
            )
        if persona_axis:
            reasons = [*reasons, f"seated as the {persona_axis} voice"]
        if not reasons and not pinned:
            # Every seat has to be able to say why it is there. A seat with
            # nothing to report is the ordinary case - the task carried no
            # signal, or this agent is simply the next one - and saying that
            # plainly beats a tooltip that opens empty.
            reasons = [
                "no strong signal in the task - seated to widen the bench"
                if not profile.dominant
                else "next best available for this task"
            ]

        members.append(
            Seat(
                id=seat_id,
                agent=agent,
                provider_id=provider_ids.get(seat_id, seat_id),
                persona=persona_id,
                score=score,
                reasons=(["pinned to this seat"] if pinned else []) + reasons,
                pinned=pinned,
            )
        )

    if len(set(used)) == 1 and len(field_agents) == 1:
        notes.append(
            f"Only {field_agents[0]} is installed, so the whole council is one "
            f"CLI. The stages still run, but the peer critique is that agent "
            f"reviewing its own work."
        )
    elif chair_agent in used:
        notes.append(
            f"{chair_agent} both holds a seat and chairs, so it will recognise "
            f"one of the positions it weighs as its own. Turn off “chair "
            f"deliberates” to seat it only as chairman."
        )

    # ---- anonymity -------------------------------------------------------
    # Aliases are shuffled per run so that "Agent A" is not permanently the
    # same CLI. Seeded by the run id: reproducible when replaying a transcript,
    # different between runs.
    letters = list(ALIASES[: len(members)])
    random.Random(run_id or "council").shuffle(letters)
    for seat, letter in zip(members, letters):
        seat.alias = f"Agent {letter}"

    return Seating(members=members, chair=chair, profile=profile, notes=notes)
