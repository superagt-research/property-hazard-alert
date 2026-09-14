from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from hazard_alert.core import Store, PublicClient, validate_event
from hazard_alert.adapters import nws

NOW = "2026-09-14T12:00:00Z"


def event(identifier="a", **changes):
    p = {"id": identifier, "status": "Actual", "event": "Tornado Warning", "sent": NOW,
         "effective": NOW, "expires": "2026-09-14T13:00:00Z", "messageType": "Alert"}
    p.update(changes)
    return nws.normalize({"type": "Feature", "properties": p, "geometry": None}, NOW)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "db.sqlite")
        self.summary = {"started_at": NOW, "completed_at": NOW, "status": "success"}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_repeat_and_old_revision(self):
        self.assertEqual(self.store.load("nws", "a", [event()], self.summary), 1)
        self.assertEqual(self.store.load("nws", "b", [event()], self.summary), 0)
        older = event(sent="2026-09-14T10:00:00Z")
        self.assertEqual(self.store.load("nws", "c", [older], self.summary), 0)
        self.assertEqual(self.store.db.execute("SELECT source_updated_at FROM events").fetchone()[0], NOW)

    def test_partial_never_publishes_current(self):
        self.store.load("nws", "a", [event()], self.summary)
        self.store.load("nws", "b", [event("b")], {**self.summary, "status": "partial"})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 2)

    def test_cancel_has_auditable_reference(self):
        self.store.load("nws", "a", [event()], self.summary)
        cancel = event("b", messageType="Cancel", references=[{"identifier": "a"}])
        self.store.load("nws", "b", [cancel], self.summary)
        self.assertEqual(cancel["lifecycle"], "retracted")
        self.assertEqual(self.store.db.execute("SELECT old_event_id,action FROM supersessions").fetchone(), ("nws:a", "cancels"))

    def test_absence_is_not_release(self):
        self.store.load("nws", "a", [event()], self.summary)
        self.store.load("nws", "b", [], self.summary)
        row = self.store.db.execute("SELECT missing_from_latest_snapshot,payload_json FROM events").fetchone()
        self.assertEqual(row[0], 1)
        self.assertIn('"lifecycle":"active"', row[1])


class NormalizationTests(unittest.TestCase):
    def test_watch_and_fire_weather_not_observed(self):
        self.assertEqual(event(event="Tornado Watch")["evidence"], "potential")
        self.assertIn("fire_weather_is_not_active_wildfire", event(event="Red Flag Warning")["quality_flags"])

    def test_tests_and_marine_excluded(self):
        self.assertIsNone(event(status="Test"))
        self.assertIsNone(event(event="Small Craft Advisory"))

    def test_radar_does_not_become_confirmed_tornado(self):
        self.assertEqual(event(parameters={"tornadoDetection": ["RADAR INDICATED"]})["evidence"], "radar_indicated")

    def test_unknown_schema_quarantines(self):
        bad = event()
        bad["effective_at"] = "2026-09-14T12:00:00"
        with self.assertRaises(ValueError):
            validate_event(bad)

    def test_url_boundary(self):
        for url in ("http://api.weather.gov/a", "https://api.weather.gov.evil.test/a", "https://127.0.0.1/a", "https://user:pw@api.weather.gov/a"):
            with self.assertRaises(ValueError):
                PublicClient.check_url(url)


if __name__ == "__main__":
    unittest.main()
