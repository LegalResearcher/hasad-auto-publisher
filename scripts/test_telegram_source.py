import os
import sys
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import telegram_source as source  # noqa: E402

CHANNEL_ID = "-1009876543210"


class TelegramSourceItemTests(unittest.TestCase):
    def test_extracts_same_video_hosts_and_direct_video_files_as_shmsan(self):
        cases = {
            "https://youtu.be/abc123": "https://youtu.be/abc123",
            "https://instagram.com/reel/example/": "https://instagram.com/reel/example/",
            "https://www.dailymotion.com/video/x123": "https://www.dailymotion.com/video/x123",
            "https://streamable.com/clip": "https://streamable.com/clip",
            "https://cdn.example/video.mp4?token=abc": "https://cdn.example/video.mp4?token=abc",
            "https://example.com/photo.jpg": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(source.extract_video_url(text), expected)

    def test_video_url_is_carried_to_normal_telegram_item(self):
        update = {
            "update_id": 9011,
            "channel_post": {
                "message_id": 51,
                "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)},
                "text": "عنوان الخبر\nتفاصيل الخبر\nhttps://x.com/source/status/12345",
            },
        }
        item = source._to_news_item(update, CHANNEL_ID)
        self.assertEqual(item["_telegram_video_url"], "https://x.com/source/status/12345")

    def test_extracts_hidden_video_url_from_text_link_entity(self):
        update = {
            "update_id": 9016,
            "channel_post": {
                "message_id": 57,
                "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)},
                "text": "شاهدوا المشاهد هنا",
                "entities": [{
                    "type": "text_link",
                    "offset": 0,
                    "length": 16,
                    "url": "https://x.com/source/status/98765",
                }],
            },
        }
        item = source._to_news_item(update, CHANNEL_ID)
        self.assertEqual(item["_telegram_video_url"], "https://x.com/source/status/98765")

    def test_extracts_photo_sent_as_image_document(self):
        update = {
            "update_id": 9017,
            "channel_post": {
                "message_id": 58,
                "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)},
                "caption": "عنوان الصورة كملف",
                "document": {
                    "file_id": "image-document",
                    "mime_type": "image/jpeg",
                    "file_name": "news-photo.jpg",
                },
            },
        }
        item = source._to_news_item(update, CHANNEL_ID)
        self.assertEqual(item["_telegram_photo_file_id"], "image-document")

    def test_edited_post_with_media_is_marked_for_merge(self):
        update = {
            "update_id": 9018,
            "edited_channel_post": {
                "message_id": 59,
                "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)},
                "text": "عنوان معدل\nنص الخبر\nhttps://x.com/source/status/12345",
                "photo": [{"file_id": "edited-photo", "width": 1200, "height": 900}],
            },
        }
        item = source._to_news_item(update, CHANNEL_ID)
        self.assertTrue(item["_telegram_media_edit"])
        self.assertEqual(item["_telegram_photo_file_id"], "edited-photo")
        self.assertEqual(item["_telegram_video_url"], "https://x.com/source/status/12345")

    def test_unpublished_edited_post_keeps_body_and_media_as_new_item(self):
        edited = source._to_news_item({
            "update_id": 9019,
            "edited_channel_post": {
                "message_id": 60,
                "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)},
                "text": "عنوان معدل\nنص الخبر",
                "photo": [{"file_id": "edited-photo", "width": 1200, "height": 900}],
            },
        }, CHANNEL_ID)
        news, late = source.merge_photo_replies_with_news_items([edited])
        self.assertEqual(news, [edited])
        self.assertEqual(late, [])

    def test_published_edited_post_routes_to_late_media_update(self):
        edited = source._to_news_item({
            "update_id": 9020,
            "edited_channel_post": {
                "message_id": 61,
                "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)},
                "text": "عنوان معدل\nنص الخبر",
                "photo": [{"file_id": "edited-photo", "width": 1200, "height": 900}],
            },
        }, CHANNEL_ID)
        news, late = source.merge_photo_replies_with_news_items(
            [edited], existing_source_urls={edited["link"]}
        )
        self.assertEqual(news, [])
        self.assertEqual(late, [edited])

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

    def test_twitter_urgent_trigger_removes_marker_and_preserves_remaining_text(self):
        original = "تويتر الخبر العاجل\nالتفاصيل كما وردت.\n"
        self.assertEqual(
            source.extract_twitter_urgent_body(original),
            "الخبر العاجل\nالتفاصيل كما وردت.\n",
        )

    def test_twitter_urgent_trigger_requires_standalone_word_without_colon(self):
        self.assertIsNone(source.extract_twitter_urgent_body("تويتر: هذا لا يطابق الكلمة المتفق عليها"))
        self.assertIsNone(source.extract_twitter_urgent_body("تويتر : هذا لا يطابق الكلمة المتفق عليها"))
        self.assertIsNone(source.extract_twitter_urgent_body("تويتر\n: هذا لا يطابق الكلمة المتفق عليها"))
        self.assertIsNone(source.extract_twitter_urgent_body("تويترية ليست علامة تشغيل"))
        self.assertIsNone(source.extract_twitter_urgent_body("خبر عن تويتر فقط"))
        self.assertEqual(source.extract_twitter_urgent_body("تويتر"), "")

    def test_original_telegram_text_is_kept_separately_for_verbatim_route(self):
        original = "تويتر  عنوان الخبر\nالنص الأصلي  \n"
        item = source._to_news_item({
            "update_id": 9021,
            "channel_post": {
                "message_id": 62,
                "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)},
                "text": original,
            },
        }, CHANNEL_ID)
        self.assertEqual(item["raw_body"], original.strip())
        self.assertEqual(item["_telegram_original_raw_text"], original)
        self.assertEqual(
            source.extract_twitter_urgent_body(item["_telegram_original_raw_text"]),
            "عنوان الخبر\nالنص الأصلي  \n",
        )

    def test_authorized_private_message_with_twitter_prefix_becomes_urgent_item(self):
        update = {
            "update_id": 9022,
            "message": {
                "message_id": 70,
                "date": 1790360000,
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345, "is_bot": False},
                "text": "تويتر خبر عاجل\nالنص كما كُتب",
            },
        }
        item = source._private_message_to_news_item(update, {"12345"}, {}, set())
        self.assertIsNotNone(item)
        self.assertTrue(item["_telegram_private_source"])
        self.assertEqual(item["title"], "خبر عاجل")
        self.assertEqual(
            source.extract_twitter_urgent_body(item["_telegram_original_raw_text"]),
            "خبر عاجل\nالنص كما كُتب",
        )
        self.assertEqual(item["link"], "telegram-private://12345/70")

    def test_private_message_from_non_admin_is_ignored(self):
        update = {
            "update_id": 9023,
            "message": {
                "message_id": 71,
                "date": 1790360000,
                "chat": {"id": 99999, "type": "private"},
                "from": {"id": 99999, "is_bot": False},
                "text": "تويتر لا ينبغي نشره",
            },
        }
        self.assertIsNone(source._private_message_to_news_item(update, {"12345"}, {}, set()))
        authorized_but_unmarked = {
            **update,
            "message": {
                **update["message"],
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345, "is_bot": False},
                "text": "رسالة خاصة عادية لا تُنشر",
            },
        }
        self.assertIsNone(
            source._private_message_to_news_item(authorized_but_unmarked, {"12345"}, {}, set())
        )

    def test_standalone_twitter_command_arms_the_next_forwarded_post(self):
        pending = {}
        consumed = set()
        trigger_update = {
            "update_id": 9024,
            "message": {
                "message_id": 72,
                "date": 1790360000,
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345, "is_bot": False},
                "text": "تويتر",
            },
        }
        self.assertIsNone(
            source._private_message_to_news_item(trigger_update, {"12345"}, pending, consumed)
        )
        self.assertIn("12345", pending)

        forward_update = {
            "update_id": 9025,
            "message": {
                "message_id": 73,
                "date": 1790360010,
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 12345, "is_bot": False},
                "forward_origin": {
                    "type": "channel",
                    "chat": {"id": int(CHANNEL_ID), "username": "sourcechannel"},
                    "message_id": 88,
                },
                "text": "العنوان المحوّل\nالنص الأصلي",
            },
        }
        item = source._private_message_to_news_item(
            forward_update, {"12345"}, pending, consumed
        )
        self.assertEqual(item["link"], "https://t.me/sourcechannel/88")
        self.assertEqual(item["_pending_twitter_user_id"], "12345")
        self.assertEqual(
            source.extract_twitter_urgent_body(item["_telegram_original_raw_text"]),
            "العنوان المحوّل\nالنص الأصلي",
        )
        self.assertIn("12345", pending)  # removed only after successful cursor commit

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

    def test_photo_reply_uses_original_news_text_and_original_post_link(self):
        update = {
            "update_id": 9005,
            "channel_post": {
                "message_id": 46,
                "date": 1790360100,
                "chat": {"id": int(CHANNEL_ID)},
                "photo": [{"file_id": "reply-photo", "width": 1200, "height": 900}],
                "reply_to_message": {
                    "message_id": 42,
                    "date": 1790360000,
                    "chat": {"id": int(CHANNEL_ID)},
                    "text": "عنوان الخبر الأصلي\nتفاصيل الخبر الأصلي كاملة.",
                },
            },
        }
        item = source._to_news_item(update, CHANNEL_ID)
        self.assertTrue(item["_telegram_photo_reply"])
        self.assertEqual(item["title"], "عنوان الخبر الأصلي")
        self.assertEqual(item["raw_body"], "عنوان الخبر الأصلي\nتفاصيل الخبر الأصلي كاملة.")
        self.assertEqual(item["link"], f"https://t.me/c/{CHANNEL_ID[4:]}/42")
        self.assertEqual(item["_telegram_photo_file_id"], "reply-photo")

    def test_same_batch_reply_photo_is_merged_into_news_item(self):
        original = source._to_news_item(
            {
                "update_id": 9006,
                "channel_post": {
                    "message_id": 47,
                    "date": 1790360000,
                    "chat": {"id": int(CHANNEL_ID)},
                    "text": "عنوان خبر جديد\nمتن الخبر.",
                },
            },
            CHANNEL_ID,
        )
        reply = source._to_news_item(
            {
                "update_id": 9007,
                "channel_post": {
                    "message_id": 48,
                    "date": 1790360100,
                    "chat": {"id": int(CHANNEL_ID)},
                    "photo": [{"file_id": "reply-photo", "width": 1200, "height": 900}],
                    "reply_to_message": {
                        "message_id": 47,
                        "date": 1790360000,
                        "chat": {"id": int(CHANNEL_ID)},
                        "text": "عنوان خبر جديد\nمتن الخبر.",
                    },
                },
            },
            CHANNEL_ID,
        )
        news, late = source.merge_photo_replies_with_news_items([original, reply])
        self.assertEqual(len(news), 1)
        self.assertEqual(late, [])
        self.assertEqual(news[0]["_telegram_photo_file_id"], "reply-photo")
        self.assertEqual(news[0]["_telegram_update_id"], 9007)

    def test_same_batch_video_only_reply_is_merged_into_original_news_item(self):
        original = source._to_news_item({"update_id": 9012, "channel_post": {
            "message_id": 52, "date": 1790360000, "chat": {"id": int(CHANNEL_ID)},
            "text": "عنوان خبر الفيديو\nمتن الخبر.",
        }}, CHANNEL_ID)
        reply = source._to_news_item({"update_id": 9013, "channel_post": {
            "message_id": 53, "date": 1790360100, "chat": {"id": int(CHANNEL_ID)},
            "text": "https://youtu.be/video123",
            "reply_to_message": {"message_id": 52, "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)}, "text": "عنوان خبر الفيديو\nمتن الخبر."},
        }}, CHANNEL_ID)
        news, late = source.merge_photo_replies_with_news_items([original, reply])
        self.assertEqual(len(news), 1)
        self.assertEqual(late, [])
        self.assertEqual(news[0]["link"], f"https://t.me/c/{CHANNEL_ID[4:]}/52")
        self.assertEqual(news[0]["_telegram_video_url"], "https://youtu.be/video123")

    def test_late_video_only_reply_routes_to_late_attachment_handling(self):
        original = source._to_news_item({"update_id": 9014, "channel_post": {
            "message_id": 54, "date": 1790360000, "chat": {"id": int(CHANNEL_ID)},
            "text": "خبر منشور سابقًا",
        }}, CHANNEL_ID)
        reply = source._to_news_item({"update_id": 9015, "channel_post": {
            "message_id": 55, "date": 1790360100, "chat": {"id": int(CHANNEL_ID)},
            "text": "https://youtu.be/video123",
            "reply_to_message": {"message_id": 54, "date": 1790360000,
                "chat": {"id": int(CHANNEL_ID)}, "text": "خبر منشور سابقًا"},
        }}, CHANNEL_ID)
        news, late = source.merge_photo_replies_with_news_items(
            [original, reply], existing_source_urls={original["link"]}
        )
        self.assertEqual(news, [original])
        self.assertEqual(late, [reply])

    def test_already_published_story_routes_reply_to_late_path(self):
        original = source._to_news_item(
            {
                "update_id": 9008,
                "channel_post": {
                    "message_id": 49,
                    "date": 1790360000,
                    "chat": {"id": int(CHANNEL_ID)},
                    "text": "عنوان منشور مسبقًا",
                },
            },
            CHANNEL_ID,
        )
        reply = source._to_news_item(
            {
                "update_id": 9009,
                "channel_post": {
                    "message_id": 50,
                    "date": 1790360100,
                    "chat": {"id": int(CHANNEL_ID)},
                    "photo": [{"file_id": "reply-photo", "width": 1200, "height": 900}],
                    "reply_to_message": {
                        "message_id": 49,
                        "date": 1790360000,
                        "chat": {"id": int(CHANNEL_ID)},
                        "text": "عنوان منشور مسبقًا",
                    },
                },
            },
            CHANNEL_ID,
        )
        news, late = source.merge_photo_replies_with_news_items(
            [original, reply],
            existing_source_urls={original["link"]},
        )
        self.assertEqual(len(news), 1)
        self.assertEqual(late, [reply])

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
    def test_pending_twitter_trigger_persists_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as data_dir, mock.patch.dict(
            os.environ, {"BOT_DATA_DIR": data_dir}
        ):
            source._save_pending_twitter_triggers({
                "12345": source.datetime.now(timezone.utc).isoformat(),
            })
            self.assertIn("12345", source._load_pending_twitter_triggers())
            source.clear_pending_twitter_triggers({"12345"})
            self.assertEqual(source._load_pending_twitter_triggers(), {})

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
        self.assertEqual(
            api_call.call_args.args[2]["allowed_updates"],
            '["channel_post","edited_channel_post","message"]',
        )

    def test_fetch_arms_private_twitter_command_for_forwarded_message(self):
        updates = [
            {
                "update_id": 9026,
                "message": {
                    "message_id": 74,
                    "date": 1790360000,
                    "chat": {"id": 12345, "type": "private"},
                    "from": {"id": 12345, "is_bot": False},
                    "text": "تويتر",
                },
            },
            {
                "update_id": 9027,
                "message": {
                    "message_id": 75,
                    "date": 1790360010,
                    "chat": {"id": 12345, "type": "private"},
                    "from": {"id": 12345, "is_bot": False},
                    "forward_origin": {
                        "type": "channel",
                        "chat": {"id": int(CHANNEL_ID), "username": "sourcechannel"},
                        "message_id": 89,
                    },
                    "text": "عنوان محوّل\nتفاصيل المنشور",
                },
            },
        ]
        environment = {
            "TELEGRAM_SOURCE_BOT_TOKEN": "test-source-token",
            "TELEGRAM_SOURCE_CHAT_ID": CHANNEL_ID,
            "SUPABASE_URL": "https://supabase.example",
            "SUPABASE_SERVICE_KEY": "test-service-key",
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(source, "get_last_update_id", return_value=9025),
            mock.patch.object(source, "_ensure_polling_mode"),
            mock.patch.object(source, "_verify_source_channel", return_value="test_bot"),
            mock.patch.object(source, "_get_authorized_private_user_ids", return_value={"12345"}),
            mock.patch.object(source, "_load_pending_twitter_triggers", return_value={}),
            mock.patch.object(source, "_save_pending_twitter_triggers") as save_pending,
            mock.patch.object(source, "_telegram_api_call", return_value={"result": updates}),
        ):
            items, cursor = source.fetch_telegram_items()

        self.assertEqual(cursor, 9027)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://t.me/sourcechannel/89")
        self.assertEqual(
            source.extract_twitter_urgent_body(items[0]["_telegram_original_raw_text"]),
            "عنوان محوّل\nتفاصيل المنشور",
        )
        self.assertEqual(items[0]["_pending_twitter_user_id"], "12345")
        save_pending.assert_called_once()
        self.assertIn("12345", save_pending.call_args.args[0])

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
