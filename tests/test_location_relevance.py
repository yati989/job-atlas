from types import SimpleNamespace

import pytest

from app.pipeline.relevance import location_ok


def remote_job(scope: str):
    return SimpleNamespace(location_raw=None, remote_scope=scope, is_remote=True)


def test_argentina_only_remote_job_is_not_india_eligible():
    assert not location_ok(remote_job("Argentina"))


@pytest.mark.parametrize(
    "country",
    [
        "Brazil",
        "Mexico",
        "France",
        "Japan",
        "South Africa",
        "United Arab Emirates",
        "Singapore",
    ],
)
def test_named_foreign_country_only_remote_job_is_not_india_eligible(country):
    assert not location_ok(remote_job(country))


@pytest.mark.parametrize(
    "scope",
    ["India, Argentina", "Worldwide", "Global", "APAC", "Asia"],
)
def test_india_inclusive_and_broad_remote_scopes_remain_eligible(scope):
    assert location_ok(remote_job(scope))
