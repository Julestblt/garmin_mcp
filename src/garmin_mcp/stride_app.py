import logging
import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from garmin_mcp.stride_garmin import ConnectionError, GarminConnectionService, GarminSessionProvider
from garmin_mcp.stride_gear import (GearRepository, GearRequestError, GearService, gear_uuid,
                                    parse_activity_gear, parse_create, parse_update)
from garmin_mcp.stride_service import (ConnectionRepository, StrideConnectionService,
                                       SupabaseTokenVerifier, SyncThrottledError)
from garmin_mcp.stride_storage import EncryptedSupabaseTokenStore, SupabaseChallengeStore, SupabaseDatabase
from garmin_mcp.stride_wellness import WellnessDayService
from garmin_mcp.stride_workouts import (WorkoutPushService, WorkoutRepository, WorkoutRequestError,
                                       parse_reconcile)


logger = logging.getLogger('garmin_mcp.stride')


@dataclass(frozen=True)
class StrideComponents:
    supabase_url: str
    database: SupabaseDatabase
    sessions: GarminSessionProvider
    verifier: SupabaseTokenVerifier
    service: StrideConnectionService
    wellness: WellnessDayService
    workouts: WorkoutPushService
    gear: GearService


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
    return StrideComponents(url, database, sessions, SupabaseTokenVerifier(url, key), service,
                            WellnessDayService(sessions),
                            WorkoutPushService(sessions, WorkoutRepository(database)),
                            GearService(sessions, GearRepository(database)))


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


async def _body(request: Request, limit: int = 16_384) -> dict[str, Any] | None:
    try:
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > limit:
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
            today = datetime.now(timezone.utc).date()
            await anyio.to_thread.run_sync(components.workouts.forget, user_id, today)
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

    @app.custom_route('/v1/connections/garmin/sync', methods=['POST'])
    async def request_sync(request: Request) -> Response:
        user_id = _identity()
        request_id = _request_id(request)
        if user_id is None:
            return _error('unauthorized', 401)
        try:
            result = await anyio.to_thread.run_sync(components.service.request_sync, user_id)
            logger.info('manual_sync_requested', extra={'request_id': request_id,
                        'user_id': str(user_id), 'status': result['request']})
            return _json(result)
        except SyncThrottledError as error:
            return JSONResponse({'error': {'code': error.code},
                                 'available_at': error.available_at},
                                status_code=error.status,
                                headers={'Cache-Control': 'no-store',
                                         'Retry-After': str(error.retry_after_seconds)})
        except ConnectionError as error:
            logger.warning('manual_sync_rejected', extra={'request_id': request_id,
                           'user_id': str(user_id), 'error_code': error.code})
            return _error(error.code, error.status)
        except Exception:
            logger.exception('manual_sync_failed', extra={'request_id': request_id,
                             'user_id': str(user_id)})
            return _error('storage_unavailable', 503)

    @app.custom_route('/v1/wellness/days/{day}', methods=['GET'])
    async def get_wellness_day(request: Request) -> Response:
        user_id = _identity()
        request_id = _request_id(request)
        if user_id is None:
            return _error('unauthorized', 401)
        try:
            day = date.fromisoformat(request.path_params['day'])
        except (KeyError, ValueError):
            return _error('invalid_request', 400)
        today = datetime.now(timezone.utc).date()
        if day > today + timedelta(days=1) or day < today - timedelta(days=730):
            return _error('invalid_request', 400)
        try:
            result = await anyio.to_thread.run_sync(components.wellness.get, user_id, day)
            return _json(result)
        except ConnectionError as error:
            logger.warning('wellness_day_failed', extra={'request_id': request_id,
                           'user_id': str(user_id), 'error_code': error.code})
            return _error(error.code, error.status)
        except Exception:
            logger.exception('wellness_day_unavailable', extra={'request_id': request_id,
                             'user_id': str(user_id)})
            return _error('provider_unavailable', 503)

    @app.custom_route('/v1/connections/garmin/workouts', methods=['PUT'])
    async def reconcile_workouts(request: Request) -> Response:
        user_id = _identity()
        request_id = _request_id(request)
        if user_id is None:
            return _error('unauthorized', 401)
        body = await _body(request, limit=262_144)
        try:
            start, end, workouts = parse_reconcile(body, datetime.now(timezone.utc).date())
        except WorkoutRequestError:
            return _error('invalid_request', 400)
        try:
            result = await anyio.to_thread.run_sync(components.workouts.reconcile, user_id,
                                                    start, end, workouts)
            return _json(result)
        except ConnectionError as error:
            logger.warning('workout_reconcile_rejected', extra={'request_id': request_id,
                           'user_id': str(user_id), 'error_code': error.code})
            return _error(error.code, error.status)
        except Exception:
            logger.exception('workout_reconcile_failed', extra={'request_id': request_id,
                             'user_id': str(user_id)})
            return _error('service_unavailable', 503)

    @app.custom_route('/v1/connections/garmin/workouts', methods=['DELETE'])
    async def remove_workouts(request: Request) -> Response:
        user_id = _identity()
        request_id = _request_id(request)
        if user_id is None:
            return _error('unauthorized', 401)
        try:
            removed = await anyio.to_thread.run_sync(components.workouts.remove_upcoming, user_id,
                                                     datetime.now(timezone.utc).date())
            logger.info('workouts_removed', extra={'request_id': request_id,
                        'user_id': str(user_id), 'removed': removed})
            return _json({'removed': removed})
        except ConnectionError as error:
            logger.warning('workout_removal_rejected', extra={'request_id': request_id,
                           'user_id': str(user_id), 'error_code': error.code})
            return _error(error.code, error.status)
        except Exception:
            logger.exception('workout_removal_failed', extra={'request_id': request_id,
                             'user_id': str(user_id)})
            return _error('service_unavailable', 503)

    async def _gear_call(request: Request, event: str, call: Any) -> Response:
        user_id = _identity()
        request_id = _request_id(request)
        if user_id is None:
            return _error('unauthorized', 401)
        try:
            result = await anyio.to_thread.run_sync(call, user_id)
            return _json(result)
        except ConnectionError as error:
            logger.warning(f'{event}_rejected', extra={'request_id': request_id,
                           'user_id': str(user_id), 'error_code': error.code})
            return _error(error.code, error.status)
        except Exception:
            logger.exception(f'{event}_failed', extra={'request_id': request_id,
                             'user_id': str(user_id)})
            return _error('service_unavailable', 503)

    @app.custom_route('/v1/connections/garmin/gear', methods=['POST'])
    async def create_gear(request: Request) -> Response:
        body = await _body(request)
        try:
            payload = parse_create(body)
        except GearRequestError:
            return _error('invalid_request', 400) if _identity() else _error('unauthorized', 401)
        return await _gear_call(request, 'gear_create',
                                lambda user_id: components.gear.create(user_id, payload))

    @app.custom_route('/v1/connections/garmin/gear/{gear_uuid}', methods=['PATCH'])
    async def update_gear(request: Request) -> Response:
        body = await _body(request)
        try:
            key = gear_uuid(request.path_params.get('gear_uuid'))
            payload = parse_update(body)
        except GearRequestError:
            return _error('invalid_request', 400) if _identity() else _error('unauthorized', 401)
        return await _gear_call(request, 'gear_update',
                                lambda user_id: components.gear.update(user_id, key, payload))

    @app.custom_route('/v1/connections/garmin/activities/{activity_id}/gear', methods=['PUT'])
    async def set_activity_gear(request: Request) -> Response:
        body = await _body(request)
        try:
            activity_id, key = parse_activity_gear(request.path_params.get('activity_id'), body)
        except GearRequestError:
            return _error('invalid_request', 400) if _identity() else _error('unauthorized', 401)
        return await _gear_call(request, 'activity_gear',
                                lambda user_id: components.gear.set_activity_gear(
                                    user_id, activity_id, key))
