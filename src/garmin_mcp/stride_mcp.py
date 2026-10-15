import asyncio
import functools
import inspect
import os
import uuid
from contextvars import ContextVar
from datetime import date
from typing import Any, Callable

import anyio
from garminconnect import (Garmin, GarminConnectAuthenticationError,
                           GarminConnectConnectionError)
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from garmin_mcp.stride_errors import (INVALID_REQUEST, PROVIDER_BUSY, PROVIDER_UNAVAILABLE,
                                      RANGE_TOO_LARGE, RATE_LIMITED, RECONNECT_REQUIRED,
                                      UNAUTHORIZED)
from garmin_mcp.stride_garmin import (ConnectionError, GarminSessionProvider, LeaseBusyError,
                                      map_error)


DEFAULT_TOOLS = {
    'get_activities', 'get_activities_by_date', 'get_activity', 'get_activity_splits',
    'get_stats', 'get_sleep_data', 'get_hrv_data', 'get_training_readiness',
    'get_training_status', 'get_rhr_day', 'get_vo2max_trend',
}

PROFILE_METHODS = {'get_display_name', 'get_full_name', 'get_unit_system', 'get_heart_rates',
                   'get_personal_record', 'get_sleep_data'}

# Inclusive number of days a single Stride-mode call may cover.
MAX_RANGE_DAYS = 14
DEFAULT_MAX_RESPONSE_BYTES = 65_536
TRUNCATION_MARKER = '[truncated]'
# Seconds an MCP call waits for a session lease held by a sync before PROVIDER_BUSY.
MCP_LEASE_WAIT_SECONDS = 3.0

_url_template: Garmin | None = None


def _garmin_url(name: str) -> str:
    """Return a Garmin Connect endpoint path, which needs no user session."""
    global _url_template
    if _url_template is None:
        _url_template = Garmin()
    return getattr(_url_template, name)


class _CallOutcome:
    """Failure code of the last Garmin call made by one tool invocation, if it failed."""

    def __init__(self) -> None:
        self.code: str | None = None


_outcome: ContextVar[_CallOutcome | None] = ContextVar('stride_mcp_call_outcome', default=None)

TOOL_ERROR_MESSAGES = {
    PROVIDER_BUSY: 'Garmin data is temporarily unavailable because a sync is using this '
                   "athlete's Garmin session. Retry in a minute.",
    RATE_LIMITED: 'Garmin is rate limiting requests. Retry later.',
    RECONNECT_REQUIRED: 'The athlete must reconnect Garmin in Stride.',
    UNAUTHORIZED: 'No authenticated Stride user.',
    PROVIDER_UNAVAILABLE: 'Garmin Connect did not answer. Retry later.',
}


def tool_error_code(error: Exception) -> str | None:
    """Map a Garmin call failure to a stable code; None when it only means no data."""
    if isinstance(error, LeaseBusyError):
        return PROVIDER_BUSY
    if isinstance(error, ConnectionError):
        return error.code
    if isinstance(error, PermissionError):
        return UNAUTHORIZED
    if isinstance(error, GarminConnectAuthenticationError):
        return RECONNECT_REQUIRED
    if isinstance(error, GarminConnectConnectionError) and 'API Error 404' in str(error):
        return None
    return map_error(error).code


class UserScopedGarminProxy:
    def __init__(self, sessions: GarminSessionProvider, nested: bool = False):
        self.sessions = sessions
        self.nested = nested

    def _call(self, name: str, use: Callable[[Garmin], Any]) -> Any:
        outcome = _outcome.get()
        try:
            access = get_access_token()
            if access is None or access.subject is None:
                raise PermissionError('authenticated_stride_user_required')
            user_id = uuid.UUID(access.subject)
            client = self.sessions.for_user(user_id, load_profile=name in PROFILE_METHODS,
                                            lease_wait=MCP_LEASE_WAIT_SECONDS)
            try:
                result = use(client)
            finally:
                self.sessions.persist(user_id, client)
        except Exception as error:
            if outcome is not None:
                outcome.code = tool_error_code(error)
            raise
        if outcome is not None:
            outcome.code = None
        return result

    def __getattr__(self, name: str) -> Any:
        if name == 'client' and not self.nested:
            return UserScopedGarminProxy(self.sessions, nested=True)
        if name.startswith('garmin_connect_') and not self.nested:
            return _garmin_url(name)
        if name == 'domain' and self.nested:
            return self._call(name, lambda client: client.client.domain)

        def invoke(*args: Any, **kwargs: Any) -> Any:
            def use(client: Garmin) -> Any:
                target = client.client if self.nested else client
                return getattr(target, name)(*args, **kwargs)
            return self._call(name, use)
        return invoke


def read_only_tool(name: str) -> bool:
    return name.startswith(('get_', 'count_', 'search_', 'download_'))


def resolve_max_response_bytes() -> int:
    raw = os.getenv('STRIDE_MCP_MAX_RESPONSE_BYTES', '').strip()
    if not raw:
        return DEFAULT_MAX_RESPONSE_BYTES
    value = int(raw)
    if value < 1024:
        raise ValueError('STRIDE_MCP_MAX_RESPONSE_BYTES must be at least 1024')
    return value


class StrideToolError(ToolError):
    """A rejected Stride-mode call, reported to the caller under a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(f'{code}: {message}')
        self.code = code

    def result(self) -> CallToolResult:
        return CallToolResult(isError=True, content=[TextContent(type='text', text=str(self))],
                              structuredContent={'error': {'code': self.code}})


def check_date_range(start_date: Any, end_date: Any) -> None:
    """Reject a range that is malformed, reversed or wider than MAX_RANGE_DAYS."""
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except (TypeError, ValueError):
        raise StrideToolError(INVALID_REQUEST, 'start_date and end_date must be YYYY-MM-DD dates')
    if end < start:
        raise StrideToolError(INVALID_REQUEST, 'end_date must be on or after start_date')
    days = (end - start).days + 1
    if days > MAX_RANGE_DAYS:
        raise StrideToolError(RANGE_TOO_LARGE, f'{start_date} to {end_date} covers {days} days; '
                                               f'request at most {MAX_RANGE_DAYS} days per call')


def cap_response(result: Any, max_bytes: int) -> Any:
    """Truncate a text result above max_bytes and say so on the first line."""
    if not isinstance(result, str):
        return result
    encoded = result.encode('utf-8')
    if len(encoded) <= max_bytes:
        return result
    kept = encoded[:max_bytes].decode('utf-8', errors='ignore')
    return (f'{TRUNCATION_MARKER} Response cut to {len(kept.encode("utf-8"))} of '
            f'{len(encoded)} bytes; the data below is incomplete. Request a single '
            f'date or a narrower range for the full data.\n{kept}')


def _run_tool(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    result = fn(*args, **kwargs)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


class StrideToolGuard:
    """Wraps a FastMCP app so every Stride-mode tool reads a bounded amount of data.

    Tools taking ``start_date`` and ``end_date`` are rejected above
    MAX_RANGE_DAYS before Garmin is called, and text results above the size cap
    are truncated behind TRUNCATION_MARKER. Tools run on a worker thread, and a
    rejected range or a failed final Garmin call returns an error result carrying
    a stable code instead of the tool's free-text error.
    """

    def __init__(self, app: Any, max_response_bytes: int):
        self._app = app
        self._max_response_bytes = max_response_bytes

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        decorator = self._app.tool(*args, **kwargs)

        def wrapper(fn: Callable[..., Any]) -> Any:
            return decorator(self.guard(fn))

        return wrapper

    def guard(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(fn)
        ranged = {'start_date', 'end_date'} <= signature.parameters.keys()
        max_bytes = self._max_response_bytes

        @functools.wraps(fn)
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            if ranged:
                bound = signature.bind(*args, **kwargs)
                try:
                    check_date_range(bound.arguments.get('start_date'),
                                     bound.arguments.get('end_date'))
                except StrideToolError as error:
                    return error.result()
            outcome = _CallOutcome()
            token = _outcome.set(outcome)
            try:
                # Tools call Garmin synchronously; a worker thread keeps the event
                # loop serving other requests while one waits for a lease or Garmin.
                result = await anyio.to_thread.run_sync(_run_tool, fn, args, kwargs)
            except Exception:
                if outcome.code is None:
                    raise
                result = None
            finally:
                _outcome.reset(token)
            if outcome.code is not None:
                return StrideToolError(outcome.code, TOOL_ERROR_MESSAGES.get(
                    outcome.code, 'Garmin call failed.')).result()
            return cap_response(result, max_bytes)

        return guarded

    def __getattr__(self, item: str) -> Any:
        return getattr(self._app, item)
