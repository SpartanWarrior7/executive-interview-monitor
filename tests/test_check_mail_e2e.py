"""Offline end-to-end test of --check-mail - no network, no credentials.

Run: python -m unittest discover tests

Every other mail test checks one piece in isolation: the parser, the
authorization verdict, the Graph request shapes. Nothing proved that the whole
loop is wired together, so a change that left `check_mail` reading the reply
but never writing the file - or, worse, replying to forged mail - would pass
the suite. This drives `cli.main(["--check-mail", ...])` with Microsoft Graph
replaced by an in-process fake and asserts only on what crossed the boundary:
what is on disk afterwards and what hit the wire.

Deliberately not asserted: how many Graph calls a run makes, or the exact
wording of a reply beyond a short phrase. Those are free to change.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interview_monitor import cli, mailer  # noqa: E402
from interview_monitor.store import Store  # noqa: E402

DMARC_PASS = "spf=pass (sender ip is 1.2.3.4) smtp.mailfrom=example.com; dkim=pass; dmarc=pass"
SENDER = "you@example.com"
MAILBOX = "alerts@example.org"
MESSAGE_ID = "<abc@mail.example>"

STARTING_LIST = {
    "executives": [
        {"id": "jensen-huang", "name": "Jensen Huang", "company": "Nvidia"},
        {"id": "satya-nadella", "name": "Satya Nadella", "company": "Microsoft"},
    ]
}


class TestCheckMailEndToEnd(unittest.TestCase):
    ENV = ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
           "COMMAND_MAILBOX", "COMMAND_SENDERS", "EMAIL_TO", "EMAIL_FROM",
           "EMAIL_BACKEND", "WATCHLIST_PATH")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.watchlist = Path(self._tmp.name, "executives.json")
        self._write_watchlist(STARTING_LIST)
        self.state = str(Path(self._tmp.name, "seen.sqlite3"))

        self._saved = {k: os.environ.get(k) for k in self.ENV}
        for key in self.ENV:
            os.environ.pop(key, None)
        os.environ.update({
            "GRAPH_TENANT_ID": "tenant", "GRAPH_CLIENT_ID": "client",
            "GRAPH_CLIENT_SECRET": "secret",
            "COMMAND_MAILBOX": MAILBOX,
            "COMMAND_SENDERS": SENDER,
            "EMAIL_TO": SENDER,
            "WATCHLIST_PATH": str(self.watchlist),
        })

        self.calls: list[dict] = []
        self.unread: list[dict] = [self._graph_message()]
        # What a per-message header fetch answers with, if one is made at all.
        self.per_message_headers: dict = {}
        self._pages_served = 0
        self._real_request = mailer._request
        mailer._request = self._fake_request

    def tearDown(self):
        mailer._request = self._real_request
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    # ----------------------------------------------------------------- #
    # A Graph that answers from memory
    # ----------------------------------------------------------------- #

    def _fake_request(self, url, *, method="GET", data=None, headers=None, timeout=30):
        headers = headers or {}
        # The token call posts a form; only Graph calls post JSON.
        is_json = "json" in headers.get("Content-Type", "")
        self.calls.append({
            "method": method,
            "url": url,
            "headers": headers,
            "body": json.loads(data.decode("utf-8")) if data and is_json else None,
        })
        return self._route(method, url)

    def _route(self, method: str, url: str) -> tuple[int, bytes]:
        """Answer by what is being asked for, not by call order, so that a
        change to how many requests a run makes does not rewrite this file."""
        if url.startswith(mailer.LOGIN_ROOT):
            return 200, b'{"access_token":"t0ken"}'
        # Only the unread search filters; a header fetch names one message.
        if method == "GET" and "filter" in url.lower():
            self._pages_served += 1
            self.assertLess(self._pages_served, 10, "runaway paging loop")
            # Page two onwards is empty, so a paged fetch terminates whether it
            # follows @odata.nextLink or reads until the mailbox runs dry.
            value = self.unread if self._pages_served == 1 else []
            return 200, json.dumps({"value": value}).encode()
        if method == "GET" and "internetMessageHeaders" in url:
            return 200, json.dumps(self.per_message_headers).encode()
        if method == "POST" and url.endswith("/reply"):
            return 200, b"{}"
        if method == "PATCH":
            return 200, b"{}"
        raise AssertionError(f"unexpected Graph call: {method} {url}")

    def _graph_message(self, *, body="remove Satya Nadella\n", sender=SENDER,
                       message_id=MESSAGE_ID, headers=(("Authentication-Results", DMARC_PASS),)):
        payload = {
            "id": "AAMk-1",
            "subject": "RE: Interview digest: 2 new interviews",
            "from": {"emailAddress": {"address": sender}},
            "receivedDateTime": "2026-08-12T09:00:00Z",
            "body": {"contentType": "text", "content": body},
            "internetMessageId": message_id,
        }
        if headers is not None:
            # Present on the collection, so no per-message header fetch is needed.
            payload["internetMessageHeaders"] = [
                {"name": name, "value": value} for name, value in headers
            ]
        return payload

    # ----------------------------------------------------------------- #
    # Running it
    # ----------------------------------------------------------------- #

    def _run(self, *extra: str) -> int:
        self._pages_served = 0
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return cli.main(["--check-mail", "--verbose", "--state", self.state, *extra])

    def _write_watchlist(self, raw: dict) -> None:
        self.watchlist.write_text(json.dumps(raw), encoding="utf-8")

    def _on_disk(self) -> list[dict]:
        return json.loads(self.watchlist.read_text(encoding="utf-8-sig"))["executives"]

    def _names(self) -> list[str]:
        return [e.get("name") for e in self._on_disk()]

    def _replies(self) -> list[str]:
        return [
            call["body"]["message"]["body"]["content"]
            for call in self.calls
            if call["method"] == "POST" and call["url"].endswith("/reply")
        ]

    def _marked_read(self) -> list[str]:
        return [
            call["url"] for call in self.calls
            if call["method"] == "PATCH" and (call["body"] or {}).get("isRead") is True
        ]

    def _recorded(self, key: str = MESSAGE_ID) -> bool:
        with Store(self.state) as store:
            return store.mail_processed(key)

    # ----------------------------------------------------------------- #
    # The loop, end to end
    # ----------------------------------------------------------------- #

    def test_a_reply_removes_someone_answers_and_closes_the_message(self):
        self.assertEqual(self._run(), 0, self.output.getvalue())

        self.assertEqual(self._names(), ["Jensen Huang"])
        self.assertTrue(self._recorded())

        self.assertEqual(len(self._replies()), 1, self.calls)
        reply = self._replies()[0]
        self.assertIn("Stopped tracking Satya Nadella", reply)
        # Self-verifying: the reply carries the list that is left.
        self.assertIn("Jensen Huang", reply)

        self.assertEqual(len(self._marked_read()), 1, self.calls)
        self.assertIn("AAMk-1", self._marked_read()[0])

    def test_a_reply_adds_someone_with_their_company_and_title(self):
        self.unread = [self._graph_message(body="add Tim Cook, Apple CEO\n")]
        self.assertEqual(self._run(), 0, self.output.getvalue())

        added = self._on_disk()[-1]
        self.assertEqual(added.get("name"), "Tim Cook")
        self.assertEqual(added.get("company"), "Apple")
        self.assertEqual(added.get("title"), "CEO")
        self.assertEqual(self._names(), ["Jensen Huang", "Satya Nadella", "Tim Cook"])
        self.assertIn("Now tracking Tim Cook", self._replies()[0])

    def test_the_same_message_twice_is_applied_once(self):
        # Marking a message read is a second network call that can fail after
        # the watchlist is written; the next tick must not redo the change.
        self.assertEqual(self._run(), 0, self.output.getvalue())
        self.assertEqual(self._run(), 0, self.output.getvalue())

        self.assertEqual(self._names(), ["Jensen Huang"])
        self.assertEqual(len(self._replies()), 1, "answered the same instruction twice")

    def test_an_unread_message_with_no_id_still_only_applies_once(self):
        # Graph has always given us internetMessageId, but the fallback key is
        # the message id, and it has to work the same way.
        self.unread = [self._graph_message(message_id="")]
        self.assertEqual(self._run(), 0, self.output.getvalue())
        self.assertEqual(self._run(), 0, self.output.getvalue())

        self.assertEqual(self._names(), ["Jensen Huang"])
        self.assertEqual(len(self._replies()), 1)
        self.assertTrue(self._recorded("AAMk-1"))

    # ----------------------------------------------------------------- #
    # Mail we must not act on
    # ----------------------------------------------------------------- #

    def test_a_stranger_is_ignored_and_never_answered(self):
        # Replying to a forged From: would make this mailbox a backscatter
        # source, so an unauthorized message earns silence, not an explanation.
        before = self.watchlist.read_bytes()
        self.unread = [self._graph_message(sender="stranger@example.com",
                                           body="remove Satya Nadella\n")]

        self.assertEqual(self._run(), 0, self.output.getvalue())

        self.assertEqual(self.watchlist.read_bytes(), before)
        self.assertEqual(self._replies(), [])
        # Still cleared from the inbox, or it would be reconsidered forever.
        self.assertEqual(len(self._marked_read()), 1, self.calls)
        self.assertTrue(self._recorded())

    def test_mail_with_no_authentication_results_is_not_trusted(self):
        before = self.watchlist.read_bytes()
        self.unread = [self._graph_message(headers=(("Received", "from somewhere"),))]

        self.assertEqual(self._run(), 0, self.output.getvalue())

        self.assertEqual(self.watchlist.read_bytes(), before)
        self.assertEqual(self._replies(), [])

    def test_mail_whose_headers_graph_will_not_return_fails_closed(self):
        # No headers on the collection and none from the per-message fetch
        # either: authentication is unknowable, which is not the same as fine.
        before = self.watchlist.read_bytes()
        self.unread = [self._graph_message(headers=None)]
        self.per_message_headers = {}

        self.assertEqual(self._run(), 0, self.output.getvalue())

        self.assertEqual(self.watchlist.read_bytes(), before)
        self.assertEqual(self._replies(), [])

    def test_a_forged_sender_that_fails_dmarc_is_ignored(self):
        before = self.watchlist.read_bytes()
        self.unread = [self._graph_message(
            headers=(("Authentication-Results", "spf=fail; dkim=none; dmarc=fail"),)
        )]

        self.assertEqual(self._run(), 0, self.output.getvalue())

        self.assertEqual(self.watchlist.read_bytes(), before)
        self.assertEqual(self._replies(), [])

    # ----------------------------------------------------------------- #
    # Dry run
    # ----------------------------------------------------------------- #

    def test_dry_run_touches_nothing(self):
        before = self.watchlist.read_bytes()

        self.assertEqual(self._run("--dry-run"), 0, self.output.getvalue())

        self.assertEqual(self.watchlist.read_bytes(), before)
        self.assertEqual(self._replies(), [])
        self.assertEqual(self._marked_read(), [])
        self.assertFalse(self._recorded())
        # It still says what it would have done.
        self.assertIn("Stopped tracking Satya Nadella", self.output.getvalue())

    def test_a_dry_run_does_not_stop_the_real_one(self):
        self.assertEqual(self._run("--dry-run"), 0, self.output.getvalue())
        self.assertEqual(self._run(), 0, self.output.getvalue())

        self.assertEqual(self._names(), ["Jensen Huang"])
        self.assertEqual(len(self._replies()), 1)

    # ----------------------------------------------------------------- #
    # Nothing to do, and nothing configured
    # ----------------------------------------------------------------- #

    def test_an_empty_mailbox_is_a_clean_exit(self):
        before = self.watchlist.read_bytes()
        self.unread = []
        self.assertEqual(self._run(), 0, self.output.getvalue())
        self.assertEqual(self.watchlist.read_bytes(), before)
        self.assertEqual(self._replies(), [])

    def test_without_credentials_it_explains_itself_instead_of_crashing(self):
        os.environ.pop("GRAPH_CLIENT_SECRET")
        self.assertEqual(self._run(), 2)
        self.assertIn("not configured", self.output.getvalue())
        self.assertEqual(self.calls, [], "reached for the network unconfigured")

    def test_a_watchlist_that_cannot_be_read_leaves_the_message_unread(self):
        # The instruction is not lost: no reply, no read flag, and a non-zero
        # exit so the timer's log says something went wrong.
        self.watchlist.write_text("{ this is not json", encoding="utf-8")

        self.assertEqual(self._run(), 3)

        self.assertEqual(self._replies(), [])
        self.assertEqual(self._marked_read(), [])
        self.assertFalse(self._recorded())


class TestCheckMailStateIsDurable(unittest.TestCase):
    """The idempotency key has to survive the process exiting."""

    def test_mail_seen_is_written_to_the_database_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "seen.sqlite3"))
            with Store(path) as store:
                store.record_mail("<x@y>", sender=SENDER, outcome="changed")
            with Store(path) as store:
                self.assertTrue(store.mail_processed("<x@y>"))
            conn = sqlite3.connect(path)
            try:
                rows = conn.execute(
                    "SELECT sender, outcome FROM mail_seen WHERE message_id = ?", ("<x@y>",)
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(rows, [(SENDER, "changed")])


if __name__ == "__main__":
    unittest.main()
