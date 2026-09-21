"""Reading commands out of the digest mailbox over Microsoft Graph.

The digest already arrives from a mailbox the user controls, so replying to it
is the least technical way to change who is tracked. This module is the read
half of that loop; `commands.py` interprets what it finds.

    COMMAND_MAILBOX   the mailbox to read, e.g. alerts@example.com.
                      Also the switch: unset means the reply channel is off.
    COMMAND_SENDERS   comma-separated addresses allowed to give commands
                      (defaults to EMAIL_TO)
    COMMAND_FOLDER    which folder to read: a well-known name (inbox, archive)
                      or a folder id. Defaults to inbox. Set it when an Outlook
                      rule files digest replies somewhere else, because mail
                      this never looks at is mail that silently never happens.

Graph credentials are the same three GRAPH_* variables the digest already uses,
but the app needs Mail.ReadWrite as well as Mail.Send: marking a message handled
is a write. Grant it through Exchange Application RBAC scoped to this mailbox,
not as an Entra application permission - see deploy/DEPLOY.md section 9.

Nothing here trusts the mail it reads. A shared mailbox accepts internet mail
and `From:` is trivially forged, so a message is only acted on when it comes
from an allowlisted address *and* Exchange's own Authentication-Results header
says the sender passed DMARC. Exchange stamps that header at the edge and
overwrites anything the sender put there, which is what makes it worth
checking.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field

from . import mailer

# Everything we need to decide what a message is and whether to trust it.
SELECT = "id,subject,from,receivedDateTime,body,internetMessageId,internetMessageHeaders"
TEXT_BODY = {"Prefer": 'outlook.body-content-type="text"'}

DEFAULT_FOLDER = "inbox"

# Ceilings on one run. A shared mailbox accepts internet mail, so it can fill
# with spam faster than anyone clears it; without a bound the paging below
# would turn a 15-minute tick into an unbounded march through the backlog,
# one extra header fetch per message. Anything over the cap stays unread and
# is picked up next tick.
MAX_MESSAGES = 200
MAX_PAGES = 20


def configured() -> bool:
    """Whether the reply-to-change channel is switched on."""
    return bool(os.environ.get("COMMAND_MAILBOX", "").strip())


@dataclass
class InboxConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    mailbox: str
    senders: list[str] = field(default_factory=list)
    folder: str = DEFAULT_FOLDER
    # Everyone the digest goes to. One shared list means one person's edit
    # changes what everybody else receives, so a change is copied to all of
    # them rather than confirmed privately to whoever asked for it.
    notify: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "InboxConfig":
        tenant = os.environ.get("GRAPH_TENANT_ID", "").strip()
        client_id = os.environ.get("GRAPH_CLIENT_ID", "").strip()
        secret = os.environ.get("GRAPH_CLIENT_SECRET", "")
        mailbox = os.environ.get("COMMAND_MAILBOX", "").strip()
        # Not required: the overwhelmingly common case is the inbox, and a
        # blank value is a filled-in-then-emptied env file, not a request to
        # read no folder at all.
        folder = os.environ.get("COMMAND_FOLDER", "").strip() or DEFAULT_FOLDER
        raw_senders = (
            os.environ.get("COMMAND_SENDERS", "").strip()
            or os.environ.get("EMAIL_TO", "").strip()
        )

        mailer._require(
            [
                ("GRAPH_TENANT_ID", tenant),
                ("GRAPH_CLIENT_ID", client_id),
                ("GRAPH_CLIENT_SECRET", secret),
                ("COMMAND_MAILBOX", mailbox),
                ("COMMAND_SENDERS (or EMAIL_TO)", raw_senders),
            ],
            "The email command channel",
        )
        return cls(tenant, client_id, secret, mailbox,
                   mailer._split_recipients(raw_senders), folder,
                   mailer._split_recipients(os.environ.get("EMAIL_TO", "").strip()))

    def others_to_tell(self, sender: str) -> list[str]:
        """Digest recipients who should be copied on a change, minus the person
        who asked for it and the mailbox itself - both already have it."""
        skip = {sender.strip().casefold(), self.mailbox.strip().casefold()}
        seen: set[str] = set()
        out: list[str] = []
        for address in self.notify:
            key = address.strip().casefold()
            if not key or key in skip or key in seen:
                continue
            seen.add(key)
            out.append(address)
        return out

    def graph(self) -> mailer.GraphConfig:
        """A GraphConfig for token fetching. Not built via from_env, which
        insists on EMAIL_TO - irrelevant when reading."""
        return mailer.GraphConfig(
            self.tenant_id, self.client_id, self.client_secret, self.mailbox, []
        )


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #

def _header_map(raw: list | None) -> dict[str, str] | None:
    """Graph's [{name, value}] list as a lowercase-keyed dict.

    None means Graph did not give us the headers at all, which is different
    from a message that genuinely has none - the caller must not read absence
    as permission. Repeated headers are joined, so a check still sees them all.
    """
    if raw is None:
        return None
    out: dict[str, str] = {}
    for header in raw:
        name = str(header.get("name", "")).strip().lower()
        if not name:
            continue
        value = str(header.get("value", "")).strip()
        out[name] = f"{out[name]}\n{value}" if name in out else value
    return out


_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"(?i)</?(br|p|div|tr|li)[^>]*>")


def html_to_text(body: str) -> str:
    """Last-resort fallback for when the Prefer: text header is ignored."""
    text = _BREAK.sub("\n", body)
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    return html.unescape(_TAG.sub("", text))


def _next_page(link: str | None) -> str:
    """An @odata.nextLink turned back into a path for Mailbox._call, or "".

    Graph hands back an absolute URL. Trimming it to a path keeps every
    request on the one client, and refusing anything that is not under the
    Graph root we already talk to means a surprising response body can never
    steer a bearer token somewhere else.
    """
    if not isinstance(link, str) or not link.startswith(f"{mailer.GRAPH_ROOT}/"):
        return ""
    return link[len(mailer.GRAPH_ROOT):]


@dataclass
class Message:
    id: str
    subject: str
    sender: str
    received: str
    body: str
    headers: dict[str, str] | None = None
    internet_message_id: str = ""

    @classmethod
    def from_graph(cls, payload: dict) -> "Message":
        body = payload.get("body") or {}
        content = body.get("content", "") or ""
        if (body.get("contentType", "") or "").lower() == "html":
            content = html_to_text(content)
        sender = (
            ((payload.get("from") or {}).get("emailAddress") or {}).get("address", "")
        )
        return cls(
            id=payload.get("id", ""),
            subject=payload.get("subject", "") or "",
            sender=sender.strip(),
            received=payload.get("receivedDateTime", "") or "",
            body=content,
            headers=_header_map(payload.get("internetMessageHeaders")),
            internet_message_id=payload.get("internetMessageId", "") or "",
        )


class Mailbox:
    """A thin Graph client for one mailbox, holding the token for the run."""

    def __init__(self, cfg: InboxConfig):
        self.cfg = cfg
        self._token = ""

    def _auth(self) -> str:
        if not self._token:
            self._token = mailer.graph_token(self.cfg.graph())
        return self._token

    def _call(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> dict:
        headers = {"Authorization": f"Bearer {self._auth()}", "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})

        status, raw = mailer._request(
            f"{mailer.GRAPH_ROOT}{path}",
            method=method,
            data=data,
            headers=headers,
            timeout=timeout,
        )
        if status not in (200, 201, 202, 204):
            action = f"{method} {path.split('?')[0]}"
            raise mailer.MailError(
                f"Microsoft Graph refused {action}. " + mailer._explain_graph_error(status, raw)
            )
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", "replace"))

    @property
    def _user(self) -> str:
        return urllib.parse.quote(self.cfg.mailbox)

    @property
    def _folder(self) -> str:
        """The folder as one URL segment. Graph takes a well-known name
        (inbox, archive) or a folder id in the same slot, and folder ids carry
        characters that must not survive into the path unescaped."""
        return urllib.parse.quote(self.cfg.folder, safe="")

    def fetch_unread(self, limit: int = 25, max_messages: int = MAX_MESSAGES) -> list[Message]:
        """Unread mail from the command folder, oldest first.

        Graph returns the collection a page at a time and points at the rest
        with @odata.nextLink, so reading only the first page would drop every
        instruction past the twenty-fifth without saying so.

        Sorted here rather than with $orderby: Graph rejects some
        filter+sort combinations on messages as too complex, and there are
        never enough of these to matter. That does mean the order is only
        right once every page is in, so the sort waits for the whole set.
        """
        query = urllib.parse.urlencode(
            {"$filter": "isRead eq false", "$top": str(limit), "$select": SELECT},
            quote_via=urllib.parse.quote,
        )
        path = f"/users/{self._user}/mailFolders/{self._folder}/messages?{query}"

        raw: list[dict] = []
        # MAX_PAGES is belt and braces: a page that comes back empty but still
        # offers a nextLink would otherwise never move len(raw) and never end.
        for _ in range(MAX_PAGES):
            if not path or len(raw) >= max_messages:
                break
            payload = self._call(path, extra_headers=TEXT_BODY)
            raw.extend(payload.get("value") or [])
            path = _next_page(payload.get("@odata.nextLink"))

        messages = [Message.from_graph(m) for m in raw]
        messages.sort(key=lambda m: m.received)
        # Only bites when the cap is not a whole number of pages. Graph hands
        # back the newest first, so what is dropped here is the newest of the
        # window we read - it stays unread and comes round again next tick,
        # while the older mail ahead of it gets handled now. Trimming before
        # the loop below is also what bounds the per-message header fetches.
        del messages[max_messages:]

        # $select does not reliably deliver internetMessageHeaders on a
        # collection. Fetch them per message rather than fail closed on all.
        for message in messages:
            if message.headers is None:
                message.headers = self.fetch_headers(message.id)
        return messages

    def fetch_headers(self, message_id: str) -> dict[str, str] | None:
        payload = self._call(
            f"/users/{self._user}/messages/{urllib.parse.quote(message_id, safe='')}"
            "?$select=internetMessageHeaders"
        )
        return _header_map(payload.get("internetMessageHeaders"))

    def mark_read(self, message_id: str) -> None:
        self._call(
            f"/users/{self._user}/messages/{urllib.parse.quote(message_id, safe='')}",
            method="PATCH",
            payload={"isRead": True},
        )

    def reply(self, message_id: str, text_body: str, html_body: str = "",
              cc: list[str] | None = None) -> None:
        """Reply in-thread. Graph quotes the original for us.

        `cc` copies people who were not on the incoming mail at all. Reply-all
        would not reach them: the command arrived addressed to this mailbox
        alone, so its recipient list is the mailbox, not the digest audience.
        """
        message: dict = {
            "body": {
                "contentType": "HTML" if html_body else "Text",
                "content": html_body or text_body,
            },
            # Graph only accepts custom headers prefixed x-; this one
            # asks well-behaved autoresponders not to answer us.
            "internetMessageHeaders": [
                {"name": "X-Auto-Response-Suppress", "value": "All"}
            ],
        }
        if cc:
            message["ccRecipients"] = [
                {"emailAddress": {"address": address}} for address in cc
            ]
        self._call(
            f"/users/{self._user}/messages/{urllib.parse.quote(message_id, safe='')}/reply",
            method="POST",
            payload={"message": message},
            timeout=60,
        )


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #

OK, SKIP, REJECT = "ok", "skip", "reject"

_AUTO_PRECEDENCE = {"bulk", "list", "auto_reply", "junk"}


@dataclass
class Verdict:
    action: str  # OK = act on it, SKIP = ignore quietly, REJECT = ignore loudly
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.action == OK


def _internal(headers: dict[str, str]) -> bool:
    """Whether Exchange itself vouches that the sender authenticated in-tenant.

    Mail between two mailboxes in the same Exchange organisation never reaches
    the public internet, so it is never SPF/DKIM/DMARC evaluated: it arrives
    stamped `dmarc=none` with no `spf=` at all, which is "not assessed" rather
    than "failed". Requiring DMARC would therefore reject every command sent
    from a colleague's mailbox - the ordinary case - while accepting mail from
    strangers on the internet.

    This header can be trusted because Exchange strips every
    `X-MS-Exchange-Organization-*` header from inbound internet mail before
    stamping its own; anything arriving from outside is marked `Anonymous`.
    It is a stronger assurance than DMARC, not a weaker one - and the
    COMMAND_SENDERS allowlist still has to pass on top of it.
    """
    return headers.get("x-ms-exchange-organization-authas", "").strip().lower() == "internal"


def _authenticated(headers: dict[str, str]) -> bool:
    if _internal(headers):
        return True
    results = headers.get("authentication-results", "").lower()
    if not results:
        return False
    if "dmarc=pass" in results:
        return True
    # Some senders publish no DMARC record; SPF and DKIM both passing is the
    # equivalent assurance that the envelope was not forged.
    return "spf=pass" in results and "dkim=pass" in results


def _is_automated(headers: dict[str, str]) -> str:
    """Why this looks machine-generated, or "" if it does not."""
    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    if auto_submitted.startswith("auto"):
        return f"Auto-Submitted: {auto_submitted}"
    if "x-auto-response-suppress" in headers:
        return "X-Auto-Response-Suppress present"
    precedence = headers.get("precedence", "").strip().lower()
    if precedence in _AUTO_PRECEDENCE:
        return f"Precedence: {precedence}"
    return ""


def authorize(message: Message, cfg: InboxConfig) -> Verdict:
    """Decide whether to act on a message.

    Loop guards come first: an autoresponder bouncing off our own confirmation
    is noise, not an attack, and replying to it would ping-pong forever.
    """
    headers = message.headers
    if headers is None:
        return Verdict(REJECT, "Graph would not return the message headers, so "
                               "authentication could not be checked")

    if message.sender.casefold() == cfg.mailbox.casefold():
        return Verdict(SKIP, "sent by this mailbox itself")
    automated = _is_automated(headers)
    if automated:
        return Verdict(SKIP, f"automated mail ({automated})")

    allowed = {s.casefold() for s in cfg.senders}
    if message.sender.casefold() not in allowed:
        return Verdict(REJECT, f"{message.sender or '(no sender)'} is not in COMMAND_SENDERS")
    if not _authenticated(headers):
        return Verdict(REJECT, f"mail claiming to be from {message.sender} is neither "
                               "internal to this tenant nor passing DMARC")
    return Verdict(OK)
