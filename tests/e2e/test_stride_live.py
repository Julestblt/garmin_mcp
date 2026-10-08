import getpass
import json
import os
import subprocess
import time
from datetime import datetime, timezone

import httpx
import pytest


pytestmark = [pytest.mark.e2e, pytest.mark.skipif(
    os.getenv('STRIDE_E2E_LIVE') != '1', reason='live Stride E2E is opt-in')]


def _request(client: httpx.Client, method: str, path: str, token: str,
             body: dict[str, str] | None = None) -> httpx.Response:
    return client.request(method, path, headers={'Authorization': f'Bearer {token}'},
                          json=body)


def _connect(client: httpx.Client, token: str, email: str, password: str) -> None:
    response = _request(client, 'POST', '/v1/connections/garmin/start', token,
                        {'email': email, 'password': password,
                         'history_start_date': datetime.now(timezone.utc).date().isoformat()})
    assert response.status_code == 200, response.status_code
    result = response.json()
    if result['status'] == 'mfa_required':
        otp = os.getenv('GARMIN_TEST_OTP') or getpass.getpass('Garmin test account OTP: ')
        response = _request(client, 'POST', '/v1/connections/garmin/mfa', token,
                            {'challenge_id': result['challenge_id'], 'otp': otp})
        assert response.status_code == 200, response.status_code
        result = response.json()
    assert result == {'status': 'connected'}


def _mcp_stats(client: httpx.Client, token: str) -> httpx.Response:
    return client.post('/mcp', headers={
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json, text/event-stream',
        'MCP-Protocol-Version': '2025-06-18',
    }, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
             'params': {'name': 'get_stats', 'arguments': {
             'date': datetime.now(timezone.utc).date().isoformat()}}})


def _tool_failed(response: httpx.Response) -> bool:
    if response.status_code != 200:
        return True
    payload = response.text
    if response.headers.get('content-type', '').startswith('text/event-stream'):
        payload = next((line[6:] for line in payload.splitlines()
                        if line.startswith('data: ')), '{}')
    body = json.loads(payload)
    return bool(body.get('error') or body.get('result', {}).get('isError'))


def test_live_connection_survives_restart_and_remains_tenant_isolated():
    required = ('STRIDE_E2E_BASE_URL', 'STRIDE_TEST_USER_A_TOKEN',
                'STRIDE_TEST_USER_B_TOKEN', 'GARMIN_TEST_EMAIL', 'GARMIN_TEST_PASSWORD')
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.skip('Missing live E2E configuration: ' + ', '.join(missing))
    token_a = os.environ['STRIDE_TEST_USER_A_TOKEN']
    token_b = os.environ['STRIDE_TEST_USER_B_TOKEN']
    with httpx.Client(base_url=os.environ['STRIDE_E2E_BASE_URL'], timeout=30) as client:
        try:
            _connect(client, token_a, os.environ['GARMIN_TEST_EMAIL'],
                     os.environ['GARMIN_TEST_PASSWORD'])
            assert _request(client, 'GET', '/v1/connections/garmin', token_a).json()[
                'status'] == 'connected'
            assert _request(client, 'GET', '/v1/connections/garmin', token_b).json()[
                'status'] != 'connected'
            subprocess.run(['docker', 'compose', 'restart', 'garmin-mcp'],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(60):
                try:
                    if client.get('/healthz').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                pytest.fail('API did not become ready after restart')
            assert _request(client, 'GET', '/v1/connections/garmin', token_a).json()[
                'status'] == 'connected'
            own_tool = _mcp_stats(client, token_a)
            assert not _tool_failed(own_tool)
            other_tool = _mcp_stats(client, token_b)
            assert _tool_failed(other_tool)
            assert _request(client, 'DELETE', '/v1/connections/garmin', token_a).status_code == 204
            assert _request(client, 'GET', '/v1/connections/garmin', token_a).json()[
                'status'] == 'disconnected'
            _connect(client, token_a, os.environ['GARMIN_TEST_EMAIL'],
                     os.environ['GARMIN_TEST_PASSWORD'])
        finally:
            _request(client, 'DELETE', '/v1/connections/garmin', token_a)
