# MCP tools in Stride mode

In Stride mode `/mcp` serves a fixed, read-only tool set to Stride's coaching agents. Each call is scoped to the user in the bearer token's subject and reads live Garmin Connect through that user's session.

## Exposed tools

`get_activities`, `get_activities_by_date`, `get_activity`, `get_activity_splits`, `get_stats`, `get_sleep_data`, `get_hrv_data`, `get_training_readiness`, `get_training_status`, `get_rhr_day`, `get_vo2max_trend`.

This set is `stride_mcp.DEFAULT_TOOLS`. `GARMIN_ENABLED_TOOLS` can narrow it but not widen it: startup fails if the allowlist names a write tool or a tool outside the set. Use `get_activities_by_date` to read the activities of a given week; `get_activities` pages newest-first with no date filter.

## Read bounds

- Range: every exposed tool that takes `start_date` and `end_date` (today `get_activities_by_date` and `get_vo2max_trend`) accepts at most 14 days, both ends included. A wider range is rejected before Garmin is called; nothing is truncated silently.
- Size: a text response above `STRIDE_MCP_MAX_RESPONSE_BYTES` (default 65536, minimum 1024) is cut to that many bytes. The first line of a cut response starts with `[truncated]` and gives the kept and original sizes, so the caller knows the data below it is incomplete and can ask for a single date or a narrower range.

## Session lease

An MCP call takes the same per-connection session lease as the sync worker. It waits at most 3 seconds for it (`stride_mcp.MCP_LEASE_WAIT_SECONDS`), then fails with `provider_busy` rather than holding the athlete's turn. The sync worker and the REST API keep their 90-second wait and still report a timeout as `provider_unavailable`. Tools run on a worker thread, so a call waiting for the lease or for Garmin does not delay other requests.

## Errors

A rejected call returns an MCP result with `isError: true`, a text block starting with the code (`provider_busy: Garmin data is temporarily unavailable…`) and `structuredContent` of `{"error": {"code": "<code>"}}`, the same shape as the REST API. Read the code from `structuredContent`; the message after it is for humans and may change.

| Code | Meaning | Retry |
| --- | --- | --- |
| `range_too_large` | The range covers more than 14 days. | Split it into calls of 14 days or less. |
| `invalid_request` | A date is not `YYYY-MM-DD`, or `end_date` is before `start_date`. | Fix the arguments. |
| `provider_busy` | A sync holds the athlete's Garmin session. | Yes, after about a minute. |
| `rate_limited` | Garmin is rate limiting this account. | Yes, after several minutes. |
| `provider_unavailable` | Garmin Connect failed or did not answer. | Yes, with backoff. |
| `reconnect_required` | The Garmin session expired or was revoked. | No; the athlete reconnects in Stride. |
| `unauthorized` | The request carries no Stride user. | No. |

The code reflects the last Garmin call the tool made. When Garmin answers 404 for a date without data, the tool returns its normal "no data" answer, not an error.
