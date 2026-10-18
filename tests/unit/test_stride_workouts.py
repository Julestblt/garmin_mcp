import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from garminconnect import GarminConnectConnectionError
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from garmin_mcp.stride_app import register_routes
from garmin_mcp.stride_garmin import ConnectionError
from garmin_mcp.stride_workouts import (PlannedWorkout, WorkoutPushService, WorkoutRequestError,
                                        garmin_workout, parse_reconcile, parse_workout)


TODAY = date(2026, 10, 7)
WARMUP = {'type': 'warmup', 'end': {'kind': 'time', 'seconds': 900},
          'target': {'kind': 'heart_rate_range', 'minBpm': 128, 'maxBpm': 148},
          'intensity': 'easy', 'cue': 'Finis vers 5:30'}
THRESHOLD = {'type': 'repeat', 'count': 4, 'steps': [
    {'type': 'work', 'end': {'kind': 'time', 'seconds': 360},
     'target': {'kind': 'pace_range', 'fastSecondsPerKm': 278, 'slowSecondsPerKm': 282},
     'intensity': 'threshold'},
    {'type': 'recovery', 'end': {'kind': 'time', 'seconds': 120},
     'target': {'kind': 'none'}, 'intensity': 'recovery'}]}
COOLDOWN = {'type': 'cooldown', 'end': {'kind': 'lap_button', 'estimatedSeconds': 600},
            'target': {'kind': 'none'}, 'intensity': 'easy'}
HILL = {'type': 'work', 'end': {'kind': 'distance', 'meters': 400},
        'target': {'kind': 'rpe', 'min': 7, 'max': 8}, 'intensity': 'interval'}


def _item(key='plan:s2-wed', day='2026-10-07', steps=None, **flags):
    return {'key': key, 'date': day, 'name': 'Seuil · 4 × 6’',
            'description': 'Développer l’allure seuil.',
            'steps': steps or [WARMUP, THRESHOLD, COOLDOWN], **flags}


def _workout(**kwargs):
    return parse_workout(_item(**kwargs))


def test_maps_a_stride_session_to_a_garmin_running_workout():
    payload, simplifications = garmin_workout(_workout())

    assert simplifications == []
    assert payload['workoutName'] == 'Stride · Seuil · 4 × 6’'
    assert payload['description'] == 'Développer l’allure seuil.'
    assert payload['sportType'] == {'sportTypeId': 1, 'sportTypeKey': 'running'}
    warmup, repeat, cooldown = payload['workoutSegments'][0]['workoutSteps']
    assert warmup['stepType']['stepTypeKey'] == 'warmup'
    assert (warmup['endCondition']['conditionTypeKey'], warmup['endConditionValue']) == ('time', 900.0)
    assert (warmup['targetValueOne'], warmup['targetValueTwo']) == (128.0, 148.0)
    assert warmup['description'] == 'Finis vers 5:30'
    assert repeat['type'] == 'RepeatGroupDTO'
    assert repeat['numberOfIterations'] == 4
    assert repeat['endCondition'] == {'conditionTypeId': 7, 'conditionTypeKey': 'iterations'}
    work, recovery = repeat['workoutSteps']
    assert work['targetType']['workoutTargetTypeKey'] == 'pace.zone'
    assert work['targetValueOne'] < work['targetValueTwo']
    assert (work['targetValueOne'], work['targetValueTwo']) == (3.5461, 3.5971)
    assert recovery['targetType']['workoutTargetTypeKey'] == 'no.target'
    assert [step['stepOrder'] for step in (warmup, repeat, work, recovery, cooldown)] == [1, 2, 3, 4, 5]
    assert cooldown['endCondition']['conditionTypeKey'] == 'lap.button'
    assert 'endConditionValue' not in cooldown


def test_rpe_targets_become_untargeted_steps_and_are_reported():
    payload, simplifications = garmin_workout(_workout(steps=[HILL]))

    step = payload['workoutSegments'][0]['workoutSteps'][0]
    assert simplifications == ['rpe_target']
    assert step['targetType']['workoutTargetTypeKey'] == 'no.target'
    assert step['description'] == 'Effort 7–8/10'
    assert (step['endCondition']['conditionTypeKey'], step['endConditionValue']) == ('distance', 400.0)


def test_content_hash_follows_content_and_ignores_flags():
    assert _workout().content_hash == _workout(force=True, allow_simplified=True).content_hash
    assert _workout().content_hash != _workout(day='2026-10-08').content_hash


@pytest.mark.parametrize('body', [
    {'from': '2026-10-07', 'to': '2026-10-13', 'workouts': [_item(extra=True)]},
    {'from': '2026-10-07', 'to': '2026-10-13', 'workouts': [_item(day='2026-10-14')]},
    {'from': '2026-10-07', 'to': '2026-11-07', 'workouts': []},
    {'from': '2026-10-05', 'to': '2026-10-13', 'workouts': []},
    {'from': '2026-10-07', 'to': '2026-10-13', 'workouts': [_item(), _item()]},
    {'from': '2026-10-07', 'to': '2026-10-13', 'workouts': [_item(key='../x')]},
    {'from': '2026-10-07', 'to': '2026-10-13', 'workouts': [_item(steps=[
        {**HILL, 'target': {'kind': 'pace_range', 'fastSecondsPerKm': 300,
                            'slowSecondsPerKm': 280}}])]},
    {'from': '2026-10-07', 'to': '2026-10-13', 'workouts': [_item(force='yes')]},
    {'from': '2026-10-07', 'to': '2026-10-13'},
])
def test_rejects_requests_outside_the_contract(body):
    with pytest.raises(WorkoutRequestError):
        parse_reconcile(body, TODAY)


def test_accepts_a_window_starting_yesterday_for_time_zones():
    start, end, workouts = parse_reconcile(
        {'from': '2026-10-06', 'to': '2026-10-12', 'workouts': [_item()]}, TODAY)

    assert (start, end, [workout.key for workout in workouts]) == (
        date(2026, 10, 6), date(2026, 10, 12), ['plan:s2-wed'])


class Client:
    def __init__(self):
        self.calls = []
        self.next_id = 100
        self.failures = {}

    def _maybe_fail(self, name):
        failure = self.failures.get(name)
        if failure:
            raise failure

    def upload_workout(self, payload):
        self.calls.append(('upload', payload['workoutName']))
        self._maybe_fail('upload')
        self.next_id += 1
        return {'workoutId': self.next_id}

    def schedule_workout(self, workout_id, day):
        self.calls.append(('schedule', workout_id, day))
        self._maybe_fail('schedule')
        return {'workoutScheduleId': workout_id * 10}

    def delete_workout(self, workout_id):
        self.calls.append(('delete', workout_id))
        self._maybe_fail('delete')


class Sessions:
    def __init__(self, status='connected'):
        self.client = Client()
        self.persisted = 0
        self.connections = SimpleNamespace(get=lambda _user: {'id': 'connection', 'status': status})

    def for_user(self, _user_id):
        return self.client

    def persist(self, _user_id, client):
        assert client is self.client
        self.persisted += 1


class Repository:
    def __init__(self):
        self.rows = {}

    def upcoming(self, _user_id, start):
        return [dict(row) for row in self.rows.values() if row['scheduled_on'] >= start.isoformat()]

    def save(self, _user_id, connection_id, row):
        assert connection_id == 'connection'
        self.rows[row['external_key']] = {'error_code': None, 'sent_at': None, **row}

    def delete(self, _user_id, key=None):
        if key is None:
            self.rows.clear()
        else:
            del self.rows[key]


def _service(status='connected'):
    sessions, repository = Sessions(status), Repository()
    return WorkoutPushService(sessions, repository), sessions, repository


def _reconcile(service, *workouts, start=TODAY, end=date(2026, 10, 13)):
    return service.reconcile(uuid.uuid4(), start, end, list(workouts))


def test_sends_new_workouts_and_leaves_unchanged_ones_alone():
    service, sessions, repository = _service()

    first = _reconcile(service, _workout())
    second = _reconcile(service, _workout())

    assert sessions.client.calls == [('upload', 'Stride · Seuil · 4 × 6’'),
                                     ('schedule', 101, '2026-10-07')]
    assert first['workouts'][0] == {'key': 'plan:s2-wed', 'date': '2026-10-07',
                                    'simplifications': [], 'status': 'sent', 'changed': True}
    assert second['workouts'][0]['changed'] is False
    assert repository.rows['plan:s2-wed']['provider_workout_id'] == 101
    assert repository.rows['plan:s2-wed']['sent_at'] is not None
    assert sessions.persisted == 2


def test_force_replaces_the_workout_without_a_duplicate():
    service, sessions, repository = _service()
    _reconcile(service, _workout())

    _reconcile(service, _workout(force=True))

    assert sessions.client.calls[2:] == [('delete', 101), ('upload', 'Stride · Seuil · 4 × 6’'),
                                         ('schedule', 102, '2026-10-07')]
    assert repository.rows['plan:s2-wed']['provider_workout_id'] == 102


def test_moves_changed_workouts_and_removes_unwanted_ones_in_the_window():
    service, sessions, repository = _service()
    _reconcile(service, _workout(), _workout(key='plan:s2-thu', day='2026-10-08'),
               _workout(key='plan:s3-sat', day='2026-10-17'), end=date(2026, 10, 20))
    sessions.client.calls.clear()

    result = _reconcile(service, _workout(day='2026-10-09'))

    assert result['removed'] == ['plan:s2-thu']
    assert sessions.client.calls == [('delete', 102), ('delete', 101),
                                     ('upload', 'Stride · Seuil · 4 × 6’'),
                                     ('schedule', 104, '2026-10-09')]
    assert set(repository.rows) == {'plan:s2-wed', 'plan:s3-sat'}
    assert repository.rows['plan:s2-wed']['scheduled_on'] == '2026-10-09'


def test_holds_simplified_workouts_until_the_athlete_confirms():
    service, sessions, repository = _service()

    held = _reconcile(service, _workout(steps=[HILL]))
    again = _reconcile(service, _workout(steps=[HILL]))
    confirmed = _reconcile(service, _workout(steps=[HILL], allow_simplified=True))

    assert held['workouts'][0]['status'] == 'needs_confirmation'
    assert held['workouts'][0]['simplifications'] == ['rpe_target']
    assert again['workouts'][0]['changed'] is False
    assert confirmed['workouts'][0]['status'] == 'sent'
    assert sessions.client.calls == [('upload', 'Stride · Seuil · 4 × 6’'),
                                     ('schedule', 101, '2026-10-07')]
    assert repository.rows['plan:s2-wed']['simplifications'] == ['rpe_target']


@pytest.mark.parametrize(('message', 'code'), [('API Error 500 - boom', 'provider_unavailable'),
                                               ('API Error 400 - invalid step', 'unsupported_data')])
def test_records_failures_per_workout_and_keeps_going(message, code):
    service, sessions, repository = _service()
    sessions.client.failures['upload'] = GarminConnectConnectionError(message)

    result = _reconcile(service, _workout(), _workout(key='plan:s2-thu', day='2026-10-08'))

    assert [item['status'] for item in result['workouts']] == ['failed', 'failed']
    assert [call[0] for call in sessions.client.calls] == ['upload', 'upload']
    assert repository.rows['plan:s2-wed']['error_code'] == code
    assert repository.rows['plan:s2-wed']['provider_workout_id'] is None


def test_rate_limiting_stops_calling_garmin_for_the_rest():
    service, sessions, repository = _service()
    sessions.client.failures['upload'] = GarminConnectConnectionError('API Error 429 - slow down')

    result = _reconcile(service, _workout(), _workout(key='plan:s2-thu', day='2026-10-08'))

    assert [item.get('error_code') for item in result['workouts']] == ['rate_limited'] * 2
    assert len(sessions.client.calls) == 1


def test_schedule_failure_deletes_the_uploaded_template():
    service, sessions, repository = _service()
    sessions.client.failures['schedule'] = GarminConnectConnectionError('API Error 503')

    result = _reconcile(service, _workout())

    assert sessions.client.calls[-1] == ('delete', 101)
    assert result['workouts'][0]['status'] == 'failed'


def test_a_workout_already_deleted_in_garmin_counts_as_removed():
    service, sessions, repository = _service()
    _reconcile(service, _workout())
    sessions.client.failures['delete'] = GarminConnectConnectionError('API Error 404 - gone')

    result = _reconcile(service)

    assert result['removed'] == ['plan:s2-wed']
    assert repository.rows == {}


def test_reconnect_required_aborts_the_reconcile():
    service, sessions, _ = _service()
    sessions.client.failures['upload'] = ConnectionError('reconnect_required', 409)

    with pytest.raises(ConnectionError) as error:
        _reconcile(service, _workout())

    assert error.value.code == 'reconnect_required'
    assert sessions.persisted == 1


@pytest.mark.parametrize(('status', 'code'), [('reconnect_required', 'reconnect_required'),
                                              ('disconnected', 'disconnected')])
def test_refuses_to_push_without_a_connected_account(status, code):
    service, sessions, _ = _service(status)

    with pytest.raises(ConnectionError) as error:
        _reconcile(service, _workout())

    assert (error.value.code, sessions.client.calls) == (code, [])


def test_remove_upcoming_deletes_only_future_stride_workouts():
    service, sessions, repository = _service()
    _reconcile(service, _workout(), _workout(key='plan:s2-thu', day='2026-10-08'))
    sessions.client.calls.clear()

    removed = service.remove_upcoming(uuid.uuid4(), date(2026, 10, 8))

    assert removed == 1
    assert sessions.client.calls == [('delete', 102)]
    assert set(repository.rows) == {'plan:s2-wed'}


def test_forget_drops_records_even_when_garmin_cannot_be_reached():
    service, sessions, repository = _service()
    _reconcile(service, _workout())
    sessions.client.failures['delete'] = GarminConnectConnectionError('API Error 503')

    service.forget(uuid.uuid4(), TODAY)

    assert repository.rows == {}


def test_forget_never_blocks_a_disconnect():
    service, _, repository = _service()
    repository.upcoming = lambda *_args: (_ for _ in ()).throw(RuntimeError('storage down'))
    repository.delete = lambda *_args: (_ for _ in ()).throw(RuntimeError('storage down'))

    service.forget(uuid.uuid4(), TODAY)


class Verifier:
    def __init__(self, user_id):
        self.user_id = user_id

    async def verify_token(self, token):
        return AccessToken(token=token, client_id=str(self.user_id), subject=str(self.user_id),
                           scopes=['stride:garmin']) if token == 'valid' else None


def _http(components, user_id):
    app = FastMCP('test', token_verifier=Verifier(user_id),
                  auth=AuthSettings(issuer_url='https://example.supabase.co',
                                    resource_server_url=None),
                  stateless_http=True)
    register_routes(app, components)
    return TestClient(app.streamable_http_app())


def test_http_reconcile_validates_the_body_and_returns_the_result(monkeypatch):
    user_id = uuid.uuid4()
    calls = []

    class Workouts:
        def reconcile(self, actual_user, start, end, workouts):
            calls.append((actual_user, start, end, [workout.key for workout in workouts]))
            return {'workouts': [], 'removed': []}

    monkeypatch.setattr('garmin_mcp.stride_app.datetime',
                        SimpleNamespace(now=lambda _tz: SimpleNamespace(date=lambda: TODAY)))
    headers = {'Authorization': 'Bearer valid'}
    with _http(SimpleNamespace(workouts=Workouts()), user_id) as client:
        invalid = client.put('/v1/connections/garmin/workouts', headers=headers,
                             json={'from': '2026-10-07', 'to': '2026-10-13'})
        valid = client.put('/v1/connections/garmin/workouts', headers=headers,
                           json={'from': '2026-10-07', 'to': '2026-10-13',
                                 'workouts': [_item()]})
        anonymous = client.put('/v1/connections/garmin/workouts', json={})

    assert (invalid.status_code, invalid.json()) == (400, {'error': {'code': 'invalid_request'}})
    assert (valid.status_code, valid.json()) == (200, {'workouts': [], 'removed': []})
    assert anonymous.status_code == 401
    assert calls == [(user_id, TODAY, date(2026, 10, 13), ['plan:s2-wed'])]


def test_http_remove_and_disconnect_clear_upcoming_workouts_first():
    user_id = uuid.uuid4()
    events = []
    workouts = SimpleNamespace(
        remove_upcoming=lambda user, _today: events.append(('remove', user)) or 3,
        forget=lambda user, _today: events.append(('forget', user)))
    service = SimpleNamespace(disconnect=lambda user: events.append(('disconnect', user)))
    headers = {'Authorization': 'Bearer valid'}
    with _http(SimpleNamespace(workouts=workouts, service=service), user_id) as client:
        removed = client.delete('/v1/connections/garmin/workouts', headers=headers)
        disconnected = client.delete('/v1/connections/garmin', headers=headers)

    assert (removed.status_code, removed.json()) == (200, {'removed': 3})
    assert disconnected.status_code == 204
    assert events == [('remove', user_id), ('forget', user_id), ('disconnect', user_id)]


def test_planned_workout_keeps_flags_out_of_the_payload():
    payload, _ = garmin_workout(PlannedWorkout('k', TODAY, 'Footing', '', [WARMUP],
                                               allow_simplified=True, force=True))

    assert 'description' not in payload
    assert set(payload) == {'workoutName', 'sportType', 'workoutSegments'}
