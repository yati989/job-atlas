"""
One-time backfill (issue #71): classify every company's `company_type`
(employer/staffing) from its existing `industry` text and report the split.

Run with:
    python -m scripts.classify_company_type
"""
from sqlalchemy import select

from app.contacts.company_type import classify
from app.db.session import get_session
from app.models.orm import Company


def main() -> None:
    with get_session() as session:
        companies = session.execute(select(Company)).scalars().all()

        counts = {"employer": 0, "staffing": 0}
        changed = 0
        for company in companies:
            new_type = classify(
                company.industry,
                company_name=company.name,
                description=company.description,
            )
            counts[new_type] += 1
            if company.company_type != new_type:
                company.company_type = new_type
                changed += 1

        print(f"Companies classified: {len(companies)}")
        print(f"  employer: {counts['employer']}")
        print(f"  staffing: {counts['staffing']}")
        print(f"Rows updated (type changed from stored default): {changed}")


if __name__ == "__main__":
    main()
