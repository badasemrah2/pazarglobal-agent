-- Migration: Atomic listing credit reservation
-- Purpose: Replace the read-modify-write wallet flow in FSMEngine.publish with a
--          row-locked, idempotent reserve/refund pair.
--
-- Fixes three concrete defects:
--   1. Race: two concurrent "onayla" could publish two listings for one deduction,
--      because balance was SELECTed and UPDATEd in separate round trips.
--   2. Promo refund bug: deduct_credit(user_id, -55) skipped the promo guard on the way
--      out but not on the way back, so a failed insert credited +55 to a promo user.
--   3. Twilio webhook retries could charge the same publish twice.
--
-- Deliberately touches NO existing table definitions. Only public.wallets is read/updated
-- (columns user_id, balance_bigint, free_unlimited_until), and one new table is added.
--
-- Rollback:
--   DROP FUNCTION IF EXISTS public.refund_listing_credit(uuid, text);
--   DROP FUNCTION IF EXISTS public.reserve_listing_credit(uuid, bigint, text);
--   DROP TABLE IF EXISTS public.listing_credit_reservations;

begin;

-- 1) Ledger of credit reservations. `reference` is the listing id, which makes a
--    retry of the same publish idempotent.
create table if not exists public.listing_credit_reservations (
    reference   text        primary key,
    user_id     uuid        not null,
    amount      bigint      not null,
    charged     boolean     not null default true,
    refunded    boolean     not null default false,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists idx_listing_credit_reservations_user
    on public.listing_credit_reservations (user_id, created_at desc);

alter table public.listing_credit_reservations enable row level security;
-- No policies on purpose: only service_role (which bypasses RLS) may touch this table.


-- 2) Reserve credit. Locks the wallet row so the balance check and the decrement
--    cannot interleave with another publish.
create or replace function public.reserve_listing_credit(
    p_user_id   uuid,
    p_cost      bigint,
    p_reference text
)
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog', 'public', 'pg_temp'
as $fn$
declare
    v_balance    bigint;
    v_free_until timestamptz;
    v_promo      boolean := false;
    v_existing   public.listing_credit_reservations%rowtype;
begin
    if p_user_id is null or p_reference is null or length(trim(p_reference)) = 0 then
        return jsonb_build_object('success', false, 'error', 'invalid_arguments');
    end if;

    if p_cost is null or p_cost < 0 then
        return jsonb_build_object('success', false, 'error', 'invalid_cost');
    end if;

    -- Idempotency: this publish was already paid for. Never charge twice.
    select * into v_existing
    from public.listing_credit_reservations
    where reference = p_reference;

    if found then
        select balance_bigint into v_balance from public.wallets where user_id = p_user_id;
        return jsonb_build_object(
            'success',    true,
            'balance',    coalesce(v_balance, 0),
            'charged',    v_existing.charged and not v_existing.refunded,
            'idempotent', true
        );
    end if;

    -- Serialise concurrent publishes for this wallet.
    select balance_bigint, free_unlimited_until
      into v_balance, v_free_until
    from public.wallets
    where user_id = p_user_id
    for update;

    if not found then
        return jsonb_build_object('success', false, 'error', 'wallet_not_found');
    end if;

    v_promo := v_free_until is not null and v_free_until > now();

    if not v_promo and coalesce(v_balance, 0) < p_cost then
        return jsonb_build_object(
            'success',  false,
            'error',    'insufficient_balance',
            'balance',  coalesce(v_balance, 0),
            'required', p_cost
        );
    end if;

    if v_promo then
        -- Promo users are not charged, but the reservation is still recorded so a
        -- refund can never hand them credits they never spent.
        insert into public.listing_credit_reservations (reference, user_id, amount, charged)
        values (p_reference, p_user_id, p_cost, false);
    else
        update public.wallets
           set balance_bigint = balance_bigint - p_cost
         where user_id = p_user_id
        returning balance_bigint into v_balance;

        insert into public.listing_credit_reservations (reference, user_id, amount, charged)
        values (p_reference, p_user_id, p_cost, true);
    end if;

    return jsonb_build_object(
        'success', true,
        'balance', coalesce(v_balance, 0),
        'charged', not v_promo,
        'promo',   v_promo
    );
end;
$fn$;


-- 3) Refund a reservation. Only refunds what was actually charged, and only once.
create or replace function public.refund_listing_credit(
    p_user_id   uuid,
    p_reference text
)
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog', 'public', 'pg_temp'
as $fn$
declare
    v_row     public.listing_credit_reservations%rowtype;
    v_balance bigint;
begin
    select * into v_row
    from public.listing_credit_reservations
    where reference = p_reference
    for update;

    if not found then
        return jsonb_build_object('success', false, 'error', 'reservation_not_found');
    end if;

    if v_row.user_id <> p_user_id then
        return jsonb_build_object('success', false, 'error', 'user_mismatch');
    end if;

    -- Promo reservations were never charged; refunding them would mint credits.
    if not v_row.charged or v_row.refunded then
        update public.listing_credit_reservations
           set refunded = true, updated_at = now()
         where reference = p_reference;
        return jsonb_build_object('success', true, 'refunded', false, 'reason', 'nothing_to_refund');
    end if;

    update public.wallets
       set balance_bigint = balance_bigint + v_row.amount
     where user_id = p_user_id
    returning balance_bigint into v_balance;

    update public.listing_credit_reservations
       set refunded = true, updated_at = now()
     where reference = p_reference;

    return jsonb_build_object('success', true, 'refunded', true, 'balance', coalesce(v_balance, 0));
end;
$fn$;


-- 4) Only the backend's service_role may call these.
revoke all on function public.reserve_listing_credit(uuid, bigint, text) from public, anon, authenticated;
revoke all on function public.refund_listing_credit(uuid, text)          from public, anon, authenticated;
grant execute on function public.reserve_listing_credit(uuid, bigint, text) to service_role;
grant execute on function public.refund_listing_credit(uuid, text)          to service_role;

commit;
