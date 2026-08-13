"""Self-check for gateway.py: run `python test_gateway.py` (or pytest).

Covers the two things that can silently break: an endpoint going missing or
losing its project label in the merged spec, and the portal's auth middleware
being bypassed by the mount.
"""

import json
import re
import sys
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

import gateway

PREFIXES = {name: prefix for name, prefix, _ in gateway.MOUNTED}


def test_every_project_contributes_labelled_endpoints():
    spec = gateway.merged_openapi()

    for name, prefix in PREFIXES.items():
        paths = [p for p in spec["paths"] if p.startswith(prefix + "/")]
        assert paths, f"{name} contributed no endpoints"

        for path in paths:
            for method, operation in spec["paths"][path].items():
                if not isinstance(operation, dict):
                    continue
                assert all(t.startswith(name) for t in operation["tags"]), (
                    f"{method.upper()} {path} is not tagged with its project: "
                    f"{operation['tags']}"
                )
                assert operation["summary"].startswith(f"[{name}]"), (
                    f"{method.upper()} {path} summary is missing the project name"
                )

    unprefixed = [
        p for p in spec["paths"] if not any(p.startswith(x) for x in PREFIXES.values())
    ]
    assert not unprefixed, f"paths outside every project prefix: {unprefixed}"


def test_schema_refs_all_resolve_after_namespacing():
    spec = gateway.merged_openapi()
    defined = set(spec["components"]["schemas"])
    refs = set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(spec)))
    assert refs <= defined, f"dangling $refs after merge: {sorted(refs - defined)}"


def test_portal_auth_guard_survives_the_mount():
    # Paths come from the portal itself rather than being spelled out here: the
    # routes have been renamed once already (/S14login -> /login), and a hard-coded
    # copy of a name only fails long after the rename.
    name = "Portal (Local Body Elections)"
    prefix = PREFIXES[name]
    portal_app = next(app for n, _p, app in gateway.MOUNTED if n == name)
    public = sys.modules["portal_backend_main"].PUBLIC_PATHS
    client = TestClient(gateway.gateway)

    guarded = next(
        r.path
        for r in portal_app.routes
        if "GET" in getattr(r, "methods", ()) and r.path not in public
    )
    response = client.get(f"{prefix}{guarded}")
    assert response.status_code == 401, f"{guarded} served without a session"
    assert response.json()["detail"] == "Not authenticated"

    # Everything in PUBLIC_PATHS (login above all) must reach the router rather than
    # the guard, otherwise nobody can log in through the gateway. A GET on the
    # POST-only login answers 405 from the router, which is proof enough it got past
    # the guard - and needs no database.
    for path in sorted(public):
        allowed = client.get(f"{prefix}{path}")
        assert allowed.status_code != 401, (
            f"{path} is behind the auth guard after mounting"
        )


def test_redirects_keep_the_project_prefix():
    # FastAPI answers a trailing-slash miss with a 307 built from the path the
    # sub-app sees, which StripPrefix has already shortened. Without the Location
    # header being rewritten the client is sent to /api/members and gets a 404.
    client = TestClient(gateway.gateway, follow_redirects=False)

    for url in ["/admin-dashboard/api/members/"]:
        response = client.get(url)
        assert response.status_code == 307, f"{url} no longer redirects"
        location = response.headers["location"]
        prefix = "/" + url.split("/")[1]
        assert urlsplit(location).path.startswith(prefix), (
            f"{url} redirects to {location}, losing the {prefix} prefix"
        )


def test_index_lists_every_project():
    body = TestClient(gateway.gateway).get("/").json()
    assert {p["prefix"] for p in body["projects"]} == set(PREFIXES.values())


if __name__ == "__main__":
    for check in [
        test_every_project_contributes_labelled_endpoints,
        test_schema_refs_all_resolve_after_namespacing,
        test_portal_auth_guard_survives_the_mount,
        test_redirects_keep_the_project_prefix,
        test_index_lists_every_project,
    ]:
        check()
        print("ok:", check.__name__)
