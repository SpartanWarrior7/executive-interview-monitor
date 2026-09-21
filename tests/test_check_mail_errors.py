"""What --check-mail does when the watchlist itself is broken.

A message that cannot be applied is left unread so the instruction survives
until someone fixes the file. These tests pin the other half of that: it is
left unread a bounded number of times, and then answered.

Offline - Microsoft Graph is faked at mailer._request.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interview_monitor import cli, mailer  # noqa: E402
from interview_monitor.store import Store  # noqa: E402

GOOD_LIST = {
    "executives": [
        {"id": "jensen-huang", "name": "Jensen Huang", "company": "Nvidia"},
        {"id": "satya-nadella", "name": "Satya Nadella", "company": "Microsoft"},
    ]
}

ENV = {
    "GRAPH_TENANT_ID": "tenant",
    "GRAPH_CLIENT_ID": "client",
    "GRAPH_CLIENT_SECRET": "secret",
    "COMMAND_MAILBOX": "alerts@example.org",
    "COMMAND_SENDERS": "you@example.com",
}


def graph_message(**kw) -> dict:
    payload = {
        "id": "AAMk-1",
        "subject": "RE: Interview digest: 2 new interviews",
        "from": {"emailAddress": {"address": "you@example.com"}},
        "receivedDateTime": "2026-08-12T09:00:00Z",
        "body": {"contentType": "text", "content": "remove Satya Nadella\n"},
        "internetMessageId": "<abc@mail.example>",
        "internetMessageHeaders": [
            {"name": "Authentication-Results", "value": "spf=pass; dkim=pass; dmarc=pass"}
        ],
    }
    payload.update(kw)
    return payload


class FakeGraph:
    """The four endpoints Mailbox touches, with a real read flag.

    Modelling isRead rather than replaying a canned response list is what
    makes a fifth run meaningful: once a message has been marked read it
    stops coming back, exactly as Exchange would stop returning it.
    """

    def __init__(self, *messages: dict):
        self.messages = {m["id"]: m for m in messages}
        self.unread = [m["id"] for m in messages]
        self.replies: list[tuple[str, str]] = []
        self.marked: list[str] = []
        self.fail_reply = False

    def install(self) -> None:
        mailer._request = self.request

    def request(self, url, *, method="GET", data=None, headers=None, timeout=30):
        is_json = "json" in (headers or {}).get("Content-Type", "")
        payload = json.loads(data.decode()) if data and is_json else None

        if url.startswith(mailer.LOGIN_ROOT):
            return 200, b'{"access_token":"t0ken"}'
        if "/mailFolders/inbox/messages?" in url:
            return 200, json.dumps(
                {"value": [self.messages[i] for i in self.unread]}
            ).encode()
        if url.endswith("/reply"):
            if self.fail_reply:
                return 403, b'{"error":{"code":"ErrorSendAsDenied","message":"no"}}'
            self.replies.append(
                (url.rsplit("/", 2)[-2], payload["message"]["body"]["content"])
            )
            return 202, b""
        if method == "PATCH" and payload == {"isRead": True}:
            message_id = url.rsplit("/", 1)[-1]
            self.marked.append(message_id)
            if message_id in self.unread:
                self.unread.remove(message_id)
            return 200, b"{}"
        raise AssertionError(f"unexpected {method} {url}")


class CheckMailCase(unittest.TestCase):
    """Base: a temp watchlist, a temp database and a faked mailbox."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watchlist = Path(self.tmp.name) / "executives.json"
        self.write_watchlist(json.dumps(GOOD_LIST))
        self.db = Path(self.tmp.name) / "seen.sqlite3"

        self._env = {k: os.environ.get(k) for k in ENV}
        os.environ.update(ENV)

        self._real_request = mailer._request
        self.graph = FakeGraph(graph_message())
        self.graph.install()

    def tearDown(self):
        mailer._request = self._real_request
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def write_watchlist(self, text: str) -> None:
        self.watchlist.write_text(text, encoding="utf-8")

    def run_check(self, *extra: str) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(
            ["--check-mail", "--verbose", "--config", str(self.watchlist),
             "--state", str(self.db), *extra]
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.check_mail(args)
        return code, out.getvalue(), err.getvalue()

    def tracked(self) -> list[str]:
        return [e["name"] for e in json.loads(
            self.watchlist.read_text(encoding="utf-8"))["executives"]]


class TestHappyPathStillWorks(CheckMailCase):
    def test_a_readable_watchlist_is_edited_and_answered(self):
        code, _, err = self.run_check()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.tracked(), ["Jensen Huang"])
        self.assertEqual(len(self.graph.replies), 1)
        self.assertIn("Stopped tracking Satya Nadella", self.graph.replies[0][1])
        self.assertEqual(self.graph.marked, ["AAMk-1"])

    def test_a_handled_message_is_remembered(self):
        self.run_check()
        with Store(self.db) as store:
            self.assertTrue(store.mail_processed("<abc@mail.example>"))

    def test_dry_run_changes_nothing_and_sends_nothing(self):
        code, out, _ = self.run_check("--dry-run")

        self.assertEqual(code, 0)
        self.assertIn("(dry run)", out)
        self.assertEqual(self.tracked(), ["Jensen Huang", "Satya Nadella"])
        self.assertEqual(self.graph.replies, [])
        self.assertEqual(self.graph.marked, [])
        with Store(self.db) as store:
            self.assertFalse(store.mail_processed("<abc@mail.example>"))


class TestCorruptWatchlist(CheckMailCase):
    def setUp(self):
        super().setUp()
        self.write_watchlist('{"executives": [')

    def test_early_attempts_leave_the_message_unread_and_unanswered(self):
        for attempt in range(1, cli.MAX_WATCHLIST_ATTEMPTS):
            code, _, err = self.run_check()
            self.assertEqual(code, 3, f"attempt {attempt}")
            self.assertIn("not valid JSON", err)
            self.assertEqual(self.graph.replies, [], f"attempt {attempt}")
            self.assertEqual(self.graph.marked, [], f"attempt {attempt}")
            self.assertEqual(self.graph.unread, ["AAMk-1"])
            with Store(self.db) as store:
                self.assertEqual(store.mail_attempts("<abc@mail.example>"), attempt)
                # Awaiting a retry is not the same as acted on.
                self.assertFalse(store.mail_processed("<abc@mail.example>"))

    def test_the_last_attempt_answers_once_and_gives_up(self):
        for _ in range(cli.MAX_WATCHLIST_ATTEMPTS):
            code, _, _ = self.run_check()

        # Reported, so no longer a failure the scheduler should shout about.
        self.assertEqual(code, 0)
        self.assertEqual(len(self.graph.replies), 1)
        message_id, body = self.graph.replies[0]
        self.assertEqual(message_id, "AAMk-1")
        self.assertIn("unusable", body)
        self.assertIn("not valid JSON", body)
        self.assertIn("stopped retrying", body)
        self.assertEqual(self.graph.marked, ["AAMk-1"])

        with Store(self.db) as store:
            self.assertTrue(store.mail_processed("<abc@mail.example>"))
            self.assertEqual(store.mail_attempts("<abc@mail.example>"), 0)

    def test_a_further_run_does_nothing_at_all(self):
        for _ in range(cli.MAX_WATCHLIST_ATTEMPTS):
            self.run_check()
        code, out, err = self.run_check()

        self.assertEqual(code, 0)
        self.assertIn("No unread mail", out)
        self.assertEqual(len(self.graph.replies), 1)  # still just the one
        self.assertEqual(self.graph.marked, ["AAMk-1"])
        self.assertEqual(err, "")

    def test_a_message_still_unread_is_never_answered_twice(self):
        # Marking read failed, so Exchange hands the message back next tick.
        for _ in range(cli.MAX_WATCHLIST_ATTEMPTS):
            self.run_check()
        self.graph.unread = ["AAMk-1"]
        self.run_check()
        self.assertEqual(len(self.graph.replies), 1)

    def test_a_fixed_file_is_applied_before_we_give_up(self):
        self.run_check()
        with Store(self.db) as store:
            self.assertEqual(store.mail_attempts("<abc@mail.example>"), 1)

        self.write_watchlist(json.dumps(GOOD_LIST))
        code, _, _ = self.run_check()

        self.assertEqual(code, 0)
        self.assertEqual(self.tracked(), ["Jensen Huang"])
        self.assertIn("Stopped tracking Satya Nadella", self.graph.replies[0][1])
        # Succeeding clears the ledger, so an unrelated later failure starts
        # its own count from one rather than inheriting this one.
        with Store(self.db) as store:
            self.assertEqual(store.mail_attempts("<abc@mail.example>"), 0)

    def test_a_failed_apology_is_still_a_delivery_failure(self):
        self.graph.fail_reply = True
        for _ in range(cli.MAX_WATCHLIST_ATTEMPTS):
            code, _, err = self.run_check()

        self.assertEqual(code, 3)
        self.assertIn("Could not tell you@example.com", err)
        # Recorded first: the decision to stop stands even though the reply
        # did not go out, so the next tick does not start apologising forever.
        with Store(self.db) as store:
            self.assertTrue(store.mail_processed("<abc@mail.example>"))

    def test_each_message_is_counted_separately(self):
        self.graph = FakeGraph(
            graph_message(id="AAMk-1", internetMessageId="<one@mail.example>"),
            graph_message(id="AAMk-2", internetMessageId="<two@mail.example>",
                          receivedDateTime="2026-08-12T10:00:00Z"),
        )
        self.graph.install()
        self.run_check()
        with Store(self.db) as store:
            self.assertEqual(store.mail_attempts("<one@mail.example>"), 1)
            self.assertEqual(store.mail_attempts("<two@mail.example>"), 1)

    def test_dry_run_writes_nothing_and_sends_nothing(self):
        for _ in range(cli.MAX_WATCHLIST_ATTEMPTS + 2):
            code, _, _ = self.run_check("--dry-run")
            self.assertEqual(code, 3)

        self.assertEqual(self.graph.replies, [])
        self.assertEqual(self.graph.marked, [])
        with Store(self.db) as store:
            self.assertEqual(store.mail_attempts("<abc@mail.example>"), 0)
            self.assertFalse(store.mail_processed("<abc@mail.example>"))

    def test_a_missing_file_is_reported_the_same_way(self):
        self.watchlist.unlink()
        for _ in range(cli.MAX_WATCHLIST_ATTEMPTS):
            self.run_check()
        self.assertIn("Watchlist not found", self.graph.replies[0][1])
        # The hint is a second line, and it survives the reply intact.
        self.assertIn("executives.example.json", self.graph.replies[0][1])


class TestRejectedMailIsStillNeverAnswered(CheckMailCase):
    def test_a_forged_sender_gets_no_apology_even_with_a_broken_file(self):
        self.write_watchlist('{"executives": [')
        self.graph = FakeGraph(graph_message(
            **{"from": {"emailAddress": {"address": "attacker@example.net"}}}))
        self.graph.install()

        code, _, err = self.run_check()

        # Closed on the first sight of it, so it never reaches the retry path
        # at all - which is the point: the apology is for people we trust.
        self.assertEqual(code, 0)
        self.assertIn("Ignored mail from attacker@example.net", err)
        self.assertEqual(self.graph.replies, [])
        self.assertEqual(self.graph.marked, ["AAMk-1"])
        with Store(self.db) as store:
            self.assertEqual(store.mail_attempts("<abc@mail.example>"), 0)


class TestAttemptLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "seen.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_attempts_accumulate_across_processes(self):
        with Store(self.db) as store:
            self.assertEqual(store.record_mail_attempt("<a@x>", error="bad"), 1)
            self.assertEqual(store.record_mail_attempt("<a@x>", error="bad"), 2)
        with Store(self.db) as store:  # the next timer tick
            self.assertEqual(store.mail_attempts("<a@x>"), 2)
            self.assertEqual(store.record_mail_attempt("<a@x>", error="bad"), 3)

    def test_only_the_first_recorder_is_told_it_won(self):
        # Two overlapping runs can both read attempts >= the limit; only one
        # may send the apology, and the insert is what decides which.
        with Store(self.db) as store:
            self.assertTrue(
                store.record_mail("<a@x>", sender="you@example.com", outcome="watchlist-error"))
        with Store(self.db) as store:
            self.assertFalse(
                store.record_mail("<a@x>", sender="you@example.com", outcome="watchlist-error"))

    def test_recording_the_message_clears_its_attempts(self):
        with Store(self.db) as store:
            store.record_mail_attempt("<a@x>", error="bad")
            store.record_mail("<a@x>", sender="you@example.com", outcome="changed")
            self.assertEqual(store.mail_attempts("<a@x>"), 0)
            self.assertTrue(store.mail_processed("<a@x>"))

    def test_an_older_database_gains_the_table_in_place(self):
        # A database written before this table existed, with rows in it.
        conn = sqlite3.connect(self.db)
        conn.executescript(
            "CREATE TABLE mail_seen (message_id TEXT PRIMARY KEY, "
            "processed_at TEXT NOT NULL, sender TEXT, outcome TEXT);"
        )
        conn.execute("INSERT INTO mail_seen VALUES ('<old@x>','2026-01-01','a@x','changed')")
        conn.commit()
        conn.close()

        with Store(self.db) as store:
            self.assertTrue(store.mail_processed("<old@x>"))
            self.assertEqual(store.mail_attempts("<old@x>"), 0)
            self.assertEqual(store.record_mail_attempt("<new@x>", error="bad"), 1)


if __name__ == "__main__":
    unittest.main()
