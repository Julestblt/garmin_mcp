# MCP tools in Stride mode

In Stride mode `/mcp` serves a fixed, read-only tool set to Stride's coaching agents. Each call is scoped to the user in the bearer token's subject and reads live Garmin Connect through that user's session.

## Exposed tools

`get_activities`, `get_activities_by_date`, `get_activity`, `get_activity_splits`, `get_stats`, `get_sleep_data`, `get_hrv_data`, `get_training_readiness`, `get_training_status`, `get_rhr_day`, `get_vo2max_trend`.

This set is `stride_mcp.DEFAULT_TOOLS`. `GARMIN_ENABLED_TOOLS` can narrow it but not widen it: startup fails if the allowlist names a write tool or a tool outside the set. Use `get_activities_by_date` to read the activities of a given week; `get_activities` pages newest-first with no date filter.

## Read bounds

- Range: every exposed tool that takes `start_date` and `end_date` (today `get_activities_by_date` and `get_vo2max_trend`) accepts at most 14 days, both ends included. A wider range is rejected before Garmin is called; nothing is truncated silently.
- Size: a text response above `STRIDE_MCP_MAX_RESPONSE_BYTES` (default 65536, minimum 1024) is cut to that many bytes. The first line of a cut response starts with `[truncated]` and gives the kept and original sizes, so the caller knows the data below it is incomplete and can ask for a single date or a narrower range.

## Errors

A rejected call returns an MCP tool error whose text contains one of these codes:

| Code | Meaning |
| --- | --- |
| `range_too_large` | The range covers more than 14 days. Split it into calls of 14 days or less. |
| `invalid_request` | A date is not `YYYY-MM-DD`, or `end_date` is before `start_date`. |
