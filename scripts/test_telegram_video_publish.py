import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TEST_TMP = tempfile.TemporaryDirectory(prefix="hasad-telegram-video-test-")
os.environ["BOT_DATA_DIR"] = os.path.join(_TEST_TMP.name, "data")
os.environ["SUPABASE_URL"] = "https://example.invalid"
os.environ["SUPABASE_SERVICE_KEY"] = "unit-test-service-key"
_ORIGINAL_CWD = os.getcwd()
os.chdir(_TEST_TMP.name)
try:
    import hasad_news_bot_fixed as hasad
    import auto_publish_alittihad_alkhabar as publisher
finally:
    os.chdir(_ORIGINAL_CWD)


class TelegramVideoPublishTests(unittest.TestCase):
    video_url = "https://youtu.be/video123"

    def _item(self):
        return {
            "title": "عنوان خبر تجريبي",
            "link": "https://t.me/c/9876543210/42",
            "pub_date": datetime(2026, 9, 26, tzinfo=timezone.utc),
            "raw_body": f"عنوان خبر تجريبي\n\nمتن الخبر.\n{self.video_url}",
            "source_feed": "telegram://-1009876543210",
            "image_url": None,
            "category": "أخبار وتقارير",
            "author": None,
            "_telegram_source": True,
            "_telegram_update_id": 9100,
            "_telegram_photo_file_id": None,
            "_telegram_video_url": self.video_url,
        }

    def _patch_run(self, stack):
        mocked = {}

        def p(name, **kwargs):
            mocked[name] = stack.enter_context(mock.patch.object(publisher, name, **kwargs))
            return mocked[name]

        p("publish_breaking_news", return_value=0)
        p("check_system_logs_size")
        p("check_and_notify_scheduled_posts")
        p("get_existing_source_urls", return_value=set())
        p("load_blocked_links", return_value=set())
        p("get_recent_published_titles", return_value=[])
        p("get_recent_raw_items", return_value=[])
        p("is_telegram_source_configured", return_value=True)
        p("fetch_telegram_items", return_value=([self._item()], 9100))
        p("collect_recent_items", return_value=[])
        p("remove_raw_duplicate_news", side_effect=lambda items, history_items, duplicates_out=None: items)
        p("remove_duplicate_news", side_effect=lambda items, history_items, duplicates_out=None: items)
        p("remove_content_duplicate_news", side_effect=lambda items, history_items, duplicates_out=None: items)
        p("apply_full_extraction")
        p("rewrite_article_with_scope", return_value={
            "title": "عنوان محرر",
            "excerpt": "ملخص محرر",
            "content": "متن محرر كامل.",
            "news_scope": "يمني",
        })
        p("word_stats", return_value=(4, 1))
        p("format_content_paragraphs", return_value="<p>متن محرر كامل.</p>")
        p("get_category_id", return_value="category-id")
        p("sb_insert", return_value="post-id")
        p("get_published_post_by_title", return_value={
            "id": "existing-post-id",
            "title": "العنوان المنشور سابقاً",
            "external_video_url": None,
        })
        p("update_published_post_video_url", return_value=True)
        p("update_published_post_cover_image", return_value=True)
        p("log_published_title")
        p("log_raw_content")
        p("save_blocked_link")
        p("seed_views")
        p("build_canonical_url", return_value="https://hasad.example/article")
        p("send_to_telegram", return_value=True)
        p("log_discovery_ready")
        p("commit_telegram_cursor")
        return mocked

    def test_video_url_is_saved_in_post_record_and_not_editorial_fields(self):
        with ExitStack() as stack:
            mocked = self._patch_run(stack)
            publisher.run()

        record = mocked["sb_insert"].call_args.args[0]
        self.assertEqual(record["external_video_url"], self.video_url)
        for field in ("title", "excerpt", "content"):
            self.assertNotIn(self.video_url, record[field])

    def test_rewriter_prompt_keeps_editorial_rules_and_strips_video_url_from_all_fields(self):
        model_result = {
            "title": f"عنوان محرر {self.video_url}",
            "excerpt": f"ملخص {self.video_url}",
            "content": f"متن الخبر.\n{self.video_url}",
            "news_scope": "يمني",
        }
        with (
            mock.patch.object(publisher, "build_prompt", return_value="النص التحريري الأصلي") as build_prompt,
            mock.patch.object(publisher, "call_with_rotation", return_value=json.dumps(model_result, ensure_ascii=False)) as call,
        ):
            result = publisher.rewrite_article_with_scope(
                "عنوان المصدر", "نص المصدر", "أخبار وتقارير", video_url=self.video_url
            )

        build_prompt.assert_called_once_with("عنوان المصدر", "نص المصدر", "أخبار وتقارير")
        self.assertIn("النص التحريري الأصلي", call.call_args.args[0])
        self.assertIn("رابط الفيديو محفوظ في حقل خارجي", call.call_args.args[0])
        for field in ("title", "excerpt", "content"):
            self.assertNotIn(self.video_url, result[field])

    def test_duplicate_telegram_video_updates_published_post_without_republishing(self):
        item = self._item()

        def mark_duplicate(items, history_items, duplicates_out=None):
            if duplicates_out is not None:
                item["_duplicate_match_title"] = "العنوان المنشور سابقاً"
                duplicates_out.append(item)
            return []

        with ExitStack() as stack:
            mocked = self._patch_run(stack)
            mocked["fetch_telegram_items"].return_value = ([item], 9100)
            mocked["remove_raw_duplicate_news"].side_effect = mark_duplicate
            publisher.run()

        mocked["get_published_post_by_title"].assert_called_once_with("العنوان المنشور سابقاً")
        mocked["update_published_post_video_url"].assert_called_once_with(
            "existing-post-id", self.video_url
        )
        mocked["sb_insert"].assert_not_called()
        mocked["commit_telegram_cursor"].assert_called_once_with(9100)

    def test_all_dedup_layers_return_historical_telegram_media_match(self):
        now = datetime.now(timezone.utc)
        item = self._item()
        item["pub_date"] = now
        historical_title = "العنوان المنشور سابقاً"
        title_duplicates = []
        with (
            mock.patch.object(hasad, "get_title_embedding", return_value=[1.0, 0.0]),
            mock.patch.object(hasad, "_cosine_similarity", return_value=0.99),
        ):
            kept = hasad.remove_duplicate_news(
                [item],
                history_items=[{"title": historical_title, "pub_date": now, "embedding": [1.0, 0.0]}],
                duplicates_out=title_duplicates,
            )
        self.assertEqual(kept, [])
        self.assertEqual(title_duplicates[0]["_duplicate_match_title"], historical_title)

        raw_item = self._item()
        raw_item["pub_date"] = now
        raw_duplicates = []
        with (
            mock.patch.object(hasad, "DATASKETCH_AVAILABLE", False),
            mock.patch.object(hasad, "_text_similarity", return_value=0.99),
        ):
            kept = hasad.remove_raw_duplicate_news(
                [raw_item],
                history_items=[{
                    "title": historical_title,
                    "pub_date": now,
                    "norm_raw": hasad._normalize_raw_for_dedup(raw_item["raw_body"]),
                }],
                duplicates_out=raw_duplicates,
            )
        self.assertEqual(kept, [])
        self.assertEqual(raw_duplicates[0]["_duplicate_match_title"], historical_title)

        content_item = self._item()
        content_item["pub_date"] = now
        content_duplicates = []
        with (
            mock.patch.object(hasad, "_extract_entities", return_value={"shared-entity"}),
            mock.patch.object(hasad, "get_content_embedding", return_value=[1.0, 0.0]),
            mock.patch.object(hasad, "_cosine_similarity", return_value=0.99),
        ):
            kept = hasad.remove_content_duplicate_news(
                [content_item],
                history_items=[{
                    "title": historical_title,
                    "pub_date": now,
                    "content_embedding": [1.0, 0.0],
                    "entities": ["shared-entity"],
                }],
                duplicates_out=content_duplicates,
            )
        self.assertEqual(kept, [])
        self.assertEqual(content_duplicates[0]["_duplicate_match_title"], historical_title)

    def test_duplicate_telegram_video_merges_into_kept_item_from_same_batch(self):
        now = datetime.now(timezone.utc)
        primary = self._item()
        primary.update({
            "title": "العنوان الأساسي",
            "pub_date": now,
            "_telegram_source": False,
            "_telegram_video_url": None,
        })
        duplicate = self._item()
        duplicate.update({"title": "عنوان بصياغة أخرى", "pub_date": now})
        with (
            mock.patch.object(hasad, "get_title_embedding", return_value=[1.0, 0.0]),
            mock.patch.object(hasad, "_cosine_similarity", return_value=0.99),
        ):
            kept = hasad.remove_duplicate_news([primary, duplicate], history_items=[])

        self.assertEqual(kept, [primary])
        self.assertEqual(primary["_telegram_video_url"], self.video_url)


if __name__ == "__main__":
    unittest.main()
