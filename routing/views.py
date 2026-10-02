import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render
from django.views.decorators.http import require_GET
from rest_framework.response import Response
from rest_framework.views import APIView

from routing.serializers import RouteRequestSerializer
from routing.services.exceptions import (
    ExternalServiceError, GeocodingError, QuotaExceededError, RoutingError,
    UpstreamResponseError, UpstreamTimeoutError,
)
from routing.services.fuel_optimizer import plan_route_fuel, summarize_fuel_ledger
from routing.services.geocoding import geocode_place
from routing.services.routing_client import get_route

MAP_RESULT_CACHE_TTL_SECONDS = 60 * 60
MAP_RESULT_CACHE_PREFIX = "route-result"


@dataclass
class _Point:
    lon: float
    lat: float
    label: str = ""


def _resolve_endpoint(data, prefix, call_counter):
    """Returns a (lon, lat) point, geocoding only if coordinates weren't given directly."""
    lat_key, lon_key = f"{prefix}_lat", f"{prefix}_lon"
    if lat_key in data and lon_key in data:
        return _Point(lon=data[lon_key], lat=data[lat_key], label=f"{data[lat_key]},{data[lon_key]}")
    place_text = data[prefix]
    result = geocode_place(place_text, call_counter=call_counter)
    return _Point(lon=result.lon, lat=result.lat, label=result.label)


def _serialize_fuel_stop(stop):
    truckstop = stop.candidate.truckstop
    return {
        "name": truckstop.name,
        "address": truckstop.address,
        "city": truckstop.city,
        "state": truckstop.state,
        "latitude": truckstop.latitude,
        "longitude": truckstop.longitude,
        "coordinate_precision": truckstop.coordinate_precision,
        "coordinate_source": truckstop.coordinate_source,
        "price_per_gallon": float(truckstop.retail_price),
        "distance_from_start_miles": round(stop.distance_from_start_miles, 2),
        "off_route_straight_line_miles": round(stop.candidate.off_route_straight_line_miles, 2),
        "fuel_on_arrival_gal": round(stop.fuel_on_arrival_gal, 2),
        "gallons_purchased": round(stop.gallons_purchased, 2),
        "cost_usd": float(stop.cost_usd),
    }


def _build_map_geojson(route_coordinates, ledger):
    features = [{
        "type": "Feature",
        "properties": {"kind": "route"},
        "geometry": {"type": "LineString", "coordinates": route_coordinates},
    }]
    for stop in ledger:
        truckstop = stop.candidate.truckstop
        features.append({
            "type": "Feature",
            "properties": {
                "kind": "fuel_stop",
                "name": truckstop.name,
                "city": truckstop.city,
                "state": truckstop.state,
                "price_per_gallon": float(truckstop.retail_price),
                "gallons_purchased": round(stop.gallons_purchased, 2),
                "cost_usd": float(stop.cost_usd),
                "coordinate_precision": truckstop.coordinate_precision,
            },
            "geometry": {"type": "Point", "coordinates": [truckstop.longitude, truckstop.latitude]},
        })
    return {"type": "FeatureCollection", "features": features}


class RoutePlanView(APIView):
    def post(self, request):
        req = RouteRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data

        call_counter = []
        try:
            # geocode start/finish concurrently, not sequentially
            with ThreadPoolExecutor(max_workers=2) as executor:
                start_future = executor.submit(_resolve_endpoint, data, "start", call_counter)
                finish_future = executor.submit(_resolve_endpoint, data, "finish", call_counter)
                start = start_future.result()
                finish = finish_future.result()
        except GeocodingError as exc:
            return Response({"status": "ERROR", "error": str(exc)}, status=422)
        except QuotaExceededError as exc:
            return Response({"status": "ERROR", "error": str(exc)}, status=503)
        except UpstreamTimeoutError as exc:
            return Response({"status": "ERROR", "error": str(exc)}, status=504)
        except UpstreamResponseError as exc:
            return Response({"status": "ERROR", "error": str(exc)}, status=502)

        try:
            route = get_route(start, finish, call_counter=call_counter)
        except RoutingError as exc:
            return Response({"status": "ERROR", "error": str(exc)}, status=422)
        except QuotaExceededError as exc:
            return Response({"status": "ERROR", "error": str(exc)}, status=503)
        except UpstreamTimeoutError as exc:
            return Response({"status": "ERROR", "error": str(exc)}, status=504)
        except UpstreamResponseError as exc:
            return Response({"status": "ERROR", "error": str(exc)}, status=502)
        except ExternalServiceError as exc:
            return Response({"status": "ERROR", "error": str(exc)}, status=502)

        initial_fuel_gal = data.get("initial_fuel_gallons", settings.VEHICLE_TANK_CAPACITY_GAL)
        result, used_fallback_corridor = plan_route_fuel(
            route.coordinates, route.distance_miles, initial_fuel_gal=initial_fuel_gal,
        )

        ledger_json = [_serialize_fuel_stop(stop) for stop in result.ledger]
        map_data = _build_map_geojson(route.coordinates, result.ledger)

        warnings = []
        if used_fallback_corridor:
            warnings.append(
                f"No station found within the primary {settings.CORRIDOR_WIDTH_MILES:.0f}mi corridor "
                f"for part of this route; widened to {settings.CORRIDOR_FALLBACK_WIDTH_MILES:.0f}mi."
            )

        payload = {
            "status": "OK" if result.feasible else "NO_FEASIBLE_FUEL_PLAN",
            "start": {"label": start.label, "longitude": start.lon, "latitude": start.lat},
            "finish": {"label": finish.label, "longitude": finish.lon, "latitude": finish.lat},
            "total_distance_miles": round(route.distance_miles, 2),
            "total_duration_seconds": round(route.duration_seconds, 1),
            "vehicle_assumptions": {
                "mpg": settings.VEHICLE_MPG,
                "tank_capacity_gal": settings.VEHICLE_TANK_CAPACITY_GAL,
                "range_miles": settings.VEHICLE_TANK_CAPACITY_GAL * settings.VEHICLE_MPG,
                "initial_fuel_gallons": initial_fuel_gal,
            },
            "fuel_ledger": summarize_fuel_ledger(
                result, route.distance_miles, settings.VEHICLE_MPG, initial_fuel_gal
            ),
            "fuel_stops": ledger_json,
            "map_data": map_data,
            "optimization_scope": (
                "Exact minimum-cost purchase plan for the given fixed route and station data; "
                "does not verify driving access to off-route stations or search for a cheaper "
                "alternate road route."
            ),
            "route_validation": "unvalidated_corridor_estimate",
            "external_calls_made": len(call_counter),
            "warnings": warnings,
            "message": result.message,
        }
        if not result.feasible:
            payload["gap_start_miles"] = result.gap_start_miles
            payload["gap_end_miles"] = result.gap_end_miles

        cache_key = uuid.uuid4().hex
        cache.set(f"{MAP_RESULT_CACHE_PREFIX}:{cache_key}", payload, MAP_RESULT_CACHE_TTL_SECONDS)
        payload["map_url"] = f"/map/{cache_key}/"

        status_code = 200 if result.feasible else 422
        return Response(payload, status=status_code)


@require_GET
def map_view(request, result_key):
    payload = cache.get(f"{MAP_RESULT_CACHE_PREFIX}:{result_key}")
    if payload is None:
        return render(request, "routing/map_expired.html", status=404)
    return render(request, "routing/map.html", {"payload": payload})
