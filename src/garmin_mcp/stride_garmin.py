import uuid
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, NoReturn, Protocol

import requests
from garminconnect import (Garmin, GarminConnectAuthenticationError,
                           GarminConnectConnectionError, GarminConnectTooManyRequestsError)

from garmin_mcp.stride_storage import SupabaseChallengeStore, TokenStore


class ConnectionError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


def map_error(error: Exception) -> ConnectionError:
    if isinstance(error, GarminConnectTooManyRequestsError):
        return ConnectionError('rate_limited', 429)
    if isinstance(error, GarminConnectAuthenticationError):
        return ConnectionError('invalid_credentials', 401)
    if isinstance(error, GarminConnectConnectionError):
        return ConnectionError('provider_unavailable', 503)
    return ConnectionError('provider_unavailable', 503)


class ConnectionReader(Protocol):
    def get(self, user_id: uuid.UUID) -> dict[str, Any]: ...
    def set_status_if_connection(self, connection_id: uuid.UUID, status: str,
                                 error_code: str | None = None) -> None: ...


def capture_mfa(client: Garmin) -> dict[str, Any]:
    internal = client.client
    session = internal._mfa_session
    jar = getattr(session.cookies, 'jar', session.cookies)
    cookies = [{'name': cookie.name, 'value': cookie.value,
                'domain': cookie.domain, 'path': cookie.path,
                'secure': cookie.secure, 'expires': cookie.expires} for cookie in jar]
    widget_csrf = None
    if internal._mfa_flow == 'widget':
        match = re.search(r'name="_csrf"\s+value="(.+?)"', internal._widget_last_resp.text)
        if not match:
            raise ConnectionError('provider_unavailable', 503)
        widget_csrf = match.group(1)
    return {
        'flow': internal._mfa_flow,
        'method': getattr(internal, '_mfa_method', 'email'),
        'cookies': cookies,
        'session_type': 'curl' if session.__class__.__module__.startswith('curl_cffi') else 'requests',
        'impersonate': getattr(session, 'impersonate', None),
        'login_params': internal._mfa_login_params,
        'post_headers': internal._mfa_post_headers,
        'service_url': getattr(internal, '_mfa_service_url', None),
        'widget_csrf': widget_csrf,
    }


def restore_mfa(client: Garmin, state: dict[str, Any]) -> None:
    if state['session_type'] == 'curl':
        from curl_cffi import requests as curl_requests
        session = curl_requests.Session(impersonate=state['impersonate'] or 'chrome')
    else:
        session = requests.Session()
    for cookie in state['cookies']:
        options = {'domain': cookie['domain'], 'path': cookie['path'],
                   'secure': cookie['secure']}
        if state['session_type'] != 'curl':
            options['expires'] = cookie['expires']
        session.cookies.set(cookie['name'], cookie['value'], **options)
    internal = client.client
    internal._mfa_session = session
    internal._mfa_flow = state['flow']
    internal._mfa_method = state['method']
    internal._mfa_login_params = state['login_params']
    internal._mfa_post_headers = state['post_headers']
    if state['service_url']:
        internal._mfa_service_url = state['service_url']
    if state['widget_csrf'] is not None:
        internal._widget_last_resp = _WidgetResponse(
            f'<input name="_csrf" value="{state["widget_csrf"]}">')


@dataclass
class _WidgetResponse:
    text: str


@dataclass(frozen=True)
class AuthenticationResult:
    status: str
    token_data: str | None = None
    challenge_id: uuid.UUID | None = None
    expected_connection_id: uuid.UUID | None = None
    history_start_date: str | None = None


class GarminSessionProvider:
    def __init__(self, tokens: TokenStore, client_factory: Callable[..., Garmin] = Garmin,
                 connections: ConnectionReader | None = None):
        self.tokens = tokens
        self.client_factory = client_factory
        self.connections = connections

    def _reconnect_required(self, connection: dict[str, Any] | None) -> NoReturn:
        if connection and self.connections:
            self.connections.set_status_if_connection(uuid.UUID(connection['id']),
                                                      'reconnect_required', 'session_expired')
        raise ConnectionError('reconnect_required', 409)

    def _acquire(self, connection_id: uuid.UUID) -> uuid.UUID:
        owner = uuid.uuid4()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.tokens.acquire(connection_id, owner):
                return owner
            time.sleep(0.1)
        raise ConnectionError('provider_unavailable', 503)

    def _release(self, client: Garmin) -> None:
        owner = getattr(client, '_stride_lease_owner', None)
        if owner is None:
            return
        client._stride_lease_owner = None
        stop = getattr(client, '_stride_lease_stop')
        stop.set()
        self.tokens.release(client._stride_connection_id, owner)

    def _renew(self, client: Garmin) -> None:
        stop = threading.Event()
        client._stride_lease_stop = stop
        owner = client._stride_lease_owner
        connection_id = client._stride_connection_id

        def maintain() -> None:
            while not stop.wait(30):
                try:
                    if not self.tokens.renew(connection_id, owner):
                        return
                except Exception:
                    return

        threading.Thread(target=maintain, name='garmin-session-lease', daemon=True).start()

    def for_user(self, user_id: uuid.UUID, load_profile: bool = False) -> Garmin:
        connection = self.connections.get(user_id) if self.connections else None
        if connection and connection['status'] != 'connected':
            raise ConnectionError('reconnect_required', 409)
        owner = None
        client = None
        ready = False
        try:
            if connection and hasattr(self.tokens, 'acquire'):
                owner = self._acquire(uuid.UUID(connection['id']))
                stored = self.tokens.load_versioned(user_id)
                token = stored[0] if stored else None
            else:
                stored = None
                token = self.tokens.load(user_id)
            if token is None:
                self._reconnect_required(connection)
            client = self.client_factory()
            if connection:
                client._stride_connection_id = uuid.UUID(connection['id'])
            if owner:
                client._stride_lease_owner = owner
                client._stride_token_version = stored[1]
                self._renew(client)
            client._stride_last_token = token
            internal = client.client
            try:
                internal.loads(token)
            except (GarminConnectAuthenticationError, GarminConnectConnectionError):
                self._reconnect_required(connection)
            if internal.di_refresh_token and internal._token_expires_soon():
                internal._refresh_session()
            if load_profile:
                profile = internal.connectapi('/userprofile-service/socialProfile')
                settings = internal.connectapi(client.garmin_connect_user_settings_url)
                client.display_name = profile.get('displayName')
                client.full_name = profile.get('fullName', '')
                client.unit_system = settings.get('userData', {}).get('measurementSystem')
            self._save_changed(user_id, client)
            ready = True
            return client
        except Exception as error:
            if isinstance(error, GarminConnectAuthenticationError):
                self._reconnect_required(connection)
            if isinstance(error, ConnectionError):
                raise
            raise map_error(error) from None
        finally:
            if not ready and owner:
                if client is None:
                    self.tokens.release(uuid.UUID(connection['id']), owner)
                else:
                    self._release(client)

    def persist(self, user_id: uuid.UUID, client: Garmin) -> None:
        try:
            self._save_changed(user_id, client)
        finally:
            self._release(client)

    def _save_changed(self, user_id: uuid.UUID, client: Garmin) -> None:
        token_data = client.client.dumps()
        if getattr(client, '_stride_last_token', None) == token_data:
            return
        connection_id = getattr(client, '_stride_connection_id', None)
        owner = getattr(client, '_stride_lease_owner', None)
        if owner:
            client._stride_token_version = self.tokens.save_if_version(
                user_id, connection_id, owner, client._stride_token_version, token_data)
        elif connection_id and hasattr(self.tokens, 'save_if_connection'):
            self.tokens.save_if_connection(user_id, connection_id, token_data)
        else:
            self.tokens.save(user_id, token_data)
        client._stride_last_token = token_data


class GarminConnectionService:
    def __init__(self, sessions: GarminSessionProvider, challenges: SupabaseChallengeStore,
                 client_factory: Callable[..., Garmin] = Garmin):
        self.sessions = sessions
        self.challenges = challenges
        self.client_factory = client_factory

    def start(self, user_id: uuid.UUID, email: str, password: str,
              connection_id: uuid.UUID | None = None) -> dict[str, str]:
        result = self.authenticate(user_id, email, password, connection_id)
        if result.token_data is not None:
            self.sessions.tokens.save(user_id, result.token_data)
        return self._public_result(result)

    @staticmethod
    def _public_result(result: AuthenticationResult) -> dict[str, str]:
        response = {'status': result.status}
        if result.challenge_id is not None:
            response['challenge_id'] = str(result.challenge_id)
        return response

    def authenticate(self, user_id: uuid.UUID, email: str, password: str,
                     expected_connection_id: uuid.UUID | None = None,
                     history_start_date: str | None = None) -> AuthenticationResult:
        client = self.client_factory(email=email, password=password, return_on_mfa=True)
        try:
            status, _ = client.login()
            if status == 'needs_mfa':
                state = capture_mfa(client)
                state['expected_connection_id'] = (str(expected_connection_id)
                                                   if expected_connection_id else None)
                state['history_start_date'] = history_start_date
                challenge_id = self.challenges.create(user_id, state)
                return AuthenticationResult('mfa_required', challenge_id=challenge_id)
            return AuthenticationResult('connected', client.client.dumps(),
                                        expected_connection_id=expected_connection_id,
                                        history_start_date=history_start_date)
        except Exception as error:
            raise map_error(error) from None

    def mfa(self, user_id: uuid.UUID, challenge_id: uuid.UUID, otp: str) -> dict[str, str]:
        result = self.continue_authentication(user_id, challenge_id, otp)
        if result.token_data is not None:
            self.sessions.tokens.save(user_id, result.token_data)
        return self._public_result(result)

    def continue_authentication(self, user_id: uuid.UUID, challenge_id: uuid.UUID,
                                otp: str) -> AuthenticationResult:
        challenge = self.challenges.consume(user_id, challenge_id)
        if challenge is None:
            raise ConnectionError('challenge_expired', 410)
        client = self.client_factory()
        restore_mfa(client, challenge.state)
        try:
            client.resume_login({}, otp)
            expected = challenge.state.get('expected_connection_id')
            return AuthenticationResult(
                'connected', client.client.dumps(),
                expected_connection_id=uuid.UUID(expected) if expected else None,
                history_start_date=challenge.state.get('history_start_date'))
        except GarminConnectAuthenticationError:
            raise ConnectionError('invalid_mfa', 401) from None
        except Exception as error:
            raise map_error(error) from None
