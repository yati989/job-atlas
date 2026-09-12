import json

from scripts.run_company_market_profiles import _snapshot_checkpoint_callback


def test_snapshot_checkpoint_callback_creates_a_missing_checkpoint(tmp_path):
    checkpoint = tmp_path / "phase-b-wave.json"

    _snapshot_checkpoint_callback(str(checkpoint))("snapshot-123", 16)

    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload["glassdoor"]["active_snapshot_id"] == "snapshot-123"
    assert payload["glassdoor"]["active_snapshot_input_count"] == 16
    assert payload["glassdoor"]["status"] == "snapshot_in_progress"
    assert payload["next_safe_resume_step"] == (
        "resume Glassdoor snapshot snapshot-123; never trigger a replacement"
    )
