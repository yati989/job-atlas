# A standalone `prospects` table, deliberately not linked to `companies`

Status: accepted

Prospect discovery needed somewhere to put a company that research surfaced
as worth cold-emailing but that no connector ever scraped a job from. Every
`Company` row in this database exists because `upsert_job()` created one
(`app/pipeline/upsert.py:23` is called from nowhere else), so `companies`
carries an implicit meaning — "we have seen a job here" — that a
research-sourced company would quietly break. We added a **separate
`prospects` table with no foreign key to `companies`**, and answer "is this
prospect also a company we scrape?" with a join on the shared normalized name
(`app/companies/naming.py`) when that question actually comes up.

## Considered options

**A `discovery_source` column on `companies`, with prospects as job-less
Company rows.** Rejected, though it was the first design. It would have let
prospects flow straight into `find-contacts`, which takes its batch from
`company_queue.get_company_queue()` and requires a `Company`. But it changes
the meaning of every existing count, query and audit that assumes a company
came from a posting, and it needs a manual `ALTER TABLE` on the live database
(there is no migration tool — `scripts/init_db.py` is `create_all` only). A
new table needs no migration at all.

**A `prospects` table with a `company_id` FK, both rows written together.**
Rejected as premature. It buys a single identity per real-world company, but
commits every write path to maintaining the link before we know whether
prospects are even useful — and the first trial gate might well say they
aren't.

## Consequences

- **Prospects cannot be worked by `find-contacts` yet.** A promotion step is
  needed and is deliberately unbuilt. This is the main cost of the decision
  and it was accepted knowingly.
- `company_categories.categories_for_company()` derives a company's search
  groups from its own job titles, so a promoted job-less prospect would
  return `[]` and land in `no_category_match`. Whatever builds promotion has
  to supply search groups some other way.
- Both sides must normalize names with the *same* function or the join
  silently misses. That is why `normalize()` was extracted out of
  `scripts/dedup_companies.py` into `app/companies/naming.py` — one rule,
  imported by both, guarded by a test asserting they are the same object.
- The join answers "is this company already in our DB". It does **not**
  answer "does this company have a live opening on its own careers page" —
  that is a different, much more expensive question, and it is out of scope.
