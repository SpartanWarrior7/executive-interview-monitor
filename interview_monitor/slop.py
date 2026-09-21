"""Decide whether an accepted item is machine-made filler rather than a real interview.

A separate judgement from `classify`, and deliberately so. `classify` answers
"is this an interview with our person"; a generated episode answers yes to that
honestly - it names the person, it says "interview", it is a podcast. What gives
it away is not the absence of interview language but the presence of other
things: a synthetic-voice disclaimer, a scraper's copyright boilerplate, a title
that is *about* the person rather than *with* them, a four-minute runtime.

So this scores those tells on their own and never touches the interview score.
The two numbers travel together and the report decides what to do with them.

Missing an interview is the expensive failure here, so:

  - Nothing in this module drops anything. A high score demotes an item into a
    marked section of the digest; it stays visible, with its reasons attached.
  - An absent field abstains. Unknown duration is not a short duration, and an
    empty publisher is not a suspicious one - the same rule the YouTube length
    filter already follows in sources.py.
  - Rescue signals are real signals, not decoration. A generated episode and a
    genuine one from an oddly-named show look alike on tells alone; what parts
    them is guest framing, runtime, and how hard the interview cues landed.
"""

from __future__ import annotations

import re

from .classify import mentions, name_pattern, normalize
from .config import MIN_SHOW_MATCH_CHARS

# (compiled pattern, weight, label). Weights are calibrated against a default
# threshold of 4: a tier-1 tell clears it alone, tier-2 needs corroboration,
# and no two tier-3 tells should fire together on a real interview.
_RAW_TELLS: list[tuple[str, int, str]] = [
    # Self-disclosure. The show saying it out loud, which is near-proof and
    # worth clearing the threshold on its own.
    (r"\bai[- ]generated\b", 4, "ai-generated"),
    (r"\bgenerated (by|with|using) (ai|artificial intelligence)\b", 4, "ai-generated"),
    (r"\bai[- ](narrated|voiced|written)\b", 4, "ai-narrated"),
    (r"\bai (voice|voices|host|hosts|narrator)\b", 4, "ai-voice"),
    (r"\bsynthetic (voice|voices|audio)\b", 4, "synthetic-audio"),
    (r"\btext[- ]to[- ]speech\b", 4, "text-to-speech"),
    (r"\bnotebooklm\b", 4, "notebooklm"),
    (r"\belevenlabs\b", 4, "elevenlabs"),
    (r"\b(auto|automatically|machine)[- ]generated\b", 4, "auto-generated"),
    (r"\bsimulated (conversation|interview|discussion)\b", 4, "simulated"),
    (r"\bnot an actual (interview|conversation)\b", 4, "not-actual"),
    # Scraper boilerplate. A real outlet has a masthead, and does not need to
    # disclaim its way out of having quoted somebody.
    (r"\bnot affiliated with\b", 3, "not-affiliated"),
    (r"\bfor (informational|entertainment|educational) purposes only\b", 3, "purposes-only"),
    (
        r"\bbased (only |solely |entirely )?on publicly available "
        r"(information|sources|data|records)\b",
        3,
        "publicly-available",
    ),
    (r"\ball (rights|trademarks) (belong|are the property of)\b", 3, "rights-belong-to"),
    (r"\bno copyright infringement (is )?intended\b", 3, "no-infringement"),
    (
        r"\bthis (episode|podcast) (is|was) (created|produced|generated) (by|with|using)\b",
        3,
        "episode-produced-by",
    ),
]

TELLS = [(re.compile(p, re.IGNORECASE), w, label) for p, w, label in _RAW_TELLS]

# Framing that describes the person instead of talking to them. Title-only:
# a genuine interview's description often recaps the guest's life story, and
# reading these out of a body would demote half of Acquired's back catalogue.
_RAW_FRAMING: list[tuple[str, int, str]] = [
    (r"\bthe (story|rise|life|journey|legacy|secrets?) of\b", 2, "story-of"),
    (r"\b(life and career|case study|profile) of\b", 2, "profile-of"),
    (r"\bnet worth\b", 2, "net-worth"),
    (r"\bsuccess story\b", 2, "success-story"),
    (r"\bbiography\b", 2, "biography"),
    (r"\b(leadership |business |life )?lessons (from|of)\b", 2, "lessons-from"),
    (r"\bhow \w+ built\b", 2, "how-x-built"),
    (r"\bdeep dive into\b", 2, "deep-dive"),
    (r"\b(everything|all) you need to know\b", 2, "need-to-know"),
    (r"^\s*who (is|was)\b", 2, "who-is"),
    # Anchored to the end on purpose. classify.py counts "explains why/how" as
    # an interview cue, and "Jensen Huang explains why Blackwell matters" is a
    # real interview - but a title that just trails off in "Explained" is a
    # summary of one.
    (r"\b(explained|recap|summary|breakdown|rundown)\s*$", 2, "summary-suffix"),
]

FRAMING = [(re.compile(p, re.IGNORECASE), w, label) for p, w, label in _RAW_FRAMING]

# Show names. Matched against the publisher only, and kept narrow, because a
# show name is one short string and a careless pattern here silently mutes a
# whole outlet.
#
# Note what is deliberately absent: a bare "AI". Nvidia's own show is called
# "The AI Podcast" and a16z's is "AI + a16z" - both carry exactly the long-form
# executive interviews this monitor exists to find. "AI" in a show name is
# evidence of the beat, not of the machine.
_RAW_SHOW_TELLS: list[tuple[str, int, str]] = [
    (r"\b(auto|ai)[- ](generated|narrated|read|voiced)\b", 3, "show-ai-generated"),
    (r"\bbiograph(y|ies|ical)\b", 2, "show-biography"),
    (r"\bnet worth\b", 2, "show-net-worth"),
    (r"\bsuccess stor(y|ies)\b", 2, "show-success-stories"),
    (r"\bin (\d+|two|three|five|ten|fifteen) minutes\b", 2, "show-in-n-minutes"),
    (r"\bwiki(pedia)?\b", 2, "show-wiki"),
    (r"\bbots?\b", 2, "show-bot"),
    (r"\bgpt\b", 2, "show-gpt"),
    (r"\bsummar(y|ies|ized)\b", 2, "show-summaries"),
    (r"\bquick (bites|takes|reads|facts)\b", 2, "show-quick-bites"),
]

SHOW_TELLS = [(re.compile(p, re.IGNORECASE), w, label) for p, w, label in _RAW_SHOW_TELLS]

# A guest introduced as a guest, written around wherever the name appears. The
# single most reliable sign that somebody actually turned up, so it outweighs
# any one tell on its own.
_GUEST_BEFORE = re.compile(
    r"\b(with|featuring|feat\.?|joined by|guest)\s*$", re.IGNORECASE
)
_GUEST_AFTER = re.compile(
    r"^\s*(joins|returns|sits down|stops by|is (our|my) guest|on the podcast)\b",
    re.IGNORECASE,
)

# Runtimes. A generated episode is cheap to make and short to listen to; a real
# executive interview is the reverse. Both ends abstain when duration is None.
VERY_SHORT_SECONDS = 120
SHORT_SECONDS = 300
SUBSTANTIAL_SECONDS = 1200

# Below this, a podcast description is a placeholder rather than show notes.
THIN_SUMMARY_CHARS = 40

# The "Name: Subtitle" shape only counts when the part before the separator is
# essentially just the name. "Satya Nadella on Microsoft's AI bet - full
# interview" splits into a six-word head and is not this shape at all.
MAX_PROFILE_HEAD_WORDS = 4

# An interview score this high was earned by explicit cues, not by the +2 every
# titled podcast episode gets. The real Bloomberg episode in the live database
# scored 12; the generated shape tops out well below that.
CONFIDENT_SCORE = 8


def _guest_framed(title: str, names: list[str]) -> bool:
    """True when the title introduces one of `names` as a guest.

    Looks at the words either side of the name rather than pasting the name
    into a dozen patterns, so "In conversation with Jensen Huang", "Jensen
    Huang joins us" and "feat. Jen-Hsun Huang" all land without the name's own
    punctuation having to be escaped into each shape.
    """
    # The name patterns have had their accents folded, so the haystack must be
    # folded the same way for the offsets to line up.
    haystack = normalize(title)
    for name in names:
        if not name.strip():
            continue
        for match in name_pattern(name).finditer(haystack):
            if _GUEST_BEFORE.search(haystack[: match.start()]):
                return True
            if _GUEST_AFTER.match(haystack[match.end():]):
                return True
    return False


def _title_shaped_like_a_profile(title: str, names: list[str]) -> bool:
    """True for "Jensen Huang: The Mindset That Built NVIDIA" - name, then topic.

    The canonical generated-episode title. Weak on its own, which is why it is
    worth 1: real shows write "Jensen Huang: Nvidia's Next Act" too. Splits on
    a colon or a *spaced* dash only, so the hyphen in "Jen-Hsun Huang" is not
    mistaken for the separator.
    """
    parts = re.split(r"\s*:\s*|\s+[-–—|]\s+", title, maxsplit=1)
    if len(parts) < 2:
        return False
    head = parts[0].strip()
    if not head or len(head.split()) > MAX_PROFILE_HEAD_WORDS:
        return False
    return mentions(head, names) is not None


def matches_show(publisher: str, patterns: list[str]) -> str:
    """The first entry of `patterns` naming this publisher, or "".

    Case-insensitive substring, the same rule the YouTube channel allowlist
    uses in sources.py, so "Bloomberg" covers "Bloomberg Tech". Entries shorter
    than MIN_SHOW_MATCH_CHARS are ignored rather than allowed to match
    everything.
    """
    haystack = (publisher or "").casefold()
    if not haystack:
        return ""
    for pattern in patterns or []:
        needle = (pattern or "").strip().casefold()
        if len(needle) >= MIN_SHOW_MATCH_CHARS and needle in haystack:
            return pattern
    return ""


def slop_score(item, exec_obj) -> tuple[int, list[str]]:
    """How much this looks like machine-made filler. Returns (score, reasons).

    Never negative: the rescues exist to pull a real interview back under the
    threshold, not to rank the obviously-genuine against each other.
    """
    score = 0
    reasons: list[str] = []
    names = list(getattr(exec_obj, "all_names", None) or [])
    title = item.title or ""

    # Whether anything said what this episode *is*, as opposed to how long or
    # how thin it is. Tracked because the structural tells below cannot tell a
    # generated episode from a short clip by a real outlet. See the gate at the
    # end of this function.
    substantive = False

    for pattern, weight, label in TELLS:
        if pattern.search(item.text):
            score += weight
            reasons.append(label)
            substantive = True

    for pattern, weight, label in FRAMING:
        if pattern.search(title):
            score += weight
            reasons.append(label)
            substantive = True

    for pattern, weight, label in SHOW_TELLS:
        if pattern.search(item.publisher or ""):
            score += weight
            reasons.append(label)
            substantive = True

    duration = item.duration_seconds
    if duration is not None and duration > 0:
        if duration < VERY_SHORT_SECONDS:
            score += 3
            reasons.append("under-2-min")
        elif duration < SHORT_SECONDS:
            score += 2
            reasons.append("under-5-min")

    # Show notes that are missing, or that are just the episode title again.
    # Only for podcasts: news RSS routinely carries no description at all.
    summary = (item.summary or "").strip()
    if item.media_type == "podcast":
        if not summary:
            score += 1
            reasons.append("no-description")
        elif summary.casefold() == title.strip().casefold():
            score += 2
            reasons.append("description-is-title")
        elif len(summary) < THIN_SUMMARY_CHARS:
            score += 1
            reasons.append("thin-description")

    guest_framed = bool(names) and _guest_framed(title, names)

    if names and not guest_framed and _title_shaped_like_a_profile(title, names):
        score += 1
        reasons.append("name-colon-title")

    # ---- rescues ---------------------------------------------------------
    if guest_framed:
        score -= 3
        reasons.append("guest-framing")

    if duration is not None and duration >= SUBSTANTIAL_SECONDS:
        score -= 2
        reasons.append("substantial-runtime")

    if item.score >= CONFIDENT_SCORE:
        score -= 2
        reasons.append("strong-interview-cues")

    # The gate that keeps short real interviews out of the demoted section.
    #
    # Runtime, thin show notes and a colon after the name are circumstantial:
    # "Jensen Huang: Nvidia earnings takeaways", four minutes, one line of notes,
    # from Bloomberg Businessweek, trips three of them and no rescue can fire.
    # That is a real interview, and it is a common shape - news outlets publish
    # short clip episodes constantly.
    #
    # This is the same judgement classify.py makes in the other direction with
    # its `strongest < 2` rule: a pile of weak cues is not evidence. Without at
    # least one tell about what the episode *is*, there is nothing here worth
    # demoting on, whatever the arithmetic came to.
    if not substantive:
        if reasons:
            reasons.append("circumstantial-only")
        return 0, reasons

    return max(score, 0), reasons


def judge(item, exec_obj, *, threshold: int, trusted_shows: list[str] | None = None) -> bool:
    """Score `item` in place and answer whether it should be demoted.

    Must run after the interview score is attached - one of the rescues reads
    it. A threshold of 0 or less switches demotion off while still recording
    the score, so the reasons stay visible in `--format json` for anyone
    tuning the lists.
    """
    item.slop_score, item.slop_reasons = slop_score(item, exec_obj)

    trusted = matches_show(item.publisher, trusted_shows or [])
    if trusted:
        item.slop_reasons.append(f"trusted:{trusted}")
        item.slop = False
        return False

    item.slop = threshold > 0 and item.slop_score >= threshold
    return item.slop
