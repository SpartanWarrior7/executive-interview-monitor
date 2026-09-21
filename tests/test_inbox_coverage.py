"""What the mailbox reader can and cannot see.

Both failures covered here are silent: a reply that lands on the second page
of unread mail, or in a folder an Outlook rule moved it to, produces no error
anywhere - it simply never takes effect. Offline; no network.
Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interview_monitor import inbox, mailer  # noqa: E402


class FakeGraph(unittest.TestCase):
    """Base: mailer._request swapped out, FIFO responses, recorded calls."""

    def setUp(self):
        self.calls = []
        self.responses = []
        self._real = mailer._request

        def fake_request(url, *, method="GET", data=None, headers=None, timeout=30):
            headers = headers or {}
            is_json = "json" in headers.get("Content-Type", "")
            self.calls.append({"url": url, "method": method, "headers": headers,
                               "body": json.loads(data.decode()) if data and is_json else None})
            return self.responses.pop(0)

        mailer._request = fake_request
        # Every run authenticates first; hand out a token once.
        self.responses.append((200, b'{"access_token":"t0ken"}'))

    def tearDown(self):
        mailer._request = self._real

    def _mailbox(self, **kw):
        cfg = inbox.InboxConfig(
            "tenant", "client", "secret", "alerts@example.org",
            ["you@example.com"], **kw,
        )
        return inbox.Mailbox(cfg)

    def _message(self, ident, received="2026-08-12T07:40:00Z"):
        return {
            "id": ident, "subject": "RE: Interview digest",
            "from": {"emailAddress": {"address": "you@example.com"}},
            "receivedDateTime": received,
            "body": {"contentType": "text", "content": "remove Satya Nadella"},
            "internetMessageId": f"<{ident}@example.com>",
            "internetMessageHeaders": [
                {"name": "Authentication-Results", "value": "dmarc=pass"},
            ],
        }

    def _page(self, ids, *, next_link=None, received="2026-08-12T07:40:00Z"):
        payload = {"value": [self._message(i, received) for i in ids]}
        if next_link is not None:
            payload["@odata.nextLink"] = next_link
        self.responses.append((200, json.dumps(payload).encode()))

    @property
    def _fetches(self):
        return [c["url"] for c in self.calls if "/messages?" in c["url"]]


class TestPaging(FakeGraph):
    def test_second_page_is_followed(self):
        self._page(["a"], next_link=f"{mailer.GRAPH_ROOT}/users/mbx/messages?%24skiptoken=X")
        self._page(["b"])
        self.assertEqual([m.id for m in self._mailbox().fetch_unread()], ["a", "b"])
        self.assertEqual(len(self._fetches), 2)
        self.assertIn("skiptoken=X", self._fetches[1])

    def test_paging_keeps_the_prefer_header_and_the_token(self):
        self._page(["a"], next_link=f"{mailer.GRAPH_ROOT}/users/mbx/messages?%24skiptoken=X")
        self._page(["b"])
        self._mailbox().fetch_unread()
        for call in self.calls[1:]:
            self.assertEqual(call["headers"]["Prefer"], 'outlook.body-content-type="text"')
            self.assertEqual(call["headers"]["Authorization"], "Bearer t0ken")

    def test_the_sort_spans_pages(self):
        # Oldest-first has to hold across the whole set, not within each page.
        self._page(["newest"], received="2026-08-12T09:00:00Z",
                   next_link=f"{mailer.GRAPH_ROOT}/users/mbx/messages?p=2")
        self._page(["oldest"], received="2026-08-12T06:00:00Z")
        self.assertEqual([m.id for m in self._mailbox().fetch_unread()],
                         ["oldest", "newest"])

    def test_a_single_page_still_makes_one_call(self):
        self._page(["a"])
        self.assertEqual(len(self._mailbox().fetch_unread()), 1)
        self.assertEqual(len(self._fetches), 1)

    def test_paging_stops_at_the_message_cap(self):
        # Two pages of three, cap of four: the run must not read a third page.
        self._page(["a", "b", "c"],
                   next_link=f"{mailer.GRAPH_ROOT}/users/mbx/messages?p=2")
        self._page(["d", "e", "f"],
                   next_link=f"{mailer.GRAPH_ROOT}/users/mbx/messages?p=3")
        messages = self._mailbox().fetch_unread(max_messages=4)
        self.assertEqual(len(self._fetches), 2)
        # Trimmed to the cap. Nothing over it is marked read elsewhere, so it
        # comes back next tick rather than being lost.
        self.assertEqual([m.id for m in messages], ["a", "b", "c", "d"])

    def test_the_cap_bounds_the_per_message_header_fetches(self):
        # Headers absent from the collection, so each kept message costs a
        # call. The ones trimmed away must not cost anything.
        page = {"value": []}
        for ident in ("a", "b", "c"):
            message = self._message(ident)
            message.pop("internetMessageHeaders")
            page["value"].append(message)
        self.responses.append((200, json.dumps(page).encode()))
        headers = json.dumps({"internetMessageHeaders": [
            {"name": "Authentication-Results", "value": "dmarc=pass"}]}).encode()
        self.responses.extend([(200, headers)] * 2)

        messages = self._mailbox().fetch_unread(max_messages=2)
        self.assertEqual(len(messages), 2)
        # Token + one collection call + exactly two header calls.
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.responses, [])

    def test_empty_pages_that_keep_offering_a_next_link_terminate(self):
        # Nothing here grows the message count, so only the page ceiling ends
        # the loop. Without it the run would never return.
        loop = json.dumps({
            "value": [], "@odata.nextLink": f"{mailer.GRAPH_ROOT}/users/mbx/messages?p=n",
        }).encode()
        self.responses.extend([(200, loop)] * (inbox.MAX_PAGES + 5))
        self.assertEqual(self._mailbox().fetch_unread(), [])
        self.assertEqual(len(self._fetches), inbox.MAX_PAGES)

    def test_a_next_link_off_the_graph_root_is_not_followed(self):
        # A bearer token is attached to every call this makes, so a link out
        # of the response body only gets followed back to where it came from.
        self._page(["a"], next_link="https://evil.example.com/v1.0/users/mbx/messages")
        self.assertEqual([m.id for m in self._mailbox().fetch_unread()], ["a"])
        self.assertEqual(len(self._fetches), 1)

    def test_next_link_variants_that_mean_stop(self):
        for link in (None, "", 42, "https://graph.microsoft.com/v1.0",
                     "https://graph.microsoft.com.evil.test/v1.0/x"):
            self.assertEqual(inbox._next_page(link), "", link)

    def test_a_next_link_becomes_a_path_under_the_graph_root(self):
        self.assertEqual(
            inbox._next_page(f"{mailer.GRAPH_ROOT}/users/mbx/messages?p=2"),
            "/users/mbx/messages?p=2",
        )

    def test_a_page_without_a_value_key_is_not_an_error(self):
        self.responses.append((200, b"{}"))
        self.assertEqual(self._mailbox().fetch_unread(), [])


class TestCommandFolder(FakeGraph):
    ENV = ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
           "COMMAND_MAILBOX", "COMMAND_SENDERS", "COMMAND_FOLDER", "EMAIL_TO")

    def setUp(self):
        super().setUp()
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ.update({
            "GRAPH_TENANT_ID": "tenant", "GRAPH_CLIENT_ID": "client",
            "GRAPH_CLIENT_SECRET": "secret",
            "COMMAND_MAILBOX": "alerts@example.org",
            "COMMAND_SENDERS": "you@example.com",
        })

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        super().tearDown()

    def test_the_default_is_still_the_inbox(self):
        self.assertEqual(inbox.InboxConfig.from_env().folder, "inbox")
        self._page(["a"])
        self._mailbox().fetch_unread()
        self.assertIn("/mailFolders/inbox/messages?", self._fetches[0])

    def test_a_named_folder_is_read_instead(self):
        os.environ["COMMAND_FOLDER"] = "archive"
        cfg = inbox.InboxConfig.from_env()
        self.assertEqual(cfg.folder, "archive")

        self._page(["a"])
        inbox.Mailbox(cfg).fetch_unread()
        self.assertIn("/mailFolders/archive/messages?", self._fetches[0])
        self.assertNotIn("/mailFolders/inbox/", self._fetches[0])

    def test_a_folder_id_is_escaped_into_one_path_segment(self):
        # Graph folder ids are base64-ish and carry = and / and +.
        os.environ["COMMAND_FOLDER"] = "AAMkAD/x+y=="
        self._page(["a"])
        inbox.Mailbox(inbox.InboxConfig.from_env()).fetch_unread()
        url = self._fetches[0]
        self.assertIn("/mailFolders/AAMkAD%2Fx%2By%3D%3D/messages?", url)

    def test_a_blank_or_padded_value_falls_back_to_the_inbox(self):
        for raw in ("", "   ", "\t"):
            os.environ["COMMAND_FOLDER"] = raw
            self.assertEqual(inbox.InboxConfig.from_env().folder, "inbox", repr(raw))
        os.environ["COMMAND_FOLDER"] = "  archive  "
        self.assertEqual(inbox.InboxConfig.from_env().folder, "archive")

    def test_the_folder_does_not_leak_into_the_other_endpoints(self):
        # Reads are folder-scoped; acting on one message is not.
        os.environ["COMMAND_FOLDER"] = "archive"
        mailbox = inbox.Mailbox(inbox.InboxConfig.from_env())
        self.responses.extend([(200, b"{}"), (202, b"")])
        mailbox.mark_read("AAMk-1")
        mailbox.reply("AAMk-1", "done")
        for call in self.calls[1:]:
            self.assertNotIn("mailFolders", call["url"])

    def test_the_folder_is_not_a_required_variable(self):
        # A missing COMMAND_FOLDER must not be reported as misconfiguration.
        os.environ.pop("COMMAND_FOLDER", None)
        self.assertEqual(inbox.InboxConfig.from_env().mailbox,
                         "alerts@example.org")


class TestAuthorizeStillFailsClosed(FakeGraph):
    """Paging must not have widened what gets acted on."""

    def _cfg(self):
        return inbox.InboxConfig("t", "c", "s", "alerts@example.org",
                                 ["you@example.com"])

    def _verdict(self, **kw):
        fields = {"id": "A", "subject": "RE: digest", "sender": "you@example.com",
                  "received": "2026-08-12T07:40:00Z", "body": "list",
                  "headers": {"authentication-results": "dmarc=pass"}}
        fields.update(kw)
        return inbox.authorize(inbox.Message(**fields), self._cfg())

    def test_a_second_page_message_with_no_headers_is_rejected(self):
        self.assertEqual(self._verdict(headers=None).action, inbox.REJECT)

    def test_a_second_page_message_is_held_to_the_same_bar(self):
        self.assertTrue(self._verdict().ok)
        self.assertEqual(self._verdict(sender="stranger@example.com").action, inbox.REJECT)
        self.assertEqual(self._verdict(headers={}).action, inbox.REJECT)


if __name__ == "__main__":
    unittest.main()
