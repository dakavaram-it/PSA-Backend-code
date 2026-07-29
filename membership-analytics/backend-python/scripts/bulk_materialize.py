"""Bulk-materialize ALL constituencies' drill-down into the pulse_trend_* tables.

~6 bulk COUNT(DISTINCT) passes scan once and produce every seat at once
(~15 min total) instead of one heavy query per seat (hours). Computed rows are
dumped to a local JSON first, so a failed DB write can be retried with --write-only
without recomputing.

Usage:
    PYTHONPATH=. python3.13 scripts/bulk_materialize.py              # compute + write
    PYTHONPATH=. python3.13 scripts/bulk_materialize.py --write-only # write from dump
"""
import json
import os
import sys
import time

from app.database.db import dakavara_session
from app.repositories.pulse_compute_repository import PulseComputeRepository
from app.services import pulse_cache

DUMP = "/tmp/pulse_bulk_dump.json"
WRITE_ONLY = "--write-only" in sys.argv


def compute():
    t0 = time.time()
    with dakavara_session() as db:
        repo = PulseComputeRepository(db)
        print("[bulk] segment passes…", flush=True)
        seg = list(repo.bulk_segments())
        print(f"[bulk] {len(seg):,} segment rows in {(time.time()-t0)/60:.1f} min", flush=True)
        print("[bulk] houses pass…", flush=True)
        houses = repo.bulk_houses()
        print(f"[bulk] {len(houses):,} seats with household data", flush=True)
    with open(DUMP, "w", encoding="utf-8") as f:
        json.dump({"seg": seg, "houses": houses}, f)
    print(f"[bulk] dumped to {DUMP}", flush=True)
    return seg, houses


def main():
    t0 = time.time()
    if WRITE_ONLY:
        if not os.path.exists(DUMP):
            print("[bulk] no dump to write; run without --write-only first"); return
        with open(DUMP, encoding="utf-8") as f:
            d = json.load(f)
        seg, houses = d["seg"], d["houses"]
        print(f"[bulk] loaded {len(seg):,} seg rows from dump", flush=True)
    else:
        seg, houses = compute()
    n_seg = pulse_cache.write_segments(seg)
    n_h = pulse_cache.write_all_houses(houses)
    _reload_api_cache()
    print(f"[bulk] DONE in {(time.time()-t0)/60:.1f} min — wrote {n_seg:,} segment rows, {n_h:,} house rows", flush=True)


def _reload_api_cache():
    """Best-effort: clear the FastAPI in-process constituency cache so it re-reads."""
    import os
    import urllib.request
    base = os.getenv("PULSE_API_BASE", "http://localhost:8001")
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/v1/pulse/constituencies/reload", method="POST"), timeout=10)
        print("[bulk] cleared FastAPI constituency cache", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[bulk] cache reload skipped ({str(e)[:50]})", flush=True)


if __name__ == "__main__":
    main()
