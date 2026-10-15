"""Stable application error codes for the Stride integration.

Provider failures are mapped to these codes before they reach clients so
that Stride never has to interpret raw Garmin exceptions. Detailed
provider errors belong in internal logs only. Codes are part of the API
contract and must not be renamed without a migration.
"""

INVALID_REQUEST = 'invalid_request'
RANGE_TOO_LARGE = 'range_too_large'
UNAUTHORIZED = 'unauthorized'
INVALID_CREDENTIALS = 'invalid_credentials'
INVALID_MFA = 'invalid_mfa'
CHALLENGE_EXPIRED = 'challenge_expired'
RATE_LIMITED = 'rate_limited'
PROVIDER_UNAVAILABLE = 'provider_unavailable'
# The Garmin session is held by another operation (usually a sync); retry shortly.
PROVIDER_BUSY = 'provider_busy'
RECONNECT_REQUIRED = 'reconnect_required'
STORAGE_UNAVAILABLE = 'storage_unavailable'
SYNC_FAILED = 'sync_failed'
# A manual sync was requested less than five minutes after the previous one.
SYNC_THROTTLED = 'sync_throttled'
PARTIAL_DATA = 'partial_data'
UNSUPPORTED_DATA = 'unsupported_data'
CONNECTION_CHANGED = 'connection_changed'
DEPENDENCY_UNAVAILABLE = 'dependency_unavailable'
SERVICE_UNAVAILABLE = 'service_unavailable'
DISCONNECTED = 'disconnected'
CONNECTION_REPLACED = 'connection_replaced'

# Provider failures that are safe to retry with backoff.
RETRYABLE_CODES = frozenset({RATE_LIMITED, PROVIDER_UNAVAILABLE})


def is_retryable(code: str) -> bool:
    """Return whether a sync failure code should be retried."""
    return code in RETRYABLE_CODES
