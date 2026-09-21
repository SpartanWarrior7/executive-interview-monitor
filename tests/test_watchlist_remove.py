"""Removing the right person when two of them share a name.

Offline. Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interview_monitor import watchlist  # noqa: E402

NAMESAKES = {
    "executives": [
        {"id": "michael-brown", "name": "Michael Brown", "company": "Initech Inc."},
        {"id": "michael-brown-acme", "name": "Michael Brown", "company": "Acme",
         "company_aliases": ["Acme Corp", "ACM"]},
        {"id": "jensen-huang", "name": "Jensen Huang", "company": "Nvidia Corporation",
         "aliases": ["Jen-Hsun Huang"]},
    ]
}


def raw() -> dict:
    return json.loads(json.dumps(NAMESAKES))


def names(new: dict) -> list[str]:
    return [f"{e['name']} ({e['company']})" for e in new["executives"]]


class TestRemoveByCompany(unittest.TestCase):
    def test_company_picks_the_right_namesake(self):
        before = raw()
        new, message = watchlist.remove(before, "Michael Brown", company="Initech")
        self.assertIsNot(new, before)  # identity is how commands.apply reads success
        self.assertEqual(names(new),
                         ["Michael Brown (Acme)", "Jensen Huang (Nvidia Corporation)"])
        self.assertIn("Stopped tracking Michael Brown (Initech Inc.)", message)

    def test_company_is_accepted_positionally_and_by_keyword(self):
        positional, _ = watchlist.remove(raw(), "Michael Brown", "Acme")
        keyword, _ = watchlist.remove(raw(), "Michael Brown", company="Acme")
        self.assertEqual(names(positional), names(keyword))
        self.assertEqual(names(keyword),
                         ["Michael Brown (Initech Inc.)", "Jensen Huang (Nvidia Corporation)"])

    def test_legal_suffixes_do_not_have_to_be_typed(self):
        """The file says "Initech Inc."; nobody types that."""
        for spelling in ("Initech", "initech", "Initech Inc.", "  INITECH  "):
            new, message = watchlist.remove(raw(), "Michael Brown", company=spelling)
            self.assertEqual(names(new)[0], "Michael Brown (Acme)", spelling)
            self.assertIn("Stopped tracking", message)

    def test_a_company_alias_identifies_the_person_too(self):
        new, _ = watchlist.remove(raw(), "Michael Brown", company="ACM")
        self.assertEqual(names(new)[0], "Michael Brown (Initech Inc.)")

    def test_null_alias_fields_do_not_crash_the_mail_run(self):
        """A hand-edited file can say "aliases": null. One bad instruction is
        survivable; an exception loses every other instruction in the email."""
        before = {"executives": [
            {"name": "Michael Brown", "company": None, "aliases": None,
             "company_aliases": None},
            {"id": "mb2", "name": "Michael Brown", "company": "Acme"},
            {"id": "nameless", "name": None, "aliases": [None, 5]},
            {"name": "Jensen Huang"},
        ]}
        new, message = watchlist.remove(before, "Michael Brown", company="Acme")
        self.assertEqual([e["name"] for e in new["executives"]],
                         ["Michael Brown", None, "Jensen Huang"])
        self.assertIn("Stopped tracking Michael Brown (Acme)", message)

    def test_an_id_still_names_exactly_one_person(self):
        """The old escape hatch has to keep working alongside the new one."""
        before = raw()
        new, _ = watchlist.remove(before, "michael-brown-acme", company="Acme")
        self.assertIsNot(new, before)
        self.assertEqual(names(new)[0], "Michael Brown (Initech Inc.)")

    def test_a_namesake_with_no_company_on_file_loses_the_tie_break(self):
        """Documented judgement call: a positive match beats an unknown. The
        reply names who went and lists who is left, so it is visible."""
        before = {"executives": [
            {"id": "mb1", "name": "Michael Brown"},
            {"id": "mb2", "name": "Michael Brown", "company": "Initech"},
            {"name": "Jensen Huang"},
        ]}
        new, message = watchlist.remove(before, "Michael Brown", company="Initech")
        self.assertEqual([e.get("id") for e in new["executives"]][0], "mb1")
        self.assertIn("Stopped tracking Michael Brown (Initech)", message)

    def test_junk_in_a_company_field_is_ignored_not_fatal(self):
        """These files are hand-edited. One bad field must not lose every
        other instruction in the same email."""
        before = {"executives": [
            {"id": "mb1", "name": "Michael Brown", "company": 123,
             "company_aliases": {"nope": True}},
            {"id": "mb2", "name": "Michael Brown", "company": "Acme"},
            {"name": "Jensen Huang"},
        ]}
        new, message = watchlist.remove(before, "Michael Brown", company="Acme")
        self.assertEqual([e.get("id") for e in new["executives"]][0], "mb1")
        self.assertIn("Stopped tracking Michael Brown (Acme)", message)
        # And the same entry must not crash the plain ambiguity path either.
        same, refusal = watchlist.remove(before, "Michael Brown")
        self.assertIs(same, before)
        self.assertIn("matches more than one person", refusal)

    def test_a_company_alias_written_as_a_bare_string_is_one_alias(self):
        """Iterating the string instead would read it a letter at a time, and
        "X" is a real company name."""
        before = {"executives": [
            {"id": "x", "name": "Michael Brown", "company": "Twitter",
             "company_aliases": "Xerox"},
            {"id": "mb2", "name": "Michael Brown", "company": "Acme"},
            {"name": "Jensen Huang"},
        ]}
        new, message = watchlist.remove(before, "Michael Brown", company="X")
        self.assertIs(new, before)
        self.assertIn("none of them is at X", message)
        removed, _ = watchlist.remove(before, "Michael Brown", company="Xerox")
        self.assertEqual([e.get("id") for e in removed["executives"]][0], "mb2")


class TestRemoveStillRefuses(unittest.TestCase):
    def test_a_bare_name_is_still_ambiguous(self):
        before = raw()
        new, message = watchlist.remove(before, "Michael Brown")
        self.assertIs(new, before)  # identity means nothing happened
        self.assertIn("matches more than one person", message)
        self.assertIn("Michael Brown (Initech Inc.)", message)
        self.assertIn("Michael Brown (Acme)", message)

    def test_the_ambiguous_reply_shows_how_to_be_specific(self):
        _, message = watchlist.remove(raw(), "Michael Brown")
        self.assertIn("remove Michael Brown, Initech Inc.", message)

    def test_ambiguity_with_no_companies_on_file_asks_plainly(self):
        before = {"executives": [{"name": "Michael Brown"},
                                 {"id": "mb2", "name": "Michael Brown"},
                                 {"name": "Jensen Huang"}]}
        new, message = watchlist.remove(before, "Michael Brown")
        self.assertIs(new, before)
        self.assertIn("Be more specific.", message)
        self.assertNotIn("for example", message)

    def test_an_unknown_company_never_guesses_a_namesake(self):
        before = raw()
        new, message = watchlist.remove(before, "Michael Brown", company="Umbrella")
        self.assertIs(new, before)
        self.assertIn("none of them is at Umbrella", message)
        self.assertIn("Michael Brown (Acme)", message)

    def test_two_namesakes_at_the_same_company_still_refuse(self):
        before = {"executives": [
            {"id": "mb1", "name": "Michael Brown", "company": "Initech", "title": "CEO"},
            {"id": "mb2", "name": "Michael Brown", "company": "Initech", "title": "CFO"},
            {"name": "Jensen Huang", "company": "Nvidia"},
        ]}
        new, message = watchlist.remove(before, "Michael Brown", company="Initech")
        self.assertIs(new, before)
        self.assertIn("matches more than one person", message)
        self.assertIn("CFO", message)
        self.assertIn("Be more specific.", message)
        # Asking again for the company they just gave would read like a loop.
        self.assertNotIn("Add the company", message)

    def test_a_company_that_narrows_but_does_not_settle_it_still_refuses(self):
        """Two of the three namesakes survive the company filter. The third
        drops out of the message - it is not who they meant."""
        before = {"executives": [
            {"id": "mb1", "name": "Michael Brown", "company": "Initech", "title": "CEO"},
            {"id": "mb2", "name": "Michael Brown", "company": "Initech Inc.", "title": "CFO"},
            {"id": "mb3", "name": "Michael Brown", "company": "Acme"},
        ]}
        new, message = watchlist.remove(before, "Michael Brown", company="Initech")
        self.assertIs(new, before)
        self.assertIn("matches more than one person", message)
        self.assertIn("Be more specific.", message)
        self.assertNotIn("Acme", message)

    def test_a_missing_company_is_tolerated_as_none(self):
        """The caller parses the company out of an email line and may have none."""
        before = raw()
        new, message = watchlist.remove(before, "Michael Brown", None)
        self.assertIs(new, before)
        self.assertIn("matches more than one person", message)
        removed, _ = watchlist.remove(raw(), "Jensen Huang", company=None)
        self.assertEqual(len(removed["executives"]), 2)

    def test_the_last_person_cannot_be_removed_by_company(self):
        before = {"executives": [{"name": "Michael Brown", "company": "Initech"}]}
        new, message = watchlist.remove(before, "Michael Brown", company="Initech")
        self.assertIs(new, before)
        self.assertIn("cannot be empty", message)

    def test_an_unknown_name_is_unknown_whatever_the_company(self):
        before = raw()
        new, message = watchlist.remove(before, "Someone Else", company="Initech")
        self.assertIs(new, before)
        self.assertIn("Not tracking", message)

    def test_an_empty_target_is_still_refused(self):
        before = raw()
        new, message = watchlist.remove(before, "   ", company="Initech")
        self.assertIs(new, before)
        self.assertIn("could not tell who to remove", message)


class TestRemoveWithoutCompanyIsUnchanged(unittest.TestCase):
    """The simple path has to keep working: Unit 1 may call remove(raw, target)."""

    def test_a_call_with_no_company_still_removes(self):
        for target in ("Jensen Huang", "jensen huang", "jensen-huang", "Jen-Hsun Huang"):
            new, message = watchlist.remove(raw(), target)
            self.assertNotIn("Jensen Huang (Nvidia Corporation)", names(new), target)
            self.assertIn("Stopped tracking", message)

    def test_a_company_is_ignored_when_only_one_person_matches(self):
        """An entry often carries no company at all, and a redundant or
        mistaken company must not turn an unambiguous request into a refusal."""
        for company in ("", "Nvidia", "Umbrella"):
            new, message = watchlist.remove(raw(), "Jensen Huang", company=company)
            self.assertIn("Stopped tracking Jensen Huang", message, company)
            self.assertEqual(len(new["executives"]), 2)

    def test_add_still_appends_untouched(self):
        before = raw()
        new, message = watchlist.add(before, name="Tim Cook", company="Apple", title="CEO")
        self.assertIsNot(new, before)
        self.assertEqual(new["executives"][-1],
                         {"id": "tim-cook", "name": "Tim Cook",
                          "company": "Apple", "title": "CEO"})
        self.assertIn("Now tracking Tim Cook (CEO, Apple)", message)


class TestRemoveThroughDisk(unittest.TestCase):
    ENV = ("WATCHLIST_PATH",)

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "executives.json"
        self.path.write_text(json.dumps(NAMESAKES), encoding="utf-8")
        os.environ["WATCHLIST_PATH"] = str(self.path)

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_the_survivor_is_still_on_disk_and_still_loadable(self):
        path = watchlist.resolve_path()
        self.assertEqual(path, self.path)
        new, _ = watchlist.remove(watchlist.load_raw(path), "Michael Brown",
                                  company="Initech")
        watchlist.save(path, new)

        reloaded = watchlist.load_raw(path)
        self.assertEqual([e["id"] for e in reloaded["executives"]],
                         ["michael-brown-acme", "jensen-huang"])
        self.assertEqual(reloaded["executives"][0]["company_aliases"], ["Acme Corp", "ACM"])

    def test_a_refusal_leaves_the_file_alone(self):
        path = watchlist.resolve_path()
        before = self.path.read_text(encoding="utf-8")
        raw_on_disk = watchlist.load_raw(path)
        new, _ = watchlist.remove(raw_on_disk, "Michael Brown")
        self.assertIs(new, raw_on_disk)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_a_shorthand_entry_can_be_removed_by_company(self):
        """Bare strings carry their own company, so the disambiguation has to
        survive load_raw's parsing of them."""
        self.path.write_text(json.dumps({"executives": [
            "Michael Brown (Initech)",
            "Michael Brown, Acme",
            "Jensen Huang (CEO, Nvidia)",
        ]}), encoding="utf-8")
        path = watchlist.resolve_path()
        new, message = watchlist.remove(watchlist.load_raw(path), "Michael Brown",
                                        company="Acme")
        watchlist.save(path, new)
        self.assertIn("Stopped tracking Michael Brown (Acme)", message)
        self.assertEqual([e["name"] for e in watchlist.load_raw(path)["executives"]],
                         ["Michael Brown", "Jensen Huang"])
        self.assertEqual(watchlist.load_raw(path)["executives"][0]["company"], "Initech")


if __name__ == "__main__":
    unittest.main()
