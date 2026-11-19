CREATE TABLE IF NOT EXISTS public.bot_telegram_section_controls (
  category text PRIMARY KEY,
  enabled boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.bot_telegram_section_controls ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Public can read Telegram section states" ON public.bot_telegram_section_controls;
CREATE POLICY "Public can read Telegram section states"
  ON public.bot_telegram_section_controls
  FOR SELECT TO anon, authenticated
  USING (true);
GRANT SELECT ON public.bot_telegram_section_controls TO anon, authenticated;
GRANT ALL ON public.bot_telegram_section_controls TO service_role;

CREATE TABLE IF NOT EXISTS public.bot_telegram_runtime_config (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  bot_token text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.bot_telegram_runtime_config ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.bot_telegram_runtime_config FROM anon, authenticated;
GRANT ALL ON public.bot_telegram_runtime_config TO service_role;
