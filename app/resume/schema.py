"""
The resume master schema: the canonical, structured shape of the user's true
experience.

`ResumeMaster` is the single source of truth every tailored resume derives
from. It holds *content only* — no presentation/LaTeX concerns live here (that
is the renderer's and template's job). A tailored resume is just another
`ResumeMaster` instance (the same shape), produced by reordering/re-emphasizing/
rewording the master's true content — never by adding anything not grounded in
it.
"""
import os
from pathlib import Path
from typing import Optional, Union

import yaml
from pydantic import BaseModel, Field


class Contact(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    location: Optional[str] = None
    links: list[str] = Field(
        default_factory=list,
        description="Profile URLs (LinkedIn, GitHub, portfolio, ...), as plain text.",
    )


class SkillGroup(BaseModel):
    category: str = Field(..., description="e.g. 'Core Skills', 'Languages', 'Tools'")
    items: list[str]


class Experience(BaseModel):
    company: str
    title: Optional[str] = Field(
        None, description="Role title; may be absent for entries like a career break."
    )
    dates: Optional[str] = Field(None, description="Free-text range, e.g. 'Jan 2024 - Aug 2025'")
    location: Optional[str] = None
    bullets: list[str] = Field(
        default_factory=list,
        description=(
            "Achievement bullets, each a complete true statement. A bullet may "
            "carry its own 'Project Name | Tools: description' label inline."
        ),
    )


class Education(BaseModel):
    institution: str
    degree: Optional[str] = None
    field: Optional[str] = None
    dates: Optional[str] = None
    gpa: Optional[str] = None
    location: Optional[str] = None


class Project(BaseModel):
    name: str
    context: Optional[str] = Field(
        None, description="Advisor/course/venue, e.g. 'Prof. P. Balamurugan'"
    )
    dates: Optional[str] = None
    bullets: list[str] = Field(default_factory=list)


class Achievement(BaseModel):
    text: str
    year: Optional[str] = None


class ResumeMaster(BaseModel):
    """The user's complete, true, canonical resume content.

    Parsed/authored once and reviewed by the user; the original resume file is
    never re-parsed after this. Both the base resume and any tailored resume are
    represented by this same model.
    """

    contact: Contact
    summary: Optional[str] = None
    skills: list[SkillGroup] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    achievements: list[Achievement] = Field(default_factory=list)


EXAMPLE_MASTER_PATH = Path(__file__).parents[2] / "examples" / "resume-master.example.yaml"


def default_master_path() -> Path:
    """Return the user's private, cross-platform resume-master location."""
    configured = os.getenv("RESUME_MASTER_PATH")
    if configured:
        return Path(configured).expanduser()
    private_home = Path(
        os.getenv(
            "JOB_ATLAS_HOME",
            os.getenv("JOB_SEARCH_AGENT_HOME", str(Path.home() / ".job-atlas")),
        )
    ).expanduser()
    return private_home / "resume-master.yaml"


MASTER_PATH = default_master_path()


def load_master(path: Union[str, Path, None] = None) -> ResumeMaster:
    """Load and validate a resume master from YAML.

    Raises pydantic's ValidationError on malformed/missing-required content and
    yaml.YAMLError on unparseable YAML — fail loudly, never silently coerce.
    """
    path = Path(path).expanduser() if path is not None else default_master_path()
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping (got {type(data).__name__}).")
    return ResumeMaster.model_validate(data)
