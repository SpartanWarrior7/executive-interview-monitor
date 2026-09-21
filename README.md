# Executive Interview Monitor

Runs on a schedule, crawls the web for interviews with the executives you
track, and emails a digest containing only the interviews it has never
reported before.

- **No new interviews** → it says exactly that.
- **New interview found** → it names the executive and gives you the link.

> **Project direction (July 2026):** Interview Monitor has pivoted from a
> website-first search tool to an automated email monitor. The scheduled digest
> is the primary interface and operational priority. The password-protected web
> UI remains available for optional, ad hoc searches.

The core crawler requires no third-party Python packages and runs on stock
Python 3.10+. Optional Spotify and YouTube sources require their API
credentials, and automated delivery uses Microsoft Graph or SMTP credentials.

## Setup

1. Put your executives in `executives.json` (start from `executives.example.json`):

```json
{
  "settings": { "lookback_days": 7, "min_score": 3 },
  "executives": [
    { "name": "Jane Executive", "company": "Acme Corp", "title": "CEO",
      "aliases": ["Jane Q. Executive"], "company_aliases": ["Acme"] },
    { "name": "Michael Brown", "company": "Initech", "company_match": "require" },
    "Pat Chairperson (Soylent)"
  ]
}
```

Only `name` is required. `aliases` catch spelling variants, `extra_terms` add
keywords to the search, and the company fields are how you keep two people with
the same name apart — see below.

An entry can also be a single string that carries the company with it, which is
the quickest way to paste in a list:

```
"Pat Chairperson"                              name only
"Pat Chairperson (Soylent)"                    name + company
"Pat Chairperson (CEO, Soylent)"               name + title + company
"Pat Chairperson, Soylent"                     comma
"Pat Chairperson, CFO, Soylent"                comma + title
"Pat Chairperson - Soylent"                    dash, or |
```

A trailing part that reads as a job title (`CEO`, `Chair`, `Head of ...`) is
taken as the title, not the company, so `"Jane Doe, CEO"` still works.

2. Baseline the history once, so the first real run doesn't dump every interview
that already exists:

```bash
python run.py --seed
```

3. From then on, each activation reports only what's new:

```bash
python run.py
```

## Output

```
No new interviews.
Checked 12 executives across 340 results | 2026-07-29 14:00 UTC
```

```
2 new interviews found.

NEW INTERVIEW with Satya Nadella (CEO, Microsoft)
  "On GPS: Exclusive interview with Microsoft CEO Satya Nadella"
  CNN | 2026-07-26 | article
  https://www.cnn.com/2026/07/26/business/video/gps-0726-microsoft-interview-satya-nadella
```

`--format markdown` for email/Slack-ready output, `--format json` to feed another
system. The JSON `status` field is either `no_new_interviews` or `new_interviews`.

## Optional search UI

The monitor answers "what's new since last time?". The web UI answers "show me
everything for this person, right now" — it consults no seen-database, so it
returns every match rather than only unreported ones.

```bash
python serve.py
```

Then open <http://127.0.0.1:8765>. Type a person's name, optionally add a company
to disambiguate, and pick a time window. Results come back as a thumbnail grid
with the platform, publisher, date, runtime and a direct link.

**Company must match** applies the same `require` rule the digest uses: with it
ticked, results that never mention the company are dropped. Leave it unticked
and the company only raises confidence. It does nothing without a company.

**Platform toggles** sit under the search bar, one per source. Platforms whose
credentials are missing render struck-through and disabled, with a tooltip
naming the environment variable they need — so an unconfigured Spotify or
YouTube is visibly off rather than silently absent. Narrowing the selection is
also the speed control: one platform returns in about a second, all five in
about fifteen.

Thumbnails come from each platform's own artwork where the API provides it
(Apple, Spotify, YouTube) and from the article's Open Graph image otherwise,
fetched in parallel. Anything without an image falls back to a lettered card
rather than a broken-image icon.

The server binds to `127.0.0.1` only and inherits credentials from the same
environment variables the CLI uses.

### Sharing it with a team

The server binds to `127.0.0.1` and has no authentication of its own, so it is
safe to run locally and unsafe to expose directly. To put it on a domain, see
[deploy/DEPLOY.md](deploy/DEPLOY.md) — reverse proxy terminates HTTPS and
handles auth, app stays on loopback.

## Automated email digest (primary)

The main mode: run on a schedule, email what's new since the last run.

```bash
python run.py --email
```

Every tracked executive appears in the digest, including the ones with nothing
new — silence about a person is part of the report:

```
Satya Nadella (CEO, Microsoft): 2 new
  1. "On GPS: Exclusive interview with Microsoft CEO Satya Nadella"
     CNN | 2026-07-26 | article
     https://www.cnn.com/2026/07/26/business/video/gps-nadella
  2. "Possible: Satya Nadella on human and token capital"
     Spotify | 2026-07-25 | podcast, 60 min
     https://open.spotify.com/episode/5oYUrm

Jensen Huang (CEO, Nvidia): No new interviews

Lisa Su (CEO, AMD): No new interviews
```

Preview it without sending:

```bash
python run.py --format digest --dry-run
```

Email goes out through **Microsoft Graph** when `GRAPH_TENANT_ID`,
`GRAPH_CLIENT_ID` and `GRAPH_CLIENT_SECRET` are set, otherwise through SMTP if
`SMTP_HOST` is. `EMAIL_BACKEND=graph|smtp` forces one. See
[deploy/interview-search.env.example](deploy/interview-search.env.example).

Graph uses the client-credentials flow — the app authenticates as itself, so no
mailbox password exists to leak or rotate. In Exchange Online, assign the app
the **Application Mail.Send** role through Application RBAC and scope it to the
mailbox configured in `EMAIL_FROM`. Do not also leave an organization-wide
Graph `Mail.Send` grant in Entra: Entra and Exchange permissions are additive,
so the unscoped grant would bypass the mailbox restriction. Failures name the
specific fix rather than returning a bare HTTP code.

Add `--email-only-if-new` if you would rather hear nothing on quiet days. The
default sends every run, so a silent inbox always means the job failed rather
than the news being quiet.

Delivery failure exits `3` while a successful crawl exits `0`, so a scheduler
can tell "found nothing" from "could not tell you".

## Changing the list by email

Editing JSON is not a reasonable ask of the person who reads the digest, so they
can change the list by **replying to it** — one instruction per line:

```
add Tim Cook, Apple CEO
remove Jensen Huang
block Business Icons Daily
list
```

Nobody should have to remember a syntax, so all of these work and mean what
they look like:

```
add Tim Cook, Apple CEO           name, plus optional company and title
add Tim Cook to tracker
remove Jensen Huang
remove Jensen Huang from tracker
Can you remove Jensen Huang       a courtesy opener is not the verb
take Jensen Huang off the list
block Business Icons Daily        mute a show, not a person
unblock Business Icons Daily
list                              who is tracked right now
help                              the instructions, spelled out again
```

`block` takes the name of a **show**, not a person — it is what to reply when a
machine-generated podcast turns up at the foot of the digest. Show names are
kept as written rather than run through the person parser, so *The AI Business
Digest* survives with its "Digest" intact. `unblock`, `mute` and `unmute` are
matched only when typed exactly: `unblock` is close enough to `block` that a
fuzzy match would obey the opposite of what was asked, and that is the one
mistake here that loses interviews.

The quoted digest underneath is ignored, so they can reply normally without
deleting anything first. The subject line counts as an instruction
too, but only when it is not a `RE:`/`FW:` reply subject, so a fresh mail
subjected `add Tim Cook` with an empty body works while a reply to *"RE:
Interview digest: 2 new interviews"* is not read as an instruction to track
something called "Interview digest".

Company and title are optional and understood in every shape the shorthand
above accepts — the reply and the config file go through the same parser, so
`Andy Jassy at Amazon` and `Lisa Su (AMD CEO)` mean here what they mean there.
Typos in the verb are tolerated, and quoted digests, bullets and phone
signatures are ignored. The reply always ends with everyone currently
tracked, so a misread instruction is visible immediately rather than weeks later.
Anything not understood is reported back with the three examples again — never
silently dropped.

### The tracker holds people, not companies

`add Acme company to tracker` is refused, and the reply asks for a named person
instead. This is not a missing feature: nothing here crawls a company. A company
is only a disambiguator — the thing that tells your `Michael Brown` apart from
the other ones in a headline — so there is no companies list for `Acme` to go
on, and adding one would silently track nobody. Name the person you actually
want to hear about:

```
add Jane Doe, Acme CEO
```

Same for `remove Acme company`: remove the people, by name.

### Two people with the same name

`remove Michael Brown` refuses when the list holds two of them and names both,
rather than guessing which one to drop. Add the company to pick one:

```
remove Michael Brown, Initech
```

### Setting it up

Check what the unread mail in the box would do, without writing anything or
answering anyone:

```bash
python run.py --check-mail --dry-run --verbose
```

Reading mail needs `COMMAND_MAILBOX` set (that variable is also the on/off
switch — without it the digest never advertises the feature) and the
**Application Mail.ReadWrite** role assigned through Exchange Application RBAC,
scoped to that mailbox exactly as `Mail.Send` is. Two checks
gate every message: the sender must be in `COMMAND_SENDERS`, and the mail must
pass DMARC according to Exchange's own `Authentication-Results` header, because
a shared mailbox accepts internet mail and `From:` is trivially forged. Mail
failing either check is dropped and never answered.

`COMMAND_FOLDER` names the mail folder to read and defaults to `inbox`. Set it
when an Outlook rule files digest replies into a subfolder: the reader looks in
exactly one folder, so a rule that moves replies out of the inbox otherwise
makes every instruction vanish without a trace.

Writes are atomic, validated before they can replace anything, and leave the
previous file as `executives.json.bak`. On a server point `WATCHLIST_PATH` at
`state/executives.json` so the list is not fighting `git pull` — see
[deploy/DEPLOY.md](deploy/DEPLOY.md) section 9.

### "I replied and nothing happened"

Rejected mail is deliberately never answered — replying to a forged `From:`
would turn the mailbox into a backscatter source — so silence is exactly what a
rejection looks like from the outside. Check, in this order:

1. **Is the sender address in `COMMAND_SENDERS`?** It defaults to `EMAIL_TO`, so
   replying from a second address of your own is a rejection.
2. **Did the reply pass DMARC?** Exchange's `Authentication-Results` header must
   show `dmarc=pass`, or `spf=pass` and `dkim=pass` together. A message whose
   headers are missing entirely is rejected on purpose rather than trusted — the
   shared mailbox accepts internet mail, and `From:` is forgeable.
3. **Is the message still unread?** Anything a human opens in the shared mailbox
   first is skipped.
4. **Is it in the folder `COMMAND_FOLDER` names?** Default `inbox`; an Outlook
   rule may have moved it.
5. **Read the log.** `journalctl -u interview-commands` names the message and the
   reason it was ignored. That is the only place a rejection is reported.
6. **Give it 16 minutes.** The timer is `OnCalendar=*:0/15`. The only two-hour
   wait anywhere in this system is Exchange's RBAC cache when a permission is
   first granted — a one-off during setup, never something a reply waits on.

## Running it on a schedule

`check-interviews.bat` writes a dated Markdown report to `reports\` and appends to
`reports\monitor.log`. Register it for 8am daily:

```bash
schtasks /create /tn "Executive Interview Monitor" /tr "C:\Interview-Monitor\check-interviews.bat" /sc daily /st 08:00
```

## Options

| Flag | Effect |
| --- | --- |
| `--seed` | Mark everything currently findable as seen; report nothing |
| `--dry-run` | Show findings without recording them as seen |
| `--days N` | Lookback window (default 7) |
| `--min-score N` | Confidence threshold; raise for stricter, lower for wider |
| `--min-video-minutes N` | Drop YouTube videos shorter than this (default 10) |
| `--sources a,b` | Restrict to named sources |
| `--format` | `text` (default), `markdown`, `json` |
| `--out PATH` | Also write the report to a file |
| `--no-resolve-links` | Keep Google redirect links instead of resolving them (faster) |
| `--stats` | Print database stats and exit |
| `--check-mail` | Apply add/remove instructions sent by reply, then exit |
| `--config PATH` | Watchlist to use (default `$WATCHLIST_PATH`, else `state/executives.json` if present, else `executives.json`) |
| `--verbose` | Show progress and per-source errors |

## Sources

| Source | Covers | Key needed |
| --- | --- | --- |
| `google-news` | Written interviews, broadest index | no |
| `bing-news` | Independent index; catches Google misses | no |
| `apple-podcasts` | Podcast episodes — most long-form interviews | no |
| `spotify` | Podcast episodes, including Spotify exclusives | yes — see below |
| `youtube` | Video interviews — noisy, see below | yes — set `YOUTUBE_API_KEY` |

A source that fails is logged and skipped; the run continues on the others.

### Enabling Spotify

Spotify needs an app registered at
[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard):

1. **Create app**. Name and description are arbitrary.
2. **Redirect URI** — required by the form even though this crawler never uses
   it. Enter `http://127.0.0.1:8080/callback`. Use `127.0.0.1`, not `localhost`;
   Spotify rejects the latter on new apps.
3. **Which API/SDK** — check **Web API**. Nothing else.
4. On the app's Settings page, copy the **Client ID**, then **View client secret**.

Set both as environment variables and open a new terminal:

```bash
setx SPOTIFY_CLIENT_ID "your-client-id"
```

```bash
setx SPOTIFY_CLIENT_SECRET "your-client-secret"
```

The source activates automatically when both are present. It uses the
client-credentials flow — no user login, no OAuth consent screen, and it reads
only the public catalogue. Tokens last an hour and are cached in memory.

Spotify overlaps heavily with `apple-podcasts`, which is fine: an episode
carried on both platforms is deduplicated by headline and reported once. What
you gain is Spotify-exclusive shows, which Apple genuinely cannot see.

Two API behaviours differ from Spotify's own documentation, both verified
against the live API:

- **Episode search rejects any `limit` above 10** with `400 Invalid limit`,
  despite the docs stating a maximum of 50. The source pages with `offset`
  instead, which works normally.
- **Show names are unavailable.** Episode search returns *simplified* episode
  objects with no `show` field, and `/v1/episodes` — which would supply it —
  returns `403 Forbidden` for client-credentials tokens; it requires a user
  token. Spotify results are therefore attributed to `Spotify` rather than to
  the show. Adding user-token OAuth purely to recover show names is not worth
  the complexity, since the episode title, date, duration and link all survive.

### A warning about YouTube

YouTube is the weakest source, and it is off unless you set `YOUTUBE_API_KEY`.
The platform has a large re-upload economy: clip channels re-cut other outlets'
interviews into shorts, reaction videos, and "full interview in 8 minutes"
condensations, all of which name the executive in the title. In testing, an
unfiltered YouTube search returned 13 hits of which one was a real interview.

Three defences are applied, in order:

1. **Duration floor** (`min_video_minutes`, default 10) — shorts and clips are
   dropped before classification. Costs 1 extra quota unit per 50 videos.
2. **Title-only cues** — commentary videos describe the interview they are
   reacting to, so their descriptions are full of interview language. For video
   only, cues in the description are ignored.
3. **Channel allowlist** (`youtube_channels`) — the only reliable defence. When
   non-empty, only videos from these channels are considered. Match is
   case-insensitive substring, so `"Bloomberg"` covers all Bloomberg channels.

Even so, expect YouTube to be largely redundant: a televised interview is
usually caught by `google-news` first, with a better link.

### YouTube quota

The Data API allows 10,000 units/day. `search.list` costs **100 units** and the
crawler issues one per executive per run, so 20 executives on a daily schedule
costs 2,000 units — comfortable. Hourly runs would exceed the cap. The duration
lookup (`videos.list`) costs 1 unit per 50 videos and is negligible.

Google News hands out redirect links, so reported items are resolved back to the
publisher's own URL before reporting (`--no-resolve-links` to skip).

## How it decides something is an interview

Two gates, both must pass:

1. **The executive is actually named.** Accent- and spacing-tolerant, full name
   required — "Nvidia said" or a bare surname does not qualify.
2. **It looks like an interview, not reportage about one.** Weighted cues, and at
   least one must be strong: `interview`, `sits down with`, `Q&A`, `fireside chat`,
   `in conversation with`, a podcast/video episode, and so on. Cues in the headline
   count for more than cues in the summary. Negative cues (`job interview`,
   `in talks to`, `declined to comment`) subtract.

Two media-specific rules exist because both failure modes showed up in live
testing:

- **Podcasts must name the executive in the episode title.** Daily AI and
  business news roundups list every executive they mention in the description,
  and would otherwise re-qualify as an interview every week.
- **Videos are scored on their title only.** Reaction and commentary channels
  describe the interview they are reacting to, so their descriptions are full of
  interview language.

Tune with `min_score`: 3 is the default, 2 widens the net, 4+ is strict.

## AI-generated podcasts

A genre of podcast now exists that is nothing but an executive's name read out
by a synthetic voice: biography readouts, "net worth" explainers, four-minute
"full interview" summaries, one episode per famous person. These clear the
interview test honestly — they name the person in the episode title, they say
"interview", they are podcasts — so `min_score` cannot see them. Raising the
threshold does not help either, because a generated episode titled
*"Jensen Huang: The Full Interview"* scores 6 against a default of 3.

They are handled separately, and the governing rule is that **nothing is dropped
for looking generated.** Missing a real interview is far more expensive than
skimming past a fake one, so a suspected episode is scored, labelled with its
reasons, and moved into a `PROBABLY NOT REAL INTERVIEWS` block at the foot of
the digest. It is still delivered, still visible, and still recorded — so it
never comes back next week as though it were new.

What gets scored (`interview_monitor/slop.py`) is not the absence of interview
language but the presence of other things:

| Tell | Worth | Example |
| --- | --- | --- |
| Self-disclosure | 4 | "AI-generated", "synthetic voices", "NotebookLM", "text-to-speech" |
| Scraper boilerplate | 3 | "not affiliated with", "based solely on publicly available information" |
| About-not-with framing | 2 | "The Rise of…", "Net Worth", "Lessons From…", a title trailing off in "Explained" |
| Show-name tells | 2–3 | "…Biography", "…in 10 Minutes", "AI-Narrated…" |
| Implausible runtime | 2–3 | under 5 minutes; under 2 minutes |
| Placeholder show notes | 1–2 | no description, or a description that is just the title again |

And what pulls an item back, so a real interview from an oddly-named show
survives: guest framing in the title (`with <name>`, `<name> joins`) is worth
−3, a runtime over 20 minutes −2, and an interview score of 8+ −2.

The last three rows of that table are **circumstantial** — they describe how
long or how thin an episode is, not what it is — and on their own they never
demote anything, however high the arithmetic runs. *"Jensen Huang: Nvidia
earnings takeaways"*, four minutes, one line of show notes, from Bloomberg
Businessweek trips all three, and news outlets publish that shape constantly.
So at least one tell about what the episode actually *is* has to fire first;
otherwise the item scores 0 and is tagged `circumstantial-only`, with the
observations still listed for tuning. This is the same judgement `classify.py`
makes in the other direction when it refuses to call a pile of weak cues an
interview.

One tell is deliberately **not** in the list: a bare "AI" in a show name.
Nvidia's own podcast is called *The AI Podcast* and a16z's is *AI + a16z*, and
both carry exactly the interviews this monitor exists to find.

Three settings control it:

| Setting | Default | Effect |
| --- | --- | --- |
| `slop_threshold` | `4` | Demote at or above this score. `3` demotes more, `6` less, `0` switches demotion off and only records the scores |
| `blocked_shows` | `[]` | Dropped from the crawl outright — see below |
| `trusted_shows` | `[]` | Never demoted, whatever the score |

Every score is exposed in `--format json`, on believed-real items as well as
demoted ones, so the thresholds can be tuned against near misses rather than
only against failures:

```bash
python run.py --dry-run --format json | python -m json.tool
```

### Blocking a show

`blocked_shows` is the one place in the pipeline that discards an interview on
purpose, and it only ever contains shows you named yourself. Reply to any
digest:

```
block Business Icons Daily
```

`unblock Business Icons Daily` undoes it. The match is a case-insensitive
substring of the show name, minimum three characters — an entry shorter than
that is refused rather than obeyed, because it would quietly mute almost
everything. Every command reply lists the shows currently blocked, for the same
reason it lists the people tracked: a misread name should be visible
immediately, not three silent digests later.

### Telling two people with the same name apart

Gate 1 proves *somebody* with that name was interviewed — not that it was your
person. `Michael Brown` matches the economist, the athlete and your CFO alike.
The company an executive is listed under is the tiebreak, and `company_match`
decides how hard it is used:

| mode | behaviour |
| --- | --- |
| `off` | ignore the company entirely |
| `boost` | **default.** A company mention adds confidence: +2 in the headline, +1 in the summary. Nothing is dropped for lacking one. |
| `require` | An item that never mentions the company is dropped, whatever its interview language. |

Set it per person, or in `settings` for everyone:

```json
{
  "settings": { "company_match": "boost" },
  "executives": [
    { "name": "Michael Brown", "company": "Initech", "company_match": "require" }
  ]
}
```

Use `require` for common names and `boost` for distinctive ones. `require` is a
gate, not a bonus — it does not add points, so `min_score` keeps meaning the
same thing for the people it applies to.

Company matching is forgiving about how the name is written. One trailing legal
suffix is stripped, so `Nvidia Corporation` in the config matches an article
that just says `Nvidia`. For anything further from the registered name — a
ticker, a former name, an abbreviation — list `company_aliases`:

```json
{ "name": "Jensen Huang", "company": "Nvidia Corporation",
  "company_aliases": ["NVDA", "Nvidia Corp"] }
```

Every company spelling is also OR'd into the search query itself, so a common
name spends its per-source result budget on the right person rather than being
filtered out after the fact.

A settings-wide `require` cannot apply to someone with no company — matching
nothing would filter out everything — so those people fall back to `off`.
Asking for `require` on a person with no company is a config error rather than
a silently empty digest.

## How it avoids repeats

A SQLite file at `state/seen.sqlite3` remembers everything reported. An item is
matched two ways, so the same interview syndicated across outlets is reported once:

- normalized URL — tracking parameters, `www.`, and trailing slashes removed
- normalized headline, scoped to the executive — outlet tags like `- CNN` or
  `| CNN Business` stripped

Delete `state/seen.sqlite3` to reset the memory (then `--seed` again).

## Tests

```bash
python -m unittest discover -s tests
```

367 offline tests as of this commit, covering name matching, interview
classification, AI-slop demotion, company disambiguation, URL and headline
normalization, deduplication, seeding, source-failure isolation, config loading,
watchlist edits, email-command parsing, and who is allowed to command the
mailbox. They touch no network and need no credentials.

`tests/test_slop.py` is deliberately lopsided: its `TestRealInterviewsSurvive`
cases are real recorded hits with their real publishers and scores, and they are
the ones that matter. A generated episode slipping through is a tolerable
failure; a real interview being demoted is not.

## Layout

```
run.py                    monitor entry point
serve.py                  search UI entry point
executives.json           the list you maintain (template once deployed)
check-interviews.bat      scheduled-task wrapper
droplet-update.cmd        deploy GitHub main to the droplet, with rollback
web/                      search UI (index.html, app.css, app.js)
interview_monitor/
  config.py               loading and validating the executive list
  watchlist.py            editing that list, atomically and only if valid
  sources.py              crawlers + link resolution
  classify.py             is this an interview with this person?
  slop.py                 is this machine-generated filler rather than an interview?
  store.py                seen-database and deduplication
  monitor.py              crawl -> classify -> dedupe orchestration
  report.py               text / markdown / json rendering
  mailer.py               sending, via Microsoft Graph or SMTP
  inbox.py                reading the digest mailbox; who is allowed to command it
  commands.py             what someone meant by "add Tim Cook, Apple CEO"
  cli.py                  argument handling
  web.py                  local HTTP server + JSON API for the search UI
deploy/                   systemd units, proxy config, DEPLOY.md, the updater
state/seen.sqlite3        memory of what has been reported
state/executives.json     the live list when WATCHLIST_PATH points here
```

## Adding a source

Subclass `Source` in `sources.py`, implement
`search(exec_obj, *, days, limit) -> list[Item]`, and add it to `build_sources()`.
Classification, deduplication, and reporting apply automatically.
