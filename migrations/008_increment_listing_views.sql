-- Migration: increment_listing_views
-- Applied live as `add_increment_listing_views`.
--
-- The listing detail page has always done this on load:
--
--     views: data.view_count || 0
--     await supabase.rpc('increment_listing_views', { listing_id: id })
--     ...
--     <span>{listing.views} görüntülenme</span>
--
-- but the function was never created. Every page view called a function that does not
-- exist, the error went nowhere, and every listing sat at 0 views since launch - which
-- is what "görüntüleme sayacı çalışmıyor" actually was.
--
-- SECURITY DEFINER is required: viewers are usually anonymous and RLS does not let anon
-- update a listing row. The function can do exactly one thing - add 1 to the counter of
-- an active listing - so exposing it to anon is safe.
--
-- Counts page views, not unique visitors: a refresh counts again. Deduplicating needs
-- per-visitor state (a cookie, a views table, or rate limiting) and is a separate
-- product decision, not a bug in this function.
--
-- Rollback: DROP FUNCTION IF EXISTS public.increment_listing_views(uuid);

begin;

create or replace function public.increment_listing_views(listing_id uuid)
returns void
language plpgsql
security definer
set search_path to 'pg_catalog', 'public', 'pg_temp'
as $fn$
begin
    update public.listings l
       set view_count = coalesce(l.view_count, 0) + 1
     where l.id = listing_id
       and l.status = 'active';
end;
$fn$;

revoke all on function public.increment_listing_views(uuid) from public;
grant execute on function public.increment_listing_views(uuid) to anon, authenticated, service_role;

commit;
