import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from garminconnect import GarminConnectTooManyRequestsError

from garmin_mcp.stride_sync import ActivitySyncWorker
from garmin_mcp.stride_thresholds import (HEART_RATE_ZONES_URL, normalize_heart_rate_zones,
                                          normalize_lactate_threshold,
                                          normalize_race_predictions)


def zone_profile(sport, **overrides):
    profile = {'sport': sport, 'trainingMethod': 'HR_MAX', 'restingHeartRateUsed': 48,
               'lactateThresholdHeartRateUsed': 171, 'zone1Floor': 98, 'zone2Floor': 118,
               'zone3Floor': 137, 'zone4Floor': 157, 'zone5Floor': 176,
               'maxHeartRateUsed': 196, 'restingHrAutoUpdateUsed': True,
               'changeState': 'UNCHANGED'}
    profile.update(overrides)
    return profile


def test_heart_rate_zones_keep_each_sport_profile():
    profiles = normalize_heart_rate_zones([zone_profile('DEFAULT'),
                                           zone_profile('running', zone5Floor=180)])
    assert [profile['sport'] for profile in profiles] == ['DEFAULT', 'RUNNING']
    running = profiles[1]
    assert running['training_method'] == 'HR_MAX'
    assert [running[f'zone{index}_floor_bpm'] for index in range(1, 6)] == [
        98, 118, 137, 157, 180]
    assert running['max_heart_rate_bpm'] == 196
    assert running['resting_heart_rate_bpm'] == 48
    assert running['lactate_threshold_heart_rate_bpm'] == 171
    assert running['resting_heart_rate_auto_update'] is True


@pytest.mark.parametrize('response', [None, {}, 'invalid', [None, 'x', {'sport': ''}]])
def test_heart_rate_zones_ignore_invalid_responses(response):
    assert normalize_heart_rate_zones(response) == []


def test_heart_rate_zones_store_missing_or_implausible_values_as_missing():
    [profile] = normalize_heart_rate_zones([zone_profile(
        'RUNNING', zone1Floor=None, maxHeartRateUsed=0, restingHeartRateUsed=999,
        trainingMethod=None, restingHrAutoUpdateUsed='yes')])
    assert profile['zone1_floor_bpm'] is None
    assert profile['max_heart_rate_bpm'] is None
    assert profile['resting_heart_rate_bpm'] is None
    assert profile['training_method'] is None
    assert profile['resting_heart_rate_auto_update'] is None


def test_lactate_threshold_history_converts_garmin_speed_to_metres_per_second():
    rows = normalize_lactate_threshold({
        'speed': [{'from': '2026-05-03', 'until': '2026-05-03', 'series': 'running',
                   'value': 0.38, 'updatedDate': '2026-05-03'},
                  {'from': '2026-08-21', 'until': '2026-08-21', 'series': 'running',
                   'value': 0.3888889, 'updatedDate': '2026-08-21'}],
        'heart_rate': [{'from': '2026-08-21', 'until': '2026-08-21', 'series': 'running',
                        'value': 171.0, 'updatedDate': '2026-08-21'}],
        'power': [{'from': '2026-08-21', 'series': 'running', 'value': 310.0}],
    })
    values = {(row['metric'], row['observed_on']): row for row in rows}
    assert values[('lactate_threshold_speed_mps', '2026-05-03')]['value_numeric'] == 3.8
    assert values[('lactate_threshold_speed_mps', '2026-08-21')]['value_numeric'] == 3.8889
    heart_rate = values[('lactate_threshold_heart_rate_bpm', '2026-08-21')]
    assert heart_rate['value_numeric'] == 171
    assert heart_rate['unit'] == 'bpm'
    assert heart_rate['domain'] == 'thresholds'
    assert heart_rate['availability'] == 'available'
    assert len(rows) == 3


@pytest.mark.parametrize('entry', [
    {'from': '2026-08-21', 'series': 'cycling', 'value': 0.4},
    {'from': '2026-08-21', 'series': 'running', 'value': 0},
    {'from': '2026-08-21', 'series': 'running', 'value': None},
    {'from': None, 'series': 'running', 'value': 0.4},
    {'from': 'soon', 'series': 'running', 'value': 0.4},
    'invalid',
])
def test_lactate_threshold_skips_unusable_entries(entry):
    assert normalize_lactate_threshold({'speed': [entry]}) == []


def test_race_predictions_merge_history_and_latest_without_duplicates():
    today = datetime.now(timezone.utc).date()
    history = [{'calendarDate': '2026-09-30', 'time5K': 1260, 'time10K': 2640,
                'timeHalfMarathon': 5880, 'timeMarathon': 12600},
               {'calendarDate': today.isoformat(), 'time5K': 1270, 'time10K': None,
                'timeHalfMarathon': 5890, 'timeMarathon': 12650}]
    latest = {'calendarDate': today.isoformat(), 'time5K': 1250, 'time10K': 2620,
              'timeHalfMarathon': 5850, 'timeMarathon': 0}
    rows = normalize_race_predictions(history, latest)
    values = {(row['metric'], row['observed_on']): row for row in rows}
    assert values[('race_prediction_5k_seconds', '2026-09-30')]['value_numeric'] == 1260
    assert values[('race_prediction_5k_seconds', today.isoformat())]['value_numeric'] == 1250
    assert values[('race_prediction_10k_seconds', today.isoformat())]['value_numeric'] == 2620
    assert values[('race_prediction_marathon_seconds', today.isoformat())][
        'value_numeric'] == 12650
    assert values[('race_prediction_5k_seconds', today.isoformat())]['availability'] == 'partial'
    assert values[('race_prediction_5k_seconds', '2026-09-30')]['availability'] == 'available'
    assert len(rows) == 8


def test_race_predictions_tolerate_missing_sources():
    assert normalize_race_predictions(None, {}, [None, {'calendarDate': None}]) == []


class ThresholdDatabase:
    def __init__(self, job):
        self.job = job
        self.zones = None
        self.observations = []
        self.stale = False

    def request(self, method, path, *, params=None, data=None, prefer=None):
        if path == 'rpc/claim_sync_job':
            if self.job['state'] != 'queued':
                return []
            self.job['state'] = 'running'
            self.job['attempts'] += 1
            return [dict(self.job)]
        if path in ('rpc/replace_heart_rate_zones', 'rpc/upsert_provider_observations'):
            if self.stale or data['p_attempts'] != self.job['attempts']:
                return False
            if path == 'rpc/replace_heart_rate_zones':
                self.zones = data['p_zones']
            else:
                self.observations.extend(data['p_observations'])
            return True
        if path == 'sync_jobs':
            if (params.get('attempts') != f"eq.{self.job['attempts']}"
                    or params.get('state') != 'eq.running'):
                return []
            self.job.update(data)
            return [dict(self.job)] if prefer else None
        if path == 'provider_connections':
            return None
        raise AssertionError(path)


class ThresholdSessions:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.calls = []

    def for_user(self, _user_id, load_profile=False):
        assert load_profile
        return self

    def persist(self, _user_id, _client):
        pass

    def _call(self, name, value):
        self.calls.append(name)
        if name in self.failures:
            raise self.failures[name]
        return value

    def connectapi(self, path):
        assert path == HEART_RATE_ZONES_URL
        return self._call('heart_rate_zones', [zone_profile('RUNNING')])

    def get_lactate_threshold(self, *, latest, start_date, end_date, aggregation):
        assert latest is False and aggregation == 'daily'
        assert date.fromisoformat(end_date) - date.fromisoformat(start_date) <= timedelta(days=364)
        return self._call('lactate_threshold', {'speed': [
            {'from': '2026-08-21', 'series': 'running', 'value': 0.38}]})

    def get_race_predictions(self, startdate=None, enddate=None, _type=None):
        if _type is None:
            return self._call('race_predictions', {'calendarDate': '2026-10-12',
                                                   'time5K': 1250})
        assert _type == 'monthly'
        return self._call('race_prediction_history', [{'calendarDate': '2026-09-30',
                                                       'time5K': 1260}])


def threshold_job(mode='historical', oldest=None):
    return {'id': str(uuid.uuid4()), 'connection_id': str(uuid.uuid4()),
            'user_id': str(uuid.uuid4()), 'state': 'queued', 'mode': mode,
            'phase': 'thresholds', 'cursor_date': None,
            'oldest_date': oldest or '2020-01-01', 'attempts': 0, 'failure_count': 0}


def test_historical_threshold_job_imports_zones_history_and_predictions():
    job = threshold_job()
    database = ThresholdDatabase(job)
    sessions = ThresholdSessions()
    assert ActivitySyncWorker(database, sessions).run_once()
    assert job['state'] == 'succeeded' and job['last_success_at']
    assert database.zones[0]['sport'] == 'RUNNING'
    metrics = {(row['metric'], row['observed_on']) for row in database.observations}
    assert metrics == {('lactate_threshold_speed_mps', '2026-08-21'),
                       ('race_prediction_5k_seconds', '2026-10-12'),
                       ('race_prediction_5k_seconds', '2026-09-30')}
    oldest = date.fromisoformat(job['oldest_synchronized_date'])
    assert oldest == datetime.now(timezone.utc).date() - timedelta(days=364)


def test_incremental_threshold_job_skips_prediction_history():
    job = threshold_job('incremental', datetime.now(timezone.utc).date().isoformat())
    sessions = ThresholdSessions()
    assert ActivitySyncWorker(ThresholdDatabase(job), sessions).run_once()
    assert 'race_prediction_history' not in sessions.calls
    assert job['state'] == 'succeeded'


def test_unavailable_zone_source_keeps_existing_zones():
    job = threshold_job()
    database = ThresholdDatabase(job)
    sessions = ThresholdSessions({'heart_rate_zones': RuntimeError('404')})
    assert ActivitySyncWorker(database, sessions).run_once()
    assert database.zones is None
    assert database.observations and job['state'] == 'succeeded'


def test_threshold_job_retries_on_rate_limit_or_when_every_source_fails():
    job = threshold_job()
    database = ThresholdDatabase(job)
    sessions = ThresholdSessions({'lactate_threshold': GarminConnectTooManyRequestsError('429')})
    assert ActivitySyncWorker(database, sessions).run_once()
    assert job['state'] == 'queued' and job['last_error_code'] == 'rate_limited'
    assert database.zones is None and not database.observations

    job = threshold_job()
    database = ThresholdDatabase(job)
    failures = {name: RuntimeError('500') for name in (
        'heart_rate_zones', 'lactate_threshold', 'race_predictions', 'race_prediction_history')}
    assert ActivitySyncWorker(database, ThresholdSessions(failures)).run_once()
    assert job['state'] == 'queued' and job['last_error_code'] == 'provider_unavailable'


def test_stale_threshold_write_does_not_advance_the_job():
    job = threshold_job()
    database = ThresholdDatabase(job)
    database.stale = True
    assert ActivitySyncWorker(database, ThresholdSessions()).run_once()
    assert job['state'] == 'running'
