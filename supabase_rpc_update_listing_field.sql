-- Atomic listing_data patch helper for active_drafts
-- Usage:
--   select public.update_listing_field('<draft_uuid>'::uuid, 'title', to_jsonb('New Title'::text));
--   select public.update_listing_field('<draft_uuid>'::uuid, 'price', to_jsonb(15000::numeric));

create or replace function public.update_listing_field(
  listing_id uuid,
  field_name text,
  field_value jsonb
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  allowed_fields text[] := array['title','description','price','category','contact_phone'];
begin
  if field_name is null or btrim(field_name) = '' then
    raise exception 'field_name is required';
  end if;

  if not (field_name = any(allowed_fields)) then
    raise exception 'invalid field_name: %', field_name;
  end if;

  update public.active_drafts
     set listing_data = jsonb_set(coalesce(listing_data, '{}'::jsonb), array[field_name], field_value, true),
         updated_at = now()
   where id = listing_id;

  return found;
end;
$$;

-- Lock down execution; typically only service_role should write drafts.
revoke all on function public.update_listing_field(uuid, text, jsonb) from public;
grant execute on function public.update_listing_field(uuid, text, jsonb) to service_role;
