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
| `REPORT_RATINGS_DB_HOST` / `_USER` / `_PASSWORD` | no | cadre performance scores (getCadreScores). All three unset ⇒ `RATINGS_DB is None` and getCadreScores answers `{"configured": false}`. |
| `REPORT_RATINGS_DB_PORT` / `_NAME` | no | default `3306` / `report_ratings`. |
| `JWT_SECRET` / `ALGORITHM` | yes | signing key and algorithm (`HS512`) for the session JWT `login` issues. Missing either raises at import; changing the secret invalidates every token already issued. |

## Endpoints

Every one requires a live session except `login` (and `/docs`, `/redoc`,
`/openapi.json`) — `PUBLIC_PATHS` in `main.py` is the whole allowlist.

| Endpoint | Purpose |
|---|---|
| `GET /getProposalElectionTypes` | active election types (`is_active = 'Y'`) |
| `GET /getAssemblyConstituenciesInAState` | every assembly in state 1 |
| `GET /getMandalsInAConstituency` | mandals (tehsils) of an assembly |
| `GET /getTownsInAConstituency` | towns (local election bodies) of an assembly |
| `GET /getProposalConstituenciesByTehsilId` | local bodies under a mandal, for one election type |
| `GET /getProposalConstituenciesByTownId` | same, under a town |
| `GET /getProposalPositionsOverviewByProposalConstituencyId` | roles + `max_positions`, `max_proposals`, `proposed_cnt` |
| `GET /getProposalPositionsByProposalConstituencyId` | roles only — **unused**, getProposalPositionsOverviewByProposalConstituencyId supersedes it |
| `GET /getProposalConstituencyReservation` | the constituency's reservation type |
| `GET /checkProposalPositionAvailability` | Available / Not Available — **unused by the frontend**, but assignProposalCandidate calls it internally |
| `POST /assignProposalCandidate` | propose a cadre for a position, with an optional `proposal_status_id` (1 Proposed — the default — or 2 Shortlisted). Re-checks reservation, slot count, duplicate; `409` with the reason in `detail`, `400` on an unknown status |
| `GET /cadreSearch` | cadre by `MembershipId` / `MobileNo` / `Name`, each row **flagged** `eligible` `'Y'`/`'N'` |
| `GET /getProposalCandidatesByProposalPositionId` | who is currently proposed for a position, each row carrying its `proposal_status_id` / `proposal_status` |
| `POST /removeProposalCandidate` | drop a candidate from a position — `is_active` goes to `'N'`, freeing the slot; `404` if the id is unknown or already removed |
| `POST /login` | credentials → session cookie. Throttled per username |
| `GET /me` | the logged-in user, or `401` |
| `POST /logout` | drop the session |
| `GET /getCadreScores` | `?mids=` comma-separated: total score, the report breakdown behind it, and the leader-feedback answers |
| `GET /getProposalPositionsWithCandidates` | every position holding at least one active candidate, **across all constituencies** — no query parameters, on purpose; the caller filters in the browser. Carries the local body *and* its assembly, plus per-status counts |
| `POST /updateProposalCandidateStatus` | move a live candidate between Proposed / Shortlisted / Confirmed. No slot or eligibility re-check — all three are counted rows; `404` if the id is unknown or removed |
| `GET /getUserAccessAssemblies` | the caller's own assembly grants, scoped by session — the wizard's Assembly picklist |
| `GET /getDashboardPositionsByConstituencyId` | every position under one assembly (`?constituency_id=`), across every election type and every local body — the Dashboard screen's whole picture in one call. Unlike getProposalPositionsWithCandidates this is a `LEFT JOIN`, so a position with no candidate still appears. Each row also carries `tehsil_id`/`town_id` (getProposalConstituenciesByTehsilId/getProposalConstituenciesByTownId's own inputs), so a caller can jump straight to that location without re-deriving it through getMandalsInAConstituency/getTownsInAConstituency |

The numbers are this API's own scheme — they do not line up with the frontend wizard's
visible "steps".

`assignProposalCandidate` and `updateProposalCandidateStatus` stamp `inserted_user_id` / `updated_user_id` from the **session**, never
from the request body — the body has no user id field and must not grow one.

## Tests

No framework, no fixtures, no database. Plain asserts, run each directly:

```bash
python test_auth.py          # login edge cases, session sweep, middleware order
python test_eligibility.py   # eligibility_flag SQL — reservation only, never location
python test_score.py         # getCadreScores arithmetic and membership-id key matching
python test_access.py        # user_access_assemblies union, getProposalPositionsWithCandidates's access scoping
python test_dashboard.py     # getDashboardPositionsByConstituencyId SQL shape — scoping, LEFT JOIN, returned columns
python test_pool.py          # connection reuse, stale-socket retry, write safety
```

`../../test_gateway.py` covers the mount-prefix behaviour these paths depend on.

## Notes

- **The database is live production.** No seed, no fixture, no local copy. `assignProposalCandidate` writes a
  real `proposal_candidate` row.
- Sessions are stateless JWTs: nothing is stored server-side, so a restart no longer logs
  everyone out — and logging out cannot revoke a token before its 8h `exp`.
- Read `CLAUDE.md` next to this file before changing anything: it records the things that
  bite, mostly places where the obvious change is wrong.
