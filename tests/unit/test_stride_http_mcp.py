import uuid
from types import SimpleNamespace

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from garmin_mcp.stride_app import register_routes
from garmin_mcp.stride_mcp import UserScopedGarminProxy, read_only_tool


class Verifier:
    def __init__(self, user_id):
        self.user_id = user_id

    async def verify_token(self, token):
        if token != 'valid':
            return None
        return AccessToken(token=token, client_id=str(self.user_id),
                           subject=str(self.user_id), scopes=['stride:garmin'])


class FakeConnectionRepository:
    def __init__(self):
        self.users = []

    def get(self, user_id):
        self.users.append(user_id)
        return {'status': 'connected'}


def test_http_requires_valid_supabase_identity():
    user_id = uuid.uuid4()
    connections = FakeConnectionRepository()
    components = SimpleNamespace(
        service=SimpleNamespace(connections=connections), database=None)
    app = FastMCP('test', token_verifier=Verifier(user_id),
                  auth=AuthSettings(issuer_url='https://example.supabase.co', resource_server_url=None),
                  stateless_http=True)
    register_routes(app, components)
    with TestClient(app.streamable_http_app()) as client:
        assert client.get('/v1/connections/garmin').status_code == 401
        assert client.get('/v1/connections/garmin', headers={
            'Authorization': 'Bearer invalid'}).status_code == 401
        response = client.get('/v1/connections/garmin', headers={
            'Authorization': 'Bearer valid'})
    assert response.status_code == 200
    assert response.json() == {'status': 'connected'}
    assert connections.users == [user_id]


def test_http_connection_start_keeps_credentials_out_of_response():
    user_id = uuid.uuid4()
    calls = []

    class Database:
        def request(self, method, path, *, data=None):
            assert (method, path) == ('POST', 'rpc/allow_auth_attempt')
            return True

    class Service:
        def start(self, actual_user, email, password):
            calls.append((actual_user, email, password))
            return {'status': 'connected'}

    components = SimpleNamespace(service=Service(), database=Database())
    app = FastMCP('test', token_verifier=Verifier(user_id),
                  auth=AuthSettings(issuer_url='https://example.supabase.co', resource_server_url=None),
                  stateless_http=True)
    register_routes(app, components)
    with TestClient(app.streamable_http_app()) as client:
        response = client.post('/v1/connections/garmin/start',
                               headers={'Authorization': 'Bearer valid'},
                               json={'email': 'runner@example.com', 'password': 'very-secret'})
    assert response.status_code == 200
    assert response.json() == {'status': 'connected'}
    assert 'very-secret' not in response.text
    assert calls == [(user_id, 'runner@example.com', 'very-secret')]


class FakeSessions:
    def __init__(self):
        self.users = []

    def for_user(self, user_id):
        self.users.append(user_id)
        return SimpleNamespace(get_stats=lambda _date: {'owner': str(user_id)})

    def persist(self, user_id, _client):
        self.users.append(user_id)


def test_mcp_proxy_isolates_authenticated_users(monkeypatch):
    from garmin_mcp.stride_mcp import get_access_token
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    current = {'id': user_a}
    monkeypatch.setattr('garmin_mcp.stride_mcp.get_access_token',
                        lambda: SimpleNamespace(subject=str(current['id'])))
    sessions = FakeSessions()
    proxy = UserScopedGarminProxy(sessions)
    assert proxy.get_stats('2026-01-01')['owner'] == str(user_a)
    current['id'] = user_b
    assert proxy.get_stats('2026-01-01')['owner'] == str(user_b)
    assert sessions.users == [user_a, user_a, user_b, user_b]


def test_stride_read_only_gate_rejects_mutations():
    assert read_only_tool('get_activities')
    assert not read_only_tool('delete_activity')
    assert not read_only_tool('upload_workout')


def test_mcp_tool_uses_authenticated_user_context():
    user_id = uuid.uuid4()
    sessions = FakeSessions()
    proxy = UserScopedGarminProxy(sessions)
    app = FastMCP('test', host='127.0.0.1', token_verifier=Verifier(user_id),
                  auth=AuthSettings(issuer_url='https://example.supabase.co', resource_server_url=None),
                  stateless_http=True, json_response=True)

    @app.tool()
    def get_stats(date: str) -> dict:
        return proxy.get_stats(date)

    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': 'get_stats', 'arguments': {'date': '2026-01-01'}}}
    headers = {'Authorization': 'Bearer valid',
               'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': '2025-06-18'}
    with TestClient(app.streamable_http_app(), base_url='http://127.0.0.1:8000') as client:
        assert client.post('/mcp', json=payload).status_code == 401
        response = client.post('/mcp', json=payload, headers=headers)
    assert response.status_code == 200
    assert str(user_id) in response.text
    assert sessions.users == [user_id, user_id]
