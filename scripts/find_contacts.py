"""
Batch entrypoint for the contact-finding module (issue #6). Manually
triggered, separate from `app.pipeline.run_all` and not registered in
`app/pipeline/registry.py`.

Processes up to `BATCH_SIZE` companies per invocation (starting default;
expected to be tuned upward based on search yield), with a human-like delay
between each company's search. Each company is enriched once — reaching
`contact_enrichment_status = "done"` removes it from future batches;
refreshing is a manual, future action outside this script's scope.

Run with:
    python -m scripts.find_contacts
"""
import atexit
import logging

from sqlalchemy import exists, select

from app.collectors.browser.session_utils import human_delay
from app.contacts.pipeline import find_contacts_for_company
from app.contacts.search_source import close_search_browser
from app.contacts.upsert import apply_contact_find_result
from app.db.session import get_session
from app.models.orm import Company, Job

# The browser search transport keeps a headed Chrome open across the whole
# batch (see search_source._ensure_browser_page) — close it on normal exit
# and as a safety net on unexpected exit, so it doesn't leak between runs.
atexit.register(close_search_browser)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("find_contacts")

BATCH_SIZE = 10


def run() -> None:
    with get_session() as session:
        # Eligible once a company has ANY job-posting history (active or
        # not) — not scoped to a currently active/category-matching
        # posting. In practice every Company row is created via job
        # upsert, so this is a defensive check rather than a live filter.
        has_job_history = exists().where(Job.company_id == Company.id)
        companies = session.execute(
            select(Company)
            .where(Company.contact_enrichment_status == "pending", has_job_history)
            .limit(BATCH_SIZE)
        ).scalars().all()

        if not companies:
            logger.info("No companies pending contact enrichment.")
            return

        logger.info("Processing %s companies for contact-finding.", len(companies))

        for i, company in enumerate(companies):
            try:
                result = find_contacts_for_company(company, session)
                rows = apply_contact_find_result(session, company, result)
                session.commit()
                logger.info(
                    "Company %s (%s): status=%s contacts=%s",
                    company.id, company.name, result.status, len(rows),
                )
            except Exception:
                session.rollback()
                logger.exception("Failed to find contacts for company %s (%s)", company.id, company.name)

            if i < len(companies) - 1:
                human_delay(5.0, 12.0)


if __name__ == "__main__":
    run()
