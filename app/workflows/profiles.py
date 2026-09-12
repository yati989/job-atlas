"""Private, versioned storage for accepted search profiles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.config.authentication import default_private_home
from app.workflows.planning import (
    ConfigurationInput,
    SearchProfile,
    SourceCapability,
    prepare_run,
)


@dataclass(frozen=True)
class AcceptedProfile:
    profile: SearchProfile
    revision: int
    fingerprint: str
    current_path: Path
    snapshot_path: Path


def _serialized(profile: SearchProfile) -> str:
    return json.dumps(profile.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def _fingerprint(serialized: str) -> str:
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ProfileStore:
    """Persist immutable accepted revisions beneath a private data root."""

    def __init__(self, private_root: Path):
        self.private_root = Path(private_root).expanduser()

    def _profile_dir(self, name: str) -> Path:
        return self.private_root / "profiles" / name

    def load(self, name: str) -> SearchProfile:
        path = self._profile_dir(name) / "current.json"
        return SearchProfile.model_validate_json(path.read_text(encoding="utf-8"))

    def accept(
        self,
        configuration: ConfigurationInput,
        source_catalog: Sequence[SourceCapability],
    ) -> AcceptedProfile:
        profile = prepare_run(configuration, source_catalog).profile
        serialized = _serialized(profile)
        fingerprint = _fingerprint(serialized)
        profile_dir = self._profile_dir(profile.name)
        revisions_dir = profile_dir / "revisions"
        current_path = profile_dir / "current.json"

        if current_path.exists():
            current = current_path.read_text(encoding="utf-8")
            if _fingerprint(current) == fingerprint:
                matches = sorted(revisions_dir.glob(f"*-{fingerprint[:12]}.json"))
                if not matches:
                    raise RuntimeError("current profile has no immutable revision")
                revision = int(matches[-1].name.split("-", 1)[0])
                return AcceptedProfile(
                    profile, revision, fingerprint, current_path, matches[-1]
                )

        revisions_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(revisions_dir.glob("[0-9][0-9][0-9][0-9]-*.json"))
        revision = (int(existing[-1].name.split("-", 1)[0]) + 1) if existing else 1
        snapshot_path = revisions_dir / f"{revision:04d}-{fingerprint[:12]}.json"
        snapshot_path.write_text(serialized, encoding="utf-8")
        temporary = profile_dir / ".current.json.tmp"
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(current_path)
        return AcceptedProfile(
            profile, revision, fingerprint, current_path, snapshot_path
        )
