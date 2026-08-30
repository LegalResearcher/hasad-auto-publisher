import fcntl
import os
import sys
from typing import Optional

# ══════════════════════════════════════════════════════════════════════
#  🔒 قفل ملف يمنع تشغيلين متزامنين لهذا السكربت (مثلاً لو تشغيل سابق
#  عبر cron لسه شغّال ولم ينتهِ خلال 5 دقائق، ولحق عليه تشغيل تالٍ).
#  بدون هذا القفل، عمليتان قد تعالجان نفس عنصر الفيد بنفس اللحظة تماماً
#  (نفس source_url ونفس created_at) لكن بصياغتين مختلفتين من Gemini
#  فيُنشر الخبر مرتين — وهي الحالة التي رُصدت فعلياً بقاعدة البيانات.
#  اللو ك غير محظر (non-blocking): لو تشغيل آخر ماسك القفل، هذا التشغيل
#  يخرج فوراً بدل ما ينتظر أو يعالج بالتوازي.
# ══════════════════════════════════════════════════════════════════════
# ملاحظة: على GitHub Actions لا حاجة فعلية لهذا القفل — كل تشغيلة على
# runner منفصل تماماً أصلاً. أبقيناه فقط لبقاء نفس السلوك لو شغّلت
# السكربت يدوياً من مكان آخر بالتوازي.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.environ.get("BOT_DATA_DIR", os.path.join(_SCRIPT_DIR, "hasad_data"))
LOCK_FILE_PATH = os.path.join(_DATA_DIR, "auto_publish_alittihad_alkhabar.lock")

sys.path.insert(0, _SCRIPT_DIR)

from hasad_news_bot_fixed import (
    log,
    RSS_MASA_URL,
    RSS_MASA_CATEGORY,
    RSS_ALITTIHAD_FULL_URL,
    RSS_ALITTIHAD_FULL_CATEGORY,
    RSS_ALKHABAR_FULL_URL,
    RSS_ALKHABAR_FULL_CATEGORY,
    RSS_YPAGENCY_FULL_URL,
    RSS_YPAGENCY_FULL_CATEGORY,
    RSS_YPAGENCY_GLOBAL_URL,
    RSS_YPAGENCY_GLOBAL_CATEGORY,
    RSS_YPAGENCY_ARAB_URL,
    RSS_YPAGENCY_ARAB_CATEGORY,
    RSS_YPAGENCY_GULF_URL,
    RSS_YPAGENCY_GULF_CATEGORY,
    RSS_YPAGENCY_REPORTS_URL,
    RSS_YPAGENCY_REPORTS_CATEGORY,
    RSS_YPAGENCY_ANALYSIS_URL,
    RSS_YPAGENCY_ANALYSIS_CATEGORY,
    RSS_YPAGENCY_OCCUPIED_PALESTINE_URL,
    RSS_YPAGENCY_OCCUPIED_PALESTINE_CATEGORY,
    NO_REWRITE_CATEGORIES,
    NO_IMAGE_CATEGORIES,
    FEATURED_SLIDER_CATEGORIES,
    DEFAULT_OPINION_AUTHOR,
    check_system_logs_size,
    check_and_notify_scheduled_posts,
    get_existing_source_urls,
    get_recent_published_titles,
    log_published_title,
    load_blocked_links,
    save_blocked_link,
    collect_recent_items,
    remove_duplicate_news,
    remove_raw_duplicate_news,
    remove_content_duplicate_news,
    get_recent_raw_items,
    log_raw_content,
    apply_full_extraction,
    build_prompt,
    call_with_rotation,
    get_category_id,
    get_post_image_urls,
    download_image_bytes,
    image_contains_blocked_logo,
    compress_image_to_webp,
    compress_image_to_thumbnail_webp,
    upload_image_to_supabase,
    generate_image_filename,
    fetch_og_image,
    word_stats,
    format_content_paragraphs,
    make_slug,
    get_or_create_author_id,
    sb_insert,
    seed_views,
    build_canonical_url,
    send_to_telegram,
    log_discovery_ready,
    apply_headline_design_to_image,
    HEADLINE_DESIGN_ENABLED,
)
import json

# ══════════════════════════════════════════════════════════════════════
#  🔒 نسخة تلقائية مقيّدة — تعمل فقط على فيدات:
#     - المساء برس
#     - الاتحاد برس
#     - الخبر اليمني (موقوف مؤقتاً)
#     - وكالة الصحافة اليمنية (موقوف مؤقتاً)
#     - سكاي نيوز عربية (رياضة) (موقوف مؤقتاً)
#     - سكاي نيوز عربية (تكنولوجيا)
#  وتنشر تلقائياً (فوري) في قسم "أخبار وتقارير" (أو "رياضة"/"تكنولوجيا"
#  بحسب مصدر الخبر)
#  تُشغَّل عبر cron كل 5 دقائق — بدون أي تفاعل يدوي.
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
#  📋 ضخ مشاهدات تليجرام (حصاد تليجرام)
#  هذا السكربت لا يتعامل مع الضخ إطلاقاً ولا يعرف رابط منشور تليجرام
#  الناتج (send_to_telegram ترجع True/False فقط، لا رابطاً). اكتشاف
#  الرابط والضخ يتم بالكامل عبر منظومة مستقلة من سكربتين:
#    - hasad_telegram_scraper.py: يزحف على الصفحة العامة لمعاينة
#      القناة (t.me/s/hasadalyoum) بعد كل نشر ليكتشف رابط كل منشور
#      جديد فعلياً، ويضيفه لقائمة انتظار الضخ.
#    - hasad_telegram_booster.py: يضخ رابطاً واحداً فقط من هذه القائمة
#      في كل تشغيل له عبر cron كل 10 دقائق.
# ══════════════════════════════════════════════════════════════════════

TARGET_CATEGORY = "أخبار وتقارير"
INTERNATIONAL_CATEGORY = "شؤون دولية"
SPORT_CATEGORY = "رياضة"
TECH_CATEGORY = "منوعات وتكنولوجيا"
WORLD_CATEGORY = "أخبار العالم"

# فيد الرياضة (سكاي نيوز عربية) — مضاف مباشرة هنا لأنه غير معرّف
# في hasad_news_bot_fixed
RSS_SKYNEWS_SPORT_URL = "https://www.skynewsarabia.com/rss/sport.xml"

# فيد التكنولوجيا (سكاي نيوز عربية) — نفس منطق فيد الرياضة تماماً
RSS_SKYNEWS_TECH_URL = "https://www.skynewsarabia.com/web/rss/technology.xml"

RSS_SKYNEWS_WORLD_URL = "http://www.skynewsarabia.com/web/rss/world.xml"

SELECTED_FEEDS = {
    # RSS_MASA_URL: TARGET_CATEGORY,  # موقوف مؤقتاً
    # RSS_ALITTIHAD_FULL_URL: TARGET_CATEGORY,  # موقوف مؤقتاً
    # RSS_ALKHABAR_FULL_URL: TARGET_CATEGORY,  # موقوف مؤقتاً
    RSS_YPAGENCY_FULL_URL: RSS_YPAGENCY_FULL_CATEGORY,
    RSS_YPAGENCY_GLOBAL_URL: RSS_YPAGENCY_GLOBAL_CATEGORY,
    RSS_YPAGENCY_ARAB_URL: RSS_YPAGENCY_ARAB_CATEGORY,
    RSS_YPAGENCY_GULF_URL: RSS_YPAGENCY_GULF_CATEGORY,
    RSS_YPAGENCY_REPORTS_URL: RSS_YPAGENCY_REPORTS_CATEGORY,
    RSS_YPAGENCY_ANALYSIS_URL: RSS_YPAGENCY_ANALYSIS_CATEGORY,
    RSS_YPAGENCY_OCCUPIED_PALESTINE_URL: RSS_YPAGENCY_OCCUPIED_PALESTINE_CATEGORY,
    RSS_SKYNEWS_SPORT_URL: SPORT_CATEGORY,
    RSS_SKYNEWS_TECH_URL: TECH_CATEGORY,
    RSS_SKYNEWS_WORLD_URL: WORLD_CATEGORY,
}

EXTENDED_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "excerpt": {"type": "STRING"},
        "content": {"type": "STRING"},
        "news_scope": {"type": "STRING"},
    },
    "required": ["title", "excerpt", "content", "news_scope"],
}

# ══════════════════════════════════════════════════════════════════════
#  🌍 تمييز الأخبار الدولية عبر Gemini نفسه (وليس عبر breadcrumb الموقع،
#  لأنه تبيّن أنه غير موثوق/غير موجود أصلاً بهذه المصادر الثلاثة).
#  البرومبت الأصلي (build_prompt) يُستخدم كما هو حرفياً دون حذف أي حرف،
#  ويُضاف إليه فقط تعليمة تصنيف إضافية + حقل جديد بمخرجات JSON.
# ══════════════════════════════════════════════════════════════════════

SCOPE_INSTRUCTIONS_SUFFIX = """
تصنيف إضافي إلزامي (بالإضافة لما سبق، لا يلغي أي تعليمة سابقة):
بعد إعادة الصياغة، حدد نطاق الخبر عبر حقل إضافي باسم "news_scope"، بقيمة واحدة فقط من اثنتين:

- "دولي": إذا كان الخبر يتناول بشكل أساسي حدثاً أو شأناً خارج اليمن (كدولة أو أطراف أو قضية دولية/إقليمية لا يشكل فيها الشأن اليمني المحور الرئيسي المباشر للخبر)، مثل: أخبار إيران، إسرائيل، أمريكا، السعودية الداخلية، دول الخليج، الأمم المتحدة، إلخ، بمعزل عن كونها تمس اليمن بشكل غير مباشر.
- "يمني": إذا كان الخبر يتناول بشكل أساسي شأناً يمنياً داخلياً (سياسياً، عسكرياً، اقتصادياً، اجتماعياً)، حتى لو تضمن إشارات لأطراف خارجية ضمن سياق الشأن اليمني.

أعد كائن JSON النهائي بصيغة: {title, excerpt, content, news_scope}
"""


def rewrite_article_with_scope(title: str, body: str, category: str = "") -> Optional[dict]:
    """يستخدم برومبت hasad_news_bot_fixed.build_prompt كما هو دون أي تعديل
    أو حذف، ويضيف إليه فقط تعليمة تصنيف الخبر (دولي/يمني) في نهايته."""
    base_prompt = build_prompt(title, body, category)
    prompt = base_prompt + SCOPE_INSTRUCTIONS_SUFFIX
    for attempt in range(1, 3):
        raw = call_with_rotation(prompt, schema=EXTENDED_RESPONSE_SCHEMA)
        try:
            data = json.loads(raw)
            if not all(k in data for k in ("title", "excerpt", "content", "news_scope")):
                if attempt < 2:
                    continue
                return None
            return data
        except Exception as e:
            if attempt < 2:
                continue
            log.warning(f"  ⚠️  فشل تحليل رد Gemini: {e}")
            return None
    return None


def get_masa_image_url(source_image_url, article_url=None, headline_text=None):
    """نفس خط أنابيب get_post_image_urls القياسي بالضبط (تحميل ← فحص شعار
    محظور ← تصميم شريط العنوان أو ضغط WebP ← رفع ← توليد مصغّرة)، لكنه
    مخصّص فقط لأخبار فيد المساء برس، بحيث أي تعديل مستقبلي هنا لا يمس
    باقي الفيدات (الاتحاد/الخبر/سكاي نيوز) إطلاقاً. لو الصورة تحتوي شعار
    المساء برس أو كانت هي نفسها صورة الشعار (بعد إضافتها لمجلد
    blocked_logos)، تُستبعد ويُترك الخبر بدون صورة.
    Returns: (featured_url, thumbnail_url) — أي منهما قد يكون None عند الفشل."""
    if not source_image_url and article_url:
        source_image_url = fetch_og_image(article_url)
    if not source_image_url:
        return None, None
    raw_bytes = download_image_bytes(source_image_url)
    if not raw_bytes:
        return None, None
    if image_contains_blocked_logo(raw_bytes):
        log.info("  🚫 [المساء برس] الصورة تحتوي شعار المصدر — سيُنشر الخبر بدون صورة.")
        return None, None

    webp_bytes = None
    if headline_text and HEADLINE_DESIGN_ENABLED:
        webp_bytes = apply_headline_design_to_image(raw_bytes, headline_text)
        if webp_bytes:
            log.info("  📰  [المساء برس] صُممت صورة الخبر بشريط العنوان.")
        else:
            log.warning("  ⚠️  [المساء برس] تعذّر تصميم صورة العنوان — سيُتابع بالضغط العادي.")

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
        log.warning(f"  ⚠️  [المساء برس] تعذّر توليد الصورة المصغّرة: {e}")

    return public_url, thumbnail_url


def run():
    log.info("═" * 60)
    log.info("  📰  حصاد اليوم — تشغيل تلقائي (المساء برس + الاتحاد برس + سكاي نيوز تكنولوجيا) [الخبر اليمني ووكالة الصحافة اليمنية وسكاي نيوز رياضة موقوفة مؤقتاً]")
    log.info("═" * 60)

    check_system_logs_size()
    check_and_notify_scheduled_posts()

    existing_urls = get_existing_source_urls()
    blocked_links = load_blocked_links()
    recent_published = get_recent_published_titles(hours=24)
    recent_raw = get_recent_raw_items()

    items = collect_recent_items(SELECTED_FEEDS)
    new_items = [
        it for it in items
        if it["link"] not in existing_urls and it["link"] not in blocked_links
    ]
    # 🥇 الطبقة الأولى: مقارنة المتن الخام (قبل Gemini) — تمسك حالات نسخ
    # البيانات الرسمية حرفياً أو شبه حرفياً عبر عدة فيدات، وتوفر استدعاء
    # Gemini على الأخبار المكررة أصلاً.
    new_items = remove_raw_duplicate_news(new_items, history_items=recent_raw)
    # 🥈 الطبقة الثانية: تشابه دلالي على العناوين، لالتقاط الحالات التي
    # أُعيدت صياغة عنوانها بشكل قريب من عنوان خبر آخر.
    new_items = remove_duplicate_news(new_items, history_items=recent_published)
    # 🥉 الطبقة الثالثة: تشابه دلالي على مقدمة المحتوى + كيان مشترك إلزامي
    # (جهة/شخصية معروفة بالخبرين)، لالتقاط خبرين مكتوبين باستقلالية كاملة
    # (عنواناً ومتناً) عن نفس التصريح/الحدث — الحالة التي تفلت من الطبقتين
    # الأوليين لأن لا النص ولا العنوان متشابهان نصياً، لكن المحتوى والجهة
    # المعنية نفسها.
    new_items = remove_content_duplicate_news(new_items, history_items=recent_published)

    # ─────────────────────────────────────────────────────────────
    # 🖼️ فيدات سكاي نيوز (الرياضة والتكنولوجيا) تزوّد رابط صورة مصغّرة
    # (thumbnail) ضمن عناصر الفيد نفسها، بجودة أقل من صورة المقال الفعلية.
    # بقية الفيدات (المساء برس مثلاً) لا توفر صورة إطلاقاً بالفيد،
    # فيعتمد السكربت تلقائياً على og:image من صفحة الخبر نفسها (أعلى
    # جودة). لتوحيد المنطق، نتجاهل هنا صورة الفيد المصغّرة لأخبار
    # سكاي نيوز فقط، لنجبر نفس آلية og:image عالية الجودة أن تُستخدم.
    # ─────────────────────────────────────────────────────────────
    for it in new_items:
        if it.get("source_feed") in (RSS_SKYNEWS_SPORT_URL, RSS_SKYNEWS_TECH_URL, RSS_SKYNEWS_WORLD_URL):
            it["image_url"] = None

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

    ok = fail = skipped = 0

    for it in new_items:
        base_category = it["category"] or TARGET_CATEGORY
        is_opinion = base_category in NO_REWRITE_CATEGORIES

        log.info(f"✍️  إعادة صياغة: {it['title'][:60]}")
        rewritten = rewrite_article_with_scope(it["title"], it["raw_body"], base_category)
        if not rewritten:
            skipped += 1
            continue
        final_title = rewritten["title"].strip()
        final_excerpt = rewritten["excerpt"].strip()
        final_content = rewritten["content"]
        news_scope = (rewritten.get("news_scope") or "").strip()

        if it.get("source_feed") == RSS_YPAGENCY_FULL_URL and news_scope == "دولي":
            log.info(f"  🚫 [وكالة الصحافة اليمنية] تخطي خبر دولي: {it['title'][:60]}")
            skipped += 1
            continue

        post_category = (
            INTERNATIONAL_CATEGORY
            if news_scope == "دولي" and base_category not in (SPORT_CATEGORY, TECH_CATEGORY, WORLD_CATEGORY)
            else base_category
        )
        if post_category == INTERNATIONAL_CATEGORY:
            log.info(f"     ↳ 🌍 خبر دولي مصنَّف عبر Gemini — سيُنشر بقسم: {INTERNATIONAL_CATEGORY}")

        words, reading_time = word_stats(final_content)
        formatted_content = format_content_paragraphs(final_content)
        item_date = it["pub_date"].isoformat()

        category_id = get_category_id(post_category)
        if not category_id:
            fail += 1
            continue

        if post_category in NO_IMAGE_CATEGORIES:
            image_url, thumbnail_url = None, None
        elif it.get("source_feed") == RSS_MASA_URL:
            image_url, thumbnail_url = get_masa_image_url(
                it.get("image_url"), it.get("link"), headline_text=final_title,
            )
        else:
            image_url, thumbnail_url = get_post_image_urls(
                it.get("image_url"), it.get("link"), headline_text=final_title,
            )

        if it.get("source_feed") == RSS_YPAGENCY_FULL_URL and not image_url:
            log.info(f"  🚫 [وكالة الصحافة اليمنية] تخطي خبر بلا صورة: {it['title'][:60]}")
            skipped += 1
            continue

        record = {
            "title": final_title,
            "slug": make_slug(final_title),
            "excerpt": final_excerpt,
            "content": formatted_content,
            "category_id": category_id,
            "source_type": "حصاد اليوم | متابعات",
            "source_url": it["link"],
            "status": "published",
            "word_count": words,
            "reading_time": reading_time,
            "created_at": item_date,
            "updated_at": item_date,
            "published_at": item_date,
            "featured_image": image_url,
            "thumbnail_image": thumbnail_url,
            "is_featured": post_category in FEATURED_SLIDER_CATEGORIES,
        }

        if is_opinion:
            opinion_author_name = it.get("author") or DEFAULT_OPINION_AUTHOR
            author_id = get_or_create_author_id(opinion_author_name)
            if author_id:
                record["author_id"] = author_id

        post_id = sb_insert(record)
        if post_id:
            ok += 1
            log_published_title(
                record["title"],
                record["created_at"],
                embedding=it.get("_title_embedding"),
                content_embedding=it.get("_content_embedding"),
                entities=it.get("_entities"),
            )
            log_raw_content(it["title"], it.get("raw_body", ""), record["created_at"])
            save_blocked_link(it["link"])  # منع إعادة النشر مستقبلاً حتى لو حُذف الخبر من الموقع
            seed_views(post_id)
            canonical_url = build_canonical_url(record["slug"], record.get("published_at") or record["created_at"])

            # إرسال الخبر إلى تليجرام. ملاحظة: send_to_telegram ترجع
            # True/False فقط (نجاح/فشل الإرسال) وليست رابط المنشور —
            # اكتشاف الرابط الفعلي يتم لاحقاً وبشكل مستقل تماماً عبر
            # سكربت hasad_telegram_scraper.py الذي يزحف على الصفحة
            # العامة لمعاينة القناة (t.me/s/hasadalyoum).
            send_to_telegram(f"{post_category} - {record['title']}", canonical_url)

            log_discovery_ready([canonical_url])
        else:
            fail += 1

    log.info("═" * 60)
    log.info(f"📊 نُشر: {ok} / فشل: {fail} / تُخُطّي: {skipped}")
    log.info("═" * 60)


def _acquire_lock_or_exit():
    """يفتح ملف القفل ويحاول مسكه بشكل غير محظر (LOCK_EX | LOCK_NB). لو
    تشغيل آخر ماسكه فعلاً، يطبع تحذيراً بنفس نظام log ويخرج فوراً
    (exit code مختلف عن نجاح/فشل عادي حتى يظهر بوضوح لو رُوجعت السجلات).
    الملف نفسه يُفتح ويُبقى مفتوحاً طوال حياة العملية عمداً (لا يُغلق
    يدوياً) — القفل يتحرر تلقائياً عند خروج العملية مهما كان السبب
    (نجاح، فشل، استثناء غير متوقع)، فما فيه خطر قفل معلّق للأبد."""
    os.makedirs(os.path.dirname(LOCK_FILE_PATH), exist_ok=True)
    lock_file = open(LOCK_FILE_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.warning(
            "  🔒 تشغيل سابق لهذا السكربت لسه شغّال (القفل ممسوك) — "
            "تخطي هذا التشغيل بالكامل لمنع معالجة نفس الأخبار مرتين."
        )
        sys.exit(0)
    return lock_file  # يُبقى بمتغيّر حتى لا يُجمَّع بواسطة GC فيُغلَق القفل قبل الأوان


if __name__ == "__main__":
    _lock_handle = _acquire_lock_or_exit()
    run()
