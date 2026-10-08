import uuid
from datetime import date, timedelta

from garmin_mcp.stride_sync import ActivitySyncWorker, normalize_activity, normalize_segments


class FakeDatabase:
    def __init__(self, job):
        self.job = job
        self.activities = {}

    def request(self, method, path, *, params=None, data=None, prefer=None):
        if path == 'rpc/claim_sync_job':
            if self.job['state'] != 'queued':
                return []
            self.job['state'] = 'running'
            self.job['attempts'] += 1
            return [dict(self.job)]
        if path == 'rpc/upsert_garmin_activity':
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
            self.job.update(data)
            return None
        if path == 'provider_connections':
            return None
        raise AssertionError(path)


class FakeSessions:
    def __init__(self):
        self.users = []

    def for_user(self, user_id):
        self.users.append(user_id)
        return self

    def get_activities_by_date(self, _start, _end):
        return [{'activityId': 123, 'activityType': {'typeKey': 'running'},
                 'distance': 1000, 'duration': 300,
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
