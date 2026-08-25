from datetime import datetime

from news.run_identity import new_run_identity, parse_run_id, portable_package_name


def test_run_identity_is_shared_format_and_parsable():
    identity = new_run_identity(
        job_id="rfi-default",
        started_at=datetime(2026, 8, 25, 8, 0, 12),
    )

    assert identity.run_id == "20260825-080012"
    assert identity.display_label == "08:00:12 · rfi-default"
    assert parse_run_id(identity.run_id) == datetime(2026, 8, 25, 8, 0, 12)


def test_same_second_existing_run_gets_unique_suffix(tmp_path):
    source_root = tmp_path / "portable"
    source_root.mkdir()
    (source_root / "Laxinwen-RFI-2026-08-25-20260825-080012-rfi-default").mkdir()

    identity = new_run_identity(
        job_id="rfi-default",
        source_id="rfi",
        output_root=source_root,
        started_at=datetime(2026, 8, 25, 8, 0, 12),
    )

    assert identity.run_id == "20260825-080012-02"


def test_portable_package_name_uses_time_without_repeating_date():
    identity = new_run_identity(
        job_id="rfi-default",
        started_at=datetime(2026, 8, 25, 8, 0, 12),
    )
    assert portable_package_name("rfi", identity.started_at, identity.run_id, identity.job_id) == (
        "Laxinwen-RFI-2026-08-25-080012-rfi-default"
    )
