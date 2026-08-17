-- PostgreSQL لا يسمح بتغيير صف الإرجاع عبر CREATE OR REPLACE؛ نعيد الإنشاء
-- داخل ترحيل واحد ثم نعيد منح صلاحيات الاستدعاء عبر واجهة REST.
DROP FUNCTION IF EXISTS public.get_bot_post_status(uuid);

CREATE FUNCTION public.get_bot_post_status(_post_id uuid)
RETURNS TABLE(
  found boolean,
  status text,
  slug text,
  created_at timestamp with time zone,
  published_at timestamp with time zone
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
  SELECT true, p.status::text, p.slug, p.created_at, p.published_at
  FROM public.posts p
  WHERE p.id = _post_id
  UNION ALL
  SELECT false, NULL::text, NULL::text, NULL::timestamptz, NULL::timestamptz
  WHERE NOT EXISTS (SELECT 1 FROM public.posts p WHERE p.id = _post_id)
  LIMIT 1;
$function$;

GRANT EXECUTE ON FUNCTION public.get_bot_post_status(uuid) TO anon, authenticated, service_role;
