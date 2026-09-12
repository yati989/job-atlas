---
name: enrich-company-domains
description: Compatibility alias for company-domain requests. Use when asked to resolve company domains; delegate to enrich-companies, which now owns domain, company facts, pain points, employer/staffing type, and data_ai/credit_risk classification together.
---

# Enrich Company Domains

This workflow has moved to `../enrich-companies/SKILL.md` so company research
is performed once at the correct seam. Read and follow that skill completely.

When the request is domain-only, still record or refresh company type and
contact search groups from the same evidence. Preserve already-current company
facts and pain points; do not redo them merely to satisfy the compatibility
invocation.
