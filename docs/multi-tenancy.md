# Tenant ownership

The identity boundary is Supabase Auth. The service uses its server-only service role key for database work after verifying each caller's bearer token. Every database lookup for connection state and Garmin tokens includes `auth.users.id` and `provider=garmin`.

MCP's authentication middleware validates the same bearer token. Tool calls obtain the subject from request context and construct a Garmin client through `GarminSessionProvider.for_user`. Stride mode uses stateless streamable HTTP and a read-only allowlist. Tools cannot choose a different user ID.

Token writes carry the connection ID. Disconnect or replacement deletes that connection, and an in-flight request holding the old ID cannot save a token into the replacement connection. The database function enforces this atomically. Separate users have separate encrypted token records and per-operation Garmin clients.

For an established connection, MCP and worker operations acquire a PostgreSQL-backed lease before loading its session. A second operation for that connection waits, then loads the newly saved token. Leases renew during long calls and expire if a process dies. Token writes also compare the stored version and current lease owner, so an expired or stale operation cannot replace a newer session. Other connections have independent leases. The local filesystem store keeps its original single-process behavior.
