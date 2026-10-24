-- Accounting (0500-0599): persisted QBO OAuth tokens (refresh tokens rotate; see
-- invoicedesk.accounting.oauth.PostgresTokenStore). Token JSON is encrypted with pgcrypto's
-- pgp_sym_encrypt using a key derived from QBO_CLIENT_SECRET, so a dump alone reveals nothing.
-- Sandbox only: a production realm can never be stored.
create table qbo_oauth_tokens (
  realm_id          text primary key check (realm_id ~ '^[0-9]{1,32}$'),
  environment       text not null default 'sandbox' check (environment = 'sandbox'),
  enc_tokens        bytea not null,
  seed_fingerprint  text not null,
  access_expires_at timestamptz not null,
  updated_at        timestamptz not null default now()
);
