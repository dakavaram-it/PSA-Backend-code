"""Materialize every constituency's drill-down into the pulse_trend_* tables.

Runs seat-by-seat (each a short, isolated query+write on its own DB connection),
so it's safe to run for hours and is resumable — already-materialized seats are
skipped unless --force. Independent of the API server (uvicorn reloads won't kill it).

Usage:
    PYTHONPATH=. python3.13 scripts/warm_constituencies.py [--force]
"""
import sys
import time

from app.database.db import dakavara_session
from app.repositories.pulse_compute_repository import PulseComputeRepository
from app.services import pulse_cache

FORCE = "--force" in sys.argv


def main():
    t0 = time.time()
    with dakavara_session() as db:
        names = PulseComputeRepository(db).list_constituencies()
    total = len(names)
    print(f"[warm] {total} constituencies; force={FORCE}", flush=True)
    done = skipped = errors = 0
    for i, name in enumerate(names, 1):
        try:
            if not FORCE and pulse_cache.read_constituency(name) is not None:
                skipped += 1
                print(f"[warm] {i}/{total} {name}: skip (already materialized)", flush=True)
                continue
            ts = time.time()
            with dakavara_session() as db:
                detail = PulseComputeRepository(db).constituency_detail(name)
            pulse_cache.write_constituency(name, detail["dims"], detail["houses"])
            done += 1
            print(f"[warm] {i}/{total} {name}: done in {time.time()-ts:.0f}s "
                  f"(houses={detail['houses']['total']})", flush=True)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"[warm] {i}/{total} {name}: ERROR {str(exc)[:140]}", flush=True)
    print(f"[warm] FINISHED in {(time.time()-t0)/60:.1f} min — "
          f"built={done}, skipped={skipped}, errors={errors}", flush=True)


if __name__ == "__main__":
    main()
