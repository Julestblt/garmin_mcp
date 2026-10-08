# Security boundary

Garmin sessions are bearer credentials. `provider_secrets` contains only Fernet ciphertext. The key comes from `GARMIN_TOKEN_ENCRYPTION_KEY`, never from Supabase. Only the trusted service role can read secrets or challenge state; RLS and revoked browser grants add defense in depth. A future secret manager can replace `TokenStore` without changing callers.

Serve the container behind a trusted TLS reverse proxy or private network. The service validates Supabase bearer tokens for application and MCP requests. `STRIDE_ALLOWED_HOSTS` restricts MCP host headers. Garmin authentication attempts have a durable five-attempts-per-ten-minutes limit per Stride user. The proxy should also enforce an IP-based limit against account-creation abuse.

Do not log passwords, OTPs, token JSON, challenge ciphertext, or service keys. Operational logs use user and job IDs plus stable error codes. Rotate the Fernet key through a planned re-encryption process; replacing it without migration makes stored sessions unreadable. Back up Supabase tables according to Stride's data policy.

Read-only mode is enforced when Stride starts: only `get_`, `count_`, `search_`, and `download_` tools may register, and the default allowlist contains ten tools. The HTTP API has no Garmin mutation route. Local stdio mode still retains upstream write tools.
