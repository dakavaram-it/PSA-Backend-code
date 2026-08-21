# portal-frontend-code-2 — Dashboard 2 API

Read-only reporting API behind **Dashboard 2** (`Frontend/src/leap/components/Dashboard2.jsx`
in the portal frontend repo). Same live `dakavara_pa` database as
[`../portal-frontend-code`](../portal-frontend-code), which serves Dashboard 1 and the
nomination wizard — a different **screen**, not a different dataset.

```bash
cd Backend
pip install -r ../requirements.txt
python main.py                 # http://127.0.0.1:4002/docs
python test_dashboard2.py      # 11 invariant checks against the live DB
```

Mounted by the repo gateway at `/portal-frontend-code-2` (`python ../gateway.py`, merged
Swagger on http://127.0.0.1:6644/docs).

## No login

There is no `/login`, no `/me`, no token and no middleware. Every endpoint takes its own
location scope:

| Parameter | Meaning |
|---|---|
| `userLocationLevelId` | `5` Assembly · `4` Parliament · omitted/null State |
| `userLocationLevelValuesStr` | the ids for that level — an array (repeat the parameter) **or** one comma-separated string |

```
GET /api/dashboard2/positionSummary
      ?userLocationLevelId=5
      &userLocationLevelValuesStr=111,127,133,134,135,136,137,140,141,354,355,356,357,358,368
```

**That pair is a filter, not a permission.** A caller may widen it at will. Do not put
anything behind this service that is not safe to serve to whoever can reach the port, and
do not add a write endpoint without adding authentication first.

Every response echoes `scope`, so "no rows because nothing is configured" and "no rows
because those ids named nothing" are distinguishable.

## Endpoints

| | |
|---|---|
| `GET /api/dashboard2/pipeline` | the six-step header, one bar per step |
| `GET /api/dashboard2/positionSummary` | the main table — body × post × counters |
| `GET /api/dashboard2/geoBreakdown` | one post, split by parliament and again by assembly |
| `GET /api/dashboard2/reservationSummary` | one post, split by reservation |
| `GET /api/dashboard2/locations` | one post's locations, paged, filterable by stage and reservation |
| `GET /api/dashboard2/locationCandidates` | the names on one or more locations |
| `GET /api/dashboard2/{mainElectionTypes,electionTypes,roles,statuses,reservations}` | the reference tables |
| `GET /api/dashboard2/{parliaments,assemblies}` | the level-4 and level-5 picklists |

A **post** is identified by the triple `(mainElectionTypeId, proposalElectionTypeId,
proposalRoleId)`, never by role alone — role 5 (Corporator) serves both Municipal Ward and
Corporation Ward seats.

## Three stages are missing, and they are served as zeros

Dashboard 2 draws seven stages. `dakavara_pa` carries four:

| Stage | Source |
|---|---|
| 0 Not started | no active `proposal_candidate` |
| 1 Proposal received | ≥ 1 active candidate |
| 2 Confirmed | ≥ 1 candidate at `proposal_status_id = 2` |
| 3 Nomination filed | ≥ 1 candidate with `is_nominated = 'Y'` |
| 4 Door to Door | **no table** |
| 5 Door to Door - 2 | **no table** |
| 6 Result declared | **no table** |

`door_to_door`, `door_to_door_2`, `declared`, `won`, `lost` and the three house counts are
returned as `0` on every counter block, and named in each response's `stagesUnavailable`.
They are **not** derived from data. See `Backend/routers/dashboard.py`'s
`EMPTY_STAGE_FIELDS`.

## Layout

```
Backend/
  main.py            the FastAPI app
  config.py          env + DB settings      (copied from ../portal-dashboard)
  db.py              connection pooling     (copied from ../portal-dashboard)
  scope.py           userLocationLevelId / userLocationLevelValuesStr -> assembly ids
  queries.py         every SELECT, with the schema reasoning behind each
  routers/
    dashboard.py     the six screen endpoints
    lookups.py       the picklists
  test_dashboard2.py the invariant suite
```

Read `Backend/queries.py` before changing any SQL — it is where the assembly-attribution
rule and the stage ladder are written down.
