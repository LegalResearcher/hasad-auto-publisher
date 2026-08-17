"""
hasad_news_bot.py
──────────────────────────────────────────────────────────────────────
يسحب أخبار آخر 24 ساعة من روابط RSS محددة، يعيد صياغتها عبر Gemini
بما يطابق الهوية التحريرية المستقلة لـ "حصاد اليوم"،
وينشرها تلقائياً في جدول posts.

المتطلبات:
    pip install requests pillow beautifulsoup4

طريقة الاستخدام:
    1. عبّئ قسم الإعدادات بالأسفل (RSS_FEEDS).
    2. نفّذ ملف SQL المرفق مرة واحدة في Supabase SQL Editor.
    3. اضبط متغيرات البيئة: SUPABASE_ANON_KEY, TELEGRAM_BOT_TOKEN, GEMINI_API_KEYS
    4. شغّل: python hasad_news_bot_fixed.py
"""

from __future__ import annotations

import difflib
import html
import io
import json
import logging
import math
import os
import random
import re
import sqlite3
import string
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

# ══════════════════════════════════════════════════════════════════════
#  ⚡ تحسينات أداء اختيارية لكشف التكرار: rapidfuzz (تشابه نصي أسرع بكثير
#  من difflib) و datasketch (MinHash/LSH لتفادي مقارنة كل خبر بكل خبر
#  آخر عند كبر حجم الأرشيف). كلاهما اختياري تماماً — لو غير مثبَّتين،
#  يعمل الكود بنفس المنطق القديم (difflib + مقارنة تسلسلية كاملة) بدون
#  أي كسر بالتشغيل.
#  للتثبيت داخل Termux:
#      pip install rapidfuzz datasketch numpy --break-system-packages
# ══════════════════════════════════════════════════════════════════════
try:
    from rapidfuzz import fuzz as _rapidfuzz_fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False

try:
    from datasketch import MinHash, MinHashLSH
    import numpy as np
    DATASKETCH_AVAILABLE = True
except ImportError:
    DATASKETCH_AVAILABLE = False


def _text_similarity(a: str, b: str) -> float:
    """تشابه نصي بين 0 و1. يستخدم rapidfuzz لو متوفر (أسرع بعشرات
    المرات من difflib على النصوص العربية الطويلة)، ويرجع تلقائياً لـ
    difflib لو rapidfuzz غير مثبّت."""
    if not a or not b:
        return 0.0
    if RAPIDFUZZ_AVAILABLE:
        return _rapidfuzz_fuzz.ratio(a, b) / 100.0
    return difflib.SequenceMatcher(None, a, b).ratio()

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = None
    ImageDraw = None
    ImageFont = None

try:
    import arabic_reshaper
    from bidi.algorithm import get_display as _bidi_get_display
except ImportError:  # pragma: no cover
    arabic_reshaper = None
    _bidi_get_display = None

# ══════════════════════════════════════════════════════════════════════
#  ⚙️  الإعدادات — عدّل هنا فقط
# ══════════════════════════════════════════════════════════════════════

ADEN_TM_ENABLED = True

RSS_FEED_CATEGORIES = {
    "/storage/emulated/0/Download/hasad_bot/aden-tm-akhbar-wataqarir.xml": "أخبار وتقارير",
    "/storage/emulated/0/Download/hasad_bot/aden-tm-akhbar-aden.xml": "أخبار محلية",
    "/storage/emulated/0/Download/hasad_bot/aden-tm-riyada.xml": "رياضة",
    "/storage/emulated/0/Download/hasad_bot/aden-tm-kitabat.xml": "آراء واتجاهات",
}

RSS_FEEDS = list(RSS_FEED_CATEGORIES.keys())

RSS_MASA_URL = "https://masa-press.net/category/اهم-الاخبار/feed/"
RSS_MASA_CATEGORY = "أخبار وتقارير"

RSS_ADEN_TM_FULL_URL = "https://www.aden-tm.net/feed"
RSS_ADEN_TM_FULL_CATEGORY = "أخبار وتقارير"

RSS_ALITTIHAD_FULL_URL = "https://alittihadpress.com/rss.php?topic=1"
RSS_ALITTIHAD_FULL_CATEGORY = "أخبار وتقارير"

RSS_ALKHABAR_FULL_URL = "https://alkhabaralyemeni.net/category/%d8%a7%d8%ae%d8%a8%d8%a7%d8%b1-%d8%a7%d9%84%d9%88%d8%b7%d9%86/feed/"
RSS_ALKHABAR_FULL_CATEGORY = "أخبار وتقارير"

RSS_YPAGENCY_FULL_URL = "https://www.ypagency.net/feed/"
RSS_YPAGENCY_FULL_CATEGORY = "أخبار وتقارير"

BLOCKED_KEYWORDS = ["مواقيت الأذان", "مليشيا"]

# BOT_DATA_DIR: مجلد بيانات محلي قابل للتهيئة عبر متغير بيئة — على GitHub
# Actions يُحافَظ عليه بين التشغيلات عبر actions/cache (راجع ملف الجدولة)
# لأن هذا البوت يعتمد على SQLite محلي لفحص التشابه الدلالي بين العناوين.
BASE_DIR = os.environ.get(
    "BOT_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "hasad_data"),
)
os.makedirs(BASE_DIR, exist_ok=True)

BLOCKED_LINKS_FILE = os.path.join(BASE_DIR, "blocked_links.json")

PENDING_SCHEDULED_FILE = os.path.join(BASE_DIR, "pending_scheduled.json")

# ملف أسرار محلي على الجهاز (لا يُشارك أبداً مع هذا السكربت) — يُستخدم فقط
# لو متغيرات البيئة (export) غير مضبوطة، وهو الوضع الشائع بتطبيقات مثل
# Pydroid3 على أندرويد اللي زر "تشغيل" فيها لا يورّث متغيرات export من
# تطبيق Terminal. أنشئ هذا الملف مرة واحدة بمحتوى مثل:
#   {"SUPABASE_ANON_KEY": "...", "TELEGRAM_BOT_TOKEN": "...", "GEMINI_API_KEYS": "key1,key2,key3"}
SECRETS_FILE = os.path.join(BASE_DIR, "secrets.json")

_LOCAL_SECRETS_CACHE: Optional[dict] = None


def _load_local_secrets() -> dict:
    global _LOCAL_SECRETS_CACHE
    if _LOCAL_SECRETS_CACHE is not None:
        return _LOCAL_SECRETS_CACHE
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        _LOCAL_SECRETS_CACHE = data if isinstance(data, dict) else {}
    except FileNotFoundError:
        _LOCAL_SECRETS_CACHE = {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️  تعذّر قراءة ملف الأسرار المحلي ({SECRETS_FILE}): {e}")
        _LOCAL_SECRETS_CACHE = {}
    return _LOCAL_SECRETS_CACHE


def _secret(key: str, default: str = "") -> str:
    """يبحث أولاً بمتغيرات البيئة (الأولوية دائماً لها لو مضبوطة)، ثم
    بملف SECRETS_FILE المحلي كخيار احتياطي — يحل مشكلة عدم توريث Pydroid3
    لمتغيرات export."""
    val = os.getenv(key, "").strip()
    if val:
        return val
    return str(_load_local_secrets().get(key, default)).strip()


# بيانات Lovable Cloud الخاصة بموقع حصاد اليوم (مفتاح publishable/anon)
SUPABASE_URL = (_secret("SUPABASE_URL") or "https://utalkwuzxoygsjiwncbo.supabase.co").rstrip("/")
SUPABASE_ANON_KEY = _secret("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = _secret("SUPABASE_SERVICE_KEY") or SUPABASE_ANON_KEY  # يقرأ الآن service_role الحقيقي من السر، مع رجوع احتياطي إلى anon فقط إن لم يوجد.

CATEGORY_OPTIONS = {
    "1": "أخبار وتقارير",
    "2": "شؤون دولية",
    "3": "آراء واتجاهات",
    "4": "رياضة",
    "5": "أخبار محلية",
    "6": "اليمن في الصحافة",
}

FEATURED_SLIDER_CATEGORIES = {"أخبار وتقارير"}

NEWS_FILES = []

HOURS_WINDOW = 24

NO_REWRITE_CATEGORIES = {"آراء واتجاهات"}

NO_IMAGE_CATEGORIES = set()

DEFAULT_OPINION_AUTHOR = "كتّاب عدن تايم"

SOURCE_LABEL = "حصاد اليوم | متابعات"

TABLE_NAME = "posts"
MAX_RETRIES = 6
MAX_BACKOFF = 60
REQUEST_TIMEOUT = 60

SUPABASE_IMAGE_BUCKET = "post-images"
IMAGE_MAX_DIMENSION = 1200
IMAGE_TARGET_MAX_BYTES = 100 * 1024
IMAGE_START_QUALITY = 85
IMAGE_MIN_QUALITY = 30
IMAGE_QUALITY_STEP = 5

# نسخة الصورة المصغّرة (thumbnail_image) — تُستخدم في بطاقات القوائم بالواجهة
# (الرئيسية/الأقسام/الأكثر قراءة) بدل الصورة الكاملة، بنفس منطق ونسب
# imageOptimizer.ts بالفرونت إند، لتقليل Storage Egress على Supabase.
THUMB_MAX_DIMENSION = 400
THUMB_TARGET_MAX_BYTES = 25 * 1024
THUMB_START_QUALITY = 80
THUMB_MIN_QUALITY = 30
THUMB_QUALITY_STEP = 5

BLOCKED_LOGOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocked_logos")
LOGO_HASH_SIZE = 8
LOGO_MATCH_MAX_DISTANCE = 6

# ── تصميم صورة العنوان (شريط سفلي احترافي بخط مستقيم بهوية أسود/ذهبي —
#    مطابقة لهيدر www.hasad-alyoum.com: خلفية سوداء، شعار "24" الدائري
#    الذهبي، واسم الموقع بالذهبي. أسلوب القنوات العالمية: خط علوي مستقيم +
#    تدرّج تعتيم ناعم فوق الصورة بدل الموجة، وفاصل رفيع خفيف بين كتلة
#    الشعار وكتلة العنوان بدل خط عمودي ثقيل بين الشعار والاسم) ───────────
HEADLINE_DESIGN_ENABLED = False
HEADLINE_SITE_NAME = "حصاد اليوم"
HEADLINE_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo-24.png")
HEADLINE_FONT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fonts", "Amiri-Bold.ttf"
)

HEADLINE_OG_WIDTH = 1200
HEADLINE_OG_HEIGHT = 630

HEADLINE_BAND_COLOR_TOP = (8, 8, 10, 255)         # أسود عميق (أعلى الشريط)
HEADLINE_BAND_COLOR_BOTTOM = (22, 22, 24, 255)    # أسود فحمي أفتح قليلاً (أسفل الشريط)
HEADLINE_CURVE_COLOR = (197, 160, 76, 255)        # ذهبي — الخط المستقيم العلوي للشريط
HEADLINE_TEXT_COLOR = (250, 250, 248, 255)        # أبيض للعنوان (وضوح على الأسود)
HEADLINE_SITE_COLOR = (212, 175, 90, 255)         # ذهبي لاسم الموقع (نفس لون HASAD AL-YOUM)
HEADLINE_DIVIDER_COLOR = (255, 255, 255, 60)      # فاصل رفيع خفيف بين كتلة الشعار وكتلة العنوان
HEADLINE_ACCENT_COLOR = (196, 155, 58, 255)       # ذهبي — الشريط العمودي الصغير (kicker) جنب العنوان
HEADLINE_LINE_THICKNESS = 3                       # سمك الخط المستقيم العلوي
HEADLINE_FADE_H = 90                              # ارتفاع تدرّج التعتيم فوق الشريط (يذوب داخل الصورة)
HEADLINE_FONT_SIZE = 54
HEADLINE_FONT_MIN_SIZE = 30       # أصغر حجم خط مسموح قبل تكبير الشريط بدل قصّ النص
HEADLINE_MAX_LINES = 2            # عدد الأسطر "المفضّل" — العنوان الطويل يتجاوزه بدل أن يُقصّ
HEADLINE_MIN_PHOTO_VISIBLE = 90   # أقل ارتفاع من الصورة الأصلية يبقى ظاهراً فوق الشريط دائماً
HEADLINE_MIN_ASPECT_COVERAGE = 0.75  # أقل نسبة من الصورة الأصلية يجب أن تبقى بعد القص لنسبة OG؛
                                       # لو القص سيحذف أكثر من (1 - هذه النسبة)، يُتخطى تصميم
                                       # الشريط بالكامل وتُنشر الصورة العادية المضغوطة بأبعادها
                                       # الطبيعية بدون أي قص.

TELEGRAM_ENABLED = True
TELEGRAM_BOT_TOKEN = _secret("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = "@hasadalyoum"

ADMIN_TELEGRAM_CHAT_ID = "85820797"
SYSTEM_LOGS_ALERT_THRESHOLD = 50_000

SITE_BASE_URL = "https://www.hasad-alyoum.com"


def send_to_telegram(title: str, article_url: str) -> bool:
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN:
        return False
    text = (
        f"{title}\n\n"
        f'أقرأ التفاصيل من "حصاد اليوم": {article_url}\n\n'
        f"📲 تابعونا على:  ⤵\n\n"
        f"✅ تيليجرام: https://t.me/hasadalyoum"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return True
        log.warning(f"  ⚠️  فشل إرسال تليجرام [{r.status_code}]: {r.text[:200]}")
        return False
    except requests.RequestException as e:
        log.warning(f"  ⚠️  خطأ إرسال تليجرام: {e}")
        return False


def send_admin_alert(text: str) -> bool:
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN or not ADMIN_TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ADMIN_TELEGRAM_CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return True
        log.warning(f"  ⚠️  فشل إرسال تنبيه إداري [{r.status_code}]: {r.text[:200]}")
        return False
    except requests.RequestException as e:
        log.warning(f"  ⚠️  خطأ إرسال تنبيه إداري: {e}")
        return False


AUTO_SEED_VIEWS = True
NEWS_REPORTS_CATEGORY_NAME = "أخبار وتقارير"


def seed_views(post_id: str) -> None:
    if not AUTO_SEED_VIEWS or not post_id:
        return
    try:
        url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}?id=eq.{post_id}&select=id,views_count,created_at,category_id"
        r = requests.get(url, headers=sb_headers(), timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log.warning(f"  ⚠️  تعذّر جلب بيانات الخبر لتحديث المشاهدات [{r.status_code}]: {r.text[:300]}")
            return
        if not r.json():
            log.warning(f"  ⚠️  تعذّر جلب بيانات الخبر لتحديث المشاهدات: لم يُعثر على السجل بالجدول (id={post_id})")
            return
        post = r.json()[0]
        current = post.get("views_count") or 0
        created_at = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff_min = (now - created_at).total_seconds() / 60

        # تحديد ما إذا كان الخبر ضمن قسم "أخبار وتقارير" — بنفس فرع المنطق المطبّق
        # في الموقع (JsonNewsImporter/PostEditor/Maintenance) بدل تطبيق نطاق هذا
        # القسم على كل الأقسام كما كان يحدث سابقاً
        news_reports_id = get_category_id(NEWS_REPORTS_CATEGORY_NAME)
        is_news_reports = bool(news_reports_id) and post.get("category_id") == news_reports_id

        if is_news_reports:
            # المنطق الأصلي: يُطبّق فقط على قسم "أخبار وتقارير"
            if current < 150:
                if diff_min < 60:
                    final = random.randint(150, 388)
                elif diff_min < 300:
                    final = random.randint(455, 700)
                else:
                    final = random.randint(600, 1500)
            else:
                final = current + random.randint(10, 59)
        else:
            # نفس بنية المنطق (تقسيم زمني ثلاثي) لكن بنطاقات مصغّرة ضمن 126-683 لباقي الأقسام
            if current < 126:
                if diff_min < 60:
                    final = random.randint(126, 250)
                elif diff_min < 300:
                    final = random.randint(251, 450)
                else:
                    final = random.randint(451, 683)
            else:
                final = min(683, current + random.randint(5, 25))

        patch_url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}?id=eq.{post_id}"
        pr = requests.patch(patch_url, headers=sb_headers(), json={"views_count": final}, timeout=REQUEST_TIMEOUT)
        if pr.status_code not in (200, 204):
            log.warning(f"  ⚠️  فشل تحديث المشاهدات [{pr.status_code}]: {pr.text[:200]}")
        else:
            log.info(f"  👁️  تحسين المشاهدات ({final})")
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning(f"  ⚠️  خطأ تحديث المشاهدات: {e}")


YEMEN_TZ = timezone(timedelta(hours=3))


def build_canonical_url(slug: str, published_at_iso: str) -> str:
    """يبني الرابط الكنسي من وقت النشر الفعلي، مع الإبقاء على created_at كبديل."""
    dt = datetime.fromisoformat(published_at_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(YEMEN_TZ)
    return f"{SITE_BASE_URL}/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/{slug}"


def log_discovery_ready(urls: list) -> None:
    """يسجل جاهزية الخبر للاكتشاف دون ادعاء فهرسة فورية من Google.

    المقالات المنشورة تظهر في Sitemap وNews Sitemap وRSS الديناميكية. لا نستخدم
    Google Indexing API للأخبار العادية لأنها ليست ضمن نطاقها المدعوم.
    """
    if urls:
        log.info(f"  🔎 جاهز للاكتشاف عبر الرابط الكنسي وSitemap وRSS ({len(urls)} رابط)")

# ══════════════════════════════════════════════════════════════════════
#  🔑 المفاتيح ذات الأولوية — تُجرَّب أولاً وبنفس هذا الترتيب تماماً:
#  المفتاح الأول، ثم الثاني، ثم الثالث (بنفس منطق التدوير ونفس
#  MODEL_CASCADE الحالي دون أي تعديل). بعد استنفاد الثلاثة على النموذج
#  الحالي، تُستكمل القائمة تلقائياً بأي مفاتيح أخرى معرّفة سابقاً عبر
#  GEMINI_API_KEYS (متغير بيئة أو secrets.json)، دون تكرار.
# ══════════════════════════════════════════════════════════════════════
PRIORITY_GEMINI_KEYS = [
    k.strip() for k in _secret("GEMINI_PRIORITY_KEYS").split(",") if k.strip()
]

_extra_configured_keys = [k.strip() for k in _secret("GEMINI_API_KEYS").split(",") if k.strip()]
_extra_configured_keys = list(reversed(_extra_configured_keys))

GEMINI_API_KEYS = PRIORITY_GEMINI_KEYS + [
    k for k in _extra_configured_keys if k not in PRIORITY_GEMINI_KEYS
]

# ══════════════════════════════════════════════════════════════════════
#  🔑 مجموعات المفاتيح لمنطق call_with_rotation: المجموعة الأولى هي
#  المفاتيح الثلاثة الجديدة ذات الأولوية، والمجموعة الثانية هي باقي
#  المفاتيح القديمة (بدون تكرار). تُستنفد المجموعة الأولى بالكامل عبر
#  كل مراحل MODEL_CASCADE أولاً، وفقط بعدها تبدأ المجموعة الثانية دورتها
#  الكاملة الخاصة عبر كل النماذج من جديد. المجموعات الفارغة تُستبعد.
# ══════════════════════════════════════════════════════════════════════
KEY_GROUPS = [
    grp for grp in (
        PRIORITY_GEMINI_KEYS,
        [k for k in _extra_configured_keys if k not in PRIORITY_GEMINI_KEYS],
    ) if grp
]

# ══════════════════════════════════════════════════════════════════════
#  🔑 منطق تدوير المفاتيح/النماذج (تدوير على مراحل متعددة، وليس مرحلتين
#  فقط كما كان سابقاً):
#
#  كل مرحلة تمثّل نموذجاً واحداً من MODEL_CASCADE بالترتيب. يبدأ بالمفتاح
#  الأول + أول نموذج بالقائمة (PRIMARY_MODEL). عند انتهاء حصة مفتاح معيّن،
#  ينتقل للمفتاح التالي **بنفس النموذج الحالي** — يستمر كذلك حتى المفتاح
#  الأخير.
#
#  عند استُنفاد حصة النموذج الحالي على كل المفاتيح العشرة، ينتقل للنموذج
#  التالي بالقائمة (MODEL_CASCADE) بدءاً من المفتاح الأول من جديد، وهكذا
#  حتى آخر نموذج بالقائمة. لو استُنفدت حصته أيضاً على كل المفاتيح، تُرفع
#  الاستثناء نهائياً (لا مزيد من الخيارات لهذا التشغيل).
#
#  بداية كل تشغيل جديد للسكريبت (تشغيل تالٍ عبر cron مثلاً) تبدأ دائماً
#  من الصفر (المفتاح الأول + أول نموذج بالقائمة) تلقائياً، لأن الحالة
#  (_current_key_idx وَ_model_stage_idx) متغيرات وحدة عادية تُهيَّأ من
#  جديد مع كل تشغيل مستقل للعملية (process)، ولا تُحفظ بين التشغيلات.
# ══════════════════════════════════════════════════════════════════════

PRIMARY_MODEL = "gemini-3.6-flash"
FALLBACK_MODEL = "gemini-3.5-flash"

# ترتيب تجربة النماذج مُحدَّث (2026-08-12) ليعكس الترتيب الفعلي بالقوة
# والحداثة بدل ترتيب SKU القديم: يبدأ بأحدث وأقوى نموذج flash متاح
# (3.6)، ثم 3.5، ثم 2.5 (الجيل الأقدم)، ثم نسخ flash-lite الاقتصادية
# من الأحدث للأقدم، وأخيراً gemini-2.5-pro كملاذ أخير (أبطأ وأغلى، لكنه
# لا يزال يعمل لو استُنفدت كل خيارات flash على كل المفاتيح).
MODEL_CASCADE = [
    PRIMARY_MODEL,          # gemini-3.6-flash
    FALLBACK_MODEL,         # gemini-3.5-flash
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
]

_current_group_idx = 0
_current_key_idx = 0
_model_stage_idx = 0


def current_model() -> str:
    return MODEL_CASCADE[_model_stage_idx]


def current_key() -> str:
    return KEY_GROUPS[_current_group_idx][_current_key_idx]


def model_url() -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{current_model()}:generateContent"
    )


# ══════════════════════════════════════════════════════════════════════
#  🧬 متجهات Gemini (embeddings) لعناوين الأخبار — تُستخدم لكشف تكرار
#  الخبر عبر مصادر مختلفة اعتماداً على التشابه الدلالي للمعنى، وليس فقط
#  التطابق الحرفي بالنص (زي difflib سابقاً). نطلب المتجه الكامل من
#  gemini-embedding-001 ثم نقصّه لأول EMBEDDING_DIM بعد فقط قبل التخزين
#  (تقليم متجهات Matryoshka بهذا الموديل يبقى دقيقاً لحساب تشابه جيب
#  التمام cosine similarity حتى بدون إعادة تطبيع، فيوفر مساحة تخزين
#  بالسجل المحلي دون فقدان دقة تُذكر لغرض كشف التكرار).
# ══════════════════════════════════════════════════════════════════════

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 256
EMBEDDING_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent"
)

_embedding_key_idx = 0


def get_title_embedding(title: str) -> Optional[list[float]]:
    """يجيب متجه Gemini embedding لعنوان خبر (لاستخدامه بكشف التكرار
    الدلالي). يدور تلقائياً على مفاتيح GEMINI_API_KEYS المتاحة عند فشل
    مفتاح معيّن (429 أو خطأ اتصال)، بتدوير مستقل تماماً عن تدوير مفاتيح
    توليد المقالات (call_with_rotation) حتى لا يتداخل معه."""
    global _embedding_key_idx
    title = (title or "").strip()
    if not title:
        return None

    body = {
        "model": f"models/{EMBEDDING_MODEL}",
        "content": {"parts": [{"text": title}]},
        "taskType": "SEMANTIC_SIMILARITY",
    }

    for _ in range(len(GEMINI_API_KEYS)):
        key = GEMINI_API_KEYS[_embedding_key_idx % len(GEMINI_API_KEYS)]
        try:
            RATE_LIMITER.wait()
            resp = requests.post(EMBEDDING_URL, params={"key": key}, json=body, timeout=30)
            if resp.status_code == 429:
                _embedding_key_idx += 1
                continue
            resp.raise_for_status()
            values = resp.json().get("embedding", {}).get("values")
            if not values:
                return None
            return values[:EMBEDDING_DIM]
        except requests.RequestException as e:
            log.warning(f"  ⚠️  فشل جلب embedding للعنوان: {e}")
            _embedding_key_idx += 1

    log.warning("  ⚠️  تعذّر جلب embedding بكل المفاتيح المتاحة — سيُستخدم التشابه النصي كاحتياط.")
    return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class DailyQuotaExceeded(Exception):
    pass


class ModelUnavailable(Exception):
    """يُرفع عند 404 (النموذج غير متاح/غير مفعّل لهذا المفتاح تحديداً)."""
    pass


class KeyForbidden(Exception):
    """يُرفع عند 403 (المفتاح مرفوض/محظور أو بلا صلاحية لهذا النموذج)."""
    pass


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("hasad_news_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, requests_per_minute: int = 12):
        self.min_interval = 60.0 / max(requests_per_minute, 1)
        self._last = 0.0

    def wait(self):
        now = time.time()
        elapsed = now - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.time()


RATE_LIMITER = RateLimiter(requests_per_minute=12)


NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


def strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_image_url(item: ET.Element, description_raw: str, content_encoded_raw: str) -> Optional[str]:
    media_content = item.find("media:content", NS)
    if media_content is not None:
        url = (media_content.get("url") or "").strip()
        if url:
            return url

    media_thumbnail = item.find("media:thumbnail", NS)
    if media_thumbnail is not None:
        url = (media_thumbnail.get("url") or "").strip()
        if url:
            return url

    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = (enclosure.get("url") or "").strip()
        enc_type = (enclosure.get("type") or "").lower()
        if url and (not enc_type or enc_type.startswith("image")):
            return url

    for raw_html in (content_encoded_raw, description_raw):
        if raw_html:
            m = IMG_TAG_RE.search(raw_html)
            if m:
                return html.unescape(m.group(1).strip())

    return None


def fetch_feed(url: str, category: str) -> list[dict]:
    if url.startswith("http://") or url.startswith("https://"):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            raw_content = resp.content
        except requests.RequestException as e:
            log.warning(f"  ⚠️  فشل سحب {url}: {e}")
            return []
    else:
        try:
            with open(url, "rb") as f:
                raw_content = f.read()
        except OSError as e:
            log.warning(f"  ⚠️  فشل قراءة الملف المحلي {url}: {e}")
            return []

    try:
        root = ET.fromstring(raw_content)
    except ET.ParseError as e:
        log.warning(f"  ⚠️  فشل تحليل XML من {url}: {e}")
        return []

    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = item.findtext("pubDate") or ""
        description = item.findtext("description") or ""
        content_encoded = item.findtext("content:encoded", namespaces=NS) or ""
        author_raw = (item.findtext("author") or item.findtext("dc:creator") or "").strip()

        if not title or not link:
            continue

        pub_date = parse_pub_date(pub_date_raw, source_url=url)
        image_url = extract_image_url(item, description, content_encoded)
        body = content_encoded or description

        items.append({
            "title": strip_html(title),
            "link": link,
            "pub_date": pub_date,
            "raw_body": strip_html(body),
            "source_feed": url,
            "image_url": image_url,
            "category": category,
            "author": strip_html(author_raw) or None,
        })
    return items


def parse_pub_date(pub_date_raw: str, source_url: str = "") -> datetime:
    raw = (pub_date_raw or "").strip()
    if not raw:
        log.warning(f"  ⚠️  تاريخ نشر فارغ من المصدر {source_url or '(غير معروف)'} — استُخدم وقت البوت الحالي كبديل.")
        return datetime.now(timezone.utc)

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass

    fallback_formats = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ]
    cleaned = raw.replace("Z", "+00:00")
    for fmt in fallback_formats:
        try:
            dt = datetime.strptime(cleaned, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue

    log.warning(
        f"  ⚠️  تعذّر تحليل تاريخ النشر '{raw}' من المصدر {source_url or '(غير معروف)'} "
        "— استُخدم وقت تشغيل البوت كبديل مؤقت. يُفضّل فحص صيغة التاريخ بهذا المصدر."
    )
    return datetime.now(timezone.utc)


def contains_blocked_keyword(title: str, body: str) -> bool:
    texts = [title or "", body or ""]
    combined = " ".join(t for t in texts if t)
    return any(kw in combined for kw in BLOCKED_KEYWORDS)


BLOCKED_KEYWORDS_EXEMPT_SOURCES = {RSS_MASA_URL, RSS_ALITTIHAD_FULL_URL, RSS_ALKHABAR_FULL_URL, RSS_YPAGENCY_FULL_URL}

# ══════════════════════════════════════════════════════════════════════
#  🚫 أخبار "عاجل" من وكالة الصحافة اليمنية — نشرات سريعة جداً بلا صور
#  غالباً، وتتكرر بصياغات متقاربة على نفس الحدث خلال دقائق. تُستبعد كلياً
#  من هذا الفيد تحديداً قبل أي معالجة أخرى (لا تُرسل لـ Gemini ولا تُنشر).
# ══════════════════════════════════════════════════════════════════════

BREAKING_NEWS_FILTER_SOURCES = {RSS_YPAGENCY_FULL_URL}
BREAKING_NEWS_TITLE_MARKERS = ("عاجل",)
BREAKING_NEWS_MAX_BODY_LEN = 300  # نصوص أطول من هذا تُعتبر خبراً مكتملاً وليس نشرة سريعة، حتى لو بدأ عنوانها بـ"عاجل"


def _is_breaking_news_item(title: str, raw_body: str) -> bool:
    t = (title or "").strip()
    starts_with_marker = any(t.startswith(marker) for marker in BREAKING_NEWS_TITLE_MARKERS)
    if not starts_with_marker:
        return False
    body_len = len((raw_body or "").strip())
    return body_len <= BREAKING_NEWS_MAX_BODY_LEN


def collect_recent_items(feed_categories: Optional[dict] = None) -> list[dict]:
    if feed_categories is None:
        feed_categories = RSS_FEED_CATEGORIES
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    all_items = []
    for feed_url, category in feed_categories.items():
        log.info(f"📡 سحب: {feed_url}  ← القسم: {category}")
        items = fetch_feed(feed_url, category)
        recent = [it for it in items if it["pub_date"] >= cutoff]
        log.info(f"   ↳ {len(items)} خبر إجمالي، {len(recent)} خلال آخر {HOURS_WINDOW} ساعة")

        if feed_url in BREAKING_NEWS_FILTER_SOURCES:
            before_breaking = len(recent)
            recent = [it for it in recent if not _is_breaking_news_item(it["title"], it["raw_body"])]
            breaking_removed = before_breaking - len(recent)
            if breaking_removed:
                log.info(f"   ↳ 🚫 تم استبعاد {breaking_removed} خبر \"عاجل\" قصير (نشرات سريعة بلا صور)")

        if feed_url in BLOCKED_KEYWORDS_EXEMPT_SOURCES:
            all_items.extend(recent)
            continue
        before = len(recent)
        recent = [it for it in recent if not contains_blocked_keyword(it["title"], it["raw_body"])]
        blocked = before - len(recent)
        if blocked:
            log.info(f"   ↳ 🚫 تم تجاوز {blocked} خبر يحتوي كلمات محظورة (لن يُرسل لـ Gemini أو يُنشر)")
        all_items.extend(recent)
    return all_items


# ══════════════════════════════════════════════════════════════════════
#  🧹 كشف واستبعاد الأخبار المكرّرة (نفس الخبر من أكثر من فيد/مصدر)
#  لتقليل خطر حذف أخبار مختلفة فعلياً بالخطأ، يُشترط تحقق الشرطين معاً:
#   1) تشابه العنوان (بعد تطبيع بسيط) ≥ DUPLICATE_TITLE_THRESHOLD (دقة عالية جداً)
#   2) تقارب زمني بين الخبرين ضمن DUPLICATE_TIME_WINDOW_MINUTES دقيقة
#  فقط عند تحقق الشرطين معاً يُعتبر الخبر الثاني مكرراً ويُستبعد، مع
#  الاحتفاظ بأول ظهور له (الأقدم حسب ترتيب الفيدات/الوقت).
# ══════════════════════════════════════════════════════════════════════

DUPLICATE_TITLE_THRESHOLD = 0.92            # احتياطي نصي (difflib) — يُستخدم فقط لو تعذّر جلب embedding
DUPLICATE_EMBEDDING_THRESHOLD = 0.93        # المعيار الأساسي: تشابه دلالي عبر المتجهات (cosine similarity)
DUPLICATE_TIME_WINDOW_MINUTES = 1440

_ARABIC_DIACRITICS_RE = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u06D6-\u06ED]")


def _normalize_title_for_dedup(title: str) -> str:
    t = title or ""
    t = _ARABIC_DIACRITICS_RE.sub("", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def remove_duplicate_news(
    items: list[dict],
    threshold: float = DUPLICATE_TITLE_THRESHOLD,
    embedding_threshold: float = DUPLICATE_EMBEDDING_THRESHOLD,
    time_window_minutes: int = DUPLICATE_TIME_WINDOW_MINUTES,
    history_items: Optional[list[dict]] = None,
) -> list[dict]:
    """يستبعد الأخبار المكررة (نفس الحدث من أكثر من مصدر) بشرطين معاً:
    تشابه دلالي مرتفع جداً بين متجهي العنوانين (Gemini embedding) + تقارب
    زمني بين تاريخي النشر. اشتراط الزمن مع التشابه الدلالي المرتفع جداً
    يقلل بشدة احتمال حذف خبر مختلف فعلياً بالخطأ.

    لو تعذّر جلب embedding لأحد العنصرين (خطأ اتصال عابر بـGemini)، يُستخدم
    تلقائياً كاحتياط تشابه العنوان النصي (difflib) بدل تعطيل كشف التكرار
    كلياً لذلك العنصر.

    كل عنصر بـitems يُخزَّن به متجه عنوانه مؤقتاً تحت المفتاح
    "_title_embedding" لإعادة استخدامه لاحقاً (مثلاً عند تسجيل الخبر
    بسجل العناوين المنشورة) دون طلب Gemini إضافي مكرر.

    history_items: أخبار منشورة فعلاً بقاعدة البيانات (من تشغيلات سابقة،
    محتملة من فيد مختلف) تُستخدم كمرجع مقارنة فقط ولا تُعاد بالنتيجة.
    كل عنصر منها يمكن أن يحمل "embedding" (متجه) بجانب "title" و"pub_date"."""
    kept: list[dict] = []
    kept_norm_titles: list[str] = []
    kept_pub_dates: list[Optional[datetime]] = []
    kept_embeddings: list[Optional[list[float]]] = []
    time_window = timedelta(minutes=time_window_minutes)

    for h in (history_items or []):
        norm = _normalize_title_for_dedup(h.get("title", ""))
        pub_date = h.get("pub_date")
        if norm and pub_date is not None:
            kept_norm_titles.append(norm)
            kept_pub_dates.append(pub_date)
            kept_embeddings.append(h.get("embedding"))

    history_count = len(kept_norm_titles)

    for it in items:
        norm = _normalize_title_for_dedup(it.get("title", ""))
        pub_date = it.get("pub_date")
        emb = get_title_embedding(it.get("title", ""))
        it["_title_embedding"] = emb
        is_dup = False
        if norm and pub_date is not None:
            for i, existing_norm in enumerate(kept_norm_titles):
                existing_pub_date = kept_pub_dates[i]
                if existing_pub_date is None:
                    continue
                if abs(pub_date - existing_pub_date) > time_window:
                    continue  # بعيدان زمنياً — لا يُعتبران مكررين مهما تشابه العنوان
                existing_emb = kept_embeddings[i]
                if emb and existing_emb:
                    sim = _cosine_similarity(emb, existing_emb)
                    is_match = sim >= embedding_threshold
                    method_label = "دلالي"
                else:
                    if not existing_norm:
                        continue
                    sim = _text_similarity(norm, existing_norm)
                    is_match = sim >= threshold
                    method_label = "نصي احتياطي"
                if is_match:
                    is_dup = True
                    source = "منشور سابقاً" if i < history_count else "بنفس الدفعة"
                    log.info(
                        f"  🔁 خبر مكرر تم استبعاده (تشابه {method_label} {sim:.0%} + تقارب زمني، {source}): "
                        f"{it.get('title', '')[:70]}"
                    )
                    break
        if is_dup:
            continue
        kept.append(it)
        kept_norm_titles.append(norm)
        kept_pub_dates.append(pub_date)
        kept_embeddings.append(emb)

    removed = len(items) - len(kept)
    if removed:
        log.info(f"🧹 تم استبعاد {removed} خبر مكرر من إجمالي {len(items)}.")
    return kept


# ══════════════════════════════════════════════════════════════════════
#  🧹🥇 طبقة أولى إضافية لكشف التكرار: مقارنة المتن الخام (raw_body) كما
#  وصل من الفيد، قبل أي إعادة صياغة من Gemini، وقبل مقارنة العناوين
#  الدلالية أعلاه.
#
#  لماذا هذه الطبقة ضرورية رغم وجود remove_duplicate_news؟
#  البيانات الرسمية (مثل بيانات المجلس السياسي الأعلى) غالباً تُنسخ
#  حرفياً أو شبه حرفياً عبر عدة مواقع. لكن بعد أن يعيد Gemini صياغة كل
#  نسخة على حدة، قد يُبرز زاوية مختلفة من نفس البيان (مرة عن استهداف
#  المطار، ومرة عن الحصار، ومرة عن دعوة النفير)، فتنخفض نسبة التشابه
#  الدلالي بين العناوين المُعاد صياغتها تحت العتبة رغم أن الحدث الجذري
#  واحد. مقارنة المتن الخام (قبل هذا "التشويش" المُصطنع من إعادة
#  الصياغة) تلتقط هذه الحالات بدقة أعلى بكثير، وكذلك توفر استدعاء
#  Gemini على الأخبار التي ستُستبعد أصلاً.
# ══════════════════════════════════════════════════════════════════════

RAW_CONTENT_DB_FILE = os.path.join(BASE_DIR, "raw_content_log.db")
RAW_CONTENT_MAX_AGE_HOURS = 48     # نافذة أوسع من سجل العناوين، لأن نفس البيان الرسمي قد يُعاد تداوله لاحقاً بنفس النص
RAW_DUPLICATE_THRESHOLD = 0.90     # تشابه نصي نهائي (rapidfuzz) على المتن الخام (نسخ حرفي أو شبه حرفي)
RAW_COMPARE_MAX_CHARS = 1500       # يكفي لالتقاط التطابق دون إبطاء المقارنة على متون طويلة جداً
MINHASH_NUM_PERM = 128             # عدد التباديل لـMinHash (128 توازن جيد بين الدقة والسرعة)
MINHASH_SHINGLE_SIZE = 4           # طول القطع الحرفية (character shingles) — مناسب للعربي أكثر من تقسيم الكلمات
LSH_CANDIDATE_THRESHOLD = 0.75     # عتبة تقريبية لجلب "مرشحين" فقط من LSH؛ التأكيد النهائي بعتبة RAW_DUPLICATE_THRESHOLD عبر rapidfuzz


def _normalize_raw_for_dedup(text: str) -> str:
    """نفس منطق تطبيع العناوين (_normalize_title_for_dedup) لكن يُطبَّق على
    المتن الخام الكامل (عنوان + raw_body) قبل أي إعادة صياغة من Gemini."""
    t = _normalize_title_for_dedup(text)
    return t[:RAW_COMPARE_MAX_CHARS]


def _get_shingles(text: str, k: int = MINHASH_SHINGLE_SIZE) -> set:
    """يقسّم النص إلى قطع حرفية متتالية بطول k (character n-grams). التقسيم
    الحرفي (وليس الكلمي) أنسب للعربية لأنه لا يتأثر بمشاكل التجزئة
    الصرفية (تعريف/تنكير، سوابق ولواحق) بين نصين متشابهين لكن بصياغة
    مختلفة قليلاً."""
    if len(text) < k:
        return {text} if text else set()
    return {text[i:i + k] for i in range(len(text) - k + 1)}


def _build_minhash(text: str):
    """يبني بصمة MinHash من قطع النص الحرفية. تُستخدم هذه البصمة لتقدير
    تشابه Jaccard بين نصين بسرعة عالية دون مقارنة كل حرف، ولإدخال النص
    في فهرس LSH لاستبعاد أغلب الأزواج غير المتشابهة فوراً بدل مقارنة كل
    خبر بكل خبر آخر (O(n²))."""
    m = MinHash(num_perm=MINHASH_NUM_PERM)
    for shingle in _get_shingles(text):
        m.update(shingle.encode("utf-8"))
    return m


def _minhash_to_blob(m) -> bytes:
    return m.hashvalues.astype("uint64").tobytes()


def _minhash_from_blob(blob: bytes):
    m = MinHash(num_perm=MINHASH_NUM_PERM)
    m.hashvalues = np.frombuffer(blob, dtype="uint64").copy()
    return m


def _get_raw_content_db() -> sqlite3.Connection:
    """يفتح اتصال SQLite بسجل المتن الخام (وينشئ الجدول والفهرس أول مرة).
    SQLite أفضل من ملف JSON واحد هنا لأنه يقرأ/يكتب صفوفاً منفردة بدل
    تحميل الأرشيف كاملاً بالذاكرة في كل تشغيل، ويدعم فهرسة تاريخ النشر
    مما يسرّع تنقية السجلات القديمة مع نمو الأرشيف بمرور الوقت."""
    os.makedirs(os.path.dirname(RAW_CONTENT_DB_FILE), exist_ok=True)
    conn = sqlite3.connect(RAW_CONTENT_DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_content (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            norm_raw TEXT NOT NULL,
            pub_date TEXT NOT NULL,
            minhash BLOB
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_content_pub_date ON raw_content(pub_date)")
    return conn


def _prune_raw_content_db(conn: sqlite3.Connection, max_age_hours: int = RAW_CONTENT_MAX_AGE_HOURS) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    conn.execute("DELETE FROM raw_content WHERE pub_date < ?", (cutoff,))
    conn.commit()


def get_recent_raw_items(hours: int = RAW_CONTENT_MAX_AGE_HOURS) -> list[dict]:
    """يعيد الأخبار (بمتنها الخام المُطبَّع + بصمة MinHash) المسجَّلة محلياً
    خلال آخر عدة ساعات، لمقارنة أي خبر جديد قادم من أي فيد بها قبل
    إرساله لـGemini."""
    conn = _get_raw_content_db()
    try:
        _prune_raw_content_db(conn, hours)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            "SELECT title, norm_raw, pub_date, minhash FROM raw_content WHERE pub_date >= ?",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for title, norm_raw, pub_date_str, minhash_blob in rows:
        try:
            pub_date = datetime.fromisoformat(pub_date_str)
        except ValueError:
            continue
        minhash = _minhash_from_blob(minhash_blob) if (minhash_blob and DATASKETCH_AVAILABLE) else None
        out.append({"title": title, "pub_date": pub_date, "norm_raw": norm_raw, "minhash": minhash})
    return out


def log_raw_content(title: str, raw_body: str, pub_date_iso: str) -> None:
    """يسجّل المتن الخام (عنوان + raw_body كما وصل من الفيد، قبل إعادة
    الصياغة) لكل خبر جديد تقرر معالجته، لمنع أي فيد آخر لاحقاً من إعادة
    إرسال نفس النص الرسمي/الخبر لـGemini."""
    norm_raw = _normalize_raw_for_dedup(raw_body)
    minhash_blob = _minhash_to_blob(_build_minhash(norm_raw)) if (norm_raw and DATASKETCH_AVAILABLE) else None

    conn = _get_raw_content_db()
    try:
        conn.execute(
            "INSERT INTO raw_content (title, norm_raw, pub_date, minhash) VALUES (?, ?, ?, ?)",
            (title, norm_raw, pub_date_iso, minhash_blob),
        )
        conn.commit()
        _prune_raw_content_db(conn)
    except sqlite3.Error as e:
        log.warning(f"⚠️  تعذّر حفظ المتن الخام بقاعدة البيانات المحلية: {e}")
    finally:
        conn.close()


def remove_raw_duplicate_news(
    items: list[dict],
    threshold: float = RAW_DUPLICATE_THRESHOLD,
    time_window_minutes: int = RAW_CONTENT_MAX_AGE_HOURS * 60,
    history_items: Optional[list[dict]] = None,
) -> list[dict]:
    """طبقة أولى (سريعة، رخيصة، بدون أي استدعاء لـGemini): تستبعد الأخبار
    التي متنها الخام (عنوان + raw_body كما وصل من الفيد) شبه مطابق نصياً
    لمتن خبر آخر — إما داخل نفس الدفعة (فيدات مختلفة نشرت نفس الخبر)، أو
    منشور/معالَج مسبقاً بسجل محلي خلال آخر RAW_CONTENT_MAX_AGE_HOURS ساعة.

    تعمل الدالة على مرحلتين لو كانت datasketch متوفرة:
      1) فهرس LSH يرشّح فوراً الأزواج المحتملة فقط (بدل مقارنة كل خبر
         بكل خبر آخر — O(n²))، وهذا يصبح مهماً مع كبر حجم الأرشيف.
      2) تأكيد نهائي بتشابه نصي دقيق (rapidfuzz) على عتبة threshold
         الحقيقية، لتفادي التطابقات التقريبية الخاطئة من LSH وحده.
    لو datasketch غير مثبّتة، ترجع تلقائياً لمقارنة تسلسلية كاملة عبر
    rapidfuzz (أو difflib لو rapidfuzz أيضاً غير متوفر) — نفس النتيجة،
    أبطأ فقط مع الأرشيفات الكبيرة جداً."""
    kept: list[dict] = []
    kept_norms: list[str] = []
    kept_pub_dates: list[Optional[datetime]] = []
    time_window = timedelta(minutes=time_window_minutes)

    for h in (history_items or []):
        norm = h.get("norm_raw", "")
        pub_date = h.get("pub_date")
        if norm and pub_date is not None:
            kept_norms.append(norm)
            kept_pub_dates.append(pub_date)

    history_count = len(kept_norms)
    use_lsh = DATASKETCH_AVAILABLE

    if use_lsh:
        lsh = MinHashLSH(threshold=LSH_CANDIDATE_THRESHOLD, num_perm=MINHASH_NUM_PERM)
        kept_minhashes: list = []
        for i, h in enumerate(history_items or []):
            norm = h.get("norm_raw", "")
            pub_date = h.get("pub_date")
            if not (norm and pub_date is not None):
                continue
            mh = h.get("minhash") or _build_minhash(norm)
            lsh.insert(f"k{len(kept_minhashes)}", mh)
            kept_minhashes.append(mh)

    for it in items:
        norm = _normalize_raw_for_dedup(it.get("raw_body", ""))
        pub_date = it.get("pub_date")
        is_dup = False
        matched_sim = 0.0
        matched_source = ""

        if norm and pub_date is not None:
            if use_lsh:
                mh = _build_minhash(norm)
                for key in lsh.query(mh):
                    idx = int(key[1:])
                    existing_norm = kept_norms[idx]
                    existing_pub_date = kept_pub_dates[idx]
                    if existing_pub_date is None or not existing_norm:
                        continue
                    if abs(pub_date - existing_pub_date) > time_window:
                        continue
                    sim = _text_similarity(norm, existing_norm)
                    if sim >= threshold:
                        is_dup = True
                        matched_sim = sim
                        matched_source = "مسجَّل سابقاً" if idx < history_count else "بنفس الدفعة"
                        break
            else:
                for i, existing_norm in enumerate(kept_norms):
                    existing_pub_date = kept_pub_dates[i]
                    if existing_pub_date is None or not existing_norm:
                        continue
                    if abs(pub_date - existing_pub_date) > time_window:
                        continue
                    sim = _text_similarity(norm, existing_norm)
                    if sim >= threshold:
                        is_dup = True
                        matched_sim = sim
                        matched_source = "مسجَّل سابقاً" if i < history_count else "بنفس الدفعة"
                        break

        if is_dup:
            log.info(
                f"  🔁🥇 خبر مكرر (تطابق نصي خام {matched_sim:.0%}، {matched_source}) "
                f"تم استبعاده قبل إرساله لـGemini: {it.get('title', '')[:70]}"
            )
            continue

        kept.append(it)
        kept_norms.append(norm)
        kept_pub_dates.append(pub_date)
        if use_lsh and norm:
            mh = _build_minhash(norm)
            lsh.insert(f"k{len(kept_minhashes)}", mh)
            kept_minhashes.append(mh)

    removed = len(items) - len(kept)
    if removed:
        log.info(f"🧹🥇 [متن خام] تم استبعاد {removed} خبر مكرر قبل معالجته بـGemini.")
    return kept


# ══════════════════════════════════════════════════════════════════════
#  🥉 طبقة ثالثة إضافية لكشف التكرار: تشابه دلالي على مقدمة المحتوى
#  (أول ~400 حرف من raw_body)، بعتبة أخف من طبقة العنوان، مقترنة إلزامياً
#  بفحص "كيان مشترك" (جهة/شخصية معروفة تظهر بالخبرين) ونافذة زمنية أضيق.
#
#  لماذا هذه الطبقة مختلفة عن الطبقتين السابقتين؟
#  الطبقة الأولى (raw_body كامل) تمسك النسخ الحرفي/شبه الحرفي.
#  الطبقة الثانية (عنوان دلالي) تمسك إعادة الصياغة القريبة للعنوان نفسه.
#  لكن فيه حالة تفلت من الاثنين: خبرين مكتوبين باستقلالية كاملة (كل موقع
#  بأسلوبه من الصفر، عنواناً ومتناً) عن نفس التصريح/الحدث — مثال: "صنعاء
#  تلوح بمعادلة الحصار" مقابل "وزير الدفاع: القوات جاهزة لفرض معادلات
#  ردع"، نفس تصريح وزير الدفاع لكن زاويتان وصياغتان مختلفتان بالكامل من
#  أول كلمة. هنا لا نص متطابق ولا عنوان متشابه، لكن **المحتوى** يتقاطع
#  معنوياً و**نفس الجهة/الشخصية** مذكورة بالخبرين.
#
#  اشتراط الكيان المشترك إلزامياً (وليس فقط تشابه دلالي بعتبة أخف) هو ما
#  يمنع هذه الطبقة من حذف خبرين مختلفين فعلياً بالخطأ رغم عتبتها الأقل
#  صرامة (0.80 بدل 0.93).
# ══════════════════════════════════════════════════════════════════════

CONTENT_DUPLICATE_EMBEDDING_THRESHOLD = 0.80
CONTENT_DUPLICATE_TIME_WINDOW_MINUTES = 180   # نافذة أضيق من طبقة العنوان (3 ساعات) لتقليل مخاطر الحذف الخاطئ
CONTENT_EMBEDDING_CHARS = 400                  # أول ~400 حرف من raw_body تكفي لالتقاط جوهر الخبر دون إبطاء الطلب

# قائمة كيانات قابلة للتوسعة يدوياً — أضف/عدّل حسب الجهات المتكررة في
# تغطيتك (سياسية، عسكرية، شخصيات). المطابقة نصية بسيطة (substring) بعد
# نفس تطبيع العناوين المستخدم بالطبقة الثانية.
KNOWN_ENTITIES = [
    "المجلس السياسي الأعلى",
    "التحالف بقيادة السعودية",
    "الحكومة المعترف بها دولياً",
    "الحاكم العسكري السعودي الشهراني",
    "وزارة الدفاع",
    "وزير الدفاع",
    "القوات المسلحة",
    "العاطفي",
    "عبدالملك الحوثي",
    "بدرالدين الحوثي",
    "قائد الثورة",
    "التحالف السعودي",
    "الحصار",
    "مطار صنعاء",
]


def _extract_entities(text: str) -> set:
    """يستخرج أي كيانات معروفة (من KNOWN_ENTITIES) موجودة بالنص، بعد نفس
    تطبيع العناوين المستخدم بالطبقة الدلالية الثانية، لضمان اتساق المطابقة
    (إزالة تشكيل، توحيد الهمزات...)."""
    norm = _normalize_title_for_dedup(text)
    found = set()
    for entity in KNOWN_ENTITIES:
        norm_entity = _normalize_title_for_dedup(entity)
        if norm_entity and norm_entity in norm:
            found.add(norm_entity)
    return found


def get_content_embedding(raw_body: str) -> Optional[list[float]]:
    """يجيب embedding لمقدمة متن الخبر (أول CONTENT_EMBEDDING_CHARS حرف من
    raw_body)، بنفس آلية get_title_embedding (تدوير مفاتيح Gemini)، لكن
    على نص المحتوى بدل العنوان — يُستخدم بطبقة كشف التكرار الثالثة."""
    snippet = (raw_body or "").strip()[:CONTENT_EMBEDDING_CHARS]
    return get_title_embedding(snippet)


def remove_content_duplicate_news(
    items: list[dict],
    embedding_threshold: float = CONTENT_DUPLICATE_EMBEDDING_THRESHOLD,
    time_window_minutes: int = CONTENT_DUPLICATE_TIME_WINDOW_MINUTES,
    history_items: Optional[list[dict]] = None,
) -> list[dict]:
    """طبقة ثالثة (بعد raw-text وbعد عنوان دلالي): تستبعد خبراً لو تحققت
    **كل** الشروط الثلاثة معاً مقارنة بخبر آخر (بنفس الدفعة أو من السجل
    المحلي):
      1) تشابه دلالي على مقدمة المحتوى ≥ embedding_threshold (0.80)
      2) كيان معروف واحد على الأقل مشترك بين الخبرين (KNOWN_ENTITIES)
      3) تقارب زمني ضمن time_window_minutes (3 ساعات افتراضياً)

    اشتراط الكيان المشترك تحديداً هو ما يسمح باستخدام عتبة أخف من طبقة
    العنوان (0.80 بدل 0.93) دون رفع خطر حذف خبرين مختلفين فعلياً بالخطأ."""
    kept: list[dict] = []
    kept_embeddings: list[Optional[list[float]]] = []
    kept_entities: list[set] = []
    kept_pub_dates: list[Optional[datetime]] = []
    time_window = timedelta(minutes=time_window_minutes)

    for h in (history_items or []):
        pub_date = h.get("pub_date")
        emb = h.get("content_embedding")
        entities = set(h.get("entities") or [])
        if pub_date is not None:
            kept_embeddings.append(emb)
            kept_entities.append(entities)
            kept_pub_dates.append(pub_date)

    history_count = len(kept_pub_dates)

    for it in items:
        raw_body = it.get("raw_body", "")
        title = it.get("title", "")
        pub_date = it.get("pub_date")
        entities = _extract_entities(f"{title} {raw_body}")
        emb = get_content_embedding(raw_body) if (raw_body and entities) else None
        # لا داعي لطلب embedding لو مفيش أي كيان معروف بالنص أصلاً —
        # الشرط الثاني (كيان مشترك) هيفشل حتماً فمفيش فايدة من الطلب.
        it["_content_embedding"] = emb
        it["_entities"] = list(entities)

        is_dup = False
        if emb and entities and pub_date is not None:
            for i in range(len(kept_pub_dates)):
                existing_pub_date = kept_pub_dates[i]
                existing_emb = kept_embeddings[i]
                existing_entities = kept_entities[i]
                if existing_pub_date is None or not existing_emb or not existing_entities:
                    continue
                if abs(pub_date - existing_pub_date) > time_window:
                    continue
                shared = entities & existing_entities
                if not shared:
                    continue
                sim = _cosine_similarity(emb, existing_emb)
                if sim >= embedding_threshold:
                    is_dup = True
                    source = "منشور سابقاً" if i < history_count else "بنفس الدفعة"
                    log.info(
                        f"  🔁🥉 خبر مكرر (تشابه محتوى {sim:.0%} + كيان مشترك "
                        f"[{', '.join(list(shared)[:2])}]، {source}) تم استبعاده: {title[:70]}"
                    )
                    break

        if is_dup:
            continue
        kept.append(it)
        kept_embeddings.append(emb)
        kept_entities.append(entities)
        kept_pub_dates.append(pub_date)

    removed = len(items) - len(kept)
    if removed:
        log.info(f"🧹🥉 [تشابه محتوى] تم استبعاد {removed} خبر مكرر (كيان مشترك + تشابه معنوي).")
    return kept


ARTICLE_REQUEST_TIMEOUT = 30
ARTICLE_HEADERS = {"User-Agent": "Mozilla/5.0 (Android; Mobile) NewsBot/1.0"}
ARTICLE_DEBUG = False
MIN_LEAF_LEN = 25
MIN_ACCEPTABLE_LOCAL_LEN = 150
ALWAYS_STRIP_TAGS = ("script", "style", "iframe", "form", "noscript", "svg")
ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]")


def _clean_text(text: str) -> str:
    text = ZERO_WIDTH_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


NOISE_LINE_PATTERNS = [
    r"^شارك$",
    r"^المصدر\s*/",
    r"^تابعونا على",
    r"^متابعات خاصة",
    r"صحيفة (الكترونية|إلكترونية) تأسست",
    r"^!Image\s*\d+",
    r"^آخر تحديث\s*:",
    r"^انشر",
    r"^تفاصيل\s*:",
    r"^\s*$",
]

STOP_REGEX_PATTERNS = [
    re.compile(r"^\S+/\d{1,2}/\S+/\d{4}\s*-\s*\d{1,2}:\d{2}\s*[صم]\.?\s*$"),
]


def _hits_stop_regex(text: str) -> bool:
    return any(p.match(text.strip()) for p in STOP_REGEX_PATTERNS)


STOP_MARKERS = [
    "مواضيع قد تهمك",
    "قد يعجبك ايضا",
    "قد يعجبك أيضا",
    "أخبار أخرى قد تعجبك",
    "المقال السابق",
    "المقال التالي",
    "الأكثر قراءة",
    "أحدث المنشورات",
    "احدث المنشورات",
    "مقالات ذات صلة",
    "التعليقات",
    "أضف تعليق",
    "اترك تعليقاً",
    "اترك رد",
    "شارك المقال",
]


def _extract_by_doc_order(h1: Optional[Tag], soup: BeautifulSoup) -> list[str]:
    start_node = h1 or soup.body or soup
    paragraphs = []
    for el in start_node.find_all_next(["p", "h2", "h3", "h4"]):
        text = _clean_text(el.get_text(" ", strip=True))
        if not text:
            continue
        if _hits_stop_regex(text):
            break
        if _text_hits_stop_marker(text):
            break
        if _is_noise_line(text):
            continue
        if len(text) < 20:
            continue
        paragraphs.append(text)
    return paragraphs


def _is_noise_line(text: str) -> bool:
    text = text.strip()
    return any(re.search(p, text) for p in NOISE_LINE_PATTERNS)


def _text_hits_stop_marker(text: str) -> bool:
    text = text.strip()
    return any(marker in text for marker in STOP_MARKERS)


def _own_visible_text_len(tag: Tag) -> int:
    return len(tag.get_text(" ", strip=True))


def _find_leaf_blocks(soup: BeautifulSoup) -> list[Tag]:
    leaves = []
    for tag in soup.find_all(True):
        if tag.name in ALWAYS_STRIP_TAGS:
            continue
        text_len = _own_visible_text_len(tag)
        if text_len < MIN_LEAF_LEN:
            continue
        has_big_child = any(
            isinstance(c, Tag) and c.name not in ALWAYS_STRIP_TAGS
            and _own_visible_text_len(c) >= MIN_LEAF_LEN
            for c in tag.find_all(True, recursive=False)
        )
        if has_big_child:
            continue
        leaves.append(tag)
    return leaves


def _walk_jsonld(node) -> list[dict]:
    found = []
    if isinstance(node, dict):
        found.append(node)
        for v in node.values():
            found.extend(_walk_jsonld(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_jsonld(item))
    return found


def _extract_from_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for script in scripts:
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in _walk_jsonld(data):
            body = obj.get("articleBody") or obj.get("text")
            if body and isinstance(body, str) and len(body) > MIN_LEAF_LEN:
                title = obj.get("headline") or obj.get("name")
                body = _clean_text(body)
                return {"title": _clean_text(title) if title else title,
                        "body": body,
                        "paragraphs": [p.strip() for p in body.split("\n") if p.strip()]}
    return None


JINA_READER_BASE = "https://r.jina.ai/"

_MD_LINK_ONLY_RE = re.compile(r"^[-*+]?\s*!?\[[^\]]*\]\([^)]*\)\s*$")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _strip_markdown_noise(text: str) -> str:
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', "", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = re.sub(r"^#+\s*", "", text)
    text = re.sub(r"[*_`]+", "", text)
    return _clean_text(text)


def fetch_via_jina(url: str) -> Optional[str]:
    reader_url = JINA_READER_BASE + url
    try:
        resp = requests.get(reader_url, headers=ARTICLE_HEADERS, timeout=ARTICLE_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    return resp.text


def extract_via_jina(url: str, fallback_title: Optional[str] = None) -> Optional[dict]:
    raw = fetch_via_jina(url)
    if not raw:
        return None

    title = fallback_title
    if not title:
        m = re.search(r"^Title:\s*(.+)$", raw, re.MULTILINE)
        if m:
            title = _clean_text(m.group(1))

    if "Markdown Content:" in raw:
        content = raw.split("Markdown Content:", 1)[1]
    else:
        content = raw

    paragraphs: list[str] = []
    for line in content.splitlines():
        line = ZERO_WIDTH_RE.sub("", line).strip()
        if not line:
            continue
        cleaned = _strip_markdown_noise(line)
        if not cleaned:
            continue
        if _hits_stop_regex(cleaned):
            break
        if _text_hits_stop_marker(cleaned):
            break
        if _is_noise_line(cleaned):
            continue
        if len(cleaned) < 25:
            continue
        paragraphs.append(cleaned)

    if not paragraphs:
        return None

    body = "\n\n".join(paragraphs)
    return {"title": title, "body": body, "paragraphs": paragraphs}


def _detect_site_category(h1: Optional[Tag]) -> Optional[str]:
    if h1 is None:
        return None
    for tag in h1.find_all_previous(["h2", "h3"]):
        text = _clean_text(tag.get_text(strip=True))
        if text:
            return text
    return None


def extract_article(url: str) -> Optional[dict]:
    try:
        resp = requests.get(url, headers=ARTICLE_HEADERS, timeout=ARTICLE_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return extract_via_jina(url)

    soup = BeautifulSoup(resp.content, "html.parser")

    h1 = soup.find("h1")
    if h1 is None:
        og_title_tag = soup.find("meta", attrs={"property": "og:title"})
        og_title = _clean_text(og_title_tag.get("content", "")) if og_title_tag else None
        if og_title:
            for candidate in soup.find_all(["h2", "h3"]):
                if _clean_text(candidate.get_text(" ", strip=True)) == og_title:
                    h1 = candidate
                    break
    title = _clean_text(h1.get_text(strip=True)) if h1 else None

    site_category = _detect_site_category(h1)

    def _with_cat(d: Optional[dict]) -> Optional[dict]:
        if d is not None:
            d.setdefault("site_category", site_category)
        return d

    jsonld_result = _extract_from_jsonld(soup)
    if jsonld_result:
        if not jsonld_result.get("title"):
            jsonld_result["title"] = title
        return _with_cat(jsonld_result)

    for tag_name in ALWAYS_STRIP_TAGS:
        for t in soup.find_all(tag_name):
            t.decompose()

    doc_order_paragraphs = _extract_by_doc_order(h1, soup)
    total_len = sum(len(p) for p in doc_order_paragraphs)

    if total_len >= MIN_ACCEPTABLE_LOCAL_LEN:
        return _with_cat({"title": title, "body": "\n\n".join(doc_order_paragraphs),
                "paragraphs": doc_order_paragraphs})

    leaves = _find_leaf_blocks(soup)
    best_paragraphs: list[str] = []
    if leaves:
        groups: dict[int, dict] = {}
        for leaf in leaves:
            parent = leaf.parent
            key = id(parent)
            g = groups.setdefault(key, {"parent": parent, "leaves": [], "total_len": 0})
            g["leaves"].append(leaf)
            g["total_len"] += _own_visible_text_len(leaf)

        best = max(groups.values(), key=lambda g: g["total_len"])
        for leaf in best["leaves"]:
            text = _clean_text(leaf.get_text(" ", strip=True))
            if _hits_stop_regex(text) or _text_hits_stop_marker(text):
                break
            if _is_noise_line(text):
                continue
            best_paragraphs.append(text)

    best_total_len = sum(len(p) for p in best_paragraphs)
    if best_total_len >= MIN_ACCEPTABLE_LOCAL_LEN:
        return _with_cat({"title": title, "body": "\n\n".join(best_paragraphs),
                "paragraphs": best_paragraphs})

    jina_result = extract_via_jina(url, fallback_title=title)
    if jina_result:
        return _with_cat(jina_result)

    if best_total_len > total_len:
        return _with_cat({"title": title, "body": "\n\n".join(best_paragraphs),
                "paragraphs": best_paragraphs})
    return _with_cat({"title": title, "body": "\n\n".join(doc_order_paragraphs),
            "paragraphs": doc_order_paragraphs})


SITE_CATEGORY_MAP = {
    "رياض": "رياضة",
    "عرب وعالم": "شؤون دولية",
    "كتابات": "آراء واتجاهات",
    "اخبار عدن": "أخبار وتقارير",
    "اخبار وتقارير": "أخبار وتقارير",
    "اخبار محافظات اليمن": "أخبار وتقارير",
    "منوعات": "منوعات",
}


def map_site_category(site_category: Optional[str]) -> Optional[str]:
    if not site_category:
        return None
    for key, target in SITE_CATEGORY_MAP.items():
        if key in site_category:
            return target
    return None


def apply_full_extraction(items: list[dict]) -> None:
    total = len(items)
    for idx, it in enumerate(items, start=1):
        log.info(f"  🧲 [{idx}/{total}] استخراج الخبر الكامل: {it['link'][:80]}")
        result = extract_article(it["link"])
        if result and result.get("body") and len(result["body"]) >= MIN_ACCEPTABLE_LOCAL_LEN:
            it["raw_body"] = result["body"]
            if it.get("source_feed") == RSS_ADEN_TM_FULL_URL:
                corrected = map_site_category(result.get("site_category"))
                if corrected is None:
                    log.info(f"     ↳ 🚫 قسم غير معروف/تعذّر اكتشافه ({result.get('site_category')!r}) — سيُستبعد الخبر.")
                    it["_excluded"] = True
                elif corrected != it["category"]:
                    log.info(f"     ↳ 🗂️  تصحيح القسم تلقائياً: {it['category']} → {corrected}")
                    it["category"] = corrected
        else:
            log.warning("  ⚠️  تعذّر استخراج الخبر كاملاً لهذا الرابط — سيُخدم نص RSS الأصلي.")


SB_HEADERS: Optional[dict] = None


def sb_headers() -> dict:
    global SB_HEADERS
    if SB_HEADERS is None:
        if not SUPABASE_SERVICE_KEY:
            raise RuntimeError("SUPABASE_ANON_KEY غير مضبوط. أضفه كمتغير بيئة قبل تشغيل البوت.")
        SB_HEADERS = {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    return SB_HEADERS


def summarize_sb_error(response: requests.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            parts = [data.get("message"), data.get("details"), data.get("hint"), data.get("code")]
            return " | ".join(str(p) for p in parts if p)[:500]
    except ValueError:
        pass
    return response.text[:500]


def cleanup_system_logs() -> None:
    url = f"{SUPABASE_URL}/rest/v1/rpc/cleanup_system_logs"
    try:
        r = requests.post(url, headers=sb_headers(), json={}, timeout=REQUEST_TIMEOUT)
        if r.status_code in (200, 204):
            try:
                data = r.json()
            except ValueError:
                data = {}
            cron_n = data.get("deleted_cron_job_run_details")
            net_n = data.get("deleted_net_http_response")
            parts = []
            if cron_n == -1:
                parts.append("cron.job_run_details غير متاح")
            elif cron_n is not None:
                parts.append(f"cron: {cron_n} سجل")
            if net_n == -1:
                parts.append("net._http_response غير متاح")
            elif net_n is not None:
                parts.append(f"net: {net_n} سجل")
            summary = " | ".join(parts) if parts else "بدون تفاصيل إضافية"
            log.info(f"🧹 تم تنظيف سجلات النظام الأقدم من 72 ساعة ({summary})")
        else:
            log.warning(f"⚠️  تعذّر تنظيف سجلات النظام [{r.status_code}]: {r.text[:200]}")
    except requests.RequestException as e:
        log.warning(f"⚠️  تعذّر تنظيف سجلات النظام (خطأ اتصال): {e}")


def check_system_logs_size() -> None:
    url = f"{SUPABASE_URL}/rest/v1/rpc/get_system_logs_counts"
    try:
        r = requests.post(url, headers=sb_headers(), json={}, timeout=REQUEST_TIMEOUT)
        if r.status_code not in (200, 204):
            log.warning(f"⚠️  تعذّر فحص حجم سجلات النظام [{r.status_code}]: {r.text[:200]}")
            return
        try:
            data = r.json()
        except ValueError:
            data = {}
        cron_n = data.get("cron_count")
        net_n = data.get("net_count")
        exceeded = []
        if isinstance(cron_n, int) and cron_n > SYSTEM_LOGS_ALERT_THRESHOLD:
            exceeded.append(f"cron.job_run_details: {cron_n:,} سجل")
        if isinstance(net_n, int) and net_n > SYSTEM_LOGS_ALERT_THRESHOLD:
            exceeded.append(f"net._http_response: {net_n:,} سجل")
        if exceeded:
            msg = (
                "🚨 تنبيه (حصاد اليوم): تجاوز حجم جداول سجلات النظام الحد "
                f"المسموح ({SYSTEM_LOGS_ALERT_THRESHOLD:,} سجل):\n\n"
                + "\n".join(f"• {line}" for line in exceeded)
                + "\n\nتحقّق من جدولة cron.schedule('cleanup-system-logs-6h') "
                  "وتأكد أنها تعمل بشكل صحيح."
            )
            log.warning(f"⚠️  {msg}")
            send_admin_alert(msg)
        else:
            log.info(f"✅ حجم سجلات النظام طبيعي (cron: {cron_n}, net: {net_n})")
    except requests.RequestException as e:
        log.warning(f"⚠️  تعذّر فحص حجم سجلات النظام (خطأ اتصال): {e}")


_CATEGORY_ID_CACHE: dict[str, Optional[str]] = {}


def get_category_id(name: str) -> Optional[str]:
    if name in _CATEGORY_ID_CACHE:
        return _CATEGORY_ID_CACHE[name]

    url = f"{SUPABASE_URL}/rest/v1/categories"
    try:
        r = requests.get(url, headers=sb_headers(),
                          params={"name": f"eq.{name}", "select": "id,name,slug"},
                          timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            r = requests.get(url, headers=sb_headers(),
                              params={"name": f"ilike.*{name}*", "select": "id,name,slug"},
                              timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            rows = r.json()
        if not rows:
            log.warning(f"⚠️  لم يُعثر على قسم باسم '{name}' بجدول categories.")
            _CATEGORY_ID_CACHE[name] = None
            return None
        category_id = rows[0]["id"]
        log.info(f"📂 التصنيف: {rows[0]['name']} (id={category_id})")
        _CATEGORY_ID_CACHE[name] = category_id
        return category_id
    except requests.RequestException as e:
        log.warning(f"⚠️  فشل الاتصال أثناء البحث عن القسم '{name}': {e}")
        return None


def load_blocked_links() -> set:
    try:
        with open(BLOCKED_LINKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"⚠️  تعذّر قراءة ملف الروابط المحظورة دائماً ({BLOCKED_LINKS_FILE}): {e}")
        return set()


def save_blocked_link(link: str) -> None:
    if not link:
        return
    blocked = load_blocked_links()
    if link in blocked:
        return
    blocked.add(link)
    try:
        os.makedirs(os.path.dirname(BLOCKED_LINKS_FILE), exist_ok=True)
        with open(BLOCKED_LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(blocked), f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning(f"⚠️  تعذّر حفظ الحظر الدائم لهذا الرابط ({BLOCKED_LINKS_FILE}): {e}")


def load_pending_scheduled() -> list:
    try:
        with open(PENDING_SCHEDULED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"⚠️  تعذّر قراءة ملف الأخبار المجدولة المعلّقة ({PENDING_SCHEDULED_FILE}): {e}")
        return []


def save_pending_scheduled(pending: list) -> None:
    try:
        os.makedirs(os.path.dirname(PENDING_SCHEDULED_FILE), exist_ok=True)
        with open(PENDING_SCHEDULED_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning(f"⚠️  تعذّر حفظ ملف الأخبار المجدولة المعلّقة ({PENDING_SCHEDULED_FILE}): {e}")


def get_bot_post_status(post_id: str) -> Optional[dict]:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/get_bot_post_status",
            headers=sb_headers(),
            json={"_post_id": post_id},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning(f"  ⚠️  تعذّر الاتصال بدالة فحص الخبر المجدول: {e}")
        return None
    if r.status_code != 200:
        log.warning(f"  ⚠️  تعذّر فحص الخبر المجدول عبر RPC [{r.status_code}]: {summarize_sb_error(r)}")
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None


def check_and_notify_scheduled_posts() -> None:
    pending = load_pending_scheduled()
    if not pending:
        return

    log.info(f"🔎 فحص {len(pending)} خبر مجدول من جلسات سابقة...")
    still_pending = []
    notified = 0

    for entry in pending:
        post_id = entry.get("id")
        if not post_id:
            continue
        row = get_bot_post_status(post_id)
        if row is None:
            log.warning(f"  ⚠️  لم يمكن تأكيد حالة الخبر المجدول '{entry.get('title', '')[:50]}' — سيبقى بقائمة الانتظار.")
            still_pending.append(entry)
            continue

        if not row.get("found"):
            log.warning(f"  🗑️  الخبر المجدول '{entry.get('title', '')[:50]}' لم يعد موجوداً بقاعدة البيانات — حُذف.")
            continue

        if row.get("status") == "published":
            # نُنفّذ تحسين المشاهدات مرة واحدة فقط لكل خبر — حتى لو فشل إرسال
            # تيليجرام وأُعيدت محاولته عدة مرات في التشغيلات اللاحقة
            if not entry.get("_views_seeded"):
                seed_views(post_id)
                entry["_views_seeded"] = True
            published_at = row.get("published_at") or entry.get("published_at") or entry.get("scheduled_at") or row.get("created_at") or entry.get("created_at")
            canonical_url = build_canonical_url(row.get("slug") or entry.get("slug"), published_at)
            if send_to_telegram(entry.get("title", ""), canonical_url):
                log.info(f"  📢 نُشر فعلياً وأُرسل لتيليجرام الآن: {entry.get('title', '')[:60]}")
                notified += 1
            else:
                log.warning(f"  ⚠️  نُشر لكن فشل إرسال تيليجرام، سيُعاد المحاولة لاحقاً: {entry.get('title', '')[:60]}")
                still_pending.append(entry)
        else:
            still_pending.append(entry)

    save_pending_scheduled(still_pending)
    log.info(f"🔎 انتهى الفحص: {notified} خبر أُرسل لتيليجرام الآن، {len(still_pending)} لسا بانتظار النشر.")


def get_existing_source_urls() -> set:
    url = f"{SUPABASE_URL}/rest/v1/rpc/get_bot_existing_source_urls"
    try:
        r = requests.post(url, headers=sb_headers(), json={}, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log.warning(f"⚠️  تعذّر جلب الروابط الحالية عبر RPC [{r.status_code}]: {summarize_sb_error(r)}")
            return set()
        rows = r.json()
        return {row["source_url"] for row in rows if row.get("source_url")}
    except requests.RequestException as e:
        log.warning(f"⚠️  فشل الاتصال أثناء جلب الروابط الحالية: {e}")
        return set()


PUBLISHED_TITLES_DB_FILE = os.path.join(BASE_DIR, "published_titles_log.db")
PUBLISHED_TITLES_MAX_AGE_HOURS = 24


def _get_published_titles_db() -> sqlite3.Connection:
    """يفتح اتصال SQLite بسجل العناوين المنشورة. نفس فائدة التحويل من JSON
    في raw_content: قراءة/كتابة صفوف منفردة بدل تحميل الملف كاملاً كل مرة،
    وفهرسة تاريخ النشر لتسريع التنقية مع نمو السجل.

    content_embedding وentities عمودان إضافيان يخدمان الطبقة الثالثة
    (remove_content_duplicate_news) — تشابه المحتوى + الكيان المشترك."""
    os.makedirs(os.path.dirname(PUBLISHED_TITLES_DB_FILE), exist_ok=True)
    conn = sqlite3.connect(PUBLISHED_TITLES_DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS published_titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            pub_date TEXT NOT NULL,
            embedding TEXT,
            content_embedding TEXT,
            entities TEXT
        )
        """
    )
    # ترقية آمنة لقواعد بيانات أُنشئت بنسخة سابقة من الكود (قبل إضافة
    # عمودي content_embedding وentities) — تُهمَل الأخطاء لو الأعمدة موجودة أصلاً.
    for col in ("content_embedding", "entities"):
        try:
            conn.execute(f"ALTER TABLE published_titles ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_published_titles_pub_date ON published_titles(pub_date)")
    return conn


def _prune_published_titles_db(conn: sqlite3.Connection, max_age_hours: int = PUBLISHED_TITLES_MAX_AGE_HOURS) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    conn.execute("DELETE FROM published_titles WHERE pub_date < ?", (cutoff,))
    conn.commit()


def log_published_title(
    title: str,
    pub_date_iso: str,
    embedding: Optional[list[float]] = None,
    content_embedding: Optional[list[float]] = None,
    entities: Optional[list] = None,
) -> None:
    """يُضاف كل خبر يُنشر فعلاً لسجل محلي دائم (بدون أي طلب لـSupabase)،
    يُستخدم لاحقاً لمنع تكرار نفس الخبر من فيد آخر عبر تشغيلات مختلفة.

    embedding: متجه العنوان (اختياري) — يُفضَّل تمرير المتجه المحسوب أصلاً
    أثناء remove_duplicate_news (المخزَّن بـit["_title_embedding"]) بدل
    طلب Gemini من جديد، حتى تبقى المقارنات المستقبلية متسقة.

    content_embedding وentities: نفس الفكرة لكن للطبقة الثالثة (تشابه
    المحتوى + الكيان المشترك) — تُمرَّر من it["_content_embedding"]
    وit["_entities"] المحسوبين أثناء remove_content_duplicate_news."""
    embedding_json = json.dumps(embedding) if embedding else None
    content_embedding_json = json.dumps(content_embedding) if content_embedding else None
    entities_json = json.dumps(entities) if entities else None
    conn = _get_published_titles_db()
    try:
        conn.execute(
            "INSERT INTO published_titles (title, pub_date, embedding, content_embedding, entities) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, pub_date_iso, embedding_json, content_embedding_json, entities_json),
        )
        conn.commit()
        _prune_published_titles_db(conn)
    except sqlite3.Error as e:
        log.warning(f"⚠️  تعذّر حفظ الخبر بسجل العناوين المحلي: {e}")
    finally:
        conn.close()

    # طبقة دائمة إضافية على Supabase (bot_published_titles_log) — لا تعتمد
    # على نجاح السجل المحلي أعلاه، وأي فشل بها يُهمَل بالكامل (راجع
    # sb_log_published_title). السجل المحلي بـSQLite يبقى الأساس السريع.
    sb_log_published_title(title, pub_date_iso, embedding, content_embedding, entities)


def get_recent_published_titles(hours: int = PUBLISHED_TITLES_MAX_AGE_HOURS) -> list[dict]:
    """يعيد الأخبار المنشورة خلال آخر عدة ساعات من السجل المحلي (بدون أي
    استعلام لـSupabase)، لمقارنتها بالأخبار الجديدة القادمة من تشغيلات لاحقة
    (منع تكرار نفس الحدث من فيد آخر حتى لو نُشر بتشغيل سابق منفصل)."""
    conn = _get_published_titles_db()
    try:
        _prune_published_titles_db(conn, hours)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            "SELECT title, pub_date, embedding, content_embedding, entities "
            "FROM published_titles WHERE pub_date >= ?",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for title, pub_date_str, embedding_json, content_embedding_json, entities_json in rows:
        try:
            pub_date = datetime.fromisoformat(pub_date_str)
        except ValueError:
            continue
        embedding = json.loads(embedding_json) if embedding_json else None
        content_embedding = json.loads(content_embedding_json) if content_embedding_json else None
        entities = json.loads(entities_json) if entities_json else None
        out.append({
            "title": title,
            "pub_date": pub_date,
            "embedding": embedding,
            "content_embedding": content_embedding,
            "entities": entities,
        })

    # دمج مع السجل الدائم على Supabase (bot_published_titles_log) — يعوّض
    # أي نقص بالسجل المحلي (مثلاً أول تشغيلة بعد إخلاء كاش GitHub Actions
    # أو أول تشغيل على جهاز/Termux جديد). عناوين موجودة محلياً أصلاً لا
    # تُكرَّر.
    local_titles = {o["title"] for o in out}
    for remote in sb_get_recent_published_titles(hours):
        if remote["title"] not in local_titles:
            out.append(remote)
            local_titles.add(remote["title"])

    return out


# ══════════════════════════════════════════════════════════════════════
#  💾 نسخة Supabase من سجل العناوين المنشورة (bot_published_titles_log)
#  — طبقة إضافية دائمة تكمّل قاعدة SQLite المحلية (hasad_data/published_
#  titles_log.db). السبب: قاعدة SQLite المحلية تُحفَظ بين تشغيلات
#  GitHub Actions عبر actions/cache بأفضل جهد فقط (restore-keys تقريبية
#  حسب أحدث نسخة متوفرة بالكاش — عرضة للإخلاء بعد 7 أيام بلا استخدام أو
#  عند امتلاء حصة الكاش)، ولا تُحفَظ إطلاقاً بين أجهزة/بيئات مختلفة
#  (مثلاً Termux محلياً مقابل GitHub Actions). جدول Supabase يضمن دوام
#  السجل 100% بغض النظر عن حالة الكاش أو بيئة التشغيل، مع إبقاء SQLite
#  كطبقة أولى أسرع (بدون أي طلب شبكة) لا تتأثر بانقطاع الاتصال بـ
#  Supabase.
# ══════════════════════════════════════════════════════════════════════
BOT_TITLES_TABLE = "bot_published_titles_log"


def sb_log_published_title(
    title: str,
    pub_date_iso: str,
    embedding: Optional[list[float]] = None,
    content_embedding: Optional[list[float]] = None,
    entities: Optional[list] = None,
) -> None:
    """يسجّل نفس الخبر بجدول bot_published_titles_log على Supabase (طبقة
    دائمة إضافية). أي فشل هنا (شبكة/صلاحية/مهلة) يُهمَل بالكامل ولا يوقف
    البوت ولا يرفع استثناء — السجل المحلي بـSQLite هو الأساس دائماً."""
    try:
        url = f"{SUPABASE_URL}/rest/v1/{BOT_TITLES_TABLE}"
        record = {
            "title": title,
            "pub_date": pub_date_iso,
            "embedding": embedding,
            "content_embedding": content_embedding,
            "entities": entities,
        }
        requests.post(
            url,
            headers={**sb_headers(), "Prefer": "return=minimal"},
            json=record,
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        log.warning(f"⚠️  تعذّر حفظ الخبر بسجل العناوين على Supabase (تُجوهل، السجل المحلي كافٍ): {e}")


def sb_get_recent_published_titles(hours: int = PUBLISHED_TITLES_MAX_AGE_HOURS) -> list[dict]:
    """يجيب الأخبار المنشورة خلال آخر عدة ساعات من جدول Supabase الدائم،
    لتعويض أي نقص بالسجل المحلي. أي فشل هنا (شبكة/صلاحية) يُرجع قائمة
    فارغة بدل تعطيل البوت — نفس فلسفة sb_log_published_title."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    out: list[dict] = []
    try:
        url = f"{SUPABASE_URL}/rest/v1/{BOT_TITLES_TABLE}"
        params = {
            "select": "title,pub_date,embedding,content_embedding,entities",
            "pub_date": f"gte.{cutoff}",
            "order": "pub_date.desc",
            "limit": "1000",
        }
        r = requests.get(url, headers=sb_headers(), params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log.warning(f"⚠️  تعذّر جلب سجل العناوين من Supabase [{r.status_code}]: {summarize_sb_error(r)}")
            return out
        for row in r.json():
            try:
                pub_date = datetime.fromisoformat(row["pub_date"])
            except (ValueError, KeyError, TypeError):
                continue
            out.append({
                "title": row.get("title", ""),
                "pub_date": pub_date,
                "embedding": row.get("embedding"),
                "content_embedding": row.get("content_embedding"),
                "entities": row.get("entities"),
            })
    except Exception as e:
        log.warning(f"⚠️  تعذّر الاتصال بـSupabase لجلب سجل العناوين (تُجوهل، السجل المحلي كافٍ): {e}")
    return out


def sb_insert(record: dict) -> Optional[str]:
    url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
    post_id = record.get("id") or str(uuid.uuid4())
    record = {**record, "id": post_id}
    delay = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, headers={**sb_headers(), "Prefer": "return=minimal"},
                               json=record, timeout=REQUEST_TIMEOUT)
            if r.status_code in (200, 201, 204):
                return post_id
            if r.status_code in (401, 403):
                log.error(f"❌ رفض الصلاحية [{r.status_code}]: {summarize_sb_error(r)}")
                return None
            if r.status_code == 409:
                status_row = get_bot_post_status(post_id)
                if status_row and status_row.get("found"):
                    log.info("   ↳ 409 لكن السجل موجود فعلاً بقاعدة البيانات — إدخالنا نجح سابقاً.")
                    return post_id
                log.info("   ↳ الخبر منشور مسبقاً بـsource_url مطابق — تخطي")
                return None
            if r.status_code == 429:
                log.warning(f"Rate limit (429) — انتظار {delay}s ...")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            log.error(f"Supabase error [{r.status_code}]: {summarize_sb_error(r)}")
        except requests.RequestException as e:
            log.warning(f"محاولة {attempt}/{MAX_RETRIES} فشلت: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return None


_AUTHOR_ID_CACHE: dict[str, str] = {}


def get_or_create_author_id(author_name: str) -> Optional[str]:
    name = (author_name or "").strip()
    if not name:
        return None

    if name in _AUTHOR_ID_CACHE:
        return _AUTHOR_ID_CACHE[name]

    url = f"{SUPABASE_URL}/rest/v1/authors"
    try:
        r = requests.get(
            url,
            headers=sb_headers(),
            params={"name": f"eq.{name}", "select": "id", "limit": "1"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 200:
            rows = r.json()
            if rows:
                author_id = rows[0]["id"]
                _AUTHOR_ID_CACHE[name] = author_id
                return author_id
    except requests.RequestException as e:
        log.warning(f"⚠️  فشل الاتصال أثناء البحث عن الكاتب '{name}': {e}")

    try:
        author_id = str(uuid.uuid4())
        r = requests.post(
            url,
            headers={**sb_headers(), "Prefer": "return=minimal"},
            json={"id": author_id, "name": name},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code in (200, 201, 204):
            _AUTHOR_ID_CACHE[name] = author_id
            log.info(f"  ➕ أُنشئ كاتب جديد بجدول authors: {name}")
            return author_id
    except requests.RequestException as e:
        log.warning(f"⚠️  فشل الاتصال أثناء إنشاء الكاتب '{name}': {e}")

    return None


try:
    RESAMPLE_FILTER = Image.Resampling.LANCZOS if Image else None
except AttributeError:
    RESAMPLE_FILTER = Image.LANCZOS


def _headline_shape_ar(text: str) -> str:
    """يهيئ نص عربي للرسم بـPillow (ربط الحروف + اتجاه RTL صحيح) — تُستخدم
    فقط كخط احتياطي عندما لا يدعم Pillow محرك raqm (شوف _headline_text_bbox
    و_headline_draw_text بالأسفل لتفادي التشكيل المزدوج)."""
    if arabic_reshaper is None or _bidi_get_display is None:
        return text
    return _bidi_get_display(arabic_reshaper.reshape(text))


def _headline_text_bbox(draw, text: str, font):
    """يقيس نص عربي. يجرّب أولاً raqm (النص الخام + direction=rtl، تشكيل
    وترتيب تلقائي صحيح لمرة واحدة)، ولو Pillow غير مبني بدعم raqm يرجع
    تلقائياً للتشكيل اليدوي (arabic_reshaper+bidi) لتفادي التشكيل المزدوج
    (الحرف المقلوب/المبعثر) الذي يحدث لو طبّقنا الاثنين معاً."""
    try:
        return draw.textbbox((0, 0), text, font=font, direction="rtl", language="ar")
    except Exception:
        return draw.textbbox((0, 0), _headline_shape_ar(text), font=font)


def _headline_draw_text(draw, xy, text: str, font, fill):
    try:
        draw.text(xy, text, font=font, fill=fill, direction="rtl", language="ar")
    except Exception:
        draw.text(xy, _headline_shape_ar(text), font=font, fill=fill)


def _headline_wrap_text(draw, text: str, font, max_width: int) -> list:
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = _headline_text_bbox(draw, test, font)
        if bbox[2] - bbox[0] <= max_width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _headline_draw_band(size: tuple, band_h: int) -> "Image.Image":
    """يرسم شريطاً سفلياً بحافة علوية مستقيمة (أسلوب القنوات العالمية:
    BBC/Reuters/Al Jazeera) بدل الموجة، مع تدرّج أسود-فحمي داخل الشريط
    وتدرّج تعتيم ناعم إضافي فوقه يذوب داخل الصورة الأصلية بدل القطع
    الفجائي، وخط حافة ذهبي رفيع مستقيم يفصل الصورة عن الشريط."""
    w, h = size
    top_y = h - band_h
    fade_h = min(HEADLINE_FADE_H, top_y)
    fade_top = top_y - fade_h

    top_rgb = HEADLINE_BAND_COLOR_TOP[:3]
    bot_rgb = HEADLINE_BAND_COLOR_BOTTOM[:3]

    band = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    band_draw = ImageDraw.Draw(band)

    # تدرّج تعتيم ناعم فوق الشريط (يذوب تدريجياً داخل الصورة الأصلية)
    for y in range(fade_top, top_y):
        t = (y - fade_top) / max(1, fade_h)
        alpha = int(255 * (t ** 1.4))
        band_draw.line([(0, y), (w, y)], fill=(*top_rgb, alpha))

    # تدرّج أسود-فحمي رأسي عبر كامل ارتفاع الشريط (معتم بالكامل)
    span = max(1, h - top_y)
    for y in range(top_y, h):
        t = (y - top_y) / span
        r = int(top_rgb[0] * (1 - t) + bot_rgb[0] * t)
        g = int(top_rgb[1] * (1 - t) + bot_rgb[1] * t)
        b = int(top_rgb[2] * (1 - t) + bot_rgb[2] * t)
        band_draw.line([(0, y), (w, y)], fill=(r, g, b, 255))

    # خط الحافة العلوية — مستقيم رفيع بدل المنحنى
    band_draw.rectangle(
        [0, top_y - HEADLINE_LINE_THICKNESS, w, top_y],
        fill=HEADLINE_CURVE_COLOR,
    )
    return band


def apply_headline_design_to_image(raw_bytes: bytes, headline_text: str) -> Optional[bytes]:
    """يصمم صورة الخبر بأسلوب المواقع العالمية الكبرى: قصّ الصورة لنسبة OG،
    ثم إضافة شريط سفلي أسود منحني فيه عنوان الخبر (مُشكَّل عربياً بشكل صحيح)
    مع شعار "24" الذهبي واسم "حصاد اليوم". يرجّع None عند أي فشل (خط ناقص/
    شعار ناقص/مكتبة ناقصة) بدون إيقاف تشغيل البوت — يُكتفى حينها بالصورة
    العادية المضغوطة."""
    if Image is None or ImageDraw is None or ImageFont is None:
        log.warning("  ⚠️  Pillow غير مكتملة (Image/ImageDraw/ImageFont) — تعذّر تصميم صورة العنوان.")
        return None
    if arabic_reshaper is None or _bidi_get_display is None:
        log.warning("  ⚠️  مكتبات النص العربي غير مثبّتة (pip install arabic-reshaper python-bidi).")
        return None
    if not os.path.isfile(HEADLINE_FONT_PATH):
        log.warning(f"  ⚠️  خط العنوان غير موجود بالمسار: {HEADLINE_FONT_PATH} — تعذّر تصميم صورة العنوان.")
        return None

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception as e:
        log.warning(f"  ⚠️  تعذّرت قراءة الصورة لتصميم العنوان: {e}")
        return None

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg

    # قصّ مركزي لنسبة OG — لكن فقط لو الصورة الأصلية قريبة بما يكفي من
    # نسبة الهدف. لو نسبتها بعيدة جداً (طولية أو مربعة مثلاً)، القص
    # سيحذف جزءاً كبيراً منها (زي حالة صورتين متجاورتين قُصّتا لرأسين
    # فقط) — فبدل ذلك نتخطى تصميم الشريط بالكامل هنا، ويُتابع الكود
    # بالمسار العادي (ضغط الصورة بأبعادها الطبيعية الكاملة بدون قص ولا
    # شريط عنوان)، بدل تشويه محتوى الصورة.
    target_w, target_h = HEADLINE_OG_WIDTH, HEADLINE_OG_HEIGHT
    target_aspect = target_w / target_h
    src_w, src_h = img.size
    src_aspect = src_w / src_h
    aspect_coverage = min(src_aspect, target_aspect) / max(src_aspect, target_aspect)
    if aspect_coverage < HEADLINE_MIN_ASPECT_COVERAGE:
        log.info(
            f"  🖼️  نسبة الصورة الأصلية بعيدة عن نسبة OG (تغطية {aspect_coverage:.0%}) "
            f"— تخطي تصميم شريط العنوان لتفادي قصّ جزء كبير من الصورة."
        )
        return None
    if src_aspect > target_aspect:
        crop_w = int(src_h * target_aspect)
        crop_h = src_h
        left = (src_w - crop_w) // 2
        top = 0
    else:
        crop_w = src_w
        crop_h = int(src_w / target_aspect)
        left = 0
        top = (src_h - crop_h) // 2
    img = img.crop((left, top, left + crop_w, top + crop_h))
    img = img.resize((target_w, target_h), RESAMPLE_FILTER)
    img = img.convert("RGBA")

    try:
        headline_font = ImageFont.truetype(HEADLINE_FONT_PATH, HEADLINE_FONT_SIZE)
    except Exception as e:
        log.warning(f"  ⚠️  تعذّر تحميل خط العنوان: {e}")
        return None

    # حافة الشريط العلوية أصبحت خطاً مستقيماً (لا انحناء)، فالهامش العلوي
    # يحتاج فقط تنفّساً بصرياً بسيطاً فوق الخط، لا تغطية قمم موجة.
    TOP_PAD = 26
    BOTTOM_PAD = 20
    LEFT_MARGIN = 30
    RIGHT_MARGIN = 40
    GAP_BETWEEN = 30  # فاصل أفقي بين كتلة الشعار وكتلة العنوان
    DIVIDER_GAP = 26  # مسافة الفاصل الرفيع بين كتلة الشعار/الاسم وكتلة العنوان
    ACCENT_BAR_W = 4  # عرض الشريط الذهبي الصغير (kicker) جنب العنوان
    ACCENT_GAP = 18   # مسافة بين الشريط الذهبي وبداية نص العنوان
    LOGO_SIZE = 108

    # قياس مسبق (بدون رسم فعلي) لعرض كتلة الشعار+الاسم — هذا العرض لا
    # يعتمد على ارتفاع الشريط، فنحسبه أولاً لمعرفة العرض المتاح للعنوان.
    measure_draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    logo_exists = os.path.isfile(HEADLINE_LOGO_PATH)
    left_block_end_x = LEFT_MARGIN
    if logo_exists:
        try:
            site_font = ImageFont.truetype(HEADLINE_FONT_PATH, 45)
        except Exception:
            site_font = headline_font
        site_gap = 22
        site_x = LEFT_MARGIN + LOGO_SIZE + site_gap
        site_bbox = _headline_text_bbox(measure_draw, HEADLINE_SITE_NAME, site_font)
        left_block_end_x = site_x + (site_bbox[2] - site_bbox[0]) + DIVIDER_GAP
    accent_x = left_block_end_x + GAP_BETWEEN
    text_start_x = accent_x + ACCENT_BAR_W + ACCENT_GAP
    max_text_w = max(100, target_w - RIGHT_MARGIN - text_start_x)

    # العنوان يُعرض كاملاً دائماً مهما طال — بدون أي قص أو حذف كلمات.
    # نجرّب أولاً بحجم الخط الافتراضي؛ إن لم يتسع بعدد الأسطر المفضّل
    # (HEADLINE_MAX_LINES) نصغّر الخط تدريجياً حتى الحد الأدنى، وإن ظل
    # النص أطول من ذلك نكتفي بأكبر عدد أسطر ينتجه الحد الأدنى — كل الكلمات
    # تبقى محفوظة داخل الأسطر (bidi/wrap لا يحذف كلمات أبداً، فقط يلفّها).
    headline_text = headline_text.strip()
    font_size = HEADLINE_FONT_SIZE
    while True:
        try:
            headline_font = ImageFont.truetype(HEADLINE_FONT_PATH, font_size)
        except Exception as e:
            log.warning(f"  ⚠️  تعذّر تحميل خط العنوان: {e}")
            return None
        lines = _headline_wrap_text(measure_draw, headline_text, headline_font, max_text_w)
        if len(lines) <= HEADLINE_MAX_LINES or font_size <= HEADLINE_FONT_MIN_SIZE:
            break
        font_size -= 2

    line_h = int(font_size * 1.25)
    text_block_h = line_h * len(lines)

    # ارتفاع الشريط: يبدأ من النسبة الافتراضية (يحافظ على الشكل المعتاد
    # للعناوين القصيرة)، ويكبر تلقائياً إن احتاج العنوان مساحة أكثر —
    # بحد أقصى يترك جزءاً من الصورة الأصلية ظاهراً فوق الشريط دائماً.
    default_band_h = int(target_h * 0.33)
    max_band_h = target_h - HEADLINE_MIN_PHOTO_VISIBLE
    required_band_h = TOP_PAD + BOTTOM_PAD + max(LOGO_SIZE, text_block_h)
    band_h = max(default_band_h, min(required_band_h, max_band_h))

    band = _headline_draw_band((target_w, target_h), band_h)
    img = Image.alpha_composite(img, band)
    draw = ImageDraw.Draw(img)

    row_top = target_h - band_h + TOP_PAD
    row_bottom = target_h - BOTTOM_PAD
    row_center_y = (row_top + row_bottom) // 2

    def _vcenter_y(bbox, center_y):
        # يحسب y بحيث يقع المركز الرأسي الفعلي للنص (حسب bbox) على center_y
        return center_y - (bbox[1] + bbox[3]) // 2

    # شعار الموقع + اسمه يسار الصف (شعار "24" الذهبي)
    try:
        if logo_exists:
            logo = Image.open(HEADLINE_LOGO_PATH).convert("RGBA")
            logo.thumbnail((LOGO_SIZE, LOGO_SIZE))
            logo_x = LEFT_MARGIN
            logo_y = row_center_y - logo.height // 2
            img.paste(logo, (logo_x, logo_y), logo)

            # مساحة تنفّس بسيطة بدل الخط العمودي الثقيل بين الشعار والاسم
            site_gap = 22
            site_x = logo_x + logo.width + site_gap

            try:
                site_font = ImageFont.truetype(HEADLINE_FONT_PATH, 45)
            except Exception:
                site_font = headline_font
            site_bbox = _headline_text_bbox(draw, HEADLINE_SITE_NAME, site_font)
            site_y = _vcenter_y(site_bbox, row_center_y)
            _headline_draw_text(draw, (site_x, site_y), HEADLINE_SITE_NAME, site_font, fill=HEADLINE_SITE_COLOR)

            # فاصل رفيع خفيف بين كتلة الشعار/الاسم وكتلة العنوان (بدل تكرار
            # نفس الخط الثقيل) — يمنح توازناً واضحاً بين يمين الصورة ويسارها
            divider_x = site_x + (site_bbox[2] - site_bbox[0]) + DIVIDER_GAP
            draw.line(
                [(divider_x, row_top + 6), (divider_x, row_bottom - 6)],
                fill=HEADLINE_DIVIDER_COLOR, width=1,
            )
        else:
            log.warning(f"  ⚠️  شعار الموقع غير موجود بالمسار: {HEADLINE_LOGO_PATH} — سيُنشر بدون شعار.")
    except Exception as e:
        log.warning(f"  ⚠️  تعذّر رسم الشعار على صورة العنوان: {e}")

    # عنوان الخبر يمين الصف، بنفس مستوى الشعار أفقياً — كل الأسطر تُرسم
    # كاملة (lines أعلاه لا يُقصّ إطلاقاً)
    start_y = row_center_y - text_block_h // 2
    right_edge = target_w - RIGHT_MARGIN

    # شريط ذهبي عمودي صغير (kicker) يفتح كتلة العنوان — لمسة القنوات العالمية
    draw.rectangle(
        [accent_x, start_y, accent_x + ACCENT_BAR_W, start_y + text_block_h],
        fill=HEADLINE_ACCENT_COLOR,
    )

    for i, line in enumerate(lines):
        bbox = _headline_text_bbox(draw, line, headline_font)
        tw = bbox[2] - bbox[0]
        x = right_edge - tw
        y = start_y + i * line_h
        _headline_draw_text(draw, (x, y), line, headline_font, fill=HEADLINE_TEXT_COLOR)

    img = img.convert("RGB")
    buf = io.BytesIO()
    try:
        img.save(buf, format="WEBP", quality=90, method=6)
    except Exception as e:
        log.warning(f"  ⚠️  فشل ترميز صورة العنوان WebP: {e}")
        return None
    return buf.getvalue()


def generate_image_filename() -> str:
    random_chars = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    timestamp = int(time.time())
    return f"{random_chars}-{timestamp}.webp"


def download_image_bytes(image_url: str) -> Optional[bytes]:
    try:
        r = requests.get(image_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.content
    except requests.RequestException as e:
        log.warning(f"  ⚠️  فشل تحميل الصورة من {image_url[:80]}: {e}")
        return None


def compress_image_to_webp(raw_bytes: bytes) -> Optional[bytes]:
    if Image is None:
        log.error("  ❌ مكتبة Pillow غير مثبّتة.")
        return None

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception as e:
        log.warning(f"  ⚠️  تعذّرت قراءة بيانات الصورة: {e}")
        return None

    if img.mode in ("P", "LA"):
        img = img.convert("RGBA")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    img.thumbnail((IMAGE_MAX_DIMENSION, IMAGE_MAX_DIMENSION), RESAMPLE_FILTER)

    quality = IMAGE_START_QUALITY
    best_attempt: Optional[bytes] = None
    while quality >= IMAGE_MIN_QUALITY:
        buf = io.BytesIO()
        try:
            img.save(buf, format="WEBP", quality=quality, method=6)
        except Exception as e:
            log.warning(f"  ⚠️  فشل ترميز WebP بجودة {quality}%: {e}")
            return None
        data = buf.getvalue()
        best_attempt = data
        if len(data) <= IMAGE_TARGET_MAX_BYTES:
            log.info(f"  🖼️  ضُغطت الصورة بجودة {quality}% → {len(data) / 1024:.1f} كيلوبايت")
            return data
        quality -= IMAGE_QUALITY_STEP

    return best_attempt


def compress_image_to_thumbnail_webp(raw_bytes: bytes) -> Optional[bytes]:
    """نفس منطق compress_image_to_webp تماماً، لكن بأبعاد وجودة النسخة
    المصغّرة (THUMB_*) بدل النسخة الكاملة (IMAGE_*). تُستخدم لتوليد
    thumbnail_image الذي تعرضه بطاقات القوائم بدل الصورة الكاملة."""
    if Image is None:
        log.error("  ❌ مكتبة Pillow غير مثبّتة.")
        return None

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
    except Exception as e:
        log.warning(f"  ⚠️  تعذّرت قراءة بيانات الصورة (thumbnail): {e}")
        return None

    if img.mode in ("P", "LA"):
        img = img.convert("RGBA")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    img.thumbnail((THUMB_MAX_DIMENSION, THUMB_MAX_DIMENSION), RESAMPLE_FILTER)

    quality = THUMB_START_QUALITY
    best_attempt: Optional[bytes] = None
    while quality >= THUMB_MIN_QUALITY:
        buf = io.BytesIO()
        try:
            img.save(buf, format="WEBP", quality=quality, method=6)
        except Exception as e:
            log.warning(f"  ⚠️  فشل ترميز WebP للصورة المصغّرة بجودة {quality}%: {e}")
            return None
        data = buf.getvalue()
        best_attempt = data
        if len(data) <= THUMB_TARGET_MAX_BYTES:
            log.info(f"  🖼️  صورة مصغّرة بجودة {quality}% → {len(data) / 1024:.1f} كيلوبايت")
            return data
        quality -= THUMB_QUALITY_STEP

    return best_attempt


def upload_image_to_supabase(image_bytes: bytes, filename: str) -> Optional[str]:
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_IMAGE_BUCKET}/{filename}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "image/webp",
        "x-upsert": "true",
        "cache-control": "31536000",
    }
    try:
        r = requests.post(upload_url, headers=headers, data=image_bytes, timeout=REQUEST_TIMEOUT)
        if r.status_code in (200, 201):
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_IMAGE_BUCKET}/{filename}"
            return public_url
        log.error(f"  ❌ فشل رفع الصورة إلى Supabase Storage [{r.status_code}]: {r.text[:200]}")
        return None
    except requests.RequestException as e:
        log.error(f"  ❌ خطأ اتصال أثناء رفع الصورة: {e}")
        return None


OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
OG_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
    re.IGNORECASE,
)
TWITTER_IMAGE_RE = re.compile(
    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
TWITTER_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    re.IGNORECASE,
)


def fetch_og_image(article_url: str) -> Optional[str]:
    if not article_url:
        return None
    try:
        r = requests.get(article_url, timeout=REQUEST_TIMEOUT, headers=ARTICLE_HEADERS)
        r.raise_for_status()
        page_html = r.text
    except requests.RequestException:
        return _fetch_image_via_jina(article_url)

    for pattern in (OG_IMAGE_RE, OG_IMAGE_RE_ALT, TWITTER_IMAGE_RE, TWITTER_IMAGE_RE_ALT):
        m = pattern.search(page_html)
        if m:
            url = html.unescape(m.group(1).strip())
            if url:
                return url
    return _fetch_image_via_jina(article_url)


_MD_IMAGE_RE = re.compile(r'!\[[^\]]*\]\((https?://[^)\s]+)\)')


def _fetch_image_via_jina(article_url: str) -> Optional[str]:
    raw = fetch_via_jina(article_url)
    if not raw:
        return None
    m = _MD_IMAGE_RE.search(raw)
    if not m:
        return None
    return html.unescape(m.group(1).strip())


_BLOCKED_LOGO_HASHES_CACHE: Optional[list] = None


def _average_hash(img: "Image.Image") -> int:
    small = img.convert("L").resize((LOGO_HASH_SIZE, LOGO_HASH_SIZE), Image.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if p > avg else "0" for p in pixels)
    return int(bits, 2)


def _hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _load_blocked_logo_hashes() -> list:
    """يجلب بصمات الشعارات المحظورة من مصدرين معاً (يُدمَجان دون إسقاط أي
    منهما، حفاظاً على الحماية الأصلية):
      1) الملفات المحلية داخل مجلد blocked_logos بالريبو (الحماية السابقة).
      2) جدول blocked_logo_hashes على Supabase (حماية إضافية، تُدار بدون
         تعديل كود أو دفع commit).
    فشل أحد المصدرين لا يوقف الآخر — يُسجَّل تحذير فقط ويُكمَل بما توفّر.
    يُخزَّن الناتج المدموج في كاش داخل الذاكرة طوال عمر التشغيلة الواحدة."""
    global _BLOCKED_LOGO_HASHES_CACHE
    if _BLOCKED_LOGO_HASHES_CACHE is not None:
        return _BLOCKED_LOGO_HASHES_CACHE

    hashes = []

    # المصدر 1: المجلد المحلي بالريبو (الحماية الأصلية)
    if Image is not None and os.path.isdir(BLOCKED_LOGOS_DIR):
        for filename in os.listdir(BLOCKED_LOGOS_DIR):
            if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            path = os.path.join(BLOCKED_LOGOS_DIR, filename)
            try:
                with Image.open(path) as logo_img:
                    hashes.append(_average_hash(logo_img))
            except Exception as e:
                log.warning(f"⚠️  تعذّر قراءة صورة الشعار المحلية ({filename}): {e}")

    # المصدر 2: جدول Supabase (حماية إضافية)
    try:
        url = f"{SUPABASE_URL}/rest/v1/blocked_logo_hashes?select=hash"
        r = requests.get(url, headers=sb_headers(), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        for row in r.json():
            try:
                hashes.append(int(row["hash"]))
            except (KeyError, ValueError, TypeError):
                continue
    except Exception as e:
        log.warning(f"⚠️  تعذّر جلب بصمات الشعارات المحظورة من Supabase: {e}")

    _BLOCKED_LOGO_HASHES_CACHE = hashes
    return hashes


def image_contains_blocked_logo(raw_bytes: bytes) -> bool:
    reference_hashes = _load_blocked_logo_hashes()
    if not reference_hashes or Image is None:
        return False

    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img_hash = _average_hash(img.convert("RGB"))
            for ref_hash in reference_hashes:
                if _hamming_distance(img_hash, ref_hash) <= LOGO_MATCH_MAX_DISTANCE:
                    return True
        return False
    except Exception as e:
        log.warning(f"⚠️  تعذّر فحص شعار الصورة: {e}")
        return False


def get_post_image_url(
    source_image_url: Optional[str], article_url: Optional[str] = None,
    headline_text: Optional[str] = None,
) -> Optional[str]:
    """يدير خط أنابيب الصورة: تحميل → (تصميم شريط العنوان أو ضغط عادي) →
    رفع إلى Supabase. لو headline_text مرّر و HEADLINE_DESIGN_ENABLED مفعّلة،
    يُصمَّم شريط أسود-ذهبي بعنوان الخبر وشعار الموقع أسفل الصورة؛ ولو فشل
    التصميم لأي سبب (خط/شعار/مكتبة ناقصة) يُتابع بالضغط العادي بدون توقف."""
    if not source_image_url and article_url:
        source_image_url = fetch_og_image(article_url)

    if not source_image_url:
        return None

    raw_bytes = download_image_bytes(source_image_url)
    if not raw_bytes:
        return None

    if image_contains_blocked_logo(raw_bytes):
        return None

    webp_bytes = None
    if headline_text and HEADLINE_DESIGN_ENABLED:
        webp_bytes = apply_headline_design_to_image(raw_bytes, headline_text)
        if webp_bytes:
            log.info("  📰  صُممت صورة الخبر بشريط العنوان.")
        else:
            log.warning("  ⚠️  تعذّر تصميم صورة العنوان — سيُتابع بالضغط العادي.")

    if not webp_bytes:
        webp_bytes = compress_image_to_webp(raw_bytes)
    if not webp_bytes:
        return None

    filename = generate_image_filename()
    public_url = upload_image_to_supabase(webp_bytes, filename)
    return public_url


def get_post_image_urls(
    source_image_url: Optional[str], article_url: Optional[str] = None,
    headline_text: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """نفس خط أنابيب get_post_image_url بالضبط، لكن يعيد أيضاً رابط نسخة
    مصغّرة (thumbnail) تُستخدم في بطاقات القوائم بالواجهة بدل الصورة الكاملة.

    يُبقي get_post_image_url الأصلية دون أي تعديل لتفادي كسر أي سكربت آخر
    يستدعيها (مصدر مصغَّر واحد فقط) — هذه دالة إضافية جديدة بجانبها.

    الصورة الأصلية تُحمَّل مرة واحدة فقط ويُشتق منها كل من النسخة الكاملة
    والمصغّرة، تفادياً لتحميل مضاعف من مصدر الخبر. النسخة المصغّرة تُولَّد
    دائماً من الصورة الأصلية النظيفة (وليس من نسخة شريط العنوان)، لأن نص
    العنوان يصبح غير مقروء عند تصغيره لأبعاد بطاقة صغيرة.

    Returns: (featured_url, thumbnail_url) — أي منهما قد يكون None عند الفشل.
    """
    if not source_image_url and article_url:
        source_image_url = fetch_og_image(article_url)

    if not source_image_url:
        return None, None

    raw_bytes = download_image_bytes(source_image_url)
    if not raw_bytes:
        return None, None

    if image_contains_blocked_logo(raw_bytes):
        return None, None

    webp_bytes = None
    if headline_text and HEADLINE_DESIGN_ENABLED:
        webp_bytes = apply_headline_design_to_image(raw_bytes, headline_text)
        if webp_bytes:
            log.info("  📰  صُممت صورة الخبر بشريط العنوان.")
        else:
            log.warning("  ⚠️  تعذّر تصميم صورة العنوان — سيُتابع بالضغط العادي.")

    if not webp_bytes:
        webp_bytes = compress_image_to_webp(raw_bytes)
    if not webp_bytes:
        return None, None

    filename = generate_image_filename()
    public_url = upload_image_to_supabase(webp_bytes, filename)

    thumbnail_url = None
    try:
        thumb_bytes = compress_image_to_thumbnail_webp(raw_bytes)
        if thumb_bytes:
            thumb_filename = f"thumb-{filename}"
            thumbnail_url = upload_image_to_supabase(thumb_bytes, thumb_filename)
    except Exception as e:
        # فشل توليد المصغّرة ليس خطأً حرجاً — الصورة الكاملة تبقى تعمل
        # والواجهة ترجع تلقائياً لعرضها لو ما فيه thumbnail_image.
        log.warning(f"  ⚠️  تعذّر توليد الصورة المصغّرة: {e}")

    return public_url, thumbnail_url


RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "excerpt": {"type": "STRING"},
        "content": {"type": "STRING"},
    },
    "required": ["title", "excerpt", "content"],
}

TITLE_ONLY_SCHEMA = {
    "type": "OBJECT",
    "properties": {"title": {"type": "STRING"}},
    "required": ["title"],
}


def build_title_only_prompt(title: str, body: str) -> str:
    return f"""\
أنت محرر عناوين محترف ورصين في موقع "حصاد اليوم" الإخباري (صنعاء). سيصلك عنوان ونص مقال رأي منسوب لكاتبه. 

مهمتك الوحيدة والصارمة:
أعد صياغة "العنوان فقط" بأسلوب صحفي عالمي وجذاب، يدمج بين الرصانة الدبلوماسية والخط الوطني والسيادي للموقع، ليكون مقنعاً ومحترماً لدى كافة القراء والطوائف بمختلف المحافظات (شمالاً وجنوباً).

المعايير العالمية والسيادية لصياغة العنوان:
1. التركيز على العمق والوقائع: ابتعد عن العناوين الإقصائية أو الشتائم السياسية والنعوت العاطفية الفجة. صغ العنوان ليعبر عن أثر الحدث، الأبعاد السيادية (كالثروات الوطنية، المرتبات، والسيادة)، أو الأبعاد الإنسانية والجامعة للمواطن اليمني كونه المحور الأساسي.
2. الرشاقة والجاذبية الصحفية: يجب أن يكون العنوان مكثفاً، بليغاً، ومثيراً لاهتمام القارئ المحايد، مع الحفاظ على الهيبة الرسمية للألقاب السيادية والمحورية المعتمدة في صنعاء عند ورودها.
3. التعبير الدقيق عن الرأي: يجب أن يختزل العنوان زاوية نظر الكاتب الأساسية ويوجه بوصلة المضمون بذكاء ومصداقية دون تحريف لفكرة المقال.

⚠️ تحذيرات وقيود صارمة:
- مَمنوع منعاً باتاً المساس بأي كلمة من نص المقال نفسه، أو تلخيصه، أو كتابة أي مقدمات أو شروح.
- مهمتك تقتصر على إعادة صياغة العنوان حصراً، وإعادته داخل قالب JSON المطلوب فقط دون أي نص خارجي.

العنوان الأصلي:
{title}

نص المقال:
{body[:4000]}

أعد كائن JSON النهائي بصيغة: {{"title": "العنوان الجديد المطور هنا"}}
"""


def build_prompt(title: str, body: str, category: str = "") -> str:
    cat = category.strip()
    neutral_categories = ["الرياضة", "رياضة", "منوعات", "طقس", "أسعار الصرف", "أسعار صرف العملات", "الذهب"]

    if any(nc in cat for nc in neutral_categories):
        return f"""
أنت محرر صحفي محترف ومتخصص في موقع "حصاد اليوم". مطلوب منك إعادة صياغة الخبر التالي ليتناسب تماماً مع قسم ({cat}) بأسلوب احترافي، خفيف، دقيق، وبعيد تماماً عن أي استقطاب سياسي أو مصطلحات عقائدية وعسكرية.

القواعد التحريرية للأقسام المحايدة:
- الأسلوب: مهني، واضح، ومباشر بحسب طبيعة القسم (رياضة ممتعة ومحايدة، منوعات مشوقة، أسعار الصرف والذهب والطقس بدقة أرقام مجردة).
- التركيز: على المعلومة والفائدة المباشرة والحدث دون إقحام السياسة أو الخلفيات الحزمية إطلاقاً.
- الصياغة: جمل متفاوتة الطول، إخفاء كامل لبصمة الآلة، والابتعاد عن الرتابة والنمطية.
- تنسيق الفقرات: اكتب content على شكل فقرات منفصلة، كل فقرة تتكون من جملتين إلى ثلاث جمل، وافصل بين كل فقرة والتي تليها بسطر فارغ كامل (سطرين جديدين \\n\\n). ممنوع كتابة المتن كفقرة واحدة متصلة مهما كان طولها.

عنوان الخبر الفيد: {title}
نص الخبر الخام:
{body[:8000]}

أعد كائن JSON: {{title, excerpt, content}}
"""
    else:
        return f"""
أنت محرر صحفي رصين ومحترف في موقع "حصاد اليوم" الإخباري (صنعاء). مهمتك هي إعادة صياغة الأخبار الخام بأسلوب يدمج بين "الخط الوطني والسيادي" وبين "المعايير التحريرية لوكالات الأنباء العالمية" ليكون المنتج النهائي رصيناً، موثقاً، ومقنعاً لجميع الطوائف والمحافظات يمنياً وعربياً.

الركائز العالمية للتحرير (تطبيق صارم):
1. قاعدة الهرم المقلوب: ابدأ بالحدث الأهم والتداعيات المباشرة في الفقرة الأولى، تليها التفاصيل والوقائع في الفقرة الثانية، واختم بالخلفية السياقية أو التاريخية للحدث في الفقرة الأخيرة.
2. الإسناد الصحفي الاحترافي (Attribution): انسب المعلومات والوقائع دائماً إلى مصادرها لتعزيز المصداقية، استخدم عبارات مثل: (بحسب مصادر محلية، وفقاً لتقارير مصرفية، أفادت بيانات رسمية، ذكرت مصادر ميدانية).
3. تجريد النعوت والصفات العاطفية: يُمنع استخدام ألفاظ المديح أو الذم الفجة (مثل: خبيث، أسطوري، مخزي، مؤامرة دنيئة). استبدلها بصياغة "الأثر والنتيجة" (مثال: بدلاً من "الانهيار الفظيع والمخزي للعملة"، اكتب: "التراجع المستمر وغير المسبوق للقيمة الشرائية للعملة المحلية"). دع الأرقام والوقائع هي من تحكم وتدين وتكشف الفساد والانفلات.

التوجيه والتوظيف التحريري (صوت المواطن والسيادة):
- البعد الإنساني الجامع: اجعل المعاناة المعيشية والخدمية للمواطن اليمني في كافة المحافظات (شمالاً وجنوباً) هي منطلق الخبر، مع إظهار مجتمعات المحافظات الواقعة تحت سيطرة الطرف الآخر كضحايا للأزمات والتدخلات الخارجية، وليس كخصوم.
- الخط السياسي والسيادي: ركز بوصلة الأخبار حول القضايا السيادية الكبرى (الملف الإنساني، صرف المرتبات من عائدات النفط والغاز، رفع القيود عن الموانئ والمطارات، وإنهاء الوجود الأجنبي)، مع تقديم مواقف سلطة صنعاء كطرف مسؤول يرتكز على الحقوق المشروعة.
- التغطية الدولية: في الأخبار الإقليمية والدولية، يتم التركيز على دعم قضايا الأمة (وعلى رأسها القضية الفلسطينية كمرتكز جامع)، ومناهضة المشاريع الاستعمارية بأسلوب تحليلي رزين وجاد.

دليل المصطلحات الإلزامي والرصين (صارم وغير قابل للتجاوز):
- "حكومة التغيير والبناء" أو "سلطات صنعاء" أو "الجيش اليمني" (عند الحديث عن القوات في صنعاء).
- "الحكومة الموالية للتحالف" أو "سلطة عدن" (بدلاً من الأوصاف الهجومية الفجة أو مصطلحات الشرعية).
- "التحالف بقيادة السعودية".
- "المحافظات/المناطق الواقعة تحت سيطرة التحالف وحكومة عدن" (توصيف جغرافي وإداري دقيق).
- استخدام الصفات الرسمية المعتمدة في صنعاء بكل رصانة وهيبة (مثل: رئيس المجلس السياسي الأعلى، قائد الثورة) عند ورودها في السياقات السيادية والمحورية.

إخفاء بصمة الآلة (النفس البشري):
- صغ المادة بجمل حيوية، سلسة، ومتفاوتة الطول، وتجنب تماماً التكرار واللوازم النمطية مثل ("وفي هذا السياق"، "جدير بالذكر").
- الخواتيم يجب أن تتنوع ذكياً بناءً على سياق الخبر الطبيعي دون قوالب جاهزة مكررة.

قواعد الصياغة الثابتة:
- أعد بناء المادة بالكامل من الصفر بأسلوبك الاحترافي الخاص.
- أول جملة في المتن (content) هي الملخص التلقائي — مكثفة، جاذبة، وتنتهي بنقطة.
- تنسيق الفقرات: اكتب content على شكل فقرات منفصلة (فقرة الهرم المقلوب، ثم فقرة التفاصيل، ثم فقرة الخلفية)، كل فقرة من جملتين إلى ثلاث جمل، وافصل بين كل فقرة والتي تليها بسطر فارغ كامل (سطرين جديدين \\n\\n). ممنوع كتابة المتن كفقرة واحدة متصلة مهما كان طولها.

تصنيف الخبر الحالي: {cat}
عنوان الخبر الفيد: {title}
نص الخبر الخام:
{body[:8000]}

أعد كائن JSON: {{title, excerpt, content}}
"""


def _parse_429(resp) -> tuple[bool, Optional[float]]:
    is_daily, retry_delay = False, None
    try:
        for detail in resp.json().get("error", {}).get("details", []):
            dtype = detail.get("@type", "")
            if "QuotaFailure" in dtype:
                for v in detail.get("violations", []):
                    qid = (v.get("quotaId", "") + v.get("quotaMetric", "")).lower()
                    if any(x in qid for x in ("perday", "per_day", "/day")):
                        is_daily = True
            if "RetryInfo" in dtype:
                m = re.match(r"(\d+(?:\.\d+)?)s", str(detail.get("retryDelay", "")))
                if m:
                    retry_delay = float(m.group(1))
    except Exception:
        pass
    if not is_daily:
        low = resp.text.lower()
        if any(x in low for x in ("perday", "per day", "/day")):
            is_daily = True
    return is_daily, retry_delay


def call_gemini(prompt_text: str, schema: dict = None) -> str:
    gen_config = {
        "temperature": 0.4,
        "maxOutputTokens": 32768,
        "responseMimeType": "application/json",
        "responseSchema": schema or RESPONSE_SCHEMA,
    }
    body = {"contents": [{"parts": [{"text": prompt_text}]}], "generationConfig": gen_config}
    headers = {"Content-Type": "application/json", "x-goog-api-key": current_key()}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            RATE_LIMITER.wait()
            resp = requests.post(model_url(), headers=headers, json=body, timeout=120)
            resp.raise_for_status()
            r = resp.json()
            if not r.get("candidates"):
                time.sleep(5)
                continue
            parts = r["candidates"][0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
        except requests.exceptions.Timeout:
            time.sleep(15)
        except requests.exceptions.HTTPError:
            code = resp.status_code
            if code == 429:
                is_daily, retry_delay = _parse_429(resp)
                if is_daily:
                    raise DailyQuotaExceeded()
                wait = min(int(retry_delay or 0) + 1 if retry_delay else 10 * attempt, MAX_BACKOFF)
                time.sleep(wait)
            elif code in (500, 503):
                time.sleep(20)
            elif code == 404:
                raise ModelUnavailable()
            elif code in (401, 403):
                raise KeyForbidden()
            else:
                raise
        except DailyQuotaExceeded:
            raise
        except ModelUnavailable:
            raise
        except KeyForbidden:
            raise
        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(10)
            else:
                raise
    raise RuntimeError("فشل الاتصال بعد كل المحاولات")


def call_with_rotation(prompt_text: str, schema: dict = None) -> str:
    global _current_group_idx, _current_key_idx, _model_stage_idx
    while True:
        current_group = KEY_GROUPS[_current_group_idx]
        try:
            return call_gemini(prompt_text, schema)
        except (DailyQuotaExceeded, ModelUnavailable, KeyForbidden) as e:
            group_label = f"مجموعة {_current_group_idx + 1}/{len(KEY_GROUPS)}"
            if isinstance(e, ModelUnavailable):
                log.warning(
                    f"  ⚠️  النموذج غير متاح لهذا المفتاح (404): {current_model()} "
                    f"[{group_label} - مفتاح {_current_key_idx + 1}/{len(current_group)}]"
                )
            elif isinstance(e, KeyForbidden):
                log.warning(
                    f"  🚫 المفتاح مرفوض (401/403): {current_model()} "
                    f"[{group_label} - مفتاح {_current_key_idx + 1}/{len(current_group)}]"
                )
            else:
                log.warning(
                    f"  🛑 انتهت الحصة اليومية ({current_model()}) "
                    f"[{group_label} - مفتاح {_current_key_idx + 1}/{len(current_group)}]"
                )

            if _current_key_idx + 1 < len(current_group):
                # لسه فيه مفاتيح تانية بنفس المجموعة الحالية لنفس مرحلة
                # النموذج — ننتقل للمفتاح التالي داخل المجموعة نفسها.
                _current_key_idx += 1
                log.info(
                    f"  🔑 مفتاح جديد [{group_label} - "
                    f"{_current_key_idx + 1}/{len(current_group)}] | {current_model()}"
                )
                continue

            if _model_stage_idx + 1 < len(MODEL_CASCADE):
                # استُنفدت مفاتيح المجموعة الحالية على النموذج الحالي —
                # ننتقل للنموذج التالي بالقائمة، بدءاً من أول مفتاح بنفس
                # المجموعة الحالية (لا ننتقل للمجموعة التالية بعد).
                _model_stage_idx += 1
                _current_key_idx = 0
                log.warning(
                    f"  🔄 استُنفدت مفاتيح {group_label} على النموذج السابق — "
                    f"التبديل المؤقت إلى {current_model()} بدءاً من أول مفتاح "
                    f"بنفس المجموعة ({_model_stage_idx + 1}/{len(MODEL_CASCADE)})."
                )
                continue

            if _current_group_idx + 1 < len(KEY_GROUPS):
                # استُنفدت مفاتيح المجموعة الحالية على كل النماذج بالقائمة —
                # ننتقل للمجموعة التالية (المفاتيح القديمة) ونعيد دورة
                # النماذج كاملة من جديد بدءاً من أول نموذج وأول مفتاح فيها.
                _current_group_idx += 1
                _model_stage_idx = 0
                _current_key_idx = 0
                log.warning(
                    f"  🔁 استُنفدت {group_label} بالكامل عبر كل النماذج — "
                    f"الانتقال إلى مجموعة {_current_group_idx + 1}/{len(KEY_GROUPS)} "
                    f"بدءاً من {current_model()}."
                )
                continue

            # استُنفدت كل المجموعات وكل المفاتيح على كل النماذج — لا مزيد
            # من الخيارات لهذا التشغيل، تُرفع الاستثناء للمتصل.
            log.error(
                f"  ❌ استُنفدت كل مجموعات المفاتيح وكل النماذج — لا مزيد من الخيارات."
            )
            raise


def rewrite_article(title: str, body: str, category: str = "") -> Optional[dict]:
    prompt = build_prompt(title, body, category)
    for attempt in range(1, 3):
        raw = call_with_rotation(prompt)
        try:
            data = json.loads(raw)
            if not all(k in data for k in ("title", "excerpt", "content")):
                return None
            return data
        except Exception as e:
            if attempt < 2:
                continue
            log.warning(f"  ⚠️  فشل تحليل رد Gemini: {e}")
            return None


def rewrite_title_only(title: str, body: str) -> Optional[str]:
    prompt = build_title_only_prompt(title, body)
    try:
        raw = call_with_rotation(prompt, schema=TITLE_ONLY_SCHEMA)
        data = json.loads(raw)
        new_title = (data.get("title") or "").strip()
        return new_title or None
    except Exception as e:
        log.warning(f"  ⚠️  فشلت إعادة صياغة العنوان فقط: {e}")
        return None


def format_content_paragraphs(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"([.؟!])\s*", r"\1\n", text)
    lines = [s.strip() for s in text.split("\n")]
    lines = [s for s in lines if s]
    return "\n".join(lines)


def make_slug(title: str) -> str:
    base = re.sub(r"[^\w\s\u0600-\u06FF-]", "", title).strip()
    base = re.sub(r"\s+", "-", base)[:80]
    suffix = uuid.uuid4().hex[:6]
    return f"{base}-{suffix}"


def word_stats(text: str) -> tuple[int, int]:
    words = len(text.split())
    reading_time = max(1, round(words / 200))
    return words, reading_time


def choose_category_name() -> str:
    print("\nاختر القسم اللي تبي تنشر فيه:")
    for key, name in CATEGORY_OPTIONS.items():
        print(f"  {key}) {name}")
    while True:
        choice = input("اكتب الرقم: ").strip()
        if choice in CATEGORY_OPTIONS:
            return CATEGORY_OPTIONS[choice]
        print("⚠️  رقم غير صحيح، جرّب مرة ثانية.")


def choose_extraction_mode() -> str:
    log.info("🧲 وضع الاستخراج التلقائي الكامل نشط.")
    return "1"


def choose_auto_mode() -> bool:
    while True:
        choice = input("\nتفعيل التلقائي؟ (كل خبر يُنشر بقسمه حسب ملفه) [Y/N]: ").strip().lower()
        if choice in ("y", "yes", "ن", "نعم"):
            return True
        if choice in ("n", "no", "لا"):
            return False
        print("⚠️  اكتب Y أو N.")


def choose_full_extraction_feeds() -> dict:
    feed_options = {
        "1": (RSS_MASA_URL, RSS_MASA_CATEGORY, "المساء"),
        "2": (RSS_ALITTIHAD_FULL_URL, RSS_ALITTIHAD_FULL_CATEGORY, "الاتحاد برس"),
        # "3": (RSS_ALKHABAR_FULL_URL, RSS_ALKHABAR_FULL_CATEGORY, "الخبر اليمني"),  # موقوف مؤقتاً
    }
    if ADEN_TM_ENABLED:
        feed_options["4"] = (RSS_ADEN_TM_FULL_URL, RSS_ADEN_TM_FULL_CATEGORY, "عدن تايم")

    print("\nأي فيد تريد العمل عليه ضمن الاستخراج الكامل؟")
    for k, (_, _, label) in feed_options.items():
        print(f"  {k}) {label}")
    print("  اكتب رقم واحد (مثال: 2)، أو أرقام مجمّعة بعلامة + (مثال: 1+3)، أو \"الكل\" للجميع.")

    while True:
        raw = input("اكتب اختيارك: ").strip().lower()
        if raw in ("الكل", "كل", "all"):
            selected_keys = list(feed_options.keys())
        else:
            parts = [p.strip() for p in raw.split("+") if p.strip()]
            if not parts or any(p not in feed_options for p in parts):
                print("⚠️  اختيار غير صحيح، جرّب مرة ثانية.")
                continue
            selected_keys = list(dict.fromkeys(parts))

        selected_feeds = {}
        chosen_labels = []
        for k in selected_keys:
            url, category, label = feed_options[k]
            selected_feeds[url] = category
            chosen_labels.append(label)
        log.info(f"📡 الفيدات المختارة للاستخراج الكامل: {'، '.join(chosen_labels)}")
        return selected_feeds


def parse_interval_minutes(raw: str) -> Optional[int]:
    raw = raw.strip()
    if raw.endswith("د"):
        raw = raw[:-1].strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None


def choose_category_overrides(new_items: list, auto_mode: bool, category_name: Optional[str]) -> dict:
    overrides: dict[int, str] = {}
    print("\nهل تريد تخصيص قسم مختلف لأي خبر من القائمة أعلاه؟")
    print("  اكتب: رقم الخبر ثم رقم القسم مفصولين بشرطة، مثال: 3-2")
    print("  (رقم القسم حسب القائمة: " + "، ".join(f"{k}={v}" for k, v in CATEGORY_OPTIONS.items()) + ")")
    print("  اترك السطر فارغاً واضغط Enter عندما تنتهي.")
    while True:
        raw = input("خبر-قسم (أو Enter للمتابعة): ").strip()
        if not raw:
            break
        m = re.match(r"^(\d+)\s*[-:]\s*(\d+)$", raw)
        if not m:
            print("⚠️  صيغة غير صحيحة.")
            continue
        news_idx, cat_key = int(m.group(1)), m.group(2)
        if news_idx < 1 or news_idx > len(new_items):
            print(f"⚠️  رقم الخبر غير موجود.")
            continue
        if cat_key not in CATEGORY_OPTIONS:
            print("⚠️  رقم القسم غير صحيح.")
            continue
        overrides[news_idx] = CATEGORY_OPTIONS[cat_key]
        print(f"  ✅ الخبر {news_idx} سيُنشر بقسم: {overrides[news_idx]}")
    return overrides


def choose_excluded_items(new_items: list) -> set[int]:
    excluded: set[int] = set()
    print("\nهل تريد منع أي خبر من القائمة من النشر نهائياً؟")
    print("  اكتب رقم الخبر واضغط إدخال (أو Enter للفراغ والمتابعة).")
    while True:
        raw = input("رقم الخبر الممنوع (أو Enter للمتابعة): ").strip()
        if not raw:
            break
        if not raw.isdigit():
            continue
        news_idx = int(raw)
        if news_idx < 1 or news_idx > len(new_items):
            continue
        excluded.add(news_idx)
        save_blocked_link(new_items[news_idx - 1]["link"])
    return excluded


def choose_skipped_items(new_items: list) -> set[int]:
    skipped_idx: set[int] = set()
    print("\nهل تريد تخطي أي خبر بهذه الجلسة فقط؟")
    while True:
        raw = input("رقم الخبر المتخطى (أو Enter للمتابعة): ").strip()
        if not raw:
            break
        if not raw.isdigit():
            continue
        news_idx = int(raw)
        if news_idx < 1 or news_idx > len(new_items):
            continue
        skipped_idx.add(news_idx)
    return skipped_idx


def choose_gemini_mode() -> str:
    print("\nكيف تريد التعامل مع Gemini؟")
    print("  1) مع Gemini عنوان+متن (الافتراضي)")
    print("  2) مع Gemini عنوان فقط دون المتن")
    print("  3) بدون Gemini عنوان+متن")
    while True:
        choice = input("اكتب الرقم: ").strip()
        if choice in ("1", "2", "3"):
            return choice
        print("⚠️  رقم غير صحيح.")


def choose_publish_mode() -> Optional[int]:
    print("\nطريقة النشر:")
    print("  1) نشر مباشر فوري (published)")
    print("  2) جدولة الأخبار بفارق زمني (scheduled)")
    while True:
        choice = input("اكتب الرقم: ").strip()
        if choice == "1":
            return None
        if choice == "2":
            while True:
                raw = input("اكتب الفارق الزمني بالدقائق: ")
                minutes = parse_interval_minutes(raw)
                if minutes:
                    return minutes
        print("⚠️  رقم غير صحيح.")


def main():
    if not RSS_FEED_CATEGORIES:
        log.error("❌ لم تُضف أي ملفات RSS بعد.")
        sys.exit(1)

    log.info("═" * 60)
    log.info("  📰  حصاد اليوم — سحب وإعادة صياغة الأخبار")
    log.info("═" * 60)

    check_system_logs_size()
    check_and_notify_scheduled_posts()

    extraction_mode = choose_extraction_mode()
    auto_mode = choose_auto_mode()
    category_name = None if auto_mode else choose_category_name()
    gemini_mode = choose_gemini_mode()

    selected_feeds = choose_full_extraction_feeds()

    if RSS_MASA_URL in selected_feeds:
        selected_feeds[RSS_MASA_URL] = choose_category_name()

    if RSS_ALITTIHAD_FULL_URL in selected_feeds:
        selected_feeds[RSS_ALITTIHAD_FULL_URL] = choose_category_name()

    if RSS_ALKHABAR_FULL_URL in selected_feeds:
        selected_feeds[RSS_ALKHABAR_FULL_URL] = choose_category_name()

    existing_urls = get_existing_source_urls()
    blocked_links = load_blocked_links()
    recent_published = get_recent_published_titles(hours=24)

    items = collect_recent_items(selected_feeds)
    new_items = [it for it in items if it["link"] not in existing_urls and it["link"] not in blocked_links]
    new_items = remove_duplicate_news(new_items, history_items=recent_published)

    log.info("─" * 60)
    log.info(f"✅ إجمالي الأخبار الجديدة المؤهلة للنشر: {len(new_items)}")
    log.info("─" * 60)

    if not new_items:
        log.info("لا يوجد أخبار جديدة حالياً.")
        return

    apply_full_extraction(new_items)
    new_items = [it for it in new_items if not it.get("_excluded")]

    if not new_items:
        log.info("لا يوجد أخبار جديدة حالياً بعد الاستبعاد.")
        return

    for idx, it in enumerate(new_items, start=1):
        shown_category = it["category"] if auto_mode else category_name
        log.info(f"  {idx}) [{shown_category}] {it['title'][:65]}")

    excluded_indices = choose_excluded_items(new_items)
    session_skip_indices = choose_skipped_items(new_items)
    overrides = choose_category_overrides(new_items, auto_mode, category_name)

    interval_minutes = choose_publish_mode()
    choice = input("اكتب 'تأكيد' للبدء الفعلي، أو أي شيء آخر للإلغاء: ").strip()
    if choice != "تأكيد":
        log.info("⏹️  تم الإلغاء.")
        return

    ok = fail = skipped = excluded_count = session_skipped_count = 0
    schedule_cursor = datetime.now(timezone.utc)
    if interval_minutes:
        schedule_cursor += timedelta(minutes=interval_minutes)

    for idx, it in enumerate(new_items, start=1):
        if idx in excluded_indices:
            excluded_count += 1
            continue
        if idx in session_skip_indices:
            session_skipped_count += 1
            continue

        is_opinion = it["category"] in NO_REWRITE_CATEGORIES
        post_category = overrides.get(idx) or (it["category"] if auto_mode else category_name)

        if is_opinion or gemini_mode == "2":
            raw_body = it["raw_body"].strip()
            new_title = rewrite_title_only(it["title"], raw_body)
            final_title = new_title or it["title"].strip()
            final_excerpt = (raw_body[:200].rstrip() + "…") if len(raw_body) > 200 else raw_body
            final_content = raw_body
        elif gemini_mode == "3":
            raw_body = it["raw_body"].strip()
            final_title = it["title"].strip()
            final_excerpt = (raw_body[:200].rstrip() + "…") if len(raw_body) > 200 else raw_body
            final_content = raw_body
        else:
            log.info(f"✍️  إعادة صياغة: {it['title'][:60]}")
            rewritten = rewrite_article(it["title"], it["raw_body"], post_category)
            if not rewritten:
                skipped += 1
                continue
            final_title = rewritten["title"].strip()
            final_excerpt = rewritten["excerpt"].strip()
            final_content = rewritten["content"]

        words, reading_time = word_stats(final_content)
        formatted_content = format_content_paragraphs(final_content)
        item_date = it["pub_date"].isoformat()

        category_id = get_category_id(post_category)
        if not category_id:
            fail += 1
            continue

        if post_category in NO_IMAGE_CATEGORIES:
            image_url, thumbnail_url = None, None
        else:
            image_url, thumbnail_url = get_post_image_urls(
                it.get("image_url"), it.get("link"), headline_text=final_title,
            )

        if interval_minutes:
            publish_dt = schedule_cursor
            schedule_cursor += timedelta(minutes=interval_minutes)
            publish_time = publish_dt.isoformat()
            record_status = "scheduled"
            record_extra = {"scheduled_at": publish_time}
            created_updated = item_date
        else:
            record_status = "published"
            record_extra = {}
            created_updated = item_date

        record = {
            "title": final_title,
            "slug": make_slug(final_title),
            "excerpt": final_excerpt,
            "content": formatted_content,
            "category_id": category_id,
            "source_type": SOURCE_LABEL,
            "source_url": it["link"],
            "status": record_status,
            "word_count": words,
            "reading_time": reading_time,
            "created_at": created_updated,
            "updated_at": created_updated,
            "published_at": created_updated if record_status == "published" else None,
            "featured_image": image_url,
            "thumbnail_image": thumbnail_url,
            "is_featured": post_category in FEATURED_SLIDER_CATEGORIES,
            **record_extra,
        }

        if is_opinion:
            opinion_author_name = it.get("author") or DEFAULT_OPINION_AUTHOR
            author_id = get_or_create_author_id(opinion_author_name)
            if author_id:
                record["author_id"] = author_id

        post_id = sb_insert(record)
        if post_id:
            ok += 1
            log_published_title(record["title"], record["created_at"])
            if not interval_minutes:
                seed_views(post_id)

            canonical_url = build_canonical_url(record["slug"], record.get("published_at") or record["created_at"])
            if interval_minutes:
                pending = load_pending_scheduled()
                pending.append({
                    "id": post_id,
                    "title": record["title"],
                    "slug": record["slug"],
                    "created_at": record["created_at"],
                    "scheduled_at": publish_time,
                })
                save_pending_scheduled(pending)
            else:
                send_to_telegram(record["title"], canonical_url)
                log_discovery_ready([canonical_url])
        else:
            fail += 1

    log.info("═" * 60)
    log.info(f"📊 نُشر: {ok} / فشل: {fail} / تُخُطّي: {skipped} / مُنع: {excluded_count} / تخطي مؤقت: {session_skipped_count}")
    log.info("═" * 60)


if __name__ == "__main__":
    main()

