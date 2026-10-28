import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BOT_DATA_DIR", "/tmp/hasad-auto-publisher-test-data")

import auto_publish_alittihad_alkhabar as publisher  # noqa: E402


class UrgentTelegramPublisherTests(unittest.TestCase):
    def test_split_routes_only_marked_telegram_posts(self):
        marked = {
            "_telegram_source": True,
            "_telegram_original_raw_text": "تويتر الخبر العاجل\nالنص الأصلي",
            "raw_body": "تويتر الخبر العاجل\nالنص الأصلي",
            "title": "تويتر الخبر العاجل",
        }
        ordinary_telegram = {
            "_telegram_source": True,
            "_telegram_original_raw_text": "خبر عادي",
            "raw_body": "خبر عادي",
            "title": "خبر عادي",
        }
        rss_item = {"title": "خبر من RSS"}

        regular, urgent = publisher._split_twitter_urgent_items(
            [marked, ordinary_telegram, rss_item]
        )

        self.assertEqual(regular, [ordinary_telegram, rss_item])
        self.assertEqual(len(urgent), 1)
        self.assertEqual(urgent[0]["title"], "الخبر العاجل")
        self.assertEqual(urgent[0]["_urgent_verbatim_text"], "الخبر العاجل\nالنص الأصلي")

    def test_marked_post_is_inserted_verbatim_as_breaking_without_telegram_resend(self):
        text = "الخبر كما كُتب\nالسطر الثاني  \n"
        item = {
            "title": "الخبر كما كُتب",
            "_urgent_verbatim_text": text,
            "link": "https://t.me/c/1234567890/42",
            "pub_date": datetime(2026, 10, 28, tzinfo=timezone.utc),
        }
        existing_urls = set()
        blocked_links = set()
        with (
            mock.patch.object(publisher, "get_category_id", return_value="urgent-category"),
            mock.patch.object(publisher, "sb_insert", return_value="post-id") as insert,
            mock.patch.object(publisher, "log_published_title"),
            mock.patch.object(publisher, "log_raw_content"),
            mock.patch.object(publisher, "save_blocked_link"),
            mock.patch.object(publisher, "seed_views"),
            mock.patch.object(publisher, "update_breaking_ticker"),
            mock.patch.object(publisher, "send_to_telegram") as send_to_telegram,
        ):
            self.assertTrue(
                publisher._publish_urgent_telegram_item(item, existing_urls, blocked_links)
            )

        record = insert.call_args.args[0]
        self.assertEqual(record["content"], text)
        self.assertEqual(record["title"], "الخبر كما كُتب")
        self.assertEqual(record["category_id"], "urgent-category")
        self.assertTrue(record["is_breaking"])
        self.assertEqual(record["source_url"], item["link"])
        self.assertFalse(send_to_telegram.called)
        self.assertIn(item["link"], existing_urls)
        self.assertIn(item["link"], blocked_links)


if __name__ == "__main__":
    unittest.main()
