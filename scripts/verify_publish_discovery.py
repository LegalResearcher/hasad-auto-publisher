from pathlib import Path

root = Path(__file__).resolve().parents[1]
main_bot = (root / 'hasad_news_bot_fixed.py').read_text(encoding='utf-8')
secondary_bot = (root / 'auto_publish_alittihad_alkhabar.py').read_text(encoding='utf-8')
migration = (root / 'supabase/migrations/20260817164600_replace_bot_post_status_with_published_at.sql').read_text(encoding='utf-8')

assert 'def log_discovery_ready' in main_bot
assert 'request_google_indexing' not in main_bot
assert 'GOOGLE_INDEXING_ENABLED' not in main_bot
assert 'def build_canonical_url(slug: str, published_at_iso: str)' in main_bot
assert 'row.get("published_at") or entry.get("published_at") or entry.get("scheduled_at")' in main_bot
assert 'build_canonical_url(record["slug"], record.get("published_at") or record["created_at"])' in main_bot
assert 'log_discovery_ready([canonical_url])' in main_bot

assert 'log_discovery_ready,' in secondary_bot
assert 'request_google_indexing' not in secondary_bot
assert 'build_canonical_url(record["slug"], record.get("published_at") or record["created_at"])' in secondary_bot

assert 'DROP FUNCTION IF EXISTS public.get_bot_post_status(uuid);' in migration
assert 'published_at timestamp with time zone' in migration
assert 'p.published_at' in migration
assert 'GRANT EXECUTE ON FUNCTION public.get_bot_post_status(uuid)' in migration

print('Publish discovery verification checks passed.')
