import math
from datetime import date, datetime, timezone
from typing import Any

from garmin_mcp.stride_models import HeartRateZoneProfile, Observation


HEART_RATE_ZONES_URL = '/biometric-service/heartRateZones'
RACE_PREDICTIONS = (
    ('race_prediction_5k_seconds', 'time5K'),
    ('race_prediction_10k_seconds', 'time10K'),
    ('race_prediction_half_marathon_seconds', 'timeHalfMarathon'),
    ('race_prediction_marathon_seconds', 'timeMarathon'),
)


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _heart_rate(value: Any) -> int | None:
    number = _positive(value)
    return round(number) if number is not None and 20 <= number <= 260 else None


def _day(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _threshold(day: date, metric: str, value: float, unit: str,
               availability: str = 'available') -> Observation:
    return {
        'domain': 'thresholds', 'metric': metric, 'observed_on': day.isoformat(),
        'observed_at': None, 'value_numeric': value, 'value_text': None, 'unit': unit,
        'availability': availability, 'source_updated_at': None,
    }


def normalize_heart_rate_zones(response: Any) -> list[HeartRateZoneProfile]:
    profiles: dict[str, HeartRateZoneProfile] = {}
    for item in response if isinstance(response, list) else []:
        if not isinstance(item, dict):
            continue
        sport = item.get('sport')
        if not isinstance(sport, str) or not sport.strip():
            continue
        method = item.get('trainingMethod')
        auto_update = item.get('restingHrAutoUpdateUsed')
        profiles[sport.strip().upper()] = {
            'sport': sport.strip().upper(),
            'training_method': method if isinstance(method, str) and method else None,
            'zone1_floor_bpm': _heart_rate(item.get('zone1Floor')),
            'zone2_floor_bpm': _heart_rate(item.get('zone2Floor')),
            'zone3_floor_bpm': _heart_rate(item.get('zone3Floor')),
            'zone4_floor_bpm': _heart_rate(item.get('zone4Floor')),
            'zone5_floor_bpm': _heart_rate(item.get('zone5Floor')),
            'max_heart_rate_bpm': _heart_rate(item.get('maxHeartRateUsed')),
            'resting_heart_rate_bpm': _heart_rate(item.get('restingHeartRateUsed')),
            'lactate_threshold_heart_rate_bpm': _heart_rate(
                item.get('lactateThresholdHeartRateUsed')),
            'resting_heart_rate_auto_update': (auto_update if isinstance(auto_update, bool)
                                               else None),
        }
    return list(profiles.values())


def normalize_lactate_threshold(response: Any) -> list[Observation]:
    root = response if isinstance(response, dict) else {}
    rows: dict[tuple[str, str], Observation] = {}
    sources = (
        ('speed', 'lactate_threshold_speed_mps', 'm/s', lambda value: round(value * 10, 4)),
        ('heart_rate', 'lactate_threshold_heart_rate_bpm', 'bpm', _heart_rate),
    )
    for key, metric, unit, convert in sources:
        entries = root.get(key)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get('series') not in (None, 'running'):
                continue
            day = _day(entry.get('from'))
            raw = _positive(entry.get('value'))
            value = convert(raw) if raw is not None else None
            if day is None or value is None:
                continue
            rows[(metric, day.isoformat())] = _threshold(day, metric, value, unit)
    return list(rows.values())


def normalize_race_predictions(*responses: Any) -> list[Observation]:
    today = datetime.now(timezone.utc).date()
    rows: dict[tuple[str, str], Observation] = {}
    for response in responses:
        items = response if isinstance(response, list) else [response]
        for item in items:
            if not isinstance(item, dict):
                continue
            day = _day(item.get('calendarDate'))
            if day is None:
                continue
            for metric, key in RACE_PREDICTIONS:
                seconds = _positive(item.get(key))
                if seconds is None:
                    continue
                rows[(metric, day.isoformat())] = _threshold(
                    day, metric, seconds, 'seconds',
                    'partial' if day == today else 'available')
    return list(rows.values())
