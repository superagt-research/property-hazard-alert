"""NWPS national gauge flood status. Point observations are not inundation maps."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
import os
from urllib.parse import urlencode

SOURCE = "nwps"
DEFAULT_INTERVAL_SECONDS = 900
INDEX_URL = "https://api.water.noaa.gov/nwps/v1/gauges"
DEFAULT_BBOX = "-90,24,-75,37"  # Bounded southeast feasibility sample, NOT national.
FLOOD_CATEGORIES = {"action", "minor", "moderate", "major"}
MISSING_CATEGORIES = {"not_defined", "obs_not_current", "fcst_not_current", "no_data", "not_available", "unknown"}
MISSING_VALUES = {-999.0, -9999.0, -99999.0}


def clean_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number not in MISSING_VALUES else None
    except (ValueError, TypeError):
        return None


def clean_time(value):
    if not value or str(value).startswith("0001-"):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        return None


def _dt(value):
    result = clean_time(value)
    return datetime.fromisoformat(result.replace("Z", "+00:00")) if result else None


def normalize_gauge(gauge: dict, now: str) -> list[dict]:
    """Emit only flood/action categories, retaining stale floods as unknown.

    Non-flood statuses stay in the raw snapshot and health metrics; omission is
    not a moratorium-release signal. Future validTime is not forecast issue time.
    """
    lid = str(gauge.get("lid") or "")
    if not lid:
        raise ValueError("NWPS gauge lacks lid")
    lon, lat = clean_number(gauge.get("longitude")), clean_number(gauge.get("latitude"))
    geometry = {"type": "Point", "coordinates": [lon, lat]} if lon is not None and lat is not None and -180 <= lon <= 180 and -90 <= lat <= 90 else None
    events = []
    for kind in ("observed", "forecast"):
        status = (gauge.get("status") or {}).get(kind) or {}
        category = str(status.get("floodCategory") or "unknown").lower()
        if category not in FLOOD_CATEGORIES:
            continue
        valid = clean_time(status.get("validTime"))
        primary = clean_number(status.get("primary"))
        secondary = clean_number(status.get("secondary"))
        flags = ["gauge_point_is_not_inundation_footprint", "property_reach_relationship_required"]
        if not geometry:
            flags.append("missing_valid_geometry")
        if primary is None or not valid:
            flags.append("missing_measurement_or_valid_time")
        lifecycle = "observed" if kind == "observed" else "active"
        if kind == "observed" and valid and _dt(now):
            age = (_dt(now) - _dt(valid)).total_seconds()
            if age > 3 * 3600:
                flags.append("observation_older_than_3h")
                lifecycle = "unknown"
            elif age < -900:
                flags.append("observation_time_in_future")
                lifecycle = "unknown"
        if kind == "forecast":
            flags.append("forecast_issue_time_unavailable_in_summary")
            if valid and _dt(now) and _dt(valid) < _dt(now):
                flags.append("forecast_valid_time_in_past")
                lifecycle = "unknown"
        if gauge.get("inService", {}).get("enabled") is False:
            flags.append("gauge_out_of_service")
            lifecycle = "unknown"
        if category == "action":
            flags.append("action_stage_does_not_mean_flooding")
        if primary is None or not valid:
            lifecycle = "unknown"
        unit = status.get("primaryUnit") or None
        secondary_unit = status.get("secondaryUnit") or None
        pedts = (gauge.get("pedts") or {}).get(kind) or None
        events.append({
            "source": SOURCE, "source_record_id": f"{lid}:{kind}", "hazard_types": ["river_flood"],
            "source_event": f"Gauge {kind} {category}", "headline": f"{gauge.get('name', lid)}: {kind} {category}",
            "description": "River gauge status at one point. Does not establish inundation at a building or across the containing county.",
            "source_url": f"https://water.noaa.gov/gauges/{lid}",
            "issued_at": None, "source_updated_at": None,
            "effective_at": valid, "expires_at": None,
            "lifecycle": lifecycle, "evidence": "potential" if category == "action" else kind,
            "severity": category, "geometry": geometry, "geometry_role": "point",
            "area_codes": {key: [gauge[key]["abbreviation"]] for key in ("state", "wfo", "rfc") if isinstance(gauge.get(key),dict) and gauge[key].get("abbreviation")},
            "metrics": {"gauge_id": lid, "reach_id": gauge.get("reachId"), "usgs_id": gauge.get("usgsId"),
                        "measurement_kind": kind, "flood_category": category, "primary_value": primary, "primary_unit": unit,
                        "secondary_value": secondary, "secondary_unit": secondary_unit, "pedts": pedts,
                        "primary_value_m": primary * 0.3048 if unit == "ft" and primary is not None else None,
                        "stage_datum": gauge.get("datums"), "flood_thresholds": gauge.get("flood", {}).get("categories")},
            "quality_flags": flags,
        })
    return events


def normalize_stageflow(payload: dict, now: str) -> dict:
    """Clean a targeted hydrograph; do not infer stage from flow-only products."""
    result = {}
    for kind in ("observed", "forecast"):
        part = payload.get(kind) or {}
        rows = []
        for item in part.get("data") or []:
            valid = clean_time(item.get("validTime"))
            if not valid:
                continue
            rows.append({"valid_at": valid, "generated_at": clean_time(item.get("generatedTime")),
                         "primary_value": clean_number(item.get("primary")), "secondary_value": clean_number(item.get("secondary"))})
        # Preserve the latest generated revision at each valid time.
        unique = {}
        for row in sorted(rows, key=lambda x: (x["valid_at"], x["generated_at"] or "")):
            unique[row["valid_at"]] = row
        result[kind] = {"evidence":kind, "issued_at":clean_time(part.get("issuedTime")) if kind == "forecast" else None,
                        "pedts":part.get("pedts") or None,"primary_name":part.get("primaryName") or None,
                        "primary_unit":part.get("primaryUnits") or None,"secondary_name":part.get("secondaryName") or None,
                        "secondary_unit":part.get("secondaryUnits") or None,"data":list(unique.values())}
    return result


def normalize(payload: dict, now: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("gauges"), list):
        raise ValueError("NWPS gauges array missing; not an empty successful snapshot")
    return [event for gauge in payload["gauges"] for event in normalize_gauge(gauge, now)]


def collect(client, now: str) -> dict:
    gauge_ids = [part.strip().upper() for part in os.getenv("NWPS_GAUGE_IDS", "").split(",") if part.strip()]
    scope = os.getenv("NWPS_SCOPE", "sample").lower()
    if gauge_ids:
        if any(not lid.isalnum() or len(lid) > 12 for lid in gauge_ids):
            raise ValueError("NWPS_GAUGE_IDS must contain alphanumeric identifiers")
        payload = {"gauges": [client.get_json(f"{INDEX_URL}/{lid}") for lid in dict.fromkeys(gauge_ids)]}
        coverage_scope = "configured_gauge_sample"
        query_scope = {"gauge_ids": list(dict.fromkeys(gauge_ids))}
    elif scope == "national":
        payload = client.get_json(INDEX_URL)
        coverage_scope = "national_inventory_requested"
        query_scope = {}
    else:
        try:
            bbox = [float(x.strip()) for x in os.getenv("NWPS_BBOX", DEFAULT_BBOX).split(",")]
        except ValueError as exc:
            raise ValueError("NWPS_BBOX requires west,south,east,north") from exc
        if len(bbox) != 4 or not all(math.isfinite(x) for x in bbox) or not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90):
            raise ValueError("NWPS_BBOX requires valid west,south,east,north")
        # Canonical numbers keep semantically identical bounding boxes on the
        # same upstream cache key (e.g. -90 and -90.0).
        query = urlencode(dict(zip(("bbox.xmin", "bbox.ymin", "bbox.xmax", "bbox.ymax"), [format(x, ".12g") for x in bbox])) | {"srid":"EPSG_4326"})
        payload = client.get_json(f"{INDEX_URL}?{query}")
        coverage_scope = "bounded_bbox_sample_not_national"
        query_scope = {"bbox":bbox, "srid":"EPSG_4326"}
    events = normalize(payload, now)
    for event in events:
        event["metrics"]["coverage_scope"] = coverage_scope
        if coverage_scope != "national_inventory_requested":
            event["quality_flags"].append("collection_scope_is_sample_not_national")
    counts = {kind: dict(Counter(str((g.get("status") or {}).get(kind, {}).get("floodCategory") or "unknown") for g in payload["gauges"])) for kind in ("observed", "forecast")}
    return {"events":events, "complete": True,
            "metrics":{"gauges_received":len(payload["gauges"]), "flood_status_counts":counts,
                       "events_emitted":len(events), "coverage_scope":coverage_scope, "query_scope":query_scope,
                       "scope":"NWPS gauge points; not a property inundation map",
                       "stale_observation_event_count":sum("observation_older_than_3h" in x["quality_flags"] for x in events),
                       "forecast_summaries_without_issue_time":sum(x["metrics"]["measurement_kind"]=="forecast" for x in events)}}
