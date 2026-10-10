# Stride Garmin architecture

Stride owns users and athlete records. This service accepts a Supabase Auth access token, resolves its `auth.users.id`, and uses that ID for connection metadata, encrypted Garmin sessions, sync jobs, and normalized activities. It has no user account table.

The HTTP API handles deterministic connection and onboarding operations. MCP remains the AI interface and exposes a small read-only tool allowlist in Stride mode. Both use `GarminSessionProvider`; existing local stdio use remains available outside Stride mode.

`TokenStore` separates session access from storage. `FileTokenStore` supports local development; `EncryptedSupabaseTokenStore` stores Fernet-encrypted session JSON in Supabase. The encryption key exists only in the service environment. `provider_connections` holds metadata, `provider_secrets` holds ciphertext, and `sync_jobs` holds durable progress. The worker writes normalized activities to `provider_activities`.

The pinned `garminconnect` client serializes established DI tokens with `client.dumps()`. Each operation constructs its own client, loads only that user's token, and persists refreshed state. It does not share a mutable Garmin client across tenants.

Normalized contracts live in `stride_models`, and provider failures are mapped to stable codes in `stride_errors`. Stride consumes normalized rows and error codes, not Garmin response shapes.

## Known limitations

- Personal records and race predictions are not ingested yet. The fitness domain covers VO2 max, training status, acute and chronic load, and load focus.
- Historical activity import does not stop after a long run of empty windows, because a gap is not proof that older history is absent. Bound work with a per-connection `history_start_date`.
- Garmin MFA continuation relies on private `garminconnect` fields. Upgrade that library only alongside the compatibility tests.
- Fernet key rotation needs a planned re-encryption pass; replacing `GARMIN_TOKEN_ENCRYPTION_KEY` without migrating stored sessions makes them unreadable.
- Activity tracks are parsed defensively from Garmin Connect's activity-details format and covered by synthetic payloads. The metric keys for cadence and power in particular should be confirmed against a real account before Stride depends on those series.
- There is no aggregate metrics endpoint. Observability is structured stderr logs (see `docs/deployment.md`).
- The API exposes read-only Garmin data and lifecycle operations. It is deliberately not a generic REST wrapper over every MCP tool.
