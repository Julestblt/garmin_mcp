import json
import logging
import sys
from datetime import datetime, timezone


LOG_FIELDS = (
    'request_id', 'user_id', 'connection_id', 'sync_job_id', 'error_code',
    'phase', 'mode', 'state', 'status', 'activity_count', 'observation_count',
    'job_count', 'duration_ms', 'attempts', 'failure_count', 'rows_imported',
    'retry', 'queue_depth', 'tool_name',
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'event': record.getMessage(),
        }
        for name in LOG_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                data[name] = value
        if record.exc_info:
            data['exception_type'] = record.exc_info[0].__name__
        return json.dumps(data, separators=(',', ':'))


def configure_logging() -> None:
    logger = logging.getLogger('garmin_mcp.stride')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
