import uuid
import threading
from types import SimpleNamespace

import pytest
import requests
from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

from garmin_mcp.stride_garmin import (ConnectionError, GarminConnectionService,
                                      GarminSessionProvider, capture_mfa, restore_mfa)
from garmin_mcp.stride_storage import FileTokenStore
from garmin_mcp.stride_service import StrideConnectionService


class FakeInternal:
    def __init__(self):
        self.calls = []
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
        self.calls.append(path)
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
    client = service.sessions.for_user(user_a)
    assert client.client.calls == []
    assert service.sessions.tokens.load(user_a) == 'refreshed-fresh-token'
    assert service.sessions.tokens.load(user_b) is None


def test_profile_is_loaded_only_when_requested(service):
    user_id = uuid.uuid4()
    service.start(user_id, 'a@example.com', 'password')
    client = service.sessions.for_user(user_id, load_profile=True)
    assert client.display_name == 'Runner'
    assert client.unit_system == 'metric'
    assert client.client.calls == ['/userprofile-service/socialProfile', '/settings']


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


def test_disconnect_keeps_history_and_delete_data_removes_it(service):
    user_id = uuid.uuid4()

    class Database:
        def __init__(self):
            self.connection = True
            self.secret = True
            self.activities = 1

        def request(self, method, path, *, data):
            assert method == 'POST'
            assert data == {'p_user_id': str(user_id)}
            if path == 'rpc/disconnect_garmin_connection':
                self.secret = False
                return True
            if path == 'rpc/delete_garmin_data':
                self.connection = False
                self.activities = 0
                return None
            raise AssertionError(path)

    database = Database()
    connections = SimpleNamespace(database=database)
    stride = StrideConnectionService(service, connections)
    stride.disconnect(user_id)
    assert not database.secret
    assert database.connection
    assert database.activities == 1
    stride.delete_data(user_id)
    assert not database.connection
    assert database.activities == 0


def test_failed_reconnect_preserves_session_and_success_replaces_it(service):
    user_id = uuid.uuid4()
    old_id = uuid.uuid4()

    class Tokens:
        def __init__(self):
            self.value = 'old-token'
            self.connection_id = old_id
            self.activations = []

        def activate(self, requested_user, expected, token, floor):
            assert requested_user == user_id
            assert expected == self.connection_id
            self.activations.append((expected, token, floor))
            self.connection_id = uuid.uuid4()
            self.value = token
            return self.connection_id

    class Connections:
        def get(self, requested_user):
            assert requested_user == user_id
            return {'id': str(tokens.connection_id), 'status': 'connected'}

    tokens = Tokens()
    service.sessions.tokens = tokens
    stride = StrideConnectionService(service, Connections())
    FakeGarmin.outcome = 'invalid'
    with pytest.raises(ConnectionError, match='invalid_credentials'):
        stride.start(user_id, 'a@example.com', 'wrong')
    assert tokens.value == 'old-token'
    assert tokens.connection_id == old_id
    assert tokens.activations == []
    FakeGarmin.outcome = 'success'
    assert stride.start(user_id, 'a@example.com', 'correct', '2024-01-01') == {
        'status': 'connected'}
    assert tokens.value == 'fresh-token'
    assert tokens.connection_id != old_id
    assert tokens.activations == [(old_id, 'fresh-token', '2024-01-01')]


def test_same_user_operations_serialize_and_other_users_continue():
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    connection_ids = {user_a: uuid.uuid4(), user_b: uuid.uuid4()}

    class Connections:
        def get(self, user_id):
            return {'id': str(connection_ids[user_id]), 'status': 'connected'}

    class LeasedTokens:
        def __init__(self):
            self.lock = threading.Lock()
            self.values = {user_a: ('token-a', 1), user_b: ('token-b', 1)}
            self.owners = {}

        def acquire(self, connection_id, owner):
            with self.lock:
                if connection_id in self.owners:
                    return False
                self.owners[connection_id] = owner
                return True

        def renew(self, connection_id, owner):
            return self.owners.get(connection_id) == owner

        def release(self, connection_id, owner):
            with self.lock:
                if self.owners.get(connection_id) == owner:
                    del self.owners[connection_id]

        def load_versioned(self, user_id):
            return self.values[user_id]

        def save_if_version(self, user_id, connection_id, owner, version, token):
            with self.lock:
                assert self.owners[connection_id] == owner
                assert self.values[user_id][1] == version
                self.values[user_id] = token, version + 1
                return version + 1

    tokens = LeasedTokens()
    sessions = GarminSessionProvider(tokens, client_factory=FakeGarmin, connections=Connections())
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def first():
        client = sessions.for_user(user_a)
        first_entered.set()
        assert release_first.wait(3)
        sessions.persist(user_a, client)

    def second():
        assert first_entered.wait(3)
        client = sessions.for_user(user_a)
        second_entered.set()
        sessions.persist(user_a, client)

    thread_a = threading.Thread(target=first)
    thread_b = threading.Thread(target=second)
    thread_a.start()
    thread_b.start()
    assert first_entered.wait(3)
    other = sessions.for_user(user_b)
    sessions.persist(user_b, other)
    assert not second_entered.wait(0.2)
    release_first.set()
    thread_a.join(3)
    thread_b.join(3)
    assert second_entered.is_set()
    assert tokens.values[user_a] == ('refreshed-refreshed-token-a', 3)
    assert tokens.values[user_b] == ('refreshed-token-b', 2)
