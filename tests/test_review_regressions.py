"""Independent regression checks for CAP validity, identity and fail-safe geometry.

Run with unittest discovery after putting src on PYTHONPATH. No live requests.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from hazard_alert.core import Store, validate_event
from hazard_alert.adapters import nws, usgs


NOW = "2026-09-14T12:00:00Z"
POLYGON = {"type": "Polygon", "coordinates": [[[-100, 40], [-99, 40], [-99, 41], [-100, 40]]]}


def alert(**overrides):
    props = {
        "id": "review-cap-a", "event": "High Wind Warning", "status": "Actual",
        "sent": "2026-09-14T08:00:00Z", "effective": "2026-09-14T08:00:00Z",
        "onset": "2026-09-14T10:00:00Z", "expires": "2026-09-14T18:00:00Z",
        "ends": "2026-09-15T18:00:00Z", "messageType": "Alert", "affectedZones": [],
    }
    props.update(overrides)
    return {"type": "Feature", "id": "https://api.weather.gov/alerts/review-cap-a",
            "properties": props, "geometry": deepcopy(POLYGON)}


class ReviewRegressions(unittest.TestCase):
    def test_failed_snapshot_does_not_clear_last_known_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite")
            try:
                event = nws.normalize(alert(), NOW)
                summary = {"started_at": NOW, "completed_at": NOW, "status": "success"}
                store.load("nws", "complete", [event], summary)
                store.load("nws", "failed", [], {**summary, "status": "failed"})
                row = store.db.execute("SELECT missing_from_latest_snapshot FROM events").fetchone()
                self.assertEqual(row[0], 0)
                state = store.db.execute("SELECT last_complete_run, status FROM source_state").fetchone()
                self.assertEqual(state, ("complete", "failed"))
            finally:
                store.db.close()

    def test_expired_cap_message_is_not_unqualified_active_until_event_end(self):
        event = nws.normalize(alert(expires="2026-09-14T11:00:00Z"), NOW)
        # A still-possible meteorological event does not make its stale CAP
        # message current; allow explicit stale/unknown/expired implementations.
        self.assertNotEqual(event["lifecycle"], "active")

    def test_feed_generation_time_does_not_create_new_earthquake_revision(self):
        feed = {"type": "FeatureCollection", "metadata": {"status": 200, "generated": 1789387200000},
                "features": [{"type": "Feature", "id": "review-usgs-a", "geometry": {"type": "Point", "coordinates": [-100, 40, 5]},
                              "properties": {"type": "earthquake", "time": 1789380000000,
                                             "updated": 1789380060000, "mag": 3.1, "status": "reviewed"}}]}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite")
            try:
                def load(run_id):
                    events = usgs.normalize(feed, NOW)
                    summary = {"started_at": NOW, "completed_at": NOW, "status": "success"}
                    return store.load("usgs", run_id, events, summary)
                self.assertEqual(load("first"), 1)
                feed["metadata"]["generated"] += 60000
                self.assertEqual(load("second"), 0)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 1)
            finally:
                store.db.close()

    def test_polygon_with_invalid_ring_is_rejected(self):
        event = nws.normalize(alert(), NOW)
        event["geometry"] = {"type": "Polygon", "coordinates": [[[-100, 40], [-99, 40], [-99, 41]]]}
        with self.assertRaises(ValueError):
            validate_event(event)

    def test_one_unsupported_zone_cannot_silently_yield_partial_warning_area(self):
        zone_a = "https://api.weather.gov/zones/forecast/XXZ001"
        zone_b = "https://api.weather.gov/zones/forecast/XXZ002"
        feature = alert(affectedZones=[zone_a, zone_b])
        feature["geometry"] = None

        class Client:
            zone_limit = 8

            def get_json(self, url):
                if url == nws.ACTIVE_URL:
                    return {"type": "FeatureCollection", "features": [feature]}
                if url.startswith("https://api.weather.gov/alerts?"):
                    return {"type": "FeatureCollection", "features": []}
                if url.startswith("https://api.weather.gov/zones?"):
                    return {"type": "FeatureCollection", "features": [
                        {"properties": {"id": "XXZ001"}, "geometry": deepcopy(POLYGON)},
                        {"properties": {"id": "XXZ002"}, "geometry": {"type": "Point", "coordinates": [-90, 35]}},
                    ]}
                if url == zone_a:
                    return {"geometry": deepcopy(POLYGON)}
                if url == zone_b:
                    return {"geometry": {"type": "Point", "coordinates": [-90, 35]}}
                raise AssertionError(url)

        event = nws.collect(Client(), NOW)["events"][0]
        self.assertIsNone(event["geometry"])
        self.assertIn("zone_geometry_unresolved", event["quality_flags"])


if __name__ == "__main__":
    unittest.main()
