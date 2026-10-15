import uuid

from garmin_mcp.stride_service import ConnectionRepository


def test_category_states_reflect_persisted_jobs():
    user_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    stored_activities = 3
    incremental = []
    jobs = {
        'activities': {'state': 'queued', 'oldest_synchronized_date': '2024-01-08',
                       'activity_count': 2},
        'recovery': {'state': 'running', 'oldest_synchronized_date': None,
                     'activity_count': 0},
        'fitness': {'state': 'succeeded', 'oldest_synchronized_date': '2024-01-01',
                    'activity_count': 0},
        'thresholds': {'state': 'failed', 'oldest_synchronized_date': None,
                       'activity_count': 0},
    }

    class Database:
        def request(self, method, path, *, params=None, data=None):
            if path == 'rpc/count_provider_activities':
                assert method == 'POST'
                assert data == {'p_connection_id': str(connection_id)}
                return stored_activities
            assert method == 'GET'
            if path == 'provider_connections':
                if params.get('status') == 'neq.disconnected':
                    return [{'id': str(connection_id), 'status': 'connected',
                             'manual_sync_available_at': '2024-01-15T00:05:00+00:00'}]
                raise AssertionError(params)
            if path == 'sync_jobs':
                assert params['connection_id'] == f'eq.{connection_id}'
                if params['mode'] == 'eq.incremental':
                    assert params['phase'] == 'in.(activities,recovery,fitness,tracks)'
                    return incremental
                assert params['mode'] == 'eq.historical'
                phase = params['phase'][3:]
                return [dict(jobs[phase], phase=phase, last_success_at=None,
                             last_error_code=None, updated_at='2024-01-15T00:00:00Z')]
            raise AssertionError(path)

    repository = ConnectionRepository(Database())
    result = repository.get_sync(user_id)
    assert result['categories']['activities']['state'] == 'partial'
    assert result['categories']['activities']['activity_count'] == 3
    assert result['categories']['recovery']['state'] == 'running'
    assert result['categories']['fitness']['state'] == 'complete'
    assert result['categories']['thresholds']['state'] == 'failed'
    assert 'activity_count' not in result['categories']['thresholds']
    assert result['categories']['patterns']['state'] == 'partial'
    jobs['activities']['state'] = 'succeeded'
    assert repository.get_sync(user_id)['categories']['patterns']['state'] == 'complete'
    stored_activities = 4
    assert repository.get_sync(user_id)['categories']['activities']['activity_count'] == 4
    assert result['incremental'] is None
    assert result['incremental_phases'] == {}
    assert result['manual_sync_available_at'] == '2024-01-15T00:05:00+00:00'


def test_incremental_phases_keep_the_latest_job_per_phase():
    connection_id = uuid.uuid4()
    rows = [
        {'phase': 'tracks', 'state': 'queued', 'last_error_code': None,
         'last_success_at': None, 'updated_at': '2024-01-15T00:03:00Z'},
        {'phase': 'activities', 'state': 'running', 'last_error_code': None,
         'last_success_at': None, 'updated_at': '2024-01-15T00:02:00Z'},
        {'phase': 'activities', 'state': 'succeeded', 'last_error_code': None,
         'last_success_at': '2024-01-14T23:00:00Z', 'updated_at': '2024-01-14T23:00:00Z'},
        {'phase': 'recovery', 'state': 'succeeded', 'last_error_code': None,
         'last_success_at': '2024-01-14T20:00:00Z', 'updated_at': '2024-01-14T20:00:00Z'},
    ]

    class Database:
        def request(self, method, path, *, params=None, data=None):
            if path == 'provider_connections':
                return [{'id': str(connection_id), 'status': 'connected'}]
            if path == 'sync_jobs' and params['mode'] == 'eq.incremental':
                return rows
            if path == 'rpc/count_provider_activities':
                return 0
            return []

    result = ConnectionRepository(Database()).get_sync(uuid.uuid4())
    assert result['incremental'] == rows[1]
    assert result['incremental_phases'] == {
        'tracks': rows[0], 'activities': rows[1], 'recovery': rows[3]}


def test_missing_jobs_return_pending_categories():
    class Database:
        def request(self, method, path, *, params=None, data=None):
            return []

    result = ConnectionRepository(Database()).get_sync(uuid.uuid4())
    assert {item['state'] for item in result['categories'].values()} == {'pending'}
