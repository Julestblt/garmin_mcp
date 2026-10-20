"""Running shoes from Garmin for Stride.

Garmin counts each pair's distance and links activities to gear. This module
normalizes the athlete's shoes and those links into ``provider_gear`` and
``provider_activity_gear``, and applies the writes Stride needs: creating a
pair, updating its wear limit, default activities or status, and setting the
pair of an activity. Gear uuids are stored as 32 lowercase hex characters,
the form Garmin's legacy gear endpoints return; the v2 endpoints receive the
dashed form.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from garmin_mcp.stride_errors import (ACTIVITY_NOT_FOUND, DISCONNECTED, GEAR_NOT_FOUND,
                                      GEAR_STARTS_AFTER_ACTIVITY, PROVIDER_UNAVAILABLE,
                                      RECONNECT_REQUIRED)
from garmin_mcp.stride_garmin import ConnectionError, GarminSessionProvider
from garmin_mcp.stride_storage import SupabaseDatabase
from garmin_mcp.stride_workouts import provider_failure


logger = logging.getLogger('garmin_mcp.stride.gear')

UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$')
ACTIVITY_ID = re.compile(r'^[0-9]{1,20}$')
DEFAULT_ACTIVITY_TYPES = frozenset({'running', 'trail_running', 'treadmill_running'})
GEAR_ACTIVITY_LIMIT = 1000
MAX_DISTANCE_METERS = 5_000_000
SOCIAL_PROFILE_URL = '/userprofile-service/socialProfile'
CREATED_NOTE = 'Ajoutée depuis Stride'
UNMATCHED_CATALOG = frozenset({'other', 'unknown shoes'})


class GearRequestError(ValueError):
    """The gear request does not match the documented contract."""


def gear_uuid(value: Any) -> str:
    if not isinstance(value, str) or not UUID_PATTERN.match(value.strip().lower()):
        raise GearRequestError('invalid_gear_uuid')
    return value.strip().lower().replace('-', '')


def dashed(key: str) -> str:
    return str(uuid.UUID(hex=key))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float('inf') else None


def _day(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _catalog(value: Any) -> str | None:
    text = _text(value)
    return None if text is None or text.lower() in UNMATCHED_CATALOG else text


def normalize_gear(user_id: uuid.UUID, connection_id: str, gear: dict[str, Any],
                   stats: dict[str, Any],
                   details: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Map a legacy Garmin gear record, its stats and v2 details to a ``provider_gear`` row.

    The legacy record names free-text pairs "Other" / "Unknown Shoes"; the v2
    details keep the brand and model the athlete typed.
    """
    details = details or {}
    if 'shoe' not in str(gear.get('gearTypeName') or '').lower():
        return None
    try:
        key = gear_uuid(gear.get('uuid'))
    except GearRequestError:
        return None
    status = str(gear.get('gearStatusName') or '').lower()
    maximum = _number(gear.get('maximumMeters'))
    distance = _number(stats.get('totalDistance'))
    activities = stats.get('totalActivities')
    return {
        'user_id': str(user_id), 'connection_id': str(connection_id), 'provider': 'garmin',
        'gear_uuid': key, 'gear_type': 'shoes',
        'brand': _text(details.get('brand')) or _catalog(gear.get('gearMakeName')),
        'model': _text(details.get('model')) or _catalog(gear.get('gearModelName')),
        'display_name': _text(gear.get('displayName')) or _text(gear.get('customMakeModel')),
        'status': 'retired' if status == 'retired' else 'active',
        'first_use_date': _day(gear.get('dateBegin')),
        'retired_on': _day(gear.get('dateEnd')) if status == 'retired' else None,
        'maximum_meters': maximum if maximum and maximum > 0 else None,
        'total_distance_meters': max(distance or 0.0, 0.0),
        'total_activities': activities if isinstance(activities, int) and activities > 0 else 0,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }


def _exact(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys() <= required | optional:
        raise GearRequestError('invalid_object')
    return value


def _label(value: Any, high: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > high:
        raise GearRequestError('invalid_text')
    return value.strip()


def _distance(value: Any, allow_zero: bool) -> int:
    low = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= MAX_DISTANCE_METERS:
        raise GearRequestError('invalid_distance')
    return value


def _activity_types(value: Any) -> list[str]:
    if not isinstance(value, list) or len(set(value)) != len(value) or any(
            item not in DEFAULT_ACTIVITY_TYPES for item in value):
        raise GearRequestError('invalid_activity_types')
    return list(value)


def parse_create(body: Any) -> dict[str, Any]:
    _exact(body, {'brand', 'model', 'name', 'first_use_date'},
           {'max_distance_meters', 'default_activity_types'})
    first_use = _day(body['first_use_date'])
    if first_use is None or len(body['first_use_date']) != 10:
        raise GearRequestError('invalid_date')
    request = {'brand': _label(body['brand'], 60), 'model': _label(body['model'], 80),
               'name': _label(body['name'], 80), 'first_use_date': first_use,
               'default_activity_types': _activity_types(body.get('default_activity_types', []))}
    if 'max_distance_meters' in body:
        request['max_distance_meters'] = _distance(body['max_distance_meters'], False)
    return request


def parse_update(body: Any) -> dict[str, Any]:
    _exact(body, set(), {'name', 'max_distance_meters', 'default_activity_types', 'status'})
    if not body:
        raise GearRequestError('empty_update')
    request: dict[str, Any] = {}
    if 'name' in body:
        request['name'] = _label(body['name'], 80)
    if 'max_distance_meters' in body:
        request['max_distance_meters'] = _distance(body['max_distance_meters'], True)
    if 'default_activity_types' in body:
        request['default_activity_types'] = _activity_types(body['default_activity_types'])
    if 'status' in body:
        if body['status'] not in ('active', 'retired'):
            raise GearRequestError('invalid_status')
        request['status'] = body['status']
    return request


def parse_activity_gear(activity_id: Any, body: Any) -> tuple[str, str | None]:
    if not isinstance(activity_id, str) or not ACTIVITY_ID.match(activity_id):
        raise GearRequestError('invalid_activity_id')
    _exact(body, {'gear_uuid'})
    return activity_id, None if body['gear_uuid'] is None else gear_uuid(body['gear_uuid'])


@dataclass(frozen=True)
class GearSnapshot:
    """Gear rows and activity links read from Garmin, written after the session is released."""
    rows: list[dict[str, Any]]
    links: dict[str, list[str]]
    complete: bool


class GearRepository:
    def __init__(self, database: SupabaseDatabase):
        self.database = database

    def list(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        return self.database.request('GET', 'provider_gear', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin',
            'select': 'gear_uuid,brand,model,total_distance_meters,total_activities'}) or []

    def activity_exists(self, user_id: uuid.UUID, activity_id: str) -> bool:
        return bool(self.database.request('GET', 'provider_activities', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin',
            'provider_activity_id': f'eq.{activity_id}', 'select': 'id', 'limit': '1'}))

    def store(self, user_id: uuid.UUID, connection_id: str, snapshot: GearSnapshot) -> None:
        if snapshot.rows:
            self.database.request('POST', 'provider_gear',
                                  params={'on_conflict': 'user_id,provider,gear_uuid'},
                                  data=snapshot.rows,
                                  prefer='resolution=merge-duplicates,return=minimal')
        if snapshot.complete:
            kept = ','.join(row['gear_uuid'] for row in snapshot.rows)
            for table in ('provider_activity_gear', 'provider_gear'):
                params = {'user_id': f'eq.{user_id}', 'provider': 'eq.garmin'}
                if kept:
                    params['gear_uuid'] = f'not.in.({kept})'
                self.database.request('DELETE', table, params=params)
        now = datetime.now(timezone.utc).isoformat()
        for key, activity_ids in snapshot.links.items():
            if activity_ids:
                self.database.request('POST', 'provider_activity_gear',
                                      params={'on_conflict': 'user_id,provider,provider_activity_id'},
                                      data=[{'user_id': str(user_id), 'connection_id': str(connection_id),
                                             'provider': 'garmin', 'provider_activity_id': activity_id,
                                             'gear_uuid': key, 'updated_at': now}
                                            for activity_id in activity_ids],
                                      prefer='resolution=merge-duplicates,return=minimal')
            params = {'user_id': f'eq.{user_id}', 'provider': 'eq.garmin', 'gear_uuid': f'eq.{key}'}
            if activity_ids:
                params['provider_activity_id'] = f'not.in.({",".join(activity_ids)})'
            self.database.request('DELETE', 'provider_activity_gear', params=params)


def _profile_number(client: Any) -> Any:
    device = client.get_device_last_used()
    number = device.get('userProfileNumber') if isinstance(device, dict) else None
    if number is None:
        profile = client.connectapi(SOCIAL_PROFILE_URL)
        number = profile.get('profileId') if isinstance(profile, dict) else None
    if number is None:
        raise ConnectionError(PROVIDER_UNAVAILABLE, 503)
    return number


def _details(client: Any, key: str) -> dict[str, Any]:
    try:
        details = client.get_gear_details(dashed(key))
    except Exception:
        logger.warning('gear_details_unavailable')
        return {}
    return details if isinstance(details, dict) else {}


def _activity_ids(activities: Any) -> list[str]:
    if not isinstance(activities, list):
        return []
    ids = (str(item.get('activityId', '')) for item in activities if isinstance(item, dict))
    return sorted({activity_id for activity_id in ids if ACTIVITY_ID.match(activity_id)})


class GearService:
    def __init__(self, sessions: GarminSessionProvider, repository: GearRepository):
        self.sessions = sessions
        self.repository = repository

    def collect(self, client: Any, user_id: uuid.UUID, connection_id: str,
                only: set[str] | None = None) -> GearSnapshot:
        """Read shoes, stats and, for pairs whose totals changed, their activities."""
        listed = client.get_gear(_profile_number(client))
        if not isinstance(listed, list):
            raise ConnectionError(PROVIDER_UNAVAILABLE, 503)
        stored = {row['gear_uuid']: row for row in self.repository.list(user_id)}
        rows: list[dict[str, Any]] = []
        links: dict[str, list[str]] = {}
        for gear in listed:
            if not isinstance(gear, dict) or 'shoe' not in str(gear.get('gearTypeName') or '').lower():
                continue
            try:
                key = gear_uuid(gear.get('uuid'))
            except GearRequestError:
                continue
            if only is not None and key not in only:
                continue
            stats = client.get_gear_stats(key)
            stats = stats if isinstance(stats, dict) else {}
            previous = stored.get(key)
            changed = only is not None or previous is None or (
                previous['total_activities'], previous['total_distance_meters']) != (
                stats.get('totalActivities', 0), _number(stats.get('totalDistance')) or 0.0)
            details = (_details(client, key) if changed
                       else {'brand': previous.get('brand'), 'model': previous.get('model')})
            row = normalize_gear(user_id, connection_id, gear, stats, details)
            if row is None:
                continue
            rows.append(row)
            if changed:
                links[key] = _activity_ids(client.get_gear_activities(key, GEAR_ACTIVITY_LIMIT))
        return GearSnapshot(rows, links, only is None)

    def _connection_id(self, user_id: uuid.UUID) -> str:
        connection = self.sessions.connections.get(user_id)
        if connection.get('status') == 'connected' and 'id' in connection:
            return connection['id']
        if connection.get('status') == 'reconnect_required':
            raise ConnectionError(RECONNECT_REQUIRED, 409)
        raise ConnectionError(DISCONNECTED, 409)

    def _write(self, user_id: uuid.UUID, apply: Any) -> GearSnapshot:
        connection_id = self._connection_id(user_id)
        client = self.sessions.for_user(user_id)
        try:
            only = apply(client)
            snapshot = self.collect(client, user_id, connection_id, only)
        except ConnectionError:
            raise
        except Exception as error:
            raise provider_failure(error) from None
        finally:
            self.sessions.persist(user_id, client)
        self.repository.store(user_id, connection_id, snapshot)
        return snapshot

    def _known(self, user_id: uuid.UUID, key: str) -> None:
        if key not in {row['gear_uuid'] for row in self.repository.list(user_id)}:
            raise ConnectionError(GEAR_NOT_FOUND, 404)

    def create(self, user_id: uuid.UUID, request: dict[str, Any]) -> dict[str, Any]:
        def apply(client: Any) -> set[str]:
            maximum = request.get('max_distance_meters')
            created = client.create_gear(
                'SHOES', request['brand'], request['model'], request['name'],
                request['first_use_date'],
                max_usage_distance_km=maximum / 1000 if maximum else None,
                notes=CREATED_NOTE,
                activity_type_keys=request['default_activity_types'] or None)
            if not isinstance(created, dict):
                raise ConnectionError(PROVIDER_UNAVAILABLE, 503)
            return {gear_uuid(created.get('uuid'))}

        snapshot = self._write(user_id, apply)
        if not snapshot.rows:
            raise ConnectionError(PROVIDER_UNAVAILABLE, 503)
        logger.info('gear_created', extra={'user_id': str(user_id)})
        return {'gear': snapshot.rows[0]}

    def update(self, user_id: uuid.UUID, key: str, request: dict[str, Any]) -> dict[str, Any]:
        self._known(user_id, key)

        def apply(client: Any) -> set[str]:
            changes: dict[str, Any] = {}
            if 'name' in request:
                changes['name'] = request['name']
            if 'max_distance_meters' in request:
                changes['max_usage_distance_km'] = request['max_distance_meters'] / 1000
            if 'default_activity_types' in request:
                changes['activity_type_keys'] = request['default_activity_types']
            if changes:
                client.update_gear(dashed(key), **changes)
            if 'status' in request:
                client.set_gear_status(dashed(key), request['status'].upper())
            return {key}

        snapshot = self._write(user_id, apply)
        if not snapshot.rows:
            raise ConnectionError(GEAR_NOT_FOUND, 404)
        logger.info('gear_updated', extra={'user_id': str(user_id), 'fields': sorted(request)})
        return {'gear': snapshot.rows[0]}

    def set_activity_gear(self, user_id: uuid.UUID, activity_id: str,
                          key: str | None) -> dict[str, Any]:
        if key is not None:
            self._known(user_id, key)
        if not self.repository.activity_exists(user_id, activity_id):
            raise ConnectionError(ACTIVITY_NOT_FOUND, 404)

        def apply(client: Any) -> set[str]:
            current = client.get_activity_gear(activity_id)
            linked = set()
            for gear in current if isinstance(current, list) else []:
                if isinstance(gear, dict) and 'shoe' in str(gear.get('gearTypeName') or '').lower():
                    try:
                        linked.add(gear_uuid(gear.get('uuid')))
                    except GearRequestError:
                        continue
            for other in sorted(linked - {key}):
                client.remove_gear_from_activity(other, activity_id)
            if key is not None and key not in linked:
                try:
                    client.add_gear_to_activity(key, activity_id)
                except Exception as error:
                    if 'start date must be before' in str(error).lower():
                        raise ConnectionError(GEAR_STARTS_AFTER_ACTIVITY, 422) from None
                    raise
            return linked | ({key} if key else set())

        snapshot = self._write(user_id, apply)
        logger.info('activity_gear_set', extra={'user_id': str(user_id), 'cleared': key is None})
        return {'provider_activity_id': activity_id, 'gear_uuid': key, 'gear': snapshot.rows}
