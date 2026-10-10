import uuid

from garmin_mcp.stride_service import ConnectionRepository


def test_category_states_reflect_persisted_jobs():
    user_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    jobs = {
        'activities': {'state': 'queued', 'oldest_synchronized_date': '2024-01-08',
                       'activity_count': 2},
        'recovery': {'state': 'running', 'oldest_synchronized_date': None,
                     'activity_count': 0},
        'fitness': {'state': 'succeeded', 'oldest_synchronized_date': '2024-01-01',
                    'activity_count': 0},
    }

    class Database:
        def request(self, method, path, *, params):
            assert method == 'GET'
            if path == 'provider_connections':
                if params.get('status') == 'neq.disconnected':
                    return [{'id': str(connection_id), 'status': 'connected'}]
                raise AssertionError(params)
            if path == 'sync_jobs':
                assert params['connection_id'] == f'eq.{connection_id}'
                if params['mode'] == 'eq.incremental':
                    assert params['phase'] == 'in.(activities,recovery,fitness)'
                    return []
                assert params['mode'] == 'eq.historical'
                phase = params['phase'][3:]
                return [dict(jobs[phase], phase=phase, last_success_at=None,
                             last_error_code=None, updated_at='2024-01-15T00:00:00Z')]
            raise AssertionError(path)

    repository = ConnectionRepository(Database())
    result = repository.get_sync(user_id)
    assert result['categories']['activities']['state'] == 'partial'
    assert result['categories']['activities']['activity_count'] == 2
    assert result['categories']['recovery']['state'] == 'running'
    assert result['categories']['fitness']['state'] == 'complete'
    assert result['categories']['patterns']['state'] == 'partial'
    jobs['activities']['state'] = 'succeeded'
    assert repository.get_sync(user_id)['categories']['patterns']['state'] == 'complete'


def test_missing_jobs_return_pending_categories():
    class Database:
        def request(self, method, path, *, params):
            return []

    result = ConnectionRepository(Database()).get_sync(uuid.uuid4())
    assert {item['state'] for item in result['categories'].values()} == {'pending'}
