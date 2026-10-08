import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any

import httpx
from mcp.server.auth.provider import AccessToken

from garmin_mcp.stride_garmin import ConnectionError, GarminConnectionService
from garmin_mcp.stride_storage import SupabaseDatabase


logger = logging.getLogger('garmin_mcp.stride')


class SupabaseTokenVerifier:
    def __init__(self, url: str, service_key: str):
        self.url = url.rstrip('/') + '/auth/v1/user'
        self.service_key = service_key

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(self.url, headers={
                    'apikey': self.service_key, 'Authorization': f'Bearer {token}'})
            if response.status_code != 200:
                return None
            user_id = str(uuid.UUID(response.json()['id']))
            return AccessToken(token=token, client_id=user_id, subject=user_id,
                               scopes=['stride:garmin'])
        except (httpx.HTTPError, ValueError, KeyError):
            logger.warning('supabase_auth_validation_failed')
            return None


class ConnectionRepository:
    def __init__(self, database: SupabaseDatabase):
        self.database = database

    def get(self, user_id: uuid.UUID) -> dict[str, Any]:
        rows = self.database.request('GET', 'provider_connections', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin',
            'status': 'neq.disconnected',
            'select': 'id,status,connected_at,last_authenticated_at,last_sync_at,last_error_code',
            'limit': '1'})
        if rows:
            return rows[0]
        rows = self.database.request('GET', 'provider_connections', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin',
            'select': 'id,status,connected_at,last_authenticated_at,last_sync_at,last_error_code',
            'order': 'created_at.desc', 'limit': '1'})
        return rows[0] if rows else {'status': 'disconnected'}

    def set_status_if_connection(self, connection_id: uuid.UUID, status: str,
                                 error_code: str | None = None) -> None:
        data: dict[str, Any] = {'status': status, 'last_error_code': error_code}
        if status == 'connected':
            data['connected_at'] = datetime.now(timezone.utc).isoformat()
            data['last_authenticated_at'] = data['connected_at']
        rows = self.database.request('PATCH', 'provider_connections',
                                     params={'id': f'eq.{connection_id}'}, data=data,
                                     prefer='return=representation')
        if not rows:
            raise RuntimeError('provider_connection_changed')

    def get_sync(self, user_id: uuid.UUID) -> dict[str, Any]:
        rows = self.database.request('GET', 'sync_jobs', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin',
            'select': 'id,state,phase,cursor_date,oldest_synchronized_date,activity_count,last_error_code,last_success_at,updated_at',
            'order': 'created_at.desc', 'limit': '1'})
        return rows[0] if rows else {'state': 'not_started'}


class StrideConnectionService:
    def __init__(self, garmin: GarminConnectionService, connections: ConnectionRepository):
        self.garmin = garmin
        self.connections = connections

    def start(self, user_id: uuid.UUID, email: str, password: str,
              history_start_date: str = '2000-01-01') -> dict[str, str]:
        try:
            floor = date.fromisoformat(history_start_date)
        except ValueError:
            raise ConnectionError('invalid_request', 400) from None
        if floor > date.today() or floor < date(1980, 1, 1):
            raise ConnectionError('invalid_request', 400)
        previous = self.connections.get(user_id)
        expected = (uuid.UUID(previous['id']) if previous['status'] != 'disconnected'
                    else None)
        result = self.garmin.authenticate(user_id, email, password, expected,
                                          history_start_date)
        if result.token_data is not None:
            try:
                self.garmin.sessions.tokens.activate(user_id, expected, result.token_data,
                                                     history_start_date)
            except RuntimeError as error:
                if str(error) == 'provider_connection_changed':
                    raise ConnectionError('connection_changed', 409) from None
                raise
        return self.garmin._public_result(result)

    def mfa(self, user_id: uuid.UUID, challenge_id: uuid.UUID, otp: str) -> dict[str, str]:
        result = self.garmin.continue_authentication(user_id, challenge_id, otp)
        if result.token_data is not None:
            try:
                self.garmin.sessions.tokens.activate(user_id, result.expected_connection_id,
                                                     result.token_data,
                                                     result.history_start_date or '2000-01-01')
            except RuntimeError as error:
                if str(error) == 'provider_connection_changed':
                    raise ConnectionError('challenge_expired', 410) from None
                raise
        return self.garmin._public_result(result)

    def disconnect(self, user_id: uuid.UUID) -> None:
        self.connections.database.request('POST', 'rpc/disconnect_garmin_connection',
                                          data={'p_user_id': str(user_id)})

    def delete_data(self, user_id: uuid.UUID) -> None:
        self.connections.database.request('POST', 'rpc/delete_garmin_data',
                                          data={'p_user_id': str(user_id)})
