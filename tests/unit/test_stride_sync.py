import uuid
from datetime import date, timedelta

from garmin_mcp.stride_sync import ActivitySyncWorker, normalize_activity


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
        if path == 'provider_activities':
            for row in data:
                key = (row['user_id'], row['provider'], row['provider_activity_id'])
                self.activities[key] = row
            return None
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
    assert len(database.activities) == 1
    assert sessions.users == [user_id, user_id]
    assert not worker.run_once()


def test_normalization_requires_stable_provider_id():
    try:
        normalize_activity(uuid.uuid4(), uuid.uuid4(), {})
    except ValueError as error:
        assert 'activityId' in str(error)
    else:
        raise AssertionError('missing provider ID was accepted')
