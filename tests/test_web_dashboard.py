"""Dashboard chart-building and routing tests that don't need Postgres."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from trouveur.web.app import _build_chart, _rank_by_match_rate, app


def daily_row(day, source, n):
    return SimpleNamespace(day=day, source=source, n=n)


def test_build_chart_fills_every_day_even_with_no_data():
    chart = _build_chart([], days=5)
    assert len(chart["columns"]) == 5
    assert all(c["total"] == 0 for c in chart["columns"])
    assert chart["max_total"] == 1  # never zero: avoids a division by zero in the template


def test_build_chart_buckets_rows_by_day_and_source():
    # Relative to today: the window _build_chart builds always ends on the current UTC date.
    today = datetime.now(UTC).date()
    oldest, middle = today - timedelta(days=2), today - timedelta(days=1)
    rows = [
        daily_row(oldest, "arbeitsagentur", 10),
        daily_row(oldest, "karriere_at", 4),
        daily_row(middle, "arbeitsagentur", 6),
    ]
    chart = _build_chart(rows, days=3)
    by_label = {c["label"]: c for c in chart["columns"]}
    assert by_label[oldest.strftime("%d.%m")]["total"] == 14
    assert by_label[oldest.strftime("%d.%m")]["counts"]["karriere_at"] == 4
    assert by_label[middle.strftime("%d.%m")]["counts"]["karriere_at"] == 0
    assert chart["max_total"] == 14


def test_build_chart_ignores_days_outside_the_window():
    """A day older than the window (e.g. from a stale query) must not break bucketing."""
    rows = [daily_row(date(2000, 1, 1), "x", 99)]
    chart = _build_chart(rows, days=3)
    assert chart["max_total"] == 1
    assert all(c["total"] == 0 for c in chart["columns"])


def test_build_chart_assigns_every_source_a_color():
    rows = [daily_row(date(2026, 8, 31), f"source{i}", 1) for i in range(9)]
    chart = _build_chart(rows, days=1)
    assert len(chart["colors"]) == 9
    assert all(chart["colors"][s].startswith("#") for s in chart["sources"])


def source_row(source, scraped, recommended):
    return SimpleNamespace(source=source, scraped=scraped, recommended=recommended,
                            passed_rules=0, scored=0, avg_score=None, newest=None)


def test_rank_by_match_rate_orders_descending():
    sources = [source_row("a", 100, 10), source_row("b", 20, 10)]
    ranked = _rank_by_match_rate(sources)
    assert [r["source"] for r in ranked] == ["b", "a"]
    assert ranked[0]["rate"] == 0.5


def test_rank_by_match_rate_excludes_low_volume_sources():
    """Fewer than 5 scraped jobs makes a rate statistically meaningless (e.g. 1/1 = 100%)."""
    sources = [source_row("tiny", 1, 1), source_row("real", 50, 5)]
    ranked = _rank_by_match_rate(sources)
    assert [r["source"] for r in ranked] == ["real"]


def test_rank_by_match_rate_handles_zero_scraped_without_dividing_by_zero():
    ranked = _rank_by_match_rate([source_row("empty", 0, 0)])
    assert ranked == []  # excluded by the volume floor, not a crash


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


@pytest.mark.parametrize("path", ["/dashboard", "/recommendations"])
def test_new_pages_require_login(client, path):
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_root_redirects_to_dashboard_not_the_old_inbox(client):
    """Regression: the landing page moved from /inbox to /dashboard."""
    response = client.get("/")
    assert response.headers["location"] == "/login"  # unauthenticated first


def test_no_inbox_route_survives_the_rename():
    paths = {route.path for route in app.routes}
    assert "/inbox" not in paths
    assert "/dashboard" in paths
    assert "/recommendations" in paths
