import json
import logging

from garmin_mcp.stride_logging import JsonFormatter


def test_formatter_emits_operational_fields_and_drops_unknown():
    record = logging.LogRecord('garmin_mcp.stride.sync', logging.INFO, __file__, 1,
                               'sync_chunk_completed', (), None)
    record.user_id = 'user-1'
    record.phase = 'activities'
    record.duration_ms = 123
    record.rows_imported = 4
    record.password = 'must-not-appear'
    record.token = 'must-not-appear'
    payload = json.loads(JsonFormatter().format(record))
    assert payload['event'] == 'sync_chunk_completed'
    assert payload['user_id'] == 'user-1'
    assert payload['phase'] == 'activities'
    assert payload['duration_ms'] == 123
    assert payload['rows_imported'] == 4
    assert 'password' not in payload
    assert 'token' not in payload
    assert 'must-not-appear' not in json.dumps(payload)
