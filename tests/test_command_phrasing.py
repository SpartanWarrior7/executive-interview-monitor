"""Instructions written as sentences, not as commands.

Nobody replies "add Tim Cook". They reply "Can you add Tim Cook to the tracker
please", and the tail of that sentence used to be taken as part of the name -
which added a person called "Tim Cook to tracker" who then matched nothing
ever again. These tests pin the sentence being taken apart, and pin the two
refusals that are better than a bad entry: scaffolding left in an "add", and a
company where a person should be.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interview_monitor import commands  # noqa: E402

WATCHLIST = {
    "_comment": "hand-written note that must survive being rewritten",
    "settings": {"lookback_days": 7},
    "executives": [
        {"id": "jensen-huang", "name": "Jensen Huang", "company": "Nvidia",
         "title": "CEO", "aliases": ["Jen-Hsun Huang"]},
        {"id": "satya-nadella", "name": "Satya Nadella", "company": "Microsoft",
         "title": "CEO"},
        {"id": "michael-brown", "name": "Michael Brown", "company": "Initech"},
    ],
}

GUIDANCE = "I track people, not companies."


class ParsingCase(unittest.TestCase):
    """Reads one line the way the mail handler does."""

    def _one(self, line: str):
        parsed, unparsed = commands.parse("RE: Interview digest", line + "\n")
        self.assertEqual(unparsed, [], f"{line!r} was not understood")
        self.assertEqual(len(parsed), 1, f"{line!r} produced {parsed}")
        return parsed[0].verb, parsed[0].argument


class ListCase(unittest.TestCase):
    """Runs instructions against a throwaway copy of the watchlist."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "executives.json"
        self.path.write_text(json.dumps(WATCHLIST), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _handle(self, body: str):
        return commands.handle("RE: Interview digest", body + "\n", self.path)

    def _names(self):
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [e["name"] for e in raw["executives"]]


class TestPhrasingIsStripped(ParsingCase):
    """What survives as the argument once the sentence is taken off."""

    def test_target_phrase_after_the_name_is_dropped(self):
        for line, expected in (
            ("remove Jensen Huang from tracker", (commands.REMOVE, "Jensen Huang")),
            ("remove Jensen Huang from the tracker", (commands.REMOVE, "Jensen Huang")),
            ("add Tim Cook to tracker", (commands.ADD, "Tim Cook")),
            ("add Tim Cook to the tracking list", (commands.ADD, "Tim Cook")),
            ("add Tim Cook onto my watchlist", (commands.ADD, "Tim Cook")),
            ("remove Jensen Huang off the list", (commands.REMOVE, "Jensen Huang")),
            ("remove Jensen Huang from the digest", (commands.REMOVE, "Jensen Huang")),
            ("add Tim Cook to the monitor", (commands.ADD, "Tim Cook")),
        ):
            with self.subTest(line=line):
                self.assertEqual(self._one(line), expected)

    def test_courtesy_after_the_target_phrase_is_also_dropped(self):
        for line in ("add Tim Cook to the tracker please",
                     "add Tim Cook to the tracker, thanks",
                     "add Tim Cook to the tracker for me",
                     "add Tim Cook to the list from now on"):
            with self.subTest(line=line):
                self.assertEqual(self._one(line), (commands.ADD, "Tim Cook"))

    def test_a_question_mark_is_not_part_of_the_name(self):
        self.assertEqual(self._one("Could you add Lisa Su (AMD CEO) to the watchlist please?"),
                         (commands.ADD, "Lisa Su (AMD CEO)"))
        self.assertEqual(self._one("can you remove Jensen Huang?"),
                         (commands.REMOVE, "Jensen Huang"))

    def test_the_company_survives_the_stripping(self):
        self.assertEqual(self._one("add Lisa Su, AMD CEO to the watchlist"),
                         (commands.ADD, "Lisa Su, AMD CEO"))
        self.assertEqual(self._one("please add Tim Cook (Apple CEO) to the tracker"),
                         (commands.ADD, "Tim Cook (Apple CEO)"))

    def test_commentary_after_the_target_phrase_is_dropped(self):
        """Once the sentence has named the tracker, the rest is an aside."""
        self.assertEqual(
            self._one("take Jensen Huang off the list - he is everywhere already"),
            (commands.REMOVE, "Jensen Huang"))
        self.assertEqual(self._one("remove Jensen Huang from the list of CEOs"),
                         (commands.REMOVE, "Jensen Huang"))

    def test_filler_nouns_around_the_subject_are_dropped(self):
        for line, expected in (
            ("remove the exec Jensen Huang", (commands.REMOVE, "Jensen Huang")),
            ("add the person Tim Cook", (commands.ADD, "Tim Cook")),
            ("add exec Tim Cook, Apple CEO", (commands.ADD, "Tim Cook, Apple CEO")),
            ("remove Jensen Huang the exec", (commands.REMOVE, "Jensen Huang")),
        ):
            with self.subTest(line=line):
                self.assertEqual(self._one(line), expected)

    def test_a_plural_target_is_stripped_too(self):
        self.assertEqual(self._one("remove Jensen Huang off the trackers"),
                         (commands.REMOVE, "Jensen Huang"))
        self.assertEqual(self._one("add Tim Cook to the lists"),
                         (commands.ADD, "Tim Cook"))

    def test_a_company_after_the_target_phrase_is_kept(self):
        """A comma is how the company arrives, so that tail is spliced back on
        rather than thrown away with the commentary."""
        self.assertEqual(self._one("add Tim Cook to the tracker, Apple CEO"),
                         (commands.ADD, "Tim Cook, Apple CEO"))

    def test_a_name_that_is_also_a_filler_noun_survives(self):
        """Gal Gadot, Guy Kawasaki and Buddy Guy are people. The filler nouns
        that double as first names need the determiner in front."""
        for line, expected in (("add Gal Gadot", "Gal Gadot"),
                               ("add Guy Kawasaki, Alltop", "Guy Kawasaki, Alltop"),
                               ("remove Buddy Guy", "Buddy Guy"),
                               ("add Chap Petersen", "Chap Petersen")):
            with self.subTest(line=line):
                self.assertEqual(self._one(line)[1], expected)

    def test_a_run_of_commas_does_not_stall_the_handler(self):
        """Every pattern here starts with [\\s,] and runs to the end of the
        line, so an inbound line of nothing but commas used to cost quadratic
        time to fail to match."""
        import time
        start = time.monotonic()
        commands.clean_argument("Tim " + "," * 8000 + " Cook")
        self.assertLess(time.monotonic() - start, 2.0)

    def test_a_real_surname_is_not_mistaken_for_the_target(self):
        """"Sam Monitor" is a person; "to the monitor" is the tracker. Only a
        preposition or an article in front makes it the tracker."""
        self.assertEqual(self._one("add Sam Monitor"), (commands.ADD, "Sam Monitor"))
        self.assertEqual(self._one("add Sam List, Acme CEO"),
                         (commands.ADD, "Sam List, Acme CEO"))
        self.assertEqual(self._one("remove Jensen Huang from Nvidia"),
                         (commands.REMOVE, "Jensen Huang from Nvidia"))


class TestVerbInASentence(ParsingCase):
    def test_courtesy_in_front_of_the_verb_is_stepped_over(self):
        for line, expected in (
            ("Can you remove Jensen Huang", (commands.REMOVE, "Jensen Huang")),
            ("Could you please add Tim Cook", (commands.ADD, "Tim Cook")),
            ("Please could you add Tim Cook", (commands.ADD, "Tim Cook")),
            ("I want to add Tim Cook", (commands.ADD, "Tim Cook")),
            ("and also remove Jensen Huang", (commands.REMOVE, "Jensen Huang")),
        ):
            with self.subTest(line=line):
                self.assertEqual(self._one(line), expected)

    def test_new_ways_of_saying_stop(self):
        for line in ("take Jensen Huang off the tracker",
                     "get rid of Jensen Huang",
                     "no longer track Jensen Huang",
                     "don't track Jensen Huang",
                     "dont track Jensen Huang",
                     "do not track Jensen Huang",
                     "unsubscribe Jensen Huang",
                     "kill Jensen Huang",
                     "stop tracking Jensen Huang",
                     "stop following Jensen Huang"):
            with self.subTest(line=line):
                self.assertEqual(self._one(line), (commands.REMOVE, "Jensen Huang"))

    def test_take_without_off_is_not_a_removal(self):
        """"take" only means remove when something is being taken off."""
        _, unparsed = commands.parse("RE: digest", "take a look at Jensen Huang\n")
        self.assertEqual(unparsed, ["take a look at Jensen Huang"])

    def test_a_verb_alike_late_in_a_sentence_is_still_not_a_command(self):
        """Only closed-list filler is stepped over, so scanning forward cannot
        turn a complaint into an instruction."""
        for line in ("sort out my subscription please",
                     "what is this thing costing us",
                     "the whole thing is too noisy",
                     "why did nobody add anything"):
            with self.subTest(line=line):
                parsed, unparsed = commands.parse("RE: digest", line + "\n")
                self.assertEqual(parsed, [], f"{line!r} became {parsed}")
                self.assertEqual(unparsed, [line])

    def test_a_subscribe_is_not_read_as_an_unsubscribe(self):
        """"subscribe" is one edit from "unsubscribe" and "skill" one from
        "kill", so those two are matched only when typed exactly."""
        for line in ("subscribe me to the digest", "skill issue"):
            with self.subTest(line=line):
                parsed, unparsed = commands.parse("RE: digest", line + "\n")
                self.assertEqual(parsed, [])
                self.assertEqual(unparsed, [line])

    def test_typo_tolerance_survives(self):
        parsed, _ = commands.parse("RE: digest", "delet Satya Nadella from the tracker\n")
        self.assertEqual((parsed[0].verb, parsed[0].argument),
                         (commands.REMOVE, "Satya Nadella"))


class TestPeopleNotCompanies(ListCase):
    def test_adding_a_company_is_refused_with_who_to_name_instead(self):
        for line, company in (("add Apple company to tracker", "Apple"),
                              ("add Umbrella Corporation", "Umbrella"),
                              ("add Acme Inc to the list", "Acme"),
                              ("add Hooli Ltd", "Hooli"),
                              ("add Soylent plc", "Soylent")):
            with self.subTest(line=line):
                outcome = self._handle(line)
                self.assertFalse(outcome.changed)
                self.assertEqual(len(outcome.rejected), 1)
                self.assertIn(GUIDANCE, outcome.rejected[0])
                self.assertIn(f"who at {company} to follow", outcome.rejected[0])
                self.assertIn(f"add Jane Doe, {company} CEO", outcome.rejected[0])
        self.assertEqual(self._names(), ["Jensen Huang", "Satya Nadella", "Michael Brown"])

    def test_a_bare_company_name_is_guidance_not_a_grumble_about_full_names(self):
        """"add Nvidia to tracker" strips to one word. The useful answer is who
        at Nvidia, not "I need a full name"."""
        outcome = self._handle("add Nvidia to tracker")
        self.assertFalse(outcome.changed)
        self.assertIn(GUIDANCE, outcome.rejected[0])
        self.assertNotIn("I need a full name", outcome.rejected[0])

    def test_removing_a_company_names_the_people_tracked_there(self):
        outcome = self._handle("remove Nvidia company from tracker")
        self.assertFalse(outcome.changed)
        self.assertIn(GUIDANCE, outcome.rejected[0])
        self.assertIn("Jensen Huang (CEO, Nvidia)", outcome.rejected[0])
        self.assertEqual(self._names(), ["Jensen Huang", "Satya Nadella", "Michael Brown"])

    def test_removing_an_untracked_company_still_explains_itself(self):
        outcome = self._handle("remove Hooli Ltd from the tracker")
        self.assertFalse(outcome.changed)
        self.assertIn(GUIDANCE, outcome.rejected[0])
        self.assertNotIn("I currently track", outcome.rejected[0])

    def test_a_person_whose_employer_has_a_legal_suffix_is_still_a_person(self):
        outcome = self._handle("add Sam Director, CFO, Umbrella Corporation")
        self.assertTrue(outcome.changed)
        self.assertIn("Sam Director", self._names())


class TestNoCorruptEntries(ListCase):
    def test_the_name_written_is_the_name_meant(self):
        outcome = self._handle("Can you add Tim Cook to the tracker please")
        self.assertTrue(outcome.changed)
        self.assertEqual(self._names(),
                         ["Jensen Huang", "Satya Nadella", "Michael Brown", "Tim Cook"])

    def test_leftover_scaffolding_is_refused_rather_than_written(self):
        """A refusal costs one reply. An entry called "Tim Cook tracker" sits
        in the list for months matching nothing."""
        for line in ("add Tim Cook tracker",
                     "add to the tracker Tim Cook",
                     "add Tim Cook to trackerr"):
            with self.subTest(line=line):
                outcome = self._handle(line)
                self.assertFalse(outcome.changed)
                self.assertEqual(len(outcome.rejected), 1)
        self.assertEqual(self._names(), ["Jensen Huang", "Satya Nadella", "Michael Brown"])

    def test_a_preposition_left_in_the_name_is_refused(self):
        """Names do not contain "to". Whatever it is carrying is the rest of
        the sentence, spelled right or not."""
        for line in ("add Tim Cook to Apple", "add Tim Cook to"):
            with self.subTest(line=line):
                outcome = self._handle(line)
                self.assertFalse(outcome.changed)
        self.assertEqual(self._names(), ["Jensen Huang", "Satya Nadella", "Michael Brown"])

    def test_the_connector_forms_are_not_caught_by_that(self):
        """"at Amazon" is the company, and the parser has already taken it off
        the name by the time the guard looks."""
        outcome = self._handle("add Andy Jassy at Amazon to the tracker")
        self.assertTrue(outcome.changed)
        self.assertIn("Andy Jassy", self._names())

    def test_a_surname_that_is_a_preposition_is_allowed(self):
        """Johnnie To is a person. A preposition inside the name, or trailing
        a longer one, is the rest of a sentence; a two-word name ending in one
        is a surname."""
        outcome = self._handle("add Johnnie To")
        self.assertTrue(outcome.changed)
        self.assertIn("Johnnie To", self._names())

    def test_an_opinion_about_coverage_is_not_written_down_as_a_person(self):
        """These parse as courtesy, a verb and a noun phrase, which is exactly
        the shape of an instruction - and exactly the junk entry to avoid."""
        for line in ("we want to include more women",
                     "I need to add more tech CEOs",
                     "can you add the new Intel CEO",
                     "add me to the tracker"):
            with self.subTest(line=line):
                outcome = self._handle(line)
                self.assertFalse(outcome.changed)
        self.assertEqual(self._names(), ["Jensen Huang", "Satya Nadella", "Michael Brown"])

    def test_a_misspelt_target_word_is_refused_not_written(self):
        """"to trackerr" is one slipped key away from the phrase we strip, and
        writing it would cost a person who never matches a headline."""
        outcome = self._handle("add Tim Cook to trackerr")
        self.assertFalse(outcome.changed)
        self.assertNotIn("Tim Cook to trackerr", self._names())

    def test_a_removal_is_forgiving_where_an_addition_cannot_be(self):
        """A removal can only ever hit somebody already on the list, so a
        misspelt tail is worth looking past rather than refusing."""
        outcome = self._handle("remove Jensen Huang from trackerr")
        self.assertTrue(outcome.changed)
        self.assertNotIn("Jensen Huang", self._names())

    def test_a_refusal_leaves_the_list_object_identical(self):
        """apply() decides applied-vs-rejected by object identity, so every
        refusal has to hand the same dict straight back."""
        raw = {"executives": [dict(e) for e in WATCHLIST["executives"]]}
        for call in (lambda: commands._add(raw, "Apple company"),
                     lambda: commands._add(raw, "Tim Cook tracker"),
                     lambda: commands._add(raw, "Nvidia"),
                     lambda: commands._remove(raw, "Nvidia company"),
                     lambda: commands._remove(raw, "Somebody Else")):
            with self.subTest(call=call):
                new, message = call()
                self.assertIs(new, raw)
                self.assertTrue(message)


class TestRemovalReadsTheCompany(ListCase):
    def test_a_name_with_the_company_attached_still_finds_the_person(self):
        outcome = self._handle("remove Michael Brown, Initech from the tracker")
        self.assertTrue(outcome.changed)
        self.assertNotIn("Michael Brown", self._names())

    def test_a_person_at_a_company_with_a_legal_suffix_is_still_removable(self):
        """"Umbrella Corporation" ends the line, but "Sam Director" starts it."""
        self._handle("add Sam Director, CFO, Umbrella Corporation")
        outcome = self._handle("remove Sam Director, CFO, Umbrella Corporation")
        self.assertTrue(outcome.changed)
        self.assertNotIn("Sam Director", self._names())

    def test_someone_already_tracked_is_removable_whatever_their_name_reads_like(self):
        """Marta Sa ends in a legal suffix as far as the company matcher is
        concerned. Being on the list settles it: the removal is tried before
        anything decides this is a company."""
        self._handle("add Marta Sa, Benfica")
        self.assertIn("Marta Sa", self._names())
        outcome = self._handle("remove Marta Sa")
        self.assertTrue(outcome.changed)
        self.assertNotIn("Marta Sa", self._names())

    def test_removing_a_bare_company_name_names_who_to_remove_instead(self):
        outcome = self._handle("remove Nvidia")
        self.assertFalse(outcome.changed)
        self.assertIn(GUIDANCE, outcome.rejected[0])
        self.assertIn("Jensen Huang (CEO, Nvidia)", outcome.rejected[0])

    def test_the_company_is_handed_on_only_if_the_list_can_use_it(self):
        """watchlist.remove takes no company today and may grow one. Asking
        the signature is what lets this work either way, so both answers are
        pinned rather than only the one that happens to be true now."""
        from interview_monitor import watchlist
        seen: list[tuple[str, str]] = []

        def fake_remove(raw, target, company=""):
            seen.append((target, company))
            return raw, "nothing to remove."

        real_remove = watchlist.remove
        real_flag = commands._REMOVE_TAKES_COMPANY
        try:
            watchlist.remove = fake_remove
            commands._REMOVE_TAKES_COMPANY = True
            commands._remove({"executives": []}, "Michael Brown, Initech")
            self.assertIn(("Michael Brown", "Initech"), seen)

            seen.clear()
            commands._REMOVE_TAKES_COMPANY = False
            commands._remove({"executives": []}, "Michael Brown, Initech")
            self.assertEqual([company for _, company in seen], ["", ""])
        finally:
            watchlist.remove = real_remove
            commands._REMOVE_TAKES_COMPANY = real_flag

    def test_an_unknown_person_is_reported_as_typed(self):
        outcome = self._handle("remove Somebody Else from the tracker")
        self.assertFalse(outcome.changed)
        self.assertIn("Somebody Else", outcome.rejected[0])


class TestOneParserForBothEntryPoints(unittest.TestCase):
    def test_email_and_config_still_read_a_person_identically(self):
        """The phrasing work must not fork the person parser: a reply and a
        config line that say the same thing describe the same person."""
        from interview_monitor.config import parse_person_line
        for text in ("Tim Cook", "Tim Cook, Apple, CEO", "Jensen Huang (CEO, Nvidia)",
                     "Andy Jassy at Amazon", "Alex Founder | Hooli", "Jane Doe, CEO",
                     "Lisa Su, chief executive of AMD", "Jane Smith-Executive"):
            with self.subTest(text=text):
                entry = parse_person_line(text)
                name, company, _ = commands.parse_person(text)
                self.assertEqual(name, entry.get("name", ""))
                self.assertEqual(company, entry.get("company", ""))


if __name__ == "__main__":
    unittest.main()
