import logging
import uuid
from datetime import datetime, timezone
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
            'select': 'id,status,connected_at,last_authenticated_at,last_sync_at,last_error_code'})
        return rows[0] if rows else {'status': 'disconnected'}

    def set_status(self, user_id: uuid.UUID, status: str, error_code: str | None = None) -> None:
        data: dict[str, Any] = {'user_id': str(user_id), 'provider': 'garmin',
                                'status': status, 'last_error_code': error_code}
        if status == 'connected':
            data['connected_at'] = datetime.now(timezone.utc).isoformat()
            data['last_authenticated_at'] = data['connected_at']
        self.database.request('POST', 'provider_connections',
                              params={'on_conflict': 'user_id,provider'}, data=data,
                              prefer='resolution=merge-duplicates')

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

    def delete(self, user_id: uuid.UUID) -> None:
        self.database.request('DELETE', 'provider_connections', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin'})

    def enqueue_initial_sync(self, user_id: uuid.UUID) -> None:
        connection = self.get(user_id)
        active = self.database.request('GET', 'sync_jobs', params={
            'connection_id': f"eq.{connection['id']}", 'mode': 'eq.historical',
            'state': 'in.(queued,running)', 'select': 'id', 'limit': '1'})
        if active:
            return
        self.database.request('POST', 'sync_jobs', data={
            'connection_id': connection['id'], 'user_id': str(user_id),
            'provider': 'garmin', 'state': 'queued', 'phase': 'activities',
            'mode': 'historical'})

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

    def start(self, user_id: uuid.UUID, email: str, password: str) -> dict[str, str]:
        self.connections.database.request('DELETE', 'auth_challenges', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin'})
        self.connections.delete(user_id)
        self.connections.set_status(user_id, 'connecting')
        connection_id = uuid.UUID(self.connections.get(user_id)['id'])
        try:
            result = self.garmin.start(user_id, email, password, connection_id)
        except ConnectionError as error:
            status = error.code if error.code in ('rate_limited', 'provider_unavailable') else 'error'
            self.connections.set_status_if_connection(connection_id, status, error.code)
            raise
        self.connections.set_status_if_connection(connection_id, result['status'])
        if result['status'] == 'connected':
            self.connections.enqueue_initial_sync(user_id)
        return result

    def mfa(self, user_id: uuid.UUID, challenge_id: uuid.UUID, otp: str) -> dict[str, str]:
        connection = self.connections.get(user_id)
        if 'id' not in connection:
            raise ConnectionError('challenge_expired', 410)
        connection_id = uuid.UUID(connection['id'])
        try:
            result = self.garmin.mfa(user_id, challenge_id, otp)
        except ConnectionError as error:
            if error.code == 'invalid_mfa':
                self.connections.set_status_if_connection(connection_id, 'reconnect_required', error.code)
            raise
        self.connections.set_status_if_connection(connection_id, 'connected')
        self.connections.enqueue_initial_sync(user_id)
        return result

    def disconnect(self, user_id: uuid.UUID) -> None:
        self.connections.database.request('DELETE', 'auth_challenges', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin'})
        self.garmin.sessions.tokens.delete(user_id)
        self.connections.delete(user_id)
