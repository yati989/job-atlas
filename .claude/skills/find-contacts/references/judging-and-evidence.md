# Judging and evidence — the bar for storing a contact, and name collisions

## What the gate already decided, and what's still yours (ADR-0006)

Step 3b's output is gated, not raw. Before this section applies, understand
the split: the gate's `wrong_company`/`former` drops and its
`sister_entity`/`no_signal` flags are **structural** — a different company
named outright, an explicit past-tense marker, or simply no parseable
company field. None of that requires reading what the person *does*, so the
gate does it. Everything below — is the seniority right, is the domain
close enough, is this really the same person, does a flagged sister-entity
belong here — is semantic and stays entirely yours. A `[FLAG:sister_entity]`
or `[FLAG:no_signal]` row is not pre-approved; it's exactly as unverified as
an ungated row would have been, just marked so you don't have to re-derive
why it's ambiguous. Tier suggestions (`[tier~head:87]`) are equally
non-binding — read the actual title, not the number.

## The evidence bar: snippet text is all you get

**You cannot open profiles to check.** `WebFetch` on a profile URL returns
**HTTP 999** (bot block), confirmed in the #75 trial. Whatever the search
snippet says is the entire evidence base for every decision.

**A profile appearing in a company-scoped search is NOT evidence that the
person works there.** Search engines match loosely. Trial example: a
`"Madan Kumar" Zensar "Data Engineering" Director` search returned **nine
different Madan Kumars**, and never tied the candidate profile to Zensar. He
was dropped — correctly.

So store a contact only when the snippet itself carries **both**:

- the **company** (in the headline, the "X - Company | LinkedIn" title, or
  stated in the snippet body), **and**
- a **title** specific enough to tier.

Snippets usually give you one or the other. Expect to drop a lot. A
follow-up confirmation search is usually *not* worth it — it was tried in
the trial and failed to resolve the ambiguity while costing a full query
against the budget.

This is the main throughput constraint of the whole approach: budget roughly
**2-3 searches per confidently storable contact**, and expect yield to vary
by company depending on how its people write their headlines — not by how
good the company is. A company where snippets don't cooperate is `partial`;
that is a normal outcome, not a failure.

Record for each kept contact: profile URL, the exact text you judged, the
tier and **why**, and the query that surfaced them.

"Exact text" is literal: copy the returned title/snippet without rewriting
it into a stronger claim. The same applies to identity â€” store the exact
LinkedIn `/in/` URL, including its numeric suffix. A paraphrased snippet or
shortened/invented profile slug destroys the audit trail and is grounds to
reject the row during review. For an `ESCALATE`, `no_signal`, or
`sister_entity` result, no preserved verbatim evidence means no contact.

Be careful with **sister entities** ("ICICI Bank" vs "ICICI Securities") and
with a founder of an *unrelated* side company — both are documented past
false accepts. When genuinely unsure, **drop the candidate**. A thin,
trustworthy batch is the goal; the trial is graded on precision.

### Four "works here" traps the gate cannot see (2026-08-03/04)

The company-identity gate is structural: it compares name strings. These four
shapes all pass it while being the wrong person, and only judgement catches
them. All four were hit live across ~30 companies, so expect them.

1. **A credential is not an employer.** Every candidate on a Udacity search
   listed Udacity as a *Nanodegree they hold* — a BlackRock director, a Google
   lead, a dozen others. Same shape for any training/certification brand
   (Coursera, upGrad, Simplilearn) and for consultancies people list as
   alumni. Ask: is this company in their *Experience*, or their *Education*?
2. **An agency-embedded recruiter is not an employee.** Medtronic's search
   returned six recruiters whose snippets read "via AMS" / "RPO" — they staff
   *for* Medtronic while employed by the agency. Storing them is actively
   wrong, not merely low-value: the derived `@medtronic.com` address would
   send a stranger's inbox a mail meant for the client. Prefer a direct
   employee whenever one exists; drop the RPO/AMS ones.
3. **A subsidiary or same-name unrelated firm.** "Medtronic Labs" is a
   separate social-enterprise from Medtronic plc; "Samsara Group" is a Mumbai
   shipping firm unrelated to Samsara Inc. (NYSE: IOT); "Binance.US" is a
   separately regulated entity. The gate flags some of these `sister_entity`
   and — before the 2026-08-04 fix — silently passed others. A flag is a
   prompt to decide, and *no* flag is not proof of sameness.
4. **The target company appearing in a past role.** The structured
   `Location·Title·Company` field is authoritative for *current* employer.
   When it names someone else and the target only appears deeper in the
   experience list, they have left — Naresh Sharma (current: NPCI, Devoir
   earlier) and Smitha R. (current: Rapid7, CrowdStrike earlier) both read as
   plausible hits until that field was checked. Note the inverse too: an
   `Ex-` marker *after* the target ("Data and AI at Samsara | Ex - HP Inc")
   describes the later-named company, not the target.

### Right company, wrong function

Company identity and *functional relevance* are separate checks, and the gate
only does the first. These all passed company-identity cleanly and were still
correctly dropped: a Marketing Director at Prescience, a VP of India
Development Center whose field is materials science at Danaher, a Head of
Facilities & IT at Quantiphi, a Head of Sales at Devoir, QA/threat-research
engineers at CrowdStrike. A high advisory tier score (`[tier~head:100]`) is
computed from title *shape*, not domain — it will happily rank a marketing
director as a head. Read the function, not the score.

## Seniority is strict. Domain is elastic. Don't confuse the two.

These are two different axes, and the 2026-08-02 review caught an error on
one while a subsequent over-correction destroyed recall on the other. Judge
them separately.

### Seniority: strict, never stretch

The tier is a **seniority** claim — does this person own the function
(`head`), run a team you'd report to (`hiring_manager`), or would they be
your peer (`ic`)? Getting this wrong is what the human review rejected:

| stored as | actual title | why it was wrong |
|---|---|---|
| `head` | Global Program Manager - Data, Analytics and AI | a program/delivery role, not the owner of the function |
| `head` | Business Unit Data Manager | a BU data manager is not a function head |

Both were rationalised as "closest function-owner signal available" and
"owns data for a business unit" — reasoning that sounds defensible in
isolation but was really driven by an empty quota and a nearby-sounding
title. **The tell:** a rationale containing *"closest available"*, *"no
exact title exists"*, or *"plausibly owns"* is not a rationale, it is an
admission the seniority does not match. Drop it, and leave the tier short —
`partial` is a normal, expected status.

### Domain: elastic, and being strict here destroys value

**The over-correction, also confirmed live.** After the above, judging
tightened on *domain* too, and started discarding genuinely useful people: a
Fraud Risk manager at Optum, an O2C/collections manager at HCLTech, a
receivables manager — all rejected for not being "credit risk" narrowly
enough. That is the wrong call.

Adjacent risk and data disciplines are **worth storing** when the seniority
is right:

- fraud risk, operational risk, enterprise risk, market risk, underwriting,
  collections/O2C, credit ops — all live in the `credit_risk` neighbourhood
- analytics, BI, data engineering, data governance, ML platform — all live
  in the `data_ai` neighbourhood

A manager in an adjacent discipline is a real, reachable person at the
target company who plausibly hires for, or can route you to, the role you
want. The cost of storing them is one line in a review file; the cost of
dropping them is a contact you never get back — and yield is the scarce
resource here, not candidates to reject.

**So: store them, and say what they are.** Put the domain distance in the
tier rationale ("fraud risk rather than credit risk modelling") so the human
reviewer sees exactly what they're getting and can decide. That is what the
evidence file is *for*. What must never be fudged is the seniority claim,
because that is the part the reviewer cannot easily re-check.

## Name collisions: a separate risk from a wrong tier

While judging, also note **how many other profiles with this exact full
name** turned up matching this company in your searches — this is
information you already have from the evidence-bar check, just not
currently kept. Set `name_collision_risk=True` (with a one-line
`collision_note`) whenever more than one did.

This matters specifically because of how the derived email will later be
used. A wrong pattern guess **bounces** — self-correcting, no harm, the
mistake is visible. A same-name collision does not: the guessed address
still delivers, just to a different person who happens to share this
contact's name, and nothing about that failure is ever visible after the
fact. It's confirmed to actually happen here — one of the leaked addresses
found for pattern confirmation was `alex2.morgan@example.com`, the `2`
being the example system's way of disambiguating two people with the same name. Any company
large enough to need that suffix is a company where a guessed address for a
common name is a live risk, not a theoretical one.

Outreach for a collision-flagged contact should go through the **LinkedIn
profile, not the guessed email** — the profile is the one unambiguous
identifier, where the email is a guess against a name that isn't unique at
that company.

## A cheaper substitute exists, but flag it as such

A full per-contact disambiguation search (one query per contact, checking
for other same-name profiles) is the rigorous version of this check. A
name-commonality/company-size **heuristic** (common Indian surname, or a
600k+-employee company where even a moderately distinctive name carries
real collision risk) is a legitimate lower-cost substitute when re-running
the full search isn't practical — but record it as a heuristic flag,
distinct from a search-verified one, so a later review doesn't treat the
two as equally confident.
