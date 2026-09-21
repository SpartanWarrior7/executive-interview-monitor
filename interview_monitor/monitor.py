"""The orchestration layer: crawl -> classify -> dedupe -> report."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .classify import classify
from .config import Config, Executive, Settings, slugify
from .slop import judge, matches_show
from .sources import FetchError, Item, Source, enrich_thumbnails, resolve_links
from .store import dedupe_keys


@dataclass
class RunResult:
    new_items: list[Item] = field(default_factory=list)
    candidates: int = 0
    considered: int = 0  # passed classification, before dedupe
    errors: list[str] = field(default_factory=list)
    executives: list[Executive] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    seeded: int = 0
    blocked: int = 0  # dropped because the reader muted the show

    @property
    def real_items(self) -> list[Item]:
        """The interviews we believe in - the body of the report."""
        return [item for item in self.new_items if not item.slop]

    @property
    def suspect_items(self) -> list[Item]:
        """Probably machine-generated, reported separately rather than dropped.

        Still in new_items, so they are still deduped and still recorded: an
        item shown once in the demoted section must not come back next week as
        though it were new.
        """
        return [item for item in self.new_items if item.slop]

    @property
    def by_executive(self) -> dict[str, list[Item]]:
        """Believed-real interviews, grouped by person.

        Suspected slop is deliberately absent: keeping it out of here is what
        demotion *is*, and it means every renderer inherits the split without
        having to remember to ask. Read suspect_items for the rest.
        """
        grouped: dict[str, list[Item]] = {}
        for item in self.real_items:
            grouped.setdefault(item.exec_id, []).append(item)
        return grouped


def _recent_enough(item: Item, cutoff: datetime) -> bool:
    # Undated items are kept; dedupe stops them from repeating.
    return item.published is None or item.published >= cutoff


def crawl(
    config: Config,
    sources: list[Source],
    *,
    days: int | None = None,
    min_score: int | None = None,
    verbose: bool = False,
    log=print,
) -> tuple[list[Item], int, list[str], int]:
    """Fetch and classify.

    Returns (accepted_items, candidate_count, errors, blocked_count). Accepted
    items carry both scores: `score`/`signals` from classification, and
    `slop_score`/`slop_reasons`/`slop` from slop.judge. Nothing is dropped for
    looking machine-generated - only for coming from a show the reader muted.
    """
    days = days if days is not None else config.settings.lookback_days
    min_score = min_score if min_score is not None else config.settings.min_score
    limit = config.settings.max_results_per_source
    blocked_shows = config.settings.blocked_shows
    trusted_shows = config.settings.trusted_shows
    slop_threshold = config.settings.slop_threshold
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    accepted: list[Item] = []
    candidates = 0
    blocked = 0
    errors: list[str] = []

    for exec_obj in config.executives:
        if verbose:
            log(f"  scanning {exec_obj.name} ...")
        for source in sources:
            try:
                items = source.search(exec_obj, days=days, limit=limit)
            except FetchError as exc:
                errors.append(f"{source.name}/{exec_obj.id}: {exc}")
                continue
            except Exception as exc:  # a broken source must not kill the run
                errors.append(f"{source.name}/{exec_obj.id}: {type(exc).__name__}: {exc}")
                continue

            candidates += len(items)
            for item in items:
                if not _recent_enough(item, cutoff):
                    continue
                named, score, signals = classify(item, exec_obj)
                if not named or score < min_score:
                    continue
                item.score = score
                item.signals = signals
                # A muted show is the one thing we discard outright. Checked
                # after classification rather than before it so that `blocked`
                # counts interviews actually withheld: a blocked show's back
                # catalogue matches the search term dozens of times per run, and
                # a footer reporting all of those as "hidden" would imply we had
                # thrown away far more than we did.
                muted = matches_show(item.publisher, blocked_shows)
                if muted:
                    blocked += 1
                    if verbose:
                        log(f"    blocked ({muted}): {item.title}")
                    continue
                # After the score, not before: one of the rescues reads it.
                judge(
                    item,
                    exec_obj,
                    threshold=slop_threshold,
                    trusted_shows=trusted_shows,
                )
                accepted.append(item)
            time.sleep(0.6)  # spread requests out across sources

    # Best first, so that if two variants of the same story survive dedupe
    # ordering, the strongest one is what gets recorded and reported.
    #
    # Interview score stays the primary key. Demotion only breaks ties: it must
    # not outrank confidence, because search_person shares this sort and the web
    # UI orders by confidence, and because dedupe keeps whichever copy comes
    # first - a weak clean copy should not displace the strong one just for
    # being unflagged.
    accepted.sort(
        key=lambda i: (
            i.score,
            not i.slop,
            i.published or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return accepted, candidates, errors, blocked


def search_person(
    name: str,
    sources: list[Source],
    *,
    company: str = "",
    company_match: str = "boost",
    days: int = 90,
    min_score: int = 3,
    limit_per_source: int = 25,
    resolve: bool = True,
    thumbnails: bool = True,
) -> tuple[list[Item], list[str]]:
    """Ad-hoc search for one person, for the web UI.

    Unlike `run`, this consults no seen-database: a search should return
    everything that matches, not only what has never been reported.

    `company_match="require"` drops results that never mention the company,
    which is what disentangles two people with the same name.
    """
    exec_obj = Executive(
        id=slugify(name) or "query",
        name=name.strip(),
        company=company.strip(),
        company_match=company_match if company.strip() else "off",
    )
    config = Config(
        executives=[exec_obj],
        settings=Settings(
            lookback_days=days, min_score=min_score, max_results_per_source=limit_per_source
        ),
    )

    accepted, _, errors, _ = crawl(config, sources, days=days, min_score=min_score)

    deduped: list[Item] = []
    seen: set[str] = set()
    for item in accepted:
        keys = {key for _, key in dedupe_keys(item)}
        if keys & seen:
            continue
        seen |= keys
        deduped.append(item)

    if resolve:
        resolve_links(deduped)
    if thumbnails:
        enrich_thumbnails(deduped)
    return deduped, errors


def run(
    config: Config,
    sources: list[Source],
    store,
    *,
    days: int | None = None,
    min_score: int | None = None,
    dry_run: bool = False,
    seed: bool = False,
    resolve: bool = True,
    verbose: bool = False,
    log=print,
) -> RunResult:
    """One full activation of the monitor."""
    result = RunResult(executives=config.executives)
    run_id = store.start_run()

    accepted, candidates, errors, blocked = crawl(
        config, sources, days=days, min_score=min_score, verbose=verbose, log=log
    )
    result.candidates = candidates
    result.considered = len(accepted)
    result.errors = errors
    result.blocked = blocked

    if seed:
        # Baseline mode: swallow everything currently out there so the next
        # run only reports genuinely new interviews.
        result.seeded = store.seed_from_existing(accepted)
        store.finish_run(run_id, candidates=candidates, new_items=0, errors="; ".join(errors))
        return result

    # In-run dedupe too: the same interview often turns up from several sources.
    seen_this_run: set[str] = set()
    for item in accepted:
        keys = {key for _, key in dedupe_keys(item)}
        if keys & seen_this_run or not store.is_new(item):
            continue
        seen_this_run |= keys
        result.new_items.append(item)

    # Only the items we are about to report need a publisher-facing link, so
    # resolve after dedupe rather than paying two requests per candidate.
    if resolve and result.new_items:
        if verbose:
            log(f"  resolving {len(result.new_items)} link(s) ...")
        resolve_links(result.new_items, log=log if verbose else None)

    if not dry_run:
        for item in result.new_items:
            store.record(item)

    store.finish_run(
        run_id,
        candidates=candidates,
        new_items=len(result.new_items),
        errors="; ".join(errors),
    )
    return result
