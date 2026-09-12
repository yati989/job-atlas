# Recipient pairing and the five tier shapes

## Why the pairing rule exists

A cold email needs the *right* evidence, not just *some* evidence. The
company might have 80 open reqs across every function you don't work in and
one that matches — mailing a data-analytics lead a resume tailored to a
credit-risk posting is not "close enough," it reads as not having read their
own team's remit. Confirmed at scale on this DB: among companies with an
emailed contact, individual companies carry 82, 46, 44, 34 and 27 distinct
job titles. This is the normal case, not an edge case.

The rule (`app/outreach/pairing.py:pair`):

1. **No company** (a prospect, or a company we don't scrape jobs from) →
   master. There is no job to be evidence for at all.
2. **No jobs at the company** → master.
3. **Recipient is `talent_acquisition`** → candidates are every enriched job
   at the company, any function. Recruiters route across every req — see
   the recruiter section below for how to actually pick one.
4. **Anyone else** → candidates are restricted to jobs in the **recipient's
   own function**, derived from their title via `classify_title()`
   (`app/config/categories.py`) mapped through `CATEGORY_TO_SEARCH_GROUP`
   (`data_ai` or `credit_risk`). No job in their function, or an
   unclassifiable title → master.

`pair()` refuses to return a mismatched `(resume_kind, reason)` pair —
`PairingResult.__post_init__` raises if you try. If `app.outreach.cli draft`
refuses your `--resume-kind`/`--pairing-reason` combination, that means the
pairing step's own output was ignored; re-run `pair` and use exactly what it
printed.

## The five recipient shapes

Every stored contact carries a `seniority_tier`. A manually-supplied
recipient (email typed in by the user, no stored Contact) may not — treat a
missing/unrecognized tier as `unknown` and use the most conservative shape
(closest to `ic`: ask, don't presume a relationship).

### `head` — function owner

They own the whole data/ML/risk org. They respond to a **specific problem**,
not "do you have openings." Lead with a real signal you can evidence (a
`pain_points` entry, a gap visible in their own job postings' seniority mix,
a hook) and connect it to one concrete thing you've done. Ask is soft: "if
that's a live problem for your team, I'd welcome a conversation" — not
"please forward my resume."

### `hiring_manager` — reports to the head, runs day-to-day hiring

Same problem-first instinct as `head`, but the ask can be more direct: this
person plausibly has open capacity now, even without a public posting. Name
the function ("your data science team") and ask plainly whether they're
looking for someone at your level.

### `ic` — individual contributor, same function, no hiring authority

Not a hiring conversation — it's a **referral request**. The ask is real,
but **do not state outright that they lack hiring authority** ("I imagine
you're not the one making hiring calls" was tried and the user rejected the
phrasing as too blunt, 2026-08-06) — imply it through the framing instead.
Resolved skeleton (only these two slots differ from the base
`hiring_manager`/`head` template in `email-template.md`; everything else —
relevant-experience sentence, pain-points line, resume line, sign-off — is
identical):

- **Opening ask**, peer-to-peer instead of "reaching out for opportunities":
  *"I've been looking at the [domain] team's work at [Company] and wanted to
  reach out directly rather than blind-apply — I noticed one open role that
  looked relevant: [link]"*
- **Closing ask**, folded into the pain-points sentence in place of "...and
  I'd be happy to contribute with my skill set if given the opportunity"
  (which wrongly implies the IC can grant the role): *"...if your team has
  room, or you know who's the right person for me to talk to, I'd really
  appreciate a pointer."*

Overselling yourself to an IC as though they can offer you a job reads as
not understanding org structure — this skeleton avoids that without saying
it out loud.

### `talent_acquisition` — recruiter, no function of their own

The only tier whose candidate pool spans every function (see pairing rule
#3). **Pick the single best-matching job to the candidate's own profile**
from the candidates `pair()` returned — same judgement as `/tailor-resumes`'
scoring, just choosing among several jobs instead of scoring one, and prefer
genuine skill overlap over a bare title-keyword match (e.g. a Python/SQL
data-engineer req over a GenAI-heavy one, even if both carry "Data
Engineer" in the title).

Resolved skeleton (2026-08-06): **only the opening-ask slot changes** from
the base template — name the job explicitly instead of "one of them I
spotted here": *"I'm reaching out about the [Job Title] opening at
[Company] — [link]"* — a recruiter juggling many reqs should not have to
guess which one you mean. Everything else, including the closing "...happy
to contribute with my skill set if given the opportunity" line, stays
as-is: unlike an IC, a recruiter genuinely can move a candidate forward.

When pairing returns `resume_kind=master`, there is no req to name. Use the
TA master fallback in `email-template.md` for every recipient tier: a generic
data-science-and-machine-learning opportunities opening, no link, and no
JD-derived pain-points paragraph. This is a copy-shape fallback only and does
not alter the stored recipient tier.

### `exec_fallback` — founder/CEO, caught only when no functional lead exists

Treat like `head` but shorter and more deferential — they almost certainly
have no time for a long pitch. One sentence on why you're reaching out, one
sentence of evidence, a soft ask. If a hook exists (funding news, a launch),
this is the tier where it earns the most attention.

## `unknown` — no tier information at all

This happens for a manually-supplied recipient with no stored Contact row.
Default to the `ic` shape (softest ask, most deferential) unless the user
supplies more context (a title that clearly reads as senior — classify it
yourself and use judgement, don't just default blindly if the title says
"VP" or "Head of").
