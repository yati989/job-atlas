# Email derivation — MX-only, 4 candidates, no search spent

## Neither SMTP verification nor leak-search survived

Confirmed live (2026-08-02): a raw TCP connect to port 25 times out for
every domain tested, including `gmail.com` as a control — this is an
outbound port block on the network the agent runs from, not the target
servers being unusually defensive. `derive_email`'s RCPT probe can never
complete here, period.

An earlier pivot tried to work around that by leak-searching a real address
in a job ad and inferring the domain's shape from it
(`pattern_name_from_example`, `PATTERN_LEAKED`, `Company.email_pattern_1/2/3`).
It worked for 5 of 6 companies in the original trial, then found nothing for
10 of 10 companies in the next batch (US staffing firms + large
multinationals don't share the "recruiter posts their email in a LinkedIn
job ad" convention that made it work originally). Dropped per
`docs/adr/0005`'s 2026-08-02 addendum. **Don't leak-search for a pattern —
this step no longer spends a query on email at all.**

## What replaces it: 4 candidates, shown together

```python
from app.contacts.email_resolution import candidate_addresses, derive_email
```

- `derive_email(full_name, domain)` — MX-checks the domain (does it accept
  mail at all) and returns the single likeliest guess (`candidates[0]`,
  pattern `first.last`) with status `unverified`, or `None` if the name
  can't be split or the domain has no mail records. This is what gets
  stored on `Contact.email_guess`/`email_verification_status` via
  `attach_email` — unchanged call shape from before, just no `known_pattern`
  argument anymore.
- `candidate_addresses(full_name, domain)` — all 4 shapes, most-likely
  first: `first.last`, `first.l`, `f.last`, `first_last`. `write_evidence`
  calls this itself to print the other 3 alongside the stored guess, so the
  reviewer sees every option rather than trusting one silent pick.

Nothing here is a storage gate — a contact with no derivable email is still
stored (per #68, deliberability is a stored attribute, not a gate). You
don't need to do anything extra to get the other 3 candidates into the
evidence file; that happens automatically in step 6.

## `Company.email_pattern_1/2/3` is historical, not consulted

Those columns hold patterns confirmed before the pivot for companies
searched under the old mechanism. They're left alone (no migration tool to
safely undo a drop) but nothing reads or writes them going forward — don't
check them before deriving an email, and don't populate them for new
companies.

## What's still persisted, and why

`Contact.first_name`/`last_name` are populated automatically at persist
time (parsed from `full_name`) — this is what `candidate_addresses`
recomputes from on every call, so nothing needs to be searched or cached
per company. Nothing in the skill's steps needs to set them explicitly.
