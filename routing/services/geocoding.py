"""Live, per-request geocoding of start/finish place names via OpenRouteService."""
import hashlib
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from routing.services.exceptions import (
    GeocodingError, QuotaExceededError, UpstreamResponseError, UpstreamTimeoutError,
)

GEOCODE_URL = f"{settings.ORS_BASE_URL}/geocode/search"
CACHE_TTL_SECONDS = 24 * 60 * 60
CACHE_PREFIX = "ors-geocode"


@dataclass
class GeocodeResult:
    lon: float
    lat: float
    label: str
    country_code: str  # ISO alpha-3, e.g. "USA"
    region_code: str  # e.g. "IL"


def _cache_key(place_text):
    digest = hashlib.sha1(place_text.strip().lower().encode()).hexdigest()
    return f"{CACHE_PREFIX}-{digest}"


def geocode_place(place_text, call_counter=None):
    """Resolve a free-text place name to coordinates, restricted to the USA."""
    cache_key = _cache_key(place_text)
    cached = cache.get(cache_key)
    if cached is not None:
        return GeocodeResult(**cached)

    try:
        response = requests.get(
            GEOCODE_URL,
            params={
                "api_key": settings.ORS_API_KEY,
                "text": place_text,
                "boundary.country": "USA",
                "size": 1,
            },
            timeout=(settings.ORS_TIMEOUT_CONNECT, settings.ORS_TIMEOUT_READ),
        )
    except requests.Timeout as exc:
        raise UpstreamTimeoutError(f"Geocoding '{place_text}' timed out") from exc
    except requests.RequestException as exc:
        raise UpstreamResponseError(f"Geocoding '{place_text}' failed: {exc}") from exc
    finally:
        if call_counter is not None:
            call_counter.append("geocode")

    if response.status_code == 429:
        raise QuotaExceededError("OpenRouteService geocoding quota exceeded")
    if response.status_code != 200:
        raise UpstreamResponseError(f"Geocoding '{place_text}' returned HTTP {response.status_code}")

    try:
        payload = response.json()
        features = payload["features"]
    except (ValueError, KeyError) as exc:
        raise UpstreamResponseError(f"Malformed geocoding response for '{place_text}'") from exc

    if not features:
        raise GeocodingError(f"Could not find a US location matching '{place_text}'")

    feature = features[0]
    lon, lat = feature["geometry"]["coordinates"]
    properties = feature.get("properties", {})
    country_code = properties.get("country_a", "")
    region_code = properties.get("region_a", "")
    label = properties.get("label") or properties.get("name") or place_text

    if country_code != "USA":
        raise GeocodingError(
            f"'{place_text}' resolved to {properties.get('country', 'a non-US location')}, "
            "not the USA"
        )

    result = GeocodeResult(lon=lon, lat=lat, label=label, country_code=country_code, region_code=region_code)
    cache.set(cache_key, result.__dict__, CACHE_TTL_SECONDS)
    return result
