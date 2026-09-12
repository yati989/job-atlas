"""Compact, deterministic edits from the reviewed resume master.

The agent emits only changed values and list selections.  Code applies that
small plan to the canonical master and validates the ordinary ResumeMaster
schema; the database can continue storing the resulting full snapshot.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .schema import ResumeMaster


_REPLACE_PATHS = re.compile(
    r"^/(?:summary|skills/\d+/items/\d+|experience/\d+/bullets/\d+|projects/\d+/bullets/\d+)$"
)
_SELECT_PATHS = re.compile(
    r"^/(?:skills|experience|projects|achievements|skills/\d+/items|experience/\d+/bullets|projects/\d+/bullets)$"
)


def _parts(path: str) -> list[str]:
    if not path.startswith("/") or path == "/":
        raise ValueError("plan paths must be non-root JSON pointers")
    parts = path[1:].split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("plan paths contain an invalid segment")
    return parts


class Replacement(BaseModel):
    path: str
    value: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        _parts(value)
        if not _REPLACE_PATHS.fullmatch(value):
            raise ValueError("only summary, skill items, and experience/project bullets may be rewritten")
        return value


class Selection(BaseModel):
    path: str
    indices: list[int] = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        _parts(value)
        if not _SELECT_PATHS.fullmatch(value):
            raise ValueError("this list cannot be selected or reordered")
        return value

    @field_validator("indices")
    @classmethod
    def validate_indices(cls, value: list[int]) -> list[int]:
        if any(index < 0 for index in value) or len(set(value)) != len(value):
            raise ValueError("selection indices must be unique non-negative integers")
        return value


class ResumeTailoringPlan(BaseModel):
    replacements: list[Replacement] = Field(default_factory=list)
    selections: list[Selection] = Field(default_factory=list)


def _resolve(data: Any, parts: list[str]) -> Any:
    current = data
    for part in parts:
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"invalid list path segment {part!r}") from exc
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise ValueError(f"unknown resume plan path segment {part!r}")
    return current


def apply_tailoring_plan(master: ResumeMaster, plan: ResumeTailoringPlan) -> ResumeMaster:
    data = deepcopy(master.model_dump())
    for edit in plan.replacements:
        parts = _parts(edit.path)
        parent = _resolve(data, parts[:-1])
        leaf = parts[-1]
        if isinstance(parent, list):
            try:
                index = int(leaf)
                original = parent[index]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"invalid replacement path {edit.path!r}") from exc
            if not isinstance(original, str):
                raise ValueError("replacements may change text fields only")
            parent[index] = edit.value
        elif isinstance(parent, dict) and leaf in parent and isinstance(parent[leaf], str):
            parent[leaf] = edit.value
        else:
            raise ValueError("replacements may change existing text fields only")

    # Child lists are selected before their parents, so all indices refer to
    # the original reviewed master rather than earlier parent reordering.
    for selection in sorted(plan.selections, key=lambda item: item.path.count("/"), reverse=True):
        target = _resolve(data, _parts(selection.path))
        if not isinstance(target, list):
            raise ValueError(f"selection path {selection.path!r} is not a list")
        try:
            chosen = [target[index] for index in selection.indices]
        except IndexError as exc:
            raise ValueError(f"selection index is outside {selection.path!r}") from exc
        target[:] = chosen
    return ResumeMaster.model_validate(data)
