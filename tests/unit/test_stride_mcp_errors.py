import asyncio
import inspect
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from garminconnect import GarminConnectConnectionError, GarminConnectTooManyRequestsError
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolRequest
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

import garmin_mcp
from garmin_mcp import _ToolFilter, register_all_tools
from garmin_mcp.stride_errors import PROVIDER_UNAVAILABLE
from garmin_mcp.stride_garmin import ConnectionError, GarminSessionProvider, LeaseBusyError
from garmin_mcp.stride_mcp import DEFAULT_TOOLS, StrideToolGuard, UserScopedGarminProxy


class HeldLeaseTokens:
    """Token store whose lease is held by a sync for the whole test."""

    def __init__(self):
        self.attempts = threading.Event()

    def acquire(self, _connection_id, _owner):
        self.attempts.set()
        return False


class Connections:
    def get(self, _user_id):
        return {'id': str(uuid.uuid4()), 'status': 'connected'}


class FakeSessions:
    def __init__(self, client):
        self.client = client
        self.lease_waits = []

    def for_user(self, _user_id, load_profile=False, lease_wait=90.0):
        self.lease_waits.append(lease_wait)
        return self.client

    def persist(self, _user_id, _client):
        pass


class Verifier:
    def __init__(self, user_id):
        self.user_id = user_id

    async def verify_token(self, token):
        if token != 'valid':
            return None
        return AccessToken(token=token, client_id='stride', subject=str(self.user_id),
                           scopes=['stride:garmin'])


@pytest.fixture
def stride_user(monkeypatch):
    user_id = uuid.uuid4()
    monkeypatch.setattr('garmin_mcp.stride_mcp.get_access_token',
                        lambda: SimpleNamespace(subject=str(user_id)))
    return user_id


def _register(app, sessions):
    proxy = UserScopedGarminProxy(sessions)
    for module in (garmin_mcp.activity_management, garmin_mcp.health_wellness,
                   garmin_mcp.training):
        module.configure(proxy)
    register_all_tools(_ToolFilter(StrideToolGuard(app, 65_536), DEFAULT_TOOLS, set()))
    return app


def _call(app, name, arguments):
    request = CallToolRequest(method='tools/call', params={'name': name, 'arguments': arguments})
    return asyncio.run(app._mcp_server.request_handlers[CallToolRequest](request)).root


def test_mcp_call_returns_busy_quickly_while_sync_holds_lease(stride_user, monkeypatch):
    monkeypatch.setattr('garmin_mcp.stride_mcp.MCP_LEASE_WAIT_SECONDS', 0.3)
    sessions = GarminSessionProvider(HeldLeaseTokens(), connections=Connections())
    app = _register(FastMCP('test'), sessions)

    started = time.monotonic()
    result = _call(app, 'get_rhr_day', {'date': '2026-10-14'})

    assert time.monotonic() - started < 2
    assert result.isError
    assert result.content[0].text.startswith('provider_busy: ')


def test_sync_and_rest_keep_the_long_lease_wait():
    signature = inspect.signature(GarminSessionProvider.for_user)
    assert signature.parameters['lease_wait'].default == 90.0

    sessions = GarminSessionProvider(HeldLeaseTokens(), connections=Connections())
    with pytest.raises(LeaseBusyError) as error:
        sessions.for_user(uuid.uuid4(), lease_wait=0)

    assert isinstance(error.value, ConnectionError)
    assert (error.value.code, error.value.status) == (PROVIDER_UNAVAILABLE, 503)


def test_mcp_proxy_uses_the_short_lease_wait(stride_user):
    sessions = FakeSessions(SimpleNamespace(get_rhr_day=lambda _date: {'restingHeartRate': 48}))
    app = _register(FastMCP('test'), sessions)

    result = _call(app, 'get_rhr_day', {'date': '2026-10-14'})

    assert not result.isError
    assert sessions.lease_waits == [3.0]


def test_waiting_tool_call_does_not_block_other_requests(monkeypatch):
    monkeypatch.setattr('garmin_mcp.stride_mcp.MCP_LEASE_WAIT_SECONDS', 1.5)
    user_id = uuid.uuid4()
    tokens = HeldLeaseTokens()
    sessions = GarminSessionProvider(tokens, connections=Connections())
    app = FastMCP('test', host='127.0.0.1', token_verifier=Verifier(user_id),
                  auth=AuthSettings(issuer_url='https://example.supabase.co',
                                    resource_server_url=None),
                  stateless_http=True, json_response=True)
    _register(app, sessions)

    @app.custom_route('/ping', methods=['GET'])
    async def ping(_request):
        return PlainTextResponse('pong')

    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': 'get_rhr_day', 'arguments': {'date': '2026-10-14'}}}
    headers = {'Authorization': 'Bearer valid',
               'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': '2025-06-18'}
    responses = {}
    with TestClient(app.streamable_http_app(), base_url='http://127.0.0.1:8000') as client:
        caller = threading.Thread(target=lambda: responses.update(
            tool=client.post('/mcp', json=payload, headers=headers)))
        caller.start()
        assert tokens.attempts.wait(5)
        started = time.monotonic()
        pong = client.get('/ping')
        ping_seconds = time.monotonic() - started
        still_waiting = caller.is_alive()
        caller.join(5)

    assert pong.text == 'pong'
    assert ping_seconds < 1
    assert still_waiting
    body = responses['tool'].json()
    assert body['result']['isError']
    assert body['result']['content'][0]['text'].startswith('provider_busy: ')


def _raise(error):
    def call(*_args, **_kwargs):
        raise error
    return call


@pytest.mark.parametrize('error,code', [
    (GarminConnectTooManyRequestsError('429'), 'rate_limited'),
    (GarminConnectConnectionError('API Error 500'), 'provider_unavailable'),
    (ConnectionError('reconnect_required', 409), 'reconnect_required'),
])
def test_garmin_failures_carry_stable_codes(stride_user, error, code):
    app = _register(FastMCP('test'), FakeSessions(SimpleNamespace(get_rhr_day=_raise(error))))

    result = _call(app, 'get_rhr_day', {'date': '2026-10-14'})

    assert result.isError
    assert result.structuredContent == {'error': {'code': code}}
    assert result.content[0].text.startswith(f'{code}: ')
    assert str(error) not in result.content[0].text.removeprefix(f'{code}: ')


def test_missing_user_is_unauthorized(monkeypatch):
    monkeypatch.setattr('garmin_mcp.stride_mcp.get_access_token', lambda: None)
    app = _register(FastMCP('test'), FakeSessions(SimpleNamespace()))

    result = _call(app, 'get_rhr_day', {'date': '2026-10-14'})

    assert result.isError
    assert result.content[0].text.startswith('unauthorized: ')


def test_not_found_stays_a_no_data_answer(stride_user):
    missing = GarminConnectConnectionError('API Error 404 - not found')
    app = _register(FastMCP('test'), FakeSessions(SimpleNamespace(get_rhr_day=_raise(missing))))

    result = _call(app, 'get_rhr_day', {'date': '2026-10-14'})

    assert not result.isError


def test_later_successful_call_clears_an_earlier_failure(stride_user):
    calls = []

    def flaky(_date):
        calls.append(_date)
        if len(calls) == 1:
            raise GarminConnectConnectionError('API Error 500')
        return {'restingHeartRate': 48}

    proxy = UserScopedGarminProxy(FakeSessions(SimpleNamespace(get_rhr_day=flaky)))
    app = FastMCP('test')

    @StrideToolGuard(app, 65_536).tool()
    async def get_rhr_day(date: str) -> str:
        try:
            return str(proxy.get_rhr_day(date))
        except Exception:
            return str(proxy.get_rhr_day(date))

    result = _call(app, 'get_rhr_day', {'date': '2026-10-14'})

    assert not result.isError
    assert '48' in result.content[0].text
