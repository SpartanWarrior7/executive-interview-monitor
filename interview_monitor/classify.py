"""Decide whether a candidate item is genuinely an interview with the executive.

Two gates, both must pass:
  1. The executive is actually named (not just their company).
  2. The item looks like an interview rather than ordinary reportage.

Gate 2 is a weighted keyword score so that a single weak cue ("said") is not
enough on its own, while an explicit cue ("sits down with") is.

The company an executive is listed under is the third input. Gate 1 proves
somebody with that name was interviewed - not that it was *our* person, and
common names collide. So a company mention adds confidence, and under
company_match="require" its absence disqualifies the item outright.
"""

from __future__ import annotations

import re
import unicodedata

from .sources import Item

# (compiled pattern, weight, label)
_RAW_SIGNALS: list[tuple[str, int, str]] = [
    # Explicit - one of these alone clears the default threshold.
    (r"\binterview(s|ed|ing)?\b", 3, "interview"),
    (r"\bsits? down with\b", 3, "sits-down-with"),
    (r"\bsat down with\b", 3, "sat-down-with"),
    (r"\bin conversation with\b", 3, "in-conversation"),
    (r"\bfireside chat\b", 3, "fireside-chat"),
    (r"\bq\s*&\s*a\b", 3, "q-and-a"),
    (r"\bone[- ]on[- ]one with\b", 3, "one-on-one"),
    (r"\bexclusive with\b", 3, "exclusive-with"),
    (r"\bfull (interview|conversation|episode)\b", 3, "full-conversation"),
    (r"\bask(ed|s)? (him|her|them) about\b", 3, "asked-about"),
    # Strong - typically an interview, occasionally a panel or a keynote.
    (r"\bpodcast\b", 2, "podcast"),
    (r"\bepisode\b", 2, "episode"),
    (r"\bspeaks? (with|to)\b", 2, "speaks-with"),
    (r"\bspoke (with|to)\b", 2, "spoke-with"),
    # "in talks to/with" is deal language, not conversation - exclude it.
    (r"(?<!in )\btalks (to|with)\b", 2, "talks-to"),
    (r"\bjoins? (us|the show|the podcast)\b", 2, "joins-show"),
    (r"\bon the record\b", 2, "on-the-record"),
    (r"\bopens up (about|on)\b", 2, "opens-up"),
    (r"\bexplains? (why|how|what)\b", 2, "explains"),
    (r"\bunfiltered\b", 2, "unfiltered"),
    (r"\btranscript\b", 2, "transcript"),
    # Weak - supporting evidence only.
    (r"\btells\b", 1, "tells"),
    (r"\bon why\b", 1, "on-why"),
    (r"\breveals?\b", 1, "reveals"),
    (r"\bdiscusses\b", 1, "discusses"),
    (r"\bweighs in\b", 1, "weighs-in"),
    (r"\bcandid\b", 1, "candid"),
    (r"\bconversation\b", 1, "conversation"),
]

SIGNALS = [(re.compile(p, re.IGNORECASE), w, label) for p, w, label in _RAW_SIGNALS]

# Coverage that mentions an interview cue but is not itself an interview.
_RAW_NEGATIVES: list[tuple[str, int, str]] = [
    (r"\bjob interview\b", -4, "job-interview"),
    (r"\binterview process\b", -3, "hiring-process"),
    (r"\bdeclined to (be interviewed|comment)\b", -3, "declined"),
    (r"\bdid not respond to (a )?request\b", -3, "no-response"),
    (r"\bstock (jumps?|falls?|slides?|rises?|drops?)\b", -2, "market-move"),
    (r"\b(shares|earnings) (report|call|beat|miss)\b", -2, "earnings"),
    (r"\bin talks (to|with)\b", -2, "deal-talks"),
    (r"\bobituary\b", -3, "obituary"),
    (r"\baccording to (a|the) (report|filing)\b", -1, "secondhand"),
]

NEGATIVES = [(re.compile(p, re.IGNORECASE), w, label) for p, w, label in _RAW_NEGATIVES]


def normalize(text: str) -> str:
    """Fold accents and collapse whitespace so name matching is forgiving."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


_NAME_CACHE: dict[str, re.Pattern[str]] = {}


def name_pattern(name: str) -> re.Pattern[str]:
    """The compiled matcher for one name, built once and reused.

    Public because slop.py needs to know *where* a name sits in a title, not
    just whether it is there, and both modules must agree on what counts as a
    mention. Match against `normalize`d text - the pattern has had its own
    accents folded.
    """
    pattern = _NAME_CACHE.get(name)
    if pattern is None:
        parts = [re.escape(p) for p in normalize(name).split() if p]
        # Allow any whitespace/punctuation between name parts (line breaks, nbsp).
        pattern = _NAME_CACHE[name] = re.compile(
            r"\b" + r"[\s\.\-]+".join(parts) + r"\b", re.IGNORECASE
        )
    return pattern


def mentions(text: str, names: list[str]) -> str | None:
    """Return the first name from `names` that appears in `text`, else None."""
    haystack = normalize(text)
    for name in names:
        if name_pattern(name).search(haystack):
            return name
    return None


def classify(item: Item, exec_obj, *, company_match: str | None = None) -> tuple[bool, int, list[str]]:
    """Score an item. Returns (names_the_exec, score, matched signal labels).

    `company_match` overrides the executive's own mode; None uses theirs.
    """
    mode = company_match or getattr(exec_obj, "company_match", "boost")
    companies = list(getattr(exec_obj, "company_names", []) or [])

    matched_name = mentions(item.text, exec_obj.all_names)
    if not matched_name:
        return False, 0, []

    # Same name, different person. Checked before any scoring: no amount of
    # interview language makes a different Michael Brown the right one.
    company_named = mentions(item.text, companies) if companies else None
    if mode == "require" and companies and not company_named:
        return True, 0, ["company-missing"]

    # A cue in the title is worth more than one buried in the summary.
    title = item.title
    body = item.summary
    score = 0
    labels: list[str] = []
    strongest = 0

    # Reaction and commentary videos describe the interview they are reacting
    # to, so their descriptions are full of interview cues. A real video
    # interview says so in its title - for video, that is all we count.
    body_counts = item.media_type != "video"

    for pattern, weight, label in SIGNALS:
        in_title = bool(pattern.search(title))
        in_body = body_counts and bool(pattern.search(body))
        if not (in_title or in_body):
            continue
        score += weight if in_title else max(weight - 1, 1)
        strongest = max(strongest, weight)
        labels.append(label if in_title else f"{label}~")

    # A podcast episode naming the person is almost always the interview
    # itself - but only if it names them in the episode title. Daily news
    # roundups list every executive they mention in the description, and would
    # otherwise re-qualify as an interview every single week.
    if item.media_type == "podcast":
        if not mentions(title, exec_obj.all_names):
            return True, 0, labels + ["podcast-not-titled"]
        score += 2
        strongest = max(strongest, 2)
        labels.append("podcast-source")
    elif item.media_type == "video":
        # Video gets a smaller nudge on purpose: clip channels re-cut other
        # people's interviews, so a video must also carry a real textual cue.
        score += 1
        labels.append("video-source")

    # A pile of weak cues is ordinary reportage quoting an executive, not an
    # interview. Require at least one strong or explicit signal.
    if strongest < 2:
        return True, 0, labels

    for pattern, weight, label in NEGATIVES:
        if pattern.search(item.text):
            score += weight
            labels.append(label)

    # The name appearing in the title (not just the body) is a relevance signal.
    if mentions(title, exec_obj.all_names):
        score += 1
        labels.append("named-in-title")

    # Company confirmation. In the title it is near-proof this is our person;
    # in the body it is corroboration, so it is worth less.
    #
    # Only under "boost". Under "require" the mention was already a gate, and
    # scoring it again would inflate every surviving item equally - which is
    # just min_score two points lower, wearing a disguise.
    if company_named and mode == "boost":
        if mentions(title, companies):
            score += 2
            labels.append("company-in-title")
        else:
            score += 1
            labels.append("company~")
    elif company_named:
        labels.append("company-match")

    return True, score, labels
