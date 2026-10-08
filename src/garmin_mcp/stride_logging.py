import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'event': record.getMessage(),
        }
        for name in ('request_id', 'user_id', 'connection_id', 'sync_job_id', 'error_code'):
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
