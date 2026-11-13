from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

import feedparser
import requests

import financebot
from news_sources import (
    collect_source_articles, entry_published_at, fetch_cninfo_announcements,
    fetch_official_feed, load_sent_articles, save_sent_articles, select_recent_articles,
)


RUN_TIME = datetime(2026, 11, 13, 9, tzinfo=timezone.utc)


def entry(link, hours=1):
    return {
        "title": "测试公告",
        "link": link,
        "published_at": RUN_TIME - timedelta(hours=hours),
    }


class NewsSourcesTests(unittest.TestCase):
    def test_recent_window_duplicates_sorting_and_limit(self):
        entries = [
            entry("https://example.com/old", 25),
            entry("https://example.com/sent"),
            entry("https://example.com/b", 3),
            entry("https://example.com/a#part", 1),
            entry("https://example.com/a", 1),
            entry("https://example.com/future", -1),
            {**entry("https://example.com/undated"), "published_at": None},
            entry("https://example.com/boundary", 24),
        ]
        selected = select_recent_articles(entries, {"https://example.com/sent"}, RUN_TIME, 2)
        self.assertEqual([item["link"] for item in selected], [
            "https://example.com/a", "https://example.com/b",
        ])
        selected = select_recent_articles(entries, set(), RUN_TIME, 20)
        self.assertIn("https://example.com/boundary", [item["link"] for item in selected])

    def test_rss_and_atom_dates_use_utc(self):
        rss = feedparser.parse(
            '<rss version="2.0"><channel><title>test</title><item>'
            '<title>test</title><pubDate>Fri, 13 Nov 2026 17:00:00 +0800</pubDate>'
            '</item></channel></rss>'
        )
        atom = feedparser.parse(
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            '<title>test</title><updated>2026-11-13T09:00:00Z</updated>'
            '</entry></feed>'
        )
        self.assertEqual(entry_published_at(rss.entries[0]), RUN_TIME)
        self.assertEqual(entry_published_at(atom.entries[0]), RUN_TIME)
        self.assertIsNone(entry_published_at({}))

    def test_collect_deduplicates_across_sources(self):
        links = set()
        fetch_text = unittest.mock.Mock(return_value="公告正文")
        articles, analysis = collect_source_articles(
            [entry("https://example.com/a")], "来源甲", links, RUN_TIME, 5, fetch_text,
        )
        self.assertEqual(len(articles), 1)
        self.assertIn("公告正文", analysis)
        self.assertIn("来源甲", analysis)
        repeated, _ = collect_source_articles(
            [entry("https://example.com/a")], "来源乙", links, RUN_TIME, 5, fetch_text,
        )
        self.assertEqual(repeated, [])
        fetch_text.assert_called_once()

    def test_body_failure_does_not_mark_article_sent(self):
        links = set()
        with self.assertRaises(ValueError):
            collect_source_articles(
                [entry("https://example.com/a")], "来源", links, RUN_TIME, 5,
                lambda url: "",
            )
        self.assertEqual(links, set())

    def test_state_persists_between_runs(self):
        directory = Path(".work/tests")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "sent_articles.json"
        path.unlink(missing_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertEqual(load_sent_articles(path), {})
        save_sent_articles({"https://example.com/old": "2026-11-12"},
                           {"https://example.com/new"}, RUN_TIME, path)
        sent = load_sent_articles(path)
        self.assertEqual(set(sent), {"https://example.com/old", "https://example.com/new"})
        self.assertEqual(select_recent_articles(
            [entry("https://example.com/new")], set(sent), RUN_TIME, 5,
        ), [])
        self.assertFalse(path.with_suffix(".pending").exists())

    @patch("news_sources.requests.post")
    def test_cninfo_maps_public_response_and_handles_no_results(self, post):
        post.return_value.json.return_value = {"announcements": [{
            "secCode": "688981", "secName": "中芯国际", "announcementTitle": "季度报告",
            "announcementTime": int(RUN_TIME.timestamp() * 1000),
            "adjunctUrl": "finalpage/2026-11-13/report.PDF", "adjunctType": "PDF",
        }]}
        entries = fetch_cninfo_announcements(RUN_TIME)
        self.assertEqual(entries[0]["published_at"], RUN_TIME)
        self.assertEqual(entries[0]["link"], "https://static.cninfo.com.cn/finalpage/2026-11-13/report.PDF")
        self.assertIn("中芯国际", entries[0]["title"])
        post.return_value.json.return_value = {"announcements": None}
        self.assertEqual(fetch_cninfo_announcements(RUN_TIME), [])
        post.return_value.json.return_value = {}
        with self.assertRaises(KeyError):
            fetch_cninfo_announcements(RUN_TIME)

    @patch("news_sources.requests.get")
    def test_official_feed_http_failure_is_reported(self, get):
        get.return_value.raise_for_status.side_effect = requests.HTTPError("403")
        with self.assertRaises(requests.HTTPError):
            fetch_official_feed("https://example.com/rss")

    @patch("financebot.save_sent_articles")
    @patch("financebot.send_email")
    @patch("financebot.save_report")
    @patch("financebot.summarize", return_value="测试分析")
    @patch("financebot.OpenAI")
    @patch("financebot.required_env", return_value="test-key")
    @patch("financebot.fetch_rss_articles")
    @patch("financebot.load_sent_articles", return_value={})
    def test_state_written_only_after_email_success(
        self, load, fetch, env, client, summarize, report, send, save,
    ):
        fetch.return_value = ({"分类": "新闻"}, "公告正文", {"https://example.com/a"})
        send.side_effect = RuntimeError("SMTP 发送失败")
        with self.assertRaises(RuntimeError):
            financebot.main()
        save.assert_not_called()
        send.side_effect = None
        events = []
        send.side_effect = lambda *args: events.append("email")
        save.side_effect = lambda *args: events.append("state")
        financebot.main()
        self.assertEqual(events, ["email", "state"])

    @patch("financebot.OpenAI")
    @patch("financebot.send_email")
    @patch("financebot.fetch_rss_articles", return_value=({}, "", set()))
    @patch("financebot.load_sent_articles", return_value={})
    def test_no_new_articles_skips_model_and_email(self, load, fetch, send, client):
        financebot.main()
        client.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
