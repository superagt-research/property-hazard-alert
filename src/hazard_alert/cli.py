"""One-shot ETL runner. Scheduler wiring is intentionally out of scope."""
import argparse
from collections import Counter
import importlib
import json
from pathlib import Path
import sys
import uuid

from .core import PublicClient, Store, utcnow, validate_event


def run_source(source, root, zone_limit=1000):
    started = utcnow()
    run_id = started.replace(":", "").replace("-", "") + "-" + uuid.uuid4().hex[:8]
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    client = PublicClient(root, source, run_id, zone_limit)
    store = Store(root / "hazards.sqlite")
    previous = store.db.execute("SELECT last_success_at FROM source_state WHERE source=?", (source,)).fetchone()
    client.last_success_at = previous[0] if previous else None
    events, rejects, metrics = [], [], {}
    error = None
    try:
        module = importlib.import_module("hazard_alert.adapters." + {"nifc": "wildfire"}.get(source, source))
        result = module.collect(client, started)
        metrics = result.get("metrics", {})
        for e in result["events"]:
            try:
                validate_event(e)
                if e["source"] != source:
                    raise ValueError("Adapter source mismatch")
                events.append(e)
            except Exception as exc:
                rejects.append({"error": str(exc), "record": e})
        status = "success" if result.get("complete") and not rejects else "partial"
    except Exception as exc:
        status, error = "failed", str(exc)
    summary = {"run_id": run_id, "source": source, "started_at": started, "completed_at": utcnow(),
               "status": status, "records_loaded": len(events), "records_quarantined": len(rejects),
               "geometry_present": sum(e.get("geometry") is not None for e in events),
               "geometry_roles": dict(Counter(e.get("geometry_role") for e in events)),
               "hazard_types": dict(Counter(h for e in events for h in e["hazard_types"])),
               "quality_flags": dict(Counter(f for e in events for f in e["quality_flags"])),
               "metrics": metrics, "error": error, "http_requests": client.requests}
    summary["changed_records"] = store.load(source, run_id, events, summary)
    store.db.close()
    out = root / "normalized" / source / run_id
    out.mkdir(parents=True, exist_ok=True)
    with (out / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False, allow_nan=False) + "\n")
    (out / "quarantine.json").write_text(json.dumps(rejects, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report = root / "reports"
    report.mkdir(exist_ok=True)
    (report / (source + "_latest.json")).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", nargs="+", choices=["nws", "usgs", "nifc", "nhc", "nwps"], default=["nws", "usgs", "nifc", "nhc", "nwps"])
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--zone-limit", type=int, default=1000, help="NWS geometry feasibility budget; unresolved areas remain explicitly unknown")
    args = parser.parse_args()
    results = []
    for source in args.sources:
        summary = run_source(source, args.data_dir, args.zone_limit)
        results.append(summary)
        print(json.dumps({k: v for k, v in summary.items() if k not in ("http_requests", "metrics")}, ensure_ascii=False), flush=True)
    return 0 if all(r["status"] == "success" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
