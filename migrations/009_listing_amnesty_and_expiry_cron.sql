-- ─────────────────────────────────────────────────────────────────────────────
-- 009  Amnesty for already-expired listings, then schedule the expiry job
--
-- Migration 007 deliberately left expire_stale_listings() unscheduled:
--
--     "Order: ship the countdown + renewal UI -> grant an amnesty (push existing
--      expires_at forward) -> only then schedule the job."
--
-- The countdown and the "Yeniden Yayınla" button are live, and the owner of this
-- project has since renewed 14 of their own listings through that UI. What remains
-- expired belongs to OTHER sellers, who have not signed in and so have never seen a
-- countdown or a renewal button. Their listings were posted when nothing warned them
-- of a deadline, and they disappeared from the public page the day expiry filtering
-- was switched on.
--
-- This migration gives those listings the same 30 days everyone else got, and only
-- then starts the job - in that order, in one transaction, so the job can never fire
-- against a listing that never got its amnesty.
-- ─────────────────────────────────────────────────────────────────────────────

begin;

-- ── 1. Amnesty ───────────────────────────────────────────────────────────────
-- Only listings that are still 'active' and genuinely past their deadline. A listing
-- already marked 'expired' by a previous run is left alone: its owner turned it off,
-- or a previous cron run retired it, and neither is ours to undo.
do $$
declare
    v_count integer;
begin
    update public.listings
       set expires_at = now() + interval '30 days',
           updated_at = now()
     where status = 'active'
       and expires_at is not null
       and expires_at <= now();

    get diagnostics v_count = row_count;
    raise notice 'Amnesty: % listing(s) given another 30 days', v_count;
end;
$$;

-- ── 2. Lock the function down ────────────────────────────────────────────────
-- expire_stale_listings() is SECURITY DEFINER, so whoever may execute it writes to
-- listings with the definer's rights. Only the scheduler needs it.
revoke all on function public.expire_stale_listings() from public;
revoke all on function public.expire_stale_listings() from anon, authenticated;
grant execute on function public.expire_stale_listings() to postgres, service_role;

-- ── 3. Schedule it ───────────────────────────────────────────────────────────
-- Guarded: on a project without pg_cron this migration must still apply cleanly and
-- say so, rather than failing halfway and leaving the amnesty uncommitted.
--
-- 03:00 UTC is 06:00 in Türkiye - the quietest hour for a marketplace.
do $$
declare
    v_has_cron boolean;
begin
    select exists (select 1 from pg_extension where extname = 'pg_cron')
      into v_has_cron;

    if not v_has_cron then
        -- The amnesty above still commits. Without the job, listings simply keep the
        -- behaviour they have today: hidden from public surfaces once past their
        -- deadline, still renewable by their owner. Nothing breaks; expiry just is
        -- not recorded in the status column.
        raise notice
            'pg_cron not installed - expiry job NOT scheduled. Enable it under '
            'Database > Extensions and re-run this migration.';
        return;
    end if;

    -- Re-running this migration must not leave two copies of the job behind.
    perform cron.unschedule('expire-listings-nightly')
      where exists (select 1 from cron.job where jobname = 'expire-listings-nightly');

    perform cron.schedule(
        'expire-listings-nightly',
        '0 3 * * *',
        $job$ select public.expire_stale_listings(); $job$
    );

    raise notice 'Scheduled expire-listings-nightly at 03:00 UTC';
end;
$$;

commit;

-- ─────────────────────────────────────────────────────────────────────────────
-- WHAT THE JOB DOES, AND WHY IT IS SAFE NOW
--
-- expire_stale_listings() sets status = 'expired' on active listings past their
-- deadline. Two things had to be true before that was survivable, and both are:
--
--   * "İlanlarım" (getUserListings) filters on user_id alone, so an expired listing
--     stays visible to its owner along with its renewal button. Public surfaces
--     already hid it via expires_at, so the status change removes nothing extra there.
--
--   * extend_listing() flips status 'expired' -> 'active', so renewing genuinely
--     brings a listing back rather than leaving it half-retired.
--
-- To verify after applying:
--   select count(*) from listings where status = 'active' and expires_at <= now();
--     -> expected 0
--   select jobname, schedule, active from cron.job
--    where jobname = 'expire-listings-nightly';
-- ─────────────────────────────────────────────────────────────────────────────
