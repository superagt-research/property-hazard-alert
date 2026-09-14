"""Semantic regression tests; synthetic cases are explicit, optional live replay.

Run with: python -m unittest discover -s tests -p test_nhc_nwps.py -v
"""
import copy
import json
from pathlib import Path
import unittest

from hazard_alert.adapters import nhc, nwps

NOW = "2026-09-14T12:49:21Z"
PROBES = Path(__file__).resolve().parent / "fixtures"


def synthetic_storm():
    return {"id":"al992026", "name":"SYNTHETIC", "classification":"TS", "latitudeNumeric":25,
            "longitudeNumeric":-80, "intensity":"35", "lastUpdate":"2026-09-14T09:00:00Z",
            "trackCone":{"issuance":"2026-09-14T09:00:00Z", "kmzFile":"https://www.nhc.noaa.gov/synthetic.kmz"}}


def synthetic_gauge():
    return {"lid":"TEST1", "name":"SYNTHETIC gauge", "latitude":31, "longitude":-95,
            "status":{"observed":{"floodCategory":"minor", "primary":-1, "primaryUnit":"ft", "secondary":-999,
                                     "secondaryUnit":"kcfs", "validTime":"2026-09-14T12:00:00Z"},
                      "forecast":{"floodCategory":"moderate", "primary":3, "primaryUnit":"ft", "validTime":"2026-09-15T12:00:00Z"}}}


class NHCSemantics(unittest.TestCase):
    def test_empty_index_is_valid_but_absent_array_is_not(self):
        self.assertEqual(nhc.normalize({"activeStorms":[]}, NOW), [])
        with self.assertRaises(ValueError):
            nhc.normalize({}, NOW)

    def test_center_cannot_prove_property_impact(self):
        event = nhc.normalize({"activeStorms":[synthetic_storm()]}, NOW)[0]
        self.assertEqual(event["geometry"]["coordinates"], [-80.0,25.0])
        self.assertIn("us_boundary_spatial_match_pending", event["quality_flags"])
        self.assertEqual(event["evidence"], "unknown")

    def test_kml21_polygon_with_hole_and_cone_semantics(self):
        kml = b'''<kml xmlns="http://earth.google.com/kml/2.1"><Placemark><Polygon>
        <outerBoundaryIs><LinearRing><coordinates>-80,20 -79,20 -79,21 -80,21 -80,20</coordinates></LinearRing></outerBoundaryIs>
        <innerBoundaryIs><LinearRing><coordinates>-79.8,20.2 -79.2,20.2 -79.2,20.8 -79.8,20.2</coordinates></LinearRing></innerBoundaryIs>
        </Polygon></Placemark></kml>'''
        events, warnings = nhc.normalize_product(synthetic_storm(), "trackCone", kml, NOW)
        self.assertFalse(warnings)
        self.assertEqual(len(events[0]["geometry"]["coordinates"]), 2)
        self.assertEqual(events[0]["evidence"], "forecast")
        self.assertIn("cone_is_not_wind_footprint", events[0]["quality_flags"])

    def test_degenerate_polygon_is_quarantined(self):
        features, warnings = nhc.parse_kmz(b'<kml><Placemark><Polygon><outerBoundaryIs><LinearRing><coordinates>-80,20 -80,20 -80,20</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>')
        self.assertEqual(features, [])
        self.assertIn("degenerate", warnings[0])

    def test_antimeridian_cannot_be_silently_used_as_planar_polygon(self):
        features, warnings = nhc.parse_kmz(b'<kml><Placemark><Polygon><outerBoundaryIs><LinearRing><coordinates>179,20 -179,20 -179,21 179,21 179,20</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>')
        self.assertEqual(features, [])
        self.assertIn("antimeridian", warnings[0])

    def test_xml_entities_are_rejected(self):
        with self.assertRaises(ValueError):
            nhc.parse_kmz(b'<!DOCTYPE kml [<!ENTITY x "data">]><kml/>')

    def test_forecast_wind_has_no_invented_valid_time(self):
        storm = synthetic_storm()
        storm["forecastWindRadiiGIS"] = storm["trackCone"]
        kml = b'<kml><Placemark><name>34</name><Polygon><outerBoundaryIs><LinearRing><coordinates>-80,20 -79,20 -79,21 -80,20</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>'
        event = nhc.normalize_product(storm,"forecastWindRadiiGIS", kml,NOW)[0][0]
        self.assertIsNone(event["effective_at"])
        self.assertEqual(event["metrics"]["wind_threshold_kt"],34)
        self.assertEqual(event["geometry_role"],"forecast_wind_extent")

    def test_link_rejects_unapproved_host(self):
        self.assertFalse(nhc._official_link("https://www.nhc.noaa.gov.evil.example/test.kmz"))
        self.assertFalse(nhc._official_link("http://www.nhc.noaa.gov/test.kmz"))

    @unittest.skipUnless((PROBES / "nhc_current.json").exists(), "captured live feed unavailable")
    def test_captured_live_geometry_replay(self):
        payload = json.loads((PROBES / "nhc_current.json").read_text())
        count = 0
        for storm in payload["activeStorms"]:
            for key in nhc.PRODUCTS:
                path = PROBES / f"{storm['id']}_{key}.kmz"
                if path.exists():
                    events, _ = nhc.normalize_product(storm,key,path.read_bytes(),NOW)
                    count += len(events)
        self.assertGreater(count, 0)


class NWPSSemantics(unittest.TestCase):
    def test_missing_sentinels_and_negative_valid_stage(self):
        self.assertIsNone(nwps.clean_number(-999))
        self.assertIsNone(nwps.clean_number("-9999"))
        self.assertEqual(nwps.clean_number(-1.5),-1.5)
        self.assertIsNone(nwps.clean_number("NaN"))
        self.assertIsNone(nwps.clean_time("0001-01-01T00:00:00Z"))

    def test_observed_and_forecast_are_distinct(self):
        events = nwps.normalize_gauge(synthetic_gauge(),NOW)
        self.assertEqual([e["evidence"] for e in events],["observed","forecast"])
        self.assertNotEqual(events[0]["source_record_id"], events[1]["source_record_id"])
        self.assertIsNone(events[1]["issued_at"])
        self.assertIn("forecast_issue_time_unavailable_in_summary",events[1]["quality_flags"])
        self.assertAlmostEqual(events[0]["metrics"]["primary_value_m"],-.3048)

    def test_old_flood_observation_is_unknown_not_current(self):
        gauge = synthetic_gauge()
        gauge["status"]["observed"]["validTime"] = "2026-09-10T12:00:00Z"
        event = nwps.normalize_gauge(gauge,NOW)[0]
        self.assertEqual(event["lifecycle"], "unknown")
        self.assertIn("observation_older_than_3h",event["quality_flags"])

    def test_missing_forecast_is_not_no_flooding(self):
        gauge = synthetic_gauge()
        gauge["status"] = {"forecast":{"floodCategory":"fcst_not_current", "primary":-999,"validTime":"0001-01-01T00:00:00Z"}}
        self.assertEqual(nwps.normalize_gauge(gauge,NOW),[])

    def test_action_stage_is_potential(self):
        gauge = synthetic_gauge()
        gauge["status"]["observed"]["floodCategory"] = "action"
        event = nwps.normalize_gauge(gauge,NOW)[0]
        self.assertEqual(event["evidence"],"potential")
        self.assertIn("action_stage_does_not_mean_flooding",event["quality_flags"])

    def test_out_of_service_flood_cannot_be_current(self):
        gauge = synthetic_gauge()
        gauge["inService"] = {"enabled":False}
        self.assertTrue(all(x["lifecycle"]=="unknown" for x in nwps.normalize_gauge(gauge,NOW)))

    def test_hydrograph_revision_and_units(self):
        payload = {"forecast":{"primaryName":"Flow", "primaryUnits":"kcfs", "issuedTime":"2026-09-14T12:00:00Z",
                               "data":[{"validTime":"2026-09-15T12:00:00Z", "generatedTime":"2026-09-14T10:00:00Z","primary":2},
                                       {"validTime":"2026-09-15T12:00:00Z", "generatedTime":"2026-09-14T11:00:00Z","primary":3}]}}
        result = nwps.normalize_stageflow(payload,NOW)["forecast"]
        self.assertEqual(result["primary_unit"],"kcfs")
        self.assertEqual(len(result["data"]),1)
        self.assertEqual(result["data"][0]["primary_value"],3)

    def test_missing_array_raises(self):
        with self.assertRaises(ValueError):
            nwps.normalize({},NOW)

    @unittest.skipUnless((PROBES / "nwps_lolt2_stageflow.json").exists(), "captured hydrograph unavailable")
    def test_captured_live_hydrograph(self):
        payload = json.loads((PROBES / "nwps_lolt2_stageflow.json").read_text())
        result = nwps.normalize_stageflow(payload,NOW)
        self.assertGreater(len(result["observed"]["data"]),0)
        self.assertEqual(result["forecast"]["data"],[])
        self.assertIsNone(result["forecast"]["issued_at"])


if __name__ == "__main__":
    unittest.main()
