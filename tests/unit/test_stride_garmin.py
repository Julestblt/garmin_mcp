import uuid
from types import SimpleNamespace

import pytest
import requests
from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

from garmin_mcp.stride_garmin import (ConnectionError, GarminConnectionService,
                                      GarminSessionProvider, capture_mfa, restore_mfa)
from garmin_mcp.stride_storage import FileTokenStore


class FakeInternal:
    def __init__(self):
        self.token = 'fresh-token'
        self._mfa_flow = 'ios'
        self._mfa_method = 'email'
        self._mfa_session = requests.Session()
        self._mfa_session.cookies.set('sso', 'cookie-value', domain='sso.garmin.com')
        self._mfa_login_params = {'clientId': 'abc'}
        self._mfa_post_headers = {'Referer': 'https://sso.garmin.com'}
        self._mfa_service_url = 'https://sso.garmin.com/mobile'
        self.di_refresh_token = 'refresh'

    def dumps(self):
        return self.token

    def loads(self, token):
        self.token = token

    def dump(self, _directory):
        pass

    def _token_expires_soon(self):
        return True

    def _refresh_session(self):
        self.token = 'refreshed-' + self.token

    def connectapi(self, path):
        if path == '/userprofile-service/socialProfile':
            return {'displayName': 'Runner', 'fullName': 'Runner'}
        return {'userData': {'measurementSystem': 'metric'}}


class FakeGarmin:
    outcome = 'success'
    last_resumed = None

    def __init__(self, **_kwargs):
        self.client = FakeInternal()
        self.garmin_connect_user_settings_url = '/settings'

    def login(self, tokenstore=None):
        if tokenstore:
            self.client.token = 'refreshed-' + self.client.token
            return None, None
        if self.outcome == 'mfa':
            return 'needs_mfa', None
        if self.outcome == 'invalid':
            raise GarminConnectAuthenticationError('bad login')
        if self.outcome == 'rate_limited':
            raise GarminConnectTooManyRequestsError('retry later')
        return None, None

    def resume_login(self, _state, otp):
        FakeGarmin.last_resumed = self.client._mfa_session.cookies.get('sso')
        if otp != '123456':
            raise GarminConnectAuthenticationError('bad code')
        return None, None


class FakeChallenges:
    def __init__(self):
        self.items = {}

    def create(self, user_id, state):
        challenge_id = uuid.uuid4()
        self.items[challenge_id] = (user_id, state)
        return challenge_id

    def consume(self, user_id, challenge_id):
        item = self.items.get(challenge_id)
        if item and item[0] == user_id:
            del self.items[challenge_id]
            return SimpleNamespace(state=item[1])
        return None


@pytest.fixture
def service(tmp_path):
    FakeGarmin.outcome = 'success'
    tokens = FileTokenStore(tmp_path)
    sessions = GarminSessionProvider(tokens, client_factory=FakeGarmin)
    return GarminConnectionService(sessions, FakeChallenges(), client_factory=FakeGarmin)


def test_successful_login_and_tenant_isolation(service):
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    assert service.start(user_a, 'a@example.com', 'password') == {'status': 'connected'}
    assert service.sessions.tokens.load(user_a) == 'fresh-token'
    with pytest.raises(ConnectionError, match='reconnect_required'):
        service.sessions.for_user(user_b)
    service.sessions.for_user(user_a)
    assert service.sessions.tokens.load(user_a) == 'refreshed-fresh-token'
    assert service.sessions.tokens.load(user_b) is None


def test_mfa_continues_with_reconstructed_session(service):
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    FakeGarmin.outcome = 'mfa'
    result = service.start(user_a, 'a@example.com', 'password')
    challenge_id = uuid.UUID(result['challenge_id'])
    assert result['status'] == 'mfa_required'
    assert service.sessions.tokens.load(user_a) is None
    with pytest.raises(ConnectionError, match='challenge_expired'):
        service.mfa(user_b, challenge_id, '123456')
    assert service.mfa(user_a, challenge_id, '123456') == {'status': 'connected'}
    assert FakeGarmin.last_resumed == 'cookie-value'
    assert service.sessions.tokens.load(user_a) == 'fresh-token'
    with pytest.raises(ConnectionError, match='challenge_expired'):
        service.mfa(user_a, challenge_id, '123456')


def test_invalid_mfa_consumes_challenge(service):
    user_id = uuid.uuid4()
    FakeGarmin.outcome = 'mfa'
    challenge_id = uuid.UUID(service.start(user_id, 'a@example.com', 'password')['challenge_id'])
    with pytest.raises(ConnectionError, match='invalid_mfa'):
        service.mfa(user_id, challenge_id, 'wrong')
    assert service.sessions.tokens.load(user_id) is None
    with pytest.raises(ConnectionError, match='challenge_expired'):
        service.mfa(user_id, challenge_id, '123456')


@pytest.mark.parametrize(('outcome', 'code'), [
    ('invalid', 'invalid_credentials'), ('rate_limited', 'rate_limited'),
])
def test_login_error_mapping(service, outcome, code):
    FakeGarmin.outcome = outcome
    with pytest.raises(ConnectionError) as captured:
        service.start(uuid.uuid4(), 'a@example.com', 'password')
    assert captured.value.code == code


def test_missing_session_marks_connection_for_reconnect(tmp_path):
    user_id = uuid.uuid4()
    connection_id = uuid.uuid4()

    class Connections:
        status = 'connected'

        def get(self, requested_user):
            assert requested_user == user_id
            return {'id': str(connection_id), 'status': self.status}

        def set_status_if_connection(self, requested_connection, status, error_code=None):
            assert requested_connection == connection_id
            assert error_code == 'session_expired'
            self.status = status

    connections = Connections()
    sessions = GarminSessionProvider(FileTokenStore(tmp_path),
                                     client_factory=FakeGarmin, connections=connections)
    with pytest.raises(ConnectionError, match='reconnect_required'):
        sessions.for_user(user_id)
    assert connections.status == 'reconnect_required'


def test_widget_challenge_keeps_only_csrf_from_html():
    client = FakeGarmin()
    client.client._mfa_flow = 'widget'
    client.client._widget_last_resp = SimpleNamespace(
        text='<input name="_csrf" value="csrf-token">secret-password')
    state = capture_mfa(client)
    assert state['widget_csrf'] == 'csrf-token'
    assert 'secret-password' not in str(state)
    restored = FakeGarmin()
    restore_mfa(restored, state)
    assert restored.client._widget_last_resp.text == '<input name="_csrf" value="csrf-token">'


def test_curl_mfa_session_cookies_survive_reconstruction():
    from curl_cffi import requests as curl_requests

    client = FakeGarmin()
    client.client._mfa_session = curl_requests.Session(impersonate='safari_ios')
    client.client._mfa_session.cookies.set('sso', 'session-secret', domain='sso.garmin.com')
    state = capture_mfa(client)
    assert state['session_type'] == 'curl'
    assert state['impersonate'] == 'safari_ios'
    restored = FakeGarmin()
    restore_mfa(restored, state)
    assert restored.client._mfa_session.cookies.get('sso') == 'session-secret'
