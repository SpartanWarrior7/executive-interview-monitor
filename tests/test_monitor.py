"""Offline tests - no network. Run: python -m unittest discover tests"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interview_monitor.classify import classify, mentions  # noqa: E402
from interview_monitor.config import (  # noqa: E402
    Executive, Config, Settings, company_core, load_config, parse_person_line,
)
from interview_monitor.monitor import run  # noqa: E402
from interview_monitor.report import render_json, render_text  # noqa: E402
from interview_monitor.sources import Item, Source  # noqa: E402
from interview_monitor.store import Store, normalize_title, normalize_url  # noqa: E402

JENSEN = Executive(id="jensen-huang", name="Jensen Huang", company="Nvidia",
                   title="CEO", aliases=["Jen-Hsun Huang"])
NOW = datetime.now(timezone.utc)


def make_item(title: str, summary: str = "", *, url: str = "https://example.com/a",
              media_type: str = "article", published: datetime | None = None) -> Item:
    return Item(exec_id=JENSEN.id, title=title, url=url, origin="test",
                publisher="Example", summary=summary, media_type=media_type,
                published=published or NOW)


class TestNameMatching(unittest.TestCase):
    def test_matches_plain_name(self):
        self.assertEqual(mentions("A chat with Jensen Huang", JENSEN.all_names), "Jensen Huang")

    def test_matches_alias(self):
        self.assertEqual(mentions("Jen-Hsun Huang speaks", JENSEN.all_names), "Jen-Hsun Huang")

    def test_ignores_company_only_mention(self):
        self.assertIsNone(mentions("Nvidia stock rises on earnings", JENSEN.all_names))

    def test_tolerates_accents_and_spacing(self):
        exec_obj = Executive(id="x", name="Jose Munoz")
        self.assertIsNotNone(mentions("An interview with José  Muñoz", exec_obj.all_names))

    def test_requires_full_name_not_surname(self):
        self.assertIsNone(mentions("Huang said Tuesday", JENSEN.all_names))


class TestClassification(unittest.TestCase):
    def _score(self, item: Item) -> int:
        _, score, _ = classify(item, JENSEN)
        return score

    def test_explicit_interview_passes(self):
        item = make_item("Axios interview: Jensen Huang on AI")
        self.assertGreaterEqual(self._score(item), 3)

    def test_podcast_episode_passes(self):
        item = make_item("Jensen Huang: The Mindset That Built Nvidia",
                         media_type="podcast")
        self.assertGreaterEqual(self._score(item), 3)

    def test_news_roundup_podcast_is_rejected(self):
        # Mentions the exec only in the description, as roundup shows do.
        item = make_item(
            "310 | 61% Believe AI Agents Could Do Half Their Job in 3 Years",
            "Plus Satya Nadella on the AI buildout, and more AI news.",
            media_type="podcast",
        )
        self.assertLess(self._score(item), 3)

    def test_video_alone_is_not_enough(self):
        # A clip channel naming the exec but with no interview cue anywhere.
        item = make_item("Jensen Huang's Rule: Do as Much as Needed",
                         media_type="video")
        self.assertLess(self._score(item), 3)

    def test_video_with_interview_cue_passes(self):
        item = make_item("Jensen Huang interview: the full conversation",
                         media_type="video")
        self.assertGreaterEqual(self._score(item), 3)

    def test_sits_down_with_passes(self):
        item = make_item("Nvidia's Jensen Huang sits down with our editor")
        self.assertGreaterEqual(self._score(item), 3)

    def test_deal_talks_is_not_an_interview(self):
        item = make_item(
            "Nvidia in talks to back $500B OpenAI data center - days after "
            "CEO Jensen Huang voiced support"
        )
        self.assertLess(self._score(item), 3)

    def test_plain_reportage_is_rejected(self):
        item = make_item("Jensen Huang reveals Nvidia's next chip at a keynote")
        self.assertLess(self._score(item), 3)

    def test_job_interview_is_rejected(self):
        item = make_item("Jensen Huang on the job interview question he always asks")
        self.assertLess(self._score(item), 3)

    def test_wrong_person_is_rejected(self):
        named, score, _ = classify(make_item("An interview with Lisa Su"), JENSEN)
        self.assertFalse(named)
        self.assertEqual(score, 0)

    def test_signal_in_body_counts_less_than_in_title(self):
        in_title = self._score(make_item("Jensen Huang interview on AI"))
        in_body = self._score(make_item("Jensen Huang on AI", "A wide-ranging interview."))
        self.assertGreater(in_title, in_body)


class TestPersonShorthand(unittest.TestCase):
    def test_plain_name(self):
        self.assertEqual(parse_person_line("Jensen Huang"), {"name": "Jensen Huang"})

    def test_parenthesised_company(self):
        self.assertEqual(
            parse_person_line("Jensen Huang (Nvidia)"),
            {"company": "Nvidia", "name": "Jensen Huang"},
        )

    def test_parenthesised_title_and_company(self):
        entry = parse_person_line("Jensen Huang (CEO, Nvidia)")
        self.assertEqual(entry["name"], "Jensen Huang")
        self.assertEqual(entry["title"], "CEO")
        self.assertEqual(entry["company"], "Nvidia")

    def test_comma_company(self):
        self.assertEqual(
            parse_person_line("Jensen Huang, Nvidia"),
            {"company": "Nvidia", "name": "Jensen Huang"},
        )

    def test_comma_title_and_company(self):
        entry = parse_person_line("Sam Director, CFO, Umbrella Corporation")
        self.assertEqual(entry["name"], "Sam Director")
        self.assertEqual(entry["title"], "CFO")
        self.assertEqual(entry["company"], "Umbrella Corporation")

    def test_dash_and_pipe_separators(self):
        for text in ("Alex Founder - Hooli", "Alex Founder | Hooli", "Alex Founder — Hooli"):
            with self.subTest(text=text):
                entry = parse_person_line(text)
                self.assertEqual(entry["name"], "Alex Founder")
                self.assertEqual(entry["company"], "Hooli")

    def test_trailing_title_alone_is_not_a_company(self):
        entry = parse_person_line("Jane Doe, CEO")
        self.assertEqual(entry["name"], "Jane Doe")
        self.assertEqual(entry.get("title"), "CEO")
        self.assertNotIn("company", entry)

    def test_hyphenated_surname_survives(self):
        # No spaces around the hyphen, so it is part of the name.
        self.assertEqual(
            parse_person_line("Jane Smith-Executive"), {"name": "Jane Smith-Executive"}
        )

    def test_company_core_strips_legal_suffix(self):
        self.assertEqual(company_core("Nvidia Corporation"), "Nvidia")
        self.assertEqual(company_core("Acme Corp."), "Acme")
        self.assertEqual(company_core("Globex, Inc."), "Globex")
        self.assertEqual(company_core("Microsoft"), "Microsoft")


class TestCompanyDisambiguation(unittest.TestCase):
    """A common name matches the wrong person; the company is the tiebreak."""

    COMMON = Executive(id="michael-brown", name="Michael Brown",
                       company="Initech", company_match="require")

    def _classify(self, exec_obj, title, summary=""):
        item = Item(exec_id=exec_obj.id, title=title, url="https://example.com/a",
                    origin="test", publisher="Example", summary=summary,
                    media_type="article", published=NOW)
        return classify(item, exec_obj)

    def test_company_mention_lifts_confidence(self):
        with_company = self._classify(JENSEN, "Nvidia's Jensen Huang interview on AI")
        without = self._classify(JENSEN, "Jensen Huang interview on AI")
        self.assertGreater(with_company[1], without[1])
        self.assertIn("company-in-title", with_company[2])

    def test_company_in_body_counts_less_than_in_title(self):
        in_title = self._classify(JENSEN, "Nvidia's Jensen Huang interview on AI")
        in_body = self._classify(JENSEN, "Jensen Huang interview on AI",
                                 "The Nvidia chief on the buildout.")
        self.assertGreater(in_title[1], in_body[1])
        self.assertIn("company~", in_body[2])

    def test_company_alias_counts(self):
        exec_obj = Executive(id="x", name="Jensen Huang", company="Nvidia",
                             company_aliases=["NVDA"])
        named, score, labels = self._classify(exec_obj, "Jensen Huang interview: NVDA at $5T")
        self.assertIn("company-in-title", labels)

    def test_legal_suffix_does_not_block_the_match(self):
        exec_obj = Executive(id="x", name="Jensen Huang", company="Nvidia Corporation")
        _, _, labels = self._classify(exec_obj, "Nvidia's Jensen Huang interview on AI")
        self.assertIn("company-in-title", labels)

    def test_require_drops_the_other_person_of_the_same_name(self):
        # A real interview, real interview language - wrong Michael Brown.
        named, score, labels = self._classify(
            self.COMMON, "Michael Brown interview: the Fed's next move",
            "The Barclays economist sits down with us.",
        )
        self.assertTrue(named)  # the name did appear
        self.assertEqual(score, 0)  # but it is not our man
        self.assertIn("company-missing", labels)

    def test_require_keeps_the_right_person(self):
        _, score, labels = self._classify(
            self.COMMON, "Michael Brown interview: inside Initech's turnaround"
        )
        self.assertGreaterEqual(score, 3)
        self.assertIn("company-match", labels)

    def test_require_gates_without_inflating_the_score(self):
        """The gate must not double as a bonus, or min_score quietly drops."""
        gated = Executive(id="x", name="Jensen Huang", company="Nvidia",
                          company_match="require")
        ignored = Executive(id="x", name="Jensen Huang", company="Nvidia",
                            company_match="off")
        title = "Nvidia's Jensen Huang interview on AI"
        self.assertEqual(self._classify(gated, title)[1], self._classify(ignored, title)[1])

    def test_boost_mode_keeps_items_with_no_company_mention(self):
        exec_obj = Executive(id="x", name="Michael Brown", company="Initech")
        _, score, _ = self._classify(exec_obj, "Michael Brown interview on the economy")
        self.assertGreaterEqual(score, 3)

    def test_off_mode_ignores_the_company_entirely(self):
        exec_obj = Executive(id="x", name="Jensen Huang", company="Nvidia",
                             company_match="off")
        _, _, labels = self._classify(exec_obj, "Nvidia's Jensen Huang interview on AI")
        self.assertNotIn("company-in-title", labels)

    def test_query_narrows_to_the_company(self):
        from interview_monitor.sources import _build_query
        query = _build_query(JENSEN, ["interview"])
        self.assertIn('"Nvidia"', query)
        # Aliases used to suppress the company hint entirely.
        self.assertIn('"Jen-Hsun Huang"', query)


class TestCompanyConfigLoading(unittest.TestCase):
    def _load(self, payload: dict):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execs.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_config(path)

    def test_shorthand_entry_carries_the_company(self):
        cfg = self._load({"executives": ["Pat Chairperson (Soylent)"]})
        self.assertEqual(cfg.executives[0].name, "Pat Chairperson")
        self.assertEqual(cfg.executives[0].company, "Soylent")

    def test_settings_default_is_inherited(self):
        cfg = self._load({
            "settings": {"company_match": "require"},
            "executives": [{"name": "Michael Brown", "company": "Initech"}],
        })
        self.assertEqual(cfg.executives[0].company_match, "require")

    def test_per_person_override_beats_the_default(self):
        cfg = self._load({
            "settings": {"company_match": "require"},
            "executives": [{"name": "Jane Doe", "company": "Acme",
                            "company_match": "boost"}],
        })
        self.assertEqual(cfg.executives[0].company_match, "boost")

    def test_global_require_cannot_filter_someone_with_no_company(self):
        cfg = self._load({
            "settings": {"company_match": "require"},
            "executives": ["Pat Chairperson"],
        })
        self.assertEqual(cfg.executives[0].company_match, "off")

    def test_explicit_require_without_a_company_is_an_error(self):
        with self.assertRaises(ValueError):
            self._load({"executives": [{"name": "Jane Doe",
                                        "company_match": "require"}]})

    def test_unknown_mode_is_an_error(self):
        with self.assertRaises(ValueError):
            self._load({"executives": [{"name": "Jane Doe", "company": "Acme",
                                        "company_match": "strict"}]})


class TestNormalization(unittest.TestCase):
    def test_strips_tracking_params_and_www(self):
        a = normalize_url("https://www.axios.com/2026/07/23/story?utm_source=x&fbclid=y")
        b = normalize_url("https://axios.com/2026/07/23/story/")
        self.assertEqual(a, b)

    def test_keeps_meaningful_params(self):
        a = normalize_url("https://youtube.com/watch?v=abc")
        b = normalize_url("https://youtube.com/watch?v=def")
        self.assertNotEqual(a, b)

    def test_title_normalization_drops_stopwords_and_case(self):
        self.assertEqual(
            normalize_title("The Interview With Jensen Huang!"),
            normalize_title("interview with jensen huang"),
        )

    def test_outlet_suffixes_are_stripped(self):
        self.assertEqual(
            normalize_title("On GPS: Exclusive interview with CEO Satya Nadella "
                            "| CNN Business - CNN"),
            normalize_title("On GPS: Exclusive interview with CEO Satya Nadella"),
        )

    def test_long_subtitle_is_not_mistaken_for_an_outlet_tag(self):
        kept = normalize_title(
            "Jensen Huang interview - why he thinks the AI bubble talk is wrong"
        )
        self.assertIn("bubble", kept)

    def test_iso8601_durations(self):
        from interview_monitor.sources import parse_iso8601_duration
        self.assertEqual(parse_iso8601_duration("PT45S"), 45)
        self.assertEqual(parse_iso8601_duration("PT12M30S"), 750)
        self.assertEqual(parse_iso8601_duration("PT1H2M3S"), 3723)
        self.assertIsNone(parse_iso8601_duration("garbage"))

    def test_bing_redirect_is_unwrapped(self):
        from interview_monitor.sources import unwrap_bing_url
        wrapped = ("http://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&tid=abc"
                   "&url=https%3a%2f%2fwww.cnn.com%2fstory&c=1&mkt=en-us")
        self.assertEqual(unwrap_bing_url(wrapped), "https://www.cnn.com/story")


class FakeSource(Source):
    name = "fake"

    def __init__(self, items: list[Item]):
        self.items = items
        self.calls = 0

    def search(self, exec_obj, *, days: int, limit: int) -> list[Item]:
        self.calls += 1
        return list(self.items)


class BrokenSource(Source):
    name = "broken"

    def search(self, exec_obj, *, days: int, limit: int) -> list[Item]:
        raise RuntimeError("upstream is down")


def make_config() -> Config:
    return Config(executives=[JENSEN], settings=Settings(lookback_days=7, min_score=3,
                                                         max_results_per_source=25))


class TestSpotifyParsing(unittest.TestCase):
    """Parses a recorded response shape - no network, no credentials."""

    def _parse(self, payload: dict) -> list[Item]:
        from interview_monitor.sources import SpotifySource
        src = SpotifySource("id", "secret")
        src._token = "fake"
        src._token_expires_at = 9e18  # skip the auth call

        import interview_monitor.sources as mod
        original = mod._fetch
        mod._fetch = lambda *a, **k: json.dumps(payload).encode()
        try:
            return src.search(JENSEN, days=7, limit=25)
        finally:
            mod._fetch = original

    def test_parses_episode_fields(self):
        items = self._parse({"episodes": {"items": [{
            "id": "abc123",
            "name": "Jensen Huang on the future of compute",
            "description": "A long conversation with the Nvidia CEO.",
            "release_date": "2026-07-25",
            "duration_ms": 3_600_000,
            "external_urls": {"spotify": "https://open.spotify.com/episode/abc123"},
            "show": {"name": "Acquired"},
        }]}})
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.title, "Jensen Huang on the future of compute")
        self.assertEqual(item.url, "https://open.spotify.com/episode/abc123")
        self.assertEqual(item.publisher, "Acquired")
        self.assertEqual(item.media_type, "podcast")
        self.assertEqual(item.duration_seconds, 3600)
        self.assertEqual(item.published.strftime("%Y-%m-%d"), "2026-07-25")

    def test_survives_null_padding(self):
        # Spotify pads the items array with nulls for unavailable episodes.
        items = self._parse({"episodes": {"items": [
            None,
            {"id": "x", "name": "Jensen Huang interview", "description": "",
             "release_date": "2026-07-25", "duration_ms": 1800000,
             "external_urls": {"spotify": "https://open.spotify.com/episode/x"}},
            None,
        ]}})
        self.assertEqual(len(items), 1)

    def test_handles_empty_and_missing_payloads(self):
        self.assertEqual(self._parse({"episodes": {"items": []}}), [])
        self.assertEqual(self._parse({"episodes": None}), [])
        self.assertEqual(self._parse({}), [])

    def test_spotify_episode_classifies_as_interview(self):
        items = self._parse({"episodes": {"items": [{
            "id": "abc", "name": "Jensen Huang on building Nvidia",
            "description": "A wide-ranging conversation.",
            "release_date": "2026-07-25", "duration_ms": 3600000,
            "external_urls": {"spotify": "https://open.spotify.com/episode/abc"},
            "show": {"name": "Invest Like the Best"},
        }]}})
        _, score, _ = classify(items[0], JENSEN)
        self.assertGreaterEqual(score, 3)


OTHER = Executive(id="lisa-su", name="Lisa Su", company="AMD", title="CEO")


class TestDigest(unittest.TestCase):
    """The digest must account for every tracked executive, not just the hits."""

    def _result(self, items):
        from interview_monitor.monitor import RunResult
        return RunResult(new_items=items, candidates=50, executives=[JENSEN, OTHER])

    def test_every_executive_appears_even_with_no_hits(self):
        from interview_monitor.report import render_digest_text
        text = render_digest_text(self._result([make_item("Jensen Huang interview")]))
        self.assertIn("Jensen Huang", text)
        self.assertIn("Lisa Su", text)

    def test_quiet_executive_uses_the_required_wording(self):
        from interview_monitor.report import render_digest_text
        text = render_digest_text(self._result([]))
        self.assertIn("Jensen Huang (CEO, Nvidia): No new interviews", text)
        self.assertIn("Lisa Su (CEO, AMD): No new interviews", text)

    def test_executive_with_hits_lists_title_and_url(self):
        from interview_monitor.report import render_digest_text
        item = make_item("Jensen Huang interview", url="https://axios.com/story")
        text = render_digest_text(self._result([item]))
        self.assertIn("Jensen Huang interview", text)
        self.assertIn("https://axios.com/story", text)
        self.assertNotIn("Jensen Huang (CEO, Nvidia): No new interviews", text)

    def test_subject_reflects_findings(self):
        from interview_monitor.report import digest_subject
        self.assertEqual(digest_subject(self._result([])),
                         "Interview digest: no new interviews")
        one = digest_subject(self._result([make_item("Jensen Huang interview")]))
        self.assertIn("1 new interview", one)

    def test_html_escapes_and_links(self):
        from interview_monitor.report import render_digest_html
        item = make_item('Jensen Huang & the "AI" <boom>', url="https://a.com/x")
        html = render_digest_html(self._result([item]))
        self.assertIn("&amp;", html)
        self.assertIn("&lt;boom&gt;", html)
        self.assertNotIn("<boom>", html)
        self.assertIn('href="https://a.com/x"', html)
        self.assertIn("Lisa Su", html)  # quiet executive still listed


class TestGraphMailer(unittest.TestCase):
    ENV = ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
           "EMAIL_FROM", "EMAIL_TO", "EMAIL_BACKEND", "SMTP_HOST")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def _configure(self):
        os.environ.update({
            "GRAPH_TENANT_ID": "tenant-abc", "GRAPH_CLIENT_ID": "client-abc",
            "GRAPH_CLIENT_SECRET": "secret-abc",
            "EMAIL_FROM": "Interview-Monitor@example.org",
            "EMAIL_TO": "you@example.com",
        })

    def test_graph_selected_when_its_vars_are_present(self):
        from interview_monitor.mailer import choose_backend
        self._configure()
        self.assertEqual(choose_backend(), "graph")

    def test_smtp_selected_when_only_smtp_configured(self):
        from interview_monitor.mailer import choose_backend
        os.environ["SMTP_HOST"] = "smtp.example.com"
        self.assertEqual(choose_backend(), "smtp")

    def test_explicit_backend_overrides_detection(self):
        from interview_monitor.mailer import choose_backend
        self._configure()
        os.environ["EMAIL_BACKEND"] = "smtp"
        self.assertEqual(choose_backend(), "smtp")

    def test_no_config_at_all_is_an_error_naming_both_options(self):
        from interview_monitor.mailer import MailError, choose_backend
        with self.assertRaises(MailError) as ctx:
            choose_backend()
        self.assertIn("GRAPH_TENANT_ID", str(ctx.exception))
        self.assertIn("SMTP_HOST", str(ctx.exception))

    def test_missing_graph_secret_is_named(self):
        from interview_monitor.mailer import GraphConfig, MailError
        self._configure()
        os.environ.pop("GRAPH_CLIENT_SECRET")
        with self.assertRaises(MailError) as ctx:
            GraphConfig.from_env()
        self.assertIn("GRAPH_CLIENT_SECRET", str(ctx.exception))

    def test_payload_shape_matches_graph_sendmail(self):
        from interview_monitor.mailer import GraphConfig, build_graph_payload
        self._configure()
        os.environ["EMAIL_TO"] = "you@example.com, ops@example.org"
        payload = build_graph_payload("Subj", "plain", "<p>rich</p>", GraphConfig.from_env())
        msg = payload["message"]
        self.assertEqual(msg["subject"], "Subj")
        self.assertEqual(msg["body"]["contentType"], "HTML")
        self.assertEqual(msg["body"]["content"], "<p>rich</p>")
        self.assertEqual(msg["from"]["emailAddress"]["address"],
                         "Interview-Monitor@example.org")
        self.assertEqual([r["emailAddress"]["address"] for r in msg["toRecipients"]],
                         ["you@example.com", "ops@example.org"])
        self.assertFalse(payload["saveToSentItems"])

    def test_payload_falls_back_to_text_when_no_html(self):
        from interview_monitor.mailer import GraphConfig, build_graph_payload
        self._configure()
        payload = build_graph_payload("S", "plain only", "", GraphConfig.from_env())
        self.assertEqual(payload["message"]["body"]["contentType"], "Text")
        self.assertEqual(payload["message"]["body"]["content"], "plain only")

    def test_permission_error_explains_the_fix(self):
        from interview_monitor.mailer import _explain_graph_error
        body = b'{"error":{"code":"ErrorAccessDenied","message":"Access is denied."}}'
        msg = _explain_graph_error(403, body)
        self.assertIn("Application RBAC", msg)
        self.assertIn("Test-ServicePrincipalAuthorization", msg)

    def test_expired_secret_error_explains_the_fix(self):
        from interview_monitor.mailer import _explain_graph_error
        body = b'{"error":"invalid_client","error_description":"AADSTS7000215: Invalid client secret"}'
        msg = _explain_graph_error(401, body)
        self.assertIn("GRAPH_CLIENT_SECRET", msg)

    def test_unknown_mailbox_error_explains_the_fix(self):
        from interview_monitor.mailer import _explain_graph_error
        body = b'{"error":{"code":"ErrorInvalidUser","message":"not found"}}'
        self.assertIn("mailbox", _explain_graph_error(404, body))


class TestMailer(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
                        "SMTP_SECURITY", "EMAIL_FROM", "EMAIL_TO")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_config_names_what_is_missing(self):
        from interview_monitor.mailer import MailConfig, MailError
        with self.assertRaises(MailError) as ctx:
            MailConfig.from_env()
        self.assertIn("SMTP_HOST", str(ctx.exception))

    def test_parses_recipients_and_defaults(self):
        from interview_monitor.mailer import MailConfig
        os.environ.update({"SMTP_HOST": "smtp.example.com",
                           "SMTP_USER": "bot@example.com",
                           "EMAIL_TO": "a@x.com, b@y.com;c@z.com"})
        cfg = MailConfig.from_env()
        self.assertEqual(cfg.recipients, ["a@x.com", "b@y.com", "c@z.com"])
        self.assertEqual(cfg.sender, "bot@example.com")  # falls back to SMTP_USER
        self.assertEqual(cfg.port, 587)                  # starttls default

    def test_ssl_security_defaults_to_465(self):
        from interview_monitor.mailer import MailConfig
        os.environ.update({"SMTP_HOST": "smtp.example.com", "SMTP_USER": "u@x.com",
                           "EMAIL_TO": "a@x.com", "SMTP_SECURITY": "ssl"})
        self.assertEqual(MailConfig.from_env().port, 465)

    def test_rejects_unknown_security(self):
        from interview_monitor.mailer import MailConfig, MailError
        os.environ.update({"SMTP_HOST": "s", "SMTP_USER": "u@x.com",
                           "EMAIL_TO": "a@x.com", "SMTP_SECURITY": "banana"})
        with self.assertRaises(MailError):
            MailConfig.from_env()

    def test_message_is_multipart_with_both_bodies(self):
        from interview_monitor.mailer import MailConfig, build_message
        os.environ.update({"SMTP_HOST": "s", "SMTP_USER": "bot@x.com",
                           "EMAIL_TO": "a@x.com,b@x.com"})
        msg = build_message("Subj", "plain body", "<p>html body</p>", MailConfig.from_env())
        self.assertEqual(msg["Subject"], "Subj")
        self.assertEqual(msg["To"], "a@x.com, b@x.com")
        self.assertTrue(msg.is_multipart())
        types = {p.get_content_type() for p in msg.walk()}
        self.assertIn("text/plain", types)
        self.assertIn("text/html", types)


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "seen.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, sources, **kw):
        with Store(self.db) as store:
            return run(make_config(), sources, store, resolve=False, **kw)

    def test_reports_new_interview_then_stays_quiet(self):
        src = FakeSource([make_item("Exclusive interview with Jensen Huang")])
        first = self._run([src])
        self.assertEqual(len(first.new_items), 1)

        second = self._run([src])
        self.assertEqual(second.new_items, [])
        self.assertIn("No new interviews.", render_text(second))

    def test_syndicated_copy_reported_once(self):
        src = FakeSource([
            make_item("Interview with Jensen Huang", url="https://a.com/1"),
            make_item("Interview with Jensen Huang", url="https://b.com/2"),
        ])
        result = self._run([src])
        self.assertEqual(len(result.new_items), 1)

    def test_same_url_with_tracking_params_reported_once(self):
        src = FakeSource([
            make_item("Interview with Jensen Huang on chips", url="https://a.com/1"),
            make_item("A different headline entirely about Nvidia interview Jensen Huang",
                      url="https://a.com/1?utm_source=news"),
        ])
        result = self._run([src])
        self.assertEqual(len(result.new_items), 1)

    def test_old_items_are_ignored(self):
        stale = make_item("Interview with Jensen Huang",
                          published=NOW - timedelta(days=45))
        result = self._run([FakeSource([stale])])
        self.assertEqual(result.new_items, [])

    def test_seed_baselines_without_reporting(self):
        src = FakeSource([make_item("Interview with Jensen Huang")])
        seeded = self._run([src], seed=True)
        self.assertEqual(seeded.new_items, [])
        self.assertEqual(seeded.seeded, 1)

        after = self._run([src])
        self.assertEqual(after.new_items, [])

    def test_dry_run_does_not_persist(self):
        src = FakeSource([make_item("Interview with Jensen Huang")])
        self.assertEqual(len(self._run([src], dry_run=True).new_items), 1)
        self.assertEqual(len(self._run([src], dry_run=True).new_items), 1)

    def test_broken_source_does_not_stop_the_run(self):
        good = FakeSource([make_item("Interview with Jensen Huang")])
        result = self._run([BrokenSource(), good])
        self.assertEqual(len(result.new_items), 1)
        self.assertEqual(len(result.errors), 1)

    def test_reports_contain_name_and_link(self):
        url = "https://axios.com/2026/07/23/jensen"
        result = self._run([FakeSource([make_item("Interview with Jensen Huang", url=url)])])
        text = render_text(result)
        self.assertIn("Jensen Huang", text)
        self.assertIn(url, text)

        payload = render_json(result)
        self.assertIn('"status": "new_interviews"', payload)
        self.assertIn(url, payload)

    def test_empty_run_json_status(self):
        result = self._run([FakeSource([])])
        self.assertIn('"status": "no_new_interviews"', render_json(result))


SAMPLE_WATCHLIST = {
    "_comment": "hand-written note that must survive being rewritten",
    "settings": {"lookback_days": 7, "min_score": 3, "youtube_channels": ["UCabc"]},
    "executives": [
        {"id": "jensen-huang", "name": "Jensen Huang", "company": "Nvidia",
         "title": "CEO", "aliases": ["Jen-Hsun Huang"]},
        {"id": "satya-nadella", "name": "Satya Nadella", "company": "Microsoft",
         "title": "CEO"},
    ],
}


class TestLoadConfig(unittest.TestCase):
    """The watchlist parser had no tests, and the email channel now writes it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "executives.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, text: str, *, encoding: str = "utf-8"):
        from interview_monitor.config import load_config
        self.path.write_text(text, encoding=encoding)
        return load_config(self.path)

    def test_survives_a_notepad_byte_order_mark(self):
        config = self._load(json.dumps(SAMPLE_WATCHLIST), encoding="utf-8-sig")
        self.assertEqual(len(config.executives), 2)
        self.assertEqual(config.settings.youtube_channels, ["UCabc"])

    def test_accepts_a_bare_list(self):
        config = self._load(json.dumps([{"name": "Pat Chairperson"}]))
        self.assertEqual(config.executives[0].id, "pat-chairperson")
        self.assertEqual(config.settings.lookback_days, 7)  # defaults still apply

    def test_accepts_a_bare_string_entry(self):
        config = self._load(json.dumps({"executives": ["Pat Chairperson"]}))
        self.assertEqual(config.executives[0].name, "Pat Chairperson")

    def test_duplicate_id_is_rejected(self):
        raw = {"executives": [{"name": "Tim Cook"}, {"id": "tim-cook", "name": "Timothy Cook"}]}
        with self.assertRaises(ValueError) as ctx:
            self._load(json.dumps(raw))
        self.assertIn("tim-cook", str(ctx.exception))

    def test_empty_list_is_rejected(self):
        with self.assertRaises(ValueError):
            self._load(json.dumps({"executives": []}))

    def test_missing_name_is_rejected(self):
        with self.assertRaises(ValueError):
            self._load(json.dumps({"executives": [{"company": "Acme"}]}))

    def test_missing_file_points_at_the_example(self):
        from interview_monitor.config import load_config
        with self.assertRaises(FileNotFoundError) as ctx:
            load_config(self.path)
        self.assertIn("executives.example.json", str(ctx.exception))


class TestWatchlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "executives.json"
        self.path.write_text(json.dumps(SAMPLE_WATCHLIST), encoding="utf-8")
        self._saved = os.environ.get("WATCHLIST_PATH")
        os.environ.pop("WATCHLIST_PATH", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("WATCHLIST_PATH", None)
        else:
            os.environ["WATCHLIST_PATH"] = self._saved
        self.tmp.cleanup()

    def _raw(self):
        from interview_monitor import watchlist
        return watchlist.load_raw(self.path)

    def test_add_appends_with_slug_id(self):
        from interview_monitor import watchlist
        new, message = watchlist.add(self._raw(), name="Tim Cook",
                                     company="Apple", title="CEO")
        self.assertIn("Tim Cook", message)
        self.assertEqual(new["executives"][-1],
                         {"id": "tim-cook", "name": "Tim Cook",
                          "company": "Apple", "title": "CEO"})

    def test_add_refuses_a_single_word_name(self):
        from interview_monitor import watchlist
        raw = self._raw()
        new, message = watchlist.add(raw, name="Cook")
        self.assertIs(new, raw)  # identity means "nothing happened"
        self.assertIn("full name", message)

    def test_add_is_idempotent_by_name_and_alias(self):
        from interview_monitor import watchlist
        raw = self._raw()
        for spelling in ("Jensen Huang", "jensen huang", "Jen-Hsun Huang"):
            new, message = watchlist.add(raw, name=spelling)
            self.assertIs(new, raw)
            self.assertIn("Already tracking", message)

    def test_remove_matches_name_alias_and_id(self):
        from interview_monitor import watchlist
        for target in ("Satya Nadella", "satya nadella", "satya-nadella"):
            new, message = watchlist.remove(self._raw(), target)
            self.assertEqual([e["name"] for e in new["executives"]], ["Jensen Huang"])
            self.assertIn("Stopped tracking", message)
        new, _ = watchlist.remove(self._raw(), "Jen-Hsun Huang")
        self.assertEqual([e["name"] for e in new["executives"]], ["Satya Nadella"])

    def test_remove_unknown_person_changes_nothing(self):
        from interview_monitor import watchlist
        raw = self._raw()
        new, message = watchlist.remove(raw, "Someone Else")
        self.assertIs(new, raw)
        self.assertIn("Not tracking", message)

    def test_remove_refuses_to_empty_the_list(self):
        from interview_monitor import watchlist
        raw, _ = watchlist.remove(self._raw(), "Jensen Huang")
        left, message = watchlist.remove(raw, "Satya Nadella")
        self.assertIs(left, raw)
        self.assertIn("cannot be empty", message)

    def test_rewriting_a_shorthand_list_keeps_the_company(self):
        """A bare string carries its own company, and an email command rewrites
        the whole file. Reading it as a plain name would leave a person whose
        name never matches a headline again - silently, months later."""
        from interview_monitor import watchlist
        from interview_monitor.config import load_config
        self.path.write_text(
            json.dumps({"executives": ["Pat Chairperson (Soylent)",
                                       "Michael Brown, Initech"]}),
            encoding="utf-8",
        )
        raw, _ = watchlist.add(watchlist.load_raw(self.path), name="Tim Cook",
                               company="Apple")
        watchlist.save(self.path, raw)

        after = {e.name: e.company for e in load_config(self.path).executives}
        self.assertEqual(after, {"Pat Chairperson": "Soylent",
                                 "Michael Brown": "Initech",
                                 "Tim Cook": "Apple"})

    def test_save_preserves_comments_and_settings(self):
        from interview_monitor import watchlist
        new, _ = watchlist.add(self._raw(), name="Tim Cook", company="Apple")
        watchlist.save(self.path, new)

        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written["_comment"], SAMPLE_WATCHLIST["_comment"])
        self.assertEqual(written["settings"], SAMPLE_WATCHLIST["settings"])
        self.assertEqual(len(written["executives"]), 3)

    def test_save_leaves_the_previous_version_as_bak(self):
        from interview_monitor import watchlist
        before = self.path.read_text(encoding="utf-8")
        new, _ = watchlist.add(self._raw(), name="Tim Cook")
        watchlist.save(self.path, new)
        backup = self.path.with_name(self.path.name + ".bak")
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), before)

    def test_save_refuses_an_invalid_list_and_leaves_the_file_alone(self):
        from interview_monitor import watchlist
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(watchlist.WatchlistError):
            watchlist.save(self.path, {"executives": []})
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_resolve_path_precedence(self):
        from interview_monitor import watchlist
        self.assertEqual(watchlist.resolve_path("explicit.json"), Path("explicit.json"))
        os.environ["WATCHLIST_PATH"] = str(self.path)
        self.assertEqual(watchlist.resolve_path(), self.path)
        self.assertEqual(watchlist.resolve_path("explicit.json"), Path("explicit.json"))
        os.environ.pop("WATCHLIST_PATH")
        self.assertEqual(watchlist.resolve_path().name, "executives.json")


class TestCommandParsing(unittest.TestCase):
    def _verbs(self, subject: str, body: str):
        from interview_monitor.commands import parse
        commands, unparsed = parse(subject, body)
        return [(c.verb, c.argument) for c in commands], unparsed

    def test_gmail_quoting_is_dropped(self):
        commands, _ = self._verbs("RE: Interview digest", (
            "remove Jensen Huang\n"
            "\n"
            "On Wed, 29 Jul 2026 at 07:31, alerts wrote:\n"
            "> add Somebody Else\n"
        ))
        self.assertEqual(commands, [("remove", "Jensen Huang")])

    def test_outlook_quoting_is_dropped(self):
        commands, _ = self._verbs("RE: digest", (
            "add Lisa Su, AMD CEO\n"
            "\n"
            "-----Original Message-----\n"
            "From: alerts <alerts@example.org>\n"
            "add Somebody Else\n"
        ))
        self.assertEqual(commands, [("add", "Lisa Su, AMD CEO")])

    def test_phone_signature_stops_parsing(self):
        commands, _ = self._verbs("RE: digest",
                                  "add Andy Jassy\n\nSent from my iPhone\nadd Nobody\n")
        self.assertEqual(commands, [("add", "Andy Jassy")])

    def test_politeness_bullets_and_numbering_are_stripped(self):
        commands, unparsed = self._verbs("RE: digest", (
            "Hi,\n"
            "Please add Tim Cook, Apple CEO\n"
            "- remove Jensen Huang\n"
            "2. list\n"
            "\n"
            "Thanks,\n"
            "Pat\n"
        ))
        self.assertEqual(commands, [("add", "Tim Cook, Apple CEO"),
                                    ("remove", "Jensen Huang"), ("list", "")])
        self.assertEqual(unparsed, [])  # the signature is not a failed instruction

    def test_verb_typos_are_tolerated(self):
        commands, _ = self._verbs("RE: digest", "delet Satya Nadella\nadresse nobody\n")
        self.assertEqual(commands[0], ("remove", "Satya Nadella"))

    def test_subject_is_a_command_only_when_not_a_reply(self):
        commands, _ = self._verbs("add Lisa Su (AMD CEO)", "")
        self.assertEqual(commands, [("add", "Lisa Su (AMD CEO)")])

        commands, _ = self._verbs("RE: Interview digest: 2 new interviews", "list\n")
        self.assertEqual(commands, [("list", "")])

    def test_unknown_instruction_is_reported_not_guessed(self):
        commands, unparsed = self._verbs("RE: digest", "sort out my subscription please\n")
        self.assertEqual(commands, [])
        self.assertEqual(unparsed, ["sort out my subscription please"])

    def test_parse_person_shapes(self):
        from interview_monitor.commands import parse_person
        self.assertEqual(parse_person("Tim Cook"), ("Tim Cook", "", ""))
        self.assertEqual(parse_person("Tim Cook, Apple, CEO"), ("Tim Cook", "Apple", "CEO"))
        self.assertEqual(parse_person("Tim Cook (Apple CEO)"), ("Tim Cook", "Apple", "CEO"))
        self.assertEqual(parse_person("Tim Cook - Apple CEO"), ("Tim Cook", "Apple", "CEO"))
        self.assertEqual(parse_person("Andy Jassy at Amazon"), ("Andy Jassy", "Amazon", ""))
        # A hyphenated name is not a name-dash-company split.
        self.assertEqual(parse_person("Jen-Hsun Huang"), ("Jen-Hsun Huang", "", ""))
        # "of" inside a company name survives, because it only splits the name off.
        self.assertEqual(parse_person("Brian Moynihan, Bank of America CEO"),
                         ("Brian Moynihan", "Bank of America", "CEO"))
        # Either order of the two tails, read by which one is a job title.
        self.assertEqual(parse_person("Sam Director, CFO, Umbrella Corporation"),
                         ("Sam Director", "Umbrella Corporation", "CFO"))
        # Shapes the config shorthand allows, now reachable by email too.
        self.assertEqual(parse_person("Alex Founder | Hooli"), ("Alex Founder", "Hooli", ""))
        self.assertEqual(parse_person("lisa su, amd ceo"), ("lisa su", "amd", "CEO"))

    def test_email_and_config_read_a_person_identically(self):
        """One parser behind both. A reply and a config line that say the same
        thing must not disagree about who that is."""
        from interview_monitor.commands import parse_person
        from interview_monitor.config import parse_person_line
        for text in ("Tim Cook", "Tim Cook, Apple, CEO", "Jensen Huang (CEO, Nvidia)",
                     "Andy Jassy at Amazon", "Alex Founder | Hooli", "Jane Doe, CEO",
                     "Lisa Su, chief executive of AMD", "Jane Smith-Executive"):
            with self.subTest(text=text):
                entry = parse_person_line(text)
                name, company, _ = parse_person(text)
                self.assertEqual(name, entry.get("name", ""))
                self.assertEqual(company, entry.get("company", ""))


class TestCommandHandling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "executives.json"
        self.path.write_text(json.dumps(SAMPLE_WATCHLIST), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _handle(self, body: str, subject: str = "RE: Interview digest", **kw):
        from interview_monitor.commands import handle
        return handle(subject, body, self.path, **kw)

    def _names(self):
        return [e["name"] for e in json.loads(self.path.read_text(encoding="utf-8"))["executives"]]

    def test_add_and_remove_are_written_once(self):
        outcome = self._handle("add Tim Cook, Apple CEO\nremove Jensen Huang\n")
        self.assertTrue(outcome.changed)
        self.assertEqual(len(outcome.applied), 2)
        self.assertEqual(self._names(), ["Satya Nadella", "Tim Cook"])
        self.assertEqual(outcome.listing, ["Satya Nadella (CEO, Microsoft)",
                                           "Tim Cook (CEO, Apple)"])

    def test_dry_run_reports_without_writing(self):
        outcome = self._handle("add Tim Cook\n", dry_run=True)
        self.assertTrue(outcome.changed)
        self.assertEqual(self._names(), ["Jensen Huang", "Satya Nadella"])

    def test_list_alone_changes_nothing_but_is_understood(self):
        outcome = self._handle("list\n")
        self.assertFalse(outcome.changed)
        self.assertTrue(outcome.listed)
        self.assertFalse(outcome.understood_nothing)
        self.assertFalse(outcome.needs_help)

    def test_rejection_with_nothing_applied_asks_for_help(self):
        outcome = self._handle("remove Somebody Else\n")
        self.assertFalse(outcome.changed)
        self.assertTrue(outcome.needs_help)

    def test_unrecognised_reply_asks_for_help_and_quotes_it(self):
        outcome = self._handle("what is this thing costing us\n")
        self.assertTrue(outcome.understood_nothing)
        self.assertIn("what is this thing costing us", outcome.unparsed)

    def test_reply_always_ends_with_the_current_list(self):
        from interview_monitor.report import render_command_reply
        text, html = render_command_reply(self._handle("add Tim Cook, Apple CEO\n"))
        self.assertIn("Now tracking 3 people:", text)
        self.assertIn("Tim Cook (CEO, Apple)", text)
        self.assertIn("Tim Cook (CEO, Apple)", html)

    def test_reply_escapes_html(self):
        from interview_monitor.report import render_command_reply
        _, html = render_command_reply(self._handle('add <script>alert(1)</script> Cook\n'))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestCommandAuth(unittest.TestCase):
    ENV = ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
           "COMMAND_MAILBOX", "COMMAND_SENDERS", "EMAIL_TO", "EMAIL_FROM")
    PASSED = "spf=pass (sender ip is 1.2.3.4) smtp.mailfrom=example.com; dkim=pass; dmarc=pass action=none"

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ.update({
            "GRAPH_TENANT_ID": "tenant", "GRAPH_CLIENT_ID": "client",
            "GRAPH_CLIENT_SECRET": "secret",
            "COMMAND_MAILBOX": "alerts@example.org",
            "COMMAND_SENDERS": "you@example.com",
        })

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    DEFAULT_HEADERS = object()

    def _message(self, *, sender="you@example.com", headers=DEFAULT_HEADERS):
        from interview_monitor.inbox import Message
        if headers is self.DEFAULT_HEADERS:
            headers = {"authentication-results": self.PASSED}
        return Message(id="AAA", subject="RE: digest", sender=sender,
                       received="2026-07-29T07:40:00Z", body="list", headers=headers,
                       internet_message_id="<a@example.com>")

    def _verdict(self, message):
        from interview_monitor.inbox import InboxConfig, authorize
        return authorize(message, InboxConfig.from_env())

    def test_allowlisted_sender_passing_dmarc_is_accepted(self):
        self.assertTrue(self._verdict(self._message()).ok)

    def test_senders_default_to_email_to(self):
        from interview_monitor.inbox import InboxConfig
        os.environ.pop("COMMAND_SENDERS")
        os.environ["EMAIL_TO"] = "you@example.com, ops@example.org"
        self.assertEqual(InboxConfig.from_env().senders,
                         ["you@example.com", "ops@example.org"])

    def test_unknown_sender_is_rejected(self):
        from interview_monitor.inbox import REJECT
        verdict = self._verdict(self._message(sender="stranger@example.com"))
        self.assertEqual(verdict.action, REJECT)
        self.assertIn("COMMAND_SENDERS", verdict.reason)

    def test_failed_dmarc_is_rejected_even_from_the_right_address(self):
        from interview_monitor.inbox import REJECT
        headers = {"authentication-results": "spf=fail; dkim=none; dmarc=fail action=quarantine"}
        verdict = self._verdict(self._message(headers=headers))
        self.assertEqual(verdict.action, REJECT)
        self.assertIn("DMARC", verdict.reason)

    def test_spf_and_dkim_pass_without_dmarc_are_rejected(self):
        # Both can pass for the attacker's own domain while From: is forged;
        # only DMARC ties them to the allowlisted address.
        from interview_monitor.inbox import REJECT
        headers = {"authentication-results": "spf=pass; dkim=pass; dmarc=none action=none"}
        self.assertEqual(self._verdict(self._message(headers=headers)).action, REJECT)

    def test_a_forged_authentication_header_below_exchanges_is_ignored(self):
        from interview_monitor.inbox import REJECT, _header_map
        headers = _header_map([
            {"name": "Authentication-Results",
             "value": "spf=pass; dkim=pass; dmarc=fail action=quarantine"},
            {"name": "Authentication-Results", "value": "dmarc=pass"},  # the forger's
        ])
        self.assertEqual(headers["authentication-results"],
                         "spf=pass; dkim=pass; dmarc=fail action=quarantine")
        self.assertEqual(self._verdict(self._message(headers=headers)).action, REJECT)

    def test_dmarc_pass_must_be_the_whole_result(self):
        from interview_monitor.inbox import REJECT
        for results in ("dmarc=passive", "xdmarc=pass; dmarc=fail", "arc=pass"):
            headers = {"authentication-results": results}
            self.assertEqual(self._verdict(self._message(headers=headers)).action,
                             REJECT, results)

    # Exchange's own stamp on mail that never left the tenant. Real headers
    # from a colleague's reply look exactly like this: dmarc=none because it
    # was never assessed, and no spf= line at all.
    INTERNAL = {"x-ms-exchange-organization-authas": "Internal",
                "authentication-results": "dkim=none (message not signed) header.d=none;"
                                          "dmarc=none action=none header.from=example.com;"}

    def test_internal_tenant_mail_is_accepted_without_dmarc(self):
        self.assertTrue(self._verdict(self._message(headers=dict(self.INTERNAL))).ok)

    def test_internal_stamp_is_read_case_and_space_insensitively(self):
        for value in ("internal", "  Internal  ", "INTERNAL"):
            headers = dict(self.INTERNAL, **{"x-ms-exchange-organization-authas": value})
            self.assertTrue(self._verdict(self._message(headers=headers)).ok, value)

    def test_external_mail_stamped_anonymous_still_needs_dmarc(self):
        from interview_monitor.inbox import REJECT
        # What Exchange stamps on inbound internet mail. The organization
        # headers are stripped at the boundary, so this is the only shape a
        # forger can produce - and it must not be enough on its own.
        headers = {"x-ms-exchange-organization-authas": "Anonymous",
                   "authentication-results": "spf=fail; dkim=none; dmarc=fail action=quarantine"}
        self.assertEqual(self._verdict(self._message(headers=headers)).action, REJECT)

    def test_internal_mail_still_has_to_be_allowlisted(self):
        from interview_monitor.inbox import REJECT
        verdict = self._verdict(
            self._message(sender="stranger@example.org", headers=dict(self.INTERNAL))
        )
        self.assertEqual(verdict.action, REJECT)
        self.assertIn("COMMAND_SENDERS", verdict.reason)

    def test_absent_headers_fail_closed(self):
        from interview_monitor.inbox import REJECT
        # Graph gave us no headers at all: authentication is unknowable.
        verdict = self._verdict(self._message(headers=None))
        self.assertEqual(verdict.action, REJECT)
        self.assertIn("headers", verdict.reason)
        # Headers present but unstamped is equally not a pass.
        self.assertEqual(self._verdict(self._message(headers={})).action, REJECT)

    def test_mail_from_the_mailbox_itself_is_skipped(self):
        from interview_monitor.inbox import SKIP
        verdict = self._verdict(self._message(sender="Alerts@example.org"))
        self.assertEqual(verdict.action, SKIP)

    def test_autoresponders_are_skipped_before_the_allowlist(self):
        from interview_monitor.inbox import SKIP
        for headers in ({"auto-submitted": "auto-replied"},
                        {"x-auto-response-suppress": "All"},
                        {"precedence": "bulk"}):
            headers["authentication-results"] = self.PASSED
            verdict = self._verdict(self._message(headers=headers))
            self.assertEqual(verdict.action, SKIP, headers)

    def test_change_is_copied_to_the_other_digest_recipients(self):
        from interview_monitor.inbox import InboxConfig
        os.environ["EMAIL_TO"] = "you@example.com, alex@example.org"
        cfg = InboxConfig.from_env()
        self.assertEqual(cfg.others_to_tell("you@example.com"),
                         ["alex@example.org"])

    def test_the_person_who_asked_is_not_copied_twice(self):
        from interview_monitor.inbox import InboxConfig
        os.environ["EMAIL_TO"] = "You@Example.com, alex@example.org"
        # They are the To: of the reply already; casing must not defeat that.
        cfg = InboxConfig.from_env()
        self.assertEqual(cfg.others_to_tell("you@EXAMPLE.com"),
                         ["alex@example.org"])

    def test_the_mailbox_never_copies_itself(self):
        from interview_monitor.inbox import InboxConfig
        # Copying the command mailbox would post our own reply back into the
        # folder we read instructions from.
        os.environ["EMAIL_TO"] = ("alex@example.org, "
                                  "alerts@example.org")
        cfg = InboxConfig.from_env()
        self.assertEqual(cfg.others_to_tell("you@example.com"),
                         ["alex@example.org"])

    def test_duplicate_recipients_are_copied_once(self):
        from interview_monitor.inbox import InboxConfig
        os.environ["EMAIL_TO"] = "alex@example.org; Alex@Example.org"
        cfg = InboxConfig.from_env()
        self.assertEqual(cfg.others_to_tell("you@example.com"),
                         ["alex@example.org"])

    def test_a_lone_recipient_leaves_nobody_to_copy(self):
        from interview_monitor.inbox import InboxConfig
        os.environ["EMAIL_TO"] = "you@example.com"
        cfg = InboxConfig.from_env()
        self.assertEqual(cfg.others_to_tell("you@example.com"), [])

    def test_command_mailbox_is_the_on_switch(self):
        from interview_monitor import inbox
        self.assertTrue(inbox.configured())
        os.environ.pop("COMMAND_MAILBOX")
        self.assertFalse(inbox.configured())

    def test_missing_mailbox_is_named(self):
        from interview_monitor.inbox import InboxConfig
        from interview_monitor.mailer import MailError
        os.environ.pop("COMMAND_MAILBOX")
        with self.assertRaises(MailError) as ctx:
            InboxConfig.from_env()
        self.assertIn("COMMAND_MAILBOX", str(ctx.exception))


class TestInbox(unittest.TestCase):
    """Graph plumbing, with mailer._request swapped out - no network."""

    def setUp(self):
        from interview_monitor import inbox, mailer
        self.calls = []
        self._real = mailer._request

        def fake_request(url, *, method="GET", data=None, headers=None, timeout=30):
            headers = headers or {}
            # The token call posts a form; only Graph calls post JSON.
            is_json = "json" in headers.get("Content-Type", "")
            self.calls.append({"url": url, "method": method, "headers": headers,
                               "body": json.loads(data.decode()) if data and is_json else None})
            return self.responses.pop(0)

        mailer._request = fake_request
        self.responses = []
        self.mailbox = inbox.Mailbox(inbox.InboxConfig(
            "tenant", "client", "secret", "alerts@example.org",
            ["you@example.com"],
        ))
        # Every call authenticates first; hand out a token once.
        self.responses.append((200, b'{"access_token":"t0ken"}'))

    def tearDown(self):
        from interview_monitor import mailer
        mailer._request = self._real

    def _graph_message(self, **kw):
        payload = {
            "id": "AAMk-1", "subject": "RE: Interview digest",
            "from": {"emailAddress": {"address": "you@example.com"}},
            "receivedDateTime": "2026-07-29T07:40:00Z",
            "body": {"contentType": "text", "content": "list"},
            "internetMessageId": "<a@example.com>",
            "internetMessageHeaders": [
                {"name": "Authentication-Results", "value": "dmarc=pass"},
                {"name": "Received", "value": "from one"},
                {"name": "Received", "value": "from two"},
            ],
        }
        payload.update(kw)
        return payload

    def test_fetch_unread_query_and_prefer_header(self):
        self.responses.append((200, json.dumps({"value": [self._graph_message()]}).encode()))
        messages = self.mailbox.fetch_unread()

        request = self.calls[-1]
        self.assertIn("/mailFolders/inbox/messages?", request["url"])
        self.assertIn("%24filter=isRead%20eq%20false", request["url"])
        self.assertIn("internetMessageHeaders", request["url"])
        self.assertNotIn("orderby", request["url"])  # sorted locally on purpose
        self.assertEqual(request["headers"]["Prefer"], 'outlook.body-content-type="text"')
        self.assertEqual(request["headers"]["Authorization"], "Bearer t0ken")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].sender, "you@example.com")
        self.assertEqual(messages[0].headers["authentication-results"], "dmarc=pass")
        # Repeated headers are kept, not overwritten.
        self.assertEqual(messages[0].headers["received"], "from one\nfrom two")

    def test_messages_are_ordered_oldest_first(self):
        self.responses.append((200, json.dumps({"value": [
            self._graph_message(id="new", receivedDateTime="2026-07-29T09:00:00Z"),
            self._graph_message(id="old", receivedDateTime="2026-07-29T08:00:00Z"),
        ]}).encode()))
        self.assertEqual([m.id for m in self.mailbox.fetch_unread()], ["old", "new"])

    def test_missing_headers_are_fetched_per_message(self):
        stripped = self._graph_message()
        stripped.pop("internetMessageHeaders")
        self.responses.append((200, json.dumps({"value": [stripped]}).encode()))
        self.responses.append((200, json.dumps({"internetMessageHeaders": [
            {"name": "Authentication-Results", "value": "dmarc=pass"}]}).encode()))

        messages = self.mailbox.fetch_unread()
        self.assertTrue(self.calls[-1]["url"].endswith("?$select=internetMessageHeaders"))
        self.assertEqual(messages[0].headers, {"authentication-results": "dmarc=pass"})

    def test_html_bodies_are_flattened_to_text(self):
        from interview_monitor.inbox import Message
        message = Message.from_graph(self._graph_message(body={
            "contentType": "html",
            "content": "<div>add Tim Cook<br>remove Jensen Huang</div>",
        }))
        self.assertEqual(message.body.split("\n")[1:3], ["add Tim Cook", "remove Jensen Huang"])

    def test_mark_read_patches_is_read(self):
        self.responses.append((200, b"{}"))
        self.mailbox.mark_read("AAMk-1")
        request = self.calls[-1]
        self.assertEqual(request["method"], "PATCH")
        self.assertTrue(request["url"].endswith("/messages/AAMk-1"))
        self.assertEqual(request["body"], {"isRead": True})

    def test_reply_threads_and_suppresses_autoresponders(self):
        self.responses.append((202, b""))
        self.mailbox.reply("AAMk-1", "plain", "<p>rich</p>")
        request = self.calls[-1]
        self.assertEqual(request["method"], "POST")
        self.assertTrue(request["url"].endswith("/messages/AAMk-1/reply"))
        message = request["body"]["message"]
        self.assertEqual(message["body"], {"contentType": "HTML", "content": "<p>rich</p>"})
        self.assertEqual(message["internetMessageHeaders"],
                         [{"name": "X-Auto-Response-Suppress", "value": "All"}])

    def test_reply_carries_no_cc_when_nobody_else_needs_telling(self):
        self.responses.append((202, b""))
        self.mailbox.reply("AAMk-1", "plain")
        self.assertNotIn("ccRecipients", self.calls[-1]["body"]["message"])

    def test_reply_copies_the_rest_of_the_digest_audience(self):
        self.responses.append((202, b""))
        self.mailbox.reply("AAMk-1", "plain", cc=["alex@example.org",
                                                  "sam@example.org"])
        message = self.calls[-1]["body"]["message"]
        self.assertEqual(
            message["ccRecipients"],
            [{"emailAddress": {"address": "alex@example.org"}},
             {"emailAddress": {"address": "sam@example.org"}}],
        )
        # Copying people in must not cost the loop guard.
        self.assertEqual(message["internetMessageHeaders"],
                         [{"name": "X-Auto-Response-Suppress", "value": "All"}])

    def test_read_permission_error_explains_the_fix(self):
        from interview_monitor.mailer import MailError
        self.responses.append(
            (403, b'{"error":{"code":"ErrorAccessDenied","message":"Access is denied."}}'))
        with self.assertRaises(MailError) as ctx:
            self.mailbox.fetch_unread()
        # The fix is an Exchange role assignment, not an Entra consent screen.
        self.assertIn("Application RBAC", str(ctx.exception))
        self.assertIn("cache", str(ctx.exception))

    def test_unlicensed_mailbox_error_explains_the_fix(self):
        from interview_monitor.mailer import _explain_graph_error
        body = b'{"error":{"code":"MailboxNotEnabledForRESTAPI","message":"no"}}'
        message = _explain_graph_error(400, body)
        self.assertIn("licence", message)

    def test_send_as_denied_points_at_the_send_role(self):
        from interview_monitor.mailer import _explain_graph_error
        body = b'{"error":{"code":"ErrorSendAsDenied","message":"no"}}'
        self.assertIn("Mail.Send", _explain_graph_error(403, body))


class TestMailSeen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "seen.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_processed_mail_is_remembered_across_runs(self):
        with Store(self.db) as store:
            self.assertFalse(store.mail_processed("<a@example.com>"))
            store.record_mail("<a@example.com>", sender="you@example.com",
                              outcome="changed")
            self.assertTrue(store.mail_processed("<a@example.com>"))
        with Store(self.db) as store:  # a new process, e.g. the next timer tick
            self.assertTrue(store.mail_processed("<a@example.com>"))

    def test_recording_the_same_message_twice_is_harmless(self):
        with Store(self.db) as store:
            store.record_mail("<a@x>", sender="a@x", outcome="changed")
            store.record_mail("<a@x>", sender="a@x", outcome="changed")
            self.assertTrue(store.mail_processed("<a@x>"))


class TestDigestFooter(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("COMMAND_MAILBOX")

    def tearDown(self):
        os.environ.pop("COMMAND_MAILBOX", None)
        if self._saved is not None:
            os.environ["COMMAND_MAILBOX"] = self._saved

    def _digest(self):
        from interview_monitor.report import render_digest_html, render_digest_text
        from interview_monitor.monitor import RunResult
        result = RunResult(executives=[JENSEN], new_items=[], candidates=0, errors=[])
        return render_digest_text(result), render_digest_html(result)

    def test_instructions_appear_when_the_channel_is_on(self):
        os.environ["COMMAND_MAILBOX"] = "alerts@example.org"
        text, html = self._digest()
        for rendered in (text, html):
            self.assertIn("add Tim Cook, Apple CEO", rendered)
            self.assertIn("reply", rendered.lower())
        self.assertIn("Tracking 1 person", text)

    def test_digest_makes_no_promise_when_the_channel_is_off(self):
        os.environ.pop("COMMAND_MAILBOX", None)
        text, html = self._digest()
        self.assertNotIn("add Tim Cook", text)
        self.assertNotIn("add Tim Cook", html)


if __name__ == "__main__":
    unittest.main()
