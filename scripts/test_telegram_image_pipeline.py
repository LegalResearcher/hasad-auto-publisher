import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hasad_news_bot_fixed as publisher  # noqa: E402
import auto_publish_alittihad_alkhabar as auto_publisher  # noqa: E402


class TelegramImagePipelineTests(unittest.TestCase):
    def test_downloaded_bytes_use_existing_image_pipeline_without_scraping_private_link(self):
        uploaded = []

        def upload(data, filename):
            uploaded.append((data, filename))
            return f"https://storage.example/{filename}"

        with (
            mock.patch.object(publisher, "fetch_og_image", side_effect=AssertionError("must not scrape")),
            mock.patch.object(publisher, "download_image_bytes", side_effect=AssertionError("must use bytes")),
            mock.patch.object(publisher, "image_contains_blocked_logo", return_value=False),
            mock.patch.object(publisher, "compress_image_to_webp", return_value=b"full-webp"),
            mock.patch.object(publisher, "compress_image_to_thumbnail_webp", return_value=b"thumb-webp"),
            mock.patch.object(publisher, "upload_image_to_supabase", side_effect=upload),
        ):
            featured, thumbnail = publisher.get_post_image_urls(
                None,
                "https://t.me/c/1234567890/99",
                headline_text="خبر تجريبي",
                source_image_bytes=b"telegram-image-bytes",
            )

        self.assertTrue(featured.startswith("https://storage.example/"))
        self.assertTrue(thumbnail.startswith("https://storage.example/thumb-"))
        self.assertEqual([data for data, _ in uploaded], [b"full-webp", b"thumb-webp"])

    def test_late_photo_reply_updates_existing_published_article(self):
        reply = {
            "link": f"https://t.me/c/{1234567890}/42",
            "_telegram_photo_file_id": "late-reply-photo",
            "_telegram_reply_to_message_id": 42,
        }
        published = {"id": "post-uuid", "title": "عنوان الخبر"}
        with (
            mock.patch.object(auto_publisher, "get_published_post_by_source_url", return_value=published),
            mock.patch.object(auto_publisher, "download_telegram_photo", return_value=b"photo-bytes"),
            mock.patch.object(auto_publisher, "get_post_image_urls", return_value=("https://image.example/featured", "https://image.example/thumb")) as process_image,
            mock.patch.object(auto_publisher, "update_published_post_cover_image", return_value=True) as update_cover,
        ):
            retry_required = auto_publisher._process_late_telegram_photo_replies([reply])

        self.assertFalse(retry_required)
        process_image.assert_called_once_with(
            None,
            headline_text="عنوان الخبر",
            source_image_bytes=b"photo-bytes",
        )
        update_cover.assert_called_once_with("post-uuid", "https://image.example/featured")

    def test_late_photo_reply_update_failure_preserves_retry(self):
        reply = {
            "link": f"https://t.me/c/{1234567890}/42",
            "_telegram_photo_file_id": "late-reply-photo",
            "_telegram_reply_to_message_id": 42,
        }
        with (
            mock.patch.object(auto_publisher, "get_published_post_by_source_url", return_value={"id": "post-uuid", "title": "عنوان الخبر"}),
            mock.patch.object(auto_publisher, "download_telegram_photo", return_value=b"photo-bytes"),
            mock.patch.object(auto_publisher, "get_post_image_urls", return_value=("https://image.example/featured", None)),
            mock.patch.object(auto_publisher, "update_published_post_cover_image", return_value=False),
        ):
            retry_required = auto_publisher._process_late_telegram_photo_replies([reply])

        self.assertTrue(retry_required)

    def test_late_video_reply_updates_published_video_field(self):
        reply = {
            "link": "https://t.me/c/1234567890/42",
            "_telegram_video_url": "https://youtu.be/video123",
        }
        with (
            mock.patch.object(auto_publisher, "get_published_post_by_source_url", return_value={"id": "post-uuid", "title": "عنوان الخبر"}),
            mock.patch.object(auto_publisher, "update_published_post_video_url", return_value=True) as update_video,
        ):
            retry_required = auto_publisher._process_late_telegram_photo_replies([reply])

        self.assertFalse(retry_required)
        update_video.assert_called_once_with("post-uuid", "https://youtu.be/video123")

    def test_late_video_update_failure_preserves_retry(self):
        reply = {
            "link": "https://t.me/c/1234567890/42",
            "_telegram_video_url": "https://youtu.be/video123",
        }
        with (
            mock.patch.object(auto_publisher, "get_published_post_by_source_url", return_value={"id": "post-uuid", "title": "عنوان الخبر"}),
            mock.patch.object(auto_publisher, "update_published_post_video_url", return_value=False),
        ):
            retry_required = auto_publisher._process_late_telegram_photo_replies([reply])

        self.assertTrue(retry_required)


if __name__ == "__main__":
    unittest.main()
