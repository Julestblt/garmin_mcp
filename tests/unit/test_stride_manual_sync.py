import uuid
from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from garmin_mcp.stride_app import register_routes
from garmin_mcp.stride_garmin import ConnectionError
from garmin_mcp.stride_service import StrideConnectionService, SyncThrottledError


class Verifier:
    def __init__(self, user_id):
        self.user_id = user_id

    async def verify_token(self, token):
        if token != 'valid':
            return None
        return AccessToken(token=token, client_id=str(self.user_id),
                           subject=str(self.user_id), scopes=['stride:garmin'])


class Connections:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []
        self.database = self

    def request(self, method, path, *, params=None, data=None):
        self.calls.append((method, path, data))
        return self.outcome

    def get_sync(self, user_id):
        return {'connection_id': 'connection', 'categories': {},
                'manual_sync_available_at': self.outcome.get('available_at')}


def _service(outcome):
    connections = Connections(outcome)
    return StrideConnectionService(garmin=None, connections=connections), connections


def _client(service, user_id):
    app = FastMCP('test', token_verifier=Verifier(user_id),
                  auth=AuthSettings(issuer_url='https://example.supabase.co',
                                    resource_server_url=None),
                  stateless_http=True)
    register_routes(app, SimpleNamespace(service=service, database=None))
    return TestClient(app.streamable_http_app())


@pytest.mark.parametrize('result', ['queued', 'already_running'])
def test_request_sync_returns_status_for_queued_and_running_syncs(result):
    user_id = uuid.uuid4()
    service, connections = _service({'result': result,
                                     'available_at': '2026-10-16T12:05:00+00:00'})

    status = service.request_sync(user_id)

    assert connections.calls == [('POST', 'rpc/request_manual_sync',
                                  {'p_user_id': str(user_id)})]
    assert status['request'] == result
    assert status['manual_sync_available_at'] == '2026-10-16T12:05:00+00:00'


@pytest.mark.parametrize(('result', 'code'), [('reconnect_required', 'reconnect_required'),
                                              ('not_connected', 'disconnected')])
def test_request_sync_rejects_connections_that_cannot_sync(result, code):
    service, _ = _service({'result': result})

    with pytest.raises(ConnectionError) as error:
        service.request_sync(uuid.uuid4())

    assert (error.value.code, error.value.status) == (code, 409)


def test_request_sync_reports_throttling_with_the_next_allowed_time():
    service, _ = _service({'result': 'throttled', 'available_at': '2026-10-16T12:05:00+00:00',
                           'retry_after_seconds': 120})

    with pytest.raises(SyncThrottledError) as error:
        service.request_sync(uuid.uuid4())

    assert (error.value.code, error.value.status) == ('sync_throttled', 429)
    assert error.value.retry_after_seconds == 120


def test_http_sync_request_requires_identity_and_returns_the_status():
    user_id = uuid.uuid4()
    service, _ = _service({'result': 'queued', 'available_at': '2026-10-16T12:05:00+00:00'})

    with _client(service, user_id) as client:
        assert client.post('/v1/connections/garmin/sync').status_code == 401
        response = client.post('/v1/connections/garmin/sync',
                               headers={'Authorization': 'Bearer valid'})

    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['request'] == 'queued'
    assert response.json()['connection_id'] == 'connection'


def test_http_sync_request_maps_throttling_and_reconnection_errors():
    user_id = uuid.uuid4()
    throttled, _ = _service({'result': 'throttled', 'available_at': '2026-10-16T12:05:00+00:00',
                             'retry_after_seconds': 42})
    reconnect, _ = _service({'result': 'reconnect_required'})

    with _client(throttled, user_id) as client:
        limited = client.post('/v1/connections/garmin/sync',
                              headers={'Authorization': 'Bearer valid'})
    with _client(reconnect, user_id) as client:
        rejected = client.post('/v1/connections/garmin/sync',
                               headers={'Authorization': 'Bearer valid'})

    assert limited.status_code == 429
    assert limited.headers['retry-after'] == '42'
    assert limited.json() == {'error': {'code': 'sync_throttled'},
                              'available_at': '2026-10-16T12:05:00+00:00'}
    assert rejected.status_code == 409
    assert rejected.json() == {'error': {'code': 'reconnect_required'}}
