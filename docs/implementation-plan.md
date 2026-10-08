# Stride implementation plan

1. Keep the existing local stdio server and Garmin tool implementations.
2. Add a Stride HTTP mode with Supabase Auth validation, server-only PostgREST persistence, and encrypted Garmin sessions.
3. Isolate every MCP tool invocation through a user-scoped client proxy and keep mutation tools off by a read-only allowlist.
4. Store MFA continuation as encrypted, expiring data outside the process. Test continuation after reconstructing a client.
5. Expose connection lifecycle endpoints and a durable sync-job boundary. Start activity ingestion in a separate worker increment.
6. Add migrations, container configuration, and tests for auth, ownership, MFA, and token persistence.

The upstream `Garmin.client.dumps()` serializes established DI tokens. MFA uses `_mfa_session`, cookies, login parameters, and (for widget login) an HTML response containing CSRF. The adapter will capture these data fields in encrypted storage; changes to the upstream library's private MFA fields require compatibility tests when upgrading `garminconnect`.

## Production completion

1. Extend existing CI with locked package and Docker builds while keeping live Garmin tests optional.
2. Add versioned token compare-and-swap shared by API, worker, and MCP; preserve the old valid connection while replacement authentication is pending.
3. Separate disconnect from delete-data, fix replay-safe sync counts, and make the historical floor connection-specific.
4. Normalize activity details, then daily health and fitness values with explicit missing/partial states and provenance.
5. Expose category sync state, add recent-data incremental jobs, and keep worker claims and retries durable.
6. Add secret-gated real E2E coverage, operational logging, migration checks, and deployment guidance.
