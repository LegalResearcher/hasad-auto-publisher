CREATE TABLE IF NOT EXISTS public.bot_telegram_sent_posts (
  post_id uuid PRIMARY KEY REFERENCES public.posts(id) ON DELETE CASCADE,
  category text NOT NULL,
  sent_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.bot_telegram_sent_posts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.bot_telegram_sent_posts FROM anon, authenticated;
GRANT ALL ON public.bot_telegram_sent_posts TO service_role;
CREATE INDEX IF NOT EXISTS idx_bot_telegram_sent_posts_category_sent_at
  ON public.bot_telegram_sent_posts(category, sent_at);

CREATE TABLE IF NOT EXISTS public.bot_telegram_backfill_queue (
  post_id uuid PRIMARY KEY REFERENCES public.posts(id) ON DELETE CASCADE,
  category text NOT NULL,
  queued_at timestamptz NOT NULL DEFAULT now(),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'sent', 'failed')),
  sent_at timestamptz,
  last_error text
);

ALTER TABLE public.bot_telegram_backfill_queue ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.bot_telegram_backfill_queue FROM anon, authenticated;
GRANT ALL ON public.bot_telegram_backfill_queue TO service_role;
CREATE INDEX IF NOT EXISTS idx_bot_telegram_backfill_queue_pending
  ON public.bot_telegram_backfill_queue(status, queued_at);
