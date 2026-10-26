"""Shared fixtures.

Fixtures here are hand-written and synthetic. Never commit a captured page or a real scraped
payload: it is third-party content, it bloats the repository, and a fixture nobody wrote is a
fixture nobody understands when it starts failing.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# A BaseException, because the runner isolates each stage behind `except Exception` and would log
# the request away: a test that reached the network once passed while it fetched for ten minutes.
class NetworkInTest(BaseException):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def refuse(self, request):
        raise NetworkInTest(f"A test sent a real request to {request.url}; stub it instead.")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def pytest_addoption(parser):
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="Rewrite golden expectations from current behaviour, then read the diff.",
    )


@pytest.fixture
def aa_listing() -> dict:
    return load("arbeitsagentur_listing.json")


@pytest.fixture
def aa_detail() -> dict:
    return load("arbeitsagentur_detail.json")


@pytest.fixture
def aa_agency_detail() -> dict:
    return load("arbeitsagentur_agency_detail.json")


@pytest.fixture
def gh_board() -> dict:
    return load("greenhouse_board.json")
