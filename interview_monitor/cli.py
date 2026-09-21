"""Command-line entry point. `python run.py --help`"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import commands, inbox, mailer, watchlist
from .config import load_config
from .monitor import run as run_monitor
from .report import (
    digest_subject,
    render,
    render_command_reply,
    render_digest_html,
    render_digest_text,
)
from .sources import build_sources
from .store import Store

ROOT = Path(__file__).resolve().parent.parent

# How many ticks a command email may fail on an unreadable watchlist before we
# stop retrying it and say so. Three tries on the 15-minute timer is roughly
# half an hour of grace for a half-finished hand edit, which is generous for a
# file nobody is editing and irrelevant to one nobody will.
MAX_WATCHLIST_ATTEMPTS = 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="interview-monitor",
        description="Crawl the web for new interviews with tracked business executives.",
    )
    p.add_argument("--config", default=None,
                   help="path to the executive list (default: $WATCHLIST_PATH, "
                        "else state/executives.json if present, else executives.json)")
    p.add_argument("--state", default=str(ROOT / "state" / "seen.sqlite3"),
                   help="path to the seen-interviews database")
    p.add_argument("--days", type=int, default=None,
                   help="lookback window in days (overrides config)")
    p.add_argument("--min-score", type=int, default=None,
                   help="confidence threshold; higher = stricter (overrides config)")
    p.add_argument("--format", choices=["text", "markdown", "json", "digest", "html"],
                   default="text",
                   help="digest = per-executive summary including those with nothing new")
    p.add_argument("--email", action="store_true",
                   help="email the digest (SMTP_* and EMAIL_TO must be set)")
    p.add_argument("--email-to", default=None,
                   help="override EMAIL_TO for this run")
    p.add_argument("--email-only-if-new", action="store_true",
                   help="skip the email entirely when nothing new was found")
    p.add_argument("--out", default=None, help="write the report to a file as well as stdout")
    p.add_argument("--min-video-minutes", type=int, default=None,
                   help="drop YouTube videos shorter than this (overrides config)")
    p.add_argument("--sources", default=None,
                   help="comma-separated subset, e.g. google-news,apple-podcasts")
    p.add_argument("--seed", action="store_true",
                   help="first-run baseline: mark everything currently findable as seen")
    p.add_argument("--dry-run", action="store_true",
                   help="report findings without recording them as seen")
    p.add_argument("--no-resolve-links", action="store_true",
                   help="keep news.google.com redirect links instead of resolving "
                        "them to the publisher's URL (faster)")
    p.add_argument("--verbose", action="store_true", help="show progress and source errors")
    p.add_argument("--stats", action="store_true", help="print database stats and exit")
    p.add_argument("--check-mail", action="store_true",
                   help="read COMMAND_MAILBOX and apply add/remove instructions "
                        "sent by reply, then exit")
    return p


def _watchlist_failure_notice(reason: str, attempts: int) -> str:
    """Plain text telling the sender their change was not applied.

    Text and not HTML on purpose: what this reply is really carrying is a file
    path and a parser error, and those survive a plain body unmangled and
    unescaped. There is nothing here worth styling.

    "unusable" rather than "unreadable" because the same error covers the file
    we refused to write - a candidate that would not have loaded back - and
    telling someone their file cannot be read when it cannot be written sends
    them looking in the wrong place. The detail line says which it was.
    """
    detail = "\n".join(f"  {line}" for line in reason.splitlines())
    return (
        "I could not make your change - the tracked-executive list on the "
        "server is unusable.\n\n"
        f"{detail}\n\n"
        f"I have tried {attempts} times now and have stopped retrying this "
        "message, so nothing you asked for was done and the list is unchanged. "
        "Once the file is valid again, send your instructions in a new reply.\n"
    )


def check_mail(args) -> int:
    """Apply watchlist changes people sent by replying to the digest."""
    try:
        cfg = inbox.InboxConfig.from_env()
    except mailer.MailError as exc:
        print(f"Email commands are not configured: {exc}", file=sys.stderr)
        return 2

    path = watchlist.resolve_path(args.config)
    mailbox = inbox.Mailbox(cfg)
    try:
        messages = mailbox.fetch_unread()
    except mailer.MailError as exc:
        print(f"Could not read {cfg.mailbox}: {exc}", file=sys.stderr)
        return 3

    if not messages:
        if args.verbose:
            print(f"No unread mail in {cfg.mailbox}.")
        return 0

    if args.verbose:
        print(f"{len(messages)} unread message(s) in {cfg.mailbox}; watchlist is {path}")

    failures = 0
    with Store(args.state) as store:
        for message in messages:
            key = message.internet_message_id or message.id
            if store.mail_processed(key):
                # Already acted on. The only thing left is the read flag, which
                # is what we failed at last time if we are here at all.
                if not args.dry_run:
                    try:
                        mailbox.mark_read(message.id)
                    except mailer.MailError as exc:
                        print(f"Could not mark {key} read: {exc}", file=sys.stderr)
                        failures += 1
                continue

            verdict = inbox.authorize(message, cfg)
            if not verdict.ok:
                # Never reply: answering a forged From: turns this mailbox into
                # a backscatter source.
                if verdict.action == inbox.REJECT:
                    print(f"Ignored mail from {message.sender or '(none)'}: {verdict.reason}",
                          file=sys.stderr)
                elif args.verbose:
                    print(f"Skipped {key}: {verdict.reason}")
                if not args.dry_run:
                    store.record_mail(key, sender=message.sender, outcome=verdict.action)
                    try:
                        mailbox.mark_read(message.id)
                    except mailer.MailError as exc:
                        print(f"Could not mark {key} read: {exc}", file=sys.stderr)
                        failures += 1
                continue

            try:
                outcome = commands.handle(
                    message.subject, message.body, path, dry_run=args.dry_run
                )
            except watchlist.WatchlistError as exc:
                # The file on disk is unusable. Leave the message unread so the
                # instruction is not lost once someone fixes it - but only for
                # a few ticks. Retrying forever is silence: the sender never
                # hears that their change did not happen, and the only trace is
                # a stderr line in a journal nobody reads.
                print(f"Watchlist error handling mail from {message.sender}: {exc}",
                      file=sys.stderr)
                if args.dry_run:
                    failures += 1
                    continue

                attempts = store.record_mail_attempt(key, error=str(exc))
                if attempts < MAX_WATCHLIST_ATTEMPTS:
                    if args.verbose:
                        print(f"Leaving {key} unread to try again "
                              f"({attempts}/{MAX_WATCHLIST_ATTEMPTS}).")
                    failures += 1
                    continue

                # Giving up. Recorded before the reply for the same reason as
                # the success path below: the durable decision comes first, and
                # a lost apology beats one sent every quarter of an hour.
                print(f"Giving up on {key} after {attempts} attempts; telling "
                      f"{message.sender} the change was not applied.", file=sys.stderr)
                if not store.record_mail(key, sender=message.sender,
                                         outcome="watchlist-error"):
                    # An overlapping run has already answered this one. The
                    # attempt count it read may have been stale; the insert is
                    # not, so it decides who apologises.
                    continue
                try:
                    mailbox.reply(message.id, _watchlist_failure_notice(str(exc), attempts))
                    mailbox.mark_read(message.id)
                except mailer.MailError as reply_exc:
                    print(f"Could not tell {message.sender} that the watchlist is "
                          f"unusable: {reply_exc}", file=sys.stderr)
                    failures += 1
                # Not counted as a failure otherwise: the message has been
                # answered and closed, and repeating exit 3 every 15 minutes
                # over one we are finished with would bury the next real one.
                continue

            text, html = render_command_reply(outcome)
            print(f"From {message.sender}: "
                  f"{len(outcome.applied)} applied, {len(outcome.rejected)} rejected"
                  f"{' (dry run)' if args.dry_run else ''}")
            if args.verbose or args.dry_run:
                print(text)

            if args.dry_run:
                continue

            # Recorded before the reply: the watchlist is already written, and
            # re-running the instructions to get a confirmation out would be
            # worse than the missing confirmation.
            store.record_mail(key, sender=message.sender,
                              outcome="changed" if outcome.changed else "no-change")
            # One shared list: an edit changes what everyone receives, so the
            # rest of the digest audience is copied in. Only on a real change -
            # "list" and "help" concern nobody but whoever asked.
            cc = cfg.others_to_tell(message.sender) if outcome.changed else []
            if cc and args.verbose:
                print(f"Copying {', '.join(cc)} on the change.")
            try:
                mailbox.reply(message.id, text, html, cc=cc)
                mailbox.mark_read(message.id)
            except mailer.MailError as exc:
                print(f"Applied the change but could not answer {message.sender}: {exc}",
                      file=sys.stderr)
                failures += 1

    return 3 if failures else 0


def main(argv: list[str] | None = None) -> int:
    # Console reports contain publisher names with non-ASCII characters.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)

    if args.stats:
        with Store(args.state) as store:
            for key, value in store.stats().items():
                print(f"{key}: {value}")
        return 0

    if args.check_mail:
        return check_mail(args)

    try:
        config = load_config(watchlist.resolve_path(args.config))
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    sources = build_sources(
        args.sources.split(",") if args.sources else None,
        min_video_seconds=(
            args.min_video_minutes
            if args.min_video_minutes is not None
            else config.settings.min_video_minutes
        ) * 60,
        youtube_channels=config.settings.youtube_channels,
    )
    if not sources:
        print("No sources enabled.", file=sys.stderr)
        return 2

    if args.verbose:
        print(f"Tracking {len(config.executives)} executive(s) "
              f"via {', '.join(s.name for s in sources)}")

    with Store(args.state) as store:
        result = run_monitor(
            config,
            sources,
            store,
            days=args.days,
            min_score=args.min_score,
            dry_run=args.dry_run,
            seed=args.seed,
            resolve=not args.no_resolve_links,
            verbose=args.verbose,
        )

    report = render(result, args.format)
    print(report)

    if args.verbose and result.errors:
        print("Source errors:", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        if args.verbose:
            print(f"Report written to {out_path}")

    if args.email:
        if args.email_only_if_new and not result.new_items:
            print("Nothing new - email skipped (--email-only-if-new).")
        else:
            try:
                sent_to = mailer.send(
                    digest_subject(result),
                    render_digest_text(result),
                    render_digest_html(result),
                    to_override=args.email_to or "",
                )
                print(f"Digest emailed to {', '.join(sent_to)}")
            except mailer.MailError as exc:
                # The crawl succeeded; only delivery failed. Say so loudly and
                # exit non-zero so a scheduler surfaces it.
                print(f"Email failed: {exc}", file=sys.stderr)
                return 3

    # Exit code 0 = ran fine (with or without findings), 1 = every source failed,
    # 3 = found results but could not deliver them.
    if result.errors and result.candidates == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
