from django.test import SimpleTestCase

from routing.services.geo_utils import (
    build_route_segments, haversine_miles, nearest_point_on_route,
    project_point_onto_segment, route_bounding_box,
)


class HaversineTests(SimpleTestCase):
    def test_known_distance_chicago_to_dallas(self):
        # Straight-line (great-circle) distance, not road distance.
        chicago = (41.87897, -87.66063)
        dallas = (32.7767, -96.7970)
        miles = haversine_miles(*chicago, *dallas)
        self.assertAlmostEqual(miles, 802, delta=15)

    def test_zero_distance_for_identical_points(self):
        self.assertAlmostEqual(haversine_miles(40.0, -90.0, 40.0, -90.0), 0.0, places=6)


class RouteBoundingBoxTests(SimpleTestCase):
    def test_box_contains_all_points_with_margin(self):
        coords = [[-90.0, 40.0], [-91.0, 41.0], [-89.5, 39.5]]
        bbox = route_bounding_box(coords, margin_miles=10)
        for lon, lat in coords:
            self.assertGreaterEqual(lat, bbox["min_lat"])
            self.assertLessEqual(lat, bbox["max_lat"])
            self.assertGreaterEqual(lon, bbox["min_lon"])
            self.assertLessEqual(lon, bbox["max_lon"])


class ProjectionTests(SimpleTestCase):
    def test_point_on_segment_has_near_zero_perpendicular_distance(self):
        segments, _ = build_route_segments([[-90.0, 40.0], [-89.0, 40.0]])
        # Midpoint of the segment, essentially on the line.
        perp, cum = project_point_onto_segment(40.0, -89.5, segments[0])
        self.assertLess(perp, 0.5)
        self.assertGreater(cum, 0)
        self.assertLess(cum, segments[0].length_miles)

    def test_point_off_segment_has_proportional_perpendicular_distance(self):
        segments, _ = build_route_segments([[-90.0, 40.0], [-89.0, 40.0]])
        # ~1 degree of longitude east-west segment near lat 40; offset north by
        # a known small amount and confirm perpendicular distance scales with it.
        near_perp, _ = project_point_onto_segment(40.01, -89.5, segments[0])
        far_perp, _ = project_point_onto_segment(40.1, -89.5, segments[0])
        self.assertGreater(far_perp, near_perp)

    def test_nearest_point_on_route_picks_closest_of_multiple_segments(self):
        coords = [[-90.0, 40.0], [-89.0, 40.0], [-89.0, 41.0]]
        segments, _ = build_route_segments(coords)
        # Point near the second segment (the vertical leg).
        perp, cum = nearest_point_on_route(40.5, -89.0, segments)
        self.assertLess(perp, 1.0)


class CumulativeDistanceTests(SimpleTestCase):
    def test_segments_sum_to_raw_total_without_rescale(self):
        coords = [[-90.0, 40.0], [-89.0, 40.0], [-88.0, 40.0]]
        segments, total = build_route_segments(coords)
        self.assertAlmostEqual(segments[-1].cum_start_miles + segments[-1].length_miles, total, places=6)

    def test_rescales_to_authoritative_total(self):
        coords = [[-90.0, 40.0], [-89.0, 40.0], [-88.0, 40.0]]
        authoritative_total = 999.0
        segments, total = build_route_segments(coords, authoritative_total_miles=authoritative_total)
        self.assertAlmostEqual(total, authoritative_total, places=6)
        self.assertAlmostEqual(
            segments[-1].cum_start_miles + segments[-1].length_miles, authoritative_total, places=6
        )
