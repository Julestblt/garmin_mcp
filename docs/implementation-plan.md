# First milestone plan

1. Keep the existing local stdio server and Garmin tool implementations.
2. Add a Stride HTTP mode with Supabase Auth validation, server-only PostgREST persistence, and encrypted Garmin sessions.
3. Isolate every MCP tool invocation through a user-scoped client proxy and keep mutation tools off by a read-only allowlist.
4. Store MFA continuation as encrypted, expiring data outside the process. Test continuation after reconstructing a client.
5. Expose connection lifecycle endpoints and a durable sync-job boundary. Start activity ingestion in a separate worker increment.
6. Add migrations, container configuration, and tests for auth, ownership, MFA, and token persistence.

The upstream `Garmin.client.dumps()` serializes established DI tokens. MFA uses `_mfa_session`, cookies, login parameters, and (for widget login) an HTML response containing CSRF. The adapter will capture these data fields in encrypted storage; changes to the upstream library's private MFA fields require compatibility tests when upgrading `garminconnect`.
