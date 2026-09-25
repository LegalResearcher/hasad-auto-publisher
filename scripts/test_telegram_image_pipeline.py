import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hasad_news_bot_fixed as publisher  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
