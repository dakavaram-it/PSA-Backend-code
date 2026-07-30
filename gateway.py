"""Single entry point for every project backend in this repository.

Each project's backend is imported unchanged from its own sub-directory and
mounted under a prefix named after the project:

    /portal-frontend-code/...   portal-frontend-code/Backend         (FastAPI)
    /admin-dashboard/...        admin-dashboard/Backend              (FastAPI)

Mounting (rather than merging routers into one app) is deliberate: each backend
keeps its own middleware stack. The portal's session guard and the per-project
CORS rules only exist as app-level middleware, and copying routes into a shared
app would silently drop them.

The trade-off of mounting is that every sub-app also gets its own /docs, so this
module builds one merged OpenAPI document instead - every path prefixed with its
mount, every operation tagged with its project name - served at /openapi.json
with a single Swagger UI at /docs.

    python gateway.py            run on http://127.0.0.1:4000  (docs at /docs)
    python gateway.py --dump     write the merged spec to openapi.json and exit

Each backend still runs standalone from its own sub-directory exactly as before.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders

ROOT = Path(__file__).resolve().parent


class StripPrefix:
    """Serve `app` with `prefix` removed from the request path.

    Starlette's Mount leaves scope["path"] fully qualified and strips the prefix
    only during route matching, so a mounted app that inspects request.url.path
    itself sees "/portal-frontend-code/S14login" instead of "/S14login". The
    portal's auth middleware does exactly that (its PUBLIC_PATHS allowlist is how
    login stays reachable), so the prefix is removed here and each backend sees
    the same paths it sees when run on its own.
    """

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix.rstrip("/")

    def _restore_prefix(self, location: str) -> str:
        """Put the prefix back on an outgoing Location header.

        Starlette builds redirect targets from the same scope["path"] this class
        rewrites, so its trailing-slash redirect would send the client to
        /api/members instead of /admin-dashboard/api/members. Relative locations
        need no help: the browser resolves them against the prefixed request URL.
        """
        parts = urlsplit(location)
        if not parts.path.startswith("/"):
            return location
        if parts.path == self.prefix or parts.path.startswith(self.prefix + "/"):
            return location
        return urlunsplit(parts._replace(path=self.prefix + parts.path))

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        scope = dict(scope)
        path = scope.get("path", "")
        if path.startswith(self.prefix):
            scope["path"] = path[len(self.prefix):] or "/"
        scope["root_path"] = ""

        async def send_with_prefix(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                location = headers.get("location")
                if location:
                    headers["location"] = self._restore_prefix(location)
            await send(message)

        await self.app(scope, receive, send_with_prefix)


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_portal() -> FastAPI:
    base = ROOT / "portal-frontend-code"
    load_dotenv(base / ".env", override=True)
    return _load_module("portal_backend_main", base / "Backend" / "main.py").app


def _load_admin() -> FastAPI:
    base = ROOT / "admin-dashboard"
    # override=True because admin's own config.py load_dotenv() will not replace
    # variables the portal's .env already put in os.environ — without it the admin
    # backend silently inherits the portal's DB credentials.
    load_dotenv(base / ".env", override=True)
    # admin's Backend/ is a flat, package-less tree ("from config import ...",
    # "from routers import ..."), which only resolves with Backend/ on sys.path —
    # normally the working directory it is started from.
    sys.path.insert(0, str(base / "Backend"))
    return _load_module("admin_backend_main", base / "Backend" / "main.py").app


# Order matters: portal and admin both read DB_HOST/DB_USER/... out of the
# environment at import time, but point at different databases. Each is loaded
# straight after its own .env, so it snapshots its own credentials.
PROJECTS = [
    ("Portal (Local Body Elections)", "/portal-frontend-code", _load_portal),
    ("Admin Dashboard", "/admin-dashboard", _load_admin),
]

gateway = FastAPI(
    title="PSA Backend - All Projects",
    description=__doc__,
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

MOUNTED: list[tuple[str, str, FastAPI]] = []
for _name, _prefix, _loader in PROJECTS:
    _sub_app = _loader()
    gateway.mount(_prefix, StripPrefix(_sub_app, _prefix))
    MOUNTED.append((_name, _prefix, _sub_app))


@gateway.on_event("startup")
def _run_sub_app_startups():
    # Starlette does not propagate lifespan to mounted apps, so the sub-apps' own
    # startup hooks would otherwise never fire.
    for name, _prefix, sub in MOUNTED:
        for handler in sub.router.on_startup:
            try:
                handler()
            except Exception as exc:  # noqa: BLE001 - a warm-up must not block boot
                print(f"[gateway] startup hook failed for {name}: {exc}")


def _namespace_schemas(spec: dict, slug: str) -> dict:
    """Namespace a sub-app's component schemas so identically named models from
    two projects (HTTPValidationError, LoginRequest, ...) cannot collide."""
    names = list((spec.get("components") or {}).get("schemas") or {})
    if not names:
        return spec
    text = json.dumps(spec)
    for name in names:
        # The trailing quote keeps "Item" from also rewriting "ItemList".
        text = text.replace(
            f'"#/components/schemas/{name}"', f'"#/components/schemas/{slug}__{name}"'
        )
    spec = json.loads(text)
    spec["components"]["schemas"] = {
        f"{slug}__{name}": body for name, body in spec["components"]["schemas"].items()
    }
    return spec


def merged_openapi() -> dict:
    merged = {
        "openapi": "3.1.0",
        "info": {
            "title": gateway.title,
            "version": gateway.version,
            "description": (
                "Every endpoint of every project backend in this repository. "
                "Operations are tagged and prefixed with the project they belong to."
            ),
        },
        "paths": {},
        "components": {"schemas": {}},
        "tags": [],
    }

    for name, prefix, sub in MOUNTED:
        slug = re.sub(r"\W+", "_", name).strip("_")
        spec = _namespace_schemas(sub.openapi(), slug)
        merged["openapi"] = spec.get("openapi", merged["openapi"])
        merged["components"]["schemas"].update(
            (spec.get("components") or {}).get("schemas") or {}
        )

        project_tags = set()
        for path, operations in (spec.get("paths") or {}).items():
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                tags = operation.get("tags") or ["API"]
                operation["tags"] = [f"{name} - {tag}" for tag in tags]
                project_tags.update(operation["tags"])
                operation["summary"] = f"[{name}] " + (
                    operation.get("summary") or operation.get("operationId", "")
                )
                # operationIds must stay unique across the merged document, or
                # client generators collapse same-named operations from two projects.
                if operation.get("operationId"):
                    operation["operationId"] = f"{slug}__{operation['operationId']}"
            merged["paths"][f"{prefix}{path}"] = operations

        for tag in sorted(project_tags):
            merged["tags"].append({"name": tag, "description": f"Served from {prefix}"})

    return merged


_MERGED_CACHE: dict = {}


@gateway.get("/openapi.json", include_in_schema=False)
def openapi_json():
    if not _MERGED_CACHE:
        _MERGED_CACHE.update(merged_openapi())
    return JSONResponse(_MERGED_CACHE)


@gateway.get("/docs", include_in_schema=False)
def docs():
    return get_swagger_ui_html(
        openapi_url="/openapi.json", title=f"{gateway.title} - Swagger UI"
    )


@gateway.get("/redoc", include_in_schema=False)
def redoc():
    return get_redoc_html(openapi_url="/openapi.json", title=f"{gateway.title} - ReDoc")


@gateway.get("/", include_in_schema=False)
def index():
    return {
        "service": gateway.title,
        "docs": "/docs",
        "openapi": "/openapi.json",
        "projects": [
            {"name": name, "prefix": prefix, "title": sub.title}
            for name, prefix, sub in MOUNTED
        ],
    }


app = gateway  # so `uvicorn gateway:app` works as well as `uvicorn gateway:gateway`


if __name__ == "__main__":
    if "--dump" in sys.argv:
        out = ROOT / "openapi.json"
        out.write_text(json.dumps(merged_openapi(), indent=2), encoding="utf-8")
        print(f"wrote {out}")
    else:
        import uvicorn

        uvicorn.run(gateway, host="0.0.0.0", port=4000)
