import math
import unittest

from crop_spray import geo


class GeoTest(unittest.TestCase):
    def test_haversine_known_distance(self):
        # 经度方向 0.001 度在纬度 30 附近约 96.5 米
        d = geo.haversine_m((120.0, 30.0), (120.001, 30.0))
        self.assertAlmostEqual(d, 96.3, places=1)

    def test_point_in_polygon(self):
        poly = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]
        self.assertTrue(geo.point_in_polygon((0.5, 0.5), poly))
        self.assertFalse(geo.point_in_polygon((1.5, 0.5), poly))

    def test_ring_area(self):
        # 100 m × 100 m 方块
        lat = 30.2
        dlng = 100 / (111_320 * math.cos(math.radians(lat)))
        dlat = 100 / 110_540
        poly = [(0.0, lat), (dlng, lat), (dlng, lat + dlat), (0.0, lat + dlat),
                (0.0, lat)]
        area = geo.ring_area_m2(poly)
        self.assertAlmostEqual(area, 10_000, delta=50)

    def test_buffer_polygon_contains_center_and_excludes_far(self):
        ring = geo.point_buffer_polygon((120.106, 30.203), 120)
        self.assertTrue(geo.point_in_polygon((120.106, 30.203), ring))
        self.assertFalse(geo.point_in_polygon((120.110, 30.203), ring))

    def test_raster_dedup(self):
        pts = [(120.0 + i * 1e-6, 30.0) for i in range(10)]
        once = geo.raster_covered_cells(pts, 2.0)
        twice = geo.raster_covered_cells(pts + pts, 2.0)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
