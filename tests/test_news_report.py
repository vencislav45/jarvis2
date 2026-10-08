from datetime import datetime, timezone
import unittest
from unittest.mock import patch
from actions.news_report import parse_feed, news_report


class NewsTests(unittest.TestCase):
    def test_freshness_deduplication_and_links(self):
        items = """<rss><channel>
        <item><title>Current headline</title><link>https://example.com/a</link><source>Publisher</source><pubDate>Tue, 22 Sep 2026 10:00:00 GMT</pubDate></item>
        <item><title>Current headline</title><link>https://example.com/a</link><pubDate>Tue, 22 Sep 2026 10:00:00 GMT</pubDate></item>
        <item><title>Old</title><link>https://example.com/b</link><pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate></item>
        <item><title>Bad link</title><link>javascript:alert(1)</link><pubDate>Tue, 22 Sep 2026 10:00:00 GMT</pubDate></item>
        <item><title>No date</title><link>https://example.com/c</link></item>
        </channel></rss>"""
        results = parse_feed(items, now=datetime(2026, 9, 22, 12, tzinfo=timezone.utc))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "Publisher")

    def test_failure_does_not_invent_news(self):
        with patch("actions.news_report.fetch_headlines", side_effect=RuntimeError("offline")):
            self.assertTrue(news_report().startswith("No news found"))

    def test_news_query_defaults_to_world(self):
        from actions.web_search import web_search
        with patch("actions.news_report.fetch_headlines", side_effect=fake_feeds) as fetch:
            output = web_search({"mode": "news"})
        self.assertEqual(fetch.call_args_list[0].args, ("world", 5))
        self.assertIn("World Publisher", output)
        self.assertIn("https://example.com/world/0", output)

    def test_world_news_has_three_bulgarian_headlines_after_world(self):
        with patch("actions.news_report.fetch_headlines", side_effect=fake_feeds), \
             patch.dict("os.environ", {"JARVIS_NEWS_COUNTRY": "Bulgaria", "JARVIS_NEWS_LOCAL_COUNT": "3"}):
            output = news_report({"topic": "world", "count": 5})
        self.assertIn("WORLD (5)", output)
        self.assertIn("BULGARIA (3)", output)
        self.assertLess(output.index("WORLD"), output.index("BULGARIA"))
        self.assertIn("8. Българска новина", output)          # numbered straight after the 5 world ones
        self.assertNotIn("9.", output)
        self.assertEqual(output.count("Shared story"), 1)      # a story in both feeds is read once

    def test_local_section_can_be_turned_off_or_missing(self):
        with patch("actions.news_report.fetch_headlines", side_effect=fake_feeds), \
             patch.dict("os.environ", {"JARVIS_NEWS_LOCAL_COUNT": "0"}):
            self.assertNotIn("BULGARIA", news_report())

        def bulgaria_down(topic, count):
            if topic == "Bulgaria":
                raise RuntimeError("offline")
            return fake_feeds(topic, count)
        with patch("actions.news_report.fetch_headlines", side_effect=bulgaria_down), \
             patch.dict("os.environ", {"JARVIS_NEWS_LOCAL_COUNT": "3"}):
            output = news_report()
        self.assertIn("WORLD (5)", output)
        self.assertIn("BULGARIA: headlines unavailable right now", output)

    def test_asking_for_bulgaria_uses_bulgarian_edition(self):
        from actions.news_report import _feed_urls
        urls = _feed_urls("България")
        self.assertIn("ceid=BG%3Abg", urls[0])
        self.assertTrue(all("BG" in u for u in urls))

    def test_publisher_suffix_removed_from_title(self):
        items = """<rss><channel><item><title>Кабинетът предлага ордени - Mediapool.bg</title>
        <link>https://example.com/a</link><source>Mediapool.bg</source>
        <pubDate>Tue, 22 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
        results = parse_feed(items.encode("utf-8"), now=datetime(2026, 9, 22, 12, tzinfo=timezone.utc))
        self.assertEqual(results[0]["title"], "Кабинетът предлага ордени")


def fake_feeds(topic, count):
    if topic == "Bulgaria":
        rows = [("Shared story", "BG")] + [(f"Българска новина {i}", "Dnes.bg") for i in range(1, 6)]
        return [{"title": t, "source": s, "published": "2026-09-22", "url": f"https://example.com/bg/{i}"}
                for i, (t, s) in enumerate(rows)][:count]
    rows = [("Shared story", "Reuters")] + [(f"World story {i}", "World Publisher") for i in range(1, 10)]
    return [{"title": t, "source": s, "published": "2026-09-22", "url": f"https://example.com/world/{i}"}
            for i, (t, s) in enumerate(rows)][:count]
