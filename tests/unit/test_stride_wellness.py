import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from garmin_mcp.stride_app import register_routes
from garmin_mcp.stride_garmin import ConnectionError
from garmin_mcp.stride_wellness import WellnessDayService, normalize_day


START = int(datetime(2024, 1, 15, 22, 0, tzinfo=timezone.utc).timestamp() * 1000)
MINUTE = 60_000


def day_payloads():
    sleep = {
        'dailySleepDTO': {
            'sleepStartTimestampGMT': START,
            'sleepEndTimestampGMT': START + 420 * MINUTE,
            'deepSleepSeconds': 3480, 'lightSleepSeconds': 12360,
            'remSleepSeconds': 5280, 'awakeSleepSeconds': 840, 'awakeCount': 1,
            'sleepScores': {'overall': {'value': 61}},
            'averageRespirationValue': 14.2,
        },
        'sleepLevels': [
            {'startGMT': '2024-01-15T22:00:00.0', 'endGMT': '2024-01-15T23:00:00.0',
             'activityLevel': 1.0},
            {'startGMT': '2024-01-15T23:00:00.0', 'endGMT': '2024-01-15T23:40:00.0',
             'activityLevel': 0.0},
            {'startGMT': '2024-01-15T23:40:00.0', 'endGMT': '2024-01-15T23:50:00.0',
             'activityLevel': 3.0},
            {'startGMT': 'invalid', 'endGMT': None, 'activityLevel': 2.0},
        ],
        'wellnessSpO2SleepSummaryDTO': {'averageSpo2': 95, 'lowestSpo2': 89},
        'avgOvernightHrv': 46,
    }
    heart = {'restingHeartRate': 47, 'heartRateValues': [
        [START, 50], [START + MINUTE, 54], [START + 2 * MINUTE, None], [START + 4 * MINUTE, 60],
        ['bad', 70],
    ]}
    stress = {
        'avgStressLevel': 34, 'maxStressLevel': 88,
        'stressValuesArray': [[START, 20], [START + MINUTE, 40], [START + 3 * MINUTE, -1]],
        'bodyBatteryValuesArray': [[START, 'MEASURED', 23, 2.0], [START + MINUTE, 'MEASURED', None, 2.0]],
    }
    steps = [
        {'startGMT': '2024-01-16T08:00:00.0', 'endGMT': '2024-01-16T08:15:00.0', 'steps': 120},
        {'startGMT': '2024-01-16T08:15:00.0', 'endGMT': '2024-01-16T08:30:00.0', 'steps': 2000},
        {'startGMT': '2024-01-16T09:00:00.0', 'endGMT': '2024-01-16T09:15:00.0', 'steps': 0},
    ]
    summary = {'totalSteps': 6050, 'dailyStepGoal': 8000, 'bodyBatteryAtWakeTime': 61,
               'bodyBatteryChargedValue': 38, 'bodyBatteryDrainedValue': 17,
               'bodyBatteryMostRecentValue': 44}
    return sleep, heart, stress, [], steps, summary


def test_day_normalizes_intraday_series_and_sleep():
    payload = normalize_day(date(2024, 1, 16), *day_payloads())
    assert payload['date'] == '2024-01-16'
    assert payload['sleep']['start'] == START
    assert [stage['stage'] for stage in payload['sleep']['stages']] == ['light', 'deep', 'awake']
    assert payload['sleep']['score'] == 61
    assert payload['sleep']['spo2_average'] == 95
    assert payload['sleep']['respiration_average'] == 14.2
    assert payload['heart_rate'] == [[START, 52.0], [START + 2 * MINUTE, None],
                                     [START + 4 * MINUTE, 60.0]]
    assert payload['stress'] == [[START, 30.0], [START + 3 * MINUTE, None]]
    assert payload['body_battery'] == [[START, 23.0]]
    hour = int(datetime(2024, 1, 16, 8, tzinfo=timezone.utc).timestamp() * 1000)
    assert payload['steps'] == [[hour, 2120], [hour + 3_600_000, 0]]
    assert payload['summary']['resting_heart_rate'] == 47
    assert payload['summary']['steps_total'] == 6050
    assert payload['summary']['body_battery_wake'] == 61


def test_day_keeps_missing_data_explicit():
    payload = normalize_day(date(2024, 1, 16), None, None, None, None, None, None)
    assert payload['sleep'] is None
    assert payload['heart_rate'] == []
    assert payload['body_battery'] == []
    assert payload['summary']['steps_total'] is None


def test_day_reads_body_battery_from_daily_report_when_stress_has_none():
    _, heart, _, _, steps, summary = day_payloads()
    battery = [{'bodyBatteryValuesArray': [[START, 40], [START + MINUTE, 41]]}]
    payload = normalize_day(date(2024, 1, 16), None, heart, {}, battery, steps, summary)
    assert payload['body_battery'] == [[START, 40.0], [START + MINUTE, 41.0]]


class FakeClient:
    def __init__(self, calls, display_name):
        self.calls = calls
        self.display_name = display_name

    def __getattr__(self, name):
        def call(day):
            if name == 'get_heart_rates':
                assert self.display_name, 'Garmin requires a display name for heart rates'
            self.calls.append((name, day))
            return None
        return call


class FakeSessions:
    def __init__(self):
        self.calls = []
        self.persisted = 0

    def for_user(self, user_id, load_profile=False):
        return FakeClient(self.calls, 'Runner' if load_profile else None)

    def persist(self, user_id, client):
        self.persisted += 1


def test_service_caches_days_and_releases_sessions():
    sessions = FakeSessions()
    now = [0.0]
    service = WellnessDayService(sessions, clock=lambda: now[0],
                                 today=lambda: date(2024, 1, 16))
    user = uuid.uuid4()
    first = service.get(user, date(2024, 1, 16))
    second = service.get(user, date(2024, 1, 16))
    assert first is second
    assert sessions.persisted == 1
    assert len(sessions.calls) == 6
    now[0] = 301
    service.get(user, date(2024, 1, 16))
    assert sessions.persisted == 2
    service.get(user, date(2024, 1, 10))
    now[0] = 3000
    service.get(user, date(2024, 1, 10))
    assert sessions.persisted == 3


def test_service_maps_provider_failures():
    class Failing(FakeSessions):
        def for_user(self, user_id, load_profile=False):
            class Client:
                def get_sleep_data(self, day):
                    raise RuntimeError('boom')
            return Client()

    sessions = Failing()
    service = WellnessDayService(sessions)
    with pytest.raises(ConnectionError) as error:
        service.get(uuid.uuid4(), date(2024, 1, 16))
    assert error.value.code == 'provider_unavailable'
    assert sessions.persisted == 1


class Verifier:
    def __init__(self, user_id):
        self.user_id = user_id

    async def verify_token(self, token):
        if token != 'valid':
            return None
        return AccessToken(token=token, client_id=str(self.user_id),
                           subject=str(self.user_id), scopes=['stride:garmin'])


def make_client(wellness, user_id):
    app = FastMCP('test', token_verifier=Verifier(user_id),
                  auth=AuthSettings(issuer_url='https://example.supabase.co',
                                    resource_server_url=None),
                  stateless_http=True)
    register_routes(app, SimpleNamespace(service=None, database=None, wellness=wellness))
    return TestClient(app.streamable_http_app())


def test_route_returns_the_day_for_the_authenticated_user():
    user_id = uuid.uuid4()
    calls = []

    class Wellness:
        def get(self, actual_user, day):
            calls.append((actual_user, day))
            return {'date': day.isoformat()}

    today = datetime.now(timezone.utc).date()
    with make_client(Wellness(), user_id) as client:
        assert client.get(f'/v1/wellness/days/{today}').status_code == 401
        response = client.get(f'/v1/wellness/days/{today}',
                              headers={'Authorization': 'Bearer valid'})
        invalid = client.get('/v1/wellness/days/not-a-date',
                             headers={'Authorization': 'Bearer valid'})
        future = client.get('/v1/wellness/days/2999-01-01',
                            headers={'Authorization': 'Bearer valid'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == {'date': today.isoformat()}
    assert calls == [(user_id, today)]
    assert invalid.json() == {'error': {'code': 'invalid_request'}}
    assert future.status_code == 400


def test_route_maps_connection_errors():
    class Wellness:
        def get(self, actual_user, day):
            raise ConnectionError('reconnect_required', 409)

    today = datetime.now(timezone.utc).date()
    with make_client(Wellness(), uuid.uuid4()) as client:
        response = client.get(f'/v1/wellness/days/{today}',
                              headers={'Authorization': 'Bearer valid'})
    assert response.status_code == 409
    assert response.json() == {'error': {'code': 'reconnect_required'}}
