import uuid
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token

from garmin_mcp.stride_garmin import GarminSessionProvider


DEFAULT_TOOLS = {
    'get_activities', 'get_activity', 'get_activity_splits', 'get_stats',
    'get_sleep_data', 'get_hrv_data', 'get_training_readiness',
    'get_training_status', 'get_rhr_day', 'get_vo2max_trend',
}

PROFILE_METHODS = {'get_display_name', 'get_full_name', 'get_unit_system', 'get_heart_rates',
                   'get_personal_record', 'get_sleep_data'}


class UserScopedGarminProxy:
    def __init__(self, sessions: GarminSessionProvider, nested: bool = False):
        self.sessions = sessions
        self.nested = nested

    def __getattr__(self, name: str) -> Any:
        if name == 'client' and not self.nested:
            return UserScopedGarminProxy(self.sessions, nested=True)
        if name == 'domain' and self.nested:
            access = get_access_token()
            if access is None or access.subject is None:
                raise PermissionError('authenticated_stride_user_required')
            user_id = uuid.UUID(access.subject)
            client = self.sessions.for_user(user_id)
            try:
                return client.client.domain
            finally:
                self.sessions.persist(user_id, client)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            access = get_access_token()
            if access is None or access.subject is None:
                raise PermissionError('authenticated_stride_user_required')
            user_id = uuid.UUID(access.subject)
            client = self.sessions.for_user(user_id, load_profile=name in PROFILE_METHODS)
            try:
                target = client.client if self.nested else client
                return getattr(target, name)(*args, **kwargs)
            finally:
                self.sessions.persist(user_id, client)
        return invoke


def read_only_tool(name: str) -> bool:
    return name.startswith(('get_', 'count_', 'search_', 'download_'))
