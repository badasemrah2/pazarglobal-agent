-- ─────────────────────────────────────────────────────────────────────────────
-- 010  Record listing charges in wallet_transactions
--
-- Migration 005 moved publishing onto reserve_listing_credit(), which takes the
-- balance under a row lock and records the reservation. It never writes to
-- wallet_transactions. So every listing published through the agent since then has
-- moved money with no entry in the ledger: balances are right, history is empty.
--
-- The old path that did write a ledger row (supabase_client.deduct_credits, reached
-- via publish_listing_tool) is no longer wired into any agent - the tools are exported
-- from tools/__init__.py and imported nowhere else. So there is no double-entry risk
-- from adding this, and no live code left that records a charge.
--
-- The entry is written INSIDE the function, in the same transaction as the balance
-- change. That is the point: the previous ledger write was a best-effort insert from
-- Python that caught its own failure, set _wallet_transactions_disabled = True and
-- carried on, so a broken ledger looked exactly like a working one. Here a ledger row
-- that cannot be written aborts the charge instead of being quietly skipped.
--
-- Promo publishes deliberately write no row: nothing was charged, and a ledger is a
-- record of money moving. listing_credit_reservations already records the free
-- publish itself.
-- ─────────────────────────────────────────────────────────────────────────────

begin;

-- ── 0. Refuse to proceed on a schema this migration does not understand ──────
--
-- wallet_transactions was created outside this repo, so its shape is asserted rather
-- than assumed. Guessing a column name or a `kind` value here would write plausible
-- but wrong rows into a financial record - worse than the empty ledger it replaces.
do $$
declare
    v_missing text;
    v_kind_def text;
begin
    if to_regclass('public.wallet_transactions') is null then
        raise exception 'wallet_transactions does not exist; nothing to write a ledger into';
    end if;

    select string_agg(c, ', ')
      into v_missing
    from unnest(array['user_id', 'amount_bigint', 'kind', 'reference', 'metadata']) AS c
    where not exists (
        select 1 from information_schema.columns
         where table_schema = 'public'
           and table_name   = 'wallet_transactions'
           and column_name  = c
    );

    if v_missing is not null then
        raise exception
            'wallet_transactions is missing column(s): %. Expected the shape implied by '
            'credit_wallet(p_user, p_amount_bigint, p_kind, p_reference, p_metadata).',
            v_missing;
    end if;

    -- A required column this migration does not fill would turn every publish into a
    -- failure, because the ledger insert is no longer best-effort. Better to refuse to
    -- apply than to take publishing down.
    select string_agg(column_name, ', ')
      into v_missing
    from information_schema.columns
    where table_schema = 'public'
      and table_name   = 'wallet_transactions'
      and is_nullable  = 'NO'
      and column_default is null
      and is_identity   = 'NO'
      and column_name not in ('user_id', 'amount_bigint', 'kind', 'reference', 'metadata');

    if v_missing is not null then
        raise exception
            'wallet_transactions requires column(s) this migration does not fill: %. '
            'Add them to both inserts below before applying.',
            v_missing;
    end if;

    -- If `kind` is constrained, the two values used below must both be permitted.
    -- supabase_client.py tried 'debit', 'spend', 'usage', 'credit' in turn precisely
    -- because nobody knew which this deployment allows; find out for certain instead.
    --
    -- On the live project the constraint permits debit, credit, spend, usage, deposit,
    -- withdrawal, refund, purchase and admin_adjust - which is why the refund below
    -- uses 'refund' rather than a generic 'credit'.
    select pg_get_constraintdef(c.oid)
      into v_kind_def
    from pg_constraint c
    join pg_class t     on t.oid = c.conrelid
    join pg_namespace n on n.oid = t.relnamespace
    where n.nspname = 'public'
      and t.relname = 'wallet_transactions'
      and c.contype = 'c'
      and pg_get_constraintdef(c.oid) ilike '%kind%'
    limit 1;

    if v_kind_def is not null
       and not (v_kind_def ilike '%''debit''%' and v_kind_def ilike '%''refund''%') then
        raise exception
            'wallet_transactions.kind does not permit both ''debit'' and ''refund''. '
            'Constraint is: %  --  edit the two kind literals in this migration to match.',
            v_kind_def;
    end if;

    raise notice 'wallet_transactions shape OK (kind constraint: %)',
        coalesce(v_kind_def, 'none');
end;
$$;


-- ── 1. Charge, and record the charge ─────────────────────────────────────────
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

        -- Same transaction as the balance change above: if this row cannot be written
        -- the charge is rolled back with it, rather than the wallet moving silently.
        insert into public.wallet_transactions (user_id, amount_bigint, kind, reference, metadata)
        values (
            p_user_id,
            -p_cost,
            'debit',
            p_reference,
            jsonb_build_object('source', 'reserve_listing_credit', 'balance_after', v_balance)
        );
    end if;

    return jsonb_build_object(
        'success', true,
        'balance', coalesce(v_balance, 0),
        'charged', not v_promo,
        'promo',   v_promo
    );
end;
$fn$;


-- ── 2. Refund, and record the refund ─────────────────────────────────────────
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

    -- The refund is its own entry rather than a deletion of the debit: the ledger
    -- records what happened, and both the charge and its reversal happened.
    insert into public.wallet_transactions (user_id, amount_bigint, kind, reference, metadata)
    values (
        p_user_id,
        v_row.amount,
        'refund',
        p_reference,
        jsonb_build_object('source', 'refund_listing_credit', 'balance_after', v_balance)
    );

    return jsonb_build_object('success', true, 'refunded', true, 'balance', coalesce(v_balance, 0));
end;
$fn$;

commit;

-- ─────────────────────────────────────────────────────────────────────────────
-- NOT BACKFILLED, ON PURPOSE
--
-- Publishes made between migration 005 and this one moved credits with no ledger
-- entry. listing_credit_reservations has a row for each, so the history could be
-- reconstructed - but a reconstructed row is a guess about when and at what balance,
-- and a ledger that mixes recorded facts with reconstructions is worse than one with
-- an acknowledged gap.
--
-- The gap is visible and datable:
--   select count(*) from listing_credit_reservations r
--    where r.charged
--      and not exists (select 1 from wallet_transactions w where w.reference = r.reference);
--
-- To verify this migration, publish one listing and then:
--   select kind, amount_bigint, reference, metadata, created_at
--     from wallet_transactions order by created_at desc limit 5;
-- ─────────────────────────────────────────────────────────────────────────────
