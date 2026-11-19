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

async function saveState(category: string, enabled: boolean): Promise<void> {
  const response = await db(`${STATE_TABLE}?on_conflict=category`, {
    method: "POST",
    headers: { prefer: "resolution=merge-duplicates,return=minimal" },
    body: JSON.stringify({ category, enabled, updated_at: new Date().toISOString() }),
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
        await saveState(category, enabled);
        await botCall(token, "answerCallbackQuery", {
          callback_query_id: callback.id,
          text: `تم ${enabled ? "تشغيل" : "إيقاف"} ${category}`,
        });
        await sendMenu(token, ADMIN_CHAT_ID, `تم ${enabled ? "تشغيل" : "إيقاف"} ${category} في قناة تيليجرام فقط.`);
      }
    }
    return;
  }

  const text = String(message.text ?? "").trim();
  const command = text.split(/\s+/, 1)[0];
  if (command === "/start" || command === "/sections" || command.startsWith("/start@") || command.startsWith("/sections@")) {
    await sendMenu(token, ADMIN_CHAT_ID, "تحكم بنشر الأقسام في قناة تيليجرام. إيقاف القسم لا يؤثر على نشره في الموقع.");
    return;
  }

  const categories = await loadCategories();
  for (const category of categories) {
    if (text === `⏸ إيقاف ${category}` || text === `▶️ تشغيل ${category}`) {
      const enabled = text.startsWith("▶️ تشغيل ");
      await saveState(category, enabled);
      await sendMenu(token, ADMIN_CHAT_ID, `تم ${enabled ? "تشغيل" : "إيقاف"} ${category} في قناة تيليجرام فقط.`);
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
    await handleUpdate(update);
    return json({ ok: true });
  } catch (_error) {
    return json({ error: "Could not process Telegram update" }, 500);
  }
});
