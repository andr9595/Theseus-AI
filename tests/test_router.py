"""The seating router, and the confidence contract it does not touch.

Two things are being protected here. The first is that routing is *about the
prompt*: a security question and a rename must not seat the same bench, or the
whole mechanism is decoration. The second is that nothing in this app invents a
confidence figure - the parser returns None for an agent that gave none, and
None has to survive all the way to the UI rather than being helpfully defaulted
somewhere in between.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicouncil import prompts, router  # noqa: E402

ALL = ["codex", "claude", "agy"]


class TestTaskProfile(unittest.TestCase):
    def test_a_prompt_with_no_signal_has_no_dominant_axis(self):
        # The right behaviour for "have a look at this" is to route on nothing
        # and let the tie-break decide, not to pick an axis confidently.
        profile = router.profile_task("hello")
        self.assertEqual(profile.dominant, [])
        self.assertAlmostEqual(sum(profile.weights.values()), 1.0, places=6)

    def test_weights_always_sum_to_one(self):
        for task in ("", "fix the crash", "a" * 5000, "?" * 40):
            profile = router.profile_task(task)
            self.assertAlmostEqual(sum(profile.weights.values()), 1.0, places=6)

    def test_the_axes_a_prompt_is_about_come_out_on_top(self):
        cases = {
            "is this JWT auth check exploitable by an attacker?": "security",
            "implement a --json flag on the status command": "implementation",
            "should we restructure this module?": "architecture",
        }
        for task, axis in cases.items():
            self.assertIn(axis, router.profile_task(task).dominant, task)

    def test_a_traceback_outweighs_any_single_keyword(self):
        # Structure is far better evidence than vocabulary: "error" appears in
        # every request to *add* error handling, and a traceback does not.
        profile = router.profile_task(
            "Traceback (most recent call last):\n  File 'x.py', line 3\nValueError"
        )
        self.assertEqual(profile.dominant[0], "debugging")
        self.assertIn("traceback", profile.signals)

    def test_one_keyword_never_zeroes_the_other_axes(self):
        # Without smoothing, a prompt whose only match is "fix" would score 1.0
        # debugging and 0.0 everywhere else - and an agent strong at everything
        # except debugging would score zero, which is not what one word means.
        profile = router.profile_task("fix")
        self.assertTrue(all(w > 0 for w in profile.weights.values()))


class TestSeating(unittest.TestCase):
    def test_the_bench_changes_with_the_prompt(self):
        security = router.route("is this auth check exploitable?", ALL, run_id="a")
        build = router.route("add a --json flag to the CLI", ALL, run_id="a")
        self.assertNotEqual(
            [s.persona for s in security.members],
            [s.persona for s in build.members],
            "the same council was seated for two unrelated tasks",
        )

    def test_a_security_prompt_seats_a_security_voice(self):
        seating = router.route("is this auth check exploitable?", ALL, run_id="a")
        self.assertIn(
            "security_review", [s.persona for s in seating.members]
        )

    def test_every_seat_explains_itself(self):
        seating = router.route("refactor the pipeline module", ALL, run_id="a")
        for seat in seating.seats:
            self.assertTrue(seat.reasons, f"{seat.id} gave no reason")

    def test_aliases_are_unique_and_hide_the_cli(self):
        seating = router.route("anything at all", ALL, run_id="a")
        aliases = [s.alias for s in seating.members]
        self.assertEqual(len(aliases), len(set(aliases)))
        for seat in seating.members:
            self.assertNotIn(seat.agent, seat.alias.lower())

    def test_aliases_are_stable_for_a_run_and_vary_between_runs(self):
        # Stable so replaying a transcript reads the same way; varying so
        # "Agent A" is not permanently the same CLI.
        first = router.route("x", ALL, run_id="run-one")
        again = router.route("x", ALL, run_id="run-one")
        other = router.route("x", ALL, run_id="run-two")
        self.assertEqual(
            [s.alias for s in first.members], [s.alias for s in again.members]
        )
        self.assertNotEqual(
            [s.alias for s in first.members], [s.alias for s in other.members]
        )

    def test_seats_prefer_distinct_agents(self):
        seating = router.route("anything", ALL, run_id="a", seat_count=3)
        agents = [s.agent for s in seating.members]
        self.assertEqual(len(set(agents)), 3, "a CLI was seated twice needlessly")

    def test_a_pin_is_honoured_and_says_so(self):
        seating = router.route(
            "anything", ALL, run_id="a", pins={"chair": "codex", "seat1": "agy"}
        )
        self.assertEqual(seating.chair.agent, "codex")
        self.assertTrue(seating.chair.pinned)
        self.assertEqual(seating.members[0].agent, "agy")
        self.assertTrue(seating.members[0].pinned)

    def test_a_pin_on_a_missing_cli_is_routed_around_and_reported(self):
        seating = router.route("anything", ["claude"], run_id="a",
                               pins={"chair": "codex"})
        self.assertEqual(seating.chair.agent, "claude")
        self.assertFalse(seating.chair.pinned)
        self.assertTrue(any("not available" in n for n in seating.notes))

    def test_a_council_never_falls_below_two_members(self):
        # One member has no peer to review, and the critique stage would have
        # nothing to do.
        seating = router.route("anything", ALL, run_id="a", seat_count=1)
        self.assertGreaterEqual(len(seating.members), 2)

    def test_one_installed_cli_still_seats_a_council_and_says_what_it_cost(self):
        seating = router.route("anything", ["claude"], run_id="a")
        self.assertTrue(seating.members)
        self.assertTrue(any("one CLI" in n for n in seating.notes))

    def test_no_installed_cli_refuses_rather_than_seating_nobody(self):
        with self.assertRaises(ValueError):
            router.route("anything", [], run_id="a")

    def test_a_chair_that_also_deliberates_is_disclosed(self):
        seating = router.route("anything", ALL, run_id="a", chair_deliberates=True)
        self.assertIn(seating.chair.agent, [s.agent for s in seating.members])
        self.assertTrue(any("holds a seat and chairs" in n for n in seating.notes))

    def test_a_chair_can_be_kept_out_of_the_deliberation(self):
        seating = router.route("anything", ALL, run_id="a", chair_deliberates=False)
        self.assertNotIn(seating.chair.agent, [s.agent for s in seating.members])

    def test_manual_routing_ignores_the_prompt(self):
        # Routing on an empty task is how the pipeline expresses "manual": every
        # agent scores its own mean, so the pins and the declared order decide.
        security = router.route("", ALL, run_id="a")
        build = router.route("", ALL, run_id="a")
        self.assertEqual(
            [s.agent for s in security.members], [s.agent for s in build.members]
        )

    def test_an_edited_capability_profile_changes_the_seating(self):
        # The operator's own reading of what a CLI is good at has to actually
        # move the router, or the panel is a decoration.
        task = "is this auth check exploitable?"
        before = router.route(task, ALL, run_id="a", chair_deliberates=False)
        after = router.route(
            task, ALL, run_id="a", chair_deliberates=False,
            capabilities={
                "claude": {d: 0.0 for d in router.DIMENSIONS},
                "codex": {d: 1.0 for d in router.DIMENSIONS},
            },
        )
        self.assertNotEqual(before.chair.agent, after.chair.agent)
        self.assertEqual(after.chair.agent, "codex")

    def test_quota_pressure_is_reported_in_the_reasons(self):
        seating = router.route(
            "anything", ALL, run_id="a", quota={"claude": 96.0}
        )
        claude = [s for s in seating.seats if s.agent == "claude"]
        self.assertTrue(claude)
        self.assertTrue(
            any("quota window used" in r for s in claude for r in s.reasons)
        )

    def test_a_pinned_persona_beats_the_one_the_task_would_choose(self):
        # The operator's behaviour for a seat has to win, or the picker on the
        # seat is a suggestion box.
        task = "is this auth check exploitable?"
        routed = router.route(task, ALL, run_id="a")
        self.assertEqual(routed.members[0].persona, "security_review")

        pinned = router.route(task, ALL, run_id="a", personas={"seat1": "visionary"})
        self.assertEqual(pinned.members[0].persona, "visionary")

    def test_clearing_a_pin_routes_the_seat_again(self):
        # An empty string is how the UI says "Auto". The config is deep-merged
        # on save, so unpinning by dropping the key would keep the old pin -
        # the panel writes a blank instead, and the router has to read that as
        # unpinned rather than as an agent named "".
        seating = router.route(
            "anything", ALL, run_id="a", pins={"chair": "", "seat1": ""}
        )
        self.assertFalse(seating.chair.pinned)
        self.assertIn(seating.chair.agent, ALL)
        self.assertFalse(seating.members[0].pinned)
        self.assertFalse([n for n in seating.notes if "is pinned" in n])


class TestHistoryFeedback(unittest.TestCase):
    def test_history_is_recorded_only_against_the_axes_that_applied(self):
        stats = {}
        weights = {d: 0.0 for d in router.DIMENSIONS}
        weights["security"] = 0.95
        weights["review"] = 0.05
        router.record_outcome(stats, "codex", weights, ok=True)
        self.assertIn("security", stats["codex"])
        # A task that was 5% about reviewing was not a reviewing task, and
        # crediting the agent for one would drown the axis that mattered.
        self.assertNotIn("review", stats["codex"])

    def test_a_good_record_helps_and_a_bad_one_hurts(self):
        weights = {d: 1.0 / len(router.DIMENSIONS) for d in router.DIMENSIONS}
        good, bad = {}, {}
        for _ in range(10):
            router.record_outcome(good, "codex", weights, ok=True)
            router.record_outcome(bad, "codex", weights, ok=False)

        profile = router.profile_task("anything")
        better, _ = router.score_seat("codex", profile, stats=good)
        worse, _ = router.score_seat("codex", profile, stats=bad)
        self.assertGreater(better, worse)

    def test_the_feedback_term_is_bounded(self):
        # A handful of runs is not a benchmark sweep. Two bad afternoons must
        # not be able to bench an agent permanently.
        weights = {d: 1.0 / len(router.DIMENSIONS) for d in router.DIMENSIONS}
        stats = {}
        for _ in range(500):
            router.record_outcome(stats, "codex", weights, ok=False)
        profile = router.profile_task("anything")
        adjustment, _ = router.score_history(stats, "codex", profile)
        self.assertGreaterEqual(adjustment, -router.HISTORY_WEIGHT - 1e-9)

    def test_a_rollback_counts_against_the_seat_that_wrote(self):
        weights = {d: 1.0 / len(router.DIMENSIONS) for d in router.DIMENSIONS}
        stats = {}
        router.record_outcome(stats, "claude", weights, ok=False, rolled_back=True)
        entry = stats["claude"]["implementation"]
        self.assertEqual(entry["rolled_back"], 1)
        self.assertEqual(entry["ok"], 0)

    def test_an_unknown_agent_scores_the_middle_rather_than_last(self):
        profile = router.profile_task("anything")
        mine, _ = router.score_seat("something-nobody-has-heard-of", profile)
        flat, _ = router.score_seat("custom", profile)
        self.assertAlmostEqual(mine, flat, places=6)


class TestConfidenceContract(unittest.TestCase):
    """The one number in this feature that must never be invented."""

    def test_a_stated_confidence_is_read_back(self):
        self.assertEqual(prompts.parse_confidence("CONFIDENCE: 62"), 62)
        self.assertEqual(prompts.parse_confidence("confidence:  0"), 0)
        self.assertEqual(prompts.parse_confidence("CONFIDENCE: 100"), 100)

    def test_an_unstated_confidence_is_none_and_not_a_default(self):
        for text in ("", "I am fairly confident.", "CONFIDENCE: high"):
            self.assertIsNone(prompts.parse_confidence(text), text)

    def test_an_out_of_range_figure_is_refused(self):
        self.assertIsNone(prompts.parse_confidence("CONFIDENCE: 420"))

    def test_the_last_figure_wins(self):
        # An agent that restates the format while explaining itself must not be
        # read as having answered its own example.
        text = "End with CONFIDENCE: 50\n\nMy answer.\n\nCONFIDENCE: 88"
        self.assertEqual(prompts.parse_confidence(text), 88)

    def test_the_trailer_is_split_off_the_body(self):
        parsed = prompts.parse_trailer(
            "## Verdict\nShip it.\n\nCONSENSUS: 78\nCONFIDENCE: 62\nBECAUSE: untested\n"
        )
        self.assertEqual(parsed["confidence"], 62)
        self.assertEqual(parsed["consensus"], 78)
        self.assertEqual(parsed["because"], "untested")
        self.assertNotIn("CONFIDENCE", parsed["body"])
        self.assertIn("Ship it.", parsed["body"])


class TestCouncilPrompts(unittest.TestCase):
    def test_a_member_is_told_to_answer_alone_and_never_to_write(self):
        text = prompts.build_member_prompt("do a thing", "/tmp")
        self.assertIn("independently", text)
        self.assertIn("DO NOT modify", text)
        self.assertIn("CONFIDENCE:", text)

    def test_a_persona_composes_onto_the_contract_rather_than_replacing_it(self):
        text = prompts.build_member_prompt(
            "do a thing", "/tmp", persona_system=prompts.PRAGMATIST_SYSTEM
        )
        self.assertIn("PRAGMATISM", text)
        self.assertIn("DO NOT modify", text, "the persona replaced the contract")

    def test_strictness_actually_changes_the_critique_instruction(self):
        gentle = prompts.build_critique_prompt(
            "t", [{"alias": "Agent A", "output": "x"}], "/tmp", strictness_level=0
        )
        harsh = prompts.build_critique_prompt(
            "t", [{"alias": "Agent A", "output": "x"}], "/tmp", strictness_level=5
        )
        self.assertNotEqual(gentle, harsh)
        self.assertIn("Collegial", gentle)
        self.assertIn("Hostile", harsh)

    def test_strictness_is_clamped_rather_than_rejected(self):
        self.assertEqual(prompts.strictness(99)["level"], 5)
        self.assertEqual(prompts.strictness(-3)["level"], 0)
        self.assertEqual(prompts.strictness("nonsense")["level"],
                         prompts.DEFAULT_STRICTNESS)

    def test_the_chairman_prompt_stays_inside_the_argv_limit(self):
        # `agy` takes its prompt as `--prompt=<text>` and build_argv refuses to
        # move a decorated placeholder to stdin. So the whole chairman prompt
        # has to fit in argv even when every member wrote at length.
        from aicouncil.providers import ARGV_PROMPT_LIMIT

        big = "x" * 200_000
        text = prompts.build_chairman_prompt(
            "t",
            [{"alias": f"Agent {c}", "output": big} for c in "ABCD"],
            [{"alias": f"Agent {c}", "output": big} for c in "ABCD"],
            "/tmp",
            conversation=[{"task": big, "replies": []}],
        )
        self.assertLess(len(text), ARGV_PROMPT_LIMIT)


if __name__ == "__main__":
    unittest.main()
