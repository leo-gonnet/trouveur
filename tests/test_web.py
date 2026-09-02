"""Web auth tests.

These exercise the routes that gate on the session cookie before touching the database, so they
run without Postgres.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trouveur.config import Settings
from trouveur.web import auth
from trouveur.web.app import app


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def settings():
    return Settings(session_secret="test-secret-not-a-real-one")


def test_login_page_renders_without_a_session(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert 'name="username"' in response.text
    assert 'name="password"' in response.text


def test_login_page_states_there_is_no_signup(client):
    assert "no sign-up" in client.get("/login").text.lower()


@pytest.mark.parametrize(
    "path",
    ["/", "/dashboard", "/recommendations", "/search", "/profile", "/companies", "/admin"],
)
def test_unauthenticated_requests_redirect_to_login(client, path):
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_unauthenticated_state_change_is_rejected(client):
    assert client.post("/jobs/1/state", data={"state": "applied"}).status_code == 401


@pytest.mark.parametrize("path", ["/admin/run", "/admin/schedule"])
def test_unauthenticated_admin_writes_do_not_reach_the_database(client, path):
    # No DB is configured in this test, so a redirect (auth check first) rather than a 500 is
    # the proof that the handler bailed before touching the queue.
    assert client.post(path, data={}).status_code == 303
    assert client.post(path, data={}).headers["location"] == "/login"


def test_web_app_never_imports_the_pipeline():
    """The admin page enqueues rows; the runner executes them. If the web module pulls in the
    pipeline, that boundary has been crossed (see AGENTS.md)."""
    import ast
    import inspect

    import trouveur.web.app as webapp

    tree = ast.parse(inspect.getsource(webapp))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("pipeline" in module for module in imported)


def test_no_registration_or_password_reset_route_exists():
    """The single login is created only by the CLI; a web route would be the largest hole."""
    paths = {route.path for route in app.routes}
    for forbidden in ("/register", "/signup", "/users", "/reset", "/forgot"):
        assert forbidden not in paths


def test_openapi_and_docs_are_disabled():
    paths = {route.path for route in app.routes}
    assert "/docs" not in paths and "/openapi.json" not in paths


def test_session_round_trips(settings):
    token = auth.issue_session(settings, "leo")

    class FakeRequest:
        cookies = {auth.COOKIE_NAME: token}

    assert auth.read_session(settings, FakeRequest()) == "leo"


def test_session_signed_with_another_secret_is_rejected(settings):
    token = auth.issue_session(Settings(session_secret="a-different-secret"), "attacker")

    class FakeRequest:
        cookies = {auth.COOKIE_NAME: token}

    assert auth.read_session(settings, FakeRequest()) is None


def test_tampered_session_is_rejected(settings):
    class FakeRequest:
        cookies = {auth.COOKIE_NAME: auth.issue_session(settings, "leo") + "x"}

    assert auth.read_session(settings, FakeRequest()) is None


def test_missing_cookie_yields_no_user(settings):
    class FakeRequest:
        cookies: dict[str, str] = {}

    assert auth.read_session(settings, FakeRequest()) is None


def test_session_cookie_is_httponly_secure_and_samesite(settings):
    from fastapi.responses import RedirectResponse

    response = RedirectResponse("/dashboard", status_code=303)
    auth.set_cookie(response, settings, auth.issue_session(settings, "leo"))
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=lax" in header.replace("samesite", "SameSite")
