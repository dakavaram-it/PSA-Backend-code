"""Materialize per-constituency household stayed/moved into pulse_trend_constituency_houses.

Uses the electoral roll (m_main_voter_details) joined to IVRS answers by MOBILE_NO
(household = part_no + house_no). One bulk pass for all seats.

Usage:
    PYTHONPATH=. python3.13 scripts/materialize_houses.py
"""
import time

from app.database.db import dakavara_session
from app.repositories.pulse_compute_repository import PulseComputeRepository
from app.services import pulse_cache


def main():
    t0 = time.time()
    with dakavara_session() as db:
        print("[houses] bulk pass via m_main_voter_details (mobile join)…", flush=True)
        houses = PulseComputeRepository(db).bulk_houses()
    print(f"[houses] {len(houses)} seats with data in {(time.time()-t0)/60:.1f} min", flush=True)
    n = pulse_cache.write_all_houses(houses)
    try:
        import os
        import urllib.request
        base = os.getenv("PULSE_API_BASE", "http://localhost:8001")
        urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/v1/pulse/constituencies/reload", method="POST"), timeout=10)
    except Exception:  # noqa: BLE001
        pass
    tot_s = sum(h["stayed"] for h in houses.values())
    tot_m = sum(h["moved"] for h in houses.values())
    print(f"[houses] DONE — wrote {n} seats, statewide stayed={tot_s:,} moved={tot_m:,} "
          f"in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
