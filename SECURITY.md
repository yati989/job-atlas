# Security policy

## Reporting a vulnerability

Do not include credentials, resumes, contact details, private job-search data,
or provider payloads in a public issue. Use GitHub's private vulnerability
reporting feature when it is available for this repository.

Until a public security contact is configured, keep the report private and do
not publish proof-of-concept data that identifies a candidate or another
person.

## Sensitive local data

The application is local-first. Keep `.env`, OAuth tokens, browser profiles,
private configuration, resumes, logs, exports, and provider evidence outside
version control. Run `python -m scripts.audit_public_release` before preparing
a release commit.
