import json
import unittest
from pathlib import Path


class ReferenceDataTest(unittest.TestCase):
    def test_geospatial_reference_is_closed(self):
        data = json.loads((Path(__file__).parents[1] / "reference" / "domain.json").read_text(encoding="utf-8"))
        self.assertEqual(data["domain"], "crop-spraying")
        boundary = data["field"]["boundary"]
        self.assertEqual(boundary[0], boundary[-1])
        self.assertLess(data["chemical_label"]["min_ml_per_mu"], data["chemical_label"]["max_ml_per_mu"])
        self.assertEqual(len({p["point_id"] for p in data["track_points"]}), len(data["track_points"]))


if __name__ == "__main__":
    unittest.main()
