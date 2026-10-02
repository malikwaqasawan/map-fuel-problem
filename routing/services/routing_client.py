"""Live routing via OpenRouteService Directions. Cached by resolved
(start, finish) coordinate pair so an identical repeat request costs zero
further external calls.
"""
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from routing.services.exceptions import (
    QuotaExceededError, RoutingError, UpstreamResponseError, UpstreamTimeoutError,
)

DIRECTIONS_URL = f"{settings.ORS_BASE_URL}/v2/directions/driving-car/geojson"
CACHE_TTL_SECONDS = 24 * 60 * 60
CACHE_PREFIX = "ors-route"

# ORS's hosted Directions service caps a single route at 6,000 km.
ORS_MAX_DISTANCE_METERS = 6_000_000


@dataclass
class RouteResult:
    distance_miles: float
    duration_seconds: float
    coordinates: list  # [[lon, lat], ...]


def _cache_key(start, finish):
    return f"{CACHE_PREFIX}:{start.lon:.5f},{start.lat:.5f}->{finish.lon:.5f},{finish.lat:.5f}"


def get_route(start, finish, call_counter=None):
    cache_key = _cache_key(start, finish)
    cached = cache.get(cache_key)
    if cached is not None:
        return RouteResult(**cached)

    try:
        response = requests.post(
            DIRECTIONS_URL,
            json={
                "coordinates": [[start.lon, start.lat], [finish.lon, finish.lat]],
                "radiuses": [-1, -1],  # search as far as needed to snap to a road
            },
            headers={"Authorization": settings.ORS_API_KEY, "Content-Type": "application/json"},
            timeout=(settings.ORS_TIMEOUT_CONNECT, settings.ORS_TIMEOUT_READ),
        )
    except requests.Timeout as exc:
        raise UpstreamTimeoutError("Directions request timed out") from exc
    except requests.RequestException as exc:
        raise UpstreamResponseError(f"Directions request failed: {exc}") from exc
    finally:
        if call_counter is not None:
            call_counter.append("directions")

    if response.status_code == 429:
        raise QuotaExceededError("OpenRouteService directions quota exceeded")
    if response.status_code != 200:
        detail = ""
        try:
            detail = response.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        raise RoutingError(f"No drivable route found (HTTP {response.status_code}). {detail}".strip())

    try:
        payload = response.json()
        feature = payload["features"][0]
        summary = feature["properties"]["summary"]
        distance_meters = summary["distance"]
        duration_seconds = summary["duration"]
        coordinates = feature["geometry"]["coordinates"]
    except (ValueError, KeyError, IndexError) as exc:
        raise UpstreamResponseError("Malformed directions response") from exc

    if distance_meters > ORS_MAX_DISTANCE_METERS:
        raise RoutingError(
            f"Route distance ({distance_meters / 1609.344:.0f} mi) exceeds the routing "
            "provider's supported maximum for a single request"
        )

    result = RouteResult(
        distance_miles=distance_meters / 1609.344,
        duration_seconds=duration_seconds,
        coordinates=coordinates,
    )
    cache.set(cache_key, result.__dict__, CACHE_TTL_SECONDS)
    return result
