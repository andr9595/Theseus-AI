"""Quota polling tests.

The property that matters: a percentage on screen is one the vendor reported.
An agent that cannot report quota must say so rather than show a number this
app inferred.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil import usage as usage_module  # noqa: E402
from aicouncil.config import ConfigStore  # noqa: E402
from aicouncil.usage import (  # noqa: E402
    UsagePoller,
    read_codex_usage,
    parse_usage,
    read_usage,
    supports_usage,
)

# Verbatim from `claude -p "/usage"` on 2026-07-25.
REAL_OUTPUT = """You are currently using your subscription to power your Claude Code usage

Current session: 20% used · resets Jul 25, 10:49pm (Europe/Copenhagen)
Current week (all models): 21% used · resets Jul 31, 1:59am (Europe/Copenhagen)

What's contributing to your limits usage?
Approximate, based on local sessions on this machine

Last 24h · 475 requests · 14 sessions
  84% of your usage was at >150k context
"""


class TestParsing(unittest.TestCase):
    def test_parses_the_real_cli_output(self):
        limits = parse_usage(REAL_OUTPUT)
        self.assertEqual(len(limits), 2)
        self.assertEqual(limits[0].label, "session")
        self.assertEqual(limits[0].percent, 20.0)
        self.assertIn("Jul 25", limits[0].resets)
        self.assertEqual(limits[1].label, "week (all models)")
        self.assertEqual(limits[1].percent, 21.0)

    def test_ignores_the_percentages_in_the_prose(self):
        # "84% of your usage was at >150k context" is not a quota limit; a
        # looser regex would report it as one and pin the chip at 84%.
        for limit in parse_usage(REAL_OUTPUT):
            self.assertNotEqual(limit.percent, 84.0)

    def test_picks_up_a_limit_shape_not_seen_before(self):
        # A plan with an extra window should appear without a code change.
        limits = parse_usage(
            "Current week (Opus): 63% used · resets Aug 2, 9:00am\n"
        )
        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0].label, "week (Opus)")
        self.assertEqual(limits[0].percent, 63.0)

    def test_handles_a_limit_with_no_reset_clause(self):
        limits = parse_usage("Current session: 5% used\n")
        self.assertEqual(limits[0].percent, 5.0)
        self.assertEqual(limits[0].resets, "")

    def test_accepts_a_fractional_percentage(self):
        self.assertEqual(parse_usage("Current session: 7.5% used")[0].percent, 7.5)

    def test_unrelated_text_yields_nothing(self):
        self.assertEqual(parse_usage("Hello, world.\nNothing here.\n"), [])
        self.assertEqual(parse_usage(""), [])


class TestSupport(unittest.TestCase):
    def test_claude_is_supported(self):
        self.assertTrue(supports_usage({"command": ["claude", "-p", "{prompt}"]}))
        self.assertTrue(
            supports_usage({"command": ["/home/me/.local/bin/claude", "-p"]})
        )

    def test_codex_is_supported_via_its_session_logs(self):
        # `codex exec "/status"` cannot answer - it sends the text to the model
        # as a prompt. But the CLI writes the server's rate-limit headers into
        # its rollout logs, which is free to read. See TestCodexUsage.
        self.assertTrue(supports_usage({"command": ["codex", "exec", "{prompt}"]}))

    def test_an_agent_with_no_known_source_says_so_rather_than_zero(self):
        reading = read_usage({"id": "x", "command": ["some-other-agent", "run"]})
        self.assertFalse(reading.supported)
        self.assertEqual(reading.limits, [])
        self.assertIsNone(reading.worst)
        self.assertIn("no quota source", reading.note.lower())

    def test_a_missing_binary_is_an_error_not_a_reading(self):
        reading = read_usage({"id": "x", "command": ["claude-not-installed-xyz"]})
        self.assertEqual(reading.limits, [])
        self.assertTrue(reading.error)


class TestWorstLimit(unittest.TestCase):
    def test_worst_is_the_one_closest_to_exhaustion(self):
        from aicouncil.usage import Limit, UsageReading

        r = UsageReading(
            provider_id="polisher",
            limits=[Limit("session", 20.0), Limit("week (all models)", 88.0)],
        )
        self.assertEqual(r.worst.percent, 88.0)
        self.assertEqual(r.worst.label, "week (all models)")

    def test_no_limits_means_no_worst(self):
        from aicouncil.usage import UsageReading

        self.assertIsNone(UsageReading(provider_id="x").worst)


class TestPoller(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-usage-"))
        self.store = ConfigStore(self.tmp / "config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_poll_records_a_reading_per_provider(self):
        poller = UsagePoller(self.store)
        snap = poller.poll_once()
        # One entry per configured chair, whichever chairs exist - including
        # Projects Mode's three, which share their binaries with the council's
        # and are therefore answered from one probe apiece.
        self.assertEqual(set(snap), set(self.store.get("providers", {})))
        self.assertLessEqual({"drafter", "polisher", "solo"}, set(snap))
        self.assertLessEqual({"architect", "coder", "qa"}, set(snap))

    def test_chairs_sharing_a_binary_are_probed_once(self):
        # Six providers are two or three CLIs. `claude -p /usage` costs a
        # process launch and up to 45 seconds, so asking per chair would have
        # the poller spend three of those to learn one number.
        calls = []
        real = usage_module.read_usage

        def counting(provider, cwd=None):
            calls.append(str((provider.get("command") or [""])[0]))
            return real(provider, cwd=cwd)

        usage_module.read_usage = counting
        try:
            snap = UsagePoller(self.store).poll_once()
        finally:
            usage_module.read_usage = real

        self.assertEqual(len(calls), len(set(calls)))
        # Every chair still gets an answer, and it is filed under its own id.
        for pid, reading in snap.items():
            self.assertEqual(reading["provider_id"], pid)

    def test_interval_has_a_floor(self):
        # A mistyped 1 would spawn a process every second forever.
        self.store.update({"usage_poll_seconds": 1})
        self.assertGreaterEqual(UsagePoller(self.store)._interval(), 60.0)

    def test_interval_survives_a_junk_value(self):
        self.store.update({"usage_poll_seconds": "soon"})
        self.assertGreaterEqual(UsagePoller(self.store)._interval(), 60.0)

    def test_a_failed_poll_keeps_the_previous_numbers(self):
        from aicouncil.usage import Limit, UsageReading

        poller = UsagePoller(self.store)
        poller._readings["polisher"] = UsageReading(
            provider_id="polisher", limits=[Limit("session", 42.0)], checked_at=1.0
        )
        # Point the agent at a binary that cannot exist, then poll.
        self.store.update({
            "providers": {"polisher": {"command": ["claude-gone-xyz", "-p"]}}
        })
        poller.poll_once()
        reading = poller._readings["polisher"]
        self.assertEqual(reading.worst.percent, 42.0, "stale numbers were discarded")
        self.assertTrue(reading.error, "the failure should still be visible")

    def test_worst_percent_is_none_when_unknown(self):
        self.assertIsNone(UsagePoller(self.store).worst_percent("polisher"))



class TestCodexUsage(unittest.TestCase):
    """Codex's quota comes from the rate-limit headers it logs per run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aicouncil-codex-"))
        self.sessions = self.tmp / "sessions" / "2026" / "07" / "25"
        self.sessions.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rollout(self, name, rate_limits):
        import json as _json
        path = self.sessions / name
        path.write_text(
            _json.dumps({"type": "event_msg", "payload": {"other": 1}}) + "\n"
            + _json.dumps({"type": "event_msg",
                           "payload": {"rate_limits": rate_limits}}) + "\n"
        )
        return path

    def test_reads_the_real_payload_shape(self):
        # Verbatim from a rollout log on 2026-07-25.
        self._rollout("a.jsonl", {
            "limit_id": "codex", "limit_name": None, "plan_type": "plus",
            "primary": {"used_percent": 1.0, "window_minutes": 10080,
                        "resets_at": 1785544255},
            "secondary": None,
        })
        r = read_codex_usage("drafter", home=str(self.tmp))
        self.assertTrue(r.supported)
        self.assertEqual(len(r.limits), 1)
        self.assertEqual(r.limits[0].percent, 1.0)
        self.assertEqual(r.limits[0].label, "week")   # 10080 min = 7 days
        self.assertIn("plus", r.note)
        self.assertTrue(r.limits[0].as_of, "reading must carry its age")

    def test_both_windows_are_reported(self):
        self._rollout("b.jsonl", {
            "primary": {"used_percent": 12.0, "window_minutes": 10080},
            "secondary": {"used_percent": 71.5, "window_minutes": 300},
        })
        r = read_codex_usage("drafter", home=str(self.tmp))
        self.assertEqual(len(r.limits), 2)
        self.assertEqual(r.worst.percent, 71.5)
        self.assertEqual(r.worst.label, "5-hour window")

    def test_newest_log_wins(self):
        import os as _os, time as _time
        old = self._rollout("old.jsonl",
                            {"primary": {"used_percent": 5.0, "window_minutes": 10080}})
        new = self._rollout("new.jsonl",
                            {"primary": {"used_percent": 40.0, "window_minutes": 10080}})
        _os.utime(old, (1, 1))
        _os.utime(new, (_time.time(), _time.time()))
        self.assertEqual(read_codex_usage("drafter", home=str(self.tmp)).worst.percent, 40.0)

    def test_last_record_in_a_file_wins(self):
        import json as _json
        (self.sessions / "c.jsonl").write_text(
            _json.dumps({"payload": {"rate_limits": {"primary": {"used_percent": 3.0, "window_minutes": 10080}}}}) + "\n"
            + _json.dumps({"payload": {"rate_limits": {"primary": {"used_percent": 9.0, "window_minutes": 10080}}}}) + "\n"
        )
        self.assertEqual(read_codex_usage("drafter", home=str(self.tmp)).worst.percent, 9.0)

    def test_no_logs_explains_itself_without_inventing_a_number(self):
        empty = Path(tempfile.mkdtemp(prefix="aicouncil-empty-"))
        try:
            r = read_codex_usage("drafter", home=str(empty))
            self.assertEqual(r.limits, [])
            self.assertIsNone(r.worst)
            self.assertIn("first Codex run", r.note)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_a_corrupt_log_is_skipped_not_fatal(self):
        (self.sessions / "bad.jsonl").write_text('{"payload": {"rate_limits": ')
        r = read_codex_usage("drafter", home=str(self.tmp))
        self.assertEqual(r.limits, [])   # nothing usable, but no exception

    def test_codex_is_now_a_supported_source(self):
        self.assertTrue(supports_usage({"command": ["codex", "exec", "{prompt}"]}))

if __name__ == "__main__":
    unittest.main()


class TestPrimaryLimit(unittest.TestCase):
    """The chip leads with the window that bites first, not the fullest one.

    Claude's 5-hour session is what actually stops work mid-afternoon; the
    weekly moves slowly. Codex reports only a weekly, so that is its lead.
    """

    def _claude(self, session_pct, week_pct):
        from aicouncil.usage import UsageReading

        return UsageReading(
            provider_id="polisher",
            limits=parse_usage(
                f"Current session: {session_pct}% used · resets Jul 25, 10:49pm\n"
                f"Current week (all models): {week_pct}% used · resets Jul 31, 2am\n"
            ),
        )

    def test_claude_leads_with_the_five_hour_session(self):
        r = self._claude(38, 22)
        self.assertEqual(r.primary.label, "session")
        self.assertEqual(r.primary.percent, 38.0)

    def test_the_session_leads_even_when_the_week_is_fuller(self):
        # This is the whole point: a 91% weekly must not displace the session
        # number, but it must still be visible.
        r = self._claude(12, 91)
        self.assertEqual(r.primary.label, "session")
        self.assertEqual(r.primary.percent, 12.0)
        self.assertEqual(r.worst.label, "week (all models)")
        self.assertEqual(r.worst.percent, 91.0)

    def test_the_weekly_is_still_reported(self):
        r = self._claude(38, 22)
        labels = [l.label for l in r.limits]
        self.assertIn("week (all models)", labels)
        self.assertEqual(len(r.limits), 2)

    def test_window_lengths_are_inferred_from_claude_labels(self):
        from aicouncil.usage import window_for_label

        self.assertEqual(window_for_label("session"), 300.0)
        self.assertEqual(window_for_label("week (all models)"), 10080.0)
        self.assertEqual(window_for_label("week (Opus)"), 10080.0)
        self.assertIsNone(window_for_label("something new"))

    def test_a_shorter_window_would_take_the_lead_automatically(self):
        # Ranking by length rather than matching the word "session" means a
        # plan that gains an hourly window is handled without a code change.
        from aicouncil.usage import Limit, UsageReading

        r = UsageReading(
            provider_id="polisher",
            limits=[
                Limit("session", 40.0, window_minutes=300.0),
                Limit("hour", 5.0, window_minutes=60.0),
            ],
        )
        self.assertEqual(r.primary.label, "hour")

    def test_codex_leads_with_its_weekly(self):
        from aicouncil.usage import Limit, UsageReading

        r = UsageReading(
            provider_id="drafter",
            limits=[Limit("week", 1.0, window_minutes=10080.0)],
        )
        self.assertEqual(r.primary.label, "week")

    def test_unknown_windows_fall_back_to_the_worst(self):
        from aicouncil.usage import Limit, UsageReading

        r = UsageReading(
            provider_id="x",
            limits=[Limit("odd", 10.0), Limit("stranger", 60.0)],
        )
        self.assertEqual(r.primary.percent, 60.0)

    def test_both_limits_reach_the_ui_payload(self):
        payload = self._claude(12, 91).to_dict()
        self.assertEqual(payload["primary"]["label"], "session")
        self.assertEqual(payload["worst"]["label"], "week (all models)")
        self.assertEqual(len(payload["limits"]), 2)
        self.assertEqual(payload["primary"]["window_minutes"], 300.0)
