from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.workflows.profiles import ProfileStore


CATALOG = ()


def _profile() -> dict:
    return {
        "version": 1,
        "name": "product-design",
        "professions": ["product designer"],
        "search_terms": ["product designer"],
        "relevance_terms": ["product designer", "ux designer"],
        "countries": ["IN"],
        "arrangements": ["remote"],
        "seniority": ["individual_contributor"],
        "collection_window_days": 30,
        "source_selection": {"mode": "all_compatible"},
    }


def test_accept_persists_a_private_versioned_profile_and_loads_current(tmp_path):
    store = ProfileStore(tmp_path)

    accepted = store.accept(_profile(), CATALOG)

    assert accepted.revision == 1
    assert accepted.snapshot_path.name.startswith("0001-")
    assert accepted.snapshot_path.exists()
    assert store.load("product-design") == accepted.profile
    assert json.loads(accepted.current_path.read_text())["name"] == "product-design"


def test_identical_accept_is_idempotent_and_changed_policy_creates_revision(tmp_path):
    store = ProfileStore(tmp_path)
    first = store.accept(_profile(), CATALOG)

    repeated = store.accept(_profile(), CATALOG)
    changed = _profile()
    changed["hard_rejects"] = ["contract work"]
    second = store.accept(changed, CATALOG)

    assert repeated.snapshot_path == first.snapshot_path
    assert repeated.revision == 1
    assert second.revision == 2
    assert first.snapshot_path.exists()
    assert store.load("product-design").hard_rejects == ("contract work",)


def test_unrecognized_secret_field_is_rejected_before_writing(tmp_path):
    profile = _profile()
    profile["bright_data_api_key"] = "secret"
    store = ProfileStore(tmp_path)

    with pytest.raises(ValidationError):
        store.accept(profile, CATALOG)

    assert not (tmp_path / "profiles").exists()
