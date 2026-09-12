"""
Quick sanity check for the contact-finding pipeline seam
(`find_contacts_for_company`) against one real company, WITHOUT writing
anything to the database — mirrors `smoke_test_connectors.py`'s role for
job connectors.

Run with:
    python -m scripts.smoke_test_contacts --company "Some Company Name"
    python -m scripts.smoke_test_contacts --company-id 42
"""
import argparse

from sqlalchemy import select

from app.contacts.pipeline import find_contacts_for_company
from app.contacts.search_source import close_search_browser
from app.db.session import get_session
from app.models.orm import Company


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", help="Exact company name to look up")
    parser.add_argument("--company-id", type=int, help="Company.id to look up")
    args = parser.parse_args()

    with get_session() as session:
        if args.company_id:
            company = session.get(Company, args.company_id)
        elif args.company:
            company = session.execute(
                select(Company).where(Company.name == args.company)
            ).scalar_one_or_none()
        else:
            company = session.execute(select(Company).limit(1)).scalar_one_or_none()

        if not company:
            print("No matching company found.")
            return

        print(f"Finding contacts for: {company.name} (id={company.id})")
        result = find_contacts_for_company(company, session)

        print(f"Status: {result.status}")
        print(f"Contacts found: {len(result.contacts)}")
        for contact in result.contacts:
            print(
                f"  - {contact.full_name} | {contact.title} | "
                f"{contact.seniority_tier} | {contact.linkedin_url} | "
                f"{contact.email_guess} ({contact.email_verification_status})"
            )


if __name__ == "__main__":
    try:
        main()
    finally:
        close_search_browser()
