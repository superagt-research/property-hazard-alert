"""NWS CAP alerts: original polygons first, forecast/county zones as coarse fallback."""
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlencode, urlparse

SOURCE = "nws"
DEFAULT_INTERVAL_SECONDS = 60
ACTIVE_URL = "https://api.weather.gov/alerts/active?status=actual"

# Product-specific allowlist: do not silently turn every public-safety product
# into a building-loss indicator. Severe convection retains a combined class.
GROUPS = {
    "tornado": ["Tornado Warning", "Tornado Watch"],
    "severe_convective_storm": ["Severe Thunderstorm Warning", "Severe Thunderstorm Watch"],
    "flash_flood": ["Flash Flood Warning", "Flash Flood Watch"],
    "river_flood": ["Flood Warning", "Flood Watch", "Flood Advisory"],
    "coastal_flood": ["Coastal Flood Warning", "Coastal Flood Watch", "Coastal Flood Advisory", "Lakeshore Flood Warning", "Lakeshore Flood Watch", "Lakeshore Flood Advisory"],
    "storm_surge": ["Storm Surge Warning", "Storm Surge Watch"],
    "tropical_cyclone": ["Hurricane Warning", "Hurricane Watch", "Tropical Storm Warning", "Tropical Storm Watch", "Typhoon Warning", "Typhoon Watch", "Hurricane Local Statement"],
    "high_wind": ["High Wind Warning", "High Wind Watch", "Wind Advisory", "Extreme Wind Warning"],
    "winter_storm": ["Winter Storm Warning", "Winter Storm Watch", "Winter Weather Advisory", "Blizzard Warning", "Blizzard Watch", "Lake Effect Snow Warning", "Heavy Snow Warning", "Snow Squall Warning"],
    "ice_storm": ["Ice Storm Warning"],
    "freeze": ["Freeze Warning", "Freeze Watch", "Hard Freeze Warning", "Hard Freeze Watch", "Extreme Cold Warning", "Extreme Cold Watch", "Cold Weather Advisory"],
    "fire_weather": ["Red Flag Warning", "Fire Weather Watch"],
    "tsunami": ["Tsunami Warning", "Tsunami Watch", "Tsunami Advisory"],
    "ashfall": ["Ashfall Warning", "Ashfall Advisory"],
}
EVENT_MAP = {event: hazard for hazard, events in GROUPS.items() for event in events}


def normalize(feature, now):
    p = feature.get("properties") or {}
    event = p.get("event", "")
    if p.get("status") != "Actual" or event not in EVENT_MAP:
        return None
    record_id = p.get("id") or feature.get("id")
    if not record_id:
        raise ValueError("NWS alert has no CAP identifier")
    parameters = p.get("parameters") or {}
    param_text = " ".join(str(v) for v in parameters.values()).upper()
    hazard = EVENT_MAP[event]
    evidence = "potential" if event.endswith("Watch") or hazard == "fire_weather" else "forecast"
    # CAP certainty=Observed is preserved, but source-specific radar/report tags
    # provide the more precise evidence label when supplied.
    if "RADAR INDICATED" in param_text or "RADAR_INDICATED" in param_text:
        evidence = "radar_indicated"
    elif "OBSERVED" in param_text or p.get("certainty") == "Observed":
        evidence = "observed"
    expires = p.get("expires")
    msg = p.get("messageType")
    lifecycle = "retracted" if msg == "Cancel" else "active"
    if lifecycle != "retracted" and expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.fromisoformat(now.replace("Z", "+00:00")):
        lifecycle = "expired"
    flags = ["us_scope_requires_property_spatial_match"]
    geometry = feature.get("geometry")
    if not geometry:
        flags.append("native_geometry_missing")
    if hazard == "fire_weather":
        flags.append("fire_weather_is_not_active_wildfire")
    if hazard == "freeze":
        flags.append("freeze_product_not_building_pipe_temperature")
    if hazard == "severe_convective_storm":
        flags.append("hail_and_wind_not_independently_confirmed")
    refs = []
    for ref in p.get("references") or []:
        if isinstance(ref, dict) and ref.get("identifier"):
            refs.append({"relation": "cancels" if msg == "Cancel" else "supersedes", "source_record_id": ref["identifier"]})
    return {
        "source": SOURCE, "source_record_id": str(record_id), "source_event": event,
        "hazard_types": [hazard], "headline": p.get("headline") or event,
        "description": p.get("description"), "source_url": p.get("@id") or feature.get("id"),
        "issued_at": p.get("sent"), "source_updated_at": p.get("sent"),
        "effective_at": p.get("effective"), "expires_at": expires,
        "onset_at": p.get("onset"), "event_ends_at": p.get("ends"),
        "lifecycle": lifecycle, "evidence": evidence, "severity": p.get("severity", "Unknown"),
        "geometry": geometry, "geometry_role": "warning_polygon" if geometry else "unknown",
        "area_codes": {**(p.get("geocode") or {}), "affected_zones": p.get("affectedZones") or [], "area_description": p.get("areaDesc")},
        "metrics": {"cap_certainty": p.get("certainty"), "cap_urgency": p.get("urgency"), "cap_message_type": msg,
                    "parameters_native": parameters, "vtec": parameters.get("VTEC", []), "cap_effective_at": p.get("effective"), "cap_expires_at": p.get("expires"), "cap_ends_at": p.get("ends")},
        "quality_flags": flags, "relationships": refs,
    }


def _pages(client, url):
    seen = set()
    features = []
    updated = None
    while url:
        if url in seen or len(seen) >= 100:
            raise ValueError("NWS pagination loop or limit")
        if urlparse(url).hostname != "api.weather.gov":
            raise ValueError("Unexpected NWS pagination host")
        seen.add(url)
        data = client.get_json(url)
        if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
            raise ValueError("NWS response is not a FeatureCollection")
        features.extend(data["features"])
        updated = data.get("updated") or updated
        url = (data.get("pagination") or {}).get("next")
    return features, updated, len(seen)


def collect(client, now):
    features, updated, pages = _pages(client, ACTIVE_URL)
    events = []
    excluded = {}
    seen = set()
    for f in features:
        e = normalize(f, now)
        if e:
            events.append(e)
            seen.add(e["source_record_id"])
        else:
            name = (f.get("properties") or {}).get("event", "unknown")
            excluded[name] = excluded.get(name, 0) + 1
    # Rolling overlap catches recent explicit cancellation/update references.
    # The active inventory and this changes window have different semantics.
    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    start_dt = now_dt - timedelta(minutes=20)
    last_success = getattr(client, "last_success_at", None)
    retention_gap = False
    if last_success:
        previous = datetime.fromisoformat(last_success.replace("Z", "+00:00")) - timedelta(minutes=5)
        start_dt = min(start_dt, previous)
        retention_gap = start_dt < now_dt - timedelta(days=7)
        start_dt = max(start_dt, now_dt - timedelta(days=7) + timedelta(minutes=1))
    start = start_dt.isoformat()
    changes_ok, change_error = True, None
    try:
        recent, _, _ = _pages(client, "https://api.weather.gov/alerts?" + urlencode({"start": start, "status": "actual", "limit": 500}))
        for f in recent:
            e = normalize(f, now)
            if e and e["source_record_id"] not in seen:
                seen.add(e["source_record_id"])
                events.append(e)
    except Exception as exc:
        changes_ok, change_error = False, str(exc)
    zone_cache = {}
    zone_errors = {}
    max_zones = getattr(client, "zone_limit", 1000)
    needed = sorted({z for e in events if not e["geometry"] for z in e["area_codes"]["affected_zones"]
                     if re.fullmatch(r"https://api\.weather\.gov/zones/(forecast|county|fire)/[A-Z0-9]+", z)})
    selected = needed[:max_zones]
    for zone_type in ("forecast", "county", "fire"):
        group = [z for z in selected if "/" + zone_type + "/" in z]
        for offset in range(0, len(group), 50):
            chunk = group[offset:offset + 50]
            url = "https://api.weather.gov/zones?" + urlencode({"type": zone_type, "id": ",".join(z.rsplit("/", 1)[1] for z in chunk), "include_geometry": "true"})
            try:
                zone_features, _, _ = _pages(client, url)
                by_id = {(f.get("properties") or {}).get("id"): f.get("geometry") for f in zone_features}
                for z in chunk:
                    zone_cache[z] = by_id.get(z.rsplit("/", 1)[1])
            except Exception as exc:
                for z in chunk:
                    zone_errors[z] = str(exc)
    # The documented include_geometry=true was observed returning null geometry
    # in live list responses. Resolve missing selected zones via detail endpoints.
    for z in selected:
        if not zone_cache.get(z):
            try:
                zone_cache[z] = client.get_json(z).get("geometry")
                zone_errors.pop(z, None)
            except Exception as exc:
                zone_errors[z] = str(exc)
    for e in events:
        if e["geometry"]:
            continue
        zones = e["area_codes"]["affected_zones"]
        parts = []
        resolved = 0
        for url in zones:
            if not re.fullmatch(r"https://api\.weather\.gov/zones/(forecast|county|fire)/[A-Z0-9]+", url):
                continue
            g = zone_cache.get(url)
            if g and g.get("type") == "Polygon":
                parts.append(g["coordinates"])
                resolved += 1
            elif g and g.get("type") == "MultiPolygon":
                parts.extend(g["coordinates"])
                resolved += 1
        if parts and resolved == len(zones):
            e["geometry"] = {"type": "MultiPolygon", "coordinates": parts}
            e["geometry_role"] = "forecast_or_county_zone"
            e["quality_flags"].append("coarse_zone_not_damage_footprint")
        else:
            # Never expose a partial zone union as the complete warning area.
            e["quality_flags"].append("zone_geometry_unresolved")
    return {"events": events, "complete": changes_ok, "metrics": {
        "active_feed_records": len(features), "normalized_records": len(events), "pages": pages,
        "feed_updated_at": updated, "excluded_by_event": excluded, "zone_requests_succeeded": sum(g is not None for g in zone_cache.values()),
        "zone_request_errors": zone_errors, "zone_budget": max_zones,
        "geometry_present": sum(e["geometry"] is not None for e in events),
        "recent_change_window_minutes": 20, "change_feed_error": change_error,
        "actual_change_window_start": start, "history_retention_gap_requires_review": retention_gap,
        "inventory_scope": "NWS national products; US property and territory scope applied downstream",
    }}
