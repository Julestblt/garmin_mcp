"""Stable normalized contracts consumed by Stride.

These models describe the provider-neutral shapes produced by the
integration layer. Provider-specific Garmin response keys must be
normalized into these shapes before they leave ``garmin_mcp``; Stride
application and AI code should not depend on Garmin response fields.

Every record carries provenance:

- ``provider``: upstream source, currently always ``garmin``.
- ``provider_activity_id`` / ``metric``: stable upstream identifier.
- ``started_at`` / ``observed_on``: upstream time the value describes.
- ``imported_at``: when this service wrote the row.
- ``source_updated_at``: upstream update time, when available, so
  consumers can reason about source freshness.
"""

from typing import Any, Literal, TypedDict

Availability = Literal[
    'available', 'missing', 'unsupported', 'unavailable_historically',
    'partial', 'stale',
]

SyncCategoryState = Literal['pending', 'running', 'partial', 'complete', 'failed']

SYNC_CATEGORY_STATES = frozenset(
    {'pending', 'running', 'partial', 'complete', 'failed'})
AVAILABILITY_STATES = frozenset(
    {'available', 'missing', 'unsupported', 'unavailable_historically',
     'partial', 'stale'})


class Activity(TypedDict):
    """Normalized activity summary plus the detail fields Stride needs."""

    user_id: str
    connection_id: str
    provider: str
    provider_activity_id: str
    activity_type: str | None
    started_at: str | None
    local_started_at: str | None
    time_zone_id: str | None
    distance_meters: Any
    duration_seconds: Any
    elevation_gain_meters: Any
    elevation_loss_meters: Any
    average_heart_rate: Any
    max_heart_rate: Any
    average_cadence: Any
    average_power_watts: Any
    max_power_watts: Any
    aerobic_training_effect: Any
    anaerobic_training_effect: Any
    route_name: str | None
    has_route: bool | None
    activity_name: str | None
    calories: Any
    workout_rpe: Any
    workout_feel: Any
    source_updated_at: str | None


class ActivitySegment(TypedDict):
    """Normalized lap or split belonging to an activity."""

    segment_type: Literal['lap', 'split']
    segment_index: int
    started_at: str | None
    distance_meters: Any
    duration_seconds: Any
    average_heart_rate: Any
    average_cadence: Any
    average_power_watts: Any
    elevation_gain_meters: Any
    elevation_loss_meters: Any


class Observation(TypedDict):
    """A single normalized daily recovery or fitness metric point."""

    domain: str
    metric: str
    observed_on: str
    observed_at: str | None
    value_numeric: int | float | None
    value_text: str | None
    unit: str | None
    availability: Availability
    source_updated_at: str | None


class SyncCategory(TypedDict, total=False):
    """Category-level onboarding progress for one sync domain."""

    state: SyncCategoryState
    oldest_synchronized_date: str
    last_success_at: str | None
    last_error_code: str | None
    activity_count: int


class ConnectionState(TypedDict, total=False):
    """Provider connection status exposed to Stride."""

    id: str
    status: str
    connected_at: str | None
    last_authenticated_at: str | None
    last_sync_at: str | None
    last_error_code: str | None


class SyncStatus(TypedDict, total=False):
    """Aggregated sync status returned by the connection API."""

    connection_id: str
    categories: dict[str, SyncCategory]
    last_sync_at: str | None
    incremental: dict[str, Any] | None
    updated_at: str | None
