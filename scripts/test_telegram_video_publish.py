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
        p("remove_raw_duplicate_news", side_effect=lambda items, history_items: items)
        p("remove_duplicate_news", side_effect=lambda items, history_items: items)
        p("remove_content_duplicate_news", side_effect=lambda items, history_items: items)
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


if __name__ == "__main__":
    unittest.main()
