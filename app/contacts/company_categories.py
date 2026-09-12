"""
Derive which target-role categories a company's own job-posting history
matches, so contact-finding searches the role ladder that actually fits
that company (not a fixed generic ladder, and not one the user has to
specify per company). No category is stored on `Job` rows today (keyword
matching happens at filter time, not persisted), so this re-derives it by
scanning each Job's title/description against `CATEGORY_KEYWORDS` — same
matching logic as `app.pipeline.filters.filter_by_keywords`, just grouped
by category instead of flattened.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.categories import CATEGORY_KEYWORDS
from app.models.orm import Job


def categories_for_company(session: Session, company_id: int) -> list[str]:
    """Return every category (in CATEGORY_KEYWORDS's insertion order) that
    at least one of this company's jobs matches on title/description."""
    stmt = select(Job.title, Job.description_raw).where(Job.company_id == company_id)
    rows = session.execute(stmt).all()

    matched: list[str] = []
    for category, keywords in CATEGORY_KEYWORDS.items():
        lowered_keywords = [kw.lower() for kw in keywords]
        for title, description in rows:
            haystack = f"{title or ''} {description or ''}".lower()
            if any(kw in haystack for kw in lowered_keywords):
                matched.append(category)
                break

    return matched
