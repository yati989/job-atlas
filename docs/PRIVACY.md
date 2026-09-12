# Privacy model

`job-atlas` runs locally and stores job-search results in a private SQLite
database by default. Advanced users can configure PostgreSQL instead.
Candidate configuration and resume data are private inputs, not repository
content.

The guided pipeline may store public job and company evidence. Optional
LinkedIn profile discovery stores only the canonical profile URL and the
minimal professional company, role, query, and relevance evidence needed for
review. It must redact email addresses found incidentally in provider payloads
before persistence, logging, display, or export.

The public pipeline does not discover, derive, verify, store, or send contact
emails. The separate `draft-outreach` skill accepts an address supplied by the
user and creates a local reviewable draft only. That address remains confined
to the private draft artifact and is not profile-discovery data.

Users are responsible for complying with job-board terms, privacy law, and
applicable communication rules. Generated resumes and drafts require human
review before use.
