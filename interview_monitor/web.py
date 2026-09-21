"""Local web UI for ad-hoc interview search.

Serves a single page plus a small JSON API, backed by the same crawlers and
classifier the scheduled monitor uses. Binds to localhost only.
"""

from __future__ import annotations

import json
import os
import traceback
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .monitor import search_person
from .sources import build_sources

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# label + why it might be unavailable, for the toggle strip
PLATFORMS = [
    {"id": "google-news", "label": "Google News", "kind": "article", "needs": None},
    {"id": "bing-news", "label": "Bing News", "kind": "article", "needs": None},
    {"id": "apple-podcasts", "label": "Apple Podcasts", "kind": "podcast", "needs": None},
    {"id": "spotify", "label": "Spotify", "kind": "podcast",
     "needs": "SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET"},
    {"id": "youtube", "label": "YouTube", "kind": "video", "needs": "YOUTUBE_API_KEY"},
]


def available_platforms() -> list[dict]:
    """Which sources this machine can actually run, and why not if it can't."""
    live = {s.name for s in build_sources()}
    return [{**p, "available": p["id"] in live} for p in PLATFORMS]


def _item_to_dict(item) -> dict:
    return {
        "title": item.title,
        "url": item.url,
        "publisher": item.publisher,
        "published": item.published.strftime("%Y-%m-%d") if item.published else "",
        "published_iso": item.published.isoformat() if item.published else "",
        "media_type": item.media_type,
        "duration_seconds": item.duration_seconds,
        "thumbnail": item.thumbnail,
        "confidence": item.score,
        "signals": item.signals,
        "source": item.origin,
        # Flagged, not filtered. An ad-hoc search consults no seen-database by
        # design and should not start hiding results either - the badge lets the
        # reader skip a generated episode without the page deciding for them.
        "slop_score": item.slop_score,
        "slop_reasons": item.slop_reasons,
        "probably_not_real": item.slop,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        """Quieter console: log API calls only.

        Must tolerate any argument types - log_error() routes through here with
        an HTTPStatus enum rather than the request line, and raising in a log
        call takes down the whole request thread.
        """
        try:
            line = fmt % args
        except Exception:
            line = " ".join(str(a) for a in args)
        if "/api/" in line:
            print(f"  {line}")

    # -- helpers ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _send_file(self, filename: str, content_type: str) -> None:
        path = WEB_DIR / filename
        if not path.exists():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, path.read_bytes(), content_type)

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._send_file("index.html", "text/html; charset=utf-8")
        elif route == "/app.css":
            self._send_file("app.css", "text/css; charset=utf-8")
        elif route == "/app.js":
            self._send_file("app.js", "application/javascript; charset=utf-8")
        elif route == "/api/platforms":
            self._send_json(200, {"platforms": available_platforms()})
        elif route == "/api/search":
            self._handle_search(urllib.parse.parse_qs(parsed.query))
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _handle_search(self, params: dict) -> None:
        query = (params.get("q") or [""])[0].strip()
        if not query:
            self._send_json(400, {"error": "Enter a person's name to search."})
            return

        company = (params.get("company") or [""])[0].strip()
        # Only the company matters here, so the flag is ignored without one.
        strict = (params.get("strict") or [""])[0].strip().lower() in ("1", "true", "on", "yes")
        company_match = "require" if (strict and company) else "boost"
        wanted = [s for s in (params.get("sources") or [""])[0].split(",") if s]
        try:
            days = max(1, min(int((params.get("days") or ["90"])[0]), 365))
        except ValueError:
            days = 90
        try:
            min_score = max(0, min(int((params.get("min_score") or ["3"])[0]), 10))
        except ValueError:
            min_score = 3

        live = {s.name for s in build_sources()}
        unavailable = [s for s in wanted if s not in live]
        sources = build_sources(wanted or None)
        if not sources:
            self._send_json(200, {
                "query": query, "count": 0, "results": [],
                "errors": [], "unavailable": unavailable,
                "note": "No platforms selected, or none of the selected ones are configured.",
            })
            return

        started = datetime.now(timezone.utc)
        try:
            items, errors = search_person(
                query, sources, company=company, company_match=company_match,
                days=days, min_score=min_score
            )
        except Exception:
            traceback.print_exc()
            self._send_json(500, {"error": "Search failed. See the server console."})
            return

        self._send_json(200, {
            "query": query,
            "company": company,
            "company_match": company_match,
            "count": len(items),
            "results": [_item_to_dict(i) for i in items],
            "errors": errors,
            "unavailable": unavailable,
            "searched": sorted(s.name for s in sources),
            "elapsed_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        })


class SearchServer(ThreadingHTTPServer):
    """HTTP server that refuses to share a port on Windows.

    http.server sets allow_reuse_address = 1, and on Windows SO_REUSEADDR lets
    a second process bind a port another process is already listening on. Two
    servers then share the port and requests go to whichever won the race -
    which looks like the running server ignoring your changes or your
    credentials. Fail loudly instead. On POSIX, reuse is kept because there it
    only bypasses TIME_WAIT, which is what you want when restarting.
    """

    allow_reuse_address = os.name != "nt"


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    try:
        server = SearchServer((host, port), Handler)
    except OSError as exc:
        print(f"Cannot bind {host}:{port} - {exc}")
        print("Another server is already using this port. Stop it, or start")
        print(f"this one elsewhere:  python serve.py --port {port + 1}")
        raise SystemExit(1) from exc
    live = [p["label"] for p in available_platforms() if p["available"]]
    off = [p["label"] for p in available_platforms() if not p["available"]]
    print(f"Interview search running at http://{host}:{port}")
    print(f"  platforms available: {', '.join(live)}")
    if off:
        print(f"  needs credentials:   {', '.join(off)}")
    print("  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    serve(port=int(os.environ.get("PORT", "8765")))
