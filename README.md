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
cd admin-dashboard/Backend     && python main.py
```

## Notes

- Each project keeps its own `.env`. `gateway.py` loads them one at a time, in
  order, because the portal and the admin dashboard both read `DB_HOST`/`DB_USER`
  at import time but point at different databases.
