"""
Pydantic schemas: the normalized shape every collector must return.

Every connector, regardless of source, must produce a list of NormalizedJob
objects. This is the contract between "messy source-specific data" and the
rest of the pipeline (storage, dedupe, NLP, scoring).
"""
from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field


class NormalizedJob(BaseModel):
    source: str = Field(..., description="Connector name, e.g. 'linkedin', 'himalayas'")
    external_job_id: str = Field(..., description="Stable ID from the source system")

    title: str
    company_name_raw: str

    location_raw: Optional[str] = None
    is_remote: Optional[bool] = None
    remote_scope: Optional[str] = None

    employment_type: Optional[str] = None
    seniority: Optional[str] = None

    description_raw: Optional[str] = None
    salary_raw: Optional[str] = None

    apply_url: Optional[str] = None
    job_url: Optional[str] = None

    posted_at: Optional[datetime] = None

    raw_payload: dict[str, Any] = Field(default_factory=dict)
