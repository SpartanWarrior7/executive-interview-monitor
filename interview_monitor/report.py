"""Rendering a run into the report the business actually reads."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import inbox
from .commands import Outcome, help_text
from .monitor import RunResult

NO_NEWS = "No new interviews."


def _when(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "date unknown"


def _exec_lookup(result: RunResult) -> dict:
    return {e.id: e for e in result.executives}


def _kind(item) -> str:
    """Media type, with runtime attached when we know it."""
    if item.duration_seconds:
        minutes = item.duration_seconds // 60
        return f"{item.media_type}, {minutes} min"
    return item.media_type


def _footer(result: RunResult) -> str:
    checked = len(result.executives)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        f"Checked {checked} executive{'s' if checked != 1 else ''} "
        f"across {result.candidates} results"
    ]
    # Said out loud on purpose. A block is the only thing that throws an
    # interview away, so the count of what it threw away belongs in the report
    # rather than only in the logs.
    if result.blocked:
        parts.append(f"{result.blocked} hidden from blocked shows")
    parts.append(stamp)
    return " | ".join(parts)


SUSPECT_HEADING = "PROBABLY NOT REAL INTERVIEWS"
SUSPECT_BLURB = (
    "Machine-generated episodes about the person rather than with them. "
    'Shown in case I am wrong. Reply "block <show name>" to stop a show for good.'
)


def _why(item) -> str:
    """The demotion reasons, deduplicated, in the order they fired."""
    seen: list[str] = []
    for reason in item.slop_reasons:
        if reason not in seen:
            seen.append(reason)
    return ", ".join(seen)


# Deliberately a fuller set than commands.help_text(): help_text answers
# someone whose reply already failed, while the footer is the only place a
# first-time reader learns the channel exists at all. So it shows the
# plain-English "... to tracker" wording people reach for unprompted as well
# as the terse form, and heads off "add Apple company" before it is typed.
FOOTER_EXAMPLES = (
    "add Tim Cook, Apple CEO",
    "add Jane Doe to tracker",
    "remove Michael Brown, Initech from tracker",
    "block Business Icons Daily",
    "list",
)

FOOTER_NOTES = (
    "One instruction per line. Name the company too when two people share a name.",
    'I track people, not companies - name a person at one, not "add Apple".',
    'Reply "help" if you get stuck.',
)


def _reply_footer(count: int) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    """(intro, examples, notes) for the digest footer, or None when off.

    This is the whole discoverability mechanism: the reader learns what to
    type from the email itself and never has to be told where the config file
    lives. None when the reply channel is switched off, so the digest never
    invites a reply nobody is listening for.

    Returned in three parts rather than as one block of lines because the HTML
    digest sets the examples in monospace and the notes in small grey text.
    """
    if not inbox.configured():
        return None
    intro = (
        f"Tracking {count} {'person' if count == 1 else 'people'}. "
        "To change that, reply to this email in plain English:"
    )
    return intro, FOOTER_EXAMPLES, FOOTER_NOTES


def _reply_instructions(count: int) -> list[str]:
    """The footer flattened into lines, for the plain-text digest."""
    footer = _reply_footer(count)
    if footer is None:
        return []
    intro, examples, notes = footer
    return [intro, *(f"    {example}" for example in examples), *notes]


def render_text(result: RunResult) -> str:
    execs = _exec_lookup(result)
    lines: list[str] = []

    if result.seeded:
        lines.append(f"Baseline recorded: {result.seeded} existing interviews marked as seen.")
        lines.append("Future runs will report only interviews published after this point.")
        lines.append("")
        lines.append(_footer(result))
        return "\n".join(lines)

    real = result.real_items
    suspects = result.suspect_items

    if not real:
        lines.append(NO_NEWS)
        lines.append("")
    else:
        count = len(real)
        lines.append(f"{count} new interview{'s' if count != 1 else ''} found.")
        lines.append("")
        for exec_id, items in result.by_executive.items():
            who = execs[exec_id].label if exec_id in execs else exec_id
            for item in items:
                lines.append(f"NEW INTERVIEW with {who}")
                lines.append(f'  "{item.title}"')
                meta = " | ".join(
                    p for p in (item.publisher, _when(item.published), _kind(item)) if p
                )
                lines.append(f"  {meta}")
                lines.append(f"  {item.url}")
                lines.append("")

    if suspects:
        lines.append(f"{SUSPECT_HEADING} ({len(suspects)})")
        lines.append(SUSPECT_BLURB)
        lines.append("")
        for item in suspects:
            who = execs[item.exec_id].label if item.exec_id in execs else item.exec_id
            lines.append(f"  about {who}: \"{item.title}\"")
            meta = " | ".join(
                p for p in (item.publisher, _when(item.published), _kind(item)) if p
            )
            lines.append(f"  {meta}")
            lines.append(f"  why: {_why(item)}")
            lines.append(f"  {item.url}")
            lines.append("")

    if result.errors:
        lines.append(f"({len(result.errors)} source error(s) - run with --verbose for detail)")
    lines.append(_footer(result))
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(result: RunResult) -> str:
    execs = _exec_lookup(result)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Executive Interview Monitor - {stamp}", ""]

    if result.seeded:
        lines.append(f"Baseline recorded: **{result.seeded}** existing interviews marked as seen.")
        return "\n".join(lines) + "\n"

    real = result.real_items
    suspects = result.suspect_items

    if not result.new_items:
        lines.append(f"**{NO_NEWS}**")
        lines.append("")
        lines.append(f"_{_footer(result)}_")
        return "\n".join(lines) + "\n"

    count = len(real)
    if real:
        lines.append(f"**{count} new interview{'s' if count != 1 else ''} found.**")
    else:
        lines.append(f"**{NO_NEWS}**")
    lines.append("")
    for exec_id, items in result.by_executive.items():
        who = execs[exec_id].label if exec_id in execs else exec_id
        lines.append(f"## {who}")
        for item in items:
            lines.append(f"- [{item.title}]({item.url})")
            meta = " | ".join(
                p for p in (item.publisher, _when(item.published), _kind(item)) if p
            )
            lines.append(f"  - {meta}")
        lines.append("")

    if suspects:
        lines.append(f"## {SUSPECT_HEADING.title()} ({len(suspects)})")
        lines.append("")
        lines.append(f"_{SUSPECT_BLURB}_")
        lines.append("")
        for item in suspects:
            who = execs[item.exec_id].label if item.exec_id in execs else item.exec_id
            lines.append(f"- [{item.title}]({item.url})")
            meta = " | ".join(
                p for p in (item.publisher, _when(item.published), _kind(item)) if p
            )
            lines.append(f"  - about {who} | {meta}")
            lines.append(f"  - why: {_why(item)}")
        lines.append("")

    if result.errors:
        lines.append(f"<sub>{len(result.errors)} source error(s) during this run.</sub>")
        lines.append("")
    lines.append(f"_{_footer(result)}_")
    return "\n".join(lines) + "\n"


def render_json(result: RunResult) -> str:
    execs = _exec_lookup(result)

    def entry(i) -> dict:
        return {
            "executive": execs[i.exec_id].name if i.exec_id in execs else i.exec_id,
            "executive_id": i.exec_id,
            "company": execs[i.exec_id].company if i.exec_id in execs else "",
            "title": i.title,
            "url": i.url,
            "publisher": i.publisher,
            "published": i.published.isoformat() if i.published else None,
            "media_type": i.media_type,
            "duration_seconds": i.duration_seconds,
            "confidence": i.score,
            "matched_signals": i.signals,
            "found_via": i.origin,
            # Exposed even when the item was not demoted, so the thresholds can
            # be tuned against near misses rather than only against failures.
            "slop_score": i.slop_score,
            "slop_reasons": i.slop_reasons,
            "probably_not_real": i.slop,
        }

    real = result.real_items
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "executives_tracked": len(result.executives),
        "candidates_scanned": result.candidates,
        "blocked_by_show": result.blocked,
        "new_interview_count": len(real),
        "status": "no_new_interviews" if not real else "new_interviews",
        "seeded": result.seeded,
        # new_interviews keeps its meaning: the ones we stand behind. Demoted
        # items move to their own key rather than vanishing, so anything reading
        # this feed can still see everything the crawl found.
        "new_interviews": [entry(i) for i in real],
        "probably_not_real": [entry(i) for i in result.suspect_items],
        "errors": result.errors,
    }
    return json.dumps(payload, indent=2)


def digest_subject(result: RunResult) -> str:
    """The subject line, counting only interviews we stand behind.

    Demoted items are counted separately rather than folded in or hidden: a
    digest carrying nothing but generated episodes must not read as "no new
    interviews", because there is something in it to look at.
    """
    real = result.real_items
    n = len(real)
    aside = len(result.suspect_items)
    tail = f" (+{aside} probably not real)" if aside else ""

    if n == 0:
        return f"Interview digest: no new interviews{tail}"
    who = {i.exec_id for i in real}
    people = "1 executive" if len(who) == 1 else f"{len(who)} executives"
    return (
        f"Interview digest: {n} new interview{'' if n == 1 else 's'} ({people}){tail}"
    )


def render_digest_text(result: RunResult) -> str:
    """Per-executive digest. Every tracked executive appears, including the
    ones with nothing new - silence about a person is itself the report."""
    grouped = result.by_executive
    execs = _exec_lookup(result)
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    lines = [f"INTERVIEW DIGEST - {stamp}", "=" * 58, ""]

    for exec_obj in result.executives:
        items = grouped.get(exec_obj.id, [])
        if not items:
            lines.append(f"{exec_obj.label}: No new interviews")
            lines.append("")
            continue

        lines.append(f"{exec_obj.label}: {len(items)} new")
        for n, item in enumerate(items, 1):
            lines.append(f'  {n}. "{item.title}"')
            meta = " | ".join(
                p for p in (item.publisher, _when(item.published), _kind(item)) if p
            )
            lines.append(f"     {meta}")
            lines.append(f"     {item.url}")
        lines.append("")

    suspects = result.suspect_items
    if suspects:
        lines.append("-" * 58)
        lines.append(f"{SUSPECT_HEADING} ({len(suspects)})")
        lines.append(SUSPECT_BLURB)
        lines.append("")
        for n, item in enumerate(suspects, 1):
            who = execs.get(item.exec_id)
            lines.append(f'  {n}. "{item.title}"')
            meta = " | ".join(
                p for p in (item.publisher, _when(item.published), _kind(item)) if p
            )
            lines.append(f"     {meta}")
            lines.append(f"     about {who.label if who else item.exec_id}")
            lines.append(f"     why: {_why(item)}")
            lines.append(f"     {item.url}")
        lines.append("")

    lines.append("-" * 58)
    lines.append(_footer(result))
    if result.errors:
        lines.append(f"{len(result.errors)} source error(s) during this run.")

    instructions = _reply_instructions(len(result.executives))
    if instructions:
        lines.extend(["", *instructions])
    return "\n".join(lines) + "\n"


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_digest_html(result: RunResult) -> str:
    """Email-client-safe HTML: inline styles only, no external assets."""
    grouped = result.by_executive
    execs = _exec_lookup(result)
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    p = "margin:0 0 4px;font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
    out = [
        '<div style="max-width:640px;margin:0 auto;padding:8px">',
        f'<p style="{p};color:#6b7280;font-size:12px;text-transform:uppercase;'
        f'letter-spacing:.06em">Interview digest &middot; {_esc(stamp)}</p>',
    ]

    for exec_obj in result.executives:
        items = grouped.get(exec_obj.id, [])
        out.append(
            '<div style="border-top:1px solid #e5e7eb;padding:14px 0 6px">'
        )
        if not items:
            out.append(
                f'<p style="{p}"><strong>{_esc(exec_obj.label)}:</strong> '
                f'<span style="color:#6b7280">No new interviews</span></p>'
            )
            out.append("</div>")
            continue

        out.append(
            f'<p style="{p}"><strong>{_esc(exec_obj.label)}:</strong> '
            f'<span style="color:#1f6feb">{len(items)} new</span></p>'
        )
        out.append('<ul style="margin:8px 0 0;padding-left:20px">')
        for item in items:
            meta = " &middot; ".join(
                _esc(x) for x in (item.publisher, _when(item.published), _kind(item)) if x
            )
            out.append(
                f'<li style="margin:0 0 10px">'
                f'<a href="{_esc(item.url)}" style="{p};color:#1f6feb;'
                f'text-decoration:none;font-weight:600">{_esc(item.title)}</a>'
                f'<br><span style="{p};color:#6b7280;font-size:12px">{meta}</span>'
                f"</li>"
            )
        out.append("</ul></div>")

    suspects = result.suspect_items
    if suspects:
        # Visually quieter than the real section - greyed, boxed, and last - so
        # it reads as an appendix rather than as more of the report.
        out.append(
            '<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;'
            'padding:12px 14px;margin-top:16px">'
            f'<p style="{p};color:#6b7280;font-size:12px;text-transform:uppercase;'
            f'letter-spacing:.06em">{_esc(SUSPECT_HEADING)} '
            f"&middot; {len(suspects)}</p>"
            f'<p style="{p};color:#6b7280;font-size:12px">{_esc(SUSPECT_BLURB)}</p>'
            '<ul style="margin:10px 0 0;padding-left:20px">'
        )
        for item in suspects:
            who = execs.get(item.exec_id)
            meta = " &middot; ".join(
                _esc(x) for x in (item.publisher, _when(item.published), _kind(item)) if x
            )
            out.append(
                f'<li style="margin:0 0 10px">'
                f'<a href="{_esc(item.url)}" style="{p};color:#6b7280;'
                f'text-decoration:none">{_esc(item.title)}</a>'
                f'<br><span style="{p};color:#9ca3af;font-size:12px">{meta}</span>'
                f'<br><span style="{p};color:#9ca3af;font-size:12px">about '
                f"{_esc(who.label if who else item.exec_id)} &middot; why: "
                f"{_esc(_why(item))}</span></li>"
            )
        out.append("</ul></div>")

    out.append(
        f'<p style="{p};color:#9ca3af;font-size:12px;border-top:1px solid #e5e7eb;'
        f'padding-top:12px;margin-top:14px">{_esc(_footer(result))}</p>'
    )

    footer = _reply_footer(len(result.executives))
    if footer is not None:
        intro, examples, notes = footer
        out.append(
            f'<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;'
            f'padding:12px 14px;margin-top:10px">'
            f'<p style="{p};color:#374151;font-size:13px">{_esc(intro)}</p>'
            f'<p style="margin:8px 0;font:13px/1.7 Consolas,Menlo,monospace;color:#111827">'
            + "<br>".join(_esc(example) for example in examples)
            + f'</p><p style="{p};color:#6b7280;font-size:12px">'
            + "<br>".join(_esc(note) for note in notes)
            + "</p></div>"
        )
    out.append("</div>")
    return "\n".join(out)


def _listing_lines(listing: list[str]) -> list[str]:
    count = len(listing)
    header = f"Now tracking {count} {'person' if count == 1 else 'people'}:"
    return [header, *(f"  {n}. {who}" for n, who in enumerate(listing, 1))]


def _blocked_header(shows: list[str]) -> str:
    count = len(shows)
    return f"Blocking {count} {'show' if count == 1 else 'shows'}:"


def _blocked_lines(shows: list[str]) -> list[str]:
    """The muted shows, when there are any. Same self-verifying purpose as the
    tracked list: a block is the only instruction that can lose an interview,
    so its result is spelled out rather than assumed."""
    if not shows:
        return []
    return [_blocked_header(shows), *(f"  {n}. {s}" for n, s in enumerate(shows, 1))]


def _help_lines(outcome: Outcome) -> list[str]:
    """The instructions, plus what we could not read, when they are owed."""
    if not outcome.needs_help:
        return []
    lines: list[str] = []
    if outcome.understood_nothing:
        lines.append("I could not find an instruction I recognised in that reply.")
        lines.extend(f'  You wrote: "{line}"' for line in outcome.unparsed[:3])
        lines.append("")
    lines.extend(help_text())
    return lines


def render_command_reply(outcome: Outcome) -> tuple[str, str]:
    """(text, html) confirming what a reply did.

    Always ends with the full list, so the reply is self-verifying: if an
    instruction was misread, the reader sees it in the list rather than
    finding out weeks later from a digest that never mentions someone. The
    instructions, when they are owed, go above that list - the list is the
    answer to "what did that do?", so nothing is allowed to follow it.
    """
    if outcome.changed:
        headline = "Your list has been updated."
    elif outcome.listed and not outcome.rejected:
        headline = "Here is everyone currently tracked."
    else:
        headline = "I did not change anything."

    lines: list[str] = [headline]
    for block in (outcome.applied, outcome.rejected):
        if block:
            lines.append("")
            lines.extend(f"  {message}" for message in block)

    help_block = _help_lines(outcome)
    if help_block:
        lines.append("")
        lines.extend(help_block)

    # Above the tracked list, not below it: the tracked list stays the last
    # word, which is what makes the reply answer "what did that do?".
    blocked_block = _blocked_lines(outcome.blocked)
    if blocked_block:
        lines.append("")
        lines.extend(blocked_block)

    lines.append("")
    lines.extend(_listing_lines(outcome.listing))

    text = "\n".join(lines) + "\n"

    p = "margin:0 0 6px;font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
    html_out = ['<div style="max-width:640px">',
                f'<p style="{p}"><strong>{_esc(headline)}</strong></p>']
    if outcome.applied:
        html_out.append(
            '<ul style="margin:8px 0 12px;padding-left:20px">'
            + "".join(f'<li style="{p}">{_esc(m)}</li>' for m in outcome.applied)
            + "</ul>"
        )
    if outcome.rejected:
        html_out.append(
            '<ul style="margin:8px 0 12px;padding-left:20px;color:#b45309">'
            + "".join(f'<li style="{p};color:#b45309">{_esc(m)}</li>' for m in outcome.rejected)
            + "</ul>"
        )
    if help_block:
        html_out.append(
            f'<p style="{p};color:#6b7280;font-size:13px;margin-top:14px">'
            + "<br>".join(_esc(line.strip()) for line in help_block if line.strip())
            + "</p>"
        )
    if outcome.blocked:
        html_out.append(
            f'<p style="{p};border-top:1px solid #e5e7eb;padding-top:10px;'
            f'color:#6b7280"><strong>{_esc(_blocked_header(outcome.blocked))}</strong></p>'
            '<ol style="margin:6px 0 0;padding-left:22px">'
            + "".join(
                f'<li style="{p};color:#6b7280">{_esc(show)}</li>'
                for show in outcome.blocked
            )
            + "</ol>"
        )
    html_out.append(
        f'<p style="{p};border-top:1px solid #e5e7eb;padding-top:10px">'
        f'<strong>{_esc(_listing_lines(outcome.listing)[0])}</strong></p>'
        '<ol style="margin:6px 0 0;padding-left:22px">'
        + "".join(f'<li style="{p}">{_esc(who)}</li>' for who in outcome.listing)
        + "</ol>"
    )
    html_out.append("</div>")
    return text, "\n".join(html_out)


def render(result: RunResult, fmt: str) -> str:
    return {
        "text": render_text,
        "markdown": render_markdown,
        "json": render_json,
        "digest": render_digest_text,
        "html": render_digest_html,
    }[fmt](result)

