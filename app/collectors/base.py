"""
Base connector interface.

Every source-specific collector (Remotive, Himalayas, We Work Remotely, ...)
must subclass BaseConnector and implement fetch(). This keeps the ingestion
pipeline agnostic to how each source actually works (API vs HTML vs browser).
"""
from abc import ABC, abstractmethod

import httpx

from app.models.schemas import NormalizedJob

# Same default every connector used to pass to its own per-call
# `httpx.Client(timeout=20.0, ...)`, before all connectors were migrated to
# the pooled client below (issues #32-#36). Kept here so the pooled client's
# default matches that original behavior exactly.
DEFAULT_TIMEOUT = 20.0

# Sensible keep-alive pool sizing for a single-process ingestion run that
# hits many distinct hosts, each a handful of times per run.
DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)


class NonRetryableFetchError(RuntimeError):
    """A terminal source condition that a runner retry must not repeat."""

    retryable = False


class PartialFetchError(RuntimeError):
    """A connector failed terminally after producing usable jobs.

    The pipeline may retain ``jobs`` through its normal gate/upsert seam while
    still reporting ``cause`` as a failed attempt.  Connectors that never need
    partial-result handling keep the existing ``fetch() -> list`` contract.
    """

    def __init__(self, message: str, jobs: list[NormalizedJob], cause: Exception):
        super().__init__(message)
        self.jobs = jobs
        self.cause = cause
        self.retryable = getattr(cause, "retryable", True)


class BaseConnector(ABC):
    source_name: str = "base"

    _client: httpx.Client | None = None

    @staticmethod
    def make_pooled_client(
        headers: dict | None = None,
        verify: bool | str = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> httpx.Client:
        """Build a keep-alive, connection-pooled httpx.Client.

        Added in issue #32; every httpx-using connector (#33-#36) now goes
        through this instead of constructing its own per-call
        `httpx.Client(...)`, passing whatever headers/verify that source
        needs rather than being forced onto one global set.
        """
        return httpx.Client(
            timeout=timeout,
            headers=headers,
            verify=verify,
            limits=DEFAULT_LIMITS,
        )

    @property
    def client(self) -> httpx.Client:
        """Lazily-created pooled client, reused across every request made
        through this connector instance (rather than opening a fresh
        connection per call). Opt-in — see `make_pooled_client`."""
        if self._client is None:
            self._client = self.make_pooled_client()
        return self._client

    def close_client(self) -> None:
        """Release the pooled client's connections, if one was ever created."""
        if self._client is not None:
            self._client.close()
            self._client = None

    @abstractmethod
    def fetch(self) -> list[NormalizedJob]:
        """Fetch and normalize jobs from this source. Must return a list of
        NormalizedJob objects — never raw/source-specific dicts."""
        raise NotImplementedError
