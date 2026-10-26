"""Importing a board list against a real Postgres: leads per list, the trial order, the counts."""

from __future__ import annotations

from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.discovery.lists import import_files
from trouveur.discovery.work import record_leads
from trouveur.models import Lead, LeadOrigin

_LIST = (
    "name,slug,url\n"
    "Eins,eins,https://job-boards.greenhouse.io/eins\n"
    "Zwei,zwei,https://job-boards.greenhouse.io/zwei\n"
    "Ohne,ohne,\n"
)


async def test_a_list_lands_as_leads_and_proposes_disabled_boards_once(clean_db, tmp_path):
    path = tmp_path / "greenhouse.csv"
    path.write_text(_LIST)

    [first] = await import_files([path], "open-boards")
    assert (first.rows, first.new_leads, first.without_link) == (3, 2, 1)
    [again] = await import_files([path], "open-boards")
    assert again.new_leads == 0

    async with connect() as conn:
        leads = await conn.exec_driver_sql(
            "SELECT origin::text, list_name, source, scope FROM discovery_lead ORDER BY scope"
        )
        tenants = await conn.exec_driver_sql(
            "SELECT scope, enabled, origin::text FROM source_tenant ORDER BY scope"
        )
        assert [tuple(row) for row in leads] == [
            ("list", "open-boards", "greenhouse", "eins"),
            ("list", "open-boards", "greenhouse", "zwei"),
        ]
        assert [tuple(row) for row in tenants] == [
            ("eins", False, "discovered"),
            ("zwei", False, "discovered"),
        ]


async def test_a_board_a_reader_reported_is_tried_before_a_whole_list(clean_db, tmp_path):
    path = tmp_path / "greenhouse.csv"
    path.write_text(_LIST)
    await import_files([path], "open-boards")
    await record_leads(
        [Lead(origin=LeadOrigin.USER_REPORT, url="https://job-boards.greenhouse.io/zzz/jobs/1")]
    )

    async with connect() as conn:
        due = await admin_q.tenants_due_for_trial(
            conn, limit=1, recheck_days=30, sources=["greenhouse"]
        )
    assert due == {"greenhouse": ["zzz"]}


async def test_the_coverage_report_counts_boards_per_list(clean_db, tmp_path):
    from trouveur.coverage import leads

    path = tmp_path / "greenhouse.csv"
    path.write_text(_LIST)
    await import_files([path], "open-boards")
    await record_leads(
        [Lead(origin=LeadOrigin.ARCHIVE, url="https://job-boards.greenhouse.io/eins/jobs/9")]
    )
    async with connect() as conn:
        await admin_q.record_trials(conn, {"greenhouse": ["eins", "zwei"]})
        await admin_q.set_tenant_enabled(conn, "greenhouse", "eins", True)
        card = await leads(conn)

    rows = {row.label: (row.leads, row.boards, row.tried, row.kept) for row in card.origins}
    assert rows == {
        "list open-boards": (2, 2, 2, 1),
        "archive": (1, 1, 1, 1),
    }
