import unittest
from urllib.parse import parse_qs, urlparse

from hazard_alert.adapters import usgs, wildfire


NOW = "2026-09-14T12:00:00Z"
MS = 1789387200000


def collection(features):
    return {"type": "FeatureCollection", "features": features}


def earthquake(**properties):
    return {"type": "Feature", "id": "us-test", "geometry": {"type": "Point", "coordinates": [-155, 19, 12]},
            "properties": {"type": "earthquake", "mag": 4, "time": MS, "updated": MS,
                           "status": "reviewed", "magType": "mw", **properties}}


def incident(**properties):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-120, 40]},
            "properties": {"OBJECTID": 1, "IrwinID": "{ABC}", "IncidentTypeCategory": "WF",
                           "IncidentName": "Test fire", "ModifiedOnDateTime_dt": MS,
                           "ActiveFireCandidate": 1, "PercentContained": None, **properties}}


def perimeter(**properties):
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[-120, 40], [-119, 40], [-119, 41], [-120, 40]]]},
            "properties": {"OBJECTID": 2, "poly_IRWINID": "abc", "attr_IrwinID": "{aBc}",
                           "attr_IncidentTypeCategory": "WF", "poly_PolygonDateTime": MS,
                           "poly_DateCurrent": MS, **properties}}


GRID = b'''<shakemap_grid xmlns="urn:test">
<grid_specification lon_min="-120" lon_max="-119" lat_min="40" lat_max="40" nlon="2" nlat="1"/>
<grid_field index="1" name="MMI" units="intensity"/>
<grid_field index="2" name="LAT" units="dd"/>
<grid_field index="3" name="PGA" units="%g"/>
<grid_field index="4" name="LON" units="dd"/>
<grid_data>5 40 3.2 -120
6 40 7.1 -119</grid_data></shakemap_grid>'''


class USGSTests(unittest.TestCase):
    def test_non_earthquake_excluded_and_epicenter_not_buffered(self):
        events = usgs.normalize(collection([earthquake(), earthquake(type="quarry blast")]), NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["geometry_role"], "point")
        self.assertEqual(len(events[0]["geometry"]["coordinates"]), 2)
        self.assertEqual(events[0]["metrics"]["depth_km"], 12)
        self.assertEqual(events[0]["lifecycle"], "observed")
        self.assertIsNone(events[0]["expires_at"])
        self.assertIn("epicenter_is_not_damage_footprint", events[0]["quality_flags"])
        self.assertNotIn("feed_generated_at", events[0]["metrics"])

    def test_invalid_coordinates_and_nonfinite_magnitude_are_unknown(self):
        feature = earthquake(mag=float("nan"))
        feature["geometry"]["coordinates"] = [999, 40]
        event = usgs.normalize(collection([feature]), NOW)[0]
        self.assertIsNone(event["geometry"])
        self.assertIsNone(event["metrics"]["magnitude"])

    def test_shakemap_deletion_supersedes_old_update(self):
        products = [{"source": "us", "code": "a", "status": "UPDATE", "updateTime": 1, "preferredWeight": 100},
                    {"source": "us", "code": "a", "status": "DELETE", "updateTime": 2, "preferredWeight": 100},
                    {"source": "ci", "code": "b", "status": "UPDATE", "updateTime": 1, "preferredWeight": 50}]
        selected = usgs.preferred_shakemap({"properties": {"products": {"shakemap": products}}})
        self.assertEqual(selected["code"], "b")

    def test_grid_uses_declared_field_order_and_preserves_units(self):
        grid = usgs.parse_shakemap_grid(GRID)
        sample = usgs.sample_shakemap_grid(grid, -119.1, 40)
        self.assertEqual(sample["values"]["MMI"], 6)
        self.assertEqual(sample["values"]["PGA"], 7.1)
        self.assertEqual(sample["units"]["PGA"], "%g")

    def test_outside_grid_is_unknown_not_zero_or_clamped(self):
        sample = usgs.sample_shakemap_grid(usgs.parse_shakemap_grid(GRID), -75, 40)
        self.assertEqual(sample, {"status": "unknown", "reason": "outside_shakemap_extent"})

    def test_grid_truncation_and_entities_rejected(self):
        with self.assertRaises(ValueError):
            usgs.parse_shakemap_grid(GRID.replace(b'nlon="2"', b'nlon="3"'))
        with self.assertRaises(ValueError):
            usgs.parse_shakemap_grid(b'<!DOCTYPE foo [<!ENTITY x "xx">]><foo/>')

    def test_optional_enrichment_failure_retains_earthquake(self):
        class Client:
            def get_json(self, url):
                if url == usgs.FEED_URL:
                    return collection([earthquake(types=",shakemap,", detail="https://earthquake.usgs.gov/test")])
                raise RuntimeError("temporary upstream outage")
        result = usgs.collect(Client(), NOW)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["events"]), 1)
        self.assertIn("shakemap_enrichment_failed", result["events"][0]["quality_flags"])


class WildfireTests(unittest.TestCase):
    def test_join_uses_case_normalized_irwin_and_origin_codes_not_impact_codes(self):
        event = wildfire.normalize(collection([incident(POOState="US-CA", POOFips="06001")]), collection([perimeter()]), NOW)[0]
        self.assertEqual(event["source_record_id"], "abc")
        self.assertEqual(event["geometry_role"], "observed_perimeter")
        self.assertEqual(event["area_codes"]["origin_county_fips"], ["06001"])
        self.assertNotIn("county_fips", event["area_codes"])

    def test_missing_perimeter_is_point_and_unknown_extent(self):
        event = wildfire.normalize(collection([incident()]), collection([]), NOW)[0]
        self.assertEqual(event["geometry_role"], "point")
        self.assertIn("perimeter_unavailable", event["quality_flags"])
        self.assertIsNone(event["metrics"]["containment_percent"])

    def test_stale_record_unknown_does_not_expire_and_stale_geometry_flagged(self):
        old = MS - 3 * 86400000
        event = wildfire.normalize(collection([incident(ModifiedOnDateTime_dt=old)]), collection([perimeter(poly_PolygonDateTime=old)]), NOW)[0]
        self.assertEqual(event["lifecycle"], "unknown")
        self.assertIsNone(event["expires_at"])
        self.assertIn("stale_incident", event["quality_flags"])
        self.assertIn("stale_perimeter", event["quality_flags"])

    def test_contained_is_not_extinguished_rx_excluded(self):
        events = wildfire.normalize(collection([incident(PercentContained=100), incident(IncidentTypeCategory="RX", IrwinID="rx")]), collection([]), NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["lifecycle"], "active")
        self.assertIn("fully_contained_does_not_mean_extinguished", events[0]["quality_flags"])

    def test_perimeter_only_incident_survives_join(self):
        events = wildfire.normalize(collection([]), collection([perimeter(attr_ModifiedOnDateTime_dt=MS, attr_ActiveFireCandidate=1)]), NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["geometry_role"], "observed_perimeter")
        self.assertIn("incident_point_missing_perimeter_attributes_used", events[0]["quality_flags"])

    def test_future_fire_out_is_unknown_not_prematurely_expired(self):
        event = wildfire.normalize(collection([incident(FireOutDateTime=MS + 3600000)]), collection([]), NOW)[0]
        self.assertEqual(event["lifecycle"], "unknown")
        self.assertIn("fire_out_time_in_future", event["quality_flags"])

    def test_snapshot_missing_id_marks_incomplete(self):
        class Client:
            def get_json(self, url):
                if "returnIdsOnly" in url:
                    return {"objectIds": [1, 2]}
                return collection([{**incident(), "properties": {**incident()["properties"], "OBJECTID": 1}}])
        payload, complete, count = wildfire._get_layer(Client(), wildfire.INCIDENT_URL, "IncidentTypeCategory", wildfire.INCIDENT_FIELDS, 25)
        self.assertFalse(complete)
        self.assertEqual(count, 2)

    def test_arcgis_http200_error_not_empty_success(self):
        class Client:
            def get_json(self, url):
                return {"error": {"code": 400, "message": "Invalid query"}}
        with self.assertRaises(ValueError):
            wildfire._get_layer(Client(), wildfire.INCIDENT_URL, "IncidentTypeCategory", wildfire.INCIDENT_FIELDS, 25)

    def test_perimeter_outage_retains_points_and_marks_incomplete(self):
        class Client:
            def get_json(self, url):
                if wildfire.PERIMETER_URL in url:
                    raise RuntimeError("temporary failure")
                if "returnIdsOnly" in url:
                    return {"objectIds": [1]}
                return collection([incident()])
        result = wildfire.collect(Client(), NOW)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["events"]), 1)
        self.assertIn("perimeter_feed_unavailable", result["events"][0]["quality_flags"])


if __name__ == "__main__":
    unittest.main()
