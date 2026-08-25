-- Migration 004: Site-internal contact & messaging tables
-- Run in Supabase Dashboard → SQL Editor
-- Tables: contact_tokens, listing_conversations, listing_messages

-- ============================================================
-- 1. contact_tokens
--    One token per listing (reused until expiry/revoke).
--    Buyers open /contact/<token> to reach the seller.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.contact_tokens (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id      UUID        NOT NULL REFERENCES public.listings(id) ON DELETE CASCADE,
    owner_user_id   UUID        NOT NULL REFERENCES auth.users(id)      ON DELETE CASCADE,
    token           TEXT        NOT NULL UNIQUE,
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked         BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contact_tokens_token      ON public.contact_tokens(token);
CREATE INDEX IF NOT EXISTS idx_contact_tokens_listing_id ON public.contact_tokens(listing_id);

-- Only one active (non-revoked, non-expired) token per listing at a time
-- (enforced in app layer; optional unique index for extra safety)
CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_tokens_listing_active
    ON public.contact_tokens(listing_id)
    WHERE revoked = FALSE;

-- RLS
ALTER TABLE public.contact_tokens ENABLE ROW LEVEL SECURITY;

-- Service role (backend) can do everything
CREATE POLICY "service_role_contact_tokens_all"
    ON public.contact_tokens
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Owners can see their own tokens
CREATE POLICY "owner_read_contact_tokens"
    ON public.contact_tokens
    FOR SELECT
    TO authenticated
    USING (owner_user_id = auth.uid());


-- ============================================================
-- 2. listing_conversations
--    One row per buyer↔listing thread.
--    Buyer identified by sender_user_id (logged-in) or
--    sender_session_id (anonymous).
-- ============================================================
CREATE TABLE IF NOT EXISTS public.listing_conversations (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id              UUID        NOT NULL REFERENCES public.listings(id) ON DELETE CASCADE,
    owner_user_id           UUID        NOT NULL REFERENCES auth.users(id)      ON DELETE CASCADE,
    contact_token_id        UUID        REFERENCES public.contact_tokens(id)    ON DELETE SET NULL,
    sender_user_id          UUID        REFERENCES auth.users(id)               ON DELETE SET NULL,
    sender_session_id       TEXT,
    sender_name             TEXT,
    source_channel          TEXT        NOT NULL DEFAULT 'web',
    last_message_preview    TEXT,
    last_message_at         TIMESTAMPTZ,
    owner_unread_count      INTEGER     NOT NULL DEFAULT 0,
    buyer_unread_count      INTEGER     NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_listing_conversations_listing_id    ON public.listing_conversations(listing_id);
CREATE INDEX IF NOT EXISTS idx_listing_conversations_owner_user_id ON public.listing_conversations(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_listing_conversations_sender_user_id ON public.listing_conversations(sender_user_id)
    WHERE sender_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_listing_conversations_updated_at    ON public.listing_conversations(updated_at DESC);

-- RLS
ALTER TABLE public.listing_conversations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_conversations_all"
    ON public.listing_conversations
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Participants (owner or registered buyer) can read their conversations
CREATE POLICY "participant_read_conversations"
    ON public.listing_conversations
    FOR SELECT
    TO authenticated
    USING (
        owner_user_id  = auth.uid()
        OR sender_user_id = auth.uid()
    );

-- Authenticated participants can update (e.g. unread counts via backend)
CREATE POLICY "participant_update_conversations"
    ON public.listing_conversations
    FOR UPDATE
    TO authenticated
    USING (
        owner_user_id  = auth.uid()
        OR sender_user_id = auth.uid()
    );


-- ============================================================
-- 3. listing_messages
--    Individual messages inside a conversation.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.listing_messages (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     UUID        NOT NULL REFERENCES public.listing_conversations(id) ON DELETE CASCADE,
    listing_id          UUID        NOT NULL REFERENCES public.listings(id)              ON DELETE CASCADE,
    sender_role         TEXT        NOT NULL CHECK (sender_role IN ('owner', 'buyer')),
    body                TEXT        NOT NULL,
    read_by_owner       BOOLEAN     NOT NULL DEFAULT FALSE,
    read_by_buyer       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_listing_messages_conversation_id ON public.listing_messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_listing_messages_created_at      ON public.listing_messages(conversation_id, created_at);

-- RLS
ALTER TABLE public.listing_messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_messages_all"
    ON public.listing_messages
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Participants can read messages in their conversations
CREATE POLICY "participant_read_messages"
    ON public.listing_messages
    FOR SELECT
    TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM public.listing_conversations c
            WHERE c.id = listing_messages.conversation_id
              AND (c.owner_user_id = auth.uid() OR c.sender_user_id = auth.uid())
        )
    );
