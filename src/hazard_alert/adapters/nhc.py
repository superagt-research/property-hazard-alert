"""NHC active cyclone inventory and linked KML/KMZ geometry, standard library only.

All basin candidates are retained. A forecast cone is NOT a damaging wind
footprint. Initial wind extent is an analysis estimate, not a ground observation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import math
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from zipfile import ZipFile

SOURCE = "nhc"
DEFAULT_INTERVAL_SECONDS = 300
INDEX_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
PRODUCTS = {
    "trackCone": ("forecast_cone", "forecast"),
    "forecastTrack": ("forecast_track", "forecast"),
    "initialWindExtent": ("observed_wind_extent", "unknown"),
    "forecastWindRadiiGIS": ("forecast_wind_extent", "forecast"),
}


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _time(value):
    if not value or str(value).startswith("0001-"):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        return None


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(node, name):
    return next((x for x in node if _local(x.tag) == name), None)


def _text(node, name):
    item = _child(node, name)
    return "" if item is None else (item.text or "").strip()


def _coordinates(text):
    result = []
    for token in (text or "").split():
        parts = token.split(",")
        if len(parts) < 2:
            raise ValueError("invalid KML coordinate")
        lon, lat = _number(parts[0]), _number(parts[1])
        if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("invalid WGS84 coordinate")
        pair = [lon, lat]
        if not result or pair != result[-1]:
            result.append(pair)
    return result


def _ring(node):
    coords = next((x.text for x in node.iter() if _local(x.tag) == "coordinates"), "")
    ring = _coordinates(coords)
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    if len(ring) < 4 or len({tuple(p) for p in ring}) < 3:
        raise ValueError("degenerate KML polygon")
    # Refuse to invent a date-line correction in this feasibility adapter.
    if any(abs(a[0] - b[0]) > 180 for a, b in zip(ring, ring[1:])):
        raise ValueError("antimeridian polygon requires geospatial repair")
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))
    if abs(area) < 1e-10:
        raise ValueError("zero-area KML polygon")
    return ring


def _geometry(node):
    kind = _local(node.tag)
    if kind == "Point":
        coords = _coordinates(_text(node, "coordinates"))
        if len(coords) != 1:
            raise ValueError("invalid KML point")
        return {"type": "Point", "coordinates": coords[0]}
    if kind == "LineString":
        coords = _coordinates(_text(node, "coordinates"))
        if len(coords) < 2:
            raise ValueError("invalid KML line")
        if any(abs(a[0] - b[0]) > 180 for a, b in zip(coords, coords[1:])):
            raise ValueError("antimeridian line requires geospatial repair")
        return {"type": "LineString", "coordinates": coords}
    if kind == "Polygon":
        outer = _child(node, "outerBoundaryIs")
        if outer is None:
            raise ValueError("missing KML outer boundary")
        rings = [_ring(outer)] + [_ring(x) for x in node if _local(x.tag) == "innerBoundaryIs"]
        return {"type": "Polygon", "coordinates": rings}
    if kind == "MultiGeometry":
        parts = [_geometry(x) for x in node if _local(x.tag) in {"Point", "LineString", "Polygon", "MultiGeometry"}]
        if not parts:
            raise ValueError("empty KML MultiGeometry")
        if all(x["type"] == "Polygon" for x in parts):
            return {"type": "MultiPolygon", "coordinates": [x["coordinates"] for x in parts]}
        return {"type": "GeometryCollection", "geometries": parts}
    raise ValueError("unsupported KML geometry")


def parse_kmz(data: bytes) -> tuple[list[dict], list[str]]:
    """Decode inline geometries only; never execute/follow KML links or HTML.

    Invalid placemarks are quarantined with diagnostics. Supports KML 2.1/2.2,
    polygon holes and disjoint polygons. No topology or dateline repair is implied.
    """
    if len(data) > 25_000_000:
        raise ValueError("KMZ exceeds 25 MB safety limit")
    if data.startswith(b"PK"):
        with ZipFile(BytesIO(data)) as archive:
            members = [x for x in archive.infolist() if x.filename.lower().endswith(".kml")]
            if not members or sum(x.file_size for x in members) > 50_000_000:
                raise ValueError("KMZ has no KML or exceeds unpacked size limit")
            documents = [archive.read(x) for x in members]
    else:
        documents = [data]
    features, warnings = [], []
    for document in documents:
        if b"<!DOCTYPE" in document.upper() or b"<!ENTITY" in document.upper():
            raise ValueError("KML XML declarations are not accepted")
        root = ET.fromstring(document)
        for index, placemark in enumerate(x for x in root.iter() if _local(x.tag) == "Placemark"):
            attributes = {"name": _text(placemark, "name"), "placemark_index": index}
            for item in placemark.iter():
                if _local(item.tag) == "Data" and item.get("name"):
                    attributes[item.get("name")] = _text(item, "value")
                if _local(item.tag) == "SimpleData" and item.get("name"):
                    attributes[item.get("name")] = (item.text or "").strip()
            geometries = [x for x in placemark if _local(x.tag) in {"Point", "LineString", "Polygon", "MultiGeometry"}]
            if not geometries:
                warnings.append(f"placemark_{index}:missing_inline_geometry")
                continue
            try:
                parsed = [_geometry(x) for x in geometries]
                geom = parsed[0] if len(parsed) == 1 else {"type": "GeometryCollection", "geometries": parsed}
                features.append({"geometry": geom, "properties": attributes})
            except ValueError as exc:
                warnings.append(f"placemark_{index}:{exc}")
    return features, warnings


def _base(storm, now):
    record_id = str(storm.get("id") or "")
    if not record_id:
        raise ValueError("NHC storm lacks id")
    lon, lat = _number(storm.get("longitudeNumeric")), _number(storm.get("latitudeNumeric"))
    geometry = None
    if lon is not None and lat is not None and -180 <= lon <= 180 and -90 <= lat <= 90:
        geometry = {"type": "Point", "coordinates": [lon, lat]}
    issued = _time(storm.get("lastUpdate"))
    flags = ["us_boundary_spatial_match_pending", "storm_center_is_not_impact_footprint"]
    if not geometry:
        flags.append("missing_valid_geometry")
    if not issued:
        flags.append("missing_source_timestamp")
    elif _time(now) and (datetime.fromisoformat(_time(now).replace("Z", "+00:00")) - datetime.fromisoformat(issued.replace("Z", "+00:00"))).total_seconds() > 9 * 3600:
        flags.append("source_advisory_older_than_9h")
    return {
        "source": SOURCE, "source_record_id": record_id, "hazard_types": ["tropical_cyclone"],
        "source_event": "Active Tropical Cyclone", "headline": f"{storm.get('classification', '')} {storm.get('name', record_id)}",
        "description": "Active cyclone basin candidate; center location alone does not establish U.S. property impact.",
        "source_url": (storm.get("publicAdvisory") or {}).get("url") or INDEX_URL,
        "issued_at": issued, "source_updated_at": issued, "effective_at": issued, "expires_at": None,
        "lifecycle": "active", "evidence": "unknown", "severity": str(storm.get("classification") or "unknown"),
        "geometry": geometry, "geometry_role": "point", "area_codes": {"basin": [record_id[:2].upper()]},
        "metrics": {"storm_id": record_id, "storm_name": storm.get("name"), "max_sustained_wind_kt": _number(storm.get("intensity")),
                    "central_pressure_hpa": _number(storm.get("pressure")), "movement_direction_deg": _number(storm.get("movementDir")),
                    "movement_speed_kt": _number(storm.get("movementSpeed")), "products": {k:v for k,v in storm.items() if isinstance(v, dict)}},
        "quality_flags": flags,
    }


def normalize(payload: dict, now: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("activeStorms"), list):
        raise ValueError("NHC activeStorms array missing; not an empty successful snapshot")
    return [_base(storm, now) for storm in payload["activeStorms"]]


def _official_link(url):
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in {"www.nhc.noaa.gov", "nhc.noaa.gov", "www.hurricanes.gov", "hurricanes.gov"}


def normalize_product(storm, product_name, data: bytes, now: str):
    base = _base(storm, now)
    product = storm[product_name]
    role, evidence = PRODUCTS[product_name]
    features, warnings = parse_kmz(data)
    events = []
    for feature in features:
        props = feature["properties"]
        # Track-point KML omits UTC valid times. Keep the track lines for display;
        # the index already supplies the analyzed current center.
        if product_name == "forecastTrack" and feature["geometry"]["type"] == "Point":
            continue
        event = {**base, "metrics": dict(base["metrics"]), "quality_flags": list(base["quality_flags"])}
        key = f"{props.get('fcstpd', '')}:{props.get('name', '')}:{props['placemark_index']}"
        event["source_record_id"] = f"{storm['id']}:{product_name}:{sha256(key.encode()).hexdigest()[:12]}"
        event.update(source_event=product_name, headline=f"{storm.get('name', storm['id'])}: {product_name}",
                     source_url=product["kmzFile"], issued_at=_time(product.get("issuance")),
                     source_updated_at=_time(product.get("fileUpdateTime")), effective_at=_time(product.get("issuance")),
                     geometry=feature["geometry"], geometry_role=role, evidence=evidence)
        event["relationships"] = [{"relation": "part_of", "source": SOURCE, "source_record_id": storm["id"]}]
        event["metrics"].update(product_type=product_name, advisory_number=product.get("advNum"), kml_attributes=props)
        if role == "forecast_cone":
            event["description"] = "Forecast uncertainty for the cyclone center; this is not a wind, surge, flood or damage footprint."
            event["quality_flags"].append("cone_is_not_wind_footprint")
        elif role == "forecast_wind_extent":
            event["description"] = "Forecast wind radii; this KML does not provide per-polygon valid times. Display for context only."
            event["quality_flags"].append("forecast_valid_time_unavailable")
            event["effective_at"] = None
        elif role == "observed_wind_extent":
            event["description"] = "Analyzed initial wind extent at the advisory time; not proof every point experienced these winds."
            event["quality_flags"].append("analyzed_extent_not_ground_observation")
        else:
            event["description"] = "Forecast center track; not a wind footprint."
            event["quality_flags"].append("track_is_not_impact_footprint")
        if "Wind" in product_name:
            event["metrics"]["wind_threshold_kt"] = _number(props.get("name"))
        events.append(event)
    return events, warnings


def collect(client, now: str) -> dict:
    payload = client.get_json(INDEX_URL)
    events = normalize(payload, now)
    failures, geometry_warnings, fetched = [], [], 0
    for storm in payload["activeStorms"]:
        for product_name in PRODUCTS:
            product = storm.get(product_name)
            if not isinstance(product, dict) or not product.get("kmzFile"):
                continue
            url = product["kmzFile"]
            try:
                if not _official_link(url):
                    raise ValueError("non-official linked product URL")
                more, warnings = normalize_product(storm, product_name, client.get_bytes(url), now)
                events.extend(more)
                fetched += 1
                geometry_warnings.extend({"storm_id":storm["id"], "product":product_name, "warning":warning} for warning in warnings)
            except Exception as exc:
                failures.append({"storm_id":storm["id"], "product":product_name, "error":str(exc)})
    return {"events": events, "metrics": {"active_storms": len(payload["activeStorms"]), "products_fetched": fetched,
            "geometry_quarantine": geometry_warnings, "product_failures": failures, "scope": "all NHC basin candidates; U.S. spatial match pending"},
            "complete": not failures and not geometry_warnings}
