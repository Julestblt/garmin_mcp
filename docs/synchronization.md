# Activity synchronization

Connection creates a durable `sync_jobs` row and returns immediately. Run `garmin-mcp-worker` separately. It claims one job atomically, fetches one seven-day window, starting with the newest dates, writes normalized activity rows in batches of 100, and saves the next cursor. A lost worker lease can be reclaimed. Replaying a window is safe because `(user_id, provider, provider_activity_id)` is unique and upserts apply provider corrections. Rows are tied to the connection, so replacing or deleting it removes that integration import; Stride's athlete model is separate.

`GET /v1/connections/garmin/sync` reports state, phase, cursor, oldest synchronized date, count of processed activity rows, and errors. There is no fabricated percentage. The default historical floor is 2000-01-01; future work should make that configurable per connection and optimize long empty history.

The current worker ingests activity summaries only. Details, splits, health, recovery, readiness, and races belong to later phases. The worker uses the Garmin date-range API in week-sized windows; this bounds each fetch, though a very dense week is still accumulated by the upstream library. Other consumers should use normalized Stride rows rather than live Garmin calls.
