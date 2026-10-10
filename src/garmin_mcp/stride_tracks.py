"""Compact, provider-neutral activity tracks for Stride.

Garmin activity details can carry thousands of GPS points and chart
samples. This module turns that payload into a small structure Stride can
render without further processing: a simplified route, fixed-size aligned
series, and a heart-rate histogram that keeps full-resolution time in any
heart-rate zone definition. No raw Garmin payload leaves this module.
"""

import math
from typing import Any, TypedDict

MAX_ROUTE_POINTS = 300
MAX_SERIES_POINTS = 240
GARMIN_MAX_CHART_SIZE = 600
GARMIN_MAX_POLYLINE_SIZE = 1200
MAX_SAMPLE_GAP_SECONDS = 10
COORDINATE_DECIMALS = 5

_ELAPSED_KEYS = ('sumElapsedDuration', 'sumDuration')
_CADENCE_KEYS = ('directDoubleCadence', 'directBikeCadence')
_ELEVATION_KEYS = ('directElevation', 'directCorrectedElevation')
_SERIES_ROUNDING = {
    'elapsed_s': 0, 'distance_m': 1, 'heart_rate': 0, 'speed_mps': 2,
    'elevation_m': 1, 'cadence': 0, 'power_w': 0,
}
_AVERAGED = ('heart_rate', 'speed_mps', 'elevation_m', 'cadence', 'power_w')
_LAST_VALUE = ('elapsed_s', 'distance_m')


class Track(TypedDict):
    """Normalized track stored for one activity."""

    status: str
    route: list[list[float]] | None
    bounds: dict[str, float] | None
    series: dict[str, list[float | int | None]] | None
    heart_rate_histogram: dict[str, int] | None
    route_point_count: int
    series_point_count: int
    source_point_count: int


def unavailable_track() -> Track:
    return {'status': 'unavailable', 'route': None, 'bounds': None, 'series': None,
            'heart_rate_histogram': None, 'route_point_count': 0,
            'series_point_count': 0, 'source_point_count': 0}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _valid_point(latitude: float | None, longitude: float | None) -> bool:
    if latitude is None or longitude is None:
        return False
    if abs(latitude) > 90 or abs(longitude) > 180:
        return False
    return not (latitude == 0 and longitude == 0)


def _samples(details: dict[str, Any]) -> list[dict[str, float | None]]:
    descriptors = details.get('metricDescriptors')
    rows = details.get('activityDetailMetrics')
    if not isinstance(descriptors, list) or not isinstance(rows, list):
        return []
    indexes: dict[str, int] = {}
    for descriptor in descriptors:
        if (isinstance(descriptor, dict) and isinstance(descriptor.get('key'), str)
                and isinstance(descriptor.get('metricsIndex'), int)):
            indexes[descriptor['key']] = descriptor['metricsIndex']

    def column(row: list[Any], *keys: str) -> float | None:
        for key in keys:
            position = indexes.get(key)
            if position is not None and position < len(row):
                value = _number(row[position])
                if value is not None:
                    return value
        return None

    samples: list[dict[str, float | None]] = []
    first_timestamp: float | None = None
    for item in rows:
        metrics = item.get('metrics') if isinstance(item, dict) else None
        if not isinstance(metrics, list):
            continue
        timestamp = column(metrics, 'directTimestamp')
        elapsed = column(metrics, *_ELAPSED_KEYS)
        if elapsed is None and timestamp is not None:
            first_timestamp = timestamp if first_timestamp is None else first_timestamp
            elapsed = (timestamp - first_timestamp) / 1000
        samples.append({
            'elapsed_s': elapsed,
            'distance_m': column(metrics, 'sumDistance'),
            'heart_rate': column(metrics, 'directHeartRate'),
            'speed_mps': column(metrics, 'directSpeed'),
            'elevation_m': column(metrics, *_ELEVATION_KEYS),
            'cadence': column(metrics, *_CADENCE_KEYS),
            'power_w': column(metrics, 'directPower'),
            'latitude': column(metrics, 'directLatitude'),
            'longitude': column(metrics, 'directLongitude'),
        })
    return samples


def _polyline_points(details: dict[str, Any]) -> list[tuple[float, float]]:
    geometry = details.get('geoPolylineDTO')
    polyline = geometry.get('polyline') if isinstance(geometry, dict) else None
    if not isinstance(polyline, list):
        return []
    points = []
    for item in polyline:
        if not isinstance(item, dict):
            continue
        latitude = _number(item.get('lat', item.get('latitude')))
        longitude = _number(item.get('lon', item.get('longitude')))
        if _valid_point(latitude, longitude):
            points.append((latitude, longitude))
    return points


def _sample_points(samples: list[dict[str, float | None]]) -> list[tuple[float, float]]:
    return [(sample['latitude'], sample['longitude']) for sample in samples
            if _valid_point(sample['latitude'], sample['longitude'])]


def _kept_indices(xs: list[float], ys: list[float], epsilon: float) -> list[int]:
    count = len(xs)
    keep = [False] * count
    keep[0] = keep[-1] = True
    stack = [(0, count - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        dx, dy = xs[last] - xs[first], ys[last] - ys[first]
        length = dx * dx + dy * dy
        farthest, farthest_index = -1.0, -1
        for index in range(first + 1, last):
            px, py = xs[index] - xs[first], ys[index] - ys[first]
            if length == 0:
                distance = math.hypot(px, py)
            else:
                ratio = min(1.0, max(0.0, (px * dx + py * dy) / length))
                distance = math.hypot(px - ratio * dx, py - ratio * dy)
            if distance > farthest:
                farthest, farthest_index = distance, index
        if farthest > epsilon:
            keep[farthest_index] = True
            stack.append((first, farthest_index))
            stack.append((farthest_index, last))
    return [index for index, kept in enumerate(keep) if kept]


def simplify_route(points: list[tuple[float, float]],
                   max_points: int = MAX_ROUTE_POINTS) -> list[tuple[float, float]]:
    """Reduce a route to at most ``max_points`` while keeping its shape."""
    deduplicated = [point for index, point in enumerate(points)
                    if index == 0 or point != points[index - 1]]
    if len(deduplicated) <= max_points:
        return deduplicated
    scale = math.cos(math.radians(sum(point[0] for point in deduplicated) / len(deduplicated)))
    xs = [point[1] * scale for point in deduplicated]
    ys = [point[0] for point in deduplicated]
    low = 0.0
    high = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
    best = [0, len(deduplicated) - 1]
    for _ in range(24):
        middle = (low + high) / 2
        indices = _kept_indices(xs, ys, middle)
        if len(indices) > max_points:
            low = middle
        else:
            high = middle
            best = indices
    return [deduplicated[index] for index in best]


def _bounds(points: list[tuple[float, float]]) -> dict[str, float]:
    latitudes = [point[0] for point in points]
    longitudes = [point[1] for point in points]
    return {'min_lat': round(min(latitudes), COORDINATE_DECIMALS),
            'min_lon': round(min(longitudes), COORDINATE_DECIMALS),
            'max_lat': round(max(latitudes), COORDINATE_DECIMALS),
            'max_lon': round(max(longitudes), COORDINATE_DECIMALS)}


def _series(samples: list[dict[str, float | None]],
            max_points: int) -> dict[str, list[float | int | None]]:
    count = len(samples)
    buckets = ([(index, index + 1) for index in range(count)] if count <= max_points else
               [(index * count // max_points, (index + 1) * count // max_points)
                for index in range(max_points)])
    result: dict[str, list[float | int | None]] = {}
    for key, digits in _SERIES_ROUNDING.items():
        values: list[float | int | None] = []
        for start, end in buckets:
            window = [value for value in (sample[key] for sample in samples[start:end])
                      if value is not None]
            if not window:
                values.append(None)
                continue
            value = window[-1] if key in _LAST_VALUE else sum(window) / len(window)
            values.append(int(round(value)) if digits == 0 else round(value, digits))
        if any(value is not None for value in values):
            result[key] = values
    return result


def _histogram(samples: list[dict[str, float | None]]) -> dict[str, int] | None:
    seconds: dict[int, float] = {}
    previous_duration = 1.0
    for index, sample in enumerate(samples):
        following = samples[index + 1]['elapsed_s'] if index + 1 < len(samples) else None
        current = sample['elapsed_s']
        if current is not None and following is not None and following >= current:
            duration = min(following - current, MAX_SAMPLE_GAP_SECONDS)
        else:
            duration = previous_duration
        previous_duration = duration
        heart_rate = sample['heart_rate']
        if heart_rate is None or heart_rate <= 0:
            continue
        key = int(round(heart_rate))
        seconds[key] = seconds.get(key, 0.0) + duration
    histogram = {str(key): int(round(value)) for key, value in sorted(seconds.items())
                 if round(value) > 0}
    return histogram or None


def normalize_track(details: Any, *, max_route_points: int = MAX_ROUTE_POINTS,
                    max_series_points: int = MAX_SERIES_POINTS) -> Track:
    """Normalize a Garmin activity-details payload; never raises on bad shapes."""
    if not isinstance(details, dict):
        return unavailable_track()
    samples = _samples(details)
    points = _polyline_points(details) or _sample_points(samples)
    route = None
    bounds = None
    if len(points) >= 2:
        bounds = _bounds(points)
        route = [[round(latitude, COORDINATE_DECIMALS), round(longitude, COORDINATE_DECIMALS)]
                 for latitude, longitude in simplify_route(points, max_route_points)]
    series = _series(samples, max_series_points) if samples else {}
    series = series if any(key not in _LAST_VALUE for key in series) else {}
    if not route and not series:
        return unavailable_track()
    series_length = len(next(iter(series.values()))) if series else 0
    return {
        'status': 'available',
        'route': route,
        'bounds': bounds,
        'series': series or None,
        'heart_rate_histogram': _histogram(samples) if samples else None,
        'route_point_count': len(route) if route else 0,
        'series_point_count': series_length,
        'source_point_count': max(len(points), len(samples)),
    }
