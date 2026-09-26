"""Read private Telegram channel posts for Hasad Al-Youm.

The source bot is intentionally separate from the output bot used by the
publisher. Updates are checkpointed in Supabase only after the article batch
has been handled, so transient failures are retried on the next run.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import requests

CURSOR_KEY = "hasad_private_source_channel"
CURSOR_TABLE = "bot_source_cursors"
REQUEST_TIMEOUT = 30
MAX_TELEGRAM_DOWNLOAD_BYTES = 20 * 1024 * 1024
logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\]\[(){}]+", re.IGNORECASE)
_VIDEO_HOST_RE = re.compile(
    r"(?:youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|facebook\.com|fb\.watch|"
    r"instagram\.com|tiktok\.com|twitter\.com|x\.com|streamable\.com|rumble\.com)",
    re.IGNORECASE,
)
_VIDEO_EXT_RE = re.compile(r"\.(?:mp4|webm|mov|m3u8)(?:$|[?#])", re.IGNORECASE)


def extract_video_url(text: str, entities: list[dict[str, Any]] | None = None) -> str | None:
    """Extract visible video URLs and Telegram's hidden text_link URLs."""
    candidates = list(_URL_RE.findall(text or ""))
    for entity in entities or []:
        if entity.get("type") == "text_link" and entity.get("url"):
            candidates.append(str(entity["url"]))
    for candidate in candidates:
        candidate = candidate.rstrip(".,؛،")
        if _VIDEO_HOST_RE.search(candidate) or _VIDEO_EXT_RE.search(candidate):
            return candidate
    return None


class TelegramFileTooLargeError(RuntimeError):
    """The hosted Telegram Bot API cannot download this file size."""


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, code: int, description: str):
        self.method = method
        self.code = code
        self.description = description
        super().__init__(f"Telegram {method} returned {code}: {description}")


def _required_config() -> tuple[str, str, str, str]:
    token = os.environ.get("TELEGRAM_SOURCE_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_SOURCE_CHAT_ID", "").strip()
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not token or not chat_id:
        raise RuntimeError(
            "Telegram source is not configured: set TELEGRAM_SOURCE_BOT_TOKEN "
            "and TELEGRAM_SOURCE_CHAT_ID."
        )
    if not supabase_url or not service_key:
        raise RuntimeError(
            "Telegram cursor storage requires SUPABASE_URL and SUPABASE_SERVICE_KEY."
        )
    return token, chat_id, supabase_url, service_key


def is_configured() -> bool:
    token = os.environ.get("TELEGRAM_SOURCE_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_SOURCE_CHAT_ID", "").strip()
    if bool(token) != bool(chat_id):
        raise RuntimeError(
            "Set both TELEGRAM_SOURCE_BOT_TOKEN and TELEGRAM_SOURCE_CHAT_ID, or leave both unset."
        )
    return bool(token and chat_id)


def _supabase_headers(service_key: str) -> dict[str, str]:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }


def get_last_update_id(supabase_url: str, service_key: str) -> int:
    response = requests.get(
        f"{supabase_url}/rest/v1/{CURSOR_TABLE}",
        headers=_supabase_headers(service_key),
        params={"select": "update_id", "source_key": f"eq.{CURSOR_KEY}", "limit": "1"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    rows = response.json()
    return int(rows[0]["update_id"]) if rows else 0


def save_last_update_id(supabase_url: str, service_key: str, update_id: int) -> None:
    response = requests.post(
        f"{supabase_url}/rest/v1/{CURSOR_TABLE}?on_conflict=source_key",
        headers={
            **_supabase_headers(service_key),
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json={
            "source_key": CURSOR_KEY,
            "update_id": int(update_id),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def _post_link(chat: dict[str, Any], message_id: int) -> str:
    username = (chat.get("username") or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}/{message_id}"
    chat_id = str(chat.get("id", ""))
    # Private Telegram links omit the Bot API's -100 prefix.
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return f"https://t.me/c/{chat_id.lstrip('-')}/{message_id}"


def _to_news_item(update: dict[str, Any], expected_chat_id: str) -> dict[str, Any] | None:
    post = update.get("channel_post") or update.get("edited_channel_post")
    if not isinstance(post, dict):
        return None
    is_edited_post = isinstance(update.get("edited_channel_post"), dict)
    chat = post.get("chat") or {}
    if str(chat.get("id", "")) != expected_chat_id:
        return None

    raw_text = (post.get("text") or post.get("caption") or "").strip()
    photo_sizes = post.get("photo") or []
    largest_photo = max(
        photo_sizes,
        key=lambda photo: (
            int(photo.get("width") or 0) * int(photo.get("height") or 0),
            int(photo.get("file_size") or 0),
        ),
        default=None,
    )
    document = post.get("document") or {}
    document_is_image = str(document.get("mime_type") or "").lower().startswith("image/")
    if not document_is_image and not document.get("mime_type"):
        document_is_image = bool(re.search(
            r"\.(?:jpe?g|png|webp|gif|bmp|tiff?)$",
            str(document.get("file_name") or ""),
            re.IGNORECASE,
        ))
    image_file_id = (largest_photo or {}).get("file_id") or (
        document.get("file_id") if document_is_image else None
    )
    reply_to = post.get("reply_to_message") or {}
    reply_to_message_id = reply_to.get("message_id")
    entities = (post.get("entities") or []) + (post.get("caption_entities") or [])
    video_url = extract_video_url(raw_text, entities)
    has_media = bool(image_file_id or video_url)
    is_attachment_reply = bool(reply_to_message_id and has_media)
    is_media_edit = bool(is_edited_post and has_media)
    original_text = (reply_to.get("text") or reply_to.get("caption") or "").strip()
    article_text = original_text if is_attachment_reply and original_text else raw_text
    if not article_text and not (is_attachment_reply or is_media_edit):
        return None

    message_id = int(post["message_id"])
    update_id = int(update["update_id"])
    source_message_id = int(reply_to_message_id) if is_attachment_reply else message_id
    source_date = reply_to.get("date") if is_attachment_reply and original_text else post.get("date")
    published_at = datetime.fromtimestamp(
        int(source_date or 0), tz=timezone.utc
    )

    # A photo/video reply is an attachment to the original channel post, not a
    # new article. Return the original post URL so the publisher can merge it
    # in-batch or update its already-published article if it arrives later.
    if is_attachment_reply or is_media_edit:
        lines = [line.strip() for line in article_text.splitlines() if line.strip()]
        title = lines[0] if lines else article_text
        return {
            "title": title,
            "link": _post_link(chat, source_message_id),
            "pub_date": published_at,
            "raw_body": article_text,
            "source_feed": f"telegram://{expected_chat_id}",
            "image_url": None,
            "category": "أخبار وتقارير",
            "author": None,
            "_telegram_source": True,
            "_telegram_photo_reply": bool(image_file_id) and is_attachment_reply,
            "_telegram_video_reply": bool(video_url) and is_attachment_reply,
            "_telegram_media_edit": is_media_edit,
            "_telegram_reply_message_id": message_id,
            "_telegram_reply_to_message_id": int(reply_to_message_id) if reply_to_message_id is not None else None,
            "_telegram_update_id": update_id,
            "_telegram_photo_file_id": image_file_id,
            "_telegram_video_url": video_url,
        }

    if not raw_text:
        return None
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    title = lines[0] if lines else raw_text
    return {
        "title": title,
        "link": _post_link(chat, message_id),
        "pub_date": published_at,
        "raw_body": raw_text,
        "source_feed": f"telegram://{expected_chat_id}",
        "image_url": None,
        "category": "أخبار وتقارير",
        "author": None,
        "_telegram_source": True,
        "_telegram_update_id": update_id,
        "_telegram_photo_file_id": image_file_id,
        "_telegram_video_url": video_url,
    }


def merge_photo_replies_with_news_items(
    items: list[dict[str, Any]],
    existing_source_urls: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach in-batch reply photos/videos to their original news items.

    Returns (news_items, late_attachment_replies). Late replies have no original
    news item in this getUpdates batch and must be matched to an already
    published post by the caller.
    """
    def has_reply_media(item: dict[str, Any]) -> bool:
        return bool(item.get("_telegram_photo_reply") or item.get("_telegram_video_reply"))

    def has_edit_media(item: dict[str, Any]) -> bool:
        return bool(item.get("_telegram_media_edit"))

    news_items = [item for item in items if not has_reply_media(item) and not has_edit_media(item)]
    news_by_link = {item.get("link"): item for item in news_items}
    late_replies = []
    existing_source_urls = existing_source_urls or set()
    def attach_media(original: dict[str, Any], attachment: dict[str, Any]) -> None:
        if attachment.get("_telegram_photo_file_id"):
            original["_telegram_photo_file_id"] = attachment.get("_telegram_photo_file_id")
        if attachment.get("_telegram_video_url"):
            original["_telegram_video_url"] = attachment.get("_telegram_video_url")
        original["_telegram_update_id"] = max(
            int(original.get("_telegram_update_id") or 0),
            int(attachment.get("_telegram_update_id") or 0),
        )

    for reply in items:
        if not has_reply_media(reply) and not has_edit_media(reply):
            continue
        source_url = reply.get("link")
        original = news_by_link.get(source_url)

        if has_edit_media(reply):
            if source_url in existing_source_urls:
                late_replies.append(reply)
            elif original:
                attach_media(original, reply)
                logger.info("Attached edited Telegram media to source post message_id=%s.", reply.get("_telegram_update_id"))
            else:
                news_items.append(reply)
                news_by_link[source_url] = reply
            continue

        if source_url in existing_source_urls:
            late_replies.append(reply)
            continue
        if not original:
            late_replies.append(reply)
            continue
        attach_media(original, reply)
        logger.info(
            "Attached Telegram reply media to source post message_id=%s.",
            reply.get("_telegram_reply_to_message_id"),
        )
    return news_items, late_replies


def download_telegram_photo(file_id: str) -> bytes:
    """Download a channel photo via the dedicated source bot (max 20 MB)."""
    token, _, _, _ = _required_config()
    try:
        metadata_response = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as error:
        raise RuntimeError(f"Telegram getFile request failed ({type(error).__name__}).") from None
    if not metadata_response.ok:
        raise RuntimeError(f"Telegram getFile returned HTTP {metadata_response.status_code}.")
    metadata = metadata_response.json()
    if not metadata.get("ok"):
        raise RuntimeError("Telegram getFile failed: " + str(metadata.get("description", "unknown error")))
    file_info = metadata.get("result") or {}
    file_path = file_info.get("file_path")
    file_size = int(file_info.get("file_size") or 0)
    if not file_path:
        raise RuntimeError("Telegram getFile response did not include file_path.")
    if file_size > MAX_TELEGRAM_DOWNLOAD_BYTES:
        raise TelegramFileTooLargeError("Telegram photo exceeds the Bot API 20 MB download limit.")
    try:
        file_response = requests.get(
            f"https://api.telegram.org/file/bot{token}/{file_path}",
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
    except requests.RequestException as error:
        raise RuntimeError(f"Telegram photo download failed ({type(error).__name__}).") from None
    if not file_response.ok:
        raise RuntimeError(f"Telegram photo download returned HTTP {file_response.status_code}.")
    chunks: list[bytes] = []
    downloaded = 0
    try:
        for chunk in file_response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            downloaded += len(chunk)
            if downloaded > MAX_TELEGRAM_DOWNLOAD_BYTES:
                raise TelegramFileTooLargeError("Telegram photo exceeds the Bot API 20 MB download limit.")
            chunks.append(chunk)
    except requests.RequestException as error:
        raise RuntimeError(f"Telegram photo download failed ({type(error).__name__}).") from None
    return b"".join(chunks)


def _telegram_api_call(token: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/{method}",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as error:
        raise RuntimeError(f"Telegram {method} request failed ({type(error).__name__}).") from None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    description = str(payload.get("description", "unknown error")).replace(token, "[redacted]")
    if not response.ok or not payload.get("ok"):
        error_code = int(payload.get("error_code") or response.status_code or 500)
        if method == "getUpdates" and error_code == 409:
            if "webhook" in description.casefold():
                description += ". A webhook was found after the preflight check; inspect who re-enabled it."
            else:
                description += ". Stop every other process polling this source bot token."
        raise TelegramAPIError(method, error_code, description)
    return payload


def _ensure_polling_mode(token: str) -> None:
    """Disable an existing webhook without dropping queued updates before polling."""
    webhook_info = _telegram_api_call(token, "getWebhookInfo").get("result") or {}
    if not (webhook_info.get("url") or "").strip():
        return
    pending_count = int(webhook_info.get("pending_update_count") or 0)
    logger.warning(
        "Telegram source bot has an active webhook; deleting it with pending updates preserved (%s queued).",
        pending_count,
    )
    _telegram_api_call(token, "deleteWebhook", {"drop_pending_updates": "false"})
    verified = _telegram_api_call(token, "getWebhookInfo").get("result") or {}
    if (verified.get("url") or "").strip():
        raise RuntimeError("Telegram source webhook is still active after deleteWebhook.")
    logger.info("Telegram source webhook removed; pending updates were preserved for polling.")


def _verify_source_channel(token: str, expected_chat_id: str) -> str:
    bot = _telegram_api_call(token, "getMe").get("result") or {}
    bot_id = bot.get("id")
    username = (bot.get("username") or "unknown").strip().lstrip("@")
    if not bot_id:
        raise RuntimeError("Telegram getMe did not return a bot id.")
    try:
        member = _telegram_api_call(
            token,
            "getChatMember",
            {"chat_id": expected_chat_id, "user_id": bot_id},
        ).get("result") or {}
    except TelegramAPIError as error:
        if error.code == 400:
            raise RuntimeError(
                f"Cannot verify configured Telegram source chat {expected_chat_id}; "
                "check that it is the channel chat ID and the source bot is a member/admin."
            ) from None
        raise
    status = str(member.get("status") or "unknown")
    if status not in {"administrator", "creator"}:
        raise RuntimeError(
            f"Telegram source bot @{username} (id {bot_id}) is not an administrator "
            f"of configured chat {expected_chat_id}; current status: {status}."
        )
    logger.info(
        "Verified Telegram source bot @%s (id %s) is %s in configured channel %s.",
        username,
        bot_id,
        status,
        expected_chat_id,
    )
    return username


def fetch_telegram_items() -> tuple[list[dict[str, Any]], int | None]:
    """Fetch pending posts and return the maximum update id for later commit."""
    if not is_configured():
        return [], None
    token, expected_chat_id, supabase_url, service_key = _required_config()
    last_update_id = get_last_update_id(supabase_url, service_key)
    _ensure_polling_mode(token)
    _verify_source_channel(token, expected_chat_id)
    payload = _telegram_api_call(
        token,
        "getUpdates",
        {
            "offset": last_update_id + 1,
            "limit": 100,
            "timeout": 0,
            "allowed_updates": '["channel_post","edited_channel_post"]',
        },
    )
    updates = payload.get("result") or []
    if not updates:
        logger.info(
            "Telegram source returned no pending channel_post/edited_channel_post updates for configured channel %s.",
            expected_chat_id,
        )
        return [], None

    highest_update_id = max(int(update["update_id"]) for update in updates)
    channel_posts = [
        update.get("channel_post") or update.get("edited_channel_post")
        for update in updates
        if isinstance(update.get("channel_post") or update.get("edited_channel_post"), dict)
    ]
    observed_chat_counts: dict[str, int] = {}
    for post in channel_posts:
        observed_id = str((post.get("chat") or {}).get("id", "missing"))
        observed_chat_counts[observed_id] = observed_chat_counts.get(observed_id, 0) + 1
    matching_posts = [
        post for post in channel_posts
        if str((post.get("chat") or {}).get("id", "")) == expected_chat_id
    ]
    matching_with_text = sum(
        bool((post.get("text") or post.get("caption") or "").strip())
        for post in matching_posts
    )
    matching_with_photo = sum(
        bool(
            post.get("photo")
            or str((post.get("document") or {}).get("mime_type") or "").lower().startswith("image/")
        )
        for post in matching_posts
    )
    logger.info(
        "Telegram update diagnostics: updates=%s, channel_posts=%s, matching_chat=%s, "
        "matching_with_text_or_caption=%s, matching_with_photo_or_image_document=%s.",
        len(updates),
        len(channel_posts),
        len(matching_posts),
        matching_with_text,
        matching_with_photo,
    )
    if channel_posts and not matching_posts:
        logger.warning(
            "Telegram channel_post/edited_channel_post chat IDs do not match TELEGRAM_SOURCE_CHAT_ID; observed IDs: %s.",
            observed_chat_counts,
        )
    elif matching_posts and not matching_with_text:
        logger.warning(
            "Telegram updates match the configured channel but contain no text/caption; "
            "text-only news cannot be rewritten. Matching posts with photos/image documents: %s.",
            matching_with_photo,
        )
    items = []
    for update in updates:
        item = _to_news_item(update, expected_chat_id)
        if item:
            items.append(item)
    return items, highest_update_id


def commit_telegram_cursor(update_id: int | None) -> None:
    if update_id is None or not is_configured():
        return
    _, _, supabase_url, service_key = _required_config()
    save_last_update_id(supabase_url, service_key, update_id)
