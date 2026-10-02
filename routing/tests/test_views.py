from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APITestCase

from routing.models import TruckStop
from routing.services.exceptions import GeocodingError, QuotaExceededError, RoutingError, UpstreamTimeoutError
from routing.services.geocoding import GeocodeResult
from routing.services.routing_client import RouteResult

# straight-line route along latitude 40N, with one TruckStop at its midpoint
FAKE_START = GeocodeResult(lon=-90.0, lat=40.0, label="Start, IL", country_code="USA", region_code="IL")
FAKE_FINISH = GeocodeResult(lon=-80.0, lat=40.0, label="Finish, OH", country_code="USA", region_code="OH")
FAKE_ROUTE = RouteResult(
    distance_miles=600.0, duration_seconds=36000.0,
    coordinates=[[-90.0, 40.0], [-85.0, 40.0], [-80.0, 40.0]],
)


class _MockResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


def _fake_geocode(place_text, call_counter=None):
    if call_counter is not None:
        call_counter.append("geocode")
    if "start" in place_text.lower():
        return FAKE_START
    if "finish" in place_text.lower():
        return FAKE_FINISH
    raise GeocodingError(f"no match for {place_text}")


def _fake_get_route(start, finish, call_counter=None):
    if call_counter is not None:
        call_counter.append("directions")
    return FAKE_ROUTE


class RoutePlanViewTests(APITestCase):
    def setUp(self):
        cache.clear()
        TruckStop.objects.create(
            source_row_number=0, opis_id=1, name="Midpoint Fuel", address="Exit 1",
            city="Midtown", state="OH", rack_id="1", retail_price=Decimal("3.00"),
            latitude=40.0, longitude=-85.0, coordinate_precision="city", coordinate_source="gazetteer",
        )

    @patch("routing.views.get_route", side_effect=_fake_get_route)
    @patch("routing.views.geocode_place", side_effect=_fake_geocode)
    def test_happy_path_returns_feasible_plan_with_three_calls(self, mock_geocode, mock_route):
        response = self.client.post(
            "/api/route/", {"start": "Start City", "finish": "Finish City"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "OK")
        self.assertEqual(body["external_calls_made"], 3)  # geocode(start) + geocode(finish) + directions
        self.assertIn("map_url", body)
        self.assertIn("map_data", body)
        self.assertEqual(mock_geocode.call_count, 2)
        self.assertEqual(mock_route.call_count, 1)

    def test_repeat_request_hits_cache_and_makes_zero_new_calls(self):
        # mocks at the HTTP layer so the real caching code in geocode_place/get_route runs
        geocode_response = _MockResponse(200, {
            "features": [{
                "geometry": {"coordinates": [-90.0, 40.0]},
                "properties": {"country_a": "USA", "region_a": "IL", "label": "Start, IL"},
            }],
        })
        directions_response = _MockResponse(200, {
            "features": [{
                "properties": {"summary": {"distance": 965600.0, "duration": 36000.0}},
                "geometry": {"coordinates": [[-90.0, 40.0], [-85.0, 40.0], [-80.0, 40.0]]},
            }],
        })

        with patch("routing.services.geocoding.requests.get", return_value=geocode_response) as mock_get, \
             patch("routing.services.routing_client.requests.post", return_value=directions_response) as mock_post:
            first = self.client.post(
                "/api/route/", {"start": "Start City", "finish": "Start City 2"}, format="json",
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["external_calls_made"], 3)
            self.assertEqual(mock_get.call_count, 2)
            self.assertEqual(mock_post.call_count, 1)

            second = self.client.post(
                "/api/route/", {"start": "Start City", "finish": "Start City 2"}, format="json",
            )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["external_calls_made"], 0)
        # No additional HTTP calls beyond the first request's.
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_post.call_count, 1)

    def test_missing_fields_returns_400(self):
        response = self.client.post("/api/route/", {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_identical_start_and_finish_returns_400(self):
        response = self.client.post(
            "/api/route/", {"start": "Same Place", "finish": "Same Place"}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_initial_fuel_gallons_returns_400(self):
        response = self.client.post(
            "/api/route/",
            {"start": "Start City", "finish": "Finish City", "initial_fuel_gallons": 999},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("routing.views.geocode_place", side_effect=GeocodingError("no match"))
    def test_geocoding_failure_returns_422(self, mock_geocode):
        response = self.client.post(
            "/api/route/", {"start": "Nowhere", "finish": "Finish City"}, format="json",
        )
        self.assertEqual(response.status_code, 422)

    @patch("routing.views.geocode_place", side_effect=QuotaExceededError("quota"))
    def test_geocoding_quota_exceeded_returns_503(self, mock_geocode):
        response = self.client.post(
            "/api/route/", {"start": "Start City", "finish": "Finish City"}, format="json",
        )
        self.assertEqual(response.status_code, 503)

    @patch("routing.views.geocode_place", side_effect=UpstreamTimeoutError("timed out"))
    def test_geocoding_timeout_returns_504(self, mock_geocode):
        response = self.client.post(
            "/api/route/", {"start": "Start City", "finish": "Finish City"}, format="json",
        )
        self.assertEqual(response.status_code, 504)

    @patch("routing.views.get_route", side_effect=RoutingError("no route"))
    @patch("routing.views.geocode_place", side_effect=_fake_geocode)
    def test_routing_failure_returns_422(self, mock_geocode, mock_route):
        response = self.client.post(
            "/api/route/", {"start": "Start City", "finish": "Finish City"}, format="json",
        )
        self.assertEqual(response.status_code, 422)

    @patch("routing.views.get_route")
    @patch("routing.views.geocode_place", side_effect=_fake_geocode)
    def test_coordinates_input_skips_geocoding(self, mock_geocode, mock_route):
        mock_route.side_effect = _fake_get_route
        response = self.client.post(
            "/api/route/",
            {"start_lat": 40.0, "start_lon": -90.0, "finish_lat": 40.0, "finish_lon": -80.0},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        mock_geocode.assert_not_called()

    @patch("routing.views.get_route")
    @patch("routing.views.geocode_place", side_effect=_fake_geocode)
    def test_infeasible_plan_returns_422_with_structured_status(self, mock_geocode, mock_route):
        # A route far longer than any seeded station can bridge.
        mock_route.side_effect = lambda *a, **k: RouteResult(
            distance_miles=5000.0, duration_seconds=200000.0,
            coordinates=[[-90.0, 40.0], [-85.0, 40.0], [-80.0, 40.0]],
        )
        response = self.client.post(
            "/api/route/", {"start": "Start City", "finish": "Finish City"}, format="json",
        )
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["status"], "NO_FEASIBLE_FUEL_PLAN")
        self.assertIsNone(body["fuel_ledger"]["fuel_purchase_cost_usd"])
        self.assertIn("map_url", body)  # base route still returned for explanation


class MapViewTests(APITestCase):
    def test_unknown_or_expired_key_returns_404(self):
        response = self.client.get("/map/does-not-exist/")
        self.assertEqual(response.status_code, 404)


class TruckStopDecimalRoundTripTests(TestCase):
    def test_price_survives_save_and_reload_exactly(self):
        truckstop = TruckStop.objects.create(
            source_row_number=0, opis_id=1, name="Test", address="Exit 1",
            city="Testville", state="OH", rack_id="1", retail_price=Decimal("3.00733333"),
        )
        reloaded = TruckStop.objects.get(pk=truckstop.pk)
        self.assertEqual(reloaded.retail_price, Decimal("3.00733333"))
        self.assertIsInstance(reloaded.retail_price, Decimal)
