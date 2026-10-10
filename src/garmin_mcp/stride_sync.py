import logging
import math
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from garmin_mcp.stride_app import create_stride_components
from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

from garmin_mcp.stride_errors import PROVIDER_UNAVAILABLE, SYNC_FAILED, is_retryable
from garmin_mcp.stride_garmin import ConnectionError, GarminSessionProvider
from garmin_mcp.stride_storage import SupabaseDatabase
from garmin_mcp.stride_logging import configure_logging
from garmin_mcp.stride_models import Activity, ActivitySegment
from garmin_mcp.stride_observations import normalize_fitness, normalize_recovery
from garmin_mcp.stride_tracks import (GARMIN_MAX_CHART_SIZE, GARMIN_MAX_POLYLINE_SIZE,
                                      normalize_track, unavailable_track)


logger = logging.getLogger('garmin_mcp.stride.sync')


TRACK_BATCH_SIZE = 5
TRACK_BATCH_SPACING_SECONDS = 5
DEFAULT_TRACK_HISTORY_DAYS = 90
MAX_TRACK_HISTORY_DAYS = 3650


def track_history_days() -> int:
    try:
        days = int(os.getenv('STRIDE_TRACK_HISTORY_DAYS', str(DEFAULT_TRACK_HISTORY_DAYS)))
    except ValueError:
        return DEFAULT_TRACK_HISTORY_DAYS
    return min(max(days, 1), MAX_TRACK_HISTORY_DAYS)


class StaleSyncJob(Exception):
    pass


def track_payload(track: dict[str, Any]) -> dict[str, Any]:
    """Drop null values so PostgreSQL receives missing keys, not JSON null."""
    return {key: value for key, value in track.items() if value is not None}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _source_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace(' ', 'T').replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def normalize_activity(user_id: uuid.UUID, connection_id: uuid.UUID,
                       activity: dict[str, Any],
                       detail: dict[str, Any] | None = None) -> Activity:
    provider_id = activity.get('activityId')
    if provider_id is None:
        raise ValueError('Garmin activity is missing activityId')
    detail = detail or {}
    summary = detail.get('summaryDTO') if isinstance(detail.get('summaryDTO'), dict) else {}
    rpe = _positive(summary.get('directWorkoutRpe', activity.get('directWorkoutRpe')))
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
        'activity_name': _text(activity.get('activityName')) or _text(detail.get('activityName')),
        'calories': _positive(summary.get('calories', activity.get('calories'))),
        'workout_rpe': round(rpe / 10, 1) if rpe is not None else None,
        'workout_feel': _positive(summary.get('directWorkoutFeel',
                                              activity.get('directWorkoutFeel'))),
        'source_updated_at': updated_at,
    }


def normalize_segments(payload: dict[str, Any]) -> list[ActivitySegment]:
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
        started = time.monotonic()
        logger.info('sync_job_started', extra={
            'sync_job_id': job['id'], 'user_id': job['user_id'],
            'phase': job.get('phase'), 'mode': job.get('mode'),
            'attempts': job.get('attempts')})
        try:
            self._process(job)
        except StaleSyncJob:
            logger.info('stale_sync_job_ignored', extra={'sync_job_id': job['id'],
                        'user_id': job['user_id']})
        except ConnectionError as error:
            self._fail(job, error.code, retry=is_retryable(error.code))
        except Exception:
            logger.exception('sync_job_failed', extra={'sync_job_id': job['id'], 'user_id': job['user_id']})
            self._fail(job, SYNC_FAILED, retry=True)
        else:
            logger.info('sync_job_finished', extra={
                'sync_job_id': job['id'], 'user_id': job['user_id'],
                'phase': job.get('phase'),
                'duration_ms': int((time.monotonic() - started) * 1000)})
        return True

    def _process(self, job: dict[str, Any]) -> None:
        if job.get('phase') in ('recovery', 'fitness'):
            self._process_observations(job)
            return
        if job.get('phase') == 'tracks':
            self._process_tracks(job)
            return
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
                'p_job_id': job['id'], 'p_attempts': job['attempts'],
                'p_activity': activity, 'p_segments': segments})
            if not stored:
                raise StaleSyncJob
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
        updated = self.database.request('PATCH', 'sync_jobs',
                              params={'id': f"eq.{job['id']}", 'attempts': f"eq.{job['attempts']}",
                                      'state': 'eq.running'},
                              data=update, prefer='return=representation')
        if not updated:
            raise StaleSyncJob
        if finished:
            self.database.request('PATCH', 'provider_connections',
                                  params={'id': f"eq.{job['connection_id']}"},
                                  data={'last_sync_at': now})
        logger.info('sync_chunk_completed', extra={'sync_job_id': job['id'],
                    'user_id': str(user_id), 'activity_count': len(rows)})

    def _process_tracks(self, job: dict[str, Any]) -> None:
        user_id = uuid.UUID(job['user_id'])
        connection_id = uuid.UUID(job['connection_id'])
        since = datetime.now(timezone.utc) - timedelta(days=track_history_days())
        pending = self.database.request('POST', 'rpc/list_activities_missing_tracks', data={
            'p_connection_id': str(connection_id), 'p_since': since.isoformat(),
            'p_limit': TRACK_BATCH_SIZE}) or []
        tracks: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        if pending:
            client = self.sessions.for_user(user_id)
            try:
                for item in pending:
                    try:
                        details = client.get_activity_details(
                            item['provider_activity_id'], maxchart=GARMIN_MAX_CHART_SIZE,
                            maxpoly=GARMIN_MAX_POLYLINE_SIZE)
                    except (GarminConnectTooManyRequestsError,
                            GarminConnectAuthenticationError):
                        raise
                    except Exception:
                        logger.warning('activity_track_unavailable', extra={
                            'sync_job_id': job['id'], 'user_id': str(user_id),
                            'provider_activity_id': item['provider_activity_id']})
                        tracks.append((item, None))
                        continue
                    tracks.append((item, normalize_track(details)))
            finally:
                self.sessions.persist(user_id, client)
            failed = sum(track is None for _, track in tracks)
            if failed >= 3 and failed == len(tracks):
                raise ConnectionError(PROVIDER_UNAVAILABLE, 503)
        for item, track in tracks:
            stored = self.database.request('POST', 'rpc/upsert_activity_track', data={
                'p_connection_id': str(connection_id), 'p_user_id': str(user_id),
                'p_job_id': job['id'], 'p_attempts': job['attempts'],
                'p_activity_id': item['id'],
                'p_track': track_payload(track if track is not None else unavailable_track())})
            if not stored:
                raise StaleSyncJob
        finished = len(pending) < TRACK_BATCH_SIZE
        now = datetime.now(timezone.utc)
        update = {
            'state': 'succeeded' if finished else 'queued',
            'failure_count': 0, 'last_error_code': None,
            'lease_expires_at': None, 'updated_at': now.isoformat(),
            'next_attempt_at': (now + timedelta(
                seconds=TRACK_BATCH_SPACING_SECONDS)).isoformat(),
        }
        if finished:
            update['last_success_at'] = now.isoformat()
        updated = self.database.request('PATCH', 'sync_jobs',
                                        params={'id': f"eq.{job['id']}",
                                                'attempts': f"eq.{job['attempts']}",
                                                'state': 'eq.running'},
                                        data=update, prefer='return=representation')
        if not updated:
            raise StaleSyncJob
        logger.info('track_chunk_completed', extra={
            'sync_job_id': job['id'], 'user_id': str(user_id), 'track_count': len(tracks),
            'unavailable_count': sum(track is None or track['status'] == 'unavailable'
                                     for _, track in tracks)})

    def _process_observations(self, job: dict[str, Any]) -> None:
        user_id = uuid.UUID(job['user_id'])
        connection_id = uuid.UUID(job['connection_id'])
        day = date.fromisoformat(job['cursor_date']) if job['cursor_date'] else datetime.now(timezone.utc).date()
        oldest = date.fromisoformat(job['oldest_date'])
        client = self.sessions.for_user(user_id, load_profile=True)
        try:
            if job['phase'] == 'recovery':
                rows = normalize_recovery(
                    day, client.get_sleep_data(day.isoformat()),
                    client.get_hrv_data(day.isoformat()),
                    client.get_rhr_day(day.isoformat()),
                    client.get_stress_data(day.isoformat()),
                    client.get_body_battery(day.isoformat()),
                    client.get_training_readiness(day.isoformat()))
            else:
                rows = normalize_fitness(
                    day, client.get_training_status(day.isoformat()),
                    client.get_max_metrics(day.isoformat()))
        finally:
            self.sessions.persist(user_id, client)
        stored = self.database.request('POST', 'rpc/upsert_provider_observations', data={
            'p_connection_id': str(connection_id), 'p_user_id': str(user_id),
            'p_job_id': job['id'], 'p_attempts': job['attempts'],
            'p_observations': rows})
        if not stored:
            raise StaleSyncJob
        finished = day <= oldest
        now = datetime.now(timezone.utc).isoformat()
        updated = self.database.request('PATCH', 'sync_jobs',
                              params={'id': f"eq.{job['id']}", 'attempts': f"eq.{job['attempts']}",
                                      'state': 'eq.running'},
                              data={'state': 'succeeded' if finished else 'queued',
                                    'cursor_date': None if finished else (day - timedelta(days=1)).isoformat(),
                                    'oldest_synchronized_date': day.isoformat(),
                                    'failure_count': 0, 'lease_expires_at': None,
                                    'updated_at': now,
                                    'next_attempt_at': (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(),
                                    'last_success_at': now if finished else None},
                              prefer='return=representation')
        if not updated:
            raise StaleSyncJob
        if finished:
            self.database.request('PATCH', 'provider_connections',
                                  params={'id': f'eq.{connection_id}'},
                                  data={'last_sync_at': now})
        logger.info('sync_chunk_completed', extra={'sync_job_id': job['id'],
                    'user_id': str(user_id), 'phase': job['phase'], 'observation_count': len(rows)})

    def _fail(self, job: dict[str, Any], code: str, retry: bool) -> None:
        attempts = job['attempts']
        failure_count = job['failure_count'] + 1
        backoff = min(3600, 30 * 2 ** min(failure_count, 7))
        state = 'queued' if retry and failure_count < 6 else 'failed'
        self.database.request('PATCH', 'sync_jobs',
                              params={'id': f"eq.{job['id']}", 'attempts': f'eq.{attempts}',
                                      'state': 'eq.running'},
                              data={'state': state, 'last_error_code': code,
                                    'failure_count': failure_count,
                                    'next_attempt_at': (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat(),
                                    'lease_expires_at': None})
        logger.warning('sync_job_retry_scheduled' if state == 'queued' else 'sync_job_failed',
                       extra={'sync_job_id': job['id'], 'user_id': job['user_id'],
                              'phase': job.get('phase'), 'error_code': code,
                              'failure_count': failure_count, 'retry': state == 'queued'})
        if code == 'reconnect_required':
            logger.warning('reconnect_required', extra={
                'sync_job_id': job['id'], 'user_id': job['user_id'],
                'connection_id': job['connection_id'], 'error_code': code})
            self.database.request('PATCH', 'provider_connections',
                                  params={'id': f"eq.{job['connection_id']}"},
                                  data={'status': 'reconnect_required', 'last_error_code': code})


def main() -> None:
    configure_logging()
    components = create_stride_components()
    worker = ActivitySyncWorker(components.database, components.sessions)
    next_cleanup = 0.0
    next_incremental_schedule = 0.0
    next_queue_report = 0.0
    while True:
        if time.monotonic() >= next_incremental_schedule:
            scheduled = components.database.request('POST', 'rpc/schedule_incremental_sync', data={})
            logger.info('incremental_jobs_scheduled', extra={'job_count': scheduled})
            next_incremental_schedule = time.monotonic() + 3600
        if time.monotonic() >= next_cleanup:
            components.database.request('DELETE', 'auth_challenges', params={
                'expires_at': f'lt.{datetime.now(timezone.utc).isoformat()}'})
            components.database.request('DELETE', 'auth_attempts', params={
                'window_number': f'lt.{int(time.time() // 600) - 2}'})
            next_cleanup = time.monotonic() + 3600
        if time.monotonic() >= next_queue_report:
            try:
                depth = components.database.count('sync_jobs', params={
                    'state': 'in.(queued,running)'})
                logger.info('sync_queue_depth', extra={'queue_depth': depth})
            except Exception:
                logger.warning('sync_queue_depth_unavailable')
            next_queue_report = time.monotonic() + 60
        if not worker.run_once():
            time.sleep(5)
