-- Migration: Listing extension + expiry function fixes
-- Applied live as `listing_extension_and_expiry_fixes`.
--
-- Background: listings get 30 days (set_listing_expires_at trigger) and then stop being
-- shown, so finished items drop out without the seller having to remember. Only half of
-- that was ever built: nothing showed the remaining time and there was no way to renew,
-- so the deadline was invisible right up until the listing vanished. This adds the
-- renewal half. The job that actually flips lapsed listings to 'expired' is deliberately
-- NOT scheduled here - see the note at the bottom.

begin;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1) calculate_expiry_date: unbreak it.
--
-- `DECLARE ttl_days INTEGER` shadowed market_data_ttl_config.ttl_days, so
-- `SELECT ttl_days INTO ttl_days` was ambiguous and every single call failed with
-- 42702. Nothing calls it today - listing expiry comes from the trigger, and this reads
-- market_data_ttl_config, which is about how fast prices go stale, not how long a listing
-- runs - but leaving a permanently failing function on the public API surface is a trap
-- for whoever wires it up next.
-- ─────────────────────────────────────────────────────────────────────────────
create or replace function public.calculate_expiry_date(p_category text)
returns timestamptz
language plpgsql
set search_path to 'pg_catalog', 'public', 'extensions', 'pg_temp'
as $fn$
declare
    v_ttl_days integer;
begin
    select c.ttl_days into v_ttl_days
    from public.market_data_ttl_config c
    where c.category = p_category;

    if v_ttl_days is null then
        v_ttl_days := 14;
    end if;

    return now() + make_interval(days => v_ttl_days);
end;
$fn$;


-- ─────────────────────────────────────────────────────────────────────────────
-- 2) extend_listing: a seller renews their own listing.
--
-- Capped at 90 days out, so renewal cannot turn into a permanent pin - that would defeat
-- the reason the expiry exists. Renewing early is never a penalty: the time already left
-- is kept and the new days are added on top. A lapsed listing restarts from today and is
-- flipped back to 'active'.
-- ─────────────────────────────────────────────────────────────────────────────
create or replace function public.extend_listing(
    p_listing_id uuid,
    p_user_id    uuid,
    p_days       integer default 30
)
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog', 'public', 'pg_temp'
as $fn$
declare
    v_owner      uuid;
    v_status     text;
    v_expires_at timestamptz;
    v_base       timestamptz;
    v_new        timestamptz;
    v_max        timestamptz := now() + interval '90 days';
begin
    if p_days is null or p_days < 1 or p_days > 90 then
        return jsonb_build_object('success', false, 'error', 'invalid_days');
    end if;

    select l.user_id, l.status, l.expires_at
      into v_owner, v_status, v_expires_at
    from public.listings l
    where l.id = p_listing_id
    for update;

    if not found then
        return jsonb_build_object('success', false, 'error', 'listing_not_found');
    end if;

    if v_owner is null or v_owner <> p_user_id then
        return jsonb_build_object('success', false, 'error', 'not_owner');
    end if;

    v_base := greatest(coalesce(v_expires_at, now()), now());
    v_new  := v_base + make_interval(days => p_days);

    if v_new > v_max then
        v_new := v_max;
    end if;

    update public.listings
       set expires_at = v_new,
           status     = case when status = 'expired' then 'active' else status end,
           updated_at = now()
     where id = p_listing_id;

    return jsonb_build_object(
        'success',      true,
        'listing_id',   p_listing_id,
        'expires_at',   v_new,
        'days_left',    greatest(0, extract(day from (v_new - now()))::int),
        'capped',       v_new = v_max,
        'reactivated',  v_status = 'expired'
    );
end;
$fn$;

-- The ownership check lives inside the function, so signed-in sellers may call it
-- directly from the browser. anon may not.
revoke all on function public.extend_listing(uuid, uuid, integer) from public, anon;
grant execute on function public.extend_listing(uuid, uuid, integer) to authenticated, service_role;

commit;

-- ─────────────────────────────────────────────────────────────────────────────
-- NOT SCHEDULED HERE, ON PURPOSE
--
-- expire_stale_listings() has never run, so most live listings are already past their
-- expires_at. Scheduling it before sellers have the countdown, the renewal button and a
-- one-off grace period would delete most of the marketplace overnight, punishing people
-- who were never warned.
--
-- Order: ship the countdown + renewal UI -> grant an amnesty (push existing expires_at
-- forward) -> only then schedule the job.
-- ─────────────────────────────────────────────────────────────────────────────
