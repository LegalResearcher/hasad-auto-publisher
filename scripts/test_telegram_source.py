import os
import sys
import unittest
from datetime import timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import telegram_source as source  # noqa: E402

CHANNEL_ID = "-1009876543210"


class TelegramSourceItemTests(unittest.TestCase):
    def test_text_post_becomes_raw_news_item(self):
        update = {
            "update_id": 9001,
            "channel_post": {
                "message_id": 42,
                "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)},
                "text": "عنوان الخبر\n\nتفاصيل الخبر كاملة.",
            },
        }
        item = source._to_news_item(update, CHANNEL_ID)
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], "عنوان الخبر")
        self.assertEqual(item["raw_body"], "عنوان الخبر\n\nتفاصيل الخبر كاملة.")
        self.assertEqual(item["link"], f"https://t.me/c/{CHANNEL_ID[4:]}/42")
        self.assertEqual(item["source_feed"], f"telegram://{CHANNEL_ID}")
        self.assertEqual(item["category"], "أخبار وتقارير")
        self.assertEqual(item["_telegram_update_id"], 9001)
        self.assertEqual(item["pub_date"].tzinfo, timezone.utc)

    def test_photo_caption_uses_largest_photo(self):
        update = {
            "update_id": 9002,
            "channel_post": {
                "message_id": 43,
                "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)},
                "caption": "عنوان الصورة\nشرح الخبر",
                "photo": [
                    {"file_id": "small", "width": 90, "height": 90},
                    {"file_id": "large", "width": 1280, "height": 960},
                    {"file_id": "wide", "width": 1200, "height": 700},
                ],
            },
        }
        item = source._to_news_item(update, CHANNEL_ID)
        self.assertEqual(item["_telegram_photo_file_id"], "large")
        self.assertEqual(item["raw_body"], "عنوان الصورة\nشرح الخبر")
        self.assertEqual(item["link"], f"https://t.me/c/{CHANNEL_ID[4:]}/43")

    def test_ignores_other_channels(self):
        update = {
            "update_id": 9003,
            "channel_post": {
                "message_id": 44,
                "date": 1790360000,
                "chat": {"id": -1001234567890},
                "text": "خبر من قناة أخرى",
            },
        }
        self.assertIsNone(source._to_news_item(update, CHANNEL_ID))

    def test_ignores_posts_without_text_or_caption(self):
        update = {
            "update_id": 9004,
            "channel_post": {
                "message_id": 45,
                "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)},
                "photo": [{"file_id": "photo-only", "width": 400, "height": 300}],
            },
        }
        self.assertIsNone(source._to_news_item(update, CHANNEL_ID))


class TelegramPollingTests(unittest.TestCase):
    def test_fetch_uses_source_secrets_and_resumes_after_saved_update(self):
        update = {
            "update_id": 9010,
            "channel_post": {
                "message_id": 50,
                "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)},
                "text": "عنوان\nمتن خام كامل",
            },
        }
        environment = {
            "TELEGRAM_SOURCE_BOT_TOKEN": "test-source-token",
            "TELEGRAM_SOURCE_CHAT_ID": CHANNEL_ID,
            "SUPABASE_URL": "https://supabase.example",
            "SUPABASE_SERVICE_KEY": "test-service-key",
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(source, "get_last_update_id", return_value=9009),
            mock.patch.object(source, "_ensure_polling_mode") as ensure_polling,
            mock.patch.object(source, "_verify_source_channel", return_value="test_bot") as verify_channel,
            mock.patch.object(source, "_telegram_api_call", return_value={"result": [update]}) as api_call,
        ):
            items, cursor = source.fetch_telegram_items()

        self.assertEqual(cursor, 9010)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["raw_body"], "عنوان\nمتن خام كامل")
        ensure_polling.assert_called_once_with("test-source-token")
        verify_channel.assert_called_once_with("test-source-token", CHANNEL_ID)
        self.assertEqual(api_call.call_args.args[1], "getUpdates")
        self.assertEqual(api_call.call_args.args[2]["offset"], 9010)

    def test_cursor_commit_uses_supabase_service_key(self):
        environment = {
            "TELEGRAM_SOURCE_BOT_TOKEN": "test-source-token",
            "TELEGRAM_SOURCE_CHAT_ID": CHANNEL_ID,
            "SUPABASE_URL": "https://supabase.example/",
            "SUPABASE_SERVICE_KEY": "test-service-key",
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(source, "save_last_update_id") as save_cursor,
        ):
            source.commit_telegram_cursor(9010)
        save_cursor.assert_called_once_with(
            "https://supabase.example", "test-service-key", 9010
        )


if __name__ == "__main__":
    unittest.main()
