"""Persistent memory of what we have already reported.

Uniqueness is checked two ways so that the same interview syndicated across
outlets (or re-linked with tracking parameters) is only reported once:
  - a normalized URL key
  - a normalized title key, scoped to the executive
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from .sources import Item

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    dedupe_key TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    exec_id    TEXT NOT NULL,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL,
    publisher  TEXT,
    origin     TEXT,
    score      INTEGER,
    published  TEXT,
    first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_exec ON seen(exec_id, first_seen);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    candidates INTEGER DEFAULT 0,
    new_items  INTEGER DEFAULT 0,
    errors     TEXT
);

-- Command emails we have already acted on. Marking a message read in the
-- mailbox is a second network call that can fail after the watchlist has
-- already been written; without this, that crash would re-apply the change on
-- the next tick.
CREATE TABLE IF NOT EXISTS mail_seen (
    message_id   TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    sender       TEXT,
    outcome      TEXT
);

-- Command emails we tried to act on and could not, because the watchlist file
-- itself was unreadable. Retrying is right - somebody may fix the file a
-- minute later - but only for a while: a file still broken several ticks on
-- will not be fixed by another silent retry, and until we stop, the sender is
-- told nothing at all. Deliberately not mail_seen: a message awaiting a retry
-- has not been acted on, and must not read as though it had. The row is
-- dropped when the message reaches mail_seen; one that leaves the inbox before
-- then - deleted, or read by hand - strands its row, which is a few bytes and
-- no consequence at the handful-per-week these arrive at.
CREATE TABLE IF NOT EXISTS mail_attempts (
    message_id TEXT PRIMARY KEY,
    attempts   INTEGER NOT NULL,
    first_at   TEXT NOT NULL,
    last_at    TEXT NOT NULL,
    last_error TEXT
);
"""

_TRACKING_PARAMS = re.compile(
    r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|cmpid|smid|partner|ito|__twitter)", re.I
)
_STOPWORDS = {
    "a", "an", "the", "on", "in", "of", "and", "or", "to", "with", "for",
    "at", "by", "from", "his", "her", "their", "its", "is", "as", "it",
}


def normalize_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url.strip().lower()
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=False)
        if not _TRACKING_PARAMS.match(k)
    ]
    query.sort()
    return urllib.parse.urlunsplit(
        ("", host, path, urllib.parse.urlencode(query), "")
    ).lstrip("/")


def strip_outlet_suffix(title: str) -> str:
    """Drop trailing ' - Outlet' / ' | Outlet' tags that aggregators append.

    The same interview reaches us as "Headline - CNN" from one index and
    "Headline | CNN Business" from another. Only short tails are removed, so a
    genuine subtitle survives.
    """
    for _ in range(2):  # aggregators sometimes stack two tags
        match = re.search(r"\s+[-|–—]\s+([^-|–—]{1,40})$", title)
        if not match:
            break
        tail_words = match.group(1).split()
        head = title[: match.start()].strip()
        if len(tail_words) > 4 or len(head.split()) < 5:
            break
        title = head
    return title


def normalize_title(title: str) -> str:
    text = re.sub(r"[^\w\s]", " ", strip_outlet_suffix(title).lower())
    words = [w for w in text.split() if w not in _STOPWORDS]
    return " ".join(words)


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def dedupe_keys(item: Item) -> list[tuple[str, str]]:
    """(kind, key) pairs identifying this item."""
    keys = [("url", _hash("url", normalize_url(item.url)))]
    if item.original_url and item.original_url != item.url:
        # Next run sees the unresolved redirect link again; key on it too.
        keys.append(("url", _hash("url", normalize_url(item.original_url))))
    title_key = normalize_title(item.title)
    if len(title_key) >= 12:  # too-short titles collide meaninglessly
        keys.append(("title", _hash("title", item.exec_id, title_key)))
    return keys


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def is_new(self, item: Item) -> bool:
        keys = [k for _, k in dedupe_keys(item)]
        placeholders = ",".join("?" * len(keys))
        row = self.conn.execute(
            f"SELECT 1 FROM seen WHERE dedupe_key IN ({placeholders}) LIMIT 1", keys
        ).fetchone()
        return row is None

    def record(self, item: Item) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                key,
                kind,
                item.exec_id,
                item.url,
                item.title,
                item.publisher,
                item.origin,
                item.score,
                item.published.isoformat() if item.published else None,
                now,
            )
            for kind, key in dedupe_keys(item)
        ]
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen "
            "(dedupe_key, kind, exec_id, url, title, publisher, origin, score, published, first_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()

    def mail_processed(self, message_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM mail_seen WHERE message_id = ? LIMIT 1", (message_id,)
        ).fetchone()
        return row is not None

    def record_mail(self, message_id: str, *, sender: str, outcome: str) -> bool:
        """Mark a message finished with. False if it already was.

        The answer is what makes "reply exactly once" true for two runs
        overlapping as well as for one run replaying: whoever wins the insert
        owns the reply, and the loser can see that it lost.
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO mail_seen (message_id, processed_at, sender, outcome) "
            "VALUES (?,?,?,?)",
            (message_id, datetime.now(timezone.utc).isoformat(), sender, outcome),
        )
        first = cur.rowcount == 1
        # The retry ledger only means anything while a message is unresolved.
        self.conn.execute("DELETE FROM mail_attempts WHERE message_id = ?", (message_id,))
        self.conn.commit()
        return first

    def mail_attempts(self, message_id: str) -> int:
        """How many times we have already tried and failed to act on this."""
        row = self.conn.execute(
            "SELECT attempts FROM mail_attempts WHERE message_id = ?", (message_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def record_mail_attempt(self, message_id: str, *, error: str) -> int:
        """Count one failed attempt at a message. Returns the new total.

        The increment is one statement rather than read-then-write, so two
        overlapping runs cannot both read 2 and both write 3. The total read
        back afterwards can still be stale, which is why the decision it feeds
        is guarded again by record_mail rather than trusted on its own.
        """
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO mail_attempts (message_id, attempts, first_at, last_at, last_error) "
            "VALUES (?,1,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            "attempts = attempts + 1, last_at = excluded.last_at, "
            "last_error = excluded.last_error",
            (message_id, now, now, error),
        )
        self.conn.commit()
        return self.mail_attempts(message_id)

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, candidates: int, new_items: int, errors: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, candidates=?, new_items=?, errors=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), candidates, new_items, errors, run_id),
        )
        self.conn.commit()

    def seed_from_existing(self, items: list[Item]) -> int:
        """Mark items as already-seen without reporting them (first-run baseline)."""
        for item in items:
            self.record(item)
        return len(items)

    def stats(self) -> dict:
        # One item can own several url keys (resolved plus original redirect),
        # so count distinct destinations rather than rows.
        seen = self.conn.execute(
            "SELECT COUNT(DISTINCT url) FROM seen WHERE kind='url'"
        ).fetchone()[0]
        runs = self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        return {"interviews_tracked": seen, "runs": runs}
