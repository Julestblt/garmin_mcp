"""Push Stride planned sessions to the Garmin calendar.

Stride sends provider-neutral workouts whose steps follow the Stride session
structure. This module validates them, maps them to Garmin structured
running workouts, and reconciles a date window: new or changed workouts are
uploaded and scheduled, unwanted ones are deleted, unchanged ones are left
alone. Only workouts recorded in ``provider_workouts`` are ever deleted, so
workouts the athlete created in Garmin Connect are never touched.
"""

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from garminconnect import GarminConnectConnectionError

from garmin_mcp.stride_errors import (DISCONNECTED, PROVIDER_UNAVAILABLE, RATE_LIMITED,
                                      RECONNECT_REQUIRED, UNSUPPORTED_DATA)
from garmin_mcp.stride_garmin import ConnectionError, GarminSessionProvider, map_error
from garmin_mcp.stride_storage import SupabaseDatabase


logger = logging.getLogger('garmin_mcp.stride.workouts')

KEY_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$')
MAX_WINDOW_DAYS = 31
MAX_WORKOUTS = 40
MAX_STEPS = 30
MAX_REPEAT_STEPS = 10
NAME_PREFIX = 'Stride · '
RPE_TARGET = 'rpe_target'

RUNNING = {'sportTypeId': 1, 'sportTypeKey': 'running'}
STEP_TYPES = {
    'warmup': {'stepTypeId': 1, 'stepTypeKey': 'warmup'},
    'cooldown': {'stepTypeId': 2, 'stepTypeKey': 'cooldown'},
    'work': {'stepTypeId': 3, 'stepTypeKey': 'interval'},
    'recovery': {'stepTypeId': 4, 'stepTypeKey': 'recovery'},
}
REPEAT_TYPE = {'stepTypeId': 6, 'stepTypeKey': 'repeat'}
INTENSITIES = frozenset({'recovery', 'easy', 'marathon', 'threshold', 'interval', 'repetition'})
API_STATUS = re.compile(r'^API Error (\d{3})')


class WorkoutRequestError(ValueError):
    """The workout request does not match the documented contract."""


@dataclass(frozen=True)
class PlannedWorkout:
    key: str
    scheduled_on: date
    name: str
    description: str
    steps: list[dict[str, Any]]
    allow_simplified: bool = False
    force: bool = False

    @property
    def content_hash(self) -> str:
        content = {'date': self.scheduled_on.isoformat(), 'name': self.name,
                   'description': self.description, 'steps': self.steps}
        encoded = json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        return hashlib.sha256(encoded.encode()).hexdigest()


def _integer(value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise WorkoutRequestError('invalid_integer')
    return value


def _text(value: Any, high: int, required: bool = True) -> str:
    if value is None and not required:
        return ''
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > high:
        raise WorkoutRequestError('invalid_text')
    return value.strip()


def _exact(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys() <= required | optional:
        raise WorkoutRequestError('invalid_object')
    return value


def _date(value: Any) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise WorkoutRequestError('invalid_date')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise WorkoutRequestError('invalid_date') from None


def _end(value: Any) -> dict[str, Any]:
    kind = value.get('kind') if isinstance(value, dict) else None
    if kind == 'time':
        _exact(value, {'kind', 'seconds'})
        return {'kind': kind, 'seconds': _integer(value['seconds'], 1, 259_200)}
    if kind == 'distance':
        _exact(value, {'kind', 'meters'}, {'estimatedSeconds'})
        end = {'kind': kind, 'meters': _integer(value['meters'], 1, 400_000)}
        if 'estimatedSeconds' in value:
            end['estimatedSeconds'] = _integer(value['estimatedSeconds'], 1, 259_200)
        return end
    if kind == 'lap_button':
        _exact(value, {'kind', 'estimatedSeconds'})
        return {'kind': kind, 'estimatedSeconds': _integer(value['estimatedSeconds'], 1, 259_200)}
    raise WorkoutRequestError('invalid_end')


def _target(value: Any) -> dict[str, Any]:
    kind = value.get('kind') if isinstance(value, dict) else None
    if kind == 'pace_range':
        _exact(value, {'kind', 'fastSecondsPerKm', 'slowSecondsPerKm'})
        fast = _integer(value['fastSecondsPerKm'], 120, 1_200)
        slow = _integer(value['slowSecondsPerKm'], 120, 1_200)
        if fast > slow:
            raise WorkoutRequestError('invalid_target')
        return {'kind': kind, 'fastSecondsPerKm': fast, 'slowSecondsPerKm': slow}
    if kind == 'heart_rate_range':
        _exact(value, {'kind', 'minBpm', 'maxBpm'})
        low, high = _integer(value['minBpm'], 40, 230), _integer(value['maxBpm'], 40, 230)
        if low > high:
            raise WorkoutRequestError('invalid_target')
        return {'kind': kind, 'minBpm': low, 'maxBpm': high}
    if kind == 'rpe':
        _exact(value, {'kind', 'min', 'max'})
        low, high = _integer(value['min'], 1, 10), _integer(value['max'], 1, 10)
        if low > high:
            raise WorkoutRequestError('invalid_target')
        return {'kind': kind, 'min': low, 'max': high}
    if kind == 'none':
        _exact(value, {'kind'})
        return {'kind': kind}
    raise WorkoutRequestError('invalid_target')


def _simple_step(value: Any) -> dict[str, Any]:
    _exact(value, {'type', 'end', 'target', 'intensity'}, {'cue'})
    if value['type'] not in STEP_TYPES or value['intensity'] not in INTENSITIES:
        raise WorkoutRequestError('invalid_step')
    step = {'type': value['type'], 'end': _end(value['end']), 'target': _target(value['target']),
            'intensity': value['intensity']}
    if 'cue' in value:
        step['cue'] = _text(value['cue'], 80)
    return step


def _step(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get('type') == 'repeat':
        _exact(value, {'type', 'count', 'steps'})
        steps = value['steps']
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_REPEAT_STEPS:
            raise WorkoutRequestError('invalid_step')
        return {'type': 'repeat', 'count': _integer(value['count'], 2, 50),
                'steps': [_simple_step(step) for step in steps]}
    return _simple_step(value)


def parse_workout(value: Any) -> PlannedWorkout:
    _exact(value, {'key', 'date', 'name', 'steps'}, {'description', 'allow_simplified', 'force'})
    key = value['key']
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        raise WorkoutRequestError('invalid_key')
    steps = value['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise WorkoutRequestError('invalid_steps')
    flags = [value.get(flag, False) for flag in ('allow_simplified', 'force')]
    if not all(isinstance(flag, bool) for flag in flags):
        raise WorkoutRequestError('invalid_flag')
    return PlannedWorkout(key, _date(value['date']), _text(value['name'], 80),
                          _text(value.get('description'), 500, required=False),
                          [_step(step) for step in steps], *flags)


def parse_reconcile(body: Any, today: date) -> tuple[date, date, list[PlannedWorkout]]:
    _exact(body, {'from', 'to', 'workouts'})
    start, end = _date(body['from']), _date(body['to'])
    if start < today - timedelta(days=1) or end < start or (end - start).days >= MAX_WINDOW_DAYS:
        raise WorkoutRequestError('invalid_window')
    items = body['workouts']
    if not isinstance(items, list) or len(items) > MAX_WORKOUTS:
        raise WorkoutRequestError('invalid_workouts')
    workouts = [parse_workout(item) for item in items]
    if len({workout.key for workout in workouts}) != len(workouts):
        raise WorkoutRequestError('duplicate_key')
    if any(not start <= workout.scheduled_on <= end for workout in workouts):
        raise WorkoutRequestError('outside_window')
    return start, end, workouts


def _speed(seconds_per_km: int) -> float:
    return round(1000 / seconds_per_km, 4)


def _garmin_step(step: dict[str, Any], order: int,
                 simplifications: set[str]) -> dict[str, Any]:
    end, target = step['end'], step['target']
    notes = [step['cue']] if 'cue' in step else []
    garmin: dict[str, Any] = {'type': 'ExecutableStepDTO', 'stepOrder': order,
                              'stepType': STEP_TYPES[step['type']]}
    if end['kind'] == 'time':
        garmin['endCondition'] = {'conditionTypeId': 2, 'conditionTypeKey': 'time'}
        garmin['endConditionValue'] = float(end['seconds'])
    elif end['kind'] == 'distance':
        garmin['endCondition'] = {'conditionTypeId': 3, 'conditionTypeKey': 'distance'}
        garmin['endConditionValue'] = float(end['meters'])
    else:
        garmin['endCondition'] = {'conditionTypeId': 1, 'conditionTypeKey': 'lap.button'}
    if target['kind'] == 'pace_range':
        garmin['targetType'] = {'workoutTargetTypeId': 6, 'workoutTargetTypeKey': 'pace.zone'}
        garmin['targetValueOne'] = _speed(target['slowSecondsPerKm'])
        garmin['targetValueTwo'] = _speed(target['fastSecondsPerKm'])
    elif target['kind'] == 'heart_rate_range':
        garmin['targetType'] = {'workoutTargetTypeId': 4,
                                'workoutTargetTypeKey': 'heart.rate.zone'}
        garmin['targetValueOne'] = float(target['minBpm'])
        garmin['targetValueTwo'] = float(target['maxBpm'])
    else:
        garmin['targetType'] = {'workoutTargetTypeId': 1, 'workoutTargetTypeKey': 'no.target'}
        if target['kind'] == 'rpe':
            simplifications.add(RPE_TARGET)
            effort = (f"{target['min']}/10" if target['min'] == target['max']
                      else f"{target['min']}–{target['max']}/10")
            notes.insert(0, f'Effort {effort}')
    if notes:
        garmin['description'] = ' · '.join(notes)
    return garmin


def garmin_workout(workout: PlannedWorkout) -> tuple[dict[str, Any], list[str]]:
    """Map a Stride workout to a Garmin running workout and its simplifications."""
    simplifications: set[str] = set()
    steps: list[dict[str, Any]] = []
    order = 0
    for step in workout.steps:
        order += 1
        if step['type'] != 'repeat':
            steps.append(_garmin_step(step, order, simplifications))
            continue
        group = {'type': 'RepeatGroupDTO', 'stepOrder': order, 'stepType': REPEAT_TYPE,
                 'numberOfIterations': step['count'], 'smartRepeat': False,
                 'endCondition': {'conditionTypeId': 7, 'conditionTypeKey': 'iterations'},
                 'endConditionValue': float(step['count']), 'workoutSteps': []}
        for child in step['steps']:
            order += 1
            group['workoutSteps'].append(_garmin_step(child, order, simplifications))
        steps.append(group)
    payload = {'workoutName': NAME_PREFIX + workout.name, 'sportType': RUNNING,
               'workoutSegments': [{'segmentOrder': 1, 'sportType': RUNNING,
                                    'workoutSteps': steps}]}
    if workout.description:
        payload['description'] = workout.description
    return payload, sorted(simplifications)


def _provider_status(error: Exception) -> int | None:
    match = API_STATUS.match(str(error)) if isinstance(error, GarminConnectConnectionError) else None
    return int(match.group(1)) if match else None


def _failure(error: Exception) -> ConnectionError:
    if isinstance(error, ConnectionError):
        return error
    status = _provider_status(error)
    if status == 429:
        return ConnectionError(RATE_LIMITED, 429)
    if status is not None and 400 <= status < 500 and status not in (401, 403):
        return ConnectionError(UNSUPPORTED_DATA, 422)
    return map_error(error)


class WorkoutRepository:
    def __init__(self, database: SupabaseDatabase):
        self.database = database

    def upcoming(self, user_id: uuid.UUID, start: date) -> list[dict[str, Any]]:
        return self.database.request('GET', 'provider_workouts', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin',
            'scheduled_on': f'gte.{start.isoformat()}',
            'select': 'external_key,scheduled_on,status,content_hash,provider_workout_id'}) or []

    def save(self, user_id: uuid.UUID, connection_id: str, row: dict[str, Any]) -> None:
        data = {'user_id': str(user_id), 'connection_id': connection_id, 'provider': 'garmin',
                'error_code': None, 'simplifications': [], 'sent_at': None,
                'updated_at': datetime.now(timezone.utc).isoformat(), **row}
        self.database.request('POST', 'provider_workouts',
                              params={'on_conflict': 'user_id,provider,external_key'},
                              data=data, prefer='resolution=merge-duplicates,return=minimal')

    def delete(self, user_id: uuid.UUID, key: str | None = None) -> None:
        params = {'user_id': f'eq.{user_id}', 'provider': 'eq.garmin'}
        if key is not None:
            params['external_key'] = f'eq.{key}'
        self.database.request('DELETE', 'provider_workouts', params=params)


class WorkoutPushService:
    def __init__(self, sessions: GarminSessionProvider, workouts: WorkoutRepository):
        self.sessions = sessions
        self.workouts = workouts

    def _connection_id(self, user_id: uuid.UUID) -> str:
        connection = self.sessions.connections.get(user_id)
        if connection.get('status') == 'connected' and 'id' in connection:
            return connection['id']
        if connection.get('status') == 'reconnect_required':
            raise ConnectionError(RECONNECT_REQUIRED, 409)
        raise ConnectionError(DISCONNECTED, 409)

    @staticmethod
    def _remove(client: Any, workout_id: int | None) -> None:
        if workout_id is None:
            return
        try:
            client.delete_workout(workout_id)
        except Exception as error:
            if _provider_status(error) != 404:
                raise

    @staticmethod
    def _send(client: Any, payload: dict[str, Any], scheduled_on: date) -> int:
        created = client.upload_workout(payload)
        workout_id = created.get('workoutId') if isinstance(created, dict) else None
        if not isinstance(workout_id, int):
            raise ConnectionError(PROVIDER_UNAVAILABLE, 503)
        try:
            client.schedule_workout(workout_id, scheduled_on.isoformat())
        except Exception:
            try:
                client.delete_workout(workout_id)
            except Exception:
                logger.warning('workout_cleanup_failed', extra={'workout_id': workout_id})
            raise
        return workout_id

    def reconcile(self, user_id: uuid.UUID, start: date, end: date,
                  workouts: list[PlannedWorkout]) -> dict[str, Any]:
        connection_id = self._connection_id(user_id)
        rows = {row['external_key']: row for row in self.workouts.upcoming(user_id, start)}
        wanted = {workout.key for workout in workouts}
        client = self.sessions.for_user(user_id)
        halted: ConnectionError | None = None
        removed: list[str] = []
        results: list[dict[str, Any]] = []
        try:
            for key, row in rows.items():
                scheduled_on = date.fromisoformat(row['scheduled_on'])
                if key in wanted or not start <= scheduled_on <= end:
                    continue
                try:
                    self._remove(client, row['provider_workout_id'])
                except Exception as error:
                    failure = _failure(error)
                    if failure.code == RECONNECT_REQUIRED:
                        raise failure from None
                    logger.warning('workout_removal_failed', extra={
                        'user_id': str(user_id), 'error_code': failure.code})
                    continue
                self.workouts.delete(user_id, key)
                removed.append(key)
            for workout in workouts:
                results.append(self._apply(client, user_id, connection_id, workout,
                                           rows.get(workout.key), halted))
                if results[-1].get('error_code') == RATE_LIMITED:
                    halted = ConnectionError(RATE_LIMITED, 429)
        finally:
            self.sessions.persist(user_id, client)
        logger.info('workouts_reconciled', extra={
            'user_id': str(user_id), 'workouts': len(results), 'removed': len(removed)})
        return {'workouts': results, 'removed': removed}

    def _apply(self, client: Any, user_id: uuid.UUID, connection_id: str,
               workout: PlannedWorkout, row: dict[str, Any] | None,
               halted: ConnectionError | None) -> dict[str, Any]:
        payload, simplifications = garmin_workout(workout)
        content_hash = workout.content_hash
        result: dict[str, Any] = {'key': workout.key,
                                  'date': workout.scheduled_on.isoformat(),
                                  'simplifications': simplifications}
        held = bool(simplifications) and not workout.allow_simplified
        if row and row['content_hash'] == content_hash and not workout.force and (
                row['status'] == 'sent' or (held and row['status'] == 'needs_confirmation')):
            return {**result, 'status': row['status'], 'changed': False}
        record = {'external_key': workout.key, 'scheduled_on': workout.scheduled_on.isoformat(),
                  'content_hash': content_hash, 'simplifications': simplifications,
                  'provider_workout_id': row['provider_workout_id'] if row else None}
        try:
            if halted:
                raise halted
            self._remove(client, record['provider_workout_id'])
            record['provider_workout_id'] = None
            if held:
                record['status'] = 'needs_confirmation'
            else:
                record['provider_workout_id'] = self._send(client, payload, workout.scheduled_on)
                record['status'] = 'sent'
                record['sent_at'] = datetime.now(timezone.utc).isoformat()
        except Exception as error:
            failure = _failure(error)
            if failure.code == RECONNECT_REQUIRED:
                raise failure from None
            record['status'] = 'failed'
            record['error_code'] = failure.code
            result['error_code'] = failure.code
            logger.warning('workout_push_failed', extra={
                'user_id': str(user_id), 'error_code': failure.code})
        self.workouts.save(user_id, connection_id, record)
        return {**result, 'status': record['status'], 'changed': True}

    def remove_upcoming(self, user_id: uuid.UUID, today: date) -> int:
        rows = self.workouts.upcoming(user_id, today)
        if not rows:
            return 0
        self._connection_id(user_id)
        client = self.sessions.for_user(user_id)
        removed = 0
        try:
            for row in rows:
                try:
                    self._remove(client, row['provider_workout_id'])
                except Exception as error:
                    raise _failure(error) from None
                self.workouts.delete(user_id, row['external_key'])
                removed += 1
        finally:
            self.sessions.persist(user_id, client)
        return removed

    def forget(self, user_id: uuid.UUID, today: date) -> None:
        """Remove upcoming workouts before a disconnect, then drop every record."""
        try:
            self.remove_upcoming(user_id, today)
        except Exception as error:
            logger.warning('workout_removal_before_disconnect_failed', extra={
                'user_id': str(user_id),
                'error_code': getattr(error, 'code', PROVIDER_UNAVAILABLE)})
        try:
            self.workouts.delete(user_id)
        except Exception:
            logger.warning('workout_records_cleanup_failed', extra={'user_id': str(user_id)})
