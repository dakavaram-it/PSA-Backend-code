# PSA Backend — all project backends

Backend code for three project backends, one Swagger UI.

| Sub-directory | Serves | Stack | Mount prefix |
|---|---|---|---|
| `portal-frontend-code/Backend/` | `../portal-frontend-code` | FastAPI | `/portal-frontend-code` |
| `admin-dashboard/Backend/` | `../admin-dashboard`, sidebar **Dashboard** | FastAPI | `/admin-dashboard` |
| `portal-dashboard/Backend/` | `../admin-dashboard`, sidebar **Portal Dashboard** | FastAPI | `/portal-dashboard` |

Each sub-directory mirrors its source project's layout (`Backend/` next to `.env`),
so every backend still runs standalone exactly as it did before.

The last two both serve the **same** frontend — one sidebar item each — but they
are separate FastAPI apps because they read entirely different tables of the same
`dakavara_pa` database: `admin-dashboard` works on `activity_member`/`user_type`/
`tdp_cadre`, `portal-dashboard` on `user`/`team_type`/`entitlement`/
`group_entitlement`/`user_groups`. The frontend keeps them apart with one proxy
prefix each, `/adminapi` and `/portalapi`.

## Run everything

```bash
pip install -r requirements.txt
python gateway.py            # http://127.0.0.1:6644/docs
python gateway.py --dump     # write the merged spec to openapi.json
python test_gateway.py       # self-check
```

`/docs` is a single Swagger UI over every backend. Every operation is tagged
`<Project> - <tag>` and its summary starts with `[<Project>]`, so the project name
comes before the endpoint. Paths carry the project prefix, e.g.:

```
POST /portal-frontend-code/S14login
GET  /admin-dashboard/api/members
GET  /portal-dashboard/api/portal/users/stats
```

`gateway.py` mounts each backend rather than merging its routes into one app, so
each keeps its own middleware — notably the portal's session guard, which would
be lost by a route merge. Because Starlette leaves the mount prefix on
`request.url.path`, a small `StripPrefix` wrapper removes it before the sub-app
sees the request; without it the portal's `PUBLIC_PATHS` allowlist stops matching
and login becomes unreachable. `test_gateway.py` locks that behaviour down.

## Run one project on its own

```bash
cd portal-frontend-code/Backend && uvicorn main:app --port 8001
cd admin-dashboard/Backend      && python main.py        # port 4000
cd portal-dashboard/Backend     && python main.py        # port 4001
```

Each project keeps its own `requirements.txt` for a standalone run — the portal's
in `Backend/`, the two dashboards' at their sub-directory roots (mirroring the
source repositories).

## Notes

- Each project keeps its own `.env`. `gateway.py` loads them one at a time, in
  order, with `override=True`, because every backend reads `DB_HOST`/`DB_USER` at
  import time but they point at different databases — and each
  `Backend/config.py` calls `load_dotenv()` itself, which will *not* replace a
  variable an earlier project already put in the environment.
- `admin-dashboard/Backend/` and `portal-dashboard/Backend/` are flat,
  package-less trees (`from config import ...`, `from routers import ...`). A
  standalone run gets that from its working directory; in the gateway both go
  through `_load_flat_backend()`. **That helper clears the shared top-level
  module names (`config`, `db`, `queries`, `schemas`, `services`, `routers`) from
  `sys.modules` before each load.** Without it the second flat backend imported
  would resolve those names to the first one's modules and silently serve its
  queries and credentials — the two trees are laid out identically, so nothing
  would fail loudly.
- The frontends are deployed from their own repositories and reach these backends
  over HTTP. `admin-dashboard` (React + Vite) points **two** prefixes here:
  `/adminapi` at `/admin-dashboard` and `/portalapi` at `/portal-dashboard`, via
  `VITE_API_BASE`/`VITE_API_PROXY` and `VITE_PORTAL_API_BASE`/
  `VITE_PORTAL_API_PROXY`. Changing a path or a response shape breaks a bundle
  built elsewhere; `admin-dashboard/CLAUDE.md` and
  `portal-frontend-code/Backend/CLAUDE.md` record those contracts. The portal's own
  endpoint list, env vars and self-checks are in `portal-frontend-code/Backend/README.md`.
