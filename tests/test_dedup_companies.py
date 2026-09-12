import builtins
from contextlib import contextmanager

from scripts import dedup_companies


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _EmptySession:
    def execute(self, _statement):
        return _EmptyResult()


@contextmanager
def _empty_session():
    yield _EmptySession()


def test_progress_output_failure_does_not_abort_dedup(monkeypatch):
    """A detached long-running pipeline may lose its stdout handle."""
    monkeypatch.setattr(dedup_companies, "get_session", _empty_session)

    def broken_print(*_args, **_kwargs):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(builtins, "print", broken_print)

    stats = dedup_companies.run(apply=False)

    assert stats == {
        "tier1_groups": 0,
        "tier1_merged": 0,
        "jobs_repointed": 0,
        "contacts_repointed": 0,
        "tier2_groups": 0,
        "tier2_candidates": [],
    }
