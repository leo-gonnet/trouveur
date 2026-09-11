"""Every route is proven to require a session.

Asserted over the whole router rather than per handler, because the failure is a two-line
omission -- forgetting `if not session: return _login_redirect()` on one new route -- and a
per-handler test only covers handlers somebody remembered to write a test for. The next route
added is the one that leaks.

No database: the session check runs before any query, so an unauthenticated request must never
reach Postgres. That is also what lets this run on every push rather than only where a server is
available, which matters for the one class of bug where "we run it nightly" is not good enough.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.routing import Route

from trouveur.web.app import app

# The only endpoints that may answer without a session, each for a stated reason.
PUBLIC = {
    "/login",      # the way in
    "/logout",     # clearing a cookie must work even with a bad one
    "/healthz",    # the container probe, which has no credentials
    "/",           # redirects to login or recommendations; discloses nothing
}


def _routes() -> list[tuple[str, str]]:
    found = []
    for route in app.routes:
        if not isinstance(route, Route) or route.path in PUBLIC:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            found.append((method, route.path))
    return found


def _concrete(path: str) -> str:
    """Fill path parameters, so the router matches and we test the handler, not a 404."""
    return (
        path.replace("{public_id}", "00000000-0000-0000-0000-000000000000")
        .replace("{job_id}", "1")
        .replace("{slug}", "example")
        .replace("{scope}", "example")
        .replace("{source}", "greenhouse")
    )


@pytest.mark.parametrize(("method", "path"), _routes(), ids=lambda v: str(v))
async def test_route_rejects_an_unauthenticated_request(method: str, path: str):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(method, _concrete(path), follow_redirects=False)

    assert response.status_code in (302, 303, 307, 401, 403), (
        f"{method} {path} answered {response.status_code} without a session; "
        "every non-public route must reject an anonymous caller."
    )
    if response.status_code in (302, 303, 307):
        assert response.headers["location"].startswith("/login")


async def test_the_router_actually_has_protected_routes():
    """Guards the guard: if _routes() ever returns nothing, the matrix above passes vacuously."""
    assert len(_routes()) >= 10


async def test_a_forged_session_cookie_is_rejected():
    """The cookie is signed; an attacker-supplied value must not authenticate.

    itsdangerous verifies the signature, so this is really asserting that the app reads the cookie
    through read_session rather than trusting it, which is the mistake that makes signing pointless.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("trouveur_session", '{"uid": 1, "u": "attacker"}')
        response = await client.get("/recommendations", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"].startswith("/login")


async def test_healthz_needs_no_session():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/healthz")).status_code == 200
