# Docker deployment

Apply all SQL files in `supabase/migrations/` in timestamp order to the Stride Supabase project. Create a Fernet key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Supply `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `GARMIN_TOKEN_ENCRYPTION_KEY` as server secrets. Keep the service role key and encryption key out of Next.js public variables.

Build with `docker build -t stride-garmin .` and start with `docker compose up -d --build`, or deploy separate API and worker containers from the same image. The API binds port 8000 inside the container. Compose publishes only on host loopback and permits the `garmin-mcp:8000` internal host for MCP. Place a TLS reverse proxy or private service network in front. If the proxy forwards a different Host value, add it to comma-separated `STRIDE_ALLOWED_HOSTS`.

The image runs as UID 10001 and needs no token volume. It exposes `/healthz` for liveness, `/readyz` for database readiness, `/mcp` for streamable HTTP, and the `/v1/connections/garmin` API. Configure `GARMIN_ENABLED_TOOLS` to restrict the AI context further. Do not expose this service directly to the public internet.

The API and worker both need Supabase access. Worker jobs survive container replacement. The service still depends on Garmin availability and Supabase Auth/PostgREST. Local legacy usage remains `GARMIN_MCP_TRANSPORT=stdio STRIDE_MODE=0 garmin-mcp`; that mode retains filesystem tokens.
