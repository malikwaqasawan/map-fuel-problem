"""Haversine distance, route bounding box, and point-to-polyline projection.
Plain float math, no GeoDjango/Shapely/geopy dependency.
"""
import math
from dataclasses import dataclass

EARTH_RADIUS_MI = 3958.8


def haversine_miles(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(min(1.0, math.sqrt(a)))


def route_bounding_box(coordinates, margin_miles):
    """coordinates: [[lon, lat], ...]. Returns a dict with a margin applied."""
    lons = [c[0] for c in coordinates]
    lats = [c[1] for c in coordinates]
    avg_lat = (min(lats) + max(lats)) / 2
    lat_margin = margin_miles / 69.0
    lon_margin = margin_miles / (69.0 * max(math.cos(math.radians(avg_lat)), 0.1))
    return {
        "min_lat": min(lats) - lat_margin,
        "max_lat": max(lats) + lat_margin,
        "min_lon": min(lons) - lon_margin,
        "max_lon": max(lons) + lon_margin,
    }


@dataclass
class RouteSegment:
    lat1: float
    lon1: float
    lat2: float
    lon2: float
    cum_start_miles: float
    length_miles: float
    min_lat: float = 0.0
    max_lat: float = 0.0
    min_lon: float = 0.0
    max_lon: float = 0.0

    def __post_init__(self):
        self.min_lat = min(self.lat1, self.lat2)
        self.max_lat = max(self.lat1, self.lat2)
        self.min_lon = min(self.lon1, self.lon2)
        self.max_lon = max(self.lon1, self.lon2)


def build_route_segments(coordinates, authoritative_total_miles=None):
    """coordinates: [[lon, lat], ...] from ORS geometry.

    Rescales cumulative distances to authoritative_total_miles (ORS's own
    reported distance) when given, so per-station distances line up with the
    headline total_distance_miles.
    """
    segments = []
    cumulative = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coordinates, coordinates[1:]):
        length = haversine_miles(lat1, lon1, lat2, lon2)
        segments.append(RouteSegment(lat1, lon1, lat2, lon2, cumulative, length))
        cumulative += length

    raw_total = cumulative
    if authoritative_total_miles is not None and raw_total > 0:
        scale = authoritative_total_miles / raw_total
        for seg in segments:
            seg.cum_start_miles *= scale
            seg.length_miles *= scale
        total = authoritative_total_miles
    else:
        total = raw_total

    return segments, total


def _to_planar_miles(lat, lon, ref_lat):
    """Local equirectangular projection - accurate enough over one short
    route segment; not meant for long distances."""
    x = math.radians(lon) * math.cos(math.radians(ref_lat)) * EARTH_RADIUS_MI
    y = math.radians(lat) * EARTH_RADIUS_MI
    return x, y


def project_point_onto_segment(lat, lon, segment):
    """Returns (perpendicular_distance_miles, cumulative_distance_along_route_miles)."""
    ref_lat = (segment.lat1 + segment.lat2) / 2
    ax, ay = _to_planar_miles(segment.lat1, segment.lon1, ref_lat)
    bx, by = _to_planar_miles(segment.lat2, segment.lon2, ref_lat)
    px, py = _to_planar_miles(lat, lon, ref_lat)

    abx, aby = bx - ax, by - ay
    ab_len_sq = abx * abx + aby * aby
    if ab_len_sq == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / ab_len_sq))

    proj_x, proj_y = ax + t * abx, ay + t * aby
    perpendicular_miles = math.hypot(px - proj_x, py - proj_y)
    cumulative_miles = segment.cum_start_miles + t * segment.length_miles
    return perpendicular_miles, cumulative_miles


def nearest_point_on_route(lat, lon, segments, max_distance_miles=None):
    """Nearest-segment projection. If max_distance_miles is given, segments
    whose bounding box (expanded by that margin) can't contain a point this
    close are skipped before the projection math runs - an exact reject,
    same results as checking every segment, just faster.
    """
    lat_margin = (max_distance_miles / 69.0) if max_distance_miles is not None else None
    lon_margin = (
        max_distance_miles / (69.0 * max(math.cos(math.radians(lat)), 0.1))
        if max_distance_miles is not None else None
    )

    best_perp, best_cum = float("inf"), None
    for segment in segments:
        if lat_margin is not None:
            if not (segment.min_lat - lat_margin <= lat <= segment.max_lat + lat_margin):
                continue
            if not (segment.min_lon - lon_margin <= lon <= segment.max_lon + lon_margin):
                continue
        perp, cum = project_point_onto_segment(lat, lon, segment)
        if perp < best_perp:
            best_perp, best_cum = perp, cum
    return best_perp, best_cum
