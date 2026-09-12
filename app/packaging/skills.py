"""Install the bundled Job Atlas skills into a user's agent skill directory."""

from __future__ import annotations

import hashlib
from importlib import metadata, resources
import json
import os
from pathlib import Path
import shutil
import tempfile

from app.skill_bundle import END_USER_SKILLS


MARKER_NAME = ".job-atlas-managed.json"


def default_skills_directory() -> Path:
    configured = os.getenv("JOB_ATLAS_SKILLS_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".agents" / "skills"


def _version() -> str:
    try:
        return metadata.version("job-atlas")
    except metadata.PackageNotFoundError:
        return "development"


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == MARKER_NAME:
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _copy_resource_tree(source, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_dir():
            _copy_resource_tree(entry, target)
        else:
            target.write_bytes(entry.read_bytes())


def _bundled_skill_source(name: str):
    installed = resources.files("app.skill_bundle").joinpath(name)
    if installed.is_dir():
        return installed
    development = Path(__file__).resolve().parents[2] / ".claude" / "skills" / name
    if development.is_dir():
        return development
    raise RuntimeError(f"Installed package is missing bundled skill: {name}")


def _managed_marker(path: Path) -> dict | None:
    marker = path / MARKER_NAME
    if not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if payload.get("managed_by") == "job-atlas" else None


def _replace_managed_skill(name: str, destination: Path, *, force: bool) -> dict:
    target = destination / name
    existing = _managed_marker(target) if target.exists() else None
    if target.exists() and existing is None and not force:
        raise RuntimeError(
            f"Refusing to overwrite unmanaged skill: {target}. "
            "Move it, or rerun with --force-skills."
        )
    if existing and existing.get("content_sha256") != _tree_hash(target) and not force:
        raise RuntimeError(
            f"Refusing to overwrite locally modified Job Atlas skill: {target}. "
            "Rerun with --force-skills to replace it."
        )

    destination.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=destination))
    backup = destination / f".{name}.job-atlas-backup"
    try:
        source = _bundled_skill_source(name)
        _copy_resource_tree(source, temporary)
        marker = {
            "managed_by": "job-atlas",
            "package_version": _version(),
            "content_sha256": _tree_hash(temporary),
        }
        (temporary / MARKER_NAME).write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            target.replace(backup)
        temporary.replace(target)
        if backup.exists():
            shutil.rmtree(backup)
        return {"name": name, "path": str(target), "status": "installed"}
    except Exception:
        if not target.exists() and backup.exists():
            backup.replace(target)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def install_bundled_skills(
    destination: Path | None = None, *, force: bool = False,
) -> list[dict]:
    root = Path(destination or default_skills_directory()).expanduser()
    return [
        _replace_managed_skill(name, root, force=force)
        for name in END_USER_SKILLS
    ]


def bundled_skill_status(destination: Path | None = None) -> list[dict]:
    root = Path(destination or default_skills_directory()).expanduser()
    results = []
    for name in END_USER_SKILLS:
        target = root / name
        marker = _managed_marker(target) if target.exists() else None
        state = "missing"
        if target.exists() and marker is None:
            state = "unmanaged"
        elif marker:
            state = (
                "installed"
                if marker.get("content_sha256") == _tree_hash(target)
                else "modified"
            )
        results.append({"name": name, "path": str(target), "status": state})
    return results


def uninstall_bundled_skills(destination: Path | None = None) -> list[dict]:
    root = Path(destination or default_skills_directory()).expanduser()
    results = []
    for name in END_USER_SKILLS:
        target = root / name
        if not target.exists():
            results.append({"name": name, "status": "missing"})
            continue
        if _managed_marker(target) is None:
            raise RuntimeError(f"Refusing to remove unmanaged skill: {target}")
        shutil.rmtree(target)
        results.append({"name": name, "status": "removed"})
    return results
