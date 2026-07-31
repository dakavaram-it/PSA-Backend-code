# PSA Backend — all project backends

Backend code for two projects, one Swagger UI.

| Sub-directory | Source project | Stack | Mount prefix |
|---|---|---|---|
| `portal-frontend-code/Backend/` | `../portal-frontend-code` | FastAPI | `/portal-frontend-code` |
| `admin-dashboard/Backend/` | `../admin-dashboard` | FastAPI | `/admin-dashboard` |

Each sub-directory mirrors its source project's layout (`Backend/` next to `.env`),
so every backend still runs standalone exactly as it did before.

## Run everything

```bash
pip install -r requirements.txt
python gateway.py            # http://127.0.0.1:8000/docs
python gateway.py --dump     # write the merged spec to openapi.json
python test_gateway.py       # self-check
```

`/docs` is a single Swagger UI over both backends. Every operation is tagged
`<Project> - <tag>` and its summary starts with `[<Project>]`, so the project name
comes before the endpoint. Paths carry the project prefix, e.g.:

```
POST /portal-frontend-code/S14login
GET  /admin-dashboard/api/members
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
```

Each project keeps its own `requirements.txt` for a standalone run — the portal's
in `Backend/`, the admin dashboard's at its sub-directory root (mirroring the
source repositories).

## Notes

- Each project keeps its own `.env`. `gateway.py` loads them one at a time, in
  order, with `override=True`, because the portal and the admin dashboard both
  read `DB_HOST`/`DB_USER` at import time but point at different databases — and
  `admin-dashboard/Backend/config.py` calls `load_dotenv()` itself, which will
  *not* replace a variable the portal already put in the environment.
- `admin-dashboard/Backend/` is a flat, package-less tree (`from config import
  ...`, `from routers import ...`). A standalone run gets that from its working
  directory; `gateway.py` puts the directory on `sys.path` in `_load_admin()`.
- The frontends are deployed from their own repositories and reach these backends
  over HTTP — `admin-dashboard` (React + Vite) points `/api` here via
  `VITE_API_BASE` or `VITE_API_PROXY`. Changing a path or a response shape breaks
  a bundle built elsewhere; `admin-dashboard/CLAUDE.md` records those contracts.
