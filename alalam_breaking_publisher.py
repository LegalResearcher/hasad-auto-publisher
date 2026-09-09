#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""نشر الأخبار العاجلة من alalam.ir/urgent إلى حصاد اليوم دون إعادة صياغة."""

import hashlib
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json
import requests
from bs4 import BeautifulSoup
from urllib3.exceptions import InsecureRequestWarning

from hasad_news_bot_fixed import (
    SUPABASE_SERVICE_KEY,
    SUPABASE_URL,
    TABLE_NAME,
    get_category_id,
    get_existing_source_urls,
    make_slug,
    sb_insert,
    seed_views,
)

BREAKING_NEWS_URL = "https://www.alalam.ir/urgent"
BREAKING_CATEGORY = "الأخبار العاجلة"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_PUBLISH_ENABLED = False  # إيقاف نشر الأخبار العاجلة إلى تيليجرام فقط
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "@hasadalyoum")
REQUEST_TIMEOUT = 30
TICKER_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "breaking-news.json")
# شهادة alalam.ir منتهية حالياً؛ تعطيل التحقق لهذا المصدر وحده مؤقتاً
# حتى لا يتوقف التقاط الأخبار العاجلة قبل تجديد شهادة الموقع.
SOURCE_VERIFY_TLS = False
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
HISTORY_FILE = Path(__file__).with_name("alalam_breaking_history.json")
MAX_HISTORY_ITEMS = 500
SEND_DELAY_SECONDS = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
TIME_PATTERN = re.compile(
    r"^قبل\s+(\d+\s+)?(دقيقة|دقيقتين|دقائق|ساعة|ساعتين|ساعات|يوم|يومين|أيام)$"
)
NAV_NOISE = {
    "الرئيسية", "أخبار", "شرق أوسط", "عالم", "رياضة", "الذكاء الاصطناعي",
    "منوعات", "فيديو", "برامجنا", "حديث الصور", "إنفوغرافيك", "ملفات",
    "البث المباشر", "راديو", "آخر الأخبار", "الأكثر قراءة", "اخترنا لكم",
    "تقارير خاصة", "علوم", "تكنولوجيا", "الأخبار العاجلة", "المزيد", "عاجل", "مباشر",
}


def load_history() -> set[str]:
    try:
        return set(json.loads(HISTORY_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_history(history: set[str]) -> None:
    HISTORY_FILE.write_text(
        json.dumps(list(history)[-MAX_HISTORY_ITEMS:], ensure_ascii=False),
        encoding="utf-8",
    )


def item_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def fetch_page_lines() -> list[str]:
    response = requests.get(
        BREAKING_NEWS_URL,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
        verify=SOURCE_VERIFY_TLS,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return [line.strip() for line in soup.get_text("\n").split("\n") if line.strip()]


def extract_items(lines: list[str]) -> list[dict[str, str | None]]:
    items: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        if not TIME_PATTERN.match(line):
            continue
        headline = summary = None
        if index >= 2:
            candidate, candidate_summary = lines[index - 2], lines[index - 1]
            if (
                not TIME_PATTERN.match(candidate)
                and candidate not in NAV_NOISE
                and len(candidate) > 8
                and not TIME_PATTERN.match(candidate_summary)
            ):
                headline, summary = candidate, candidate_summary
        if headline is None and index >= 1:
            candidate = lines[index - 1]
            if not TIME_PATTERN.match(candidate) and candidate not in NAV_NOISE and len(candidate) > 8:
                headline = candidate
        if not headline:
            continue
        headline = re.sub(r"\s*(المزيد|اقرأ المزيد)\s*$", "", headline).strip()
        if headline in seen:
            continue
        seen.add(headline)
        items.append({"headline": headline, "summary": summary, "time_label": line})
    return items


def update_breaking_ticker(headlines: list[str]) -> None:
    """يحفظ آخر ثلاثة عناوين في ملف ثابت خارج Supabase."""
    headlines = [headline.strip() for headline in headlines if headline and headline.strip()]
    headlines = headlines[:3]
    if not headlines:
        return
    try:
        with open(TICKER_JSON_PATH, encoding="utf-8") as file:
            current = json.load(file)
        if current.get("items") == headlines:
            return
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(), "items": headlines}
    with open(TICKER_JSON_PATH, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def send_to_telegram(headline: str) -> None:
    if not TELEGRAM_PUBLISH_ENABLED or not TELEGRAM_BOT_TOKEN:
        return
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHANNEL_ID,
            "text": f"<b>🔴 عاجل</b>\n\n<b>⭕️ {html.escape(headline)}</b>",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200 or not response.json().get("ok"):
        raise RuntimeError(f"Telegram send failed [{response.status_code}]: {response.text[:300]}")


def publish_item(item: dict[str, str | None], category_id: str, existing_urls: set[str], history: set[str]) -> bool:
    headline = str(item["headline"] or "").strip()
    summary = str(item.get("summary") or "").strip()
    source_url = f"{BREAKING_NEWS_URL}#item-{item_hash(headline)[:16]}"
    fingerprint = item_hash(headline)
    if not headline or fingerprint in history or source_url in existing_urls:
        return False

    # النص الخام كما استُخرج من الصفحة؛ لا Gemini ولا format_content_paragraphs.
    raw_content = headline + (f"\n\n{summary}" if summary else "")
    now = datetime.now(timezone.utc).isoformat()
    words = len(raw_content.split())
    record: dict[str, Any] = {
        "title": headline,
        "slug": make_slug(headline),
        "excerpt": summary or headline,
        "content": raw_content,
        "category_id": category_id,
        "source_type": "العالم | أخبار عاجلة",
        "source_url": source_url,
        "status": "published",
        "word_count": words,
        "reading_time": 1,
        "created_at": now,
        "updated_at": now,
        "published_at": now,
        "featured_image": None,
        "thumbnail_image": None,
        "is_featured": False,
        "is_breaking": True,
    }
    post_id = sb_insert(record)
    if not post_id:
        return False
    seed_views(post_id)
    history.add(fingerprint)
    existing_urls.add(source_url)
    send_to_telegram(headline)
    return True


def publish_breaking_news() -> int:
    category_id = get_category_id(BREAKING_CATEGORY)
    if not category_id:
        raise RuntimeError(f"Category not found: {BREAKING_CATEGORY}")
    items = list(reversed(extract_items(fetch_page_lines())))
    history = load_history()
    existing_urls = get_existing_source_urls()
    published = 0
    published_headlines: list[str] = []
    for item in items:
        if publish_item(item, category_id, existing_urls, history):
            published += 1
        headline = str(item.get("headline") or "").strip()
        if headline:
            published_headlines.append(headline)
    if published_headlines:
        # items مرتبة من الأقدم إلى الأحدث؛ اعرض أحدث ثلاثة فقط، والأحدث أولاً.
        latest_headlines = list(reversed(published_headlines[-3:]))
        update_breaking_ticker(latest_headlines)
    save_history(history)
    print(f"Alalam breaking publisher: {published} new item(s) published")
    return published


def main() -> None:
    publish_breaking_news()


if __name__ == "__main__":
    main()
