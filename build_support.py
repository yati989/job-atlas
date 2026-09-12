"""Setuptools hooks that bundle canonical skills without duplicating their source."""

from __future__ import annotations

from pathlib import Path
import shutil

from setuptools.command.build_py import build_py as _build_py

END_USER_SKILLS = (
    "full-pipeline",
    "enrich-jobs",
    "enrich-companies",
    "tailor-resumes",
    "find-profile-links",
    "mock-interview",
    "draft-outreach",
)


class BuildPy(_build_py):
    """Copy selected canonical skills into the built Python package."""

    def run(self) -> None:
        super().run()
        root = Path(__file__).resolve().parent
        destination = Path(self.build_lib) / "app" / "skill_bundle"
        for name in END_USER_SKILLS:
            shutil.copytree(
                root / ".claude" / "skills" / name,
                destination / name,
                dirs_exist_ok=True,
            )

    def get_outputs(self, include_bytecode: bool = True) -> list[str]:
        outputs = list(super().get_outputs(include_bytecode=include_bytecode))
        root = Path(__file__).resolve().parent
        destination = Path(self.build_lib) / "app" / "skill_bundle"
        for name in END_USER_SKILLS:
            source = root / ".claude" / "skills" / name
            outputs.extend(
                str(destination / name / path.relative_to(source))
                for path in source.rglob("*")
                if path.is_file()
            )
        return outputs
