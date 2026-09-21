"""Crawlers. Each source turns an executive into a list of candidate Items.

Everything here uses public, key-free endpoints except YouTube, which is
enabled only when YOUTUBE_API_KEY is set in the environment.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import html
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Terms that, OR'd together, bias a search toward interview-shaped coverage.
INTERVIEW_QUERY_TERMS = [
    "interview",
    "podcast",
    '"sits down"',
    '"in conversation"',
    '"speaks with"',
    '"talks to"',
    "Q&A",
    '"fireside chat"',
]


@dataclass
class Item:
    """One candidate piece of coverage, before classification."""

    exec_id: str
    title: str
    url: str
    origin: str  # which crawler produced it
    publisher: str = ""
    published: datetime | None = None
    summary: str = ""
    media_type: str = "article"  # article | podcast | video
    score: int = 0
    signals: list[str] = field(default_factory=list)
    original_url: str = ""  # pre-resolution link, kept for stable dedupe
    duration_seconds: int | None = None
    thumbnail: str = ""
    # How much this looks machine-generated rather than interviewed, and why.
    # Set after classification by slop.judge; `slop` is the demotion decision,
    # never a reason to drop the item. See slop.py.
    slop_score: int = 0
    slop_reasons: list[str] = field(default_factory=list)
    slop: bool = False

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}"


class FetchError(RuntimeError):
    pass


def _fetch(
    url: str,
    *,
    timeout: int = 20,
    retries: int = 2,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> bytes:
    """GET (or POST, if `data` is given) with a browser-ish UA and linear backoff."""
    ctx = ssl.create_default_context()
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml, application/json;q=0.9, */*;q=0.8",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-US,en;q=0.9",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            # APIs explain themselves in the error body; keep it for diagnosis.
            detail = ""
            try:
                body = exc.read()
                if exc.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                detail = f" {body[:200].decode('utf-8', 'replace')}"
            except Exception:
                pass
            last = FetchError(f"HTTP {exc.code}{detail}")
            if exc.code < 500 or attempt >= retries:
                break  # 4xx will not fix itself on retry
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # noqa: PERF203
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"{url} -> {last}")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _millis_to_seconds(value) -> int | None:
    """A runtime in milliseconds as whole seconds, or None if it is unusable.

    None rather than 0 for anything missing or malformed, because every reader
    of duration_seconds treats unknown as "abstain" and zero as "impossibly
    short". Apple sends trackTimeMillis as a number, Spotify sends duration_ms,
    and both occasionally send neither.
    """
    try:
        seconds = int(value) // 1000
    except (TypeError, ValueError):
        return None
    return seconds or None


_GOOGLE_ARTICLE_RE = re.compile(r"news\.google\.com/rss/articles/([A-Za-z0-9_\-]+)")
_BATCHEXECUTE = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def _decode_legacy_google_url(article_id: str) -> str | None:
    """Older article ids are base64 of a protobuf holding the URL in the clear."""
    padded = article_id + "=" * (-len(article_id) % 4)
    try:
        blob = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return None
    found = re.search(rb"https?://[\x20-\x7e]+", blob)
    if not found:
        return None
    candidate = found.group(0).decode("ascii", "ignore")
    # The protobuf field is length-prefixed, so trailing bytes can leak in.
    candidate = re.split(r"[^\w\-./:?=&%~+#@,;$()!*']", candidate)[0]
    return candidate if candidate.startswith("http") and len(candidate) > 15 else None


def resolve_google_news_url(url: str) -> str:
    """Turn a news.google.com redirect into the publisher's own URL.

    Newer article ids are opaque, so we ask Google's own splash endpoint to
    resolve them - the same call the article page makes in a browser. Any
    failure falls back to the Google link, which still redirects correctly.
    """
    match = _GOOGLE_ARTICLE_RE.search(url)
    if not match:
        return url
    article_id = match.group(1)

    legacy = _decode_legacy_google_url(article_id)
    if legacy:
        return legacy

    try:
        page = _fetch(f"https://news.google.com/rss/articles/{article_id}", retries=1)
        text = page.decode("utf-8", "replace")
        signature = re.search(r'data-n-a-sg="([^"]+)"', text)
        timestamp = re.search(r'data-n-a-ts="([^"]+)"', text)
        if not (signature and timestamp):
            return url

        inner = (
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
            'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
            f'"{article_id}",{timestamp.group(1)},"{signature.group(1)}"]'
        )
        body = "f.req=" + urllib.parse.quote(
            json.dumps([[["Fbv4je", inner, None, "generic"]]])
        )
        req = urllib.request.Request(
            _BATCHEXECUTE,
            data=body.encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
        found = re.search(r'https?://(?!news\.google)[^\\"]+', raw)
        return found.group(0) if found else url
    except Exception:  # resolution is a nicety, never a failure mode
        return url


_OG_PATTERNS = [
    re.compile(
        r'<meta[^>]+property=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::url)?["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
]


def fetch_og_image(url: str, *, timeout: int = 6) -> str:
    """Pull an article's social-preview image. Best effort - blank on any failure."""
    try:
        html_bytes = _fetch(url, timeout=timeout, retries=0)
    except FetchError:
        return ""
    # og: tags live in <head>; no need to scan a megabyte of body markup.
    head = html_bytes[:200_000].decode("utf-8", "replace")
    for pattern in _OG_PATTERNS:
        found = pattern.search(head)
        if found:
            image = html.unescape(found.group(1).strip())
            if image.startswith("//"):
                image = "https:" + image
            elif image.startswith("/"):
                parts = urllib.parse.urlsplit(url)
                image = f"{parts.scheme}://{parts.netloc}{image}"
            if image.startswith("http"):
                return image
    return ""


def enrich_thumbnails(items: list[Item], *, max_workers: int = 8) -> None:
    """Fill in article thumbnails concurrently; podcasts/videos already have art."""
    pending = [i for i in items if not i.thumbnail and i.url.startswith("http")]
    if not pending:
        return
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for item, image in zip(pending, pool.map(lambda i: fetch_og_image(i.url), pending)):
            item.thumbnail = image


def resolve_links(items: list[Item], *, log=None) -> None:
    """Resolve redirect links in place, keeping the original for dedupe."""
    for item in items:
        if not _GOOGLE_ARTICLE_RE.search(item.url):
            continue
        resolved = resolve_google_news_url(item.url)
        if resolved != item.url:
            item.original_url = item.url
            item.url = resolved
            if not item.publisher:
                item.publisher = urllib.parse.urlparse(resolved).netloc.removeprefix("www.")
        elif log:
            log(f"  could not resolve link for: {item.title[:60]}")
        time.sleep(0.3)


def unwrap_bing_url(url: str) -> str:
    """Bing News RSS wraps links in apiclick.aspx?...&url=<encoded target>."""
    if "bing.com/news/apiclick.aspx" not in url:
        return url
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    target = (query.get("url") or [""])[0]
    return target if target.startswith("http") else url


def _build_query(exec_obj, terms: list[str]) -> str:
    names = " OR ".join(f'"{n}"' for n in exec_obj.all_names)
    topic = " OR ".join(terms + [f'"{t}"' for t in exec_obj.extra_terms])
    query = f"({names}) ({topic})"

    companies = getattr(exec_obj, "company_names", None) or (
        [exec_obj.company] if exec_obj.company else []
    )
    if companies and getattr(exec_obj, "company_match", "boost") != "off":
        # Narrow at the source so a common name does not spend the result
        # budget on the wrong person. OR'd, because the same company is
        # written several ways ("Nvidia", "Nvidia Corp", "NVDA").
        where = " OR ".join(f'"{c}"' for c in companies)
        query = f"{query} ({where})"
    return query


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

class Source:
    name = "source"

    def search(self, exec_obj, *, days: int, limit: int) -> list[Item]:  # pragma: no cover
        raise NotImplementedError


class GoogleNewsSource(Source):
    """Google News RSS search - broadest coverage of written interviews."""

    name = "google-news"

    def search(self, exec_obj, *, days: int, limit: int) -> list[Item]:
        query = f"{_build_query(exec_obj, INTERVIEW_QUERY_TERMS)} when:{max(days, 1)}d"
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(query)
            + "&hl=en-US&gl=US&ceid=US:en"
        )
        root = ElementTree.fromstring(_fetch(url))
        items: list[Item] = []
        for node in list(root.iterfind(".//item"))[:limit]:
            link = (node.findtext("link") or "").strip()
            if not link:
                continue
            source_node = node.find("source")
            items.append(
                Item(
                    exec_id=exec_obj.id,
                    title=_clean(node.findtext("title")),
                    url=link,
                    origin=self.name,
                    publisher=_clean(source_node.text if source_node is not None else ""),
                    published=_parse_date(node.findtext("pubDate")),
                    summary=_clean(node.findtext("description")),
                    media_type="article",
                )
            )
        return items


class BingNewsSource(Source):
    """Bing News RSS - independent index, and it returns publisher URLs directly.

    Bing quietly serves an HTML results page instead of RSS when a query gets
    complex, so we issue a few short queries rather than one long boolean one.
    """

    name = "bing-news"
    QUERY_TEMPLATES = ["{name} interview", "{name} podcast", '{name} "sits down with"']

    def search(self, exec_obj, *, days: int, limit: int) -> list[Item]:
        items: list[Item] = []
        seen: set[str] = set()
        failures: list[str] = []
        per_query = max(limit // len(self.QUERY_TEMPLATES), 5)

        for template in self.QUERY_TEMPLATES:
            query = template.format(name=f'"{exec_obj.name}"')
            url = (
                "https://www.bing.com/news/search?q="
                + urllib.parse.quote(query)
                + "&format=RSS&setmkt=en-US"
            )
            try:
                payload = _fetch(url)
            except FetchError as exc:
                failures.append(str(exc))
                continue
            if payload.lstrip()[:9].lower().startswith(b"<!doctype"):
                failures.append(f"HTML fallback for query: {query}")
                continue
            try:
                root = ElementTree.fromstring(payload)
            except ElementTree.ParseError as exc:
                failures.append(f"non-XML response: {exc}")
                continue

            for node in list(root.iterfind(".//item"))[:per_query]:
                link = unwrap_bing_url((node.findtext("link") or "").strip())
                if not link or link in seen:
                    continue
                seen.add(link)
                items.append(
                    Item(
                        exec_id=exec_obj.id,
                        title=_clean(node.findtext("title")),
                        url=link,
                        origin=self.name,
                        publisher=urllib.parse.urlparse(link).netloc.removeprefix("www."),
                        published=_parse_date(node.findtext("pubDate")),
                        summary=_clean(node.findtext("description")),
                        media_type="article",
                    )
                )
            time.sleep(1.5)  # Bing serves the HTML page instead of RSS if pushed

        if not items and failures:
            raise FetchError("; ".join(failures[:2]))
        return items[:limit]


class ApplePodcastSource(Source):
    """iTunes Search API - podcast episodes, where most long-form interviews live."""

    name = "apple-podcasts"

    def search(self, exec_obj, *, days: int, limit: int) -> list[Item]:
        items: list[Item] = []
        seen: set[str] = set()
        for name in exec_obj.all_names:
            url = (
                "https://itunes.apple.com/search?"
                + urllib.parse.urlencode(
                    {
                        "term": name,
                        "entity": "podcastEpisode",
                        "limit": min(limit, 50),
                        "country": "US",
                    }
                )
            )
            payload = json.loads(_fetch(url).decode("utf-8", "replace"))
            for row in payload.get("results", []):
                link = row.get("trackViewUrl") or row.get("episodeUrl") or ""
                if not link or link in seen:
                    continue
                seen.add(link)
                items.append(
                    Item(
                        exec_id=exec_obj.id,
                        title=_clean(row.get("trackName")),
                        url=link,
                        origin=self.name,
                        publisher=_clean(row.get("collectionName")),
                        published=_parse_date(row.get("releaseDate")),
                        summary=_clean(row.get("description") or row.get("shortDescription")),
                        media_type="podcast",
                        duration_seconds=_millis_to_seconds(row.get("trackTimeMillis")),
                        thumbnail=(
                            row.get("artworkUrl600")
                            or row.get("artworkUrl100")
                            or row.get("artworkUrl60")
                            or ""
                        ),
                    )
                )
            time.sleep(0.4)  # be polite to the iTunes endpoint
        return items[:limit]


def _pick_spotify_image(images) -> str:
    """Spotify lists images largest-first; the middle one is plenty for a card."""
    if not isinstance(images, list):
        return ""
    urls = [i.get("url", "") for i in images if isinstance(i, dict) and i.get("url")]
    if not urls:
        return ""
    return urls[len(urls) // 2] if len(urls) > 2 else urls[0]


class SpotifySource(Source):
    """Spotify podcast episodes. Needs SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET.

    Worth running alongside Apple Podcasts rather than instead of it: Spotify
    carries exclusive shows Apple cannot see, and episodes carried on both
    platforms collapse in deduplication.
    """

    name = "spotify"
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    # Spotify documents a max limit of 50, but episode search rejects anything
    # above 10 with "Invalid limit". Verified empirically - paginate instead.
    PAGE_SIZE = 10

    def __init__(self, client_id: str, client_secret: str, market: str = "US"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.market = market
        self._token = ""
        self._token_expires_at = 0.0

    def _token_header(self) -> dict[str, str]:
        """Client-credentials token, cached until just before it expires."""
        if self._token and time.time() < self._token_expires_at:
            return {"Authorization": f"Bearer {self._token}"}

        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        try:
            raw = _fetch(
                self.TOKEN_URL,
                data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                retries=1,
            )
        except FetchError as exc:
            raise FetchError(f"Spotify auth failed (check client id/secret): {exc}") from exc

        payload = json.loads(raw.decode("utf-8", "replace"))
        self._token = payload.get("access_token", "")
        if not self._token:
            raise FetchError("Spotify auth returned no access_token")
        # Refresh a minute early rather than racing the expiry.
        self._token_expires_at = time.time() + int(payload.get("expires_in", 3600)) - 60
        return {"Authorization": f"Bearer {self._token}"}

    def search(self, exec_obj, *, days: int, limit: int) -> list[Item]:
        items: list[Item] = []
        seen: set[str] = set()

        for name in exec_obj.all_names:
            collected = 0
            for offset in range(0, max(limit, 1), self.PAGE_SIZE):
                url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode(
                    {
                        "q": name,
                        "type": "episode",
                        "market": self.market,  # omitting this returns null items
                        "limit": self.PAGE_SIZE,
                        "offset": offset,
                    }
                )
                payload = json.loads(
                    _fetch(url, headers=self._token_header()).decode("utf-8", "replace")
                )
                # Spotify pads the array with nulls for unavailable episodes,
                # and the envelope shape varies, so trust nothing about it.
                envelope = payload.get("episodes")
                rows = envelope.get("items") if isinstance(envelope, dict) else None
                rows = rows or []
                if not rows:
                    break

                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    link = ((row.get("external_urls") or {}).get("spotify") or "").strip()
                    if not link or link in seen:
                        continue
                    seen.add(link)
                    items.append(
                        Item(
                            exec_id=exec_obj.id,
                            title=_clean(row.get("name")),
                            url=link,
                            origin=self.name,
                            # Search returns simplified episodes with no show,
                            # and /v1/episodes is 403 for app-only auth, so the
                            # platform is the best attribution available.
                            publisher=_clean((row.get("show") or {}).get("name")) or "Spotify",
                            published=_parse_date(row.get("release_date")),
                            summary=_clean(row.get("description")),
                            media_type="podcast",
                            duration_seconds=_millis_to_seconds(row.get("duration_ms")),
                            thumbnail=_pick_spotify_image(row.get("images")),
                        )
                    )
                    collected += 1

                if len(rows) < self.PAGE_SIZE or collected >= limit:
                    break
                time.sleep(0.3)
            time.sleep(0.4)

        return items[:limit]


def parse_iso8601_duration(value: str) -> int | None:
    """'PT1H2M3S' -> seconds."""
    match = re.fullmatch(
        r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", (value or "").strip()
    )
    if not match:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


class YouTubeSource(Source):
    """YouTube Data API - video interviews. Skipped unless YOUTUBE_API_KEY is set.

    YouTube is the noisiest source by far: clip channels re-cut other people's
    interviews into shorts and reaction videos, all of which name the executive
    in the title. We drop anything too short to be an actual interview before
    it ever reaches classification.
    """

    name = "youtube"

    def __init__(self, api_key: str, min_duration_seconds: int = 600,
                 channels: list[str] | None = None):
        self.api_key = api_key
        self.min_duration_seconds = min_duration_seconds
        self.channels = [c.lower() for c in (channels or [])]

    def _attach_durations(self, items: list[Item]) -> None:
        """One videos.list call per 50 videos - costs 1 quota unit, vs 100 for a search."""
        for start in range(0, len(items), 50):
            batch = items[start : start + 50]
            ids = ",".join(i.url.rsplit("=", 1)[-1] for i in batch)
            url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(
                {"key": self.api_key, "part": "contentDetails", "id": ids}
            )
            try:
                payload = json.loads(_fetch(url).decode("utf-8", "replace"))
            except (FetchError, json.JSONDecodeError):
                return  # leave durations unknown; the length filter then abstains
            durations = {
                row.get("id"): parse_iso8601_duration(
                    (row.get("contentDetails") or {}).get("duration", "")
                )
                for row in payload.get("items", [])
            }
            for item in batch:
                item.duration_seconds = durations.get(item.url.rsplit("=", 1)[-1])

    def search(self, exec_obj, *, days: int, limit: int) -> list[Item]:
        published_after = (
            datetime.now(timezone.utc).timestamp() - days * 86400
        )
        after_iso = datetime.fromtimestamp(published_after, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        query = f"{exec_obj.name} interview"
        url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(
            {
                "key": self.api_key,
                "part": "snippet",
                "q": query,
                "type": "video",
                "order": "date",
                "maxResults": min(limit, 50),
                "publishedAfter": after_iso,
            }
        )
        payload = json.loads(_fetch(url).decode("utf-8", "replace"))
        items: list[Item] = []
        for row in payload.get("items", []):
            video_id = (row.get("id") or {}).get("videoId")
            snippet = row.get("snippet") or {}
            if not video_id:
                continue
            items.append(
                Item(
                    exec_id=exec_obj.id,
                    title=_clean(snippet.get("title")),
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    origin=self.name,
                    publisher=_clean(snippet.get("channelTitle")),
                    published=_parse_date(snippet.get("publishedAt")),
                    summary=_clean(snippet.get("description")),
                    media_type="video",
                    thumbnail=(
                        ((snippet.get("thumbnails") or {}).get("high")
                         or (snippet.get("thumbnails") or {}).get("medium")
                         or (snippet.get("thumbnails") or {}).get("default")
                         or {}).get("url", "")
                    ),
                )
            )

        if self.channels:
            items = [
                i for i in items
                if any(c in i.publisher.lower() for c in self.channels)
            ]

        self._attach_durations(items)
        kept = []
        for item in items:
            if "#shorts" in item.title.lower():
                continue
            # Unknown duration abstains rather than rejects; classification still
            # has to find a real interview cue.
            if item.duration_seconds is not None and item.duration_seconds < self.min_duration_seconds:
                continue
            kept.append(item)
        return kept


def build_sources(
    enabled: list[str] | None = None,
    *,
    min_video_seconds: int = 600,
    youtube_channels: list[str] | None = None,
) -> list[Source]:
    """Instantiate every source that is available in this environment."""
    available: list[Source] = [
        GoogleNewsSource(),
        BingNewsSource(),
        ApplePodcastSource(),
    ]
    spotify_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    spotify_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if spotify_id and spotify_secret:
        available.append(SpotifySource(spotify_id, spotify_secret))

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if api_key:
        available.append(
            YouTubeSource(
                api_key,
                min_duration_seconds=min_video_seconds,
                channels=youtube_channels,
            )
        )

    if enabled:
        wanted = {name.strip().lower() for name in enabled}
        available = [s for s in available if s.name in wanted]
    return available
