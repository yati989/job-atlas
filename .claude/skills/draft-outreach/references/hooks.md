# Hooks: sourcing rule, why it's hard, and the collision-risk block

## What a hook is

A specific, current, verifiable fact that opens the email with something
better than a generic "I'm reaching out about opportunities" — a funding
round, a product launch, the recipient's own recent post, a press mention.
It's optional. Its absence is not a defect in the email; an invented one is.

## The sourcing rule

**A hook is only usable with a first-party or named source, plus the
verbatim text it came from, both stored on the draft.**

First-party/named means: the company's own newsroom or blog, an official
press release, the recipient's own LinkedIn/blog post, or a bylined news
article you can point to. It does **not** mean a search engine's summary
paragraph.

**No verifiable hook within budget → write the evidence-bound version.
Never invent one to fill the slot.**

## Why this is a hard rule, not caution

This isn't hypothetical risk-aversion. In one `find-prospects` run (33
candidates, 2026-08-05), the search summarizer **fabricated three company
facts outright**:

- Claimed Moov "has offices in India, including Bengaluru, Chennai, Delhi,
  Hyderabad, Kolkata, Mumbai, and Pune." Moov's own careers page states
  verbatim: *"Our team spans the U.S."*
- Claimed Hiro Systems has locations "including Singapore and Bengaluru."
  Hiro's own careers page lists 13 locations, none in India.
- Conflated Snap! Mobile (a school-fundraising payments company) with Snap
  Inc./Snapchat, attributing Snapchat's real Mumbai office to the wrong
  company.

Those were caught because a company's own careers page could be checked
against the claim. **A hook about a person — "I saw your talk on X," "loved
your post about Y" — has no such page to check it against.** If it's
fabricated, wrong, or about a different person with the same name, there is
no cheap way to catch it before it's already been sent, and the failure mode
is worse than a wrong office claim: it reads as either a lie or as having
mixed the recipient up with someone else, in a message that exists
specifically to make a good first impression.

The defense that worked in that run, and applies here identically: **one
uncharged `WebFetch` of the primary source settles it.** Read the actual
page. Quote what it actually says. Store the URL.

## Search budget

Same shape as `find-prospects`: a hook is worth at most **2–3 searches +
fetches per recipient**. If nothing first-party turns up in that budget,
stop and write the evidence-bound email — do not keep digging for a hook
that isn't there. A cold email with no hook is a normal, fine email; a cold
email with a fabricated one is a landmine.

## Collision-risk flagging (ADR-0012 — amends the original block)

25 of 817 stored contacts carry `name_collision_risk = True` — the
`find-contacts` search turned up more than one LinkedIn profile with that
exact name at that company, so the guessed email (derived from the name
alone) might land in a stranger's inbox who happens to share the target's
name. `find-contacts`' own evidence file already tells the user: *"Contact
via the LinkedIn profile above, not this email."*

Every stored email is additionally `unverified` (ADR-0005 dropped SMTP
verification) — but a wrong-pattern guess just bounces. A name collision
**delivers successfully to the wrong human.** That's a different and worse
failure class — but as of ADR-0012, it is handled by *flagging*, not
blocking: `app.outreach.cli draft` writes the row with `collision_risk =
True` and `status=drafted` (same as any other draft), and `push`/
`push_drafts.py` push it exactly like any other row. The full-pipeline
run workbook (issue #82) surfaces `collision_risk` per contact so the
candidate can double-check before sending — the review moved from
"before the draft can be pushed" to "before the candidate hits send in
Gmail," which is where a human is already looking at the message anyway.
