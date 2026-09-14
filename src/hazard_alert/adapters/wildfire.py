"""NIFC/WFIGS public current wildfire incidents joined to observed perimeters.

Use IRWIN IDs, not names, as the join key. A missing perimeter is unknown spatial
extent, and a stale record or a disappearing row does not establish extinction.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import math
from urllib.parse import urlencode

SOURCE = "nifc"
DEFAULT_INTERVAL_SECONDS = 300
INCIDENT_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0"
PERIMETER_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0"
STALE_INCIDENT_HOURS = 24
STALE_PERIMETER_HOURS = 24

INCIDENT_FIELDS = [
    "OBJECTID", "IrwinID", "IncidentName", "IncidentTypeCategory", "IncidentSize", "PercentContained",
    "FireDiscoveryDateTime", "ModifiedOnDateTime_dt", "CreatedOnDateTime_dt", "ICS209ReportDateTime",
    "ContainmentDateTime", "ControlDateTime", "FireOutDateTime", "FireBehaviorGeneral",
    "POOState", "POOFips", "POOCounty", "ActiveFireCandidate", "IsCpxChild", "CpxID", "CpxName",
    "UniqueFireIdentifier", "GlobalID",
]
PERIMETER_FIELDS = [
    "OBJECTID", "poly_IRWINID", "poly_IncidentName", "poly_DateCurrent", "poly_PolygonDateTime",
    "poly_GISAcres", "poly_MapMethod", "poly_Source", "GlobalID",
] + ["attr_" + f for f in INCIDENT_FIELDS if f not in ("OBJECTID", "GlobalID")]


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _time(value):
    number = _number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _id(value):
    return str(value).strip().strip("{}").lower() if value else None


def _age_hours(time_string, now):
    if not time_string:
        return None
    return (datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.fromisoformat(time_string.replace("Z", "+00:00"))).total_seconds() / 3600


def _get_layer(client, url, type_field, fields, page_size):
    """Snapshot IDs first, then fetch immutable ID batches without offset races.

    New incidents appearing after ID capture wait for the next run. A requested
    ID missing from a batch makes the snapshot incomplete and cannot clear alerts.
    """
    ids_url = url + "/query?" + urlencode({"where": type_field + "='WF'", "returnIdsOnly": "true", "f": "json"})
    response = client.get_json(ids_url)
    if response.get("error") or not isinstance(response.get("objectIds"), list):
        raise ValueError("NIFC ID query failed or lacked objectIds")
    ids = sorted(set(response["objectIds"]))
    if len(ids) > 100000:
        raise ValueError("NIFC current snapshot unexpectedly exceeds 100000 records")
    features, returned = [], set()
    complete = True
    for offset in range(0, len(ids), page_size):
        batch = ids[offset:offset + page_size]
        query = {"objectIds": ",".join(str(i) for i in batch), "outFields": ",".join(fields),
                 "returnGeometry": "true", "outSR": "4326", "f": "geojson"}
        page = client.get_json(url + "/query?" + urlencode(query))
        if page.get("error") or page.get("type") != "FeatureCollection" or not isinstance(page.get("features"), list):
            raise ValueError("NIFC feature query failed or returned an invalid collection")
        if page.get("exceededTransferLimit") or page.get("properties", {}).get("exceededTransferLimit"):
            complete = False
        for feature in page["features"]:
            oid = feature.get("properties", {}).get("OBJECTID", feature.get("id"))
            if oid not in batch or oid in returned:
                complete = False
                continue
            returned.add(oid)
            features.append(feature)
    return {"type": "FeatureCollection", "features": features}, complete and returned == set(ids), len(ids)


def normalize(incidents: dict, perimeters: dict, now: str) -> list[dict]:
    for payload in (incidents, perimeters):
        if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
            raise ValueError("NIFC payload is not a GeoJSON FeatureCollection")
    perimeter_by_id = defaultdict(list)
    for feature in perimeters["features"]:
        p = feature.get("properties") or {}
        if p.get("attr_IncidentTypeCategory") != "WF":
            continue
        key = _id(p.get("attr_IrwinID") or p.get("poly_IRWINID"))
        if key:
            perimeter_by_id[key].append(feature)
    all_incidents = list(incidents["features"])
    incident_ids = {_id((f.get("properties") or {}).get("IrwinID")) for f in all_incidents}
    # A perimeter feed can publish a fire before the points layer catches up.
    for key, perimeter_features in perimeter_by_id.items():
        if key in incident_ids:
            continue
        feature = max(perimeter_features, key=lambda f: _number(f["properties"].get("poly_DateCurrent")) or 0)
        p = feature["properties"]
        properties = {name[5:]: value for name, value in p.items() if name.startswith("attr_")}
        properties["IrwinID"] = key
        properties["_perimeter_only"] = True
        all_incidents.append({"type": "Feature", "geometry": None, "properties": properties})
    events = []
    seen = set()
    # Prefer latest row if the upstream temporarily duplicates an IRWIN identity.
    all_incidents.sort(key=lambda f: _number((f.get("properties") or {}).get("ModifiedOnDateTime_dt")) or 0, reverse=True)
    duplicates = Counter(_id((f.get("properties") or {}).get("IrwinID")) for f in all_incidents)
    for feature in all_incidents:
        p = feature.get("properties") or {}
        if p.get("IncidentTypeCategory") != "WF":
            continue
        key = _id(p.get("IrwinID"))
        flags = []
        if not key:
            fallback = p.get("UniqueFireIdentifier") or p.get("GlobalID")
            if not fallback:
                raise ValueError("NIFC wildfire lacks a stable IRWIN or alternate identity")
            key = "fallback:" + str(fallback).lower()
            flags.append("missing_irwin_id")
        if key in seen:
            continue
        seen.add(key)
        if duplicates[key] > 1:
            flags.append("duplicate_irwin_rows_latest_selected")
        if p.get("_perimeter_only"):
            flags.append("incident_point_missing_perimeter_attributes_used")
        geometry = feature.get("geometry")
        if not geometry or geometry.get("type") != "Point":
            geometry = None
        role = "point" if geometry else "unknown"
        perimeter_time = None
        polygon_update = None
        perimeter_metrics = {}
        polygons = perimeter_by_id.get(key, [])
        if polygons:
            selected = max(polygons, key=lambda f: (_number(f["properties"].get("poly_PolygonDateTime")) or 0,
                                                    _number(f["properties"].get("poly_DateCurrent")) or 0))
            poly = selected["properties"]
            candidate = selected.get("geometry")
            if candidate and candidate.get("type") in ("Polygon", "MultiPolygon"):
                geometry, role = candidate, "observed_perimeter"
            else:
                flags.append("invalid_perimeter_geometry")
            perimeter_time = _time(poly.get("poly_PolygonDateTime"))
            polygon_update = _time(poly.get("poly_DateCurrent"))
            perimeter_metrics = {
                "perimeter_area_acres": _number(poly.get("poly_GISAcres")),
                "perimeter_observed_at": perimeter_time, "perimeter_record_updated_at": polygon_update,
                "perimeter_mapping_method": poly.get("poly_MapMethod"), "perimeter_source": poly.get("poly_Source"),
            }
            if len(polygons) > 1:
                flags.append("multiple_perimeters_latest_observation_selected")
            if perimeter_time is None:
                flags.append("perimeter_observation_time_unknown")
            elif _age_hours(perimeter_time, now) > STALE_PERIMETER_HOURS:
                flags.append("stale_perimeter")
            elif _age_hours(perimeter_time, now) < -0.25:
                flags.append("perimeter_observation_time_in_future")
            flags.append("observed_perimeter_not_spread_forecast")
        if role != "observed_perimeter":
            flags.extend(["perimeter_unavailable", "incident_point_is_not_fire_extent"])
        updated = _time(p.get("ModifiedOnDateTime_dt"))
        source_updated = max([t for t in (updated, polygon_update) if t], default=None)
        discovery = _time(p.get("FireDiscoveryDateTime"))
        report_at = _time(p.get("ICS209ReportDateTime"))
        out_at = _time(p.get("FireOutDateTime"))
        lifecycle = "active"
        if out_at and _age_hours(out_at, now) < 0:
            lifecycle = "unknown"
            flags.append("fire_out_time_in_future")
        elif out_at:
            lifecycle = "expired"
        elif updated is None:
            flags.append("incident_update_time_unknown")
            lifecycle = "unknown"
        elif _age_hours(updated, now) > STALE_INCIDENT_HOURS:
            flags.append("stale_incident")
            lifecycle = "unknown"
        elif _age_hours(updated, now) < -0.25:
            flags.append("incident_update_time_in_future")
            lifecycle = "unknown"
        percent = _number(p.get("PercentContained"))
        if percent is not None and not 0 <= percent <= 100:
            percent = None
            flags.append("invalid_containment_percent")
        if percent is None:
            flags.append("containment_unknown")
        elif percent == 100 and not out_at:
            flags.append("fully_contained_does_not_mean_extinguished")
        if p.get("ActiveFireCandidate") not in (True, 1):
            flags.append("active_candidate_not_confirmed")
            if lifecycle == "active":
                lifecycle = "unknown"
        area = _number(p.get("IncidentSize"))
        if area is not None and area < 0:
            area = None
            flags.append("invalid_incident_area")
        state = str(p.get("POOState") or "").removeprefix("US-")
        fips = str(p.get("POOFips") or "").strip()
        if fips and (not fips.isdigit() or len(fips) != 5):
            flags.append("invalid_origin_county_fips")
            fips = ""
        name = str(p.get("IncidentName") or "Unnamed wildfire").strip()
        relationships = []
        if p.get("CpxID"):
            relationships.append({"type": "member_of_fire_complex", "source": SOURCE,
                                  "source_record_id": _id(p["CpxID"]), "name": p.get("CpxName")})
        events.append({
            "source": SOURCE, "source_record_id": key, "hazard_types": ["wildfire"],
            "source_event": "Wildfire incident", "headline": name + (f" — {state}" if state else ""),
            "description": "NIFC current wildfire record. Perimeter, where supplied, is an observation; absence or containment does not establish no property exposure.",
            "source_url": PERIMETER_URL if role == "observed_perimeter" else INCIDENT_URL,
            "issued_at": discovery, "source_updated_at": source_updated,
            "effective_at": discovery, "expires_at": out_at,
            "lifecycle": lifecycle, "evidence": "observed", "severity": p.get("FireBehaviorGeneral") or "Unknown",
            "geometry": geometry, "geometry_role": role,
            # Origin codes locate the ignition record; they are never impact-area codes.
            "area_codes": {"origin_state": [state] if state else [], "origin_county_fips": [fips] if fips else [],
                           "origin_county_name": [p["POOCounty"]] if p.get("POOCounty") else []},
            "metrics": {"incident_area_acres": area, "containment_percent": percent,
                        "incident_record_updated_at": updated, "incident_reported_at": report_at,
                        "contained_at": _time(p.get("ContainmentDateTime")), "controlled_at": _time(p.get("ControlDateTime")),
                        "fire_out_at": out_at, "active_fire_candidate": p.get("ActiveFireCandidate"),
                        "unique_fire_identifier": p.get("UniqueFireIdentifier"), **perimeter_metrics},
            "quality_flags": flags, "relationships": relationships,
        })
    return events


def collect(client, now: str) -> dict:
    # Keep GET URLs below ArcGIS/CDN request-line limits (200 IDs caused HTTP 404
    # in a live probe even though the same official query with 50 IDs succeeds).
    incidents, incident_complete, incident_count = _get_layer(client, INCIDENT_URL, "IncidentTypeCategory", INCIDENT_FIELDS, 50)
    metrics = {"incident_snapshot_ids": incident_count, "incident_rows": len(incidents["features"]),
               "perimeter_snapshot_ids": None, "perimeter_rows": 0, "perimeter_feed_available": True}
    try:
        perimeters, perimeter_complete, perimeter_count = _get_layer(client, PERIMETER_URL, "attr_IncidentTypeCategory", PERIMETER_FIELDS, 25)
        metrics.update(perimeter_snapshot_ids=perimeter_count, perimeter_rows=len(perimeters["features"]))
    except Exception as exc:
        perimeters = {"type": "FeatureCollection", "features": []}
        perimeter_complete = False
        metrics.update(perimeter_feed_available=False, perimeter_error_type=type(exc).__name__)
    events = normalize(incidents, perimeters, now)
    if not metrics["perimeter_feed_available"]:
        for event in events:
            event["quality_flags"].append("perimeter_feed_unavailable")
    metrics["normalized_count"] = len(events)
    metrics["with_perimeter"] = sum(e["geometry_role"] == "observed_perimeter" for e in events)
    metrics["stale_incident_count"] = sum("stale_incident" in e["quality_flags"] for e in events)
    metrics["stale_perimeter_count"] = sum("stale_perimeter" in e["quality_flags"] for e in events)
    return {"events": events, "metrics": metrics, "complete": incident_complete and perimeter_complete}
