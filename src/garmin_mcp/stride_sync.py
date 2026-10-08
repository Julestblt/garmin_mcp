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


def normalize_activity(user_id: uuid.UUID, activity: dict[str, Any]) -> dict[str, Any]:
    provider_id = activity.get('activityId')
    if provider_id is None:
        raise ValueError('Garmin activity is missing activityId')
    activity_type = activity.get('activityType') or {}
    started_at = activity.get('startTimeGMT')
    if isinstance(started_at, str) and started_at and not started_at.endswith(('Z', '+00:00')):
        started_at += 'Z'
    return {
        'user_id': str(user_id), 'provider': 'garmin',
        'provider_activity_id': str(provider_id),
        'activity_type': activity_type.get('typeKey') if isinstance(activity_type, dict) else None,
        'started_at': started_at,
        'distance_meters': activity.get('distance'),
        'duration_seconds': activity.get('duration'),
    }


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
        end = date.fromisoformat(job['cursor_date']) if job['cursor_date'] else datetime.now(timezone.utc).date()
        oldest = date.fromisoformat(job['oldest_date'])
        start = max(oldest, end - timedelta(days=6))
        client = self.sessions.for_user(user_id)
        try:
            activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
        finally:
            self.sessions.persist(user_id, client)
        if not isinstance(activities, list):
            raise ValueError('Garmin returned invalid activity list')
        rows = [normalize_activity(user_id, activity) for activity in activities]
        for offset in range(0, len(rows), 100):
            self.database.request('POST', 'provider_activities',
                                  params={'on_conflict': 'user_id,provider,provider_activity_id'},
                                  data=rows[offset:offset + 100],
                                  prefer='resolution=merge-duplicates')
        finished = start == oldest
        now = datetime.now(timezone.utc).isoformat()
        update = {
            'state': 'succeeded' if finished else 'queued',
            'cursor_date': None if finished else (start - timedelta(days=1)).isoformat(),
            'oldest_synchronized_date': start.isoformat(),
            'activity_count': job['activity_count'] + len(rows),
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
