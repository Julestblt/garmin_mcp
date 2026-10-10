import uuid
from datetime import date, timedelta

import pytest
from garminconnect import GarminConnectTooManyRequestsError

from garmin_mcp.stride_sync import (ActivitySyncWorker, normalize_activity,
                                    normalize_segments, track_history_days)


class FakeDatabase:
    def __init__(self, job):
        self.job = job
        self.activities = {}
        self.stale = False

    def request(self, method, path, *, params=None, data=None, prefer=None):
        if path == 'rpc/claim_sync_job':
            if self.job['state'] != 'queued':
                return []
            self.job['state'] = 'running'
            self.job['attempts'] += 1
            return [dict(self.job)]
        if path == 'rpc/upsert_garmin_activity':
            if self.stale or data['p_attempts'] != self.job['attempts']:
                return False
            row = data['p_activity']
            key = (row['user_id'], row['provider'], row['provider_activity_id'])
            self.activities[key] = row
            return True
        if path == 'provider_activities':
            return [{'provider_activity_id': row['provider_activity_id'],
                     'source_updated_at': row.get('source_updated_at'),
                     'detail_imported_at': '2026-01-01T00:00:00Z'}
                    for row in self.activities.values()]
        if path == 'rpc/count_provider_activities':
            return sum(row['connection_id'] == data['p_connection_id']
                       for row in self.activities.values())
        if path == 'sync_jobs':
            if (params.get('attempts') != f"eq.{self.job['attempts']}"
                    or params.get('state') != 'eq.running'):
                return []
            self.job.update(data)
            return [dict(self.job)] if prefer else None
        if path == 'provider_connections':
            return None
        raise AssertionError(path)


class FakeSessions:
    def __init__(self):
        self.users = []
        self.distance = 1000

    def for_user(self, user_id):
        self.users.append(user_id)
        return self

    def get_activities_by_date(self, _start, _end):
        return [{'activityId': 123, 'activityType': {'typeKey': 'running'},
                 'distance': self.distance, 'duration': 300,
                 'startTimeGMT': '2026-01-01 10:00:00'}]

    def get_activity(self, _activity_id):
        return {'elevationGain': 100, 'averageHR': 150}

    def get_activity_splits(self, _activity_id):
        return {'lapDTOs': [{'distance': 1000, 'duration': 300}]}

    def persist(self, _user_id, _client):
        pass


def test_activity_sync_is_newest_first_resumable_and_idempotent():
    user_id = uuid.uuid4()
    today = date(2026, 1, 14)
    job = {'id': str(uuid.uuid4()), 'connection_id': str(uuid.uuid4()),
           'user_id': str(user_id), 'state': 'queued',
           'cursor_date': today.isoformat(), 'oldest_date': (today - timedelta(days=13)).isoformat(),
           'activity_count': 0, 'attempts': 0, 'failure_count': 0}
    database = FakeDatabase(job)
    sessions = FakeSessions()
    worker = ActivitySyncWorker(database, sessions)
    assert worker.run_once()
    assert job['cursor_date'] == '2026-01-07'
    assert job['oldest_synchronized_date'] == '2026-01-08'
    assert worker.run_once()
    assert job['state'] == 'succeeded'
    assert job['cursor_date'] is None
    assert job['activity_count'] == 1
    assert len(database.activities) == 1
    assert sessions.users == [user_id, user_id]
    assert not worker.run_once()


def test_replayed_window_does_not_inflate_activity_count():
    user_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    job = {'id': str(uuid.uuid4()), 'connection_id': str(connection_id),
           'user_id': str(user_id), 'state': 'queued', 'cursor_date': '2026-01-07',
           'oldest_date': '2026-01-01', 'activity_count': 0, 'attempts': 0,
           'failure_count': 0}
    database = FakeDatabase(job)
    worker = ActivitySyncWorker(database, FakeSessions())
    assert worker.run_once()
    assert job['activity_count'] == 1
    job.update(state='queued', cursor_date='2026-01-07')
    assert worker.run_once()
    assert job['activity_count'] == 1
    assert len(database.activities) == 1


def test_incremental_sync_updates_provider_corrections_without_duplicates():
    user_id = uuid.uuid4()
    job = {'id': str(uuid.uuid4()), 'connection_id': str(uuid.uuid4()),
           'user_id': str(user_id), 'state': 'queued', 'mode': 'incremental',
           'cursor_date': '2026-01-07', 'oldest_date': '2026-01-01',
           'activity_count': 0, 'attempts': 0, 'failure_count': 0}
    database = FakeDatabase(job)
    sessions = FakeSessions()
    worker = ActivitySyncWorker(database, sessions)
    assert worker.run_once()
    sessions.distance = 1200
    job.update(state='queued', cursor_date='2026-01-07')
    assert worker.run_once()
    assert len(database.activities) == 1
    assert next(iter(database.activities.values()))['distance_meters'] == 1200
    assert job['activity_count'] == 1


def test_normalization_requires_stable_provider_id():
    try:
        normalize_activity(uuid.uuid4(), uuid.uuid4(), {})
    except ValueError as error:
        assert 'activityId' in str(error)
    else:
        raise AssertionError('missing provider ID was accepted')


def test_activity_detail_and_segments_are_normalized_without_raw_payload():
    activity = normalize_activity(uuid.uuid4(), uuid.uuid4(), {
        'activityId': 123, 'activityType': {'typeKey': 'trail_running'},
        'startTimeGMT': '2026-01-01 10:00:00', 'distance': 1000}, {
        'averageHR': 151, 'elevationGain': 72, 'avgPower': 235,
        'timeZoneId': 'Europe/Paris', 'updateDate': '2026-01-02 10:00:00'})
    assert activity['activity_type'] == 'trail_running'
    assert activity['distance_meters'] == 1000
    assert activity['average_heart_rate'] == 151
    assert activity['elevation_gain_meters'] == 72
    assert activity['average_power_watts'] == 235
    assert activity['source_updated_at'] == '2026-01-02 10:00:00Z'
    assert 'raw' not in activity
    segments = normalize_segments({'lapDTOs': [{'distance': 1000, 'duration': 300}],
                                   'splitDTOs': [{'averageHR': 151}]})
    assert [(item['segment_type'], item['segment_index']) for item in segments] == [
        ('lap', 0), ('split', 0)]


def test_stale_worker_write_is_ignored_and_does_not_advance_cursor():
    user_id = uuid.uuid4()
    job = {'id': str(uuid.uuid4()), 'connection_id': str(uuid.uuid4()),
           'user_id': str(user_id), 'state': 'queued',
           'cursor_date': '2026-01-07', 'oldest_date': '2026-01-01',
           'activity_count': 0, 'attempts': 0, 'failure_count': 0}
    database = FakeDatabase(job)
    database.stale = True
    worker = ActivitySyncWorker(database, FakeSessions())
    assert worker.run_once()
    assert job['state'] == 'running'
    assert job['cursor_date'] == '2026-01-07'
    assert not database.activities


def test_name_effort_and_calories_come_from_data_already_fetched():
    activity = normalize_activity(uuid.uuid4(), uuid.uuid4(), {
        'activityId': 9, 'activityName': '  Seuil 3 x 10\'  ', 'calories': 640.0,
        'activityType': {'typeKey': 'running'}}, {
        'summaryDTO': {'directWorkoutRpe': 80, 'directWorkoutFeel': 75}})
    assert activity['activity_name'] == "Seuil 3 x 10'"
    assert activity['calories'] == 640.0
    assert activity['workout_rpe'] == 8.0
    assert activity['workout_feel'] == 75.0
    bare = normalize_activity(uuid.uuid4(), uuid.uuid4(), {
        'activityId': 10, 'activityName': '  ', 'directWorkoutRpe': 0})
    assert bare['activity_name'] is None
    assert bare['workout_rpe'] is None and bare['workout_feel'] is None
    assert bare['calories'] is None


class TrackDatabase:
    def __init__(self, job, activities):
        self.job = job
        self.pending = list(activities)
        self.tracks = {}
        self.stale = False
        self.since = None

    def request(self, method, path, *, params=None, data=None, prefer=None):
        if path == 'rpc/claim_sync_job':
            if self.job['state'] != 'queued':
                return []
            self.job['state'] = 'running'
            self.job['attempts'] += 1
            return [dict(self.job)]
        if path == 'rpc/list_activities_missing_tracks':
            self.since = data['p_since']
            remaining = [item for item in self.pending if item['id'] not in self.tracks]
            return remaining[:data['p_limit']]
        if path == 'rpc/upsert_activity_track':
            if self.stale:
                return False
            self.tracks[data['p_activity_id']] = data['p_track']
            return True
        if path == 'sync_jobs':
            if (params.get('attempts') != f"eq.{self.job['attempts']}"
                    or params.get('state') != 'eq.running'):
                return []
            self.job.update(data)
            return [dict(self.job)] if prefer else None
        raise AssertionError(path)


class TrackSessions:
    def __init__(self, failures=()):
        self.requests = []
        self.failures = failures

    def for_user(self, _user_id):
        return self

    def persist(self, _user_id, _client):
        pass

    def get_activity_details(self, activity_id, maxchart, maxpoly):
        self.requests.append((activity_id, maxchart, maxpoly))
        failure = self.failures.get(activity_id) if isinstance(self.failures, dict) else None
        if failure:
            raise failure
        return {'metricDescriptors': [{'metricsIndex': 0, 'key': 'directHeartRate'},
                                      {'metricsIndex': 1, 'key': 'sumElapsedDuration'}],
                'activityDetailMetrics': [{'metrics': [140, 0.0]}, {'metrics': [142, 1.0]}]}


def track_job():
    return {'id': str(uuid.uuid4()), 'connection_id': str(uuid.uuid4()),
            'user_id': str(uuid.uuid4()), 'state': 'queued', 'mode': 'incremental',
            'phase': 'tracks', 'cursor_date': None, 'oldest_date': '2026-01-01',
            'attempts': 0, 'failure_count': 0}


def activities(count):
    return [{'id': f'activity-{index}', 'provider_activity_id': str(1000 + index),
             'source_updated_at': None} for index in range(count)]


def test_tracks_job_imports_small_batches_then_succeeds():
    job = track_job()
    database = TrackDatabase(job, activities(7))
    sessions = TrackSessions()
    worker = ActivitySyncWorker(database, sessions)
    assert worker.run_once()
    assert job['state'] == 'queued' and len(database.tracks) == 5
    assert all(request[1:] == (600, 1200) for request in sessions.requests)
    job['next_attempt_at'] = None
    job['state'] = 'queued'
    assert worker.run_once()
    assert job['state'] == 'succeeded' and len(database.tracks) == 7
    assert job['last_success_at']
    assert all(track['status'] == 'available' for track in database.tracks.values())


def test_a_single_failing_activity_is_marked_unavailable_without_blocking_others():
    job = track_job()
    database = TrackDatabase(job, activities(3))
    sessions = TrackSessions({'1001': RuntimeError('not found')})
    worker = ActivitySyncWorker(database, sessions)
    assert worker.run_once()
    assert database.tracks['activity-1']['status'] == 'unavailable'
    assert database.tracks['activity-0']['status'] == 'available'
    assert database.tracks['activity-2']['status'] == 'available'
    assert job['state'] == 'succeeded'


def test_rate_limits_and_total_failures_retry_without_marking_activities():
    job = track_job()
    database = TrackDatabase(job, activities(4))
    failures = {str(1000 + index): GarminConnectTooManyRequestsError('429')
                for index in range(4)}
    worker = ActivitySyncWorker(database, TrackSessions(failures))
    assert worker.run_once()
    assert not database.tracks
    assert job['state'] == 'queued' and job['failure_count'] == 1

    job = track_job()
    database = TrackDatabase(job, activities(4))
    failures = {str(1000 + index): RuntimeError('500') for index in range(4)}
    worker = ActivitySyncWorker(database, TrackSessions(failures))
    assert worker.run_once()
    assert not database.tracks
    assert job['state'] == 'queued' and job['last_error_code'] == 'provider_unavailable'


def test_stale_track_write_does_not_advance_the_job():
    job = track_job()
    database = TrackDatabase(job, activities(2))
    database.stale = True
    worker = ActivitySyncWorker(database, TrackSessions())
    assert worker.run_once()
    assert job['state'] == 'running'
    assert not database.tracks


def test_track_window_is_configurable_and_bounded(monkeypatch):
    monkeypatch.delenv('STRIDE_TRACK_HISTORY_DAYS', raising=False)
    assert track_history_days() == 90
    monkeypatch.setenv('STRIDE_TRACK_HISTORY_DAYS', '30')
    assert track_history_days() == 30
    monkeypatch.setenv('STRIDE_TRACK_HISTORY_DAYS', '0')
    assert track_history_days() == 1
    monkeypatch.setenv('STRIDE_TRACK_HISTORY_DAYS', '99999')
    assert track_history_days() == 3650
    monkeypatch.setenv('STRIDE_TRACK_HISTORY_DAYS', 'soon')
    assert track_history_days() == 90
