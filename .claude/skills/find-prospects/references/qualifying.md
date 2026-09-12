# Stage B: qualifying a candidate

Stage A got you a pile of names. Stage B decides which of them are actually
worth a cold email — and it is the part that costs your session time, so the
budget is capped at **3 searches per candidate** (plus one uncharged
`WebFetch`).

You are answering two questions, in this order. The first can reject; the
second can reject. Nothing else can.

## Gate 1: is there an in-scope function here?

**A company qualifies only on evidence of a data / ML / analytics / credit
risk function.**

This is the hard gate, and the reason is downstream: a cold email needs *a
person to send it to*. If nobody at this company does data work, then
`find-contacts` will find nobody, no email gets written, and every minute
spent here was wasted. Industry fit does not rescue a company with no data
function — a perfectly on-thesis 40-person logistics startup with three
engineers and no analyst is not a prospect.

### What counts as evidence

In rough order of strength:

1. **A named in-scope person at the company** — a LinkedIn result whose
   *title* carries both a data-ish role and this company. Strongest, because
   it is also the eventual contact.
2. **An archived or expired in-scope job posting** — proves they staff the
   function, and an expired posting is *ideal*: the need was real and the
   posting is gone, which is exactly the cold-email window.
3. **A public engineering or data blog** with in-scope content authored by
   the company.
4. **A careers page listing a data/ML team** even with no current opening.

### What does not count

- The company's marketing copy saying it is "AI-powered" or "data-driven".
  Nearly every fintech says this; it is a claim about the product, not
  evidence of a team.
- "Data" or "AI" appearing in the company's own name.
- A recruiter or a talent-acquisition person at the company. They prove the
  company hires, not that it hires *in scope*.
- The search summarizer's prose (see `harvesting.md` — it has been measured
  fabricating exactly this kind of attribution).

Record the evidence **verbatim** in `--function-evidence`. `batch.qualify()`
refuses a row without it, because the trial-gate reviewer's whole job is
re-checking that text against the live web, and a paraphrase makes that
impossible.

Failing this gate → `--reason no_function`.

## Gate 2: is there a positive India signal?

Remote **widens** this gate rather than narrowing it. A fully-remote company
with zero India office qualifies, and so does a company with an India office
in **any** city — Chennai and Hyderabad count exactly as much as Bengaluru.

### This gate is deliberately looser than `location_ok()` — do not "fix" it

The job relevance gate (`app/pipeline/relevance.py:118`) reads *"Onsite/hybrid
must be Bangalore; remote must not be foreign-only"*, and will drop a
hybrid Chennai posting as `no_signal`. **This gate does not follow it, and
that divergence is intentional** (confirmed by the user at the first trial
gate, 2026-08-05, after it surfaced Rocket Companies in Chennai and Wise in
Hyderabad).

The two gates answer different questions:

- `location_ok()` filters **live postings you would apply to**. A hybrid
  Chennai role is useless to someone in Bangalore, so it is dropped.
- This gate filters **companies worth starting a conversation with**. Cold
  outreach is not an application: the opening is "do you have anything
  remote, or in Bangalore?", and a company with an India hub is a legitimate
  person to ask — whatever is or isn't posted today.

Prospect discovery is explicitly not restricted to companies currently
hiring, so importing a *currently-postable* location rule would defeat its
purpose. An earlier version of this file called the divergence an
"inconsistency"; it is not.

But "we're remote" very often means US-timezone-only or contractor-only in
practice. So:

> **A company qualifies only on a *positive* India signal. We never try to
> prove a company doesn't hire in India.**

Positive signals:

- An employee visibly located in India on LinkedIn.
- A careers page, handbook or hiring page naming India or APAC.
- An India office address.
- A past in-scope posting that was India-located or India-eligible.

Absence of any of these is a `no_india_signal` disqualification, not an
invitation to go hunting for a negative. Proving a negative is unbounded work
and this gate is capped at three searches.

Record it verbatim in `--india-signal`.

## Ranking bands

Band is assigned at qualification and drives review order. Industry never
gates — it only ranks.

| band | shape | why |
|---|---|---|
| **1** | Remote-first **and** matching industry (fintech / lending / neobank / BNPL / credit bureau / insurtech / logistics) | Strongest possible pitch: domain story from the resume master *and* no location friction |
| **2** | Bengaluru-based in a matching industry, **or** remote-first in any industry with a real data org | One of the two advantages, not both |
| **3** | India-present, other industry, has a data org | Function fits; the email has to lean on ML engineering rather than credit risk |

"Matching industry" means matching the candidate's actual history in
`app/resume/master.yaml` — credit risk and ML at Kotak811 (digital banking),
Navi (lending), Delhivery (logistics). Not "finance" broadly.

## Staffing firms are out

A staffing or recruitment firm gets `--reason staffing`, even when it clearly
employs data people.

They fail Gate 1 by construction: their data practitioners are *placed at
clients*, so the company has no in-house function to join, and a cold email
about a data role lands with a recruiter rather than a hiring team.
`find-contacts` already has a separate staffing ladder for the ones that
reach the DB through job postings — this is not lost coverage, it is the
right pipeline.

Tell-tales: "staffing", "recruitment", "talent solutions", "manpower",
"consultancy services", "RPO", or a careers page listing roles at *other*
companies.

## Budget discipline

Three searches is enough for a binary yes/no. It is not enough to build a
roster, and it is not meant to be.

A workable shape per candidate:

1. One search establishing the function (`"data scientist" at <company>`, or
   `<company> engineering team`).
2. One `WebFetch` of the careers/about page — **uncharged**, and usually the
   single most informative action available. Use it early.
3. One or two searches resolving whatever is still ambiguous — usually the
   India signal.

When `spend` exits 2, the budget is gone: disqualify `cap_reached` and move
on. A `cap_reached` row is not a verdict that the company is bad, and it is
kept precisely so it can be revisited when you have more time or a better
angle.

If you find yourself wanting a fourth search, that is the signal to stop, not
to negotiate. The cap exists because `find-contacts`' per-company spend
eroded from 3.1 to 2.0 calls while its budget lived only in prose.
