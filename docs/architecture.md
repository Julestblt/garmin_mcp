# Stride Garmin architecture

Stride owns users and athlete records. This service accepts a Supabase Auth access token, resolves its `auth.users.id`, and uses that ID for connection metadata, encrypted Garmin sessions, sync jobs, and normalized activities. It has no user account table.

The HTTP API handles deterministic connection and onboarding operations. MCP remains the AI interface and exposes a small read-only tool allowlist in Stride mode. Both use `GarminSessionProvider`; existing local stdio use remains available outside Stride mode.

`TokenStore` separates session access from storage. `FileTokenStore` supports local development; `EncryptedSupabaseTokenStore` stores Fernet-encrypted session JSON in Supabase. The encryption key exists only in the service environment. `provider_connections` holds metadata, `provider_secrets` holds ciphertext, and `sync_jobs` holds durable progress. The worker writes normalized activities to `provider_activities`.

The pinned `garminconnect` client serializes established DI tokens with `client.dumps()`. Each operation constructs its own client, loads only that user's token, and persists refreshed state. It does not share a mutable Garmin client across tenants.
