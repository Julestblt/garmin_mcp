# Planned workouts

Stride pushes planned run and trail sessions to the athlete's Garmin calendar so the watch can guide them. Apart from the running-shoe writes in `docs/gear.md`, this is the only Garmin write this service performs. Only the Stride server triggers it, through the REST API, on behalf of the signed-in athlete. The Stride-mode MCP tool set stays read-only.

## Reconcile a window

`PUT /v1/connections/garmin/workouts` takes the full set of workouts wanted in a date window:

```json
{
  "from": "2026-10-07",
  "to": "2026-10-13",
  "workouts": [{
    "key": "plan-id:s2-wed",
    "date": "2026-10-07",
    "name": "Seuil · 4 × 6’",
    "description": "Développer l’allure seuil.",
    "steps": [],
    "allow_simplified": false,
    "force": false
  }]
}
```

- `from` may be yesterday at the earliest, to allow for time zones; the window spans at most 31 days and 40 workouts. Every workout date must fall inside it.
- `key` is Stride's stable identifier for a session (`[A-Za-z0-9][A-Za-z0-9:_-]{0,127}`). `steps` follows the Stride session structure: `warmup`, `work`, `recovery` and `cooldown` steps, and `repeat` groups of up to 10 steps. Steps end on `time`, `distance` or `lap_button` and target a `pace_range`, a `heart_rate_range`, an `rpe` range or `none`. Unknown fields are rejected.
- Each workout is uploaded as `Stride · <name>` and scheduled on its date. A workout whose content hash (date, name, description, steps) matches the recorded one is left alone, unless `force` is set; `force` replaces it without creating a duplicate. A changed or moved workout is deleted on Garmin and uploaded again.
- Recorded workouts in the window that are missing from the request are deleted on Garmin. Workouts outside the window are untouched.
- Garmin has no RPE target. Such steps are sent without a target, with the effort written in the step note, and are reported in `simplifications` as `rpe_target`. A simplified workout is not sent (status `needs_confirmation`) until the request sets `allow_simplified`.

The response lists each workout with its `status` (`sent`, `failed` or `needs_confirmation`), `changed`, `simplifications` and, on failure, a stable `error_code` (`provider_unavailable`, `rate_limited` or `unsupported_data` when Garmin rejects the workout). One failure does not block the others. A rate limit stops further Garmin calls for this request, and those workouts are marked `failed` with `rate_limited`. `reconnect_required` aborts the request with HTTP 409, as does `disconnected`. The response also lists `removed` keys.

`DELETE /v1/connections/garmin/workouts` deletes every upcoming recorded workout, for example when the athlete turns sending off. Disconnecting does the same, best effort, and then drops every record.

## State

`provider_workouts` keeps one row per user and key: date, status, content hash, Garmin workout id, error code, simplifications and send time. Athletes can read their own rows under RLS; only the service writes them. A workout already deleted in Garmin Connect counts as removed. Stride never touches workouts the athlete created in Garmin Connect.

Garmin pace targets are speeds in metres per second, slowest first. Heart-rate ranges use Garmin's custom heart-rate target.
