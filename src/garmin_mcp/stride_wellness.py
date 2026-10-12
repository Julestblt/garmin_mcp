import logging
import threading
import time
import uuid
from collections import OrderedDict
from datetime import date, datetime, timezone
from typing import Any, Callable

from garmin_mcp.stride_garmin import ConnectionError, GarminSessionProvider, map_error


logger = logging.getLogger('garmin_mcp.stride.wellness')

SLEEP_STAGES = {0: 'deep', 1: 'light', 2: 'rem', 3: 'awake'}
HEART_RATE_BUCKET_MS = 120_000
STRESS_BUCKET_MS = 180_000
STEPS_BUCKET_MS = 3_600_000


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch_ms(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    return None


def _samples(values: Any, pick: Callable[[list[Any]], Any]) -> list[tuple[int, float | None]]:
    if not isinstance(values, list):
        return []
    samples = []
    for item in values:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        timestamp = _epoch_ms(item[0])
        if timestamp is None:
            continue
        value = _number(pick(list(item)))
        samples.append((timestamp, value if value is not None and value >= 0 else None))
    return sorted(samples)


def _bucket(samples: list[tuple[int, float | None]], size: int,
            total: bool = False) -> list[list[float | int | None]]:
    buckets: OrderedDict[int, list[float]] = OrderedDict()
    for timestamp, value in samples:
        key = timestamp - timestamp % size
        bucket = buckets.setdefault(key, [])
        if value is not None:
            bucket.append(value)
    result: list[list[float | int | None]] = []
    for key, values in buckets.items():
        if not values:
            result.append([key, None])
        elif total:
            result.append([key, round(sum(values))])
        else:
            result.append([key, round(sum(values) / len(values), 1)])
    return result


def _battery_level(item: list[Any]) -> Any:
    if len(item) >= 3 and isinstance(item[1], str):
        return item[2]
    return item[1]


def _sleep(sleep: Any) -> dict[str, Any] | None:
    root = _mapping(sleep)
    daily = _mapping(root.get('dailySleepDTO'))
    start = _epoch_ms(daily.get('sleepStartTimestampGMT'))
    end = _epoch_ms(daily.get('sleepEndTimestampGMT'))
    if start is None or end is None or end <= start:
        return None
    stages = []
    for level in root.get('sleepLevels') or []:
        level = _mapping(level)
        number = _number(level.get('activityLevel'))
        stage = SLEEP_STAGES.get(int(number)) if number is not None else None
        level_start = _epoch_ms(level.get('startGMT'))
        level_end = _epoch_ms(level.get('endGMT'))
        if stage and level_start is not None and level_end is not None and level_end > level_start:
            stages.append({'start': level_start, 'end': level_end, 'stage': stage})
    spo2 = _mapping(root.get('wellnessSpO2SleepSummaryDTO'))
    scores = _mapping(daily.get('sleepScores'))
    return {
        'start': start,
        'end': end,
        'stages': sorted(stages, key=lambda item: item['start']),
        'deep_seconds': _number(daily.get('deepSleepSeconds')),
        'light_seconds': _number(daily.get('lightSleepSeconds')),
        'rem_seconds': _number(daily.get('remSleepSeconds')),
        'awake_seconds': _number(daily.get('awakeSleepSeconds')),
        'awake_count': _number(daily.get('awakeCount')),
        'score': _number(_mapping(scores.get('overall')).get('value')),
        'respiration_average': _number(daily.get('averageRespirationValue')),
        'spo2_average': _number(spo2.get('averageSpo2', daily.get('averageSpO2Value'))),
        'spo2_lowest': _number(spo2.get('lowestSpo2', daily.get('lowestSpO2Value'))),
        'hrv_average': _number(root.get('avgOvernightHrv')),
    }


def normalize_day(day: date, sleep: Any, heart: Any, stress: Any, battery: Any,
                  steps: Any, summary: Any) -> dict[str, Any]:
    stress_root = _mapping(stress)
    stats = _mapping(summary)
    battery_values = stress_root.get('bodyBatteryValuesArray')
    if not battery_values and isinstance(battery, list):
        battery_values = next((_mapping(item).get('bodyBatteryValuesArray') for item in battery
                               if _mapping(item).get('bodyBatteryValuesArray')), None)
    step_samples = []
    for interval in steps if isinstance(steps, list) else []:
        interval = _mapping(interval)
        start = _epoch_ms(interval.get('startGMT'))
        count = _number(interval.get('steps'))
        if start is not None:
            step_samples.append((start, count))
    return {
        'date': day.isoformat(),
        'sleep': _sleep(sleep),
        'heart_rate': _bucket(_samples(_mapping(heart).get('heartRateValues'), lambda item: item[1]),
                              HEART_RATE_BUCKET_MS),
        'stress': _bucket(_samples(stress_root.get('stressValuesArray'), lambda item: item[1]),
                          STRESS_BUCKET_MS),
        'body_battery': [[timestamp, value] for timestamp, value in
                         _samples(battery_values, _battery_level) if value is not None],
        'steps': _bucket(sorted(step_samples), STEPS_BUCKET_MS, total=True),
        'summary': {
            'resting_heart_rate': _number(_mapping(heart).get('restingHeartRate',
                                                             stats.get('restingHeartRate'))),
            'steps_total': _number(stats.get('totalSteps')),
            'steps_goal': _number(stats.get('dailyStepGoal')),
            'stress_average': _number(stress_root.get('avgStressLevel',
                                                      stats.get('averageStressLevel'))),
            'stress_max': _number(stress_root.get('maxStressLevel',
                                                  stats.get('maxStressLevel'))),
            'body_battery_wake': _number(stats.get('bodyBatteryAtWakeTime')),
            'body_battery_charged': _number(stats.get('bodyBatteryChargedValue')),
            'body_battery_drained': _number(stats.get('bodyBatteryDrainedValue')),
            'body_battery_highest': _number(stats.get('bodyBatteryHighestValue')),
            'body_battery_lowest': _number(stats.get('bodyBatteryLowestValue')),
            'body_battery_latest': _number(stats.get('bodyBatteryMostRecentValue')),
        },
    }


class WellnessDayService:
    def __init__(self, sessions: GarminSessionProvider,
                 clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], date] = lambda: datetime.now(timezone.utc).date(),
                 current_ttl: float = 300, past_ttl: float = 21_600, capacity: int = 256):
        self.sessions = sessions
        self.clock = clock
        self.today = today
        self.current_ttl = current_ttl
        self.past_ttl = past_ttl
        self.capacity = capacity
        self._cache: OrderedDict[tuple[uuid.UUID, date], tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, user_id: uuid.UUID, day: date) -> dict[str, Any]:
        key = (user_id, day)
        now = self.clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                self._cache.move_to_end(key)
                return cached[1]
        client = self.sessions.for_user(user_id)
        value = day.isoformat()
        try:
            payload = normalize_day(
                day, client.get_sleep_data(value), client.get_heart_rates(value),
                client.get_stress_data(value), client.get_body_battery(value),
                client.get_steps_data(value), client.get_user_summary(value))
        except ConnectionError:
            raise
        except Exception as error:
            raise map_error(error) from None
        finally:
            self.sessions.persist(user_id, client)
        ttl = self.current_ttl if day >= self.today() else self.past_ttl
        with self._lock:
            self._cache[key] = (now + ttl, payload)
            self._cache.move_to_end(key)
            while len(self._cache) > self.capacity:
                self._cache.popitem(last=False)
        logger.info('wellness_day_loaded', extra={
            'user_id': str(user_id), 'heart_rate_points': len(payload['heart_rate']),
            'stress_points': len(payload['stress'])})
        return payload
