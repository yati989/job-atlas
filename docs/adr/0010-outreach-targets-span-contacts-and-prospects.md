# An outreach draft addresses a recipient, not a Contact

Status: accepted

Outreach must reach two populations that this schema keeps deliberately apart:
people found by `find-contacts` (rows in `contacts`, hanging off `companies`),
and people at **prospect** companies, which ADR-0008 put in a standalone table
with no link to `companies` at all. Since `contacts.company_id` is `NOT NULL`,
a prospect *cannot* have a Contact row — so there is no existing place to put
"the person I am emailing at a prospect company."

We therefore made the outreach row itself the addressing authority. Every row
carries `to_email` and `to_name` directly, and `contact_id`, `company_id` and
`prospect_id` are all **nullable**. A stored contact fills `contact_id`; a
prospect fills `prospect_id` and carries the address the candidate supplied by
hand.

This names something true rather than working around a limitation: **outreach
is the first concept in this system that does not care whether its target came
from a scraped job or from research.** Ingestion, contact-finding and prospect
discovery are all built around that distinction; sending an email is not.

## Considered options

**Build the prospect→company promotion step now.** Rejected as disproportionate.
It would let prospects have Contact rows, but it is the step ADR-0008
deliberately deferred, and it drags `company_categories` with it — a job-less
company derives no search groups and lands in `no_category_match`. All of that
to store an email address the candidate typed in themselves; no contact
*discovery* is involved here at all.

**A separate contacts table for prospects.** Rejected. Two things called
"contact" would diverge, and every downstream consumer would need to know which
one it was holding.

## Consequences

- `to_email` is denormalized against `contacts.email_guess` for the contact
  case. **The row's copy is authoritative** — it records what was actually
  addressed, which is what matters if the contact's guessed email is later
  corrected or re-derived.
- Nothing enforces at the schema level that exactly one of the three foreign
  keys is set. That is a code-level invariant, and the persistence layer must
  check it; a row with neither a contact nor a prospect is a bug.
- If promotion is ever built, this table does not need to change — a promoted
  prospect simply starts arriving with `contact_id` set instead.
