ALTER TABLE public.bot_telegram_section_controls
  ADD COLUMN IF NOT EXISTS disabled_at timestamptz;

ALTER TABLE public.bot_telegram_backfill_queue
  DROP CONSTRAINT IF EXISTS bot_telegram_backfill_queue_status_check;
ALTER TABLE public.bot_telegram_backfill_queue
  ADD CONSTRAINT bot_telegram_backfill_queue_status_check
  CHECK (status IN ('pending', 'processing', 'sent', 'failed'));
ALTER TABLE public.bot_telegram_backfill_queue
  ADD COLUMN IF NOT EXISTS processing_at timestamptz;

CREATE TABLE IF NOT EXISTS public.bot_telegram_backfill_worker (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  owner text,
  locked_until timestamptz
);
INSERT INTO public.bot_telegram_backfill_worker (singleton)
VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

ALTER TABLE public.bot_telegram_backfill_worker ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.bot_telegram_backfill_worker FROM anon, authenticated;
GRANT ALL ON public.bot_telegram_backfill_worker TO service_role;
