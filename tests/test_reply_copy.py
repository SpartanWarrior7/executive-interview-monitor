"""What the digest footer and the command reply actually say.

The reply channel is only usable if the email teaches it, so the wording is
behaviour here, not decoration: these tests pin the phrasings a reader is
shown and the order the pieces appear in. Offline - no network.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interview_monitor.commands import Outcome, help_text  # noqa: E402
from interview_monitor.config import Executive  # noqa: E402
from interview_monitor.monitor import RunResult  # noqa: E402
from interview_monitor.report import (  # noqa: E402
    FOOTER_EXAMPLES,
    FOOTER_NOTES,
    _esc,
    render_command_reply,
    render_digest_html,
    render_digest_text,
)

JENSEN = Executive(id="jensen-huang", name="Jensen Huang", company="Nvidia", title="CEO")
SATYA = Executive(id="satya-nadella", name="Satya Nadella", company="Microsoft", title="CEO")
LISTING = ["Jensen Huang (CEO, Nvidia)", "Satya Nadella (CEO, Microsoft)"]

# Taken from commands.help_text() rather than copied, so rewording the
# instructions there does not turn into a false failure here.
HELP_HEAD = next(line.strip() for line in help_text() if line.strip())


class TestFooterCopy(unittest.TestCase):
    ENV = ("COMMAND_MAILBOX",)

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        os.environ["COMMAND_MAILBOX"] = "alerts@example.org"

    def tearDown(self):
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def _digest(self, *executives: Executive) -> tuple[str, str]:
        result = RunResult(
            executives=list(executives) or [JENSEN, SATYA],
            new_items=[], candidates=0, errors=[],
        )
        return render_digest_text(result), render_digest_html(result)

    def _monospace_block(self, html: str) -> str:
        """The examples paragraph, which must not swallow the notes."""
        marker = 'monospace;color:#111827">'
        start = html.index(marker) + len(marker)
        return html[start:html.index("</p>", start)]

    def test_footer_teaches_the_wording_people_reach_for(self):
        text, html = self._digest()
        for phrasing in ("add Jane Doe to tracker",
                         "remove Michael Brown, Initech from tracker"):
            self.assertIn(phrasing, text)
            self.assertIn(_esc(phrasing), html)

    def test_footer_still_shows_the_terse_form(self):
        text, html = self._digest()
        self.assertIn("add Tim Cook, Apple CEO", text)
        self.assertIn("add Tim Cook, Apple CEO", html)

    def test_footer_shows_a_company_used_for_disambiguation(self):
        text, _ = self._digest()
        self.assertIn(", Initech", text)
        self.assertIn("share a name", text)

    def test_footer_heads_off_adding_a_company(self):
        text, html = self._digest()
        self.assertIn("people, not companies", text)
        self.assertIn("people, not companies", html)

    def test_examples_and_notes_reach_both_renderings(self):
        text, html = self._digest()
        for line in FOOTER_EXAMPLES + FOOTER_NOTES:
            self.assertIn(line, text)
            self.assertIn(_esc(line), html)

    def test_html_keeps_notes_out_of_the_example_block(self):
        # Every note used to have to be the single last line; more than one
        # would have been typeset as if it were a command to copy.
        _, html = self._digest()
        block = self._monospace_block(html)
        for example in FOOTER_EXAMPLES:
            self.assertIn(_esc(example), block)
        for note in FOOTER_NOTES:
            self.assertNotIn(_esc(note), block)

    def test_html_escapes_the_footer_copy(self):
        _, html = self._digest()
        self.assertIn("&quot;help&quot;", html)

    def test_footer_counts_the_people_it_tracks(self):
        one, _ = self._digest(JENSEN)
        two, _ = self._digest(JENSEN, SATYA)
        self.assertIn("Tracking 1 person.", one)
        self.assertIn("Tracking 2 people.", two)

    def test_footer_is_silent_when_the_channel_is_off(self):
        os.environ.pop("COMMAND_MAILBOX", None)
        text, html = self._digest()
        for rendered in (text, html):
            self.assertNotIn("to tracker", rendered)
            self.assertNotIn("add Tim Cook", rendered)


class TestCommandReplyCopy(unittest.TestCase):
    def test_the_list_is_last_even_when_the_instructions_are_owed(self):
        outcome = Outcome(unparsed=["hello there"], listing=list(LISTING))
        self.assertTrue(outcome.needs_help)
        text, html = render_command_reply(outcome)

        self.assertTrue(text.rstrip().endswith("2. Satya Nadella (CEO, Microsoft)"), text)
        # The instructions come first; the list is the answer, so it closes.
        self.assertLess(text.index(HELP_HEAD), text.index("Now tracking"))
        self.assertLess(html.index(_esc(HELP_HEAD)), html.index("<ol"))
        self.assertTrue(html.rstrip().endswith("</div>"), html)

    def test_an_unreadable_line_is_quoted_back_in_both_renderings(self):
        text, html = render_command_reply(
            Outcome(unparsed=["hello there"], listing=list(LISTING))
        )
        self.assertIn('You wrote: "hello there"', text)
        self.assertIn("You wrote: &quot;hello there&quot;", html)

    def test_a_rejected_instruction_still_gets_told_what_to_type(self):
        outcome = Outcome(rejected=["I do not track Somebody Else."], listing=list(LISTING))
        text, html = render_command_reply(outcome)
        self.assertIn(HELP_HEAD, text)
        self.assertIn(_esc(HELP_HEAD), html)
        self.assertTrue(text.rstrip().endswith("2. Satya Nadella (CEO, Microsoft)"), text)

    def test_a_reply_that_worked_is_not_lectured(self):
        outcome = Outcome(applied=["Now tracking Tim Cook (CEO, Apple)."],
                          changed=True, listing=list(LISTING))
        text, html = render_command_reply(outcome)
        self.assertIn("Your list has been updated.", text)
        self.assertNotIn(HELP_HEAD, text)
        self.assertNotIn(_esc(HELP_HEAD), html)
        self.assertTrue(text.rstrip().endswith("2. Satya Nadella (CEO, Microsoft)"), text)

    def test_a_quoted_line_cannot_smuggle_markup_into_the_reply(self):
        _, html = render_command_reply(
            Outcome(unparsed=["<script>alert(1)</script>"], listing=list(LISTING))
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
