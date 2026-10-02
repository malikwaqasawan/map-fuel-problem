import random
from decimal import Decimal
from functools import reduce
from types import SimpleNamespace

from django.test import SimpleTestCase

from routing.services.fuel_optimizer import RouteCandidate, plan_fuel_purchases, summarize_fuel_ledger


def make_station(distance_miles, price, name="station"):
    truckstop = SimpleNamespace(retail_price=Decimal(str(price)), name=name)
    return RouteCandidate(
        truckstop=truckstop, distance_from_start_miles=float(distance_miles), off_route_straight_line_miles=0.0,
    )


def total_cost(result):
    return reduce(lambda acc, stop: acc + stop.cost_usd, result.ledger, Decimal("0"))


class WorkedExampleTests(SimpleTestCase):
    """600-mile trip, full 50-gal tank, one station at mile 400, nothing
    cheaper further out. Correct purchase is 10 gal, just enough to finish."""

    def test_600_mile_trip_one_stop_at_400_buys_exactly_enough_to_finish(self):
        stations = [make_station(400, price=3.00)]
        result = plan_fuel_purchases(stations, total_distance_miles=600, tank_capacity_gal=50.0, mpg=10.0)

        self.assertTrue(result.feasible)
        self.assertEqual(len(result.ledger), 1)
        stop = result.ledger[0]
        self.assertAlmostEqual(stop.fuel_on_arrival_gal, 10.0, places=4)
        self.assertAlmostEqual(stop.gallons_purchased, 10.0, places=4)
        self.assertAlmostEqual(float(stop.cost_usd), 30.0, places=2)
        self.assertAlmostEqual(result.final_fuel_gal, 0.0, places=4)

    def test_fuel_ledger_invariant_holds(self):
        stations = [make_station(400, price=3.00)]
        total_distance = 600
        result = plan_fuel_purchases(stations, total_distance_miles=total_distance, tank_capacity_gal=50.0, mpg=10.0)
        purchased = sum(s.gallons_purchased for s in result.ledger)
        consumed = total_distance / 10.0
        initial = 50.0
        self.assertAlmostEqual(initial + purchased - consumed, result.final_fuel_gal, places=4)


class PurchaseRuleTests(SimpleTestCase):
    def test_partial_fill_to_reach_a_cheaper_station_ahead(self):
        # low starting fuel forces a real, partial purchase
        stations = [make_station(50, price=3.50), make_station(300, price=2.50)]
        result = plan_fuel_purchases(
            stations, total_distance_miles=600, tank_capacity_gal=50.0, mpg=10.0, initial_fuel_gal=10.0,
        )
        self.assertTrue(result.feasible)
        first_stop = result.ledger[0]
        self.assertAlmostEqual(first_stop.gallons_purchased, 20.0, places=4)
        self.assertLess(first_stop.gallons_purchased, 50.0)  # partial, not a full tank

    def test_no_cheaper_station_ahead_fills_to_full_when_trip_continues(self):
        stations = [make_station(50, price=3.00)]
        result = plan_fuel_purchases(stations, total_distance_miles=2000, tank_capacity_gal=50.0, mpg=10.0)
        self.assertTrue(len(result.ledger) >= 1)
        stop = result.ledger[0]
        departure_fuel = stop.fuel_on_arrival_gal + stop.gallons_purchased
        self.assertAlmostEqual(departure_fuel, 50.0, places=4)  # topped up to a full tank


class ShortTripTests(SimpleTestCase):
    def test_trip_within_range_makes_zero_purchases(self):
        result = plan_fuel_purchases([], total_distance_miles=300, tank_capacity_gal=50.0, mpg=10.0)
        self.assertTrue(result.feasible)
        self.assertEqual(result.ledger, [])
        self.assertAlmostEqual(result.final_fuel_gal, 20.0, places=4)  # 50 - 300/10

        summary = summarize_fuel_ledger(result, total_distance_miles=300, mpg=10.0, initial_fuel_gal=50.0)
        self.assertEqual(summary["fuel_purchase_cost_usd"], 0.0)
        self.assertEqual(summary["total_gallons_consumed"], 30.0)  # consumption != 0, distinct from purchases


class InfeasibilityTests(SimpleTestCase):
    def test_gap_exceeding_range_is_reported_as_infeasible_not_silently_substituted(self):
        # One station at mile 400 fills to full (50 gal -> 500mi range), but
        # the remaining 800mi to a 1200mi destination still exceeds range.
        stations = [make_station(400, price=3.00)]
        result = plan_fuel_purchases(stations, total_distance_miles=1200, tank_capacity_gal=50.0, mpg=10.0)

        self.assertFalse(result.feasible)
        self.assertEqual(len(result.ledger), 1)  # partial plan preserved for explanation
        self.assertEqual(result.gap_start_miles, 400)
        self.assertEqual(result.gap_end_miles, 1200)
        self.assertIn("No feasible fuel plan", result.message)
        self.assertIn("available station data", result.message)

    def test_summary_cost_is_null_when_infeasible(self):
        stations = [make_station(400, price=3.00)]
        result = plan_fuel_purchases(stations, total_distance_miles=1200, tank_capacity_gal=50.0, mpg=10.0)
        summary = summarize_fuel_ledger(result, total_distance_miles=1200, mpg=10.0, initial_fuel_gal=50.0)
        self.assertIsNone(summary["fuel_purchase_cost_usd"])


def _brute_force_min_cost(positions_and_prices, destination, capacity):
    """Exhaustive integer-fuel DP oracle (mpg=1, so fuel units == distance
    units). Propagates {fuel_level: min_cost_so_far} through each leg and
    each station's integer buy choices. Returns None if infeasible."""
    INF = float("inf")
    dp = {capacity: 0}
    prev_pos = 0
    stops = list(positions_and_prices) + [(destination, None)]

    for pos, price in stops:
        leg = pos - prev_pos
        advanced = {}
        for fuel, cost in dp.items():
            remaining = fuel - leg
            if remaining >= 0:
                advanced[remaining] = min(advanced.get(remaining, INF), cost)
        dp = advanced
        if not dp:
            return None

        if price is not None:
            bought = {}
            for fuel, cost in dp.items():
                for buy in range(0, capacity - fuel + 1):
                    new_fuel = fuel + buy
                    new_cost = cost + buy * price
                    if new_cost < bought.get(new_fuel, INF):
                        bought[new_fuel] = new_cost
            dp = bought
        prev_pos = pos

    return min(dp.values()) if dp else None


class OracleComparisonTests(SimpleTestCase):
    """Checks the greedy against a brute-force DP oracle on random small instances."""

    def test_greedy_matches_brute_force_on_random_small_instances(self):
        random.seed(1234)
        num_cases = 40
        feasible_cases = infeasible_cases = 0

        for _ in range(num_cases):
            capacity = random.randint(5, 15)
            destination = random.randint(capacity + 5, capacity * 4)
            num_stations = random.randint(1, 5)
            positions = sorted(random.sample(range(1, destination), min(num_stations, destination - 1)))
            prices = [random.randint(1, 10) for _ in positions]

            stations = [make_station(p, price) for p, price in zip(positions, prices)]
            result = plan_fuel_purchases(
                stations, total_distance_miles=destination, tank_capacity_gal=capacity, mpg=1.0,
            )
            oracle_cost = _brute_force_min_cost(list(zip(positions, prices)), destination, capacity)

            if oracle_cost is None:
                infeasible_cases += 1
                self.assertFalse(
                    result.feasible,
                    f"Oracle says infeasible but greedy found a plan: positions={positions} "
                    f"prices={prices} dest={destination} cap={capacity}",
                )
            else:
                feasible_cases += 1
                self.assertTrue(
                    result.feasible,
                    f"Oracle found cost {oracle_cost} but greedy reported infeasible: "
                    f"positions={positions} prices={prices} dest={destination} cap={capacity}",
                )
                greedy_cost = float(total_cost(result))
                self.assertAlmostEqual(
                    greedy_cost, oracle_cost, places=4,
                    msg=(
                        f"Greedy cost {greedy_cost} != oracle optimum {oracle_cost}: "
                        f"positions={positions} prices={prices} dest={destination} cap={capacity}"
                    ),
                )

        # Sanity: the random instances actually exercised both branches.
        self.assertGreater(feasible_cases, 0)
