"""Offline tests for AI-slop demotion. Run: python -m unittest discover tests

The bias under test is deliberate and one-directional: a real interview must
never be demoted, and a generated episode getting through is a tolerable
failure. So the real-interview cases below are the load-bearing ones - they are
taken from the actual hits in state/seen.sqlite3, with their real publishers and
real scores, and if any of them starts failing the weights are wrong even if
every slop case still passes.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interview_monitor import slop, watchlist  # noqa: E402
from interview_monitor.commands import handle  # noqa: E402
from interview_monitor.config import Executive, Settings, load_config  # noqa: E402
from interview_monitor.report import (  # noqa: E402
    SUSPECT_HEADING,
    digest_subject,
    render_digest_html,
    render_digest_text,
    render_json,
)
from interview_monitor.monitor import RunResult  # noqa: E402
from interview_monitor.slop import judge, matches_show, slop_score  # noqa: E402
from interview_monitor.sources import Item  # noqa: E402

JENSEN = Executive(id="jensen-huang", name="Jensen Huang", company="Nvidia",
                   title="CEO", aliases=["Jen-Hsun Huang"])
SATYA = Executive(id="satya-nadella", name="Satya Nadella", company="Microsoft",
                  title="CEO")
NOW = datetime.now(timezone.utc)

DEFAULT_THRESHOLD = Settings().slop_threshold


def episode(title, summary="", *, publisher="Some Show", exec_obj=JENSEN,
            media_type="podcast", duration=None, score=5,
            url="https://example.com/e1") -> Item:
    """A candidate that has already passed classification, as slop.judge sees it."""
    return Item(
        exec_id=exec_obj.id, title=title, url=url, origin="apple-podcasts",
        publisher=publisher, summary=summary, media_type=media_type,
        published=NOW, duration_seconds=duration, score=score,
    )


def scored(item, exec_obj=JENSEN) -> int:
    return slop_score(item, exec_obj)[0]


def reasons(item, exec_obj=JENSEN) -> list[str]:
    return slop_score(item, exec_obj)[1]


class TestRealInterviewsSurvive(unittest.TestCase):
    """The regression suite that matters. Every case is a real recorded hit."""

    def assertNotDemoted(self, item, exec_obj=JENSEN):
        score, why = slop_score(item, exec_obj)
        self.assertLess(
            score, DEFAULT_THRESHOLD,
            f"demoted a real interview ({score} >= {DEFAULT_THRESHOLD}): {why}",
        )

    def test_bloomberg_tech_episode(self):
        self.assertNotDemoted(episode(
            "Special Edition: Nvidia CEO Jensen Huang on Investing in South Korea",
            "Bloomberg Tech's Ed Ludlow speaks with Nvidia CEO Jensen Huang in Seoul "
            "about the company's investment in South Korea and the state of AI demand.",
            publisher="Bloomberg Tech", duration=1500, score=12,
        ))

    def test_y_combinator_episode(self):
        """The sharp one: name-colon title AND a "built" framing, from a real show.

        If the weights are ever tuned upward this is the case that breaks first,
        which is exactly why it is here.
        """
        self.assertNotDemoted(episode(
            "Jensen Huang: The Mindset That Built NVIDIA",
            "Jensen Huang joins Garry Tan to talk about the early years of Nvidia, "
            "why he kept going through the near-death moments, and what he looks "
            "for in founders today.",
            publisher="Y Combinator Startup Podcast", duration=2400, score=5,
        ))

    def test_cnn_gps_interview(self):
        self.assertNotDemoted(episode(
            "On GPS: Exclusive interview with Microsoft CEO Satya Nadella",
            "Fareed speaks with Microsoft CEO Satya Nadella about AI, jobs and "
            "the company's capital spending.",
            publisher="CNN", media_type="article", score=4,
        ), SATYA)

    def test_ai_in_the_show_name_is_not_a_tell(self):
        """Nvidia's own show is called "The AI Podcast". It must not be a signal."""
        self.assertNotDemoted(episode(
            "Jensen Huang on the next era of accelerated computing",
            "A wide-ranging conversation recorded at GTC about Blackwell, robotics "
            "and where inference costs are heading over the next five years.",
            publisher="The AI Podcast", duration=2700, score=6,
        ))

    def test_explains_why_is_still_an_interview(self):
        """classify.py counts "explains why" as a positive; only a trailing
        "Explained" is the summary shape."""
        self.assertNotDemoted(episode(
            "Jensen Huang explains why Blackwell demand is insatiable",
            "The Nvidia chief executive sits down with us for a full hour on "
            "supply, competition and what comes after Blackwell.",
            publisher="Acquired", duration=3600, score=7,
        ))

    def test_short_real_interview_is_not_demoted_on_length_alone(self):
        """A brief but genuine interview: length is worth 2, never enough alone."""
        item = episode(
            "In conversation with Jensen Huang",
            "A short interview recorded backstage at Computex about the road ahead.",
            publisher="Bloomberg Tech", duration=180, score=6,
        )
        self.assertNotDemoted(item)
        self.assertIn("under-5-min", reasons(item))

    def test_short_thin_clip_from_a_real_outlet(self):
        """The circumstantial trio - short, thin notes, name-then-colon - with no
        tell about what the episode is. News outlets publish these constantly and
        none of the rescues can fire on them."""
        item = episode(
            "Jensen Huang: Nvidia earnings takeaways",
            "Bloomberg Businessweek podcast.",
            publisher="Bloomberg Businessweek", duration=240, score=5,
        )
        self.assertNotDemoted(item)
        why = reasons(item)
        self.assertIn("circumstantial-only", why)
        # The observations are still recorded - only the demotion is withheld.
        self.assertIn("under-5-min", why)
        self.assertIn("thin-description", why)
        self.assertIn("name-colon-title", why)

    def test_circumstantial_tells_alone_score_zero(self):
        item = episode(
            "Jensen Huang: the week in chips", "", duration=100, score=4,
        )
        self.assertEqual(scored(item), 0)

    def test_one_substantive_tell_re_enables_the_structural_ones(self):
        """The gate suppresses circumstantial-only evidence, not the tells."""
        item = episode(
            "Jensen Huang: Biography", "", publisher="Quick Facts",
            duration=100, score=4,
        )
        score, why = slop_score(item, JENSEN)
        self.assertNotIn("circumstantial-only", why)
        self.assertGreaterEqual(score, DEFAULT_THRESHOLD)


class TestSlopIsDemoted(unittest.TestCase):
    def assertDemoted(self, item, exec_obj=JENSEN):
        score, why = slop_score(item, exec_obj)
        self.assertGreaterEqual(
            score, DEFAULT_THRESHOLD, f"missed slop (scored {score}): {why}"
        )

    def test_self_disclosed_ai_narration_alone_is_enough(self):
        self.assertDemoted(episode(
            "Jensen Huang: A Life in Chips",
            "This episode is AI-generated and uses synthetic voices.",
            publisher="Tech Titans", duration=1800, score=6,
        ))

    def test_scraper_boilerplate_plus_framing(self):
        self.assertDemoted(episode(
            "Jensen Huang: The Rise of Nvidia",
            "Explore the remarkable journey of Jensen Huang, based solely on "
            "publicly available information. We are not affiliated with Nvidia.",
            publisher="Business Icons Daily", duration=380, score=6,
        ))

    def test_net_worth_explainer(self):
        self.assertDemoted(episode(
            "Tim Cook Success Story Explained",
            "A quick look at the numbers.",
            publisher="AI Biography Digest", duration=240, score=5,
        ))

    def test_notebooklm_disclosure(self):
        self.assertDemoted(episode(
            "Deep dive into Jensen Huang",
            "Generated with NotebookLM for entertainment purposes only.",
            publisher="Daily Deep Dives", duration=600, score=5,
        ))

    def test_two_minute_episode_with_no_description(self):
        self.assertDemoted(episode(
            "Who is Jensen Huang?", "", publisher="Wiki Voice", duration=90, score=5,
        ))

    def test_description_that_is_only_the_title(self):
        item = episode(
            "Jensen Huang Net Worth", "Jensen Huang Net Worth",
            publisher="Money Facts", duration=400, score=5,
        )
        self.assertDemoted(item)
        self.assertIn("description-is-title", reasons(item))


class TestRescues(unittest.TestCase):
    def test_guest_framing_pulls_a_borderline_item_back(self):
        args = dict(publisher="Icons Weekly", duration=900, score=5)
        about = episode("The story of Jensen Huang", "Lessons from a founder.", **args)
        with_guest = episode(
            "The story of Nvidia, with Jensen Huang", "Lessons from a founder.", **args
        )
        self.assertGreater(scored(about), scored(with_guest))
        self.assertIn("guest-framing", reasons(with_guest))

    def test_joins_after_the_name_is_guest_framing(self):
        item = episode(
            "Jensen Huang joins us to talk Blackwell",
            "A full hour with the Nvidia chief executive.",
            duration=1900, score=6,
        )
        self.assertIn("guest-framing", reasons(item))

    def test_guest_framing_works_on_an_alias(self):
        item = episode(
            "In conversation with Jen-Hsun Huang", "Recorded live.", duration=1900,
        )
        self.assertIn("guest-framing", reasons(item))

    def test_substantial_runtime_is_a_rescue(self):
        short = episode("Jensen Huang: The Legacy of Nvidia", "Notes.", duration=600)
        long = episode("Jensen Huang: The Legacy of Nvidia", "Notes.", duration=3600)
        self.assertGreater(scored(short), scored(long))
        self.assertIn("substantial-runtime", reasons(long))

    def test_strong_interview_cues_are_a_rescue(self):
        weak = episode("Jensen Huang: The Rise of Nvidia", "Notes about him.", score=4)
        strong = episode("Jensen Huang: The Rise of Nvidia", "Notes about him.", score=12)
        self.assertGreater(scored(weak), scored(strong))
        self.assertIn("strong-interview-cues", reasons(strong))

    def test_score_never_goes_negative(self):
        item = episode(
            "A conversation with Jensen Huang", "Full transcript below.",
            duration=5400, score=14,
        )
        self.assertEqual(scored(item), 0)

    def test_name_colon_title_needs_a_short_head(self):
        """"Satya Nadella on Microsoft's AI bet - the full interview" is not the
        "Name: topic" shape, however it splits."""
        item = episode(
            "Satya Nadella on Microsoft's AI bet - the full interview",
            "A long conversation.", exec_obj=SATYA, duration=2000,
        )
        self.assertNotIn("name-colon-title", reasons(item, SATYA))

    def test_hyphenated_name_is_not_split_as_a_separator(self):
        item = episode("Jen-Hsun Huang on the early days", "Notes.", duration=2000)
        self.assertNotIn("name-colon-title", reasons(item))


class TestJudge(unittest.TestCase):
    def test_trusted_show_is_never_demoted(self):
        item = episode(
            "Jensen Huang: The Rise of Nvidia",
            "AI-generated. Not affiliated with Nvidia.",
            publisher="Acquired", duration=200, score=5,
        )
        self.assertTrue(judge(item, JENSEN, threshold=DEFAULT_THRESHOLD))

        fresh = episode(
            "Jensen Huang: The Rise of Nvidia",
            "AI-generated. Not affiliated with Nvidia.",
            publisher="Acquired", duration=200, score=5,
        )
        self.assertFalse(
            judge(fresh, JENSEN, threshold=DEFAULT_THRESHOLD, trusted_shows=["Acquired"])
        )
        self.assertFalse(fresh.slop)
        self.assertIn("trusted:Acquired", fresh.slop_reasons)
        # The score is still recorded, so the reasons stay auditable.
        self.assertGreaterEqual(fresh.slop_score, DEFAULT_THRESHOLD)

    def test_threshold_of_zero_disables_demotion_but_keeps_the_score(self):
        item = episode(
            "Jensen Huang: A Life in Chips", "This episode is AI-generated.",
            duration=200,
        )
        self.assertFalse(judge(item, JENSEN, threshold=0))
        self.assertFalse(item.slop)
        self.assertGreater(item.slop_score, 0)

    def test_judge_records_score_and_reasons_on_the_item(self):
        item = episode("Jensen Huang Net Worth", "Facts only.", duration=200)
        judge(item, JENSEN, threshold=DEFAULT_THRESHOLD)
        self.assertTrue(item.slop)
        self.assertIn("net-worth", item.slop_reasons)


class TestMatchesShow(unittest.TestCase):
    def test_case_insensitive_substring(self):
        self.assertEqual(matches_show("Bloomberg Tech", ["bloomberg"]), "bloomberg")

    def test_no_match_returns_empty(self):
        self.assertEqual(matches_show("Bloomberg Tech", ["Acquired"]), "")

    def test_empty_publisher_abstains(self):
        self.assertEqual(matches_show("", ["Acquired"]), "")

    def test_too_short_an_entry_matches_nothing(self):
        """A stray one-character entry must not mute every show on earth."""
        self.assertEqual(matches_show("Bloomberg Tech", ["a"]), "")
        self.assertEqual(matches_show("Bloomberg Tech", ["", "  "]), "")


class TestSlopConfig(unittest.TestCase):
    def _load(self, settings: dict):
        path = Path(tempfile.mkdtemp()) / "execs.json"
        path.write_text(json.dumps({
            "settings": settings,
            "executives": [{"name": "Jensen Huang", "company": "Nvidia"}],
        }))
        return load_config(path)

    def test_defaults(self):
        config = self._load({})
        self.assertEqual(config.settings.slop_threshold, 4)
        self.assertEqual(config.settings.blocked_shows, [])
        self.assertEqual(config.settings.trusted_shows, [])

    def test_lists_are_read_and_cleaned(self):
        config = self._load({
            "blocked_shows": ["Business Icons Daily", "  ", "business icons daily"],
            "trusted_shows": "Acquired",
            "slop_threshold": 6,
        })
        self.assertEqual(config.settings.blocked_shows, ["Business Icons Daily"])
        self.assertEqual(config.settings.trusted_shows, ["Acquired"])
        self.assertEqual(config.settings.slop_threshold, 6)

    def test_too_short_a_blocked_show_is_a_hard_error(self):
        """Silently obeying "a" would empty the digest with no explanation."""
        with self.assertRaises(ValueError) as caught:
            self._load({"blocked_shows": ["a"]})
        self.assertIn("too short", str(caught.exception))

    def test_a_non_string_entry_is_refused(self):
        with self.assertRaises(ValueError):
            self._load({"trusted_shows": [{"name": "Acquired"}]})


class TestBlockCommand(unittest.TestCase):
    def _watchlist(self, settings: dict | None = None) -> Path:
        path = Path(tempfile.mkdtemp()) / "execs.json"
        path.write_text(json.dumps({
            "settings": settings or {},
            "executives": [
                {"name": "Jensen Huang", "company": "Nvidia"},
                {"name": "Tim Cook", "company": "Apple"},
            ],
        }))
        return path

    def test_block_keeps_the_show_name_verbatim(self):
        """"Digest" is a target noun for the person parser and must survive here."""
        path = self._watchlist()
        outcome = handle("", "block The AI Business Digest", path)
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.blocked, ["The AI Business Digest"])
        saved = json.loads(path.read_text())
        self.assertEqual(saved["settings"]["blocked_shows"], ["The AI Business Digest"])

    def test_block_strips_quotes_and_courtesy(self):
        path = self._watchlist()
        outcome = handle("", 'please mute "Business Icons Daily" from my list, thanks', path)
        self.assertEqual(outcome.blocked, ["Business Icons Daily"])

    def test_a_show_name_ending_in_a_courtesy_word_survives(self):
        """"Tech Today" truncated to "Tech" would mute Bloomberg Tech and
        TechCrunch as well - a substring match makes over-stripping expensive."""
        for name in ("Tech Today", "Nvidia Now", "Marketplace Tech"):
            path = self._watchlist()
            outcome = handle("", f"block {name}", path)
            self.assertEqual(outcome.blocked, [name], name)

    def test_a_real_courtesy_still_comes_off_a_show_name(self):
        path = self._watchlist()
        outcome = handle("", "block Tech Today please", path)
        self.assertEqual(outcome.blocked, ["Tech Today"])

    def test_a_comma_proves_a_trailing_now_is_an_aside(self):
        path = self._watchlist()
        outcome = handle("", "block Business Icons Daily, now", path)
        self.assertEqual(outcome.blocked, ["Business Icons Daily"])

    def test_unblock_is_not_read_as_block(self):
        """difflib scores "unblock" against "block" at 0.83, over the 0.8 cutoff."""
        path = self._watchlist({"blocked_shows": ["Business Icons Daily"]})
        outcome = handle("", "unblock Business Icons Daily", path)
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.blocked, [])

    def test_blocking_the_same_show_twice_is_refused(self):
        path = self._watchlist({"blocked_shows": ["Business Icons Daily"]})
        outcome = handle("", "block Business Icons Daily", path)
        self.assertFalse(outcome.changed)
        self.assertTrue(any("already blocked" in m for m in outcome.rejected))

    def test_too_short_a_block_is_refused_not_obeyed(self):
        path = self._watchlist()
        outcome = handle("", "block ab", path)
        self.assertFalse(outcome.changed)
        self.assertTrue(any("too short" in m for m in outcome.rejected))

    def test_unblocking_something_unblocked_says_so(self):
        path = self._watchlist()
        outcome = handle("", "unblock Business Icons Daily", path)
        self.assertFalse(outcome.changed)
        self.assertTrue(any("not blocked" in m for m in outcome.rejected))

    def test_blocking_does_not_disturb_the_tracked_list(self):
        path = self._watchlist({"lookback_days": 14})
        handle("", "block Business Icons Daily", path)
        saved = json.loads(path.read_text())
        self.assertEqual(len(saved["executives"]), 2)
        self.assertEqual(saved["settings"]["lookback_days"], 14)

    def test_block_and_add_in_one_reply(self):
        path = self._watchlist()
        outcome = handle("", "add Lisa Su, AMD CEO\nblock Business Icons Daily", path)
        self.assertEqual(len(outcome.applied), 2)
        self.assertEqual(outcome.blocked, ["Business Icons Daily"])
        self.assertEqual(len(outcome.listing), 3)

    def test_a_surname_at_the_start_of_a_line_is_not_a_block_verb(self):
        """"ban" is deliberately not a synonym: Ban Ki-moon is a person."""
        path = self._watchlist()
        outcome = handle("", "Ban Ki-moon", path)
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.blocked, [])
        self.assertIn("Ban Ki-moon", outcome.unparsed)

    def test_science_is_not_read_as_silence(self):
        path = self._watchlist()
        outcome = handle("", "science podcasts are great", path)
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.blocked, [])

    def test_mute_and_silence_still_block(self):
        for verb in ("mute", "silence"):
            path = self._watchlist()
            outcome = handle("", f"{verb} Business Icons Daily", path)
            self.assertEqual(outcome.blocked, ["Business Icons Daily"], verb)

    def test_blocked_shows_reads_a_missing_settings_block(self):
        self.assertEqual(watchlist.blocked_shows({"executives": []}), [])
        self.assertEqual(watchlist.blocked_shows({"settings": "nonsense"}), [])


class TestDigestSplit(unittest.TestCase):
    def _result(self, items: list[Item], executives=None) -> RunResult:
        return RunResult(new_items=items, executives=executives or [JENSEN],
                         candidates=40)

    def _real(self) -> Item:
        return episode(
            "Jensen Huang on the next compute era",
            "A full hour with the Nvidia chief executive.",
            publisher="Acquired", duration=3600, score=9,
            url="https://example.com/real",
        )

    def _suspect(self) -> Item:
        item = episode(
            "Jensen Huang: The Rise of Nvidia",
            "AI-generated, based solely on publicly available information.",
            publisher="Business Icons Daily", duration=300, score=5,
            url="https://example.com/slop",
        )
        judge(item, JENSEN, threshold=DEFAULT_THRESHOLD)
        assert item.slop, item.slop_reasons
        return item

    def test_by_executive_excludes_suspects(self):
        result = self._result([self._real(), self._suspect()])
        self.assertEqual(len(result.real_items), 1)
        self.assertEqual(len(result.suspect_items), 1)
        self.assertEqual(len(result.by_executive[JENSEN.id]), 1)
        # Both are still in new_items, so both are still recorded and deduped.
        self.assertEqual(len(result.new_items), 2)

    def test_suspects_appear_only_in_their_own_section(self):
        text = render_digest_text(self._result([self._real(), self._suspect()]))
        body, _, appendix = text.partition(SUSPECT_HEADING)
        self.assertIn("Jensen Huang on the next compute era", body)
        self.assertNotIn("The Rise of Nvidia", body)
        self.assertIn("The Rise of Nvidia", appendix)
        self.assertIn("why:", appendix)

    def test_no_section_when_nothing_is_flagged(self):
        text = render_digest_text(self._result([self._real()]))
        self.assertNotIn(SUSPECT_HEADING, text)

    def test_html_digest_lists_suspects_with_reasons(self):
        html = render_digest_html(self._result([self._real(), self._suspect()]))
        self.assertIn(SUSPECT_HEADING, html)
        self.assertIn("https://example.com/slop", html)
        self.assertIn("why:", html)

    def test_subject_counts_real_and_suspect_separately(self):
        self.assertEqual(
            digest_subject(self._result([self._real(), self._suspect()])),
            "Interview digest: 1 new interview (1 executive) (+1 probably not real)",
        )

    def test_subject_says_so_when_everything_is_suspect(self):
        """A digest of nothing but slop must not read as an empty one."""
        subject = digest_subject(self._result([self._suspect()]))
        self.assertEqual(
            subject, "Interview digest: no new interviews (+1 probably not real)"
        )

    def test_subject_is_unchanged_when_nothing_is_flagged(self):
        self.assertEqual(
            digest_subject(self._result([self._real()])),
            "Interview digest: 1 new interview (1 executive)",
        )

    def test_json_separates_the_two_lists(self):
        payload = json.loads(render_json(self._result([self._real(), self._suspect()])))
        self.assertEqual(payload["new_interview_count"], 1)
        self.assertEqual(len(payload["new_interviews"]), 1)
        self.assertEqual(len(payload["probably_not_real"]), 1)
        self.assertTrue(payload["probably_not_real"][0]["probably_not_real"])
        self.assertTrue(payload["probably_not_real"][0]["slop_reasons"])
        # Recorded on believed-real items too, for tuning against near misses.
        self.assertIn("slop_score", payload["new_interviews"][0])
        self.assertFalse(payload["new_interviews"][0]["probably_not_real"])

    def test_footer_reports_blocked_items(self):
        result = self._result([self._real()])
        result.blocked = 3
        self.assertIn("3 hidden from blocked shows", render_digest_text(result))


class TestBlockedShowsAreDropped(unittest.TestCase):
    """The one hard drop in the pipeline, exercised through crawl()."""

    def _crawl(self, blocked: list[str]):
        from interview_monitor.config import Config
        from interview_monitor.monitor import crawl
        from interview_monitor.sources import Source

        # Slop that still reads as an interview to the classifier - which is the
        # whole problem this feature exists for.
        wanted = episode(
            "Jensen Huang: The Rise of Nvidia",
            "A full interview with him. This episode is AI-generated and is not "
            "affiliated with Nvidia.",
            publisher="Business Icons Daily", url="https://example.com/blocked",
        )
        keeper = episode(
            "An interview with Jensen Huang", "A full hour with the Nvidia chief.",
            publisher="Acquired", url="https://example.com/keep", duration=3600,
        )

        class FakeSource(Source):
            name = "fake"

            def search(self, exec_obj, *, days, limit):
                return [wanted, keeper]

        config = Config(
            executives=[JENSEN],
            settings=Settings(blocked_shows=blocked, max_results_per_source=10),
        )
        return crawl(config, [FakeSource()], days=7, min_score=3)

    def test_blocked_show_is_dropped_and_counted(self):
        accepted, candidates, _, blocked = self._crawl(["Business Icons Daily"])
        self.assertEqual(candidates, 2)
        self.assertEqual(blocked, 1)
        self.assertEqual([i.publisher for i in accepted], ["Acquired"])

    def test_nothing_is_dropped_without_a_blocklist(self):
        """Slop is demoted, never dropped - both items survive the crawl."""
        accepted, _, _, blocked = self._crawl([])
        self.assertEqual(blocked, 0)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(sorted(i.slop for i in accepted), [False, True])

    def test_demotion_breaks_a_tie_between_equal_scores(self):
        """Same title and notes, so the same interview score; only the show
        differs. There the demoted copy sorts last."""
        from interview_monitor.monitor import crawl
        from interview_monitor.config import Config
        from interview_monitor.sources import Source

        title = "Jensen Huang on Nvidia's next act"
        real = episode(title, "", publisher="Acquired",
                       url="https://example.com/real")
        fake = episode(title, "", publisher="AI-Narrated Business Icons",
                       url="https://example.com/fake")

        class FakeSource(Source):
            name = "fake"

            def search(self, exec_obj, *, days, limit):
                return [fake, real]  # slop first, to prove the sort moved it

        accepted, _, _, _ = crawl(
            Config(executives=[JENSEN], settings=Settings()),
            [FakeSource()], days=7, min_score=3,
        )
        self.assertEqual(len(accepted), 2)
        self.assertEqual(accepted[0].score, accepted[1].score)
        self.assertEqual(accepted[0].url, "https://example.com/real")
        self.assertFalse(accepted[0].slop)
        self.assertTrue(accepted[1].slop)

    def test_demotion_only_breaks_ties_and_never_outranks_confidence(self):
        """search_person shares this sort, and the web UI orders by confidence."""
        from interview_monitor.monitor import crawl
        from interview_monitor.config import Config
        from interview_monitor.sources import Source

        strong_slop = episode(
            "Jensen Huang: The Rise of Nvidia",
            "An exclusive full interview. Sits down with us. This episode is "
            "AI-generated.",
            publisher="Icons Daily", url="https://example.com/strong",
        )
        weak_real = episode(
            "Jensen Huang podcast episode", "An episode.",
            publisher="Acquired", url="https://example.com/weak", duration=3600,
        )

        class FakeSource(Source):
            name = "fake"

            def search(self, exec_obj, *, days, limit):
                return [weak_real, strong_slop]

        accepted, _, _, _ = crawl(
            Config(executives=[JENSEN], settings=Settings()),
            [FakeSource()], days=7, min_score=3,
        )
        by_url = {i.url: i for i in accepted}
        self.assertTrue(by_url["https://example.com/strong"].slop)
        self.assertFalse(by_url["https://example.com/weak"].slop)
        self.assertGreater(
            by_url["https://example.com/strong"].score,
            by_url["https://example.com/weak"].score,
        )
        # Higher interview score wins even though it is the demoted one.
        self.assertEqual(accepted[0].url, "https://example.com/strong")

    def test_blocked_count_ignores_items_that_would_never_have_been_reported(self):
        """The footer calls this what blocking threw away, so it must not count
        a blocked show's whole back catalogue of non-interviews."""
        from interview_monitor.monitor import crawl
        from interview_monitor.config import Config
        from interview_monitor.sources import Source

        reportable = episode(
            "An interview with Jensen Huang", "A full hour.",
            publisher="Business Icons Daily", url="https://example.com/a",
        )
        # Names the exec only in the description: rejected by the podcast rule,
        # so it was never going to be reported and blocking did not lose it.
        noise = episode(
            "Episode 412: the week in chips",
            "We mention Jensen Huang and many others.",
            publisher="Business Icons Daily", url="https://example.com/b",
        )

        class FakeSource(Source):
            name = "fake"

            def search(self, exec_obj, *, days, limit):
                return [reportable, noise]

        accepted, candidates, _, blocked = crawl(
            Config(executives=[JENSEN],
                   settings=Settings(blocked_shows=["Business Icons Daily"])),
            [FakeSource()], days=7, min_score=3,
        )
        self.assertEqual(accepted, [])
        self.assertEqual(candidates, 2)
        self.assertEqual(blocked, 1)


class TestAppleDuration(unittest.TestCase):
    def test_track_time_millis_becomes_seconds(self):
        from interview_monitor.sources import _millis_to_seconds

        self.assertEqual(_millis_to_seconds(2_400_000), 2400)
        self.assertEqual(_millis_to_seconds("2400000"), 2400)

    def test_unusable_values_abstain_rather_than_reporting_zero(self):
        from interview_monitor.sources import _millis_to_seconds

        for value in (None, "", "abc", 0, {}):
            self.assertIsNone(_millis_to_seconds(value), value)


if __name__ == "__main__":
    unittest.main()
