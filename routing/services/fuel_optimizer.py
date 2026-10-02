"""Candidate station discovery along a route, and the fuel-ledger purchase
algorithm (greedy solution to the fixed-path gas station problem).
"""
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings

from routing.models import TruckStop
from routing.services.geo_utils import build_route_segments, nearest_point_on_route, route_bounding_box

EPSILON_MILES = 1e-6  # float-noise tolerance for "can we just barely reach this"
PURCHASE_EPSILON_GAL = 1e-3  # below this is float noise from chained target-fuel math, not a real purchase


@dataclass
class RouteCandidate:
    truckstop: TruckStop
    distance_from_start_miles: float
    off_route_straight_line_miles: float

    @property
    def price(self):
        return self.truckstop.retail_price  # Decimal


@dataclass
class PurchaseStop:
    candidate: RouteCandidate
    distance_from_start_miles: float
    fuel_on_arrival_gal: float
    gallons_purchased: float
    cost_usd: Decimal


@dataclass
class FuelPlanResult:
    feasible: bool
    ledger: list = field(default_factory=list)
    final_fuel_gal: float | None = None
    message: str | None = None
    gap_start_miles: float | None = None
    gap_end_miles: float | None = None


def find_candidates_along_route(coordinates, total_distance_miles):
    """Single DB query against the wider fallback corridor; the narrower
    primary-corridor subset is filtered from the same result in-memory so
    plan_route_fuel never needs a second query for its retry tier.
    """
    segments, _ = build_route_segments(coordinates, total_distance_miles)
    fallback_width = settings.CORRIDOR_FALLBACK_WIDTH_MILES
    bbox = route_bounding_box(coordinates, margin_miles=fallback_width + 10)

    queryset = TruckStop.objects.filter(
        latitude__isnull=False,
        longitude__isnull=False,
        latitude__gte=bbox["min_lat"], latitude__lte=bbox["max_lat"],
        longitude__gte=bbox["min_lon"], longitude__lte=bbox["max_lon"],
    )

    candidates = []
    for truckstop in queryset.iterator():
        perpendicular, cumulative = nearest_point_on_route(
            truckstop.latitude, truckstop.longitude, segments, max_distance_miles=fallback_width,
        )
        if perpendicular <= fallback_width:
            candidates.append(RouteCandidate(truckstop, cumulative, perpendicular))

    candidates.sort(key=lambda c: c.distance_from_start_miles)
    return candidates


def plan_fuel_purchases(stations_sorted, total_distance_miles, tank_capacity_gal, mpg, initial_fuel_gal=None):
    """Greedy gas-station-problem solver: at each station, buy just enough to
    reach the next strictly-cheaper station within tank range; otherwise fill
    up, capped by what's needed to finish the trip.
    """
    range_miles = tank_capacity_gal * mpg
    fuel = tank_capacity_gal if initial_fuel_gal is None else initial_fuel_gal
    position = 0.0
    ledger = []

    for idx, station in enumerate(stations_sorted):
        leg_miles = station.distance_from_start_miles - position
        fuel -= leg_miles / mpg
        if fuel < -EPSILON_MILES:
            return FuelPlanResult(
                feasible=False, ledger=ledger,
                gap_start_miles=position, gap_end_miles=station.distance_from_start_miles,
                message=(
                    f"No feasible fuel plan found with available station data between "
                    f"mile {position:.1f} and mile {station.distance_from_start_miles:.1f} "
                    f"({leg_miles:.1f} mi exceeds the {range_miles:.0f} mi vehicle range). "
                    "This reflects a gap in available station data, not proof no station exists there."
                ),
            )
        fuel = max(fuel, 0.0)
        position = station.distance_from_start_miles

        reach = position + range_miles
        cheaper = next(
            (s for s in stations_sorted[idx + 1:]
             if s.distance_from_start_miles <= reach and s.price < station.price),
            None,
        )
        target_distance = cheaper.distance_from_start_miles if cheaper else min(reach, total_distance_miles)
        target_fuel = min((target_distance - position) / mpg, tank_capacity_gal)
        purchase = max(0.0, target_fuel - fuel)
        fuel += purchase

        if purchase > PURCHASE_EPSILON_GAL:
            cost = Decimal(str(round(purchase, 6))) * station.price
            ledger.append(PurchaseStop(
                candidate=station, distance_from_start_miles=position,
                fuel_on_arrival_gal=fuel - purchase, gallons_purchased=purchase, cost_usd=cost,
            ))

    final_leg_miles = total_distance_miles - position
    fuel -= final_leg_miles / mpg
    if fuel < -EPSILON_MILES:
        return FuelPlanResult(
            feasible=False, ledger=ledger,
            gap_start_miles=position, gap_end_miles=total_distance_miles,
            message=(
                f"No feasible fuel plan found with available station data between "
                f"mile {position:.1f} and the destination at mile {total_distance_miles:.1f} "
                f"({final_leg_miles:.1f} mi exceeds the {range_miles:.0f} mi vehicle range). "
                "This reflects a gap in available station data, not proof no station exists there."
            ),
        )

    return FuelPlanResult(feasible=True, ledger=ledger, final_fuel_gal=max(fuel, 0.0))


def plan_route_fuel(coordinates, total_distance_miles, initial_fuel_gal=None):
    """Try the narrow corridor first; retry with the wider fallback corridor
    only if that comes back infeasible."""
    tank_capacity_gal = settings.VEHICLE_TANK_CAPACITY_GAL
    mpg = settings.VEHICLE_MPG

    all_candidates = find_candidates_along_route(coordinates, total_distance_miles)
    primary_width = settings.CORRIDOR_WIDTH_MILES
    primary = [c for c in all_candidates if c.off_route_straight_line_miles <= primary_width]

    result = plan_fuel_purchases(primary, total_distance_miles, tank_capacity_gal, mpg, initial_fuel_gal)
    used_fallback_corridor = False

    if not result.feasible and len(all_candidates) > len(primary):
        fallback_result = plan_fuel_purchases(
            all_candidates, total_distance_miles, tank_capacity_gal, mpg, initial_fuel_gal
        )
        if fallback_result.feasible or len(fallback_result.ledger) >= len(result.ledger):
            result = fallback_result
            used_fallback_corridor = True

    return result, used_fallback_corridor


def summarize_fuel_ledger(result, total_distance_miles, mpg, initial_fuel_gal):
    total_gallons_consumed = total_distance_miles / mpg
    total_gallons_purchased = sum(stop.gallons_purchased for stop in result.ledger)
    total_cost = sum((stop.cost_usd for stop in result.ledger), Decimal("0"))
    return {
        "total_gallons_consumed": round(total_gallons_consumed, 4),
        "total_gallons_purchased": round(total_gallons_purchased, 4),
        "initial_fuel_gallons": initial_fuel_gal,
        "final_fuel_gallons": round(result.final_fuel_gal, 4) if result.final_fuel_gal is not None else None,
        "fuel_purchase_cost_usd": float(total_cost) if result.feasible else None,
        "cost_basis": "purchases_during_trip_excluding_initial_fuel",
    }
