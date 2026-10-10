import math

from garmin_mcp.stride_tracks import (MAX_ROUTE_POINTS, MAX_SERIES_POINTS, normalize_track,
                                      simplify_route)

DESCRIPTORS = [
    {'metricsIndex': 0, 'key': 'directTimestamp'},
    {'metricsIndex': 1, 'key': 'sumElapsedDuration'},
    {'metricsIndex': 2, 'key': 'sumDistance'},
    {'metricsIndex': 3, 'key': 'directHeartRate'},
    {'metricsIndex': 4, 'key': 'directSpeed'},
    {'metricsIndex': 5, 'key': 'directElevation'},
    {'metricsIndex': 6, 'key': 'directDoubleCadence'},
    {'metricsIndex': 7, 'key': 'directLatitude'},
    {'metricsIndex': 8, 'key': 'directLongitude'},
]


def loop(count, radius=0.01, center=(48.85, 2.35)):
    return [(center[0] + radius * math.sin(2 * math.pi * index / count),
             center[1] + radius * math.cos(2 * math.pi * index / count))
            for index in range(count)]


def details(count=1000, polyline=True, heart_rate=lambda index: 140 + index % 30):
    points = loop(count)
    rows = [{'metrics': [1_700_000_000_000 + index * 1000, float(index), index * 3.0,
                         heart_rate(index), 3.0, 100 + index % 5, 170,
                         points[index][0], points[index][1]]}
            for index in range(count)]
    payload = {'metricDescriptors': DESCRIPTORS, 'activityDetailMetrics': rows}
    if polyline:
        payload['geoPolylineDTO'] = {'polyline': [{'lat': lat, 'lon': lon}
                                                  for lat, lon in points]}
    return payload


def test_route_is_simplified_within_budget_and_keeps_its_shape():
    track = normalize_track(details(1000))
    assert track['status'] == 'available'
    assert 20 < track['route_point_count'] <= MAX_ROUTE_POINTS
    assert len(track['route']) == track['route_point_count']
    latitudes = [point[0] for point in track['route']]
    assert max(latitudes) > 48.859 and min(latitudes) < 48.841
    assert track['bounds']['max_lat'] >= max(latitudes)
    assert track['source_point_count'] == 1000
    assert all(len(str(coordinate).split('.')[1]) <= 5
               for point in track['route'] for coordinate in point)


def test_series_are_aligned_downsampled_and_omit_empty_metrics():
    track = normalize_track(details(1000))
    series = track['series']
    assert track['series_point_count'] == MAX_SERIES_POINTS
    assert {len(values) for values in series.values()} == {MAX_SERIES_POINTS}
    assert 'power_w' not in series
    assert series['elapsed_s'][-1] > series['elapsed_s'][0]
    assert series['distance_m'] == sorted(series['distance_m'])
    assert all(135 <= value <= 175 for value in series['heart_rate'])
    assert series['cadence'][0] == 170


def test_short_activities_keep_every_sample():
    track = normalize_track(details(50))
    assert track['series_point_count'] == 50
    assert track['route_point_count'] == 50


def test_route_falls_back_to_sample_coordinates_without_polyline():
    track = normalize_track(details(200, polyline=False))
    assert track['route_point_count'] > 2
    assert track['bounds'] is not None


def test_treadmill_activity_has_curves_but_no_route():
    payload = details(120, polyline=False)
    for row in payload['activityDetailMetrics']:
        row['metrics'][7] = None
        row['metrics'][8] = None
    track = normalize_track(payload)
    assert track['status'] == 'available'
    assert track['route'] is None and track['bounds'] is None
    assert 'heart_rate' in track['series']


def test_heart_rate_histogram_preserves_full_resolution_time():
    track = normalize_track(details(1000, heart_rate=lambda index: 150 if index < 600 else 170))
    histogram = track['heart_rate_histogram']
    assert histogram == {'150': 600, '170': 400}


def test_pauses_do_not_inflate_the_histogram():
    payload = details(4, polyline=False, heart_rate=lambda index: 150)
    payload['activityDetailMetrics'][2]['metrics'][1] = 5000.0
    payload['activityDetailMetrics'][3]['metrics'][1] = 5001.0
    histogram = normalize_track(payload)['heart_rate_histogram']
    assert histogram == {'150': 13}


def test_invalid_coordinates_and_non_finite_values_are_dropped():
    payload = details(30)
    payload['geoPolylineDTO']['polyline'] += [
        {'lat': 0, 'lon': 0}, {'lat': 95, 'lon': 10}, {'lat': None, 'lon': 3}, 'junk']
    payload['activityDetailMetrics'][5]['metrics'][3] = float('nan')
    payload['activityDetailMetrics'][6]['metrics'][3] = None
    track = normalize_track(payload)
    assert track['route_point_count'] == 30
    assert track['series']['heart_rate'][5] is None


def test_malformed_or_empty_payloads_are_unavailable():
    for payload in (None, [], {}, {'metricDescriptors': 'x', 'activityDetailMetrics': 3},
                    {'geoPolylineDTO': {'polyline': [{'lat': 1, 'lon': 1}]}}):
        track = normalize_track(payload)
        assert track['status'] == 'unavailable'
        assert track['route'] is None and track['series'] is None
        assert track['source_point_count'] == 0


def test_simplification_is_deterministic_and_never_exceeds_budget():
    points = loop(4000, radius=0.05)
    first = simplify_route(points, 120)
    assert first == simplify_route(points, 120)
    assert 2 <= len(first) <= 120
    assert first[0] == points[0] and first[-1] == points[-1]
    assert simplify_route(points[:10], 120) == points[:10]
