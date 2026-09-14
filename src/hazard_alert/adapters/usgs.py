"""USGS earthquakes and versioned ShakeMap enrichment; no synthetic damage circles.

The global feed is deliberately retained. Geographic relevance must be evaluated
against the insured portfolio and an authoritative US boundary downstream.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

SOURCE = "usgs"
DEFAULT_INTERVAL_SECONDS = 60
FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
MAX_SHAKEMAP_ENRICHMENTS = 10
MAX_GRID_BYTES = 24 * 1024 * 1024


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


def _official_url(url):
    parsed = urlparse(url or "")
    return parsed.scheme == "https" and parsed.hostname == "earthquake.usgs.gov"


def normalize(payload: dict, now: str) -> list[dict]:
    if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise ValueError("USGS response is not a GeoJSON FeatureCollection")
    if payload.get("metadata", {}).get("status", 200) != 200:
        raise ValueError("USGS feed reported a non-success status")
    events = []
    for feature in payload["features"]:
        p = feature.get("properties") or {}
        # Quarry blasts, explosions and other non-earthquake events are excluded.
        if p.get("type") != "earthquake":
            continue
        if not feature.get("id"):
            raise ValueError("USGS earthquake lacks a stable event ID")
        flags = ["global_feed_requires_us_or_portfolio_spatial_filter", "epicenter_is_not_damage_footprint"]
        geometry = feature.get("geometry")
        coordinates = geometry.get("coordinates", []) if geometry else []
        if geometry and geometry.get("type") == "Point" and len(coordinates) >= 2:
            lon, lat = _number(coordinates[0]), _number(coordinates[1])
            if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
                geometry = None
            else:
                # USGS's third ordinate is depth in km, not GeoJSON altitude in
                # metres; keep depth in explicit metrics and normalize to 2D.
                geometry = {"type": "Point", "coordinates": [lon, lat]}
        else:
            geometry = None
        if geometry is None:
            flags.append("missing_or_invalid_geometry")
        if p.get("status") != "reviewed":
            flags.append("preliminary_earthquake_parameters")
        mag = _number(p.get("mag"))
        if mag is None:
            flags.append("magnitude_unknown")
        if not _time(p.get("time")):
            flags.append("event_time_unknown")
        source_url = p.get("url")
        if not _official_url(source_url):
            source_url = "https://earthquake.usgs.gov/earthquakes/eventpage/" + str(feature["id"])
        events.append({
            "source": SOURCE, "source_record_id": str(feature["id"]),
            "hazard_types": ["earthquake"], "source_event": "Earthquake",
            "headline": p.get("title") or f"M{mag if mag is not None else '?'} earthquake — {p.get('place') or 'location unknown'}",
            "description": "Observed earthquake epicenter. Magnitude is not property-level shaking or loss. Tsunami flag is not a tsunami warning.",
            "source_url": source_url,
            "issued_at": _time(p.get("time")), "source_updated_at": _time(p.get("updated")),
            "effective_at": _time(p.get("time")), "expires_at": None,
            "lifecycle": "retracted" if p.get("status") == "deleted" else "observed",
            "evidence": "observed", "severity": p.get("alert") or "Unknown",
            "geometry": geometry, "geometry_role": "point" if geometry else "unknown",
            "area_codes": {},
            "metrics": {
                "magnitude": mag, "magnitude_type": p.get("magType"),
                "depth_km": _number(coordinates[2]) if len(coordinates) > 2 else None,
                "maximum_instrumental_mmi": _number(p.get("mmi")),
                "maximum_reported_cdi": _number(p.get("cdi")),
                "pager_alert": p.get("alert"), "usgs_review_status": p.get("status"),
                "tsunami_screening_flag": p.get("tsunami"),
                "detail_url": p.get("detail") if _official_url(p.get("detail")) else None,
                "upstream_alias_ids": [i for i in str(p.get("ids") or "").split(",") if i],
                "available_product_types": [i for i in str(p.get("types") or "").split(",") if i],
            },
            "quality_flags": flags,
        })
    return events


def preferred_shakemap(detail: dict) -> dict | None:
    """Respect product weight, retain only live ACTUAL maps, then latest revision."""
    products = detail.get("properties", {}).get("products", {}).get("shakemap", [])
    # A deletion supersedes an older update for the same source/code.
    latest = {}
    for product in products:
        key = (product.get("source"), product.get("code"))
        if key not in latest or (product.get("updateTime") or 0) > (latest[key].get("updateTime") or 0):
            latest[key] = product
    eligible = [p for p in latest.values()
                if p.get("status") != "DELETE"
                and p.get("properties", {}).get("event-type", "ACTUAL").upper() == "ACTUAL"
                and p.get("properties", {}).get("map-status", "").upper() != "CANCELLED"]
    return max(eligible, key=lambda p: (p.get("preferredWeight") or 0, p.get("updateTime") or 0), default=None)


def parse_shakemap_grid(content: bytes) -> dict:
    """Parse self-described XML fields; do not assume column order or PGA units.

    Returns data needed to reproduce intensity checks, with no polygon inference.
    Grids mix observations and model estimates and are not observed damage maps.
    """
    if len(content) > MAX_GRID_BYTES:
        raise ValueError("ShakeMap grid exceeds the prototype byte limit")
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("DTD/entity declarations are not accepted")
    root = ET.fromstring(content)
    def elements(name):
        return [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == name]
    fields = sorted(elements("grid_field"), key=lambda e: int(e.attrib["index"]))
    if not fields or [int(e.attrib["index"]) for e in fields] != list(range(1, len(fields) + 1)):
        raise ValueError("Invalid or non-contiguous ShakeMap grid_field indexes")
    names = [el.attrib["name"].upper() for el in fields]
    if len(names) != len(set(names)) or not {"LON", "LAT", "MMI"}.issubset(names):
        raise ValueError("ShakeMap grid lacks unique LON/LAT/MMI fields")
    data = elements("grid_data")
    if len(data) != 1:
        raise ValueError("ShakeMap grid_data missing or ambiguous")
    rows = []
    for line in (data[0].text or "").splitlines():
        if not line.strip():
            continue
        row = [float(x) for x in line.split()]
        if len(row) != len(names) or not all(math.isfinite(x) for x in row):
            raise ValueError("Invalid ShakeMap grid row")
        rows.append(row)
    specifications = elements("grid_specification")
    specification = specifications[0].attrib if specifications else {}
    expected = int(specification.get("nlat", 0)) * int(specification.get("nlon", 0))
    if not rows or (expected and len(rows) != expected):
        raise ValueError("ShakeMap grid row count does not match its specification")
    return {
        "fields": names, "units": {el.attrib["name"].upper(): el.attrib.get("units") for el in fields},
        "rows": rows, "specification": dict(specification), "metadata": dict(root.attrib),
    }


def summarize_shakemap_grid(grid: dict) -> dict:
    names, rows = grid["fields"], grid["rows"]
    metrics = {"cell_count": len(rows), "units": grid["units"], "specification": grid["specification"]}
    for name in ("MMI", "PGA", "PGV", "STDPGA", "STDMMI"):
        if name in names:
            values = [r[names.index(name)] for r in rows]
            metrics[name.lower() + "_min"] = min(values)
            metrics[name.lower() + "_max"] = max(values)
    return metrics


def sample_shakemap_grid(grid: dict, longitude: float, latitude: float) -> dict:
    """Nearest grid node for feasibility only; outside extent is unknown, never 0.

    This approximation is not a certified property loss calculation. Production
    should use the authoritative raster/HDF surface and agreed interpolation.
    """
    lon, lat = _number(longitude), _number(latitude)
    if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return {"status": "unknown", "reason": "invalid_coordinates"}
    s = grid["specification"]
    try:
        if not (float(s["lon_min"]) <= lon <= float(s["lon_max"]) and float(s["lat_min"]) <= lat <= float(s["lat_max"])):
            return {"status": "unknown", "reason": "outside_shakemap_extent"}
    except (KeyError, TypeError, ValueError):
        return {"status": "unknown", "reason": "missing_shakemap_extent"}
    names = grid["fields"]
    x, y = names.index("LON"), names.index("LAT")
    lon_scale = math.cos(math.radians(lat))
    row = min(grid["rows"], key=lambda r: ((r[x] - lon) * lon_scale) ** 2 + (r[y] - lat) ** 2)
    return {"status": "estimated", "method": "nearest_grid_node",
            "node_longitude": row[x], "node_latitude": row[y],
            "values": {name: row[i] for i, name in enumerate(names) if name not in ("LON", "LAT")},
            "units": grid["units"], "quality_flags": ["modelled_shaking_not_observed_damage", "nearest_node_approximation"]}


def collect(client, now: str) -> dict:
    payload = client.get_json(FEED_URL)
    events = normalize(payload, now)
    metrics = {"upstream_event_count": len(payload["features"]), "normalized_count": len(events),
               "feed_generated_at": _time(payload.get("metadata", {}).get("generated")),
               "scope": "global_feed_pending_downstream_us_portfolio_filter", "shakemap_enriched": 0,
               "shakemap_grids_parsed": 0, "shakemap_errors": 0, "shakemap_deferred": 0}
    candidates = [e for e in events if "shakemap" in e["metrics"]["available_product_types"]]
    candidates.sort(key=lambda e: e["metrics"]["magnitude"] or 0, reverse=True)
    for index, event in enumerate(candidates):
        if index >= MAX_SHAKEMAP_ENRICHMENTS:
            event["quality_flags"].append("shakemap_enrichment_deferred")
            metrics["shakemap_deferred"] += 1
            continue
        try:
            url = event["metrics"]["detail_url"]
            if not url:
                raise ValueError("Missing official event detail URL")
            detail = client.get_json(url)
            product = preferred_shakemap(detail)
            if not product:
                event["quality_flags"].append("shakemap_missing_or_retracted")
                continue
            props = product.get("properties", {})
            content = product.get("contents", {})
            grid_item = content.get("download/grid.xml") or {}
            grid_url = grid_item.get("url")
            event["metrics"]["shakemap"] = {
                "source": product.get("source"), "code": product.get("code"),
                "version": props.get("version"), "updated_at": _time(product.get("updateTime")),
                "review_status": props.get("review-status"),
                "maximum_mmi": _number(props.get("maxmmi-grid") or props.get("maxmmi")),
                "grid_url": grid_url if _official_url(grid_url) else None,
                "method": "instrument_and_model_based_shaking_estimate",
            }
            event["quality_flags"].append("shakemap_is_modelled_shaking_not_damage")
            metrics["shakemap_enriched"] += 1
            if not _official_url(grid_url):
                event["quality_flags"].append("shakemap_grid_unavailable")
                continue
            if (_number(grid_item.get("length")) or 0) > MAX_GRID_BYTES:
                event["quality_flags"].append("shakemap_grid_deferred_byte_limit")
                metrics["shakemap_deferred"] += 1
                continue
            grid = parse_shakemap_grid(client.get_bytes(grid_url))
            event["metrics"]["shakemap"]["grid_summary"] = summarize_shakemap_grid(grid)
            metrics["shakemap_grids_parsed"] += 1
        except Exception as exc:
            # A failed optional detail feed never erases a valid observed event.
            event["quality_flags"].append("shakemap_enrichment_failed")
            event["metrics"]["shakemap_error_type"] = type(exc).__name__
            metrics["shakemap_errors"] += 1
    expected = payload.get("metadata", {}).get("count")
    complete = expected is None or expected == len(payload["features"])
    metrics["optional_enrichment_complete"] = not metrics["shakemap_errors"] and not metrics["shakemap_deferred"]
    return {"events": events, "metrics": metrics, "complete": complete}
