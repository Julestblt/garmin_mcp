from datetime import date, datetime, timezone
from typing import Any, TypedDict


class Observation(TypedDict):
    domain: str
    metric: str
    observed_on: str
    observed_at: str | None
    value_numeric: int | float | None
    value_text: str | None
    unit: str | None
    availability: str
    source_updated_at: str | None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), {})
    return _mapping(value)


def _timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None
                else parsed).isoformat()
    return None


def _observation(day: date, domain: str, metric: str, value: Any,
                 unit: str | None = None, observed_at: str | None = None,
                 source_updated_at: str | None = None) -> Observation:
    numeric = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    text = value if isinstance(value, str) else None
    present = numeric is not None or text is not None
    availability = ('partial' if day == datetime.now(timezone.utc).date() else 'available')
    if not present:
        availability = 'missing'
    return {
        'domain': domain, 'metric': metric, 'observed_on': day.isoformat(),
        'observed_at': observed_at, 'value_numeric': numeric,
        'value_text': text, 'unit': unit, 'availability': availability,
        'source_updated_at': source_updated_at,
    }


def normalize_recovery(day: date, sleep: Any, hrv: Any, rhr: Any,
                       stress: Any, battery: Any, readiness: Any) -> list[Observation]:
    sleep_day = _mapping(_mapping(sleep).get('dailySleepDTO'))
    scores = _mapping(sleep_day.get('sleepScores'))
    hrv_summary = _mapping(_mapping(hrv).get('hrvSummary'))
    battery_day = _first_mapping(battery)
    snapshots = [item for item in readiness if isinstance(item, dict)] if isinstance(readiness, list) else []
    readiness_day = (max(snapshots, key=lambda item: str(item.get('timestampLocal') or
                                                 item.get('timestamp') or ''))
                     if snapshots else _mapping(readiness))
    started = _timestamp(sleep_day.get('sleepStartTimestampGMT'))
    updated = _timestamp(sleep_day.get('sleepEndTimestampGMT'))
    metrics = (
        ('sleep_duration_seconds', sleep_day.get('sleepTimeSeconds'), 'seconds', started, updated),
        ('sleep_deep_seconds', sleep_day.get('deepSleepSeconds'), 'seconds', started, updated),
        ('sleep_light_seconds', sleep_day.get('lightSleepSeconds'), 'seconds', started, updated),
        ('sleep_rem_seconds', sleep_day.get('remSleepSeconds'), 'seconds', started, updated),
        ('sleep_awake_seconds', sleep_day.get('awakeSleepSeconds'), 'seconds', started, updated),
        ('sleep_score', _mapping(scores.get('overall')).get('value'), 'score', started, updated),
        ('hrv_last_night_ms', hrv_summary.get('lastNightAvg'), 'milliseconds',
         _timestamp(hrv_summary.get('createTimeStamp')),
         _timestamp(hrv_summary.get('createTimeStamp'))),
        ('hrv_weekly_average_ms', hrv_summary.get('weeklyAvg'), 'milliseconds', None,
         _timestamp(hrv_summary.get('createTimeStamp'))),
        ('hrv_status', hrv_summary.get('status'), None, None,
         _timestamp(hrv_summary.get('createTimeStamp'))),
        ('resting_heart_rate_bpm', _mapping(rhr).get('restingHeartRate'), 'bpm', None, None),
        ('average_stress', _mapping(stress).get('avgStressLevel'), 'score', None, None),
        ('body_battery_latest', battery_day.get('bodyBatteryMostRecentValue'), 'score',
         _timestamp(battery_day.get('endTimestampGMT')), None),
        ('training_readiness', readiness_day.get('score',
                                                readiness_day.get('trainingReadinessLevel')),
         'score', None, None),
        ('recovery_time_minutes', readiness_day.get('recoveryTime'), 'minutes', None, None),
    )
    return [_observation(day, 'recovery', *metric) for metric in metrics]


def normalize_fitness(day: date, status: Any, max_metrics: Any) -> list[Observation]:
    root = _mapping(status)
    latest = _mapping(_mapping(root.get('mostRecentTrainingStatus')).get(
        'latestTrainingStatusData'))
    devices = [item for item in latest.values() if isinstance(item, dict)]
    device = next((item for item in devices if item.get('primaryTrainingDevice')),
                  devices[0] if devices else {})
    load = _mapping(device.get('acuteTrainingLoadDTO'))
    balance = _mapping(_mapping(root.get('mostRecentTrainingLoadBalance')).get(
        'metricsTrainingLoadBalanceDTOMap'))
    balance_devices = [item for item in balance.values() if isinstance(item, dict)]
    focus = next((item for item in balance_devices if item.get('primaryTrainingDevice')),
                 balance_devices[0] if balance_devices else {})
    vo2 = _mapping(_mapping(root.get('mostRecentVO2Max')).get('generic'))
    if not vo2:
        vo2 = _mapping(_first_mapping(max_metrics).get('generic'))
    metrics = (
        ('vo2_max', vo2.get('vo2MaxValue'), 'ml/kg/min'),
        ('training_status', device.get('trainingStatus'), None),
        ('training_load_acute', load.get('dailyTrainingLoadAcute'), 'load'),
        ('training_load_chronic', load.get('dailyTrainingLoadChronic'), 'load'),
        ('load_focus_low_aerobic', focus.get('monthlyLoadAerobicLow'), 'load'),
        ('load_focus_high_aerobic', focus.get('monthlyLoadAerobicHigh'), 'load'),
        ('load_focus_anaerobic', focus.get('monthlyLoadAnaerobic'), 'load'),
    )
    return [_observation(day, 'fitness', name, value, unit) for name, value, unit in metrics]
