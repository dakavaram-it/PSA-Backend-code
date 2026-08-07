# Local Body Elections API — portal backend

FastAPI + PyMySQL. One file, `main.py`: a session-guarded HTTP surface over the party's
live membership database, serving the nomination wizard in the `portal-frontend-code`
frontend repository (React + Vite, deployed separately).

The frontend calls these paths under its own `/leapapi` prefix, which its Vite proxy
rewrites to this app's gateway mount (`/portal-frontend-code`). Change a path or a
response shape here and a bundle built in the other repo breaks.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --port 8001        # standalone; /docs is this app alone
```

Normally it runs under the repo-root gateway instead — `python gateway.py`, which mounts
this app at `/portal-frontend-code` and merges its Swagger into `http://127.0.0.1:6644/docs`.
See `../../README.md`.

## Configuration

`.env` lives at the **project root** (`../.env`, next to this `Backend/`), not in here.
Copy `../.env.example`.

| Variable | Required | Note |
|---|---|---|
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | yes | the membership database. Missing any one raises at import. |
| `REPORT_RATINGS_DB_HOST` / `_USER` / `_PASSWORD` | no | cadre performance scores (S17). All three unset ⇒ `RATINGS_DB is None` and S17 answers `{"configured": false}`. |
| `REPORT_RATINGS_DB_PORT` / `_NAME` | no | default `3306` / `report_ratings`. |
| `COOKIE_SECURE` | no | `true` wherever the app is served over HTTPS. Default `false` for local dev and the plain-HTTP PM2 deployment. |

## Endpoints

Every one requires a live session except `S14login` (and `/docs`, `/redoc`,
`/openapi.json`) — `PUBLIC_PATHS` in `main.py` is the whole allowlist.

| Endpoint | Purpose |
|---|---|
| `GET /S1getProposalElectionTypes` | active election types (`is_active = 'Y'`) |
| `GET /S2getAssemblyConstituenciesInAState` | every assembly in state 1 |
| `GET /S3getMandalsInAConstituency` | mandals (tehsils) of an assembly |
| `GET /S4getTownsInAConstituency` | towns (local election bodies) of an assembly |
| `GET /S5getProposalConstituenciesByTehsilId` | local bodies under a mandal, for one election type |
| `GET /S6getProposalConstituenciesByTownId` | same, under a town |
| `GET /S7getProposalPositionsOverviewByProposalConstituencyId` | roles + `max_positions`, `max_proposals`, `proposed_cnt` |
| `GET /S8getProposalPositionsByProposalConstituencyId` | roles only — **unused**, S7 supersedes it |
| `GET /S9getProposalConstituencyReservation` | the constituency's reservation type |
| `GET /S10checkProposalPositionAvailability` | Available / Not Available — **unused by the frontend**, but S11 calls it internally |
| `POST /S11assignProposalCandidate` | propose a cadre for a position, with an optional `proposal_status_id` (1 Proposed — the default — or 2 Shortlisted). Re-checks reservation, slot count, duplicate; `409` with the reason in `detail`, `400` on an unknown status |
| `GET /S12cadreSearch` | cadre by `MembershipId` / `MobileNo` / `Name`, each row **flagged** `eligible` `'Y'`/`'N'` |
| `GET /S13getProposalCandidatesByProposalPositionId` | who is currently proposed for a position, each row carrying its `proposal_status_id` / `proposal_status` |
| `POST /S18removeProposalCandidate` | drop a candidate from a position — `is_active` goes to `'N'`, freeing the slot; `404` if the id is unknown or already removed |
| `POST /S14login` | credentials → session cookie. Throttled per username |
| `GET /S15me` | the logged-in user, or `401` |
| `POST /S16logout` | drop the session |
| `GET /S17getCadreScores` | `?mids=` comma-separated: total score, the report breakdown behind it, and the leader-feedback answers |
| `GET /S19getProposalPositionsWithCandidates` | every position holding at least one active candidate, **across all constituencies** — no query parameters, on purpose; the caller filters in the browser. Carries the local body *and* its assembly, plus per-status counts |
| `POST /S20updateProposalCandidateStatus` | move a live candidate between Proposed / Shortlisted / Confirmed. No slot or eligibility re-check — all three are counted rows; `404` if the id is unknown or removed |
| `GET /S21getUserAccessAssemblies` | the caller's own assembly grants, scoped by session — the wizard's Assembly picklist |
| `GET /S22getDashboardPositionsByConstituencyId` | every position under one assembly (`?constituency_id=`), across every election type and every local body — the Dashboard screen's whole picture in one call. Unlike S19 this is a `LEFT JOIN`, so a position with no candidate still appears. Each row also carries `tehsil_id`/`town_id` (S5/S6's own inputs), so a caller can jump straight to that location without re-deriving it through S3/S4 |

The numbers are this API's own scheme — they do not line up with the frontend wizard's
visible "steps".

`S11` and `S20` stamp `inserted_user_id` / `updated_user_id` from the **session**, never
from the request body — the body has no user id field and must not grow one.

## Tests

No framework, no fixtures, no database. Plain asserts, run each directly:

```bash
python test_auth.py          # login edge cases, session sweep, middleware order
python test_eligibility.py   # eligibility_flag SQL — reservation only, never location
python test_score.py         # S17 arithmetic and membership-id key matching
python test_access.py        # user_access_assemblies union, S19's access scoping
python test_dashboard.py     # S22 SQL shape — scoping, LEFT JOIN, returned columns
```

`../../test_gateway.py` covers the mount-prefix behaviour these paths depend on.

## Notes

- **The database is live production.** No seed, no fixture, no local copy. `S11` writes a
  real `proposal_candidate` row.
- Sessions live in `main.SESSIONS`, a process dict — a restart (including every
  `--reload` edit) logs everyone out.
- Read `CLAUDE.md` next to this file before changing anything: it records the things that
  bite, mostly places where the obvious change is wrong.
