-- Migration: Free-platform promo window
--
-- Problem: the launch promo expired on 2026-05-15 and nobody noticed for three months.
-- With no payment integration there is no way to top a wallet up, so publishing simply
-- stopped working:
--   * existing sellers ran out of credit and were blocked,
--   * every account created after 2026-05-15 was issued an ALREADY EXPIRED promo by
--     handle_new_user(), so new users could not publish from the day they signed up.
--
-- Fix: public.promo_config is the single source of truth. handle_new_user() reads
-- free_unlimited_until from it (id = 1) and falls back to now() + 90 days only when the
-- table is missing, so moving this one row is all that is needed for new signups.
--
-- IMPORTANT - do NOT "fix" this by replacing handle_new_user(). The live function is far
-- more defensive than the version in 20260214_wallets_free_unlimited_90d.sql: it detects
-- which columns public.profiles actually has, handles role vs user_role, and resolves the
-- phone from either raw_user_meta_data or auth.users. An earlier draft of this migration
-- did replace it with a simplified body and would have thrown all of that away.
--
-- Existing wallets were moved to the same deadline separately (a plain UPDATE over
-- public.wallets), so config and wallets agree.
--
-- When the platform starts charging: set this to the real end of the free period and ship
-- the payment flow at the same time. Nothing else needs to change.
--
-- Rollback:
--   update public.promo_config set free_unlimited_until = '2026-05-15 01:00:30.983986+00'
--    where id = 1;

begin;

update public.promo_config
   set free_unlimited_until = timestamptz '2027-08-21 03:35:26+00'
 where id = 1;

-- Safety net: if the config row is ever missing, handle_new_user() silently falls back to
-- a rolling 90-day window, which is what let this expire unnoticed in the first place.
insert into public.promo_config (id, free_unlimited_until)
select 1, timestamptz '2027-08-21 03:35:26+00'
where not exists (select 1 from public.promo_config where id = 1);

commit;
