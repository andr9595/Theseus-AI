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

from aicouncil.config import ConfigStore  # noqa: E402
from aicouncil.usage import (  # noqa: E402
    UsagePoller,
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

    def test_codex_is_not(self):
        # `codex exec "/status"` sends the text to the model as a prompt: it
        # costs ~16k tokens and cannot answer. Polling it would spend the
        # quota it claims to measure.
        self.assertFalse(supports_usage({"command": ["codex", "exec", "{prompt}"]}))

    def test_an_unsupported_agent_reports_no_data_not_zero(self):
        reading = read_usage({"id": "drafter", "command": ["codex", "exec"]})
        self.assertFalse(reading.supported)
        self.assertEqual(reading.limits, [])
        self.assertIsNone(reading.worst)
        self.assertIn("quota", reading.note.lower())

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
        self.assertEqual(set(snap), {"drafter", "polisher"})

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


if __name__ == "__main__":
    unittest.main()
