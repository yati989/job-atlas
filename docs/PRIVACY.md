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
emails. Its profile-discovery stage stores only LinkedIn links and minimal
professional evidence.

`draft-outreach` is a separate, explicitly invoked workflow. It can use an
address supplied by the user or, when given an existing company identity,
invoke standalone contact and email discovery. It stores its contact and draft
records in the configured private database and can create reviewable Gmail Drafts;
it never sends messages. This outreach data is separate from public
profile-discovery data.

Users are responsible for complying with job-board terms, privacy law, and
applicable communication rules. Generated resumes and drafts require human
review before use.
