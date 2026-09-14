"""Shared raw archive, quality checks and idempotent local storage.

SQLite proves the data contract locally; production should use S3 + PostGIS.
No cloud resources, policies, rating rules or external flags are modified.
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler


def utcnow():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Naive datetime has no source timezone")
    return dt.astimezone(timezone.utc)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


class PublicClient:
    def __init__(self, root, source, run_id, zone_limit=1000):
        self.root = Path(root)
        self.source = source
        self.run_id = run_id
        self.zone_limit = zone_limit
        self.requests = []
        self.cache_dir = self.root / "http_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        class CheckedRedirect(HTTPRedirectHandler):
            def redirect_request(handler, request, fp, code, msg, headers, newurl):
                self.check_url(newurl)
                return super().redirect_request(request, fp, code, msg, headers, newurl)
        self.opener = build_opener(CheckedRedirect())

    @staticmethod
    def check_url(url):
        p = urlparse(url)
        host = (p.hostname or "").lower()
        allowed = ("weather.gov", "noaa.gov", "usgs.gov", "arcgis.com", "nifc.gov")
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or not any(host == s or host.endswith("." + s) for s in allowed):
            raise ValueError("Only configured official public HTTPS hosts are allowed: " + host)

    def get_bytes(self, url):
        self.check_url(url)
        key = digest(url.encode())
        cache_meta = self.cache_dir / (key + ".json")
        cached = json.loads(cache_meta.read_text()) if cache_meta.exists() else {}
        headers = {"User-Agent": os.getenv("HAZARD_USER_AGENT", "PropertyHazardFeasibility/0.1"), "Accept": "*/*"}
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
        started = utcnow()
        for attempt in range(3):
            try:
                with self.opener.open(Request(url, headers=headers), timeout=35) as response:
                    self.check_url(response.url)
                    payload = response.read(80 * 1024 * 1024 + 1)
                    if len(payload) > 80 * 1024 * 1024:
                        raise ValueError("Response exceeds feasibility download limit (80 MiB)")
                    keep = {"content-type", "etag", "last-modified", "date", "age", "cache-control", "expires", "x-request-id"}
                    meta = {k: v for k, v in response.headers.items() if k.lower() in keep}
                    status = response.status
                break
            except HTTPError as exc:
                if exc.code == 304 and cached.get("raw_path") and Path(cached["raw_path"]).exists():
                    payload = Path(cached["raw_path"]).read_bytes()
                    meta = cached.get("headers", {})
                    status = 304
                    break
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    self.requests.append({"url": url, "requested_at": started, "status": exc.code, "error": str(exc)})
                    raise
                retry = exc.headers.get("Retry-After", "")
                try:
                    delay = float(retry)
                except ValueError:
                    try:
                        delay = (email.utils.parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError):
                        delay = 2 ** attempt + random.random()
                if delay > 55:
                    raise RuntimeError("Retry-After exceeds this local run's wait budget; retry on next scheduled run") from exc
                time.sleep(max(0, delay))
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == 2:
                    self.requests.append({"url": url, "requested_at": started, "status": None, "error": str(exc)})
                    raise
                time.sleep(2 ** attempt + random.random())
        sha = digest(payload)
        raw_dir = self.root / "raw" / self.source / self.run_id
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / (sha + ".bin")
        path.write_bytes(payload)
        entry = {"url": url, "requested_at": started, "retrieved_at": utcnow(), "status": status,
                 "bytes": len(payload), "sha256": sha, "raw_path": str(path.resolve()), "headers": meta}
        self.requests.append(entry)
        lower_headers = {k.lower(): v for k, v in meta.items()}
        cache_meta.write_text(json.dumps({**entry, "etag": lower_headers.get("etag"), "last_modified": lower_headers.get("last-modified")}), encoding="utf-8")
        return payload

    def get_json(self, url):
        value = json.loads(self.get_bytes(url))
        if isinstance(value, dict) and value.get("error"):
            raise ValueError("Source returned error object: " + str(value["error"])[:300])
        return value


def validate_event(event):
    for key in ("source", "source_record_id", "hazard_types", "source_event", "evidence", "geometry_role", "quality_flags"):
        if key not in event:
            raise ValueError("Missing " + key)
    if not isinstance(event["source_record_id"], str) or not event["source_record_id"]:
        raise ValueError("source_record_id must be a nonempty string")
    if not isinstance(event["hazard_types"], list) or not event["hazard_types"]:
        raise ValueError("Missing hazard classification")
    for key in ("issued_at", "source_updated_at", "effective_at", "expires_at"):
        parse_time(event.get(key))
    geometry = event.get("geometry")
    if geometry:
        if geometry.get("type") not in ("Point", "MultiPoint", "Polygon", "MultiPolygon", "LineString", "MultiLineString", "GeometryCollection"):
            raise ValueError("Unknown GeoJSON geometry type")
        def walk(coords):
            if not isinstance(coords, (list, tuple)) or not coords:
                raise ValueError("Empty geometry coordinates")
            if isinstance(coords[0], (int, float)):
                if len(coords) < 2 or not -180 <= coords[0] <= 180 or not -90 <= coords[1] <= 90:
                    raise ValueError("Coordinates must be WGS84 longitude, latitude")
            else:
                for sub in coords:
                    walk(sub)
        if geometry["type"] == "GeometryCollection":
            if not geometry.get("geometries"):
                raise ValueError("Empty GeometryCollection")
            for sub in geometry.get("geometries", []):
                validate_event({**event, "geometry": sub})
        else:
            walk(geometry.get("coordinates"))
        kind, coords = geometry["type"], geometry.get("coordinates", [])
        polygons = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
        for polygon in polygons:
            for ring in polygon:
                if len(ring) < 4 or ring[0] != ring[-1]:
                    raise ValueError("Polygon rings require at least four positions and explicit closure")
        lines = [coords] if kind == "LineString" else coords if kind == "MultiLineString" else []
        if any(len(line) < 2 for line in lines):
            raise ValueError("LineString requires at least two positions")
    canonical(event)


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS runs(run_id TEXT,source TEXT,started_at TEXT,completed_at TEXT,status TEXT,summary_json TEXT,PRIMARY KEY(run_id,source));
        CREATE TABLE IF NOT EXISTS revisions(event_id TEXT,revision_hash TEXT,first_seen_at TEXT,payload_json TEXT,PRIMARY KEY(event_id,revision_hash));
        CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,source TEXT,source_record_id TEXT,revision_hash TEXT,source_updated_at TEXT,first_seen_at TEXT,last_seen_at TEXT,last_seen_run TEXT,missing_from_latest_snapshot INTEGER DEFAULT 0,payload_json TEXT);
        CREATE TABLE IF NOT EXISTS source_state(source TEXT PRIMARY KEY,last_attempt_at TEXT,last_success_at TEXT,last_complete_run TEXT,status TEXT);
        """)

    def load(self, source, run_id, events, summary):
        now = summary["completed_at"]
        complete = summary["status"] == "success"
        changes = 0
        with self.db:
            if complete:
                self.db.execute("UPDATE events SET missing_from_latest_snapshot=1 WHERE source=?", (source,))
            for e in events:
                event_id = source + ":" + e["source_record_id"]
                content = {k: v for k, v in e.items() if k not in ("retrieved_at", "first_seen_at", "last_seen_at")}
                # This is a normalized snapshot revision (including derived quality).
                # Immutable original source bytes have their own raw SHA-256 in the manifest.
                revision = digest(content)
                payload = canonical(e)
                self.db.execute("INSERT OR IGNORE INTO revisions VALUES(?,?,?,?)", (event_id, revision, now, payload))
                if not complete:
                    continue  # Preserve observations for inspection; only publish complete inventories.
                old = self.db.execute("SELECT source_updated_at,revision_hash FROM events WHERE event_id=?", (event_id,)).fetchone()
                updated = e.get("source_updated_at")
                newer = not old or not old[0] or (updated is not None and parse_time(updated) >= parse_time(old[0]))
                if newer:
                    changes += int(not old or old[1] != revision)
                    self.db.execute("""INSERT INTO events VALUES(?,?,?,?,?,?,?,?,0,?) ON CONFLICT(event_id) DO UPDATE SET
                        revision_hash=excluded.revision_hash,source_updated_at=excluded.source_updated_at,last_seen_at=excluded.last_seen_at,
                        last_seen_run=excluded.last_seen_run,missing_from_latest_snapshot=0,payload_json=excluded.payload_json""",
                        (event_id, source, e["source_record_id"], revision, updated, now, now, run_id, payload))
                else:
                    self.db.execute("UPDATE events SET last_seen_at=?,last_seen_run=?,missing_from_latest_snapshot=0 WHERE event_id=?", (now, run_id, event_id))
            # CAP explicit update/cancel references are a separate local lifecycle annotation;
            # source payloads and revision history remain immutable.
            self.db.execute("CREATE TABLE IF NOT EXISTS supersessions(old_event_id TEXT,new_event_id TEXT,action TEXT,at TEXT,PRIMARY KEY(old_event_id,new_event_id))")
            for e in events if complete else []:
                for ref in e.get("relationships", []):
                    if ref.get("source_record_id") and ref.get("relation") in ("supersedes", "cancels"):
                        self.db.execute("INSERT OR IGNORE INTO supersessions VALUES(?,?,?,?)", (source + ":" + ref["source_record_id"], source + ":" + e["source_record_id"], ref["relation"], e.get("source_updated_at") or now))
            self.db.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?)", (run_id, source, summary["started_at"], now, summary["status"], canonical(summary)))
            self.db.execute("""INSERT INTO source_state VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,
                last_success_at=CASE WHEN excluded.last_success_at IS NOT NULL THEN excluded.last_success_at ELSE source_state.last_success_at END,
                last_complete_run=CASE WHEN excluded.last_complete_run IS NOT NULL THEN excluded.last_complete_run ELSE source_state.last_complete_run END,
                status=excluded.status""", (source, now, now if complete else None, run_id if complete else None, summary["status"]))
        return changes
