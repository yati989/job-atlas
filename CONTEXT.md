# CONTEXT — Domain Glossary

The ubiquitous language for this project. Definitions only — no implementation
details. When code or docs name one of these concepts, use the term as defined
here.

## Job-ingestion relevance

- **Relevant job** — a job posting that passes the accepted search profile's
  hard-reject, role, seniority, experience, and location policy. Relevant jobs
  enter canonical job storage; rejected and Needs review observations retain
  separate run evidence.

- **Relevance gate** — the single, central check every connector observation
  passes through, producing *kept*, *rejected*, or *Needs review*. Its policy
  comes from the immutable accepted search profile, not from a connector.

- **In-scope role** — a job whose title is supported by the accepted profile's
  profession and relevance vocabulary and does not hit its explicit role
  exclusions. Description-only keyword overlap does not establish title fit.

- **Geography-eligible** — an India-eligible location compatible with the
  accepted Indian cities and work arrangements. Unknown evidence is Needs
  review unless the profile explicitly accepts a compatibility fallback.

- **Onsite** — a job requiring physical presence at a selected city. **Hybrid**
  also requires selected-city compatibility, while **remote** uses country or
  regional eligibility evidence.

- **Collection window** — the requested posting-age horizon supplied to
  sources that support it. Posting dates and source limitations are retained;
  the window is not silently reapplied as a relevance axis.

- **Seniority preference** — the title-level career bands accepted by the
  search profile. It is distinct from explicit years-of-experience evidence.

- **Search vocabulary** — the accepted profile's fetch-side terms used by
  sources that support native search. It is intentionally distinct from the
  broader relevance vocabulary.

- **Relevance vocabulary** — the accepted profile's gate-side title terms,
  broad enough to recognize valid title variants without increasing native
  source-query workload.

- **Leak** — a stored job that should not have passed the relevance gate (wrong
  role, wrong location, out of band). The leak rate is the quality metric the
  ingestion pipeline is audited against.

- **Relevance audit run** — an explicitly requested collection that preserves
  every raw job occurrence rejected by the relevance gate, including duplicate
  sightings from separate search instances. It is evidence for evaluating the
  gate and does not make a rejected posting a stored **job**.

- **Relevance audit observation** — one rejected raw occurrence in a relevance
  audit run, with its connector/search provenance, normalized posting evidence,
  and first failing axis.

## Guided public workflow

- **Phase A evidence** — reusable pre-selection analysis. Job evidence is bound
  to one immutable posting version; company evidence establishes canonical
  identity and the profession-specific function groups needed downstream.

- **Selection preview** — a reviewable all-eligible, manual, or saved-filter
  result. It can show retained, excluded, and unresolved jobs without deleting
  any collected outcome.

- **Frozen selection** — an immutable posting-version/company shortlist used by
  downstream work. It may be created only after every included job and company
  has completed Phase A. A later choice creates a new revision.

- **Workflow stage status** — durable progress for an optional stage:
  *not_requested*, *awaiting_confirmation*, *running*, *partial*, *failed*,
  *completed*, or *skipped*.

## Contact-finding

- **Company** — an employer we have seen at least one scraped job posting from.
  The row exists *because* a posting created it, so "in the companies table"
  means "our connectors surfaced a job here", never merely "we know of them".
  A company we only know about through research is a **prospect**, in a
  separate table (ADR-0008).

- **Contact** — a person at a target company worth reaching out to, stored with
  their name, title, LinkedIn URL, **seniority tier**, guessed email, and email
  **verification status**. Deduplicated per company by LinkedIn URL.

- **Profile link** — a relevant person's canonical LinkedIn URL plus minimal
  professional relevance evidence. It is not a **contact** and contains no
  discovered, guessed, or incidentally observed email address.

- **Profile discovery** — the links-only use of the established people-search
  plan and judgement rules for an approved company scope. It ends at
  **profile links** and never crosses into contact email resolution or outreach.

- **Role ladder** — the ordered set of **seniority tiers** searched for a
  company, derived from that company's own job-posting categories: *head* →
  *hiring_manager* → *ic* → *talent_acquisition* → *exec_fallback* (a
  founder/CEO caught only when no functional lead is found). "Tier" is the
  rung a contact was matched at. *head* (function owner) was split out of what
  was previously a single *hiring_manager* tier (#68) — the function owner and
  the person you'd actually report to are different outreach targets.

- **Search group** — a validated lowercase function slug that bundles related
  role families into one query set. Existing specialized groups include
  `data_ai` and `credit_risk`; public profiles may derive another group such as
  `product_design` from the frozen profession vocabulary.

- **Sourcing** — how contacts are discovered. The supported path is
  **agentic public search**: a billed Bright Data SERP passthrough returns
  candidate profiles, and the agent's own judgement (not string matching)
  decides who's real and which tier they occupy (ADR-0005). No logged-in
  LinkedIn people-search path is part of the active product.

- **Search plan / coverage** — the code-generated, per-company worklist of
  every query the ladder authorises (`search_plan.plan_for_company`), and the
  subset of it not yet reflected in the billed-call ledger
  (`coverage_for_company`). Replaced a prose-only search budget after it was
  measured to erode silently under pace — see ADR-0005's addendum history and
  `search_plan.py`'s module docstring.

- **Profile relevance gate** — the structural pre-screen a search result
  passes through before the agent judges it: profile-URL shape and company
  identity (exact/structural only — see ADR-0006) may reject a candidate;
  tier-fit and domain-distance scoring are advisory and can never reject one.
  Distinct from the job-ingestion **relevance gate** above — same shared
  vocabulary ("axis", "first failing axis", drop-at-ingest vs. store-and-flag),
  different domain and different axis set; disambiguate by context or say
  "profile relevance gate" explicitly.

- **Email verification status** — a confidence signal on a guessed address, not
  a deliverability guarantee: *verified*, *catch_all* (domain accepts anything),
  *unverified*, or *invalid*. Used to rank which contacts to keep.

- **Contact enrichment status** — a company's contact-finding state: *pending*,
  *done*, *partial* (search budget exhausted, quota still short — a normal
  outcome, not a failure), *no_linkedin_match* (searched, no confident company
  match — flagged not guessed), or *no_category_match* (no job history to
  derive a ladder from).

## Prospect discovery

- **Prospect** — a company that research says is worth **cold-emailing** about
  an in-scope role, and that is *not* already a **company** (i.e. no connector
  ever scraped a job from it). Stored in its own table with no link to
  `companies` (ADR-0008); the two are compared by a join on normalized name
  when the question arises. Note what that join means: "already in our DB",
  **not** "has a live opening on its careers page".

- **Thesis** — one sector-shaped bet about where prospects live ("India lending
  & BNPL", "remote-first fintech"). Both the unit of work — one thesis per run
  — and the unit of human review, since companies within a thesis are
  comparable to each other in a way a mixed batch is not. The ordered list is
  checked in (`app/config/prospect_theses.py`) rather than invented per run, so
  progress survives the session.

- **Harvest / qualify** — the two stages of discovery. *Harvesting* mines
  directory and listing pages for candidate company names in bulk, at zero
  cost. *Qualifying* decides, per candidate, whether it passes the **function
  gate** and the India signal. Harvesting is deliberately indiscriminate;
  judgement lives entirely in qualifying.

- **Function gate** — the hard admission test for a prospect: evidence of an
  in-scope data / ML / analytics / credit-risk function at the company. Without
  one there is nobody to email, so everything downstream is wasted. Industry
  match never gates — it only ranks. Distinct from the job **relevance gate**
  and the **profile relevance gate**; disambiguate by saying "function gate".

- **Ranking band** — how strong a qualified prospect is, 1–3: *1* remote-first
  **and** in a matching industry, *2* one of those two, *3* function fits but
  neither. Assigned at qualification; drives review order, never admission.

## Resume tailoring (ATS Resume Builder)

- **Resume master** — the candidate's complete, true, canonical resume content
  (`app/resume/master.yaml`, validated by `ResumeMaster`). Authored/reviewed
  once; the single source of truth every tailored resume derives from. The
  original resume file is never re-parsed after this. Content only — no
  presentation.

- **Tailored resume** — one JD-specific rendering of the master: the same true
  content **reordered, re-emphasized, and reworded** to mirror a job's
  vocabulary. Never adds anything not grounded in the master. Both the base and
  a tailored resume share the `ResumeMaster` shape.

- **Tailored plan** — the per-job decision the agent makes about *which* true
  bullets/skills to surface and *how* to reword them; it renders to the tailored
  master.

- **ATS-safe** — a resume layout an Applicant Tracking System parses correctly:
  single-column, standard fonts, standard section headings, no
  icons/tables/text-boxes/header-footer content. Proven per-render by the
  **round-trip check** (`pdftotext` re-extraction confirms name, companies, and
  surfaced skills survive) — a document that only *looks* right but scrambles on
  extraction is not ATS-safe.

- **Match score** — an explainable, itemized percentage: the weighted coverage
  of a JD's requirements (must-haves weighted heavily) by *true* master content.
  Never rises by fabrication.

- **Match report** — the per-JD coverage accounting behind the score: which
  requirements are covered (and where evidenced) and which are missing, the
  missing ones split into the two gap kinds below.

- **Surfaceable gap** — a JD requirement whose supporting evidence *is* in the
  master but wasn't surfaced yet; closed automatically by tailoring.

- **Real gap** — a JD requirement genuinely absent from the candidate's
  background. Cannot be closed by editing the resume; reported honestly so the
  candidate can address it (cover letter, upskilling) or skip the role. "A
  perfect score" means actually acquiring the missing thing, never faking it.

## Outreach

- **Standalone draft workflow** — explicitly requested work for one recipient
  or a user-bounded batch that may resolve contacts, prepare truthful messages,
  and create Gmail Drafts. It is outside the guided full pipeline and never
  sends a message automatically.

- **Decision run** — an immutable, ranked snapshot of active canonical
  postings in one explicit posting-time window, awaiting a named approval.
- **Posting version** — one immutable material episode of a connector job;
  applications, screening facts, and decision snapshots attach to it.
- **Effective salary** — explicit reliable guaranteed cash when available,
  otherwise the company AmbitionBox estimate, otherwise unknown.
- **Qualified WLB** — a Glassdoor work-life rating whose review count meets
  the configured minimum; an unqualified rating is unknown, not a rejection.
- **Approval artifact** — the immutable ordered company and posting-version
  manifest produced from a named decision run and its group cutoffs.
- **Initial outreach** — a first cold-email message, distinct from a follow-up.
- **Successful initial contact** — an initial message presumed delivered or
  replied to; a late bounce reverses it.
- **Calibration contact** — the one first recipient used to learn a company
  domain's email pattern before the rest of that company's drafts are released.
- **Verified domain pattern** — a domain-address shape supported by a reply or
  non-bounced delivery, with confidence retained in the delivery ledger.

- **Outreach draft** — one prepared cold email to one **recipient**, asking
  about opportunities. Never sent by the system: it is pushed to Gmail Drafts
  for the candidate to read, edit and send by hand (ADR-0009). Deduplicated
  per recipient — re-running updates the existing draft rather than making a
  second one.

- **Recipient** — whoever the draft is addressed to. Deliberately *not* the
  same concept as **contact**: a recipient may be a stored contact, or just an
  address the candidate supplied for a **prospect** company. Outreach is the
  first concept here that doesn't care whether its target came from a scraped
  job or from research (ADR-0010).

- **Tailoring evidence** — the job a resume is tailored against for outreach
  purposes. Distinct from an application target: the candidate is not applying
  to that posting, so the posting's *recency doesn't matter* — a six-month-old
  req still evidences what the company staffs, on what stack, at what
  seniority. A company with no jobs at all yields no tailoring evidence, and
  the recipient gets the plain **resume master** instead.

- **Recipient pairing** — the rule that a resume must be tailored against a
  job in the *recipient's own* function, never merely the company's best job.
  A credit-risk-tailored resume mailed to an analytics lead is worse than a
  neutral one, so no job in the recipient's function means the master file.
  **Recruiters** (`talent_acquisition`) are the exception: they own no
  function and sit across every req, so they get the job that best matches the
  candidate, named explicitly in the email so the ask is unambiguous.

- **Hook** — a specific, current, verifiable fact about the company or
  recipient that opens the email. Only usable with a **first-party or named
  source** (company newsroom, official blog, the person's own post) plus the
  verbatim text it came from, both stored on the draft. Search-summary prose
  is never a source — it has been measured fabricating claims outright
  (see `find-prospects`' harvesting reference). No verifiable hook means the
  email falls back to evidence-bound personalisation, never an invented one.

- **Send surface** — where the candidate actually reviews and sends. Gmail
  Drafts, not the terminal. The database records what was drafted; after
  sending, the sync pulls back what was *actually sent*, since edits made in
  Gmail are the real message (ADR-0009).
