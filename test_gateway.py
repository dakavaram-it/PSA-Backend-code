"""Self-check for gateway.py: run `python test_gateway.py` (or pytest).

Covers the two things that can silently break: an endpoint going missing or
losing its project label in the merged spec, and the portal's auth middleware
being bypassed by the mount.
"""

import json
import re
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
    client = TestClient(gateway.gateway)
    portal = PREFIXES["Portal (Local Body Elections)"]

    guarded = client.get(f"{portal}/S15me")
    assert guarded.status_code == 401, "portal endpoint served without a session"
    assert guarded.json()["detail"] == "Not authenticated"

    # /S14login is in the portal's PUBLIC_PATHS allowlist. It must reach the handler
    # rather than the guard, otherwise nobody can log in through the gateway. The
    # handler needs the database, so a connection error still proves it got past the
    # guard; only "Not authenticated" means the allowlist stopped matching.
    try:
        login = client.post(f"{portal}/S14login", json={"username": "x", "password": "y"})
    except Exception:
        return
    assert login.json().get("detail") != "Not authenticated", (
        "the login endpoint is behind the auth guard after mounting"
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
