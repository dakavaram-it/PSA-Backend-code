"""Dynamic Panel & Volatility engine — in-memory.

Loads pulse_trend_voter_matrix (one row per phone: bloc per survey + attributes)
into numpy arrays once, then computes metrics for ANY survey subset in <1s:
panel size (phones in all selected), loyalty/volatility by party, households, and
caste/age/gender/latest-vote breakdowns of the volatile group, plus a within-selection
repeat distribution.
"""
import threading
import time

import numpy as np
from sqlalchemy import text

from db_config.db import dakavara_session

MATRIX_TABLE = "dakavara_pa.pulse_trend_voter_matrix"
SIDS = [19, 20, 21, 24, 25, 26, 28, 31]
SID_META = {
    19: {"label": "S19 · Apr–May ’24", "wave": "Apr–May ’24"},
    20: {"label": "S20 · Apr–May ’24", "wave": "Apr–May ’24"},
    21: {"label": "S21 · 09 May ’24", "wave": "Apr–May ’24"},
    24: {"label": "S24 · 11 May ’24", "wave": "Apr–May ’24"},
    25: {"label": "S25 · 12 May ’24", "wave": "Apr–May ’24"},
    26: {"label": "S26 · 16 May ’24", "wave": "Apr–May ’24"},
    28: {"label": "S28 · Dec ’25", "wave": "Dec ’25"},
    31: {"label": "S31 · Jun ’26", "wave": "Jun ’26"},
}
PARTY = {1: "TDP/NDA", 2: "YSRCP", 3: "Others"}

_LOCK = threading.Lock()
_STATE = {"ready": False, "loading": False, "rows": 0, "loadedAt": None, "loadSec": None, "error": None}
_M = {}  # numpy arrays: blocs, caste/cat/age/gen codes+labels, hh codes


def status():
    return dict(_STATE, surveys=[{"sid": s, **SID_META[s]} for s in SIDS])


class _Factorizer:
    """Streaming string -> int32 code encoder. Keeps only int codes + a small label
    list (never the full string column), so memory stays low for 4.46M rows.
    code 0 == '' (missing)."""

    def __init__(self):
        self.labels = [""]
        self.idx = {"": 0}
        self.parts = []

    def add(self, values):
        idx, labels = self.idx, self.labels
        out = []
        for v in values:
            v = v or ""
            c = idx.get(v)
            if c is None:
                c = len(labels); idx[v] = c; labels.append(v)
            out.append(c)
        self.parts.append(np.array(out, dtype=np.int32))

    def finalize(self):
        codes = np.concatenate(self.parts) if self.parts else np.zeros(0, dtype=np.int32)
        return codes, self.labels


def load():
    """(Re)load the matrix into memory. Safe to call in a background thread."""
    with _LOCK:
        if _STATE["loading"]:
            return
        _STATE["loading"] = True
        _STATE["error"] = None
    t0 = time.time()
    try:
        k = len(SIDS)
        cols = ", ".join(f"COALESCE(s{n},0)" for n in SIDS)  # NULL bloc -> 0 (absent)
        # stream in chunks and factorize on the fly → only compact int arrays are kept
        # in memory (never a multi-GB list of 4.46M string rows).
        bloc_parts = []
        facs = {key: _Factorizer() for key in ("caste", "cat", "age", "gen", "hh")}
        n = 0
        with dakavara_session() as db:
            res = db.execute(text(
                f"SELECT {cols}, caste_name, caste_category, age_range, gender, hh "
                f"FROM {MATRIX_TABLE}").execution_options(stream_results=True, max_row_buffer=100000))
            while True:
                chunk = res.fetchmany(100000)
                if not chunk:
                    break
                n += len(chunk)
                bloc_parts.append(np.array([r[:k] for r in chunk], dtype=np.int8))
                facs["caste"].add(r[k] for r in chunk)
                facs["cat"].add(r[k + 1] for r in chunk)
                facs["age"].add(r[k + 2] for r in chunk)
                facs["gen"].add(r[k + 3] for r in chunk)
                facs["hh"].add(r[k + 4] for r in chunk)
        blocs = np.vstack(bloc_parts) if bloc_parts else np.zeros((0, k), np.int8)
        _M.update(blocs=blocs,
                  caste=facs["caste"].finalize(), cat=facs["cat"].finalize(),
                  age=facs["age"].finalize(), gen=facs["gen"].finalize(),
                  hh=facs["hh"].finalize()[0])
        _STATE.update(ready=True, rows=n, loadedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
                      loadSec=round(time.time() - t0, 1))
    except Exception as exc:  # noqa: BLE001
        _STATE["error"] = str(exc)[:160]
        _STATE["ready"] = False
    finally:
        _STATE["loading"] = False


def load_async():
    threading.Thread(target=load, daemon=True).start()


def _breakdown(codes, labels, idx, denom, top=10):
    cnt = np.bincount(codes[idx], minlength=len(labels))
    order = np.argsort(cnt)[::-1]
    out = []
    for o in order:
        if cnt[o] <= 0:
            continue
        out.append({"seg": labels[o] or "Unknown", "n": int(cnt[o]),
                    "pct": round(100 * cnt[o] / denom, 1) if denom else 0})
        if len(out) >= top:
            break
    return out


def compute(sids):
    """Metrics for the chosen survey set (panel = phones present in ALL of them)."""
    if not _STATE["ready"]:
        return {"ready": False}
    sel = [s for s in sids if s in SIDS]
    if len(sel) < 2:
        return {"ready": True, "error": "Pick at least 2 surveys."}
    blocs = _M["blocs"]
    cidx = [SIDS.index(s) for s in sel]
    sub = blocs[:, cidx]                       # (N, k)
    nsel = (sub != 0).sum(axis=1)
    total_any = int((nsel >= 1).sum())
    present = nsel == len(cidx)                # in ALL selected
    panel = int(present.sum())
    if panel == 0:
        return {"ready": True, "surveys": sel, "panel": 0, "totalInAny": total_any}

    present_idx = np.nonzero(present)[0]
    pb = sub[present]                          # (panel, k)
    first = pb[:, 0]
    loyal_mask = (pb == first[:, None]).all(axis=1)
    loyal = int(loyal_mask.sum())
    volatile = panel - loyal
    loyal_party = first[loyal_mask]
    loyal_by = [{"party": PARTY[p], "n": int((loyal_party == p).sum()),
                 "pct": round(100 * int((loyal_party == p).sum()) / panel, 1)} for p in (1, 2, 3)]
    vol_idx = present_idx[~loyal_mask]

    # latest vote of the volatile group = bloc in the most-recent selected survey
    last_col = cidx[int(np.argmax([SIDS[c] for c in cidx]))]
    lv = blocs[vol_idx, last_col]
    latest_vote = [{"party": PARTY[p], "n": int((lv == p).sum())} for p in (1, 2, 3)]

    hh_codes = _M["hh"]
    panel_hh = hh_codes[present_idx]
    matched = int((panel_hh > 0).sum())
    households = int(np.unique(panel_hh[panel_hh > 0]).size)

    return {
        "ready": True,
        "surveys": [{"sid": s, **SID_META[s]} for s in sel],
        "totalInAny": total_any,
        "panel": panel,
        "loyal": loyal, "loyalPct": round(100 * loyal / panel, 1),
        "volatile": volatile, "volatilePct": round(100 * volatile / panel, 1),
        "loyalByParty": loyal_by,
        "households": households, "panelMatchedToHouse": matched,
        "volatile_breakdown": {
            "caste": _breakdown(*_M["caste"], vol_idx, volatile),
            "casteCategory": _breakdown(*_M["cat"], vol_idx, volatile),
            "age": _breakdown(*_M["age"], vol_idx, volatile),
            "gender": _breakdown(*_M["gen"], vol_idx, volatile),
        },
        "latestVote": latest_vote,
        "repeatDistribution": [{"n": int(c), "phones": int((nsel == c).sum())} for c in range(1, len(cidx) + 1)],
    }
