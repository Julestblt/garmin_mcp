import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from garmin_mcp.stride_observations import normalize_fitness, normalize_recovery
from garmin_mcp.stride_sync import ActivitySyncWorker


@pytest.mark.parametrize('value', [0, 49, 52.5])
def test_recovery_reads_resting_hr_from_wellness_metrics_for_requested_day(value):
    day = date(2024, 1, 15)
    response = {'allMetrics': {'metricsMap': {'WELLNESS_RESTING_HEART_RATE': [
        {'calendarDate': '2024-01-14', 'value': 99},
        {'calendarDate': day.isoformat(), 'value': value},
        {'calendarDate': '2024-01-16', 'value': 88},
    ]}}}
    rows = normalize_recovery(day, None, None, response, None, None, None)
    resting = next(row for row in rows if row['metric'] == 'resting_heart_rate_bpm')
    assert resting['value_numeric'] == value
    assert resting['value_text'] is None
    assert resting['observed_on'] == day.isoformat()
    assert resting['unit'] == 'bpm'
    assert resting['availability'] == 'available'


@pytest.mark.parametrize('response', [
    None, {}, {'allMetrics': None},
    {'allMetrics': {'metricsMap': {'WELLNESS_RESTING_HEART_RATE': None}}},
    {'allMetrics': {'metricsMap': {'WELLNESS_RESTING_HEART_RATE': {}}}},
    {'allMetrics': {'metricsMap': {'WELLNESS_RESTING_HEART_RATE': [
        None, 'invalid', {'calendarDate': '2024-01-14', 'value': 55},
        {'calendarDate': '2024-01-15', 'value': None},
    ]}}},
])
def test_recovery_keeps_missing_resting_hr_when_no_matching_metric(response):
    rows = normalize_recovery(date(2024, 1, 15), None, None, response, None, None, None)
    resting = next(row for row in rows if row['metric'] == 'resting_heart_rate_bpm')
    assert resting['value_numeric'] is None
    assert resting['value_text'] is None
    assert resting['availability'] == 'missing'


def test_recovery_marks_current_day_resting_hr_as_partial():
    day = datetime.now(timezone.utc).date()
    response = {'allMetrics': {'metricsMap': {'WELLNESS_RESTING_HEART_RATE': [
        {'calendarDate': day.isoformat(), 'value': 51},
    ]}}}
    rows = normalize_recovery(day, None, None, response, None, None, None)
    resting = next(row for row in rows if row['metric'] == 'resting_heart_rate_bpm')
    assert resting['value_numeric'] == 51
    assert resting['availability'] == 'partial'


def test_recovery_keeps_zero_distinct_from_missing_and_partial():
    day = date(2024, 1, 15)
    rows = normalize_recovery(
        day, {'dailySleepDTO': {'sleepTimeSeconds': 0, 'deepSleepSeconds': None}},
        {'hrvSummary': {'lastNightAvg': 0}}, {'restingHeartRate': 0},
        {'avgStressLevel': 0}, [{'bodyBatteryMostRecentValue': 0}],
        {'score': 0, 'recoveryTime': 0})
    values = {row['metric']: row for row in rows}
    assert values['sleep_duration_seconds']['value_numeric'] == 0
    assert values['sleep_duration_seconds']['availability'] == 'available'
    assert values['sleep_deep_seconds']['value_numeric'] is None
    assert values['sleep_deep_seconds']['availability'] == 'missing'
    assert values['hrv_last_night_ms']['value_numeric'] == 0
    assert values['recovery_time_minutes']['value_numeric'] == 0
    today = datetime.now(timezone.utc).date()
    partial = normalize_recovery(today, {'dailySleepDTO': {'sleepTimeSeconds': 100}},
                                 None, None, None, None, None)
    assert next(row for row in partial if row['metric'] == 'sleep_duration_seconds')[
        'availability'] == 'partial'


def test_fitness_selects_primary_device_and_preserves_provider_values():
    rows = normalize_fitness(date(2024, 1, 15), {
        'mostRecentTrainingStatus': {'latestTrainingStatusData': {
            'secondary': {'trainingStatus': 'UNPRODUCTIVE'},
            'primary': {'primaryTrainingDevice': True, 'trainingStatus': 'PRODUCTIVE',
                        'acuteTrainingLoadDTO': {'dailyTrainingLoadAcute': 0}}}},
        'mostRecentVO2Max': {'generic': {'vo2MaxValue': 52.5}},
        'mostRecentTrainingLoadBalance': {'metricsTrainingLoadBalanceDTOMap': {
            'primary': {'primaryTrainingDevice': True, 'monthlyLoadAerobicLow': 210}}}}, {})
    values = {row['metric']: row for row in rows}
    assert values['training_status']['value_text'] == 'PRODUCTIVE'
    assert values['training_load_acute']['value_numeric'] == 0
    assert values['load_focus_low_aerobic']['value_numeric'] == 210
    assert values['vo2_max']['value_numeric'] == 52.5
    assert values['training_load_chronic']['availability'] == 'missing'


def test_recovery_worker_resumes_from_persisted_day_cursor():
    user_id = uuid.uuid4()
    day = date(2024, 1, 15)
    job = {'id': str(uuid.uuid4()), 'connection_id': str(uuid.uuid4()),
           'user_id': str(user_id), 'phase': 'recovery', 'state': 'queued',
           'cursor_date': day.isoformat(), 'oldest_date': (day - timedelta(days=1)).isoformat(),
           'attempts': 0, 'failure_count': 0}

    class Database:
        def __init__(self):
            self.observations = {}

        def request(self, method, path, *, data=None, params=None, prefer=None):
            if path == 'rpc/claim_sync_job':
                if job['state'] != 'queued':
                    return []
                job['state'] = 'running'
                job['attempts'] += 1
                return [dict(job)]
            if path == 'rpc/upsert_provider_observations':
                if data['p_attempts'] != job['attempts']:
                    return False
                for row in data['p_observations']:
                    self.observations[(row['metric'], row['observed_on'])] = row
                return True
            if path == 'sync_jobs':
                if (params.get('attempts') != f"eq.{job['attempts']}"
                        or params.get('state') != 'eq.running'):
                    return []
                job.update(data)
                return [dict(job)] if prefer else None
            if path == 'provider_connections':
                return None
            raise AssertionError(path)

    class Sessions:
        def for_user(self, requested_user, load_profile=False):
            assert requested_user == user_id and load_profile
            return self

        def persist(self, requested_user, _client):
            assert requested_user == user_id

        def get_sleep_data(self, _day):
            return {'dailySleepDTO': {'sleepTimeSeconds': 0}}

        def get_hrv_data(self, _day):
            return None

        def get_rhr_day(self, _day):
            return None

        def get_stress_data(self, _day):
            return None

        def get_body_battery(self, _day):
            return []

        def get_training_readiness(self, _day):
            return []

    database = Database()
    worker = ActivitySyncWorker(database, Sessions())
    assert worker.run_once()
    assert job['cursor_date'] == '2024-01-14'
    assert worker.run_once()
    assert job['state'] == 'succeeded'
    assert len(database.observations) == 28
