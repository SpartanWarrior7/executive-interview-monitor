"""Loading and validating the tracked-executive list."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


COMPANY_MATCH_MODES = ("off", "boost", "require")

# Blocked and trusted show names are matched as case-insensitive substrings of a
# publisher, so a very short entry would match almost everything. The floor is
# enforced here at load time and again wherever the match is made.
MIN_SHOW_MATCH_CHARS = 3

# Legal-form noise. "Nvidia Corporation" in the config should still match an
# article that just says "Nvidia".
_COMPANY_SUFFIXES = re.compile(
    r"[\s,]+(?:inc|inc\.|incorporated|corp|corp\.|corporation|co|co\.|company|"
    r"ltd|ltd\.|limited|llc|l\.l\.c\.|plc|lp|llp|group|holdings?|"
    r"technologies|technology|labs?|ag|sa|s\.a\.|nv|n\.v\.|gmbh|pty|oyj|ab)$",
    re.IGNORECASE,
)

# Used to tell "Jane Doe, CEO" from "Jane Doe, Acme" in shorthand entries.
_TITLE_WORDS = re.compile(
    r"^(?:c[eft]o|coo|cmo|cio|ciso|cro|chro|chief\b|"
    r"co[- ]?founder|founder|chair(?:man|woman|person)?|president|"
    r"vice[- ]president|vp|svp|evp|managing director|md|partner|"
    r"general (?:partner|counsel|manager)|head of\b|director|owner)",
    re.IGNORECASE,
)


# A connector word standing where punctuation would otherwise be, as in
# "Andy Jassy at Amazon" or "chief executive of AMD". Only consulted when the
# separators above found no company, so "Bank of America" survives intact.
_CONNECTOR = re.compile(r"\s+(?:at|of|from|with)\s+", re.IGNORECASE)


def company_core(company: str) -> str:
    """`Nvidia Corporation` -> `Nvidia`. Strips one trailing legal suffix."""
    return _COMPANY_SUFFIXES.sub("", company.strip()).strip(" ,.&-") or company.strip()


def looks_like_title(text: str) -> bool:
    return bool(_TITLE_WORDS.match(text.strip()))


def parse_person_line(text: str) -> dict:
    """Read a person written as one string into name / title / company parts.

    Lets an executive list be pasted in the shape people actually write it,
    so the company travels with the name instead of being lost:

        Jensen Huang
        Jensen Huang (Nvidia)
        Jensen Huang (CEO, Nvidia)
        Jensen Huang, Nvidia
        Jensen Huang, CEO, Nvidia
        Jensen Huang - Nvidia          (also | and en/em dashes)
        Andy Jassy at Amazon           (also of, from, with)
        Tim Cook, Apple CEO            (title trailing the company)
        Jensen Huang (CEO) at Nvidia   (the brackets need not end the line)
    """
    entry: dict = {}
    # What a bracketed aside said, when it is only a fallback. See below.
    aside: dict = {}
    rest = text.strip()

    # A bracketed aside: "(Nvidia)" or "(CEO, Nvidia)". Deliberately not
    # anchored to the end of the line, because people write words after it
    # too; anchoring meant "Tim Cook (Apple CEO) to the tracker" was read as
    # one long name. Kept to a single line, and the leading group stays greedy,
    # so that as before the last bracketed group on the line is the one read.
    paren = re.match(r"^(.*)[\(\[]([^\)\]]*)[\)\]][^\S\n]*(.*)$", rest)
    if paren:
        rest = paren.group(1).strip()
        trailing = paren.group(3).strip()
        # A full stop closing the sentence is not part of anyone's name. Only
        # dropped when that is all that follows, so the one in "Bo Yang Jr."
        # survives.
        if not trailing.strip(" .,;:!?"):
            trailing = ""
        if trailing:
            # Whatever followed the brackets is still part of the line, so it
            # rejoins the name candidate and the branches below get their
            # usual crack at it. Punctuation that carries the sentence on
            # keeps its place: "Jensen Huang (CEO), Nvidia", not " , Nvidia".
            joiner = "" if trailing[0] in ",;:" else " "
            rest = f"{rest}{joiner}{trailing}".strip()
        # Brackets that end the line are the best evidence there is. Brackets
        # with words after them are weaker than a separator, because the aside
        # is often not the employer at all: "Jane Doe (nee Smith), Acme" works
        # for Acme. So that reading is held back and only filled in below for
        # whatever the separators did not turn up.
        found = aside if trailing else entry
        inner = [p.strip() for p in paren.group(2).split(",") if p.strip()]
        if len(inner) >= 2:
            found["title"], found["company"] = inner[0], inner[-1]
        elif inner:
            key = "title" if looks_like_title(inner[0]) else "company"
            found[key] = inner[0]

    # Otherwise a separator: pipe, spaced dash, or comma.
    if "company" not in entry:
        parts = [p.strip() for p in re.split(r"\s*[|]\s*|\s+[-–—]\s+", rest) if p.strip()]
        if len(parts) == 1:
            parts = [p.strip() for p in rest.split(",") if p.strip()]
        if len(parts) >= 3:
            rest = parts[0]
            # "Sam Director, CFO, Umbrella" and "Tim Cook, Apple, CEO" are both
            # things people write, so read which tail is the job rather than
            # trusting the order.
            title, company = parts[1], parts[-1]
            if looks_like_title(company) and not looks_like_title(title):
                title, company = company, title
            entry.setdefault("title", title)
            entry["company"] = company
        elif len(parts) == 2:
            rest = parts[0]
            key = "title" if looks_like_title(parts[1]) else "company"
            entry.setdefault(key, parts[1])

    # Still nothing? A connector word may be carrying the company. Prefer
    # splitting the title, so "Lisa Su, chief executive of AMD" keeps the job
    # and the employer apart rather than calling the whole tail a title.
    if "company" not in entry:
        for key in ("title", None):
            if key is None and aside.get("company"):
                # Splitting the name on a connector is the weakest reading of
                # all, weaker than brackets: in "Jensen Huang (Nvidia) from
                # the tracker", "from" is not introducing an employer.
                break
            source = entry.get("title", "") if key else rest
            parts = _CONNECTOR.split(source, maxsplit=1) if source else []
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                entry["company"] = parts[1].strip()
                if key:
                    entry[key] = parts[0].strip()
                else:
                    rest = parts[0]
                break

    # Whatever the rest of the line did not turn up, the brackets still know.
    for key, value in aside.items():
        entry.setdefault(key, value)

    # "Apple CEO" arrives as one segment; the job title is the tail of it.
    if entry.get("company") and "title" not in entry:
        words = entry["company"].split()
        if len(words) > 1 and looks_like_title(words[-1]):
            entry["title"] = words[-1]
            entry["company"] = " ".join(words[:-1])

    # Punctuation stranded by a mid-line bracket is not part of anyone's name.
    entry["name"] = rest.strip().strip(" ,;:")
    return entry


@dataclass
class Executive:
    id: str
    name: str
    company: str = ""
    title: str = ""
    aliases: list[str] = field(default_factory=list)
    extra_terms: list[str] = field(default_factory=list)
    # Other ways the company is written: tickers, former names, abbreviations.
    company_aliases: list[str] = field(default_factory=list)
    # off = ignore the company | boost = reward a mention | require = drop
    # items that never mention it. Inherited from settings when loaded
    # from a config file; `require` is the answer for common names.
    company_match: str = "boost"

    @property
    def all_names(self) -> list[str]:
        """Every spelling we will accept as a mention of this person."""
        return [self.name, *self.aliases]

    @property
    def company_names(self) -> list[str]:
        """Every spelling we will accept as a mention of the company."""
        if not self.company:
            return list(self.company_aliases)
        names = [self.company, *self.company_aliases]
        core = company_core(self.company)
        if core.lower() not in {n.lower() for n in names}:
            names.append(core)
        return names

    @property
    def label(self) -> str:
        bits = [self.name]
        if self.title and self.company:
            bits.append(f"({self.title}, {self.company})")
        elif self.company:
            bits.append(f"({self.company})")
        return " ".join(bits)


@dataclass
class Settings:
    lookback_days: int = 7
    min_score: int = 3
    max_results_per_source: int = 25
    min_video_minutes: int = 10  # shorter YouTube uploads are clips, not interviews
    # Default company_match for every executive that does not set its own.
    company_match: str = "boost"
    # When non-empty, only these YouTube channels are trusted. This is the only
    # reliable defence against the re-upload economy, where random channels
    # repost other outlets' interviews under their own titles.
    youtube_channels: list[str] = field(default_factory=list)
    # At or above this slop score, an item is moved into the "probably not real"
    # section of the digest - never dropped. 0 switches demotion off and leaves
    # the scores visible in --format json for tuning. See slop.py.
    slop_threshold: int = 4
    # Shows the reader has personally muted, by replying "block <show>". The one
    # place in the pipeline that discards an interview outright, because it is
    # the only one where a person asked for it rather than a heuristic guessed.
    blocked_shows: list[str] = field(default_factory=list)
    # Shows exempt from demotion however they score. For a real outlet whose
    # name happens to trip a tell.
    trusted_shows: list[str] = field(default_factory=list)


@dataclass
class Config:
    executives: list[Executive]
    settings: Settings


def _company_match(value, context: str) -> str:
    mode = str(value or "boost").strip().lower()
    if mode not in COMPANY_MATCH_MODES:
        raise ValueError(
            f"company_match for {context} must be one of "
            f"{', '.join(COMPANY_MATCH_MODES)} (got {value!r})"
        )
    return mode


def _show_list(value, context: str) -> list[str]:
    """Clean a blocked/trusted show list, refusing entries too short to be safe.

    These are matched as case-insensitive substrings of a publisher name, so a
    one- or two-character entry would silently swallow most of the digest. A
    typo that wide is worth stopping on rather than obeying - the whole point
    of this feature is that nothing goes missing quietly.
    """
    if value is None:
        return []
    if isinstance(value, str):  # a bare string is one show, not a list of letters
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"settings.{context} must be a list of show names (got {value!r})")

    shows: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError(f"settings.{context} entries must be show names (got {entry!r})")
        name = entry.strip()
        if not name:
            continue
        if len(name) < MIN_SHOW_MATCH_CHARS:
            raise ValueError(
                f"settings.{context} entry {name!r} is too short - it would match almost "
                f"every show. Use at least {MIN_SHOW_MATCH_CHARS} characters."
            )
        if name.casefold() not in {s.casefold() for s in shows}:
            shows.append(name)
    return shows


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found: {path}\n"
            "Create it from executives.example.json and list the executives you track."
        )

    # utf-8-sig, not utf-8: Notepad and PowerShell write a BOM, and plain
    # utf-8 decoding chokes on it.
    raw = json.loads(path.read_text(encoding="utf-8-sig"))

    # Accept either {"executives": [...]} or a bare [...] list.
    if isinstance(raw, list):
        raw_execs, raw_settings = raw, {}
    else:
        raw_execs = raw.get("executives", [])
        raw_settings = raw.get("settings", {}) or {}

    if not raw_execs:
        raise ValueError(f"No executives listed in {path}")

    default_match = _company_match(raw_settings.get("company_match", "boost"), "settings")

    executives: list[Executive] = []
    seen_ids: set[str] = set()
    for entry in raw_execs:
        # A bare string may carry the company with it: "Jensen Huang (Nvidia)".
        if isinstance(entry, str):
            entry = parse_person_line(entry)
        name = (entry.get("name") or "").strip()
        if not name:
            raise ValueError(f"Executive entry is missing a 'name': {entry!r}")
        exec_id = (entry.get("id") or slugify(name)).strip()
        if exec_id in seen_ids:
            raise ValueError(f"Duplicate executive id: {exec_id!r}")
        seen_ids.add(exec_id)
        # An unset per-person mode inherits the settings default, so every
        # later stage can read exec_obj.company_match on its own.
        raw_mode = entry.get("company_match")
        mode = _company_match(raw_mode, name) if raw_mode else default_match
        has_company = bool((entry.get("company") or "").strip() or entry.get("company_aliases"))
        if mode == "require" and not has_company:
            if raw_mode:
                # Asked for by name: a silent empty digest is worse than a stop.
                raise ValueError(
                    f"{name}: company_match is 'require' but no company is set. "
                    "Add a 'company', or drop the company_match override."
                )
            # A settings-wide 'require' cannot apply to someone with no
            # company; matching nothing would filter everything out.
            mode = "off"
        executives.append(
            Executive(
                id=exec_id,
                name=name,
                company=(entry.get("company") or "").strip(),
                title=(entry.get("title") or "").strip(),
                aliases=[a.strip() for a in entry.get("aliases", []) if a.strip()],
                extra_terms=[t.strip() for t in entry.get("extra_terms", []) if t.strip()],
                company_aliases=[
                    c.strip() for c in entry.get("company_aliases", []) if c.strip()
                ],
                company_match=mode,
            )
        )

    settings = Settings(
        lookback_days=int(raw_settings.get("lookback_days", 7)),
        min_score=int(raw_settings.get("min_score", 3)),
        max_results_per_source=int(raw_settings.get("max_results_per_source", 25)),
        min_video_minutes=int(raw_settings.get("min_video_minutes", 10)),
        youtube_channels=[c.strip() for c in raw_settings.get("youtube_channels", []) if c.strip()],
        company_match=default_match,
        slop_threshold=int(raw_settings.get("slop_threshold", 4)),
        blocked_shows=_show_list(raw_settings.get("blocked_shows"), "blocked_shows"),
        trusted_shows=_show_list(raw_settings.get("trusted_shows"), "trusted_shows"),
    )
    return Config(executives=executives, settings=settings)
