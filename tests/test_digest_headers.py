"""The digest must not provoke an autoresponder.

The digest is sent from the same mailbox that reads replies as commands. An
out-of-office answering the digest therefore lands in the command inbox, where
it is either logged as noise or - if the absent person's autoresponder keeps
their own From: and passes DMARC - parsed as an instruction and answered, which
is the start of a ping-pong.

Suppressing at source is only safe if the suppression directive cannot end up
on a *human* reply, because the command reader skips incoming mail that carries
it. These tests pin both halves of that: the header goes out on the digest, and
a hand-written reply that does not carry it is still acted on.

Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interview_monitor import inbox, mailer  # noqa: E402

SUPPRESS_NAME, SUPPRESS_VALUE = mailer.AUTO_RESPONSE_SUPPRESS


def graph_headers(payload: dict) -> dict[str, str]:
    """The digest's custom headers, keyed the way inbox._header_map keys them."""
    return {
        str(h["name"]).lower(): str(h["value"])
        for h in payload["message"].get("internetMessageHeaders", [])
    }


class TestDigestSuppressesAutoResponses(unittest.TestCase):
    ENV = ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
           "EMAIL_FROM", "EMAIL_TO", "EMAIL_BACKEND",
           "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_SECURITY")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def _graph(self):
        os.environ.update({
            "GRAPH_TENANT_ID": "tenant-abc", "GRAPH_CLIENT_ID": "client-abc",
            "GRAPH_CLIENT_SECRET": "secret-abc",
            "EMAIL_FROM": "alerts@example.org",
            "EMAIL_TO": "you@example.com",
        })
        return mailer.GraphConfig.from_env()

    def _smtp(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "EMAIL_FROM": "alerts@example.org",
            "EMAIL_TO": "you@example.com",
        })
        return mailer.MailConfig.from_env()

    # -- outbound ---------------------------------------------------------- #

    def test_graph_digest_asks_for_no_auto_replies(self):
        payload = mailer.build_graph_payload("Subj", "plain", "<p>rich</p>", self._graph())
        # Assert on the raw list, not the dict view: a duplicated entry would
        # collapse in the dict and ship two copies of the directive.
        self.assertEqual(payload["message"]["internetMessageHeaders"],
                         [{"name": SUPPRESS_NAME, "value": SUPPRESS_VALUE}])

    def test_graph_custom_header_is_one_graph_will_accept(self):
        # Graph rejects sendMail outright unless every custom header starts x-.
        payload = mailer.build_graph_payload("Subj", "plain", "", self._graph())
        names = list(graph_headers(payload))
        self.assertTrue(names)
        for name in names:
            self.assertTrue(name.startswith("x-"), name)

    def test_graph_digest_is_otherwise_unchanged(self):
        self._graph()
        os.environ["EMAIL_TO"] = "you@example.com, ops@example.org"
        payload = mailer.build_graph_payload("Subj", "plain", "<p>rich</p>",
                                             mailer.GraphConfig.from_env())
        msg = payload["message"]
        self.assertEqual(msg["subject"], "Subj")
        self.assertEqual(msg["body"], {"contentType": "HTML", "content": "<p>rich</p>"})
        self.assertEqual(msg["from"]["emailAddress"]["address"],
                         "alerts@example.org")
        self.assertEqual([r["emailAddress"]["address"] for r in msg["toRecipients"]],
                         ["you@example.com", "ops@example.org"])
        self.assertFalse(payload["saveToSentItems"])

    def test_smtp_digest_carries_the_same_directive(self):
        # Both backends send the same digest; a person on SMTP should not get a
        # different autoresponder outcome from a person on Graph.
        msg = mailer.build_message("Subj", "plain", "", self._smtp())
        self.assertEqual(msg[SUPPRESS_NAME], SUPPRESS_VALUE)
        self.assertEqual(msg["X-Entity-Ref-ID"], "interview-digest")
        self.assertEqual(msg["Subject"], "Subj")

    def test_smtp_digest_sets_the_header_once(self):
        # EmailMessage appends rather than replaces, so a duplicate would ship
        # two copies of the directive.
        msg = mailer.build_message("Subj", "plain", "<p>rich</p>", self._smtp())
        self.assertEqual(len(msg.get_all(SUPPRESS_NAME) or []), 1)


class TestSuppressionDoesNotSilenceHumans(unittest.TestCase):
    """The reason the outbound header is safe, pinned as a test.

    inbox.authorize skips any incoming message carrying
    X-Auto-Response-Suppress. That is deliberate - it is how our own mail is
    recognised if it ever loops back - but it means the feature would break
    silently if a mail client copied the header from the digest onto a reply.
    Clients do not: a reply is a new message that inherits only Subject,
    In-Reply-To and References, and this header is a per-message delivery
    directive, not a thread property. These tests hold both sides of that line.
    """

    CFG = inbox.InboxConfig(
        tenant_id="t", client_id="c", client_secret="s",
        mailbox="alerts@example.org",
        senders=["you@example.com"],
    )

    def _reply(self, extra: dict[str, str] | None = None) -> inbox.Message:
        headers = {"authentication-results": "spf=pass; dkim=pass; dmarc=pass"}
        headers.update(extra or {})
        return inbox.Message(
            id="AAMk-1",
            subject="RE: Interview digest: 2 new interviews",
            sender="you@example.com",
            received="2026-08-12T09:00:00Z",
            body="remove Satya Nadella\n",
            headers=headers,
            internet_message_id="<abc@mail.example>",
        )

    def test_a_hand_written_reply_to_the_digest_is_still_acted_on(self):
        verdict = inbox.authorize(self._reply(), self.CFG)
        self.assertTrue(verdict.ok, verdict.reason)

    def test_the_headers_we_now_send_would_silence_a_reply_that_echoed_them(self):
        # Feed the digest's own headers back in as if a client had copied them
        # onto a reply. The message is skipped - which is what makes a looped
        # copy of our own digest safe, and equally what would break every human
        # reply if a client ever did echo the header. If that shows up in the
        # wild the fix is to narrow the loop guard to Auto-Submitted /
        # Precedence, not to stop suppressing autoresponses.
        cfg = mailer.GraphConfig("t", "c", "s", "alerts@example.org",
                                 ["you@example.com"])
        sent = graph_headers(mailer.build_graph_payload("Digest", "body", "", cfg))
        self.assertIn(SUPPRESS_NAME.lower(), sent)
        verdict = inbox.authorize(self._reply(sent), self.CFG)
        self.assertEqual(verdict.action, inbox.SKIP)

    def test_a_real_out_of_office_is_skipped_on_its_own_headers_too(self):
        # Belt and braces: suppression is a request, not a guarantee, so the
        # guard still has to catch an autoresponder that ignores it.
        verdict = inbox.authorize(
            self._reply({"auto-submitted": "auto-replied"}), self.CFG
        )
        self.assertEqual(verdict.action, inbox.SKIP)


if __name__ == "__main__":
    unittest.main()
