"""Understanding what someone typed in a reply to the digest.

The audience is a person with an inbox, not a shell, so the parser assumes
mess: quoted digests underneath, phone signatures, bullets, "Please add:",
capitals, typos. Three things can be asked for - add someone, remove someone,
show the list - and anything not understood is reported back rather than
guessed at or silently dropped.

Nobody writes "add Tim Cook". They write "Can you add Tim Cook to the tracker
please", so the instruction has to be dug out of the sentence around it: the
courtesy in front of the verb, and the thing being edited named after the
subject. Taking the tail verbatim is worse than useless for "add" - it wrote a
person called "Tim Cook to tracker", who then sat in the list matching no
headline ever again. Anything still looking like scaffolding after the
stripping is refused rather than written.

Companies are not tracked, only people. "add Nvidia" is answered with who to
name instead, because a one-word entry would match every article about the
company and drown the digest.

Reading the person out of "add Tim Cook, Apple CEO" is the same problem as
reading a shorthand entry out of executives.json, so it is the same parser:
config.parse_person_line.
"""

from __future__ import annotations

import difflib
import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import watchlist
from .config import company_core, parse_person_line

ADD, REMOVE, LIST, HELP = "add", "remove", "list", "help"
BLOCK, UNBLOCK = "block", "unblock"

SYNONYMS = {
    "add": ADD, "track": ADD, "watch": ADD, "follow": ADD, "start": ADD,
    "monitor": ADD, "include": ADD,
    "remove": REMOVE, "delete": REMOVE, "drop": REMOVE, "stop": REMOVE,
    "unfollow": REMOVE, "untrack": REMOVE, "exclude": REMOVE,
    "unsubscribe": REMOVE, "kill": REMOVE,
    "list": LIST, "show": LIST, "status": LIST, "who": LIST,
    "help": HELP, "?": HELP,
    # Muting a show, not a person. Kept apart from REMOVE because the argument
    # is a publisher and the two lists are separate.
    #
    # Not "ban": it is a surname, and a line starting "Ban Ki-moon" would be
    # read as an instruction to block a show called "Ki-moon". The two verbs
    # below already cover what anyone would type.
    "block": BLOCK, "mute": BLOCK, "silence": BLOCK,
    "unblock": UNBLOCK, "unmute": UNBLOCK,
}

# Verbs to match only when typed exactly. Their near neighbours mean the
# opposite ("subscribe" is one edit from "unsubscribe") or nothing at all
# ("skill" from "kill"), and reading a subscribe as an unsubscribe is the
# worst answer available.
#
# block/unblock and mute/unmute are here for exactly that reason: difflib
# scores "unblock" against "block" at 0.83, comfortably over the 0.8 cutoff, so
# left fuzzy an "unblock" would be obeyed as a "block" - the one inversion in
# this whole table that silently throws interviews away. "silence" joins them
# because "science" scores 0.86 against it and is not a request to mute
# anything.
EXACT_ONLY = frozenset({
    "unsubscribe", "kill", "block", "unblock", "mute", "unmute", "silence",
})
FUZZY_VERBS = tuple(word for word in SYNONYMS if word not in EXACT_ONLY)

# Verbs that are only verbs as a phrase. Checked before the table above so
# "no longer track Jensen" is not read as a bare "track".
PHRASES: tuple[tuple[str, str], ...] = (
    ("get rid of", REMOVE),
    ("do not track", REMOVE),
    ("dont track", REMOVE),
    ("don't track", REMOVE),
    ("no longer", REMOVE),
)

# Words allowed to stand between the start of a line and the verb. A closed
# list, not a token count: "sort out my subscription" must still fail to parse
# rather than matching some verb-alike four words in.
LEAD_FILLER = frozenset({
    "please", "pls", "plz", "kindly", "can", "could", "would", "will", "you",
    "i", "we", "just", "also", "and", "then", "hey", "hi", "hello", "ok",
    "okay", "now", "let", "lets", "let's", "us", "want", "wanna", "need", "to",
})

# How far in the verb may be. "Please could you add ..." is four words.
MAX_LEAD = 3

EXAMPLES = ("add Tim Cook, Apple CEO", "remove Jensen Huang",
            "block Business Icons Daily", "list")

# Where a mail client starts quoting what it is replying to. Everything from
# the first of these to the end of the message is somebody else's words.
QUOTE_MARKER = re.compile(
    r"""^\s*(
        >                              # the classic quote prefix
      | On\s.{0,120}\bwrote:           # Gmail, Apple Mail
      | -{2,}\s*Original\s+Message     # Outlook
      | _{5,}                          # Outlook's horizontal rule
      | From:\s                        # forwarded header block
      | Sent\s+from\s+my\s             # phone signature
      | \*?From:\*?\s                  # HTML-ish forwarded header
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Lines that are politeness, not instructions.
NOISE = re.compile(
    r"^\s*(thanks?|thank\s+you|cheers|best|regards|kind\s+regards|hi|hello|"
    r"hey|ta|please|ok|okay)\b[\s,.!]*$",
    re.IGNORECASE,
)

# Leading bullets and numbering: "- ", "* ", "1. ", "1) ".
BULLET = re.compile(r"^\s*(?:[-*•·]+|\d+[.)])\s+")

REPLY_SUBJECT = re.compile(r"^\s*(re|fw|fwd|aw|antw)\s*:", re.IGNORECASE)

# Job titles worth writing in capitals when someone typed them in lower case.
ACRONYMS = {"ceo", "cfo", "coo", "cto", "cio", "cmo", "cro", "cpo", "vp",
            "evp", "svp", "md"}

# The thing being edited, named in passing: "... to the tracker", "... off my
# watchlist". A preposition has to introduce it, which is what keeps a real
# surname safe: "Sam Monitor" is a person, "to the monitor" is the tracker.
TARGET_PHRASE = re.compile(
    r"""[\s,]{1,4}(?:to|from|off|on|onto|in|into|of|out\s+of)\s+
        (?:the\s+|my\s+|our\s+|your\s+)?
        (?:trackers?|tracking(?:\s+lists?)?|watch\s*lists?|lists?|monitors?
          |digests?|reports?)
        \b""",
    re.IGNORECASE | re.VERBOSE,
)

# Politeness trailing the subject: "add Tim Cook to the tracker please".
COURTESY_TAIL = re.compile(
    r"[\s,]{1,4}(?:please|pls|plz|thanks?|thank\s+you|thx|cheers|asap|now|today|"
    r"for\s+(?:me|us)|going\s+forward|from\s+now\s+on)[\s.,!?]*$",
    re.IGNORECASE,
)

# The courtesy words that are never part of a show's name, for the one caller
# that is cleaning a publisher rather than a person.
#
# "now" and "today" are deliberately missing from this list even though
# COURTESY_TAIL carries them: "Tech Today", "Nvidia Now" and "Marketplace Tech"
# are real show names, and a blocked "Tech Today" truncated to "Tech" would
# silence Bloomberg Tech and TechCrunch as well - a substring match makes an
# over-eager strip here much worse than a missed one.
SHOW_COURTESY_TAIL = re.compile(
    r"[\s,]{1,4}(?:please|pls|plz|thanks?|thank\s+you|thx|cheers|"
    r"for\s+(?:me|us)|going\s+forward|from\s+now\s+on)[\s.,!?]*$",
    re.IGNORECASE,
)

# The rest of the courtesy vocabulary, but only where a comma proves it is an
# aside rather than the last word of the title: "block Tech Today, thanks now".
SHOW_COURTESY_AFTER_COMMA = re.compile(
    r",\s*(?:now|today|asap|already)[\s.,!?]*$", re.IGNORECASE
)

# Filler nouns wrapped around the subject: "remove the exec Jensen Huang".
# "guy" and friends need the determiner, because Gal Gadot, Guy Kawasaki and
# Buddy Guy are people and stripping their name is not a recoverable mistake.
LEAD_NOUN = re.compile(
    r"^(?:(?:persons?|people|execs?|executives?)"
    r"|(?:the|this|that|our|my)\s+(?:persons?|people|execs?|executives?|guy|gal|chap))\s+",
    re.IGNORECASE,
)
TRAIL_NOUN = re.compile(
    r"[\s,]{1,4}the\s+(?:person|exec|executive|guy)$", re.IGNORECASE
)
LEAD_ARTICLE = re.compile(r"^the\s+", re.IGNORECASE)

# What is left of the verb when the verb was a phrase: "stop tracking X",
# "no longer follow X".
LEAD_GERUND = re.compile(
    r"^(?:(?:to|for)\s+)?"
    r"(?:tracking|track|following|follow|watching|watch|monitoring|adding|removing)\s+",
    re.IGNORECASE,
)

# Scaffolding that survived the stripping above, with no preposition to give
# it away: "add Tim Cook tracker". An "add" carrying any of this is refused -
# a bad refusal costs one reply, a bad entry costs months of a person who
# never matches anything.
TARGET_RESIDUE = re.compile(
    r"\b(?:tracker|watch\s*list|watchlist|tracking|digest)\b", re.IGNORECASE
)

TARGET_NOUNS = ("tracker", "trackers", "watchlist", "tracking", "digest",
                "list", "monitor", "report")

# A preposition has no business inside a person's name; whatever one is
# carrying is the rest of the sentence.
PREPOSITIONS = frozenset({"to", "from", "off", "on", "onto", "into"})

# Words that describe people instead of naming one.
NOT_NAME_WORDS = frozenset({
    "more", "new", "other", "another", "some", "any", "all", "every", "each",
    "few", "several", "many", "most", "everyone", "everybody", "anyone",
    "anybody", "someone", "somebody", "nobody", "people", "persons", "women",
    "men", "guys", "folks", "execs", "executives", "ceos", "cfos", "names",
    "me", "us", "them", "him", "her", "you", "everything", "anything",
    "something", "stuff", "things",
})

# Legal forms that mark an employer rather than a person. Deliberately shorter
# than the list config.company_core strips: "co", "sa", "ag" and "nv" are all
# surnames as well, and refusing Marta Sa by email leaves no way to say it.
LEGAL_WORDS = frozenset({
    "company", "corp", "corporation", "inc", "incorporated", "ltd", "limited",
    "plc", "llc", "llp", "group", "holdings", "gmbh",
})


@dataclass
class Command:
    verb: str
    argument: str = ""
    source: str = ""


@dataclass
class Outcome:
    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    listing: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    # Muted shows, for the same reason `listing` exists: a reply that changed
    # something has to show the reader the result, or a misread show name goes
    # unnoticed until a digest quietly stops mentioning somebody.
    blocked: list[str] = field(default_factory=list)
    changed: bool = False
    help_requested: bool = False
    listed: bool = False

    @property
    def understood_nothing(self) -> bool:
        return not (self.applied or self.rejected or self.help_requested or self.listed)

    @property
    def needs_help(self) -> bool:
        """Whether to spell the instructions out again. Anyone who got nothing
        they asked for is owed them, not just whoever typed "help"."""
        return self.help_requested or self.understood_nothing or (
            bool(self.rejected) and not self.applied
        )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def strip_quoted(body: str) -> str:
    """Drop the quoted original, so we read only what this person typed."""
    kept: list[str] = []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if QUOTE_MARKER.match(line):
            break
        kept.append(line)
    return "\n".join(kept)


def _clean(line: str) -> str:
    line = BULLET.sub("", line)
    line = " ".join(line.split())
    return line.rstrip(".!;")


def _key(word: str) -> str:
    return word.strip().strip(":,.").lower()


def _verb(word: str) -> str:
    """A verb from one word, tolerating typos."""
    key = _key(word)
    if key in SYNONYMS:
        return SYNONYMS[key]
    if len(key) >= 4:
        close = difflib.get_close_matches(key, FUZZY_VERBS, n=1, cutoff=0.8)
        if close:
            return SYNONYMS[close[0]]
    return ""


def split_verb(words: list[str]) -> tuple[str, list[str]]:
    """(verb, the words after it), looking past the courtesy in front of it.

    "Can you remove Jensen Huang" and "Please could you add Tim Cook" are how
    people write. Only words on LEAD_FILLER are stepped over, so a line that
    happens to contain a verb-alike late on is still reported as not
    understood instead of being acted on.
    """
    keys = [_key(w) for w in words]
    for start in range(min(len(words), MAX_LEAD + 1)):
        for phrase, verb in PHRASES:
            parts = phrase.split()
            if keys[start:start + len(parts)] == parts:
                return verb, words[start + len(parts):]
        # "take Jensen Huang off the list" - it is the "off" that makes this a
        # removal, so "take" on its own stays unrecognised.
        if keys[start] == "take" and "off" in keys[start + 1:]:
            return REMOVE, words[start + 1:]
        verb = _verb(words[start])
        if verb:
            return verb, words[start + 1:]
        if keys[start] not in LEAD_FILLER:
            break
    return "", words


def _cut_target(text: str) -> str:
    """Take out "to the tracker" and, usually, whatever trails it.

    Once the sentence has named the tracker the rest is commentary - "off the
    list - he is everywhere already" - so the default is to cut to the end of
    the line. The exception is a comma, which is how the company arrives:
    "add Tim Cook to the tracker, Apple CEO" keeps the Apple CEO.
    """
    found = TARGET_PHRASE.search(text)
    if not found:
        return text
    head, rest = text[:found.start()], text[found.end():].strip()
    return head + rest if rest.startswith(",") else head


def clean_argument(text: str) -> str:
    """The subject of an instruction with the sentence around it taken off.

    "Tim Cook to the tracker please" -> "Tim Cook". A loop rather than one
    pass, because the pieces stack in any order: the courtesy hides the target
    phrase, which hides the filler noun.
    """
    # Commas are collapsed as well as whitespace. Every pattern below starts
    # with [\s,] and runs to the end, so a line of nothing but commas would
    # cost quadratic time to fail to match.
    text = re.sub(r"(?:\s*,\s*)+", ", ", " ".join(text.split())).strip(" :,")
    previous = ""
    while text and text != previous:
        previous = text
        text = _cut_target(text).strip()
        for pattern in (COURTESY_TAIL, TRAIL_NOUN,
                        LEAD_GERUND, LEAD_NOUN, LEAD_ARTICLE):
            text = pattern.sub("", text).strip()
        text = text.strip(" :,;?!")
    return text


def clean_show_argument(text: str) -> str:
    """The show name out of "block the AI Business Digest, please".

    Deliberately much lighter-handed than clean_argument. That one is tuned for
    people's names and strips lead nouns, gerunds and articles - all of which
    are ordinary words in a podcast title. "Executives Unplugged" would come
    back as "Unplugged", and "The Daily" as "Daily", either of which then
    matches shows nobody asked to mute.

    So only three things come off: surrounding quotes, a courtesy that cannot be
    part of a title, and an explicit "... from my list" phrase. Whatever is left
    is the show's name as the reader wrote it.

    The asymmetry is the point. A show name is matched as a substring, so a word
    stripped in error widens the block to every show that shares the remaining
    prefix, while a courtesy left on simply fails to match anything and gets
    reported back. Over-stripping loses interviews silently; under-stripping
    does not.
    """
    text = " ".join(text.split()).strip(" :,")
    previous = ""
    while text and text != previous:
        previous = text
        text = _cut_target(text).strip()
        text = SHOW_COURTESY_TAIL.sub("", text).strip()
        text = SHOW_COURTESY_AFTER_COMMA.sub("", text).strip()
        # Straight and curly quotes, and the stray leading article people add
        # when quoting a title they are looking at.
        text = text.strip("\"'‘’“”").strip()
        text = text.strip(" :,;?!")
    return text


def _target_word(word: str) -> bool:
    key = word.strip(".,").lower()
    if key in TARGET_NOUNS:
        return True
    # "to trackerr": one slipped key should not be the difference between a
    # refusal and a person called "Tim Cook to trackerr".
    return len(key) >= 5 and bool(
        difflib.get_close_matches(key, TARGET_NOUNS, n=1, cutoff=0.75)
    )


def looks_like_instruction(text: str) -> bool:
    """Whether what is left of a subject still reads as part of the sentence.

    Only consulted on the way in to "add", where being wrong writes a person
    who matches no headline ever again. A surviving preposition is the tell:
    "Tim Cook to trackerr" and "Tim Cook to Apple" are half a sentence rather
    than a name, and the typo does not have to be spelled right to be caught.
    Anything the connector words carry ("Andy Jassy at Amazon") has already
    been split off into the company by then.

    A two-word name ending in one is left alone, because that is a surname:
    Johnnie To.
    """
    if TARGET_RESIDUE.search(text):
        return True
    keys = [w.strip(".,").lower() for w in text.split()]
    if not keys:
        return False
    if all(key in PREPOSITIONS for key in keys):
        return True
    if any(key in PREPOSITIONS for key in keys[:-1]):
        return True
    return len(keys) > 2 and keys[-1] in PREPOSITIONS


def is_not_a_name(text: str) -> bool:
    """Whether this is a description of people rather than one person.

    "we want to include more women" and "add the new Intel CEO" are opinions,
    not instructions, but they parse as one: courtesy, a verb, and a noun
    phrase. Writing the noun phrase down as a person is the junk entry this
    module exists to avoid, so a word that no name contains turns it down.
    """
    return any(w.strip(".,").lower() in NOT_NAME_WORDS for w in text.split())


def looks_like_company(text: str) -> bool:
    """Whether this names an employer rather than a person.

    Only the legal form is evidence. Nothing else here is: plenty of companies
    are two ordinary words, and guessing would turn away real people.
    """
    words = [w.strip(".,").lower() for w in text.split() if w.strip(".,")]
    return len(words) > 1 and (words[-1] in LEGAL_WORDS or words[0] in LEGAL_WORDS)


def company_name(text: str) -> str:
    """`Nvidia company` -> `Nvidia`, for quoting back at the sender."""
    words = text.split()
    if len(words) > 1 and words[0].strip(".,").lower() in LEGAL_WORDS:
        words = words[1:]
    return company_core(" ".join(words)) or text.strip()


def company_guidance(text: str, raw: dict | None = None) -> str:
    """The answer to "add Nvidia": who at Nvidia, not Nvidia.

    Given the current list as well, it names the people already tracked there,
    so "remove Nvidia" turns into an instruction the sender can just send back.
    """
    core = company_name(text)
    message = (
        "I track people, not companies. "
        f"Tell me who at {core} to follow, e.g.\n"
        f"    add Jane Doe, {core} CEO"
    )
    tracked = _tracked_at(raw, core) if raw is not None else []
    if tracked:
        message += (
            f"\nAt {core} I currently track {'; '.join(tracked)}"
            " - name one of them to remove them."
        )
    return message


def _tracked_at(raw: dict, core: str) -> list[str]:
    """Labels of the tracked people whose employer is this company."""
    wanted = core.casefold()
    if not wanted:
        return []
    # describe() is one line per person, in order, so it can be zipped back
    # onto the entries rather than rebuilding the label here.
    entries = raw.get("executives", [])
    return [
        label
        for label, entry in zip(watchlist.describe(raw), entries)
        if (entry.get("company") or "").strip()
        and wanted in {
            (entry.get("company") or "").strip().casefold(),
            company_core(entry.get("company") or "").casefold(),
        }
    ]


def parse_person(text: str) -> tuple[str, str, str]:
    """(name, company, title) from the shapes people actually type.

    The same job as reading a shorthand line out of executives.json, so it is
    the same parser - a reply that says "Tim Cook, Apple CEO" and a config
    entry that says the same thing must not disagree about who that is.
    """
    entry = parse_person_line(text.strip().strip(" ,"))
    title = " ".join(
        word.upper() if word.strip(".,").lower() in ACRONYMS else word
        for word in entry.get("title", "").split()
    )
    return entry.get("name", ""), entry.get("company", ""), title


def parse(subject: str, body: str) -> tuple[list[Command], list[str]]:
    """(commands, lines we could not make sense of).

    The subject line counts as a command only when it is not a reply subject -
    that lets someone send a fresh "add Tim Cook" with an empty body, while
    "RE: Interview digest: 2 new interviews" is correctly ignored.
    """
    candidates: list[str] = []
    if subject and not REPLY_SUBJECT.match(subject):
        candidates.append(subject)
    candidates.extend(strip_quoted(body).split("\n"))

    commands: list[Command] = []
    unparsed: list[str] = []
    signing_off = False
    for raw_line in candidates:
        line = _clean(raw_line)
        if not line:
            continue
        if NOISE.match(line):
            # "Thanks," - what follows it is a signature, not an instruction.
            signing_off = True
            continue
        words = line.split()
        verb, rest = split_verb(words)
        if signing_off and len(words) <= 3 and not verb:
            continue
        if not verb:
            unparsed.append(line)
            continue
        argument = " ".join(rest)
        # A show name is not a person's name and must not be cleaned like one.
        cleaned = (
            clean_show_argument(argument)
            if verb in (BLOCK, UNBLOCK)
            else clean_argument(argument)
        )
        commands.append(Command(verb, cleaned, line))

    return commands, unparsed


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #

def help_text() -> list[str]:
    return [
        "Reply to this email with one instruction per line:",
        *(f"    {example}" for example in EXAMPLES),
        "",
        'For "add", the name is all I need - company and job title are optional '
        "but make the search sharper.",
        "I follow people rather than companies, so name someone at the company "
        "instead of the company itself.",
        '"block" takes the name of a show, not a person - use it on the '
        "machine-generated podcasts at the foot of the digest, and I will stop "
        'showing them to you. "unblock" undoes it.',
    ]


# watchlist.remove may or may not take a company yet. Asking rather than
# assuming keeps this working either way instead of raising TypeError the day
# the signature changes.
_REMOVE_TAKES_COMPANY = "company" in inspect.signature(watchlist.remove).parameters


def _remove_from(raw: dict, target: str, company: str = "") -> tuple[dict, str]:
    if company and _REMOVE_TAKES_COMPANY:
        return watchlist.remove(raw, target, company=company)
    return watchlist.remove(raw, target)


def _add(raw: dict, argument: str) -> tuple[dict, str]:
    """Add the person named in an already-cleaned argument, or explain why not."""
    name, company, title = parse_person(argument)
    if name and looks_like_instruction(name):
        return raw, (
            f'I did not add "{argument}" - that still reads like part of the '
            f"instruction rather than a person's name. Try: {EXAMPLES[0]}"
        )
    if name and is_not_a_name(name):
        return raw, (
            f'I did not add "{argument}" - I need one person by name. '
            f"Try: {EXAMPLES[0]}"
        )
    # A lone word is a company far more often than a person, and it would
    # match every article that mentions the company.
    if name and (looks_like_company(name) or (len(name.split()) == 1 and not company)):
        return raw, company_guidance(name)
    return watchlist.add(raw, name=name, company=company, title=title)


def _remove(raw: dict, argument: str) -> tuple[dict, str]:
    """Remove the person named in an already-cleaned argument, or explain why not."""
    name, company, _ = parse_person(argument)
    # The name, not the whole argument: "Sam Director, CFO, Umbrella
    # Corporation" ends in a legal suffix but still names a person.
    subject = name or argument

    # Try the whole thing first, then the name pulled out of it, so both
    # "remove Michael Brown" and "remove Michael Brown, Initech" land - and a
    # company name inside the list is still matched verbatim. Always tried
    # before deciding this is a company: somebody on the list is removable by
    # name whatever their name looks like.
    attempts = [(argument, "")]
    if name and name != argument:
        # "remove Jensen Huang from trackerr" parses the misspelt tail as the
        # company; passing that on would only narrow the search wrongly.
        if company and (looks_like_instruction(company)
                        or any(_target_word(w) for w in company.split())):
            company = ""
        attempts.append((name, company))
    refusal = ""
    for target, target_company in attempts:
        new, message = _remove_from(raw, target, target_company)
        if new is not raw:
            return new, message
        refusal = refusal or message

    # Nobody of that name. If it is a company - either by its legal form or
    # because we track someone there - answer with who they can name instead.
    if subject and (looks_like_company(subject) or _tracked_at(raw, company_name(subject))):
        return raw, company_guidance(subject, raw)
    return raw, refusal


def apply(
    commands: list[Command],
    unparsed: list[str],
    path: str | Path,
    *,
    dry_run: bool = False,
) -> Outcome:
    """Run every command against one loaded watchlist, saving once at the end."""
    outcome = Outcome(unparsed=list(unparsed))
    raw = watchlist.load_raw(path)

    for command in commands:
        if command.verb == HELP:
            outcome.help_requested = True
            continue
        if command.verb == LIST:
            # No message needed: every reply ends with the list anyway.
            outcome.listed = True
            continue
        if command.verb == ADD:
            new, message = _add(raw, command.argument)
        elif command.verb == BLOCK:
            new, message = watchlist.block(raw, command.argument)
        elif command.verb == UNBLOCK:
            new, message = watchlist.unblock(raw, command.argument)
        else:
            new, message = _remove(raw, command.argument)

        # add/remove hand back the same dict when they refuse, so identity is
        # the answer to both "did it work" and "which list to report".
        if new is not raw:
            outcome.applied.append(message)
            outcome.changed = True
        else:
            outcome.rejected.append(message)
        raw = new

    # Leftover lines are only worth mentioning when something else did work;
    # otherwise the whole message failed and the reply leads with the
    # instructions instead of nitpicking each line.
    if commands and unparsed:
        for line in unparsed[:3]:
            outcome.rejected.append(f"I did not understand \"{line}\" - so I left it alone.")

    if outcome.changed and not dry_run:
        watchlist.save(path, raw)

    outcome.listing = watchlist.describe(raw)
    outcome.blocked = watchlist.blocked_shows(raw)
    return outcome


def handle(subject: str, body: str, path: str | Path, *, dry_run: bool = False) -> Outcome:
    """Parse a message and act on it."""
    commands, unparsed = parse(subject, body)
    return apply(commands, unparsed, path, dry_run=dry_run)
