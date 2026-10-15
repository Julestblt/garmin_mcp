import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolRequest

import garmin_mcp
from garmin_mcp import _ToolFilter, register_all_tools
from garmin_mcp.stride_mcp import (DEFAULT_TOOLS, TRUNCATION_MARKER,
                                   StrideToolGuard, UserScopedGarminProxy, cap_response,
                                   read_only_tool, resolve_max_response_bytes)


class FakeGarmin:
    def __init__(self, calls, activities):
        self.calls = calls
        self.activities = activities

    def connectapi(self, url, params=None):
        self.calls.append((url, params))
        return self.activities

    def get_vo2max_trend(self, *args):
        self.calls.append(('vo2', args))
        return []


class FakeSessions:
    def __init__(self, client):
        self.client = client

    def for_user(self, _user_id, load_profile=False):
        return self.client

    def persist(self, _user_id, _client):
        pass


@pytest.fixture
def stride_user(monkeypatch):
    user_id = uuid.uuid4()
    monkeypatch.setattr('garmin_mcp.stride_mcp.get_access_token',
                        lambda: SimpleNamespace(subject=str(user_id)))
    return user_id


def _stride_app(client, max_bytes=65_536):
    """Register the Stride tool set the way main() does, on a fake Garmin client."""
    proxy = UserScopedGarminProxy(FakeSessions(client))
    for module in (garmin_mcp.activity_management, garmin_mcp.health_wellness,
                   garmin_mcp.training):
        module.configure(proxy)
    fastmcp = FastMCP('test')
    register_all_tools(_ToolFilter(StrideToolGuard(fastmcp, max_bytes), DEFAULT_TOOLS, set()))
    return fastmcp


def _call(app, name, arguments):
    request = CallToolRequest(method='tools/call', params={'name': name, 'arguments': arguments})
    return asyncio.run(app._mcp_server.request_handlers[CallToolRequest](request)).root


def _activity(activity_id):
    return {'activityId': activity_id, 'activityName': 'Run',
            'activityType': {'typeKey': 'running'}, 'startTimeLocal': '2026-10-05 07:00:00'}


def test_activities_by_date_rejects_range_over_14_days(stride_user):
    calls = []
    app = _stride_app(FakeGarmin(calls, [_activity(1)]))

    result = _call(app, 'get_activities_by_date',
                   {'start_date': '2026-10-01', 'end_date': '2026-10-15'})

    assert result.isError
    assert 'range_too_large' in result.content[0].text
    assert '15 days' in result.content[0].text
    assert calls == []


def test_activities_by_date_returns_range_of_14_days(stride_user):
    calls = []
    app = _stride_app(FakeGarmin(calls, [_activity(1), _activity(2)]))

    result = _call(app, 'get_activities_by_date',
                   {'start_date': '2026-10-01', 'end_date': '2026-10-14'})

    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert [a['id'] for a in payload['activities']] == [1, 2]
    assert payload['date_range'] == {'start': '2026-10-01', 'end': '2026-10-14'}
    url, params = calls[0]
    assert url == '/activitylist-service/activities/search/activities'
    assert (params['startDate'], params['endDate']) == ('2026-10-01', '2026-10-14')


@pytest.mark.parametrize('start_date,end_date,code', [
    ('2026-10-01', '2026-11-30', 'range_too_large'),
    ('2026-10-14', '2026-10-01', 'invalid_request'),
    ('01/10/2026', '2026-10-14', 'invalid_request'),
])
def test_every_exposed_range_tool_rejects_invalid_ranges(stride_user, start_date, end_date, code):
    calls = []
    app = _stride_app(FakeGarmin(calls, []))
    tools = asyncio.run(app.list_tools())
    ranged = sorted(t.name for t in tools
                    if {'start_date', 'end_date'} <= t.inputSchema['properties'].keys())

    assert ranged == ['get_activities_by_date', 'get_vo2max_trend']
    for name in ranged:
        result = _call(app, name, {'start_date': start_date, 'end_date': end_date})
        assert result.isError, name
        assert code in result.content[0].text, name
    assert calls == []


def test_exposed_tools_are_the_documented_read_only_set(stride_user):
    app = _stride_app(FakeGarmin([], []))
    names = {t.name for t in asyncio.run(app.list_tools())}

    assert names == DEFAULT_TOOLS
    assert all(read_only_tool(name) for name in names)


def test_large_response_is_truncated_with_marker(stride_user):
    activities = [_activity(i) for i in range(200)]
    app = _stride_app(FakeGarmin([], activities), max_bytes=4096)

    result = _call(app, 'get_activities_by_date',
                   {'start_date': '2026-10-01', 'end_date': '2026-10-14', 'page_size': 200})

    text = result.content[0].text
    assert not result.isError
    assert text.startswith(TRUNCATION_MARKER)
    header, body = text.split('\n', 1)
    assert 'incomplete' in header
    assert len(body.encode('utf-8')) <= 4096


def test_cap_response_keeps_small_results_and_utf8_boundaries():
    assert cap_response('{"ok": true}', 1024) == '{"ok": true}'
    assert cap_response({'structured': True}, 1) == {'structured': True}

    capped = cap_response('é' * 1000, 1025)
    header, body = capped.split('\n', 1)
    assert header.startswith(TRUNCATION_MARKER)
    assert body == 'é' * 512
    assert 'of 2000 bytes' in header


def test_max_response_bytes_is_configurable(monkeypatch):
    monkeypatch.delenv('STRIDE_MCP_MAX_RESPONSE_BYTES', raising=False)
    assert resolve_max_response_bytes() == 65_536
    monkeypatch.setenv('STRIDE_MCP_MAX_RESPONSE_BYTES', '32768')
    assert resolve_max_response_bytes() == 32_768
    monkeypatch.setenv('STRIDE_MCP_MAX_RESPONSE_BYTES', '10')
    with pytest.raises(ValueError):
        resolve_max_response_bytes()


def _run_stride_main(monkeypatch, enabled=None):
    monkeypatch.setenv('STRIDE_MODE', 'true')
    monkeypatch.setenv('GARMIN_MCP_TRANSPORT', 'streamable-http')
    monkeypatch.delenv('GARMIN_DISABLED_TOOLS', raising=False)
    monkeypatch.delenv('STRIDE_MCP_MAX_RESPONSE_BYTES', raising=False)
    if enabled is None:
        monkeypatch.delenv('GARMIN_ENABLED_TOOLS', raising=False)
    else:
        monkeypatch.setenv('GARMIN_ENABLED_TOOLS', enabled)
    monkeypatch.setattr('garmin_mcp.stride_logging.configure_logging', lambda: None)
    monkeypatch.setattr('garmin_mcp.stride_app.create_stride_components', lambda: SimpleNamespace(
        sessions=FakeSessions(None), verifier=SimpleNamespace(verify_token=None),
        supabase_url='https://example.supabase.co'))
    captured = []
    monkeypatch.setattr(FastMCP, 'run', lambda self, **_kwargs: captured.append(
        {t.name for t in asyncio.run(self.list_tools())}))
    garmin_mcp.main()
    return captured[0]


def test_stride_main_exposes_default_set(monkeypatch):
    assert _run_stride_main(monkeypatch) == DEFAULT_TOOLS


def test_stride_main_rejects_tools_outside_documented_set(monkeypatch):
    with pytest.raises(ValueError, match='documented set: get_devices'):
        _run_stride_main(monkeypatch, 'get_activities,get_devices')


def test_stride_main_rejects_write_tools(monkeypatch):
    with pytest.raises(ValueError, match='read-only'):
        _run_stride_main(monkeypatch, 'get_activities,delete_activity')

