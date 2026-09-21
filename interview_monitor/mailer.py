"""Sending the digest by email.

Two backends, chosen automatically by which credentials are present:

  Microsoft Graph (preferred - no mailbox password anywhere)
    GRAPH_TENANT_ID       directory (tenant) ID of the Azure app registration
    GRAPH_CLIENT_ID       application (client) ID
    GRAPH_CLIENT_SECRET   client secret value
    EMAIL_FROM            the sending mailbox, e.g. bot@example.com
    EMAIL_TO              recipient(s), comma-separated

  SMTP (fallback)
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD
    SMTP_SECURITY         starttls (default) | ssl | none
    EMAIL_FROM / EMAIL_TO

Set EMAIL_BACKEND=graph|smtp to force one. The default picks Graph when its
three variables are present, else SMTP.

The Graph path uses the client-credentials flow: the app authenticates as
itself, so there is no user password and no interactive sign-in.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
LOGIN_ROOT = "https://login.microsoftonline.com"

# The digest is sent from the same mailbox that reads replies as commands, so
# anything that answers it automatically - an out-of-office, a read receipt, a
# delivery report - comes back as unread mail the command reader has to judge.
# This asks Exchange to hold those back at source rather than filter them after
# the fact. It is a directive about *this* message only; a person replying by
# hand sends a new message and does not carry it forward, which is why putting
# it on outbound mail does not collide with the inbox loop guard that skips
# incoming mail bearing it. Graph only accepts custom headers prefixed x-.
AUTO_RESPONSE_SUPPRESS = ("X-Auto-Response-Suppress", "All")


class MailError(RuntimeError):
    pass


def _split_recipients(raw: str) -> list[str]:
    return [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]


def _require(pairs: list[tuple[str, str]], context: str) -> None:
    missing = [name for name, value in pairs if not value]
    if missing:
        raise MailError(
            f"{context} is not configured. Missing: {', '.join(missing)}. "
            "See deploy/interview-search.env.example."
        )


# --------------------------------------------------------------------------- #
# Microsoft Graph
# --------------------------------------------------------------------------- #

@dataclass
class GraphConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    sender: str
    recipients: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, *, to_override: str = "") -> "GraphConfig":
        tenant = os.environ.get("GRAPH_TENANT_ID", "").strip()
        client_id = os.environ.get("GRAPH_CLIENT_ID", "").strip()
        secret = os.environ.get("GRAPH_CLIENT_SECRET", "")
        sender = os.environ.get("EMAIL_FROM", "").strip()
        raw_to = (to_override or os.environ.get("EMAIL_TO", "")).strip()

        _require(
            [
                ("GRAPH_TENANT_ID", tenant),
                ("GRAPH_CLIENT_ID", client_id),
                ("GRAPH_CLIENT_SECRET", secret),
                ("EMAIL_FROM", sender),
                ("EMAIL_TO", raw_to),
            ],
            "Microsoft Graph email",
        )
        return cls(tenant, client_id, secret, sender, _split_recipients(raw_to))


def _request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> tuple[int, bytes]:
    """One HTTP call. HTTP errors come back as (status, body) so the caller can
    explain them; transport errors raise, because there is nothing to explain."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        raise MailError(f"Could not reach {urllib.parse.urlsplit(url).netloc}: {exc}") from exc


def _post(url: str, *, data: bytes, headers: dict[str, str], timeout: int = 30) -> tuple[int, bytes]:
    return _request(url, method="POST", data=data, headers=headers, timeout=timeout)


def _explain_graph_error(status: int, body: bytes) -> str:
    """Turn Graph's JSON error into something that says what to fix."""
    text = body.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return f"HTTP {status}: {text[:300]}"

    err = payload.get("error")
    if isinstance(err, dict):
        code = err.get("code", "")
        message = err.get("message", "")
    else:  # token endpoint uses a flatter shape
        code = str(err or "")
        message = payload.get("error_description", "")

    hint = ""
    blob = f"{code} {message}"
    if "AADSTS7000215" in blob or code == "invalid_client":
        hint = " -> GRAPH_CLIENT_SECRET is wrong or expired (secrets expire; check the app registration)."
    elif "AADSTS700016" in blob or "unauthorized_client" in blob:
        hint = " -> GRAPH_CLIENT_ID is not an app in this tenant. Check GRAPH_TENANT_ID."
    elif "AADSTS90002" in blob:
        hint = " -> GRAPH_TENANT_ID does not exist."
    # Specific codes before the bare status checks: ErrorSendAsDenied arrives as
    # a 403 and ErrorInvalidUser as a 404, so a status-first chain would bury
    # both under the generic advice.
    elif code == "ErrorSendAsDenied":
        hint = (
            " -> The app may reach this mailbox but not send as it: the "
            "Application Mail.Send role is missing or scoped elsewhere."
        )
    elif code == "MailboxNotEnabledForRESTAPI":
        hint = (
            " -> The mailbox is not reachable over Graph - it has no Exchange "
            "Online licence, or it is on-premises rather than in the tenant."
        )
    elif code == "ErrorItemNotFound":
        hint = " -> That message no longer exists (moved or deleted since we read it)."
    elif code == "ErrorInvalidUser":
        hint = (
            " -> The EMAIL_FROM mailbox does not exist in this tenant, or has "
            "no licence. It must be a real mailbox the app may send as."
        )
    elif code in ("ErrorAccessDenied", "Authorization_RequestDenied") or status == 403:
        hint = (
            " -> No Application RBAC role assignment covers this mailbox. Check "
            "Test-ServicePrincipalAuthorization -Identity <app-id> -Resource "
            "<mailbox>: the role must be assigned AND the mailbox in scope. A "
            "brand-new grant can take up to 2 hours to leave Exchange's cache."
        )
    elif status == 404:
        hint = (
            " -> The EMAIL_FROM mailbox does not exist in this tenant, or has "
            "no licence. It must be a real mailbox the app may send as."
        )

    return f"HTTP {status} {code}: {message[:240]}{hint}"


def graph_token(cfg: GraphConfig) -> str:
    body = urllib.parse.urlencode(
        {
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "scope": GRAPH_SCOPE,
            "grant_type": "client_credentials",
        }
    ).encode()
    status, raw = _post(
        f"{LOGIN_ROOT}/{cfg.tenant_id}/oauth2/v2.0/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status != 200:
        raise MailError("Microsoft Graph auth failed. " + _explain_graph_error(status, raw))

    token = json.loads(raw.decode("utf-8", "replace")).get("access_token", "")
    if not token:
        raise MailError("Microsoft Graph returned no access_token.")
    return token


def build_graph_payload(subject: str, text_body: str, html_body: str, cfg: GraphConfig) -> dict:
    return {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML" if html_body else "Text",
                "content": html_body or text_body,
            },
            "toRecipients": [{"emailAddress": {"address": a}} for a in cfg.recipients],
            "from": {"emailAddress": {"address": cfg.sender}},
            "internetMessageHeaders": [
                {"name": AUTO_RESPONSE_SUPPRESS[0], "value": AUTO_RESPONSE_SUPPRESS[1]}
            ],
        },
        # The mailbox is a sender, not an archive; skip the Sent Items copy.
        "saveToSentItems": False,
    }


def send_via_graph(subject: str, text_body: str, html_body: str, cfg: GraphConfig) -> list[str]:
    token = graph_token(cfg)
    payload = json.dumps(build_graph_payload(subject, text_body, html_body, cfg)).encode("utf-8")
    status, raw = _post(
        f"{GRAPH_ROOT}/users/{urllib.parse.quote(cfg.sender)}/sendMail",
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=60,
    )
    # sendMail answers 202 Accepted with an empty body.
    if status not in (200, 202):
        raise MailError("Microsoft Graph refused the message. " + _explain_graph_error(status, raw))
    return cfg.recipients


# --------------------------------------------------------------------------- #
# SMTP
# --------------------------------------------------------------------------- #

@dataclass
class MailConfig:
    host: str
    port: int
    user: str
    password: str
    security: str
    sender: str
    recipients: list[str]

    @classmethod
    def from_env(cls, *, to_override: str = "") -> "MailConfig":
        host = os.environ.get("SMTP_HOST", "").strip()
        user = os.environ.get("SMTP_USER", "").strip()
        password = os.environ.get("SMTP_PASSWORD", "")
        security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
        sender = os.environ.get("EMAIL_FROM", "").strip() or user
        raw_to = (to_override or os.environ.get("EMAIL_TO", "")).strip()

        default_port = "465" if security == "ssl" else "587"
        try:
            port = int(os.environ.get("SMTP_PORT", default_port))
        except ValueError as exc:
            raise MailError("SMTP_PORT is not a number") from exc

        _require(
            [("SMTP_HOST", host), ("EMAIL_TO", raw_to), ("EMAIL_FROM (or SMTP_USER)", sender)],
            "SMTP email",
        )
        if security not in ("starttls", "ssl", "none"):
            raise MailError(f"SMTP_SECURITY must be starttls, ssl or none (got {security!r})")

        return cls(host, port, user, password, security, sender, _split_recipients(raw_to))


def build_message(subject: str, text_body: str, html_body: str, cfg: MailConfig) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.sender.split("@")[-1] or None)
    msg["X-Entity-Ref-ID"] = "interview-digest"
    msg[AUTO_RESPONSE_SUPPRESS[0]] = AUTO_RESPONSE_SUPPRESS[1]
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def send_via_smtp(subject: str, text_body: str, html_body: str, cfg: MailConfig) -> list[str]:
    msg = build_message(subject, text_body, html_body, cfg)
    context = ssl.create_default_context()
    try:
        if cfg.security == "ssl":
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=context) as server:
                if cfg.user:
                    server.login(cfg.user, cfg.password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as server:
                server.ehlo()
                if cfg.security == "starttls":
                    server.starttls(context=context)
                    server.ehlo()
                if cfg.user:
                    server.login(cfg.user, cfg.password)
                server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(
            "SMTP rejected the credentials. For Gmail this must be an App "
            f"Password, not your account password. ({exc.smtp_code})"
        ) from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailError(f"Could not send mail via {cfg.host}:{cfg.port} - {exc}") from exc
    return cfg.recipients


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

def choose_backend() -> str:
    """Which backend to use, from EMAIL_BACKEND or whatever is configured."""
    explicit = os.environ.get("EMAIL_BACKEND", "").strip().lower()
    if explicit:
        if explicit not in ("graph", "smtp"):
            raise MailError(f"EMAIL_BACKEND must be graph or smtp (got {explicit!r})")
        return explicit

    graph_ready = all(
        os.environ.get(k, "").strip()
        for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET")
    )
    if graph_ready:
        return "graph"
    if os.environ.get("SMTP_HOST", "").strip():
        return "smtp"
    raise MailError(
        "Email is not configured. Set GRAPH_TENANT_ID, GRAPH_CLIENT_ID and "
        "GRAPH_CLIENT_SECRET for Microsoft Graph, or SMTP_HOST for SMTP. "
        "See deploy/interview-search.env.example."
    )


def send(subject: str, text_body: str, html_body: str = "", *, to_override: str = "") -> list[str]:
    """Send the digest via whichever backend is configured. Returns recipients."""
    if choose_backend() == "graph":
        return send_via_graph(subject, text_body, html_body,
                              GraphConfig.from_env(to_override=to_override))
    return send_via_smtp(subject, text_body, html_body,
                         MailConfig.from_env(to_override=to_override))
