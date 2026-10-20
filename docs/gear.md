# Running shoes

Stride tracks the athlete's running shoes with Garmin as the kilometre counter (Stride ADR 0013). This service normalizes the athlete's shoes and the shoe linked to each activity, and applies the gear writes Stride needs. Only the Stride server calls these routes, on behalf of the signed-in athlete. The Stride-mode MCP tool set stays read-only.

## Normalized rows

Both tables have owner-only read access and are deleted with the connection.

- `provider_gear`: one row per shoe (`gear_type = 'shoes'`; bikes and other gear are skipped). `gear_uuid` is the 32-character lowercase hex form returned by Garmin's legacy gear endpoints. Columns: `brand`, `model`, `display_name`, `status` (`active` or `retired`), `first_use_date`, `retired_on`, `maximum_meters` (Garmin's wear limit, null when unset), `total_distance_meters` and `total_activities` (Garmin's gear stats).
- `provider_activity_gear`: the shoe of each Garmin activity, keyed by `provider_activity_id` (the same id as `provider_activities.provider_activity_id`), so a link can exist before the activity is imported.

Garmin has no field for distance run before a shoe entered Garmin. Stride keeps that starting distance and adds `total_distance_meters`.

## Refresh

The last window of every activity job (historical or incremental) refreshes shoes: one profile lookup, the gear list, one stats call per shoe, and, for each shoe whose totals changed since the last refresh, its v2 details and its activity list. The legacy gear list names pairs typed freely as "Other" / "Unknown Shoes", so brand and model come from the v2 details. A shoe that disappeared from Garmin is deleted with its links. A failed refresh is logged as `gear_refresh_failed` and never fails the activity job. A manual sync therefore refreshes shoes too.

## Create a shoe

`POST /v1/connections/garmin/gear`

```json
{
  "brand": "HOKA",
  "model": "Speedgoat 7",
  "name": "Speedgoat 7",
  "first_use_date": "2026-10-20",
  "max_distance_meters": 700000,
  "default_activity_types": ["trail_running"]
}
```

`max_distance_meters` (1 to 5,000,000) and `default_activity_types` (`running`, `trail_running`, `treadmill_running`) are optional. The shoe is created through Garmin's gear v2 endpoint with the note « Ajoutée depuis Stride », because Garmin rejects an empty note. The response is `{"gear": <provider_gear row>}`.

Garmin only links a shoe to activities on or after its `first_use_date`, so send the date the pair was first worn, not the day it is added.

## Update a shoe

`PATCH /v1/connections/garmin/gear/{gear_uuid}` accepts any of `name`, `max_distance_meters` (0 removes the limit), `default_activity_types` and `status` (`active` or `retired`). The uuid may be dashed or not. Unknown shoes return `404 gear_not_found`. The response is `{"gear": <row>}`.

## Set the shoe of an activity

`PUT /v1/connections/garmin/activities/{provider_activity_id}/gear` with `{"gear_uuid": "<uuid>"}` links the shoe and unlinks any other shoe from that activity in Garmin; `{"gear_uuid": null}` removes the shoe. The activity must be one of the athlete's imported activities (`404 activity_not_found`) and the shoe one of their shoes (`404 gear_not_found`). The response holds the refreshed rows of the shoes involved. An activity before the shoe's first use date returns `422 gear_starts_after_activity`.

## Errors

Writes return the standard error body. `disconnected` and `reconnect_required` (409) come from the connection state, `rate_limited` (429) and `provider_unavailable` (503) from Garmin, including a refused gear creation.
