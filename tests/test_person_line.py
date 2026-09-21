"""A bracketed aside in the middle of a line must not swallow the name.

Someone replying to the digest writes "add Tim Cook (Apple CEO) to the
tracker", not the tidy "Tim Cook (Apple CEO)" the config file gets. The
parser is shared by both, so these tests pin the new shape alongside every
shape that already worked - the two readers must not drift apart again.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interview_monitor.commands import parse_person
from interview_monitor.config import parse_person_line


# (line, name, company, title) - "" means the key must be absent.
SHAPES = [
    ("Jensen Huang", "Jensen Huang", "", ""),
    ("Jensen Huang (Nvidia)", "Jensen Huang", "Nvidia", ""),
    ("Jensen Huang (CEO, Nvidia)", "Jensen Huang", "Nvidia", "CEO"),
    ("Jensen Huang, Nvidia", "Jensen Huang", "Nvidia", ""),
    ("Jensen Huang, CEO, Nvidia", "Jensen Huang", "Nvidia", "CEO"),
    ("Jensen Huang - Nvidia", "Jensen Huang", "Nvidia", ""),
    ("Jensen Huang | Nvidia", "Jensen Huang", "Nvidia", ""),
    ("Jensen Huang – Nvidia", "Jensen Huang", "Nvidia", ""),
    ("Jensen Huang — Nvidia", "Jensen Huang", "Nvidia", ""),
    ("Andy Jassy at Amazon", "Andy Jassy", "Amazon", ""),
    ("Tim Cook, Apple CEO", "Tim Cook", "Apple", "CEO"),
    ("Sam Director, CFO, Umbrella Corporation",
     "Sam Director", "Umbrella Corporation", "CFO"),
    ("Pat Chairperson (Soylent)", "Pat Chairperson", "Soylent", ""),
    ("Jane Doe, CEO", "Jane Doe", "", "CEO"),
    ("Jane Smith-Executive", "Jane Smith-Executive", "", ""),
    ("Jen-Hsun Huang", "Jen-Hsun Huang", "", ""),
    ("Brian Moynihan, Bank of America CEO", "Brian Moynihan", "Bank of America", "CEO"),
    ("Lisa Su, chief executive of AMD", "Lisa Su", "AMD", "chief executive"),
]


class TestShapesThatAlreadyWorked(unittest.TestCase):
    """Every documented shape, re-pinned. Loosening the bracket rule must not
    change which of the later branches fires for any of them."""

    def test_documented_shapes(self):
        for text, name, company, title in SHAPES:
            with self.subTest(text=text):
                entry = parse_person_line(text)
                self.assertEqual(entry.get("name", ""), name)
                self.assertEqual(entry.get("company", ""), company)
                self.assertEqual(entry.get("title", ""), title)

    def test_a_plain_name_gains_nothing(self):
        # No stray empty keys: a bare name is still exactly one key.
        self.assertEqual(parse_person_line("Jensen Huang"), {"name": "Jensen Huang"})


class TestBracketsAwayFromTheEnd(unittest.TestCase):
    def test_words_after_the_brackets_no_longer_hide_the_company(self):
        entry = parse_person_line("Tim Cook (Apple CEO) to the tracker")
        self.assertEqual(entry.get("company"), "Apple")
        self.assertEqual(entry.get("title"), "CEO")
        # The leftover words stay with the name rather than being silently
        # dropped. Recognising "to the tracker" as addressing the tracker
        # belongs to the email layer, which does not do it yet, so the name is
        # still wrong here - but the company and the job title are now right,
        # and this parser also reads executives.json, where no such phrase
        # exists.
        self.assertEqual(entry["name"], "Tim Cook to the tracker")

    def test_company_alone_in_brackets_mid_line(self):
        entry = parse_person_line("Tim Cook (Apple) to the tracker")
        self.assertEqual(entry.get("company"), "Apple")
        self.assertNotIn("title", entry)

    def test_square_brackets_too(self):
        entry = parse_person_line("Tim Cook [Apple] please")
        self.assertEqual(entry.get("company"), "Apple")
        self.assertEqual(entry["name"], "Tim Cook please")

    def test_bracketed_title_lets_a_connector_still_find_the_company(self):
        entry = parse_person_line("Jensen Huang (CEO) at Nvidia")
        self.assertEqual(entry["name"], "Jensen Huang")
        self.assertEqual(entry.get("title"), "CEO")
        self.assertEqual(entry.get("company"), "Nvidia")

    def test_bracketed_title_lets_a_comma_still_find_the_company(self):
        entry = parse_person_line("Jensen Huang (CEO), Nvidia")
        self.assertEqual(entry["name"], "Jensen Huang")
        self.assertEqual(entry.get("title"), "CEO")
        self.assertEqual(entry.get("company"), "Nvidia")

    def test_brackets_leading_the_line(self):
        entry = parse_person_line("(Nvidia) Jensen Huang")
        self.assertEqual(entry["name"], "Jensen Huang")
        self.assertEqual(entry.get("company"), "Nvidia")

    def test_trailing_punctuation_does_not_stick_to_the_name(self):
        for text in ("Jensen Huang (Nvidia).", "Jensen Huang (Nvidia),",
                     "Jensen Huang (Nvidia) ;"):
            with self.subTest(text=text):
                entry = parse_person_line(text)
                self.assertEqual(entry["name"], "Jensen Huang")
                self.assertEqual(entry.get("company"), "Nvidia")

    def test_brackets_that_are_not_the_company_do_not_claim_to_be(self):
        """An aside with words after it loses to a separator. People bracket
        all sorts of things - a maiden name, a former employer, a pronoun -
        and the company is what comes after the comma."""
        for text, company in (
            ("Jane Doe (nee Smith), Acme", "Acme"),
            ("Jane Doe (formerly Initech), Acme Corp", "Acme Corp"),
            ("Jensen Huang (pictured) - Nvidia", "Nvidia"),
            ("Robert Smith (Vista Equity) | Vista", "Vista"),
        ):
            with self.subTest(text=text):
                entry = parse_person_line(text)
                self.assertEqual(entry.get("company"), company)
                # The separator must not be left stranded in the name either.
                self.assertEqual(entry["name"], text.split(" (")[0])

    def test_a_pronoun_aside_does_not_cost_the_job_title(self):
        entry = parse_person_line("Tim Cook (he/him), Apple CEO")
        self.assertEqual(entry["name"], "Tim Cook")
        self.assertEqual(entry.get("company"), "Apple")
        self.assertEqual(entry.get("title"), "CEO")

    def test_the_aside_fills_only_what_the_rest_of_the_line_missed(self):
        for text, name, company, title in (
            ("Jane Doe (CFO), Acme", "Jane Doe", "Acme", "CFO"),
            ("Jane Doe (Acme), CEO", "Jane Doe", "Acme", "CEO"),
            ("Jensen Huang (Nvidia) — chief", "Jensen Huang", "Nvidia", "chief"),
            ("Lisa Su (AMD) - chief executive of AMD",
             "Lisa Su", "AMD", "chief executive"),
        ):
            with self.subTest(text=text):
                entry = parse_person_line(text)
                self.assertEqual(entry["name"], name)
                self.assertEqual(entry.get("company"), company)
                self.assertEqual(entry.get("title"), title)

    def test_a_connector_in_the_leftover_words_does_not_outrank_the_brackets(self):
        # "from" here is addressing the tracker, not introducing an employer.
        # Stripping that phrase is commands.py's job; not being fooled by it
        # is this parser's.
        entry = parse_person_line("Jensen Huang (Nvidia) from the tracker")
        self.assertEqual(entry.get("company"), "Nvidia")

    def test_brackets_do_not_reach_across_a_line_break(self):
        # One person per line: a bracket on one line must not pick up the next.
        self.assertEqual(parse_person_line("Jensen Huang (Nvidia)\nCEO"),
                         {"name": "Jensen Huang (Nvidia)\nCEO"})

    def test_a_full_stop_inside_the_remaining_words_survives(self):
        # Only punctuation that is all that follows gets dropped, so a suffix
        # keeps its own full stop.
        entry = parse_person_line("Bo Yang (Acme) Jr.")
        self.assertEqual(entry["name"], "Bo Yang Jr.")
        self.assertEqual(entry.get("company"), "Acme")

    def test_last_brackets_on_the_line_still_win(self):
        # Unchanged from before: with two asides, the later one is read as the
        # employer, because that is where people put it.
        entry = parse_person_line("Jane Doe (formerly Initech) (Acme)")
        self.assertEqual(entry.get("company"), "Acme")

    def test_a_name_that_merely_contains_a_bracket_is_left_alone(self):
        self.assertEqual(parse_person_line("Jane Smith-Executive"),
                         {"name": "Jane Smith-Executive"})


class TestBothReadersStillAgree(unittest.TestCase):
    """commands.parse_person and config.parse_person_line are one parser; a
    reply and a config line saying the same thing must not disagree."""

    def test_same_answer_from_both_doors(self):
        texts = [shape[0] for shape in SHAPES] + [
            "Tim Cook (Apple CEO) to the tracker",
            "Jensen Huang (CEO) at Nvidia",
            "(Nvidia) Jensen Huang",
            "Jensen Huang (Nvidia).",
        ]
        for text in texts:
            with self.subTest(text=text):
                entry = parse_person_line(text)
                name, company, title = parse_person(text)
                self.assertEqual(name, entry.get("name", ""))
                self.assertEqual(company, entry.get("company", ""))
                # parse_person only capitalises the job title; same words.
                self.assertEqual(title.lower(), entry.get("title", "").lower())


if __name__ == "__main__":
    unittest.main()
