-- Persistent Telegram getUpdates cursor for private source-channel polling.
CREATE TABLE IF NOT EXISTS public.bot_source_cursors (
    source_key text PRIMARY KEY,
    update_id bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.bot_source_cursors ENABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.bot_source_cursors TO service_role;
