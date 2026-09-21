"""Reading and writing the tracked-executive list.

The list used to be hand-edited only. Now the email command handler edits it
too, so writes have to survive a crash mid-write and must refuse to leave an
invalid file behind - a truncated watchlist would silently stop the digest.

Everything here works on the raw JSON dict rather than the `Executive`
dataclasses, so `_comment`, `settings` and any future keys survive a
round-trip untouched. Two shapes are normalised on the way out: a bare
`[...]` list becomes `{"executives": [...]}`, and a bare string entry becomes
`{"name": "..."}`. Both are semantically identical to what was read.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .config import (
    MIN_SHOW_MATCH_CHARS,
    company_core,
    load_config,
    parse_person_line,
    slugify,
)

ROOT = Path(__file__).resolve().parent.parent
FILENAME = "executives.json"


class WatchlistError(RuntimeError):
    pass


def resolve_path(explicit: str | Path | None = None) -> Path:
    """Where the watchlist lives.

    --config wins, then $WATCHLIST_PATH, then state/executives.json if it
    exists, then the repo copy. The state/ copy is what the mail handler
    writes on the server: the repo copy is tracked in git and would collide
    with every future `git pull`. A plain dev checkout has no state/ copy, so
    it keeps using executives.json exactly as before.
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("WATCHLIST_PATH", "").strip()
    if env:
        return Path(env)
    state_copy = ROOT / "state" / FILENAME
    if state_copy.exists():
        return state_copy
    return ROOT / FILENAME


def load_raw(path: str | Path) -> dict:
    """The file as a dict with a normalised `executives` list of dicts."""
    path = Path(path)
    if not path.exists():
        raise WatchlistError(
            f"Watchlist not found: {path}\n"
            "Create it from executives.example.json."
        )
    # utf-8-sig for the same reason as config.load_config: Notepad writes a BOM.
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise WatchlistError(f"{path} is not valid JSON: {exc}") from exc

    if isinstance(raw, list):
        raw = {"executives": raw}
    elif not isinstance(raw, dict):
        raise WatchlistError(f"{path} should contain a JSON object or list")

    # A bare string carries its own company - "Pat Chairperson (Soylent)" - so
    # it has to be read the way load_config reads it. Storing {"name": <the
    # whole string>} would leave a person whose name never matches a headline.
    entries = raw.get("executives") or []
    raw["executives"] = [
        parse_person_line(e) if isinstance(e, str) else dict(e) for e in entries
    ]
    return raw


def _text(value: object) -> str:
    """One trimmed string out of a hand-edited field.

    Anything that is not a string - null, a number, a nested object - counts
    as absent instead of raising. These files are edited by hand, and one
    malformed entry losing every other instruction in the same email is a
    worse outcome than quietly ignoring the field.
    """
    return value.strip() if isinstance(value, str) else ""


def _texts(value: object) -> list[str]:
    """The strings out of a hand-edited list field.

    Also accepts a bare string where a list belongs, which is the other thing
    people type. Iterating that string instead would read it one letter at a
    time, and a one-letter alias matches far too much - "X" is a real company
    name.
    """
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _entry_id(entry: dict) -> str:
    return _text(entry.get("id")) or slugify(_text(entry.get("name")))


def _label(entry: dict) -> str:
    name = _text(entry.get("name"))
    title, company = _text(entry.get("title")), _text(entry.get("company"))
    if title and company:
        return f"{name} ({title}, {company})"
    if company:
        return f"{name} ({company})"
    return name


def describe(raw: dict) -> list[str]:
    """One human-readable line per tracked person, for the email reply."""
    return [_label(e) for e in raw.get("executives", [])]


def _matches(entry: dict, target: str) -> bool:
    wanted = target.strip().casefold()
    if not wanted:
        return False
    slug = slugify(target)
    name = _text(entry.get("name"))
    candidates = {_entry_id(entry).casefold(), slugify(name), name.casefold()}
    candidates.update(a.casefold() for a in _texts(entry.get("aliases")))
    return wanted in candidates or (bool(slug) and slug in candidates)


def _employer_matches(entry: dict, company: str) -> bool:
    """Whether `company` names this person's employer.

    Both sides are reduced with config.company_core, so the file saying
    "Nvidia Corporation" and the email saying "Nvidia" are the same employer.
    company_aliases are honoured too - that is where tickers and former names
    live, and someone typing "Facebook" for a Meta entry deserves to be
    understood.
    """
    wanted = {company.strip().casefold(), company_core(company).casefold()}
    wanted.discard("")
    if not wanted:
        return False
    written = [_text(entry.get("company")), *_texts(entry.get("company_aliases"))]
    known: set[str] = set()
    for name in written:
        if name:
            known.update({name.casefold(), company_core(name).casefold()})
    return bool(wanted & known)


def add(raw: dict, *, name: str, company: str = "", title: str = "") -> tuple[dict, str]:
    """Add a person. Returns (new_raw, message). The dict is unchanged when
    the message explains a refusal."""
    name = " ".join(name.split())
    if not name:
        return raw, "I could not tell who to add - no name in that line."
    if len(name.split()) < 2:
        return raw, (
            f"I need a full name, not just \"{name}\" - a single word matches "
            "far too much. Try: add Tim Cook, Apple CEO"
        )

    for entry in raw.get("executives", []):
        if _matches(entry, name):
            return raw, f"Already tracking {_label(entry)} - nothing to do."

    exec_id = slugify(name)
    if any(_entry_id(e) == exec_id for e in raw.get("executives", [])):
        return raw, f"An entry with the id {exec_id!r} already exists."

    entry = {"id": exec_id, "name": name}
    if company:
        entry["company"] = company
    if title:
        entry["title"] = title

    new = dict(raw)
    new["executives"] = [*raw.get("executives", []), entry]
    return new, f"Now tracking {_label(entry)}."


def remove(raw: dict, target: str, company: str | None = "") -> tuple[dict, str]:
    """Remove a person by name, alias or id. Returns (new_raw, message).

    `company` is optional and only breaks ties: two tracked people called
    Michael Brown could otherwise be removed by id alone, which is not
    something anyone knows off the top of their head. It deliberately does
    not filter a single unambiguous match - config entries often carry no
    company at all, and refusing "remove Tim Cook, Apple" because the entry
    for Tim Cook says nothing about Apple would break the ordinary case to
    guard against a rare one.
    """
    target = " ".join(target.split())
    # `or ""`: the caller parses the company out of an email line and may have
    # nothing to hand over.
    company = " ".join((company or "").split())
    if not target:
        return raw, "I could not tell who to remove - no name in that line."

    entries = raw.get("executives", [])
    hits = [e for e in entries if _matches(e, target)]
    if not hits:
        return raw, f"Not tracking anyone called \"{target}\" - nothing to remove."

    missed_company = False
    if len(hits) > 1 and company:
        # A namesake with no company on file is not a match and drops out of
        # the tie-break. That is a judgement call: it could be the person
        # meant, employer simply unrecorded. A positive match wins over an
        # unknown because the reply names who went and lists who is left, so
        # a wrong guess is visible and one instruction away from undone.
        narrowed = [e for e in hits if _employer_matches(e, company)]
        if narrowed:
            hits = narrowed
        else:
            # Keep every namesake in the message: naming the companies we do
            # have is more use than repeating the one we could not find.
            missed_company = True

    if len(hits) > 1:
        who = "; ".join(_label(e) for e in hits)
        if missed_company:
            return raw, (
                f"\"{target}\" matches more than one person ({who}), and none of "
                f"them is at {company}. Which one did you mean?"
            )
        # Not when a company was already given and still did not single anyone
        # out - asking again for what they just typed reads like a loop.
        example = "" if company else next(
            (c for c in (_text(e.get("company")) for e in hits) if c), ""
        )
        hint = (
            f"Add the company to say which one - for example: remove {target}, {example}"
            if example
            else "Be more specific."
        )
        return raw, f"\"{target}\" matches more than one person ({who}). {hint}"

    if len(entries) == 1:
        return raw, (
            f"{_label(hits[0])} is the only person on the list, and the list "
            "cannot be empty. Add someone else first."
        )

    new = dict(raw)
    new["executives"] = [e for e in entries if e is not hits[0]]
    return new, f"Stopped tracking {_label(hits[0])}."


def blocked_shows(raw: dict) -> list[str]:
    """The muted show names currently on file."""
    settings = raw.get("settings")
    if not isinstance(settings, dict):
        return []
    return _texts(settings.get("blocked_shows"))


def _with_blocked(raw: dict, shows: list[str]) -> dict:
    """A copy of `raw` with `blocked_shows` replaced, leaving settings intact."""
    new = dict(raw)
    settings = raw.get("settings")
    new["settings"] = {**settings} if isinstance(settings, dict) else {}
    new["settings"]["blocked_shows"] = shows
    return new


def block(raw: dict, show: str) -> tuple[dict, str]:
    """Mute a podcast or channel by name. Returns (new_raw, message).

    A blocked show is dropped from the crawl outright, which makes this the one
    place in the whole pipeline that can lose a real interview. That is on
    purpose - somebody asked for it by name - but it is also why the floor on
    length is enforced here and not only at load time: "block a" would mute
    almost every show on earth, and the digest would go quiet without saying
    why.
    """
    show = " ".join(show.split())
    if not show:
        return raw, "I could not tell which show to block - no name in that line."
    if len(show) < MIN_SHOW_MATCH_CHARS:
        return raw, (
            f"I did not block \"{show}\" - it is too short, and I match show names "
            f"loosely enough that it would mute almost everything. Give me at "
            f"least {MIN_SHOW_MATCH_CHARS} characters of the show's name."
        )

    current = blocked_shows(raw)
    if any(s.casefold() == show.casefold() for s in current):
        return raw, f"\"{show}\" is already blocked."

    return _with_blocked(raw, [*current, show]), (
        f"Blocked \"{show}\" - I will not show you anything from it again."
    )


def unblock(raw: dict, show: str) -> tuple[dict, str]:
    """Un-mute a show. Returns (new_raw, message)."""
    show = " ".join(show.split())
    if not show:
        return raw, "I could not tell which show to unblock - no name in that line."

    current = blocked_shows(raw)
    kept = [s for s in current if s.casefold() != show.casefold()]
    if len(kept) == len(current):
        if not current:
            return raw, f"\"{show}\" is not blocked - nothing is."
        return raw, (
            f"\"{show}\" is not one of the shows I am blocking "
            f"({', '.join(current)})."
        )

    return _with_blocked(raw, kept), f"Unblocked \"{show}\" - it can appear again."


def save(path: str | Path, raw: dict) -> None:
    """Write atomically, and only if the result is a valid config.

    Order matters: validate the candidate before it can replace anything, and
    keep the previous file as .bak so a bad edit is one `mv` from recovery.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    try:
        load_config(tmp)
    except (ValueError, FileNotFoundError) as exc:
        tmp.unlink(missing_ok=True)
        raise WatchlistError(f"Refusing to write an invalid watchlist: {exc}") from exc

    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)
