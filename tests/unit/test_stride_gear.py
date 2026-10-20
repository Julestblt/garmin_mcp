import uuid
from types import SimpleNamespace

import pytest
from garminconnect import GarminConnectConnectionError
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from garmin_mcp.stride_app import register_routes
from garmin_mcp.stride_garmin import ConnectionError
from garmin_mcp.stride_gear import (GearRepository, GearRequestError, GearService, GearSnapshot,
                                    dashed, gear_uuid, normalize_gear, parse_activity_gear,
                                    parse_create, parse_update)


USER = uuid.UUID('11111111-1111-1111-1111-111111111111')
EVO = 'a1b2c3d4e5f60718293a4b5c6d7e8f90'
ZEGAMA = '0f1e2d3c4b5a69788796a5b4c3d2e1f0'


def _legacy(key, name, status='active', kind='Shoes', maximum=0):
    return {'uuid': key, 'gearTypeName': kind, 'gearStatusName': status,
            'gearMakeName': name.split()[0], 'gearModelName': ' '.join(name.split()[1:]),
            'displayName': name, 'dateBegin': '2026-06-02T00:00:00.0',
            'dateEnd': '2026-09-01T00:00:00.0' if status == 'retired' else None,
            'maximumMeters': maximum}


class Client:
    def __init__(self):
        self.gear = {EVO: _legacy(EVO, 'Adidas Adizero Evo SL 3', maximum=388000),
                     ZEGAMA: _legacy(ZEGAMA, 'Nike Zegama'),
                     'ffffffffffffffffffffffffffffffff': _legacy(
                         'ffffffffffffffffffffffffffffffff', 'Canyon Ultimate', kind='Bike')}
        self.stats = {EVO: {'totalDistance': 182000.0, 'totalActivities': 21},
                      ZEGAMA: {'totalDistance': 241354.0, 'totalActivities': 30}}
        self.activities = {EVO: ['101', '102'], ZEGAMA: ['103']}
        self.calls = []
        self.fail = None

    def get_device_last_used(self):
        return {'userProfileNumber': 42}

    def get_gear(self, profile):
        assert profile == 42
        return list(self.gear.values())

    def get_gear_stats(self, key):
        self.calls.append(('stats', key))
        return self.stats.get(key, {})

    def get_gear_details(self, key):
        self.calls.append(('details', key))
        gear = self.gear[key.replace('-', '')]
        brand, model = getattr(self, 'created', (gear['gearMakeName'], gear['gearModelName'])) if key.replace('-', '') == '9' * 32 else (gear['gearMakeName'], gear['gearModelName'])
        return {'brand': brand, 'model': model, 'name': gear['displayName']}

    def get_gear_activities(self, key, limit):
        self.calls.append(('activities', key))
        return [{'activityId': int(item)} for item in self.activities.get(key, [])]

    def create_gear(self, gear_type, brand, model, name, first_use_date, **options):
        self.calls.append(('create', gear_type, brand, model, name, first_use_date, options))
        if self.fail:
            raise self.fail
        key = '9' * 32
        self.gear[key] = {**_legacy(key, name), 'gearMakeName': 'Other', 'gearModelName': 'Unknown Shoes'}
        self.created = (brand, model)
        return {'uuid': dashed(key), 'gearType': 'SHOES'}

    def update_gear(self, key, **changes):
        self.calls.append(('update', key, changes))

    def set_gear_status(self, key, status):
        self.calls.append(('status', key, status))
        self.gear[key.replace('-', '')]['gearStatusName'] = status.lower()

    def get_activity_gear(self, activity_id):
        return [self.gear[key] for key, ids in self.activities.items() if activity_id in ids]

    def remove_gear_from_activity(self, key, activity_id):
        self.calls.append(('unlink', key, activity_id))
        self.activities[key].remove(activity_id)

    def add_gear_to_activity(self, key, activity_id):
        self.calls.append(('link', key, activity_id))
        if activity_id == '104':
            raise GarminConnectConnectionError(
                "API Error 400 - Gear's start date must be before activity's start date")
        self.activities.setdefault(key, []).append(activity_id)


class Sessions:
    def __init__(self, status='connected'):
        self.client = Client()
        self.persisted = 0
        self.connections = SimpleNamespace(get=lambda _user: {'id': 'connection', 'status': status})

    def for_user(self, _user_id):
        return self.client

    def persist(self, _user_id, client):
        self.persisted += 1


class Database:
    def __init__(self):
        self.gear = {}
        self.links = {}
        self.activities = {'101', '102', '103', '104'}
        self.requests = []

    def request(self, method, path, params=None, data=None, prefer=None):
        self.requests.append((method, path))
        params = params or {}
        if method == 'GET' and path == 'provider_gear':
            return [dict(row) for row in self.gear.values()]
        if method == 'GET' and path == 'provider_activities':
            return [{'id': 'x'}] if params['provider_activity_id'][3:] in self.activities else []
        if method == 'POST' and path == 'provider_gear':
            for row in data:
                self.gear[row['gear_uuid']] = row
            return None
        if method == 'POST' and path == 'provider_activity_gear':
            for row in data:
                self.links[row['provider_activity_id']] = row['gear_uuid']
            return None
        if method == 'DELETE':
            target = self.gear if path == 'provider_gear' else self.links
            for key, value in list(target.items()):
                gear = key if path == 'provider_gear' else value
                activity = None if path == 'provider_gear' else key
                if 'gear_uuid' in params:
                    rule = params['gear_uuid']
                    kept = rule[8:-1].split(',') if rule.startswith('not.in.') else None
                    if kept is not None and gear in kept:
                        continue
                    if rule.startswith('eq.') and gear != rule[3:]:
                        continue
                if 'provider_activity_id' in params and activity in params[
                        'provider_activity_id'][8:-1].split(','):
                    continue
                del target[key]
            return None
        raise AssertionError((method, path))


def _service(status='connected'):
    sessions, database = Sessions(status), Database()
    return GearService(sessions, GearRepository(database)), sessions, database


def _sync(service, sessions):
    snapshot = service.collect(sessions.client, USER, 'connection')
    service.repository.store(USER, 'connection', snapshot)
    return snapshot


def test_uuids_accept_both_garmin_forms():
    assert gear_uuid('A1B2C3D4-E5F6-0718-293A-4B5C6D7E8F90') == EVO
    assert gear_uuid(EVO) == EVO
    assert dashed(EVO) == 'a1b2c3d4-e5f6-0718-293a-4b5c6d7e8f90'
    with pytest.raises(GearRequestError):
        gear_uuid('not-a-uuid')


def test_normalizes_shoes_and_skips_other_gear():
    row = normalize_gear(USER, 'connection', _legacy(EVO, 'Adidas Adizero Evo SL 3', maximum=388000),
                         {'totalDistance': 182000.0, 'totalActivities': 21})
    retired = normalize_gear(USER, 'connection', _legacy(ZEGAMA, 'Nike Zegama', status='retired'), {})

    assert {key: row[key] for key in ('gear_uuid', 'brand', 'model', 'status', 'first_use_date',
                                      'maximum_meters', 'total_distance_meters', 'total_activities')} == {
        'gear_uuid': EVO, 'brand': 'Adidas', 'model': 'Adizero Evo SL 3', 'status': 'active',
        'first_use_date': '2026-06-02', 'maximum_meters': 388000.0,
        'total_distance_meters': 182000.0, 'total_activities': 21}
    assert (retired['status'], retired['retired_on'], retired['maximum_meters'],
            retired['total_distance_meters']) == ('retired', '2026-09-01', None, 0.0)
    assert normalize_gear(USER, 'connection', _legacy(EVO, 'Canyon Ultimate', kind='Bike'), {}) is None


def test_sync_stores_shoes_and_links_and_drops_removed_gear():
    service, sessions, database = _service()

    _sync(service, sessions)
    sessions.client.calls.clear()
    del sessions.client.gear[ZEGAMA]
    sessions.client.stats[EVO] = {'totalDistance': 182000.0, 'totalActivities': 21}
    _sync(service, sessions)

    assert set(database.gear) == {EVO}
    assert database.links == {'101': EVO, '102': EVO}
    assert ('activities', EVO) not in sessions.client.calls


def test_sync_refreshes_links_when_totals_change():
    service, sessions, database = _service()
    _sync(service, sessions)

    sessions.client.activities[EVO].append('104')
    sessions.client.stats[EVO] = {'totalDistance': 190000.0, 'totalActivities': 22}
    _sync(service, sessions)

    assert database.links['104'] == EVO
    assert database.gear[EVO]['total_distance_meters'] == 190000.0


def test_parses_create_update_and_activity_requests():
    assert parse_create({'brand': 'HOKA', 'model': 'Speedgoat 7', 'name': 'Speedgoat 7',
                         'first_use_date': '2026-10-20', 'max_distance_meters': 700000,
                         'default_activity_types': ['trail_running']}) == {
        'brand': 'HOKA', 'model': 'Speedgoat 7', 'name': 'Speedgoat 7',
        'first_use_date': '2026-10-20', 'max_distance_meters': 700000,
        'default_activity_types': ['trail_running']}
    assert parse_update({'max_distance_meters': 0, 'status': 'retired'}) == {
        'max_distance_meters': 0, 'status': 'retired'}
    assert parse_activity_gear('20598229037', {'gear_uuid': dashed(EVO)}) == ('20598229037', EVO)
    assert parse_activity_gear('20598229037', {'gear_uuid': None}) == ('20598229037', None)
    for invalid in ({'brand': 'HOKA'}, {'brand': '', 'model': 'x', 'name': 'x', 'first_use_date': '2026-10-20'},
                    {'brand': 'x', 'model': 'x', 'name': 'x', 'first_use_date': '20/10/2026'},
                    {'brand': 'x', 'model': 'x', 'name': 'x', 'first_use_date': '2026-10-20',
                     'default_activity_types': ['cycling']}):
        with pytest.raises(GearRequestError):
            parse_create(invalid)
    for invalid in ({}, {'status': 'lost'}, {'max_distance_meters': -1}, {'color': 'red'}):
        with pytest.raises(GearRequestError):
            parse_update(invalid)
    with pytest.raises(GearRequestError):
        parse_activity_gear('abc', {'gear_uuid': None})


def test_create_sends_a_shoe_to_garmin_and_stores_it():
    service, sessions, database = _service()

    result = service.create(USER, parse_create({
        'brand': 'HOKA', 'model': 'Speedgoat 7', 'name': 'Speedgoat 7',
        'first_use_date': '2026-10-20', 'max_distance_meters': 700000,
        'default_activity_types': ['trail_running']}))

    assert sessions.client.calls[0] == ('create', 'SHOES', 'HOKA', 'Speedgoat 7', 'Speedgoat 7',
                                        '2026-10-20', {'max_usage_distance_km': 700.0,
                                                       'notes': 'Ajoutée depuis Stride',
                                                       'activity_type_keys': ['trail_running']})
    assert result['gear']['gear_uuid'] == '9' * 32
    assert (result['gear']['brand'], result['gear']['model']) == ('HOKA', 'Speedgoat 7')
    assert set(database.gear) == {'9' * 32}
    assert sessions.persisted == 1


def test_create_maps_garmin_refusals():
    service, sessions, _database = _service()
    sessions.client.fail = GarminConnectConnectionError('API Error 403 forbidden')

    with pytest.raises(ConnectionError) as error:
        service.create(USER, parse_create({'brand': 'HOKA', 'model': 'Speedgoat 7', 'name': 'S7',
                                           'first_use_date': '2026-10-20'}))

    assert error.value.code == 'provider_unavailable'
    assert sessions.persisted == 1


def test_update_changes_limit_and_retires_known_gear_only():
    service, sessions, database = _service()
    _sync(service, sessions)

    result = service.update(USER, EVO, {'max_distance_meters': 388000, 'status': 'retired'})

    assert ('update', dashed(EVO), {'max_usage_distance_km': 388.0}) in sessions.client.calls
    assert ('status', dashed(EVO), 'RETIRED') in sessions.client.calls
    assert result['gear']['status'] == 'retired'
    assert database.gear[EVO]['status'] == 'retired'
    with pytest.raises(ConnectionError) as error:
        service.update(USER, '1' * 32, {'name': 'x'})
    assert (error.value.code, error.value.status) == ('gear_not_found', 404)


def test_set_activity_gear_moves_the_activity_to_another_pair():
    service, sessions, database = _service()
    _sync(service, sessions)

    result = service.set_activity_gear(USER, '103', EVO)

    assert ('unlink', ZEGAMA, '103') in sessions.client.calls
    assert ('link', EVO, '103') in sessions.client.calls
    assert database.links['103'] == EVO
    assert result['gear_uuid'] == EVO
    assert {row['gear_uuid'] for row in result['gear']} == {EVO, ZEGAMA}


def test_set_activity_gear_rejects_unknown_activities_and_disconnected_accounts():
    service, sessions, _database = _service()
    _sync(service, sessions)

    with pytest.raises(ConnectionError) as missing:
        service.set_activity_gear(USER, '999', EVO)
    disconnected, _, _ = _service(status='reconnect_required')
    disconnected.repository.database.gear = {EVO: {'gear_uuid': EVO}}
    with pytest.raises(ConnectionError) as reconnect:
        disconnected.set_activity_gear(USER, '101', EVO)

    assert missing.value.code == 'activity_not_found'
    assert reconnect.value.code == 'reconnect_required'


class Verifier:
    async def verify_token(self, token):
        return AccessToken(token=token, client_id=str(USER), subject=str(USER),
                           scopes=['stride:garmin']) if token == 'valid' else None


def _http(gear):
    app = FastMCP('test', token_verifier=Verifier(),
                  auth=AuthSettings(issuer_url='https://example.supabase.co',
                                    resource_server_url=None),
                  stateless_http=True)
    register_routes(app, SimpleNamespace(gear=gear))
    return TestClient(app.streamable_http_app())


def test_http_gear_routes_validate_and_map_errors():
    calls = []

    class Gear:
        def create(self, user_id, request):
            calls.append(('create', user_id, request['brand']))
            return {'gear': {'gear_uuid': EVO}}

        def update(self, user_id, key, request):
            raise ConnectionError('gear_not_found', 404)

        def set_activity_gear(self, user_id, activity_id, key):
            calls.append(('set', user_id, activity_id, key))
            return {'provider_activity_id': activity_id, 'gear_uuid': key, 'gear': []}

    headers = {'Authorization': 'Bearer valid'}
    body = {'brand': 'HOKA', 'model': 'Speedgoat 7', 'name': 'S7', 'first_use_date': '2026-10-20'}
    with _http(Gear()) as client:
        created = client.post('/v1/connections/garmin/gear', headers=headers, json=body)
        invalid = client.post('/v1/connections/garmin/gear', headers=headers, json={'brand': 'x'})
        anonymous = client.post('/v1/connections/garmin/gear', json=body)
        missing = client.patch(f'/v1/connections/garmin/gear/{EVO}', headers=headers,
                               json={'status': 'retired'})
        bad_uuid = client.patch('/v1/connections/garmin/gear/nope', headers=headers,
                                json={'status': 'retired'})
        linked = client.put('/v1/connections/garmin/activities/103/gear', headers=headers,
                            json={'gear_uuid': dashed(EVO)})

    assert (created.status_code, created.json()) == (200, {'gear': {'gear_uuid': EVO}})
    assert (invalid.status_code, invalid.json()) == (400, {'error': {'code': 'invalid_request'}})
    assert anonymous.status_code == 401
    assert (missing.status_code, missing.json()) == (404, {'error': {'code': 'gear_not_found'}})
    assert bad_uuid.status_code == 400
    assert linked.status_code == 200
    assert calls == [('create', USER, 'HOKA'), ('set', USER, '103', EVO)]


def test_snapshot_from_a_partial_refresh_keeps_other_gear():
    service, sessions, database = _service()
    _sync(service, sessions)

    snapshot = service.collect(sessions.client, USER, 'connection', only={EVO})
    service.repository.store(USER, 'connection', snapshot)

    assert isinstance(snapshot, GearSnapshot) and not snapshot.complete
    assert set(database.gear) == {EVO, ZEGAMA}


def test_unmatched_catalog_names_are_dropped_without_v2_details():
    row = normalize_gear(USER, 'connection', {**_legacy(EVO, 'Stride test'), 'gearMakeName': 'Other',
                                              'gearModelName': 'Unknown Shoes'}, {})

    assert (row['brand'], row['model'], row['display_name']) == (None, None, 'Stride test')


def test_unchanged_pairs_keep_stored_brand_without_extra_calls():
    service, sessions, database = _service()
    _sync(service, sessions)
    sessions.client.calls.clear()

    _sync(service, sessions)

    assert not any(call[0] in ('details', 'activities') for call in sessions.client.calls)
    assert database.gear[EVO]['brand'] == 'Adidas'


def test_linking_before_the_first_use_date_returns_a_clear_error():
    service, sessions, _database = _service()
    _sync(service, sessions)

    with pytest.raises(ConnectionError) as error:
        service.set_activity_gear(USER, '104', EVO)

    assert (error.value.code, error.value.status) == ('gear_starts_after_activity', 422)
