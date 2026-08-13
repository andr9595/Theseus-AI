#!/usr/bin/env python3
"""What one council run actually cost, per seat, from the vendors' own logs.

This app never computes a usage figure of its own - see `usage.py` - and the
CLIs it drives do not report their token spend back through the stream it
reads, so a finished transcript records how long each seat took and nothing
about what it burned. That is enough to know a run was slow and not enough to
know which seat to change.

So this reads the two logs the vendors already write, joins them to a
transcript by working folder and wall-clock window, and prints the run seat by
seat. It opens nothing for writing.

    python3 scripts/council-cost.py                # the most recent run
    python3 scripts/council-cost.py 1785926593-5d2a52953401
    python3 scripts/council-cost.py --json

One trap, and it is worth stating because getting it wrong doubles every
Claude figure: `~/.claude/projects/**/*.jsonl` writes one record per content
block, and every block of one assistant message repeats that message's whole
`usage` object. Summing the file line by line counts a reply that thought and
then called two tools three times over. The join is on `message.id`, which is
what makes a request a request.

Codex needs no such care: `~/.codex/sessions/**/*.jsonl` reports one running
total per session, and the last one it wrote is the session's total.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil import config as cfg  # noqa: E402


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

# A cache read is not a fresh token and is not billed as one. No vendor here
# publishes the weighting a subscription quota actually applies, so the raw
# volume is reported as raw volume and the weighted figure is labelled as the
# public API's ratios rather than as this account's bill.
CACHE_READ_WEIGHT = 0.1
CACHE_WRITE_WEIGHT = 1.25


def _iso_to_epoch(stamp: str) -> float:
    """Epoch seconds from a log's ISO timestamp, or 0.0 if it is unreadable."""
    try:
        return datetime.fromisoformat(
            str(stamp).replace("Z", "+00:00")
        ).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


# -- the vendors' logs ------------------------------------------------------

def _claude_sessions(workspace: str) -> List[Dict[str, Any]]:
    """Every Claude session logged for this working folder, with its totals.

    The slug is the folder with every separator replaced by a dash, which is
    how the CLI names its own project directory. Sessions are summed whole and
    matched by when they opened, not by which of their records fall inside a
    stage's window: a stage is recorded as finished when its answer arrives,
    and the CLI can still flush a record after that. Splitting on the window
    hands that record to whichever stage started next.
    """
    folder = CLAUDE_PROJECTS / ("-" + str(workspace).strip("/").replace("/", "-"))
    if not folder.is_dir():
        return []

    sessions: List[Dict[str, Any]] = []
    for path in sorted(folder.glob("*.jsonl")):
        opened = 0.0
        seen: Dict[str, Dict[str, Any]] = {}  # message.id -> usage, deduped
        for record in _read_jsonl(path):
            at = _iso_to_epoch(record.get("timestamp", ""))
            if at and (not opened or at < opened):
                opened = at
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if isinstance(usage, dict):
                seen[str(message.get("id") or f"{path.name}:{at}")] = usage
        if not seen or not opened:
            # No usage at all: the five-minute `claude -p /usage` poll opens a
            # session here too, and spends nothing.
            continue
        total = lambda key: sum(  # noqa: E731
            int(u.get(key) or 0) for u in seen.values()
        )
        sessions.append({
            "opened": opened,
            "requests": len(seen),
            "fresh_input": total("input_tokens"),
            "cache_write": total("cache_creation_input_tokens"),
            "cache_read": total("cache_read_input_tokens"),
            "output": total("output_tokens"),
        })
    return sessions


def _codex_sessions(workspace: str) -> List[Dict[str, Any]]:
    """The same for Codex, which reports a running total rather than per-call.

    The answer for a session is therefore the last total it wrote, not a sum
    over its records, and there is no request count to report.
    """
    if not CODEX_SESSIONS.is_dir():
        return []

    sessions: List[Dict[str, Any]] = []
    for path in sorted(CODEX_SESSIONS.rglob("rollout-*.jsonl")):
        records = _read_jsonl(path)
        if not records:
            continue
        meta = records[0].get("payload")
        cwd = str(meta.get("cwd") or "") if isinstance(meta, dict) else ""
        if cwd and Path(cwd) != Path(workspace):
            continue
        latest: Optional[Dict[str, Any]] = None
        for record in records:
            payload = record.get("payload")
            info = payload.get("info") if isinstance(payload, dict) else None
            if isinstance(info, dict) and isinstance(
                info.get("total_token_usage"), dict
            ):
                latest = info["total_token_usage"]
        if latest is None:
            continue
        cached = int(latest.get("cached_input_tokens") or 0)
        sessions.append({
            "opened": _iso_to_epoch(records[0].get("timestamp", "")),
            "requests": 0,
            "fresh_input": max(0, int(latest.get("input_tokens") or 0) - cached),
            "cache_write": int(latest.get("cache_write_input_tokens") or 0),
            "cache_read": cached,
            "output": int(latest.get("output_tokens") or 0),
        })
    return sessions


SESSIONS = {"claude": _claude_sessions, "codex": _codex_sessions}


# -- the report -------------------------------------------------------------

def _raw(usage: Dict[str, Any]) -> int:
    return (
        usage["fresh_input"] + usage["cache_write"] + usage["cache_read"]
    )


def _weighted(usage: Dict[str, Any]) -> int:
    return int(
        usage["fresh_input"]
        + CACHE_WRITE_WEIGHT * usage["cache_write"]
        + CACHE_READ_WEIGHT * usage["cache_read"]
    )


def collect(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One row per stage of the run, in the order the run ran them.

    A stage claims the session its own CLI opened while it was running. The
    matching is done here rather than per stage, and each session is claimed
    once, because the tolerance either end of a window is wide enough that a
    session opening on a stage boundary would otherwise be counted twice - the
    critique's, handed to the position that had just finished.
    """
    workspace = str(run.get("workspace") or "")
    stages = run.get("stages") or {}
    order = run.get("stage_order") or list(stages)

    rows: List[Dict[str, Any]] = []
    for stage_id in order:
        stage = stages.get(stage_id) or {}
        started = float(stage.get("started_at") or 0)
        ended = float(stage.get("ended_at") or 0)
        rows.append({
            "stage": stage_id,
            "agent": str(stage.get("agent") or ""),
            "kind": str(stage.get("kind") or ""),
            "state": str(stage.get("state") or ""),
            "model": str(stage.get("model") or ""),
            "effort": str(stage.get("effort") or ""),
            "seconds": round(ended - started, 1) if started and ended else 0.0,
            "started": started,
            "ended": ended,
            "usage": None,
        })

    unclaimed: Dict[str, List[Dict[str, Any]]] = {}
    for row in sorted(rows, key=lambda r: r["started"]):
        agent = row["agent"]
        if agent not in SESSIONS or not row["started"] or not row["ended"]:
            continue
        if agent not in unclaimed:
            unclaimed[agent] = sorted(
                SESSIONS[agent](workspace), key=lambda s: s["opened"]
            )
        for session in unclaimed[agent]:
            # Opened after this stage began - a couple of seconds of clock
            # skew allowed - and before its answer came back.
            if row["started"] - 5 <= session["opened"] <= row["ended"]:
                row["usage"] = {
                    k: v for k, v in session.items() if k != "opened"
                }
                unclaimed[agent].remove(session)
                break
    return rows


def _thousands(value: int) -> str:
    return f"{value:,}"


def report(run: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    task = " ".join(str(run.get("task") or "").split())
    lines.append(f"run     {run.get('id')}")
    lines.append(f"task    {task[:70]}{'…' if len(task) > 70 else ''}")
    lines.append("")

    header = (
        f"{'stage':<16}{'agent':<8}{'effort':<8}{'reqs':>6}"
        f"{'raw in':>12}{'weighted':>11}{'out':>9}{'secs':>8}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    per_agent: Dict[str, Dict[str, int]] = {}
    unmeasured: List[str] = []
    for row in rows:
        usage = row["usage"]
        if usage is None:
            if row["state"] == "done":
                unmeasured.append(f"{row['stage']} ({row['agent'] or '?'})")
            lines.append(
                f"{row['stage']:<16}{row['agent']:<8}{row['effort']:<8}"
                f"{'—':>6}{'—':>12}{'—':>11}{'—':>9}{row['seconds']:>8.0f}"
            )
            continue
        bucket = per_agent.setdefault(
            row["agent"], {"requests": 0, "raw": 0, "weighted": 0, "output": 0}
        )
        bucket["requests"] += usage["requests"]
        bucket["raw"] += _raw(usage)
        bucket["weighted"] += _weighted(usage)
        bucket["output"] += usage["output"]
        lines.append(
            f"{row['stage']:<16}{row['agent']:<8}{row['effort']:<8}"
            f"{usage['requests'] or '—':>6}"
            f"{_thousands(_raw(usage)):>12}{_thousands(_weighted(usage)):>11}"
            f"{_thousands(usage['output']):>9}{row['seconds']:>8.0f}"
        )

    lines.append("")
    total_raw = sum(b["raw"] for b in per_agent.values()) or 1
    for agent, bucket in sorted(
        per_agent.items(), key=lambda kv: -kv[1]["raw"]
    ):
        share = 100.0 * bucket["raw"] / total_raw
        lines.append(
            f"{agent:<16}{bucket['requests'] or '—':>6} requests  "
            f"{_thousands(bucket['raw']):>12} raw in  "
            f"{_thousands(bucket['weighted']):>11} weighted  "
            f"{_thousands(bucket['output']):>9} out  {share:5.1f}% of measured"
        )

    # The question this script was written for: what does the chair's second
    # seat cost? Only answerable when one agent holds more than one stage.
    doubled = [
        agent for agent in per_agent
        if len([r for r in rows if r["agent"] == agent and r["usage"]]) > 1
        and any(r["agent"] == agent and r["kind"] == "chair" for r in rows)
    ]
    for agent in doubled:
        extra_rows = [
            r for r in rows
            if r["agent"] == agent and r["usage"] and r["kind"] != "chair"
        ]
        if not extra_rows:
            continue
        extra_raw = sum(_raw(r["usage"]) for r in extra_rows)
        extra_weighted = sum(_weighted(r["usage"]) for r in extra_rows)
        agent_raw = per_agent[agent]["raw"] or 1
        lines.append("")
        lines.append(
            f"{agent} chaired and also sat: "
            f"{_thousands(extra_raw)} raw / {_thousands(extra_weighted)} "
            f"weighted input on top of chairing, "
            f"{100.0 * extra_raw / agent_raw:.0f}% of its own spend, across "
            f"{', '.join(r['stage'] for r in extra_rows)}."
        )

    if unmeasured:
        lines.append("")
        lines.append(
            "No usage log for " + ", ".join(unmeasured) + ". That CLI either "
            "writes none this script can read or ran outside the window; the "
            "shares above are of what was measured, not of the whole run."
        )
    return "\n".join(lines)


def load_run(argument: str) -> Dict[str, Any]:
    if argument:
        path = Path(argument)
        if not path.is_file():
            path = cfg.runs_dir() / f"{argument}.json"
        if not path.is_file():
            raise SystemExit(f"No transcript at {path}.")
    else:
        transcripts = sorted(
            cfg.runs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime
        )
        if not transcripts:
            raise SystemExit(f"No transcripts in {cfg.runs_dir()}.")
        path = transcripts[-1]
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report one council run's token spend, seat by seat."
    )
    parser.add_argument(
        "run", nargs="?", default="",
        help="run id or transcript path; the most recent run by default",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the rows as JSON instead",
    )
    args = parser.parse_args(argv)

    run = load_run(args.run)
    rows = collect(run)
    if args.json:
        print(json.dumps({"run": run.get("id"), "stages": rows}, indent=2))
    else:
        print(report(run, rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
