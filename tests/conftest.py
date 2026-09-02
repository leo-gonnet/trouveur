from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def search_payload() -> dict:
    return load("arbeitsagentur_search.json")


@pytest.fixture
def detail_payload() -> dict:
    return load("arbeitsagentur_detail.json")
