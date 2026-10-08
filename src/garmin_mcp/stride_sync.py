import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from garmin_mcp.stride_app import create_stride_components
from garmin_mcp.stride_garmin import ConnectionError, GarminSessionProvider
from garmin_mcp.stride_storage import SupabaseDatabase
from garmin_mcp.stride_logging import configure_logging


logger = logging.getLogger('garmin_mcp.stride.sync')


def _source_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace(' ', 'T').replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def normalize_activity(user_id: uuid.UUID, connection_id: uuid.UUID,
                       activity: dict[str, Any],
                       detail: dict[str, Any] | None = None) -> dict[str, Any]:
    provider_id = activity.get('activityId')
    if provider_id is None:
        raise ValueError('Garmin activity is missing activityId')
    detail = detail or {}
    activity_type = detail.get('activityType') or activity.get('activityType') or {}
    started_at = activity.get('startTimeGMT')
    if isinstance(started_at, str) and started_at and not started_at.endswith(('Z', '+00:00')):
        started_at += 'Z'
    updated_at = detail.get('updateDate') or activity.get('updateDate')
    if isinstance(updated_at, str) and updated_at and not updated_at.endswith(('Z', '+00:00')):
        updated_at += 'Z'
    return {
        'user_id': str(user_id), 'connection_id': str(connection_id), 'provider': 'garmin',
        'provider_activity_id': str(provider_id),
        'activity_type': activity_type.get('typeKey') if isinstance(activity_type, dict) else None,
        'started_at': started_at,
        'distance_meters': detail.get('distance', activity.get('distance')),
        'duration_seconds': detail.get('duration', activity.get('duration')),
        'local_started_at': detail.get('startTimeLocal', activity.get('startTimeLocal')),
        'time_zone_id': detail.get('timeZoneId', activity.get('timeZoneId')),
        'elevation_gain_meters': detail.get('elevationGain', activity.get('elevationGain')),
        'elevation_loss_meters': detail.get('elevationLoss', activity.get('elevationLoss')),
        'average_heart_rate': detail.get('averageHR', activity.get('averageHR')),
        'max_heart_rate': detail.get('maxHR', activity.get('maxHR')),
        'average_cadence': detail.get('averageRunningCadenceInStepsPerMinute',
                                      activity.get('averageRunningCadenceInStepsPerMinute')),
        'average_power_watts': detail.get('avgPower', activity.get('avgPower')),
        'max_power_watts': detail.get('maxPower', activity.get('maxPower')),
        'aerobic_training_effect': detail.get('aerobicTrainingEffect',
                                               activity.get('aerobicTrainingEffect')),
        'anaerobic_training_effect': detail.get('anaerobicTrainingEffect',
                                                 activity.get('anaerobicTrainingEffect')),
        'route_name': detail.get('courseName', activity.get('courseName')),
        'has_route': detail.get('hasPolyline', activity.get('hasPolyline')),
        'source_updated_at': updated_at,
    }


def normalize_segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for kind, keys in (('lap', ('lapDTOs', 'laps')),
                       ('split', ('splitDTOs', 'splits'))):
        source = next((payload[key] for key in keys if isinstance(payload.get(key), list)), [])
        for index, item in enumerate(source):
            if not isinstance(item, dict):
                continue
            started_at = item.get('startTimeGMT')
            if isinstance(started_at, str) and started_at and not started_at.endswith(('Z', '+00:00')):
                started_at += 'Z'
            rows.append({
                'segment_type': kind, 'segment_index': index,
                'started_at': started_at,
                'distance_meters': item.get('distance'),
                'duration_seconds': item.get('duration'),
                'average_heart_rate': item.get('averageHR'),
                'average_cadence': item.get('averageRunCadence'),
                'average_power_watts': item.get('averagePower'),
                'elevation_gain_meters': item.get('elevationGain'),
                'elevation_loss_meters': item.get('elevationLoss'),
            })
    return rows


class ActivitySyncWorker:
    def __init__(self, database: SupabaseDatabase, sessions: GarminSessionProvider):
        self.database = database
        self.sessions = sessions

    def run_once(self) -> bool:
        jobs = self.database.request('POST', 'rpc/claim_sync_job', data={})
        if not jobs:
            return False
        job = jobs[0]
        try:
            self._process(job)
        except ConnectionError as error:
            self._fail(job, error.code, retry=error.code in ('rate_limited', 'provider_unavailable'))
        except Exception:
            logger.exception('sync_job_failed', extra={'sync_job_id': job['id'], 'user_id': job['user_id']})
            self._fail(job, 'sync_failed', retry=True)
        return True

    def _process(self, job: dict[str, Any]) -> None:
        user_id = uuid.UUID(job['user_id'])
        connection_id = uuid.UUID(job['connection_id'])
        end = date.fromisoformat(job['cursor_date']) if job['cursor_date'] else datetime.now(timezone.utc).date()
        oldest = date.fromisoformat(job['oldest_date'])
        start = max(oldest, end - timedelta(days=6))
        client = self.sessions.for_user(user_id)
        try:
            activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
            if not isinstance(activities, list):
                raise ValueError('Garmin returned invalid activity list')
            provider_ids = [str(item['activityId']) for item in activities]
            if any(not provider_id.isdigit() for provider_id in provider_ids):
                raise ValueError('Garmin returned invalid activity ID')
            known: dict[str, dict[str, Any]] = {}
            for offset in range(0, len(provider_ids), 100):
                batch = provider_ids[offset:offset + 100]
                stored = self.database.request('GET', 'provider_activities', params={
                    'user_id': f'eq.{user_id}', 'provider': 'eq.garmin',
                    'provider_activity_id': f'in.({",".join(batch)})',
                    'select': 'provider_activity_id,source_updated_at,detail_imported_at'})
                known.update({row['provider_activity_id']: row for row in stored})
            rows = []
            for activity in activities:
                provider_id = str(activity['activityId'])
                previous = known.get(provider_id)
                if (job.get('mode') != 'incremental' and previous
                        and previous['detail_imported_at']
                        and _source_time(previous['source_updated_at']) ==
                        _source_time(activity.get('updateDate'))):
                    continue
                detail = client.get_activity(provider_id)
                splits = client.get_activity_splits(provider_id)
                rows.append((normalize_activity(user_id, connection_id, activity, detail),
                             normalize_segments(splits)))
        finally:
            self.sessions.persist(user_id, client)
        for activity, segments in rows:
            stored = self.database.request('POST', 'rpc/upsert_garmin_activity', data={
                'p_connection_id': str(connection_id), 'p_user_id': str(user_id),
                'p_activity': activity, 'p_segments': segments})
            if not stored:
                raise ConnectionError('reconnect_required', 409)
        activity_count = self.database.request('POST', 'rpc/count_provider_activities',
                                               data={'p_connection_id': str(connection_id)})
        finished = start == oldest
        now = datetime.now(timezone.utc).isoformat()
        update = {
            'state': 'succeeded' if finished else 'queued',
            'cursor_date': None if finished else (start - timedelta(days=1)).isoformat(),
            'oldest_synchronized_date': start.isoformat(),
            'activity_count': activity_count,
            'failure_count': 0,
            'lease_expires_at': None, 'updated_at': now,
            'next_attempt_at': (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(),
        }
        if finished:
            update['last_success_at'] = now
        self.database.request('PATCH', 'sync_jobs',
                              params={'id': f"eq.{job['id']}", 'attempts': f"eq.{job['attempts']}"},
                              data=update)
        if finished:
            self.database.request('PATCH', 'provider_connections',
                                  params={'id': f"eq.{job['connection_id']}"},
                                  data={'last_sync_at': now})
        logger.info('sync_chunk_completed', extra={'sync_job_id': job['id'],
                    'user_id': str(user_id), 'activity_count': len(rows)})

    def _fail(self, job: dict[str, Any], code: str, retry: bool) -> None:
        attempts = job['attempts']
        failure_count = job['failure_count'] + 1
        backoff = min(3600, 30 * 2 ** min(failure_count, 7))
        state = 'queued' if retry and failure_count < 6 else 'failed'
        self.database.request('PATCH', 'sync_jobs',
                              params={'id': f"eq.{job['id']}", 'attempts': f'eq.{attempts}'},
                              data={'state': state, 'last_error_code': code,
                                    'failure_count': failure_count,
                                    'next_attempt_at': (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat(),
                                    'lease_expires_at': None})
        if code == 'reconnect_required':
            self.database.request('PATCH', 'provider_connections',
                                  params={'id': f"eq.{job['connection_id']}"},
                                  data={'status': 'reconnect_required', 'last_error_code': code})


def main() -> None:
    configure_logging()
    components = create_stride_components()
    worker = ActivitySyncWorker(components.database, components.sessions)
    next_cleanup = 0.0
    while True:
        if time.monotonic() >= next_cleanup:
            components.database.request('DELETE', 'auth_challenges', params={
                'expires_at': f'lt.{datetime.now(timezone.utc).isoformat()}'})
            components.database.request('DELETE', 'auth_attempts', params={
                'window_number': f'lt.{int(time.time() // 600) - 2}'})
            next_cleanup = time.monotonic() + 3600
        if not worker.run_once():
            time.sleep(5)
