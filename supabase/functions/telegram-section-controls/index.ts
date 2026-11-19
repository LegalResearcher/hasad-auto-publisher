import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? Deno.env.get("SUPABASE_SERVICE_KEY") ?? "";
const ADMIN_CHAT_ID = "85820797";
const BOT_CONFIG_TABLE = "bot_telegram_runtime_config";
const STATE_TABLE = "bot_telegram_section_controls";
const CATEGORY_TABLE = "categories";

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

async function db(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: SERVICE_KEY,
      authorization: `Bearer ${SERVICE_KEY}`,
      "content-type": "application/json",
      ...(init.headers ?? {}),
    },
  });
}

async function botCall(token: string, method: string, payload: unknown): Promise<any> {
  const response = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(`Telegram ${method} returned ${response.status}`);
  return data.result;
}

async function loadToken(): Promise<string | null> {
  const response = await db(`${BOT_CONFIG_TABLE}?select=bot_token&singleton=eq.true&limit=1`);
  if (!response.ok) throw new Error("Could not read bot configuration");
  const rows = await response.json();
  return rows[0]?.bot_token ?? null;
}

async function loadCategories(): Promise<string[]> {
  const response = await db(`${CATEGORY_TABLE}?select=name&order=name.asc&limit=100`);
  if (!response.ok) throw new Error("Could not read site categories");
  const rows = await response.json();
  return [...new Set(rows.map((row: { name: string }) => row.name).filter(Boolean))];
}

async function loadStates(): Promise<Map<string, boolean>> {
  const response = await db(`${STATE_TABLE}?select=category,enabled&limit=200`);
  if (!response.ok) throw new Error("Could not read category publishing states");
  const rows = await response.json();
  return new Map(rows.map((row: { category: string; enabled: boolean }) => [row.category, row.enabled]));
}

function makeKeyboard(categories: string[], states: Map<string, boolean>): object {
  const labels = categories.map((name) => `${states.get(name) === false ? "▶️ تشغيل" : "⏸ إيقاف"} ${name}`);
  return {
    keyboard: Array.from({ length: Math.ceil(labels.length / 2) }, (_, i) => labels.slice(i * 2, i * 2 + 2)),
    resize_keyboard: true,
    is_persistent: true,
    one_time_keyboard: false,
  };
}

async function saveState(category: string, enabled: boolean, disabledAt: string | null = null, updatedAt = new Date().toISOString()): Promise<void> {
  const response = await db(`${STATE_TABLE}?on_conflict=category`, {
    method: "POST",
    headers: { prefer: "resolution=merge-duplicates,return=minimal" },
    body: JSON.stringify({ category, enabled, disabled_at: disabledAt, updated_at: updatedAt }),
  });
  if (!response.ok) throw new Error("Could not save category publishing state");
}

async function sendMenu(token: string, chatId: string, text: string): Promise<void> {
  const categories = await loadCategories();
  const states = await loadStates();
  await botCall(token, "sendMessage", {
    chat_id: chatId,
    text,
    reply_markup: makeKeyboard(categories, states),
  });
}

const BACKFILL_CATEGORY = "أخبار وتقارير";
const SITE_BASE_URL = "https://www.hasad-alyoum.com";
const SHARE_BASE_URL = "https://hasad-alyoum.com";
const CHANNEL_URL = "https://t.me/hasadalyoum";

async function getCategoryId(category: string): Promise<string | null> {
  const query = new URLSearchParams({ select: "id", name: `eq.${category}`, limit: "1" });
  const response = await db(`${CATEGORY_TABLE}?${query}`);
  if (!response.ok) throw new Error("Could not find category id");
  const rows = await response.json();
  return rows[0]?.id ?? null;
}

async function getSectionState(category: string): Promise<{ enabled: boolean; updated_at: string; disabled_at: string | null } | null> {
  const query = new URLSearchParams({ select: "enabled,updated_at,disabled_at", category: `eq.${category}`, limit: "1" });
  const response = await db(`${STATE_TABLE}?${query}`);
  if (!response.ok) throw new Error("Could not read previous section state");
  const rows = await response.json();
  return rows[0] ?? null;
}

async function queueBackfill(category: string, disabledAt: string, reenabledAt: string): Promise<number> {
  if (category !== BACKFILL_CATEGORY) return 0;
  const categoryId = await getCategoryId(category);
  if (!categoryId) return 0;
  const sentIds = new Set<string>();
  for (let offset = 0; ; offset += 500) {
    const sentQuery = new URLSearchParams({
      select: "post_id", category: `eq.${category}`, sent_at: `gte.${disabledAt}`,
      limit: "500", offset: String(offset),
    });
    const sentResponse = await db(`bot_telegram_sent_posts?${sentQuery}`);
    if (!sentResponse.ok) throw new Error("Could not read Telegram sent-post history");
    const sentRows = await sentResponse.json();
    for (const row of sentRows) sentIds.add(row.post_id);
    if (sentRows.length < 500) break;
  }

  let queuedCount = 0;
  for (let offset = 0; ; offset += 500) {
    const postsQuery = new URLSearchParams({
      select: "id", category_id: `eq.${categoryId}`, status: "eq.published",
      published_at: `gte.${disabledAt}`, order: "published_at.asc",
      limit: "500", offset: String(offset),
    });
    // Use an upper bound so a slow scan cannot pull in articles from after re-enable.
    postsQuery.set("and", `(published_at.lte.${reenabledAt})`);
    const postsResponse = await db(`posts?${postsQuery}`);
    if (!postsResponse.ok) throw new Error("Could not find posts published during pause");
    const posts = await postsResponse.json();
    const pending = posts.filter((post: { id: string }) => !sentIds.has(post.id));
    for (let i = 0; i < pending.length; i += 100) {
      const batch = pending.slice(i, i + 100).map((post: { id: string }) => ({
        post_id: post.id,
        category,
        status: "pending",
      }));
      const queueResponse = await db("bot_telegram_backfill_queue?on_conflict=post_id", {
        method: "POST",
        headers: { prefer: "resolution=ignore-duplicates,return=representation" },
        body: JSON.stringify(batch),
      });
      if (!queueResponse.ok) throw new Error("Could not queue paused Telegram posts");
      queuedCount += (await queueResponse.json()).length;
    }
    if (posts.length < 500) break;
  }
  return queuedCount;
}

async function updateQueue(postId: string, values: Record<string, unknown>): Promise<void> {
  const query = new URLSearchParams({ post_id: `eq.${postId}` });
  const response = await db(`bot_telegram_backfill_queue?${query}`, {
    method: "PATCH",
    headers: { prefer: "return=minimal" },
    body: JSON.stringify(values),
  });
  if (!response.ok) throw new Error("Could not update backfill queue record");
}

async function wasSent(postId: string): Promise<boolean> {
  const query = new URLSearchParams({ select: "post_id", post_id: `eq.${postId}`, limit: "1" });
  const response = await db(`bot_telegram_sent_posts?${query}`);
  if (!response.ok) throw new Error("Could not check whether Telegram post was already sent");
  return (await response.json()).length > 0;
}

async function markSent(postId: string, category: string): Promise<void> {
  const response = await db("bot_telegram_sent_posts?on_conflict=post_id", {
    method: "POST",
    headers: { prefer: "resolution=ignore-duplicates,return=minimal" },
    body: JSON.stringify({ post_id: postId, category, sent_at: new Date().toISOString() }),
  });
  if (!response.ok) throw new Error("Could not record Telegram send");
}

function escapeHtml(value: string): string {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#x27;");
}

function buildTelegramCaption(post: Record<string, any>, articleUrl: string): string {
  const title = escapeHtml(String(post.title ?? "").trim());
  let excerpt = escapeHtml(String(post.excerpt ?? "").trim());
  const xText = `${String(post.excerpt ?? "").trim()}\n\n${SHARE_BASE_URL}/share/${post.id}\n\nحصاد اليوم\nانضم إلى قناتنا: ${CHANNEL_URL}`;
  const encoded = encodeURIComponent(xText).replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
  const footer = `\n\nــــــــــــــــــــــــــــ\n\n<b>🔗 اقرأ الخبر كاملاً عبر موقع حصاد اليوم:\n${escapeHtml(articleUrl)}\n\n𝕏 <a href="https://x.com/intent/post?text=${encoded}">مشاركة الخبر على منصة X</a>\n\n📲 للاشتراك بالقناة عبر تيليجرام:\n${CHANNEL_URL}</b>`;
  const caption = (summary: string) => `<b>${title}</b>${summary ? `\n\n<blockquote expandable>${summary}</blockquote>` : ""}${footer}`;
  let result = caption(excerpt);
  const visible = (html: string) => [...html.replace(/<[^>]+>/g, "").replace(/&(?:amp|lt|gt|quot|#x27);/g, "x")]
    .reduce((length, character) => length + (character.codePointAt(0)! > 0xffff ? 2 : 1), 0);
  if (visible(result) > 1024 && excerpt) {
    const available = Math.max(0, 1024 - visible(caption("")) - 40);
    excerpt = available ? `${excerpt.slice(0, available).trimEnd()}…` : "";
    result = caption(excerpt);
  }
  return result;
}

function canonicalUrl(post: Record<string, any>): string {
  const date = new Date(post.published_at ?? post.created_at);
  const yemenDate = new Date(date.getTime() + 3 * 60 * 60 * 1000);
  const year = yemenDate.getUTCFullYear();
  const month = String(yemenDate.getUTCMonth() + 1).padStart(2, "0");
  const day = String(yemenDate.getUTCDate()).padStart(2, "0");
  return `${SITE_BASE_URL}/${year}/${month}/${day}/${post.slug}`;
}

async function sendBackfillPost(token: string, post: Record<string, any>): Promise<void> {
  const articleUrl = canonicalUrl(post);
  const caption = buildTelegramCaption(post, articleUrl);
  if (post.featured_image && /^https?:\/\//i.test(post.featured_image)) {
    try {
      await botCall(token, "sendPhoto", {
        chat_id: "@hasadalyoum", photo: post.featured_image,
        caption, parse_mode: "HTML", show_caption_above_media: false,
      });
      return;
    } catch {
      // Match the regular publisher's text fallback if Telegram cannot fetch a photo.
    }
  }
  await botCall(token, "sendMessage", {
    chat_id: "@hasadalyoum", text: caption, parse_mode: "HTML", disable_web_page_preview: true,
  });
}

async function acquireBackfillLock(owner: string): Promise<boolean> {
  const now = new Date().toISOString();
  const lockedUntil = new Date(Date.now() + 5 * 60 * 1000).toISOString();
  const query = new URLSearchParams({
    singleton: "eq.true",
    or: `(locked_until.is.null,locked_until.lt.${now})`,
  });
  const response = await db(`bot_telegram_backfill_worker?${query}`, {
    method: "PATCH",
    headers: { prefer: "return=representation" },
    body: JSON.stringify({ owner, locked_until: lockedUntil }),
  });
  if (!response.ok) throw new Error("Could not acquire Telegram backfill lock");
  return (await response.json()).length > 0;
}

async function releaseBackfillLock(owner: string): Promise<void> {
  const query = new URLSearchParams({ singleton: "eq.true", owner: `eq.${owner}` });
  const response = await db(`bot_telegram_backfill_worker?${query}`, {
    method: "PATCH",
    headers: { prefer: "return=minimal" },
    body: JSON.stringify({ owner: null, locked_until: new Date().toISOString() }),
  });
  if (!response.ok) throw new Error("Could not release Telegram backfill lock");
}

async function claimNextBackfillItem(): Promise<{ post_id: string; category: string } | null> {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const query = new URLSearchParams({
      select: "post_id,category", category: `eq.${BACKFILL_CATEGORY}`,
      status: "eq.pending", order: "queued_at.asc", limit: "1",
    });
    const response = await db(`bot_telegram_backfill_queue?${query}`);
    if (!response.ok) throw new Error("Could not read Telegram backfill queue");
    const entries = await response.json();
    if (!entries.length) return null;
    const claimQuery = new URLSearchParams({ post_id: `eq.${entries[0].post_id}`, status: "eq.pending" });
    const claim = await db(`bot_telegram_backfill_queue?${claimQuery}`, {
      method: "PATCH",
      headers: { prefer: "return=representation" },
      body: JSON.stringify({ status: "processing", processing_at: new Date().toISOString() }),
    });
    if (!claim.ok) throw new Error("Could not claim Telegram backfill item");
    const claimed = await claim.json();
    if (claimed.length) return claimed[0];
  }
  return null;
}

async function drainBackfillQueue(): Promise<void> {
  const token = await loadToken();
  if (!token) return;
  const owner = crypto.randomUUID();
  if (!await acquireBackfillLock(owner)) return;
  let processed = 0;
  let continueAfterRelease = false;
  try {
    // A worker that died mid-send leaves a processing row. Do not retry it
    // blindly: mark it for manual review to avoid a duplicate channel post.
    const staleBefore = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    const staleQuery = new URLSearchParams({ category: `eq.${BACKFILL_CATEGORY}`, status: "eq.processing", processing_at: `lt.${staleBefore}` });
    const staleResponse = await db(`bot_telegram_backfill_queue?${staleQuery}`, {
      method: "PATCH",
      headers: { prefer: "return=minimal" },
      body: JSON.stringify({ status: "failed", processing_at: null, last_error: "Worker interrupted; verify channel before retrying" }),
    });
    if (!staleResponse.ok) throw new Error("Could not recover stale Telegram backfill claims");

    for (;;) {
      const entry = await claimNextBackfillItem();
      if (!entry) break;
      try {
        if (await wasSent(entry.post_id)) {
          await updateQueue(entry.post_id, { status: "sent", processing_at: null, sent_at: new Date().toISOString() });
          continue;
        }
        const postQuery = new URLSearchParams({
          select: "id,title,slug,excerpt,featured_image,published_at,created_at,category_id,status",
          id: `eq.${entry.post_id}`, limit: "1",
        });
        const postResponse = await db(`posts?${postQuery}`);
        if (!postResponse.ok) throw new Error("Could not load queued article");
        const posts = await postResponse.json();
        const post = posts[0];
        const categoryId = await getCategoryId(BACKFILL_CATEGORY);
        if (!post || post.status !== "published" || post.category_id !== categoryId) {
          await updateQueue(entry.post_id, { status: "failed", processing_at: null, last_error: "Post is no longer published in the queued category" });
          continue;
        }
        await sendBackfillPost(token, post);
        await markSent(entry.post_id, BACKFILL_CATEGORY);
        await updateQueue(entry.post_id, { status: "sent", processing_at: null, sent_at: new Date().toISOString(), last_error: null });
        await new Promise((resolve) => setTimeout(resolve, 3000));
        processed += 1;
        if (processed >= 30) {
          continueAfterRelease = true;
          break;
        }
      } catch (_error) {
        await updateQueue(entry.post_id, { status: "pending", processing_at: null, last_error: "Telegram send failed; will retry on next control action" });
        break;
      }
    }
  } finally {
    await releaseBackfillLock(owner);
  }
  if (continueAfterRelease) {
    await fetch(`${SUPABASE_URL}/functions/v1/telegram-section-controls`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-telegram-bot-api-secret-token": await getWebhookSecret() },
      body: JSON.stringify({ resume_backfill: true }),
    });
  }
}

async function setSectionState(category: string, enabled: boolean): Promise<number> {
  const previous = await getSectionState(category);
  const currentEnabled = previous?.enabled ?? true;
  const pendingBackfillStart = previous?.disabled_at ?? (previous?.enabled === false ? previous.updated_at : null);

  if (!enabled) {
    if (!currentEnabled) return 0; // repeated stop must not reset the pause window
    const disabledAt = previous?.disabled_at ?? new Date().toISOString();
    await saveState(category, false, disabledAt, disabledAt);
    return 0;
  }

  const shouldBackfill = category === BACKFILL_CATEGORY && Boolean(pendingBackfillStart);
  if (currentEnabled && !shouldBackfill) return 0;

  if (!shouldBackfill) {
    await saveState(category, true, null);
    return 0;
  }

  // Enable first, then capture a fixed upper bound. The disabled_at marker stays
  // until queueing succeeds, so a transient DB error can be retried safely.
  const initialEnabledAt = new Date().toISOString();
  await saveState(category, true, pendingBackfillStart, initialEnabledAt);
  const reenabledAt = new Date().toISOString();
  const queued = await queueBackfill(category, pendingBackfillStart!, reenabledAt);
  await saveState(category, true, null, reenabledAt);
  EdgeRuntime.waitUntil(drainBackfillQueue());
  return queued;
}

async function handleUpdate(update: any): Promise<void> {
  const token = await loadToken();
  if (!token) return;
  const message = update.message ?? update.callback_query?.message;
  if (!message || String(message.chat?.id ?? "") !== ADMIN_CHAT_ID) return;

  if (update.callback_query) {
    const callback = update.callback_query;
    const match = /^section:(on|off):(\d+)$/.exec(callback.data ?? "");
    if (match) {
      const categories = await loadCategories();
      const category = categories[Number(match[2])];
      if (category) {
        const enabled = match[1] === "on";
        const queued = await setSectionState(category, enabled);
        await botCall(token, "answerCallbackQuery", {
          callback_query_id: callback.id,
          text: `تم ${enabled ? "تشغيل" : "إيقاف"} ${category}`,
        });
        const extra = queued ? ` وسيُستدرك ${queued} خبرًا قديمًا من فترة الإيقاف.` : "";
        await sendMenu(token, ADMIN_CHAT_ID, `تم ${enabled ? "تشغيل" : "إيقاف"} ${category} في قناة تيليجرام فقط.${extra}`);
      }
    }
    return;
  }

  const text = String(message.text ?? "").trim();
  const command = text.split(/\s+/, 1)[0];
  if (command === "/start" || command === "/sections" || command.startsWith("/start@") || command.startsWith("/sections@")) {
    EdgeRuntime.waitUntil(drainBackfillQueue());
    await sendMenu(token, ADMIN_CHAT_ID, "تحكم بنشر الأقسام في قناة تيليجرام. إيقاف القسم لا يؤثر على نشره في الموقع.");
    return;
  }

  const categories = await loadCategories();
  for (const category of categories) {
    if (text === `⏸ إيقاف ${category}` || text === `▶️ تشغيل ${category}`) {
      const enabled = text.startsWith("▶️ تشغيل ");
      const queued = await setSectionState(category, enabled);
      const extra = queued ? ` وسيُستدرك ${queued} خبرًا قديمًا من فترة الإيقاف.` : "";
      await sendMenu(token, ADMIN_CHAT_ID, `تم ${enabled ? "تشغيل" : "إيقاف"} ${category} في قناة تيليجرام فقط.${extra}`);
      return;
    }
  }
}

async function getWebhookSecret(): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(SERVICE_KEY));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("").slice(0, 48);
}

Deno.serve(async (request: Request) => {
  if (!SUPABASE_URL || !SERVICE_KEY) return json({ error: "Server is not configured" }, 503);

  const url = new URL(request.url);
  if (url.pathname.endsWith("/setup")) {
    if (request.method !== "POST" || request.headers.get("authorization") !== `Bearer ${SERVICE_KEY}`) {
      return json({ error: "Unauthorized" }, 401);
    }
    try {
      const { bot_token } = await request.json();
      if (typeof bot_token !== "string" || !/^\d+:[A-Za-z0-9_-]+$/.test(bot_token)) {
        return json({ error: "Invalid bot token" }, 400);
      }
      const me = await botCall(bot_token, "getMe", {});
      const save = await db(`${BOT_CONFIG_TABLE}?on_conflict=singleton`, {
        method: "POST",
        headers: { prefer: "resolution=merge-duplicates,return=minimal" },
        body: JSON.stringify({ singleton: true, bot_token, updated_at: new Date().toISOString() }),
      });
      if (!save.ok) throw new Error("Could not store bot configuration");
      const secret = await getWebhookSecret();
      await botCall(bot_token, "setWebhook", {
        url: `${SUPABASE_URL}/functions/v1/telegram-section-controls`,
        secret_token: secret,
        allowed_updates: ["message", "callback_query"],
        drop_pending_updates: false,
      });
      await sendMenu(bot_token, ADMIN_CHAT_ID, "لوحة التحكم بأقسام قناة تيليجرام جاهزة. الإيقاف لا يؤثر على النشر في الموقع.");
      return json({ ok: true, bot_username: me.username });
    } catch (_error) {
      return json({ error: "Webhook setup failed; check the configured credentials and Supabase logs" }, 502);
    }
  }

  if (request.method !== "POST") return json({ ok: true, service: "telegram-section-controls" });
  if (request.headers.get("x-telegram-bot-api-secret-token") !== await getWebhookSecret()) {
    return json({ error: "Unauthorized" }, 401);
  }
  try {
    const update = await request.json();
    if (update?.resume_backfill === true) {
      EdgeRuntime.waitUntil(drainBackfillQueue());
      return json({ ok: true });
    }
    await handleUpdate(update);
    return json({ ok: true });
  } catch (_error) {
    return json({ error: "Could not process Telegram update" }, 500);
  }
});
