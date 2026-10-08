import logging
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from garmin_mcp.stride_garmin import ConnectionError, GarminConnectionService, GarminSessionProvider
from garmin_mcp.stride_service import ConnectionRepository, StrideConnectionService, SupabaseTokenVerifier
from garmin_mcp.stride_storage import EncryptedSupabaseTokenStore, SupabaseChallengeStore, SupabaseDatabase


logger = logging.getLogger('garmin_mcp.stride')


@dataclass(frozen=True)
class StrideComponents:
    supabase_url: str
    database: SupabaseDatabase
    sessions: GarminSessionProvider
    verifier: SupabaseTokenVerifier
    service: StrideConnectionService


def create_stride_components() -> StrideComponents:
    url = os.environ['SUPABASE_URL']
    key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    encryption_key = os.environ['GARMIN_TOKEN_ENCRYPTION_KEY']
    database = SupabaseDatabase(url, key)
    tokens = EncryptedSupabaseTokenStore(database, encryption_key)
    connections = ConnectionRepository(database)
    sessions = GarminSessionProvider(tokens, connections=connections)
    challenges = SupabaseChallengeStore(database, encryption_key)
    service = StrideConnectionService(GarminConnectionService(sessions, challenges), connections)
    return StrideComponents(url, database, sessions, SupabaseTokenVerifier(url, key), service)


def _identity() -> uuid.UUID | None:
    access = get_access_token()
    if access is None or access.subject is None:
        return None
    try:
        return uuid.UUID(access.subject)
    except ValueError:
        return None


def _request_id(request: Request) -> str:
    try:
        return str(uuid.UUID(request.headers.get('x-request-id', '')))
    except ValueError:
        return str(uuid.uuid4())


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({'error': {'code': code}}, status_code=status,
                        headers={'Cache-Control': 'no-store'})


def _json(data: dict[str, Any]) -> JSONResponse:
    return JSONResponse(data, headers={'Cache-Control': 'no-store'})


async def _body(request: Request) -> dict[str, Any] | None:
    try:
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 16_384:
                return None
        data = json.loads(payload)
    except (ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def register_routes(app: Any, components: StrideComponents) -> None:
    @app.custom_route('/readyz', methods=['GET'])
    async def ready(_request: Request) -> Response:
        try:
            await anyio.to_thread.run_sync(lambda: components.database.request(
                'GET', 'provider_connections', params={'select': 'id', 'limit': '1'}))
        except Exception:
            return _error('dependency_unavailable', 503)
        return _json({'status': 'ready'})

    @app.custom_route('/v1/connections/garmin', methods=['GET'])
    async def get_connection(request: Request) -> Response:
        user_id = _identity()
        request_id = _request_id(request)
        if user_id is None:
            return _error('unauthorized', 401)
        try:
            result = await anyio.to_thread.run_sync(components.service.connections.get, user_id)
            return _json(result)
        except Exception:
            logger.exception('connection_lookup_failed', extra={'request_id': request_id, 'user_id': str(user_id)})
            return _error('storage_unavailable', 503)

    @app.custom_route('/v1/connections/garmin/start', methods=['POST'])
    async def start(request: Request) -> Response:
        user_id = _identity()
        request_id = _request_id(request)
        if user_id is None:
            return _error('unauthorized', 401)
        body = await _body(request)
        if body is None or not isinstance(body.get('email'), str) or not isinstance(body.get('password'), str):
            return _error('invalid_request', 400)
        email, password = body['email'], body['password']
        if not 3 <= len(email) <= 320 or '@' not in email or not 1 <= len(password) <= 1024:
            return _error('invalid_request', 400)
        history_start_date = body.get('history_start_date',
                                      os.getenv('STRIDE_HISTORY_START_DATE', '2000-01-01'))
        if not isinstance(history_start_date, str) or len(history_start_date) != 10:
            return _error('invalid_request', 400)
        try:
            allowed = await anyio.to_thread.run_sync(lambda: components.database.request(
                'POST', 'rpc/allow_auth_attempt', data={'p_user_id': str(user_id)}))
            if not allowed:
                logger.warning('garmin_auth_attempt_denied', extra={
                    'request_id': request_id, 'user_id': str(user_id),
                    'error_code': 'rate_limited'})
                return _error('rate_limited', 429)
            result = await anyio.to_thread.run_sync(components.service.start, user_id, email,
                                                    password, history_start_date)
            logger.info('connection_start_completed', extra={'request_id': request_id,
                        'user_id': str(user_id), 'status': result['status']})
            return _json(result)
        except ConnectionError as error:
            logger.warning('garmin_connection_failed', extra={'request_id': request_id,
                           'user_id': str(user_id), 'error_code': error.code})
            return _error(error.code, error.status)
        except Exception:
            logger.exception('connection_start_failed', extra={'request_id': request_id, 'user_id': str(user_id)})
            return _error('service_unavailable', 503)

    @app.custom_route('/v1/connections/garmin/mfa', methods=['POST'])
    async def mfa(request: Request) -> Response:
        user_id = _identity()
        request_id = _request_id(request)
        if user_id is None:
            return _error('unauthorized', 401)
        body = await _body(request)
        try:
            challenge_id = uuid.UUID(body['challenge_id']) if body else None
            otp = body['otp'] if body else None
        except (ValueError, KeyError, TypeError):
            return _error('invalid_request', 400)
        if challenge_id is None or not isinstance(otp, str) or not 1 <= len(otp) <= 32:
            return _error('invalid_request', 400)
        try:
            allowed = await anyio.to_thread.run_sync(lambda: components.database.request(
                'POST', 'rpc/allow_auth_attempt', data={'p_user_id': str(user_id)}))
            if not allowed:
                return _error('rate_limited', 429)
            result = await anyio.to_thread.run_sync(components.service.mfa, user_id, challenge_id, otp)
            logger.info('mfa_completed', extra={'request_id': request_id, 'user_id': str(user_id)})
            return _json(result)
        except ConnectionError as error:
            logger.warning('garmin_mfa_failed', extra={'request_id': request_id,
                           'user_id': str(user_id), 'error_code': error.code})
            return _error(error.code, error.status)
        except Exception:
            logger.exception('mfa_continuation_failed', extra={'request_id': request_id, 'user_id': str(user_id)})
            return _error('service_unavailable', 503)

    @app.custom_route('/v1/connections/garmin', methods=['DELETE'])
    async def disconnect(request: Request) -> Response:
        user_id = _identity()
        request_id = _request_id(request)
        if user_id is None:
            return _error('unauthorized', 401)
        try:
            await anyio.to_thread.run_sync(components.service.disconnect, user_id)
            logger.info('connection_disconnected', extra={'request_id': request_id, 'user_id': str(user_id)})
            return Response(status_code=204)
        except Exception:
            logger.exception('disconnect_failed', extra={'request_id': request_id, 'user_id': str(user_id)})
            return _error('storage_unavailable', 503)

    @app.custom_route('/v1/connections/garmin/data', methods=['DELETE'])
    async def delete_data(request: Request) -> Response:
        user_id = _identity()
        request_id = _request_id(request)
        if user_id is None:
            return _error('unauthorized', 401)
        try:
            await anyio.to_thread.run_sync(components.service.delete_data, user_id)
            logger.info('garmin_data_deleted', extra={'request_id': request_id,
                        'user_id': str(user_id)})
            return Response(status_code=204)
        except Exception:
            logger.exception('garmin_data_deletion_failed', extra={'request_id': request_id,
                             'user_id': str(user_id)})
            return _error('storage_unavailable', 503)

    @app.custom_route('/v1/connections/garmin/sync', methods=['GET'])
    async def get_sync(request: Request) -> Response:
        user_id = _identity()
        request_id = _request_id(request)
        if user_id is None:
            return _error('unauthorized', 401)
        try:
            result = await anyio.to_thread.run_sync(components.service.connections.get_sync, user_id)
            return _json(result)
        except Exception:
            logger.exception('sync_lookup_failed', extra={'request_id': request_id, 'user_id': str(user_id)})
            return _error('storage_unavailable', 503)
