# 0006: A structural profile relevance gate, amending — not reversing — ADR-0005's ban on pre-judgement filtering

## Status

Accepted (2026-08-03). **Amends [ADR 0005](0005-agentic-search-contact-sourcing.md)**:
0005 forbids reintroducing the old heuristic profile matcher
(`people_search._title_matches`, `search_source._mentions_company`) as
pre-judgement filtering. This ADR adds ONE structural axis (company identity)
that may filter before the agent reads a result, and explains in detail why
that is not the same failure class 0005 removed. Everything else 0005
established — the agent's tier/domain judgement, the evidence trail, the
trial gate — is unchanged.

## Context

### Where this came from

A 50-company pilot (2026-08-03) surfaced two problems in the same session.
The first — a code-unenforced search budget — is [tracked and fixed
separately](../../.claude/skills/find-contacts/references/search-tactics.md);
this ADR is about the second: **almost all of the agent's manual reading
time was spent on a mechanical task, not a judgement call.** Real examples
from that single session:

- A "Weave" search surfaced Bridgeweave Ltd., Powerweave, DataWeave, and
  I-WEAVE SOLUTIONS — five unrelated companies whose names merely contain
  "weave" as a substring.
- "Samarth Bhatia — International Travel Consultant at Prosperity Travels"
  surfaced against a Trail Blazer Consulting search purely because a past
  role there appeared deep in his snippet.
- "Himanshu Tyagi... Ex-PwC, Dunnhumby, Capgemini" surfaced against a
  Capgemini search; his structured, current employer is NexGen Analytix.
- A "Madan Kumar" / Zensar search returned nine different people named Madan
  Kumar, none tied to Zensar by the search engine's own matching.

None of these require judgement about the person's *seniority* or *domain
fit* — they are answerable from the search result's own structure, before
any reading of what the person actually does. The user asked for this
mechanical layer to move into code, and separately asked whether semantic
(NLP-style) matching should replace the token/keyword approach the jobs
pipeline uses. This ADR is the answer to both, and they turn out to require
opposite techniques.

### Why semantic similarity would make ADR-0005's exact failure worse, not better

ADR 0005's central complaint (quoted there): the old heuristics
"demonstrably accepted a person at one bank for a search against a
*different* bank... because a single shared word passed the check" —
`{"icici","bank"}` matched "Lloyds Banking Group" because both contain
"bank". A semantic/embedding approach does not fix this; it makes the same
mistake with more confidence. "ICICI Bank" and "ICICI Securities" score
*highly* similar under any reasonable embedding — they genuinely are
semantically close. They are also, provably, different employers. Sister
entities and unrelated companies with related names are exactly the region
where semantic similarity and business-identity correctness diverge, and
this project has hit that region live more than once: Capgemini vs.
Capgemini Invent, Binance vs. Binance.US, Datakrew vs. DataKrew Pvt. Ltd.
(a genuine duplicate company row, unrelated to this ADR but the same
underlying hazard). A gate that lets similarity score *reject* anyone
reintroduces ADR-0005's failure with a more persuasive-looking justification
attached.

## Decision

Split contact-relevance checking into four axes, and draw one hard line:

| axis | technique | may this axis reject a candidate? |
|---|---|---|
| profile shape | structural (URL pattern) | yes |
| **company identity** | **exact/structural only, never scored** | **yes** |
| tier fit | semantic scoring, advisory | **never** |
| domain distance | semantic scoring, advisory | **never** |

**Company identity is structural, not semantic, and this is the crux of why
this ADR is compatible with 0005 rather than a reversal of it.** The check
is not "does this text resemble the target company" (a similarity score) —
it is "does the search result's own structured metadata name the target
company, its exact self-reported form, or a specific documented alias
pattern (a trailing legal suffix, a known corporate-family suffix like
'Invent'/'.US'/'Global Services')". That is the same kind of check
`bright_data_profiles`'s `/in/` URL-shape filter already does — a
structural fact about the result, verifiable without reading what the
person does — just applied to one more field. The ADR-0005 addendum already
draws exactly this "structural filtering only, judgement stays with the
agent" line for the URL check; this ADR extends the line, not moves it.

Ambiguous company matches (a prefix relationship that isn't confirmed
same-company, or no structured company field at all) are **never** silently
auto-accepted or auto-rejected. They pass the gate and carry a flag
(`sister_entity` / `no_signal`) for the agent to resolve — because the two
real ambiguous cases hit live were judged **opposite ways by a human**:
"Capgemini Invent" was kept (the group's real consulting arm), "Binance.US"
was dropped (a separately regulated legal entity). No string-shape rule can
make that call; only domain knowledge can, which is exactly the kind of
judgement ADR-0005 reserves for the agent.

Tier and domain scoring (`profile_relevance.TierScorer`) is real semantic
matching — `rapidfuzz` fuzzy-string scoring against a broad marker
vocabulary — and it is exactly where 0005's constraint bites hardest: it
can **never** drop a candidate. It exists only to reduce triage time by
ranking and annotating what the agent already sees, and the agent's own
tier judgement (SKILL.md step 4) remains the recorded decision. A tier
score can be visibly wrong without causing harm; a tier score that silently
removed a candidate would not be.

### Store-and-flag, not drop-at-ingest, for the ambiguous company case

The jobs relevance gate (ADR-0002) drops anything uncertain at ingest,
because jobs are cheap and plentiful and precision was the complaint.
Contacts are the opposite: each one costs a billed search call and possibly
a manual judgement pass, and the two ambiguous cases above were genuinely
contested. So `profile_relevance.filter_relevant` returns a 4-tuple, not the
jobs gate's 3-tuple — the extra `flags` element carries kept-but-ambiguous
candidates forward instead of discarding them. This is a deliberate,
named divergence from the ADR-0002 pattern this module otherwise mirrors
exactly (one predicate per axis, ordered `AXIS_CHECKS`, first-failure
attribution via `first_failing_axis`).

### Implementation notes that turned out to be load-bearing

Two real parsing bugs were caught and fixed before this gate could be
trusted, both discovered by running it against real, not synthetic, Bright
Data output:

- **Bright Data glues the structured company field directly onto following
  prose with no separator** — "CapgeminiCurrently working in Capgemini as
  Data Science Manager". A camelCase-style boundary-recovery pass
  (`_CASE_BOUNDARY_RE`) is required before word-splitting, or the entire
  run-on blob reads as one non-matching token.
- **Bright Data returns more than one snippet shape.** Most profiles read
  `Location·Title·Company...`; some read `Title at Company·
  Experience: Company · Location...` with no location segment at all.
  Trusting a fixed segment position mis-parsed three real Highbrow
  Technologies employees as wrong-company. The gate now scans every
  structural marker the snippet offers (`_candidate_company_segments`) —
  each `·`-segment, text after `' at '` in segment 0 only (restricting this
  to segment 0 specifically was itself a fix: extracting it from every
  segment let an unrelated past-employer mention deep in free text
  masquerade as the current company), and text after an explicit
  `'Experience:'` label anywhere.
- **Trailing legal-entity suffixes ("Inc", "Ltd") are boilerplate, not a
  sister-entity signal** — "Highbrow Technology **Inc**" must read as the
  same company as "Highbrow Technologies", not a flagged ambiguity.
- **Simple pluralization needs tolerance without becoming fuzzy matching** —
  a profile's own self-reported "Highbrow Technolog**y**" vs. the DB's
  "Highbrow Technolog**ies**" is the same company; a narrow singular/plural
  stem comparison (`_singularize`) handles exactly this without opening the
  door to general similarity matching.

## Verification

Three layers, mirroring the jobs gate's synthetic-suite / standing-audit
split (`scripts/test_relevance_gate.py`, `scripts/audit_leak_rate.py`):

1. **Synthetic + real-raw-snippet cases** (`tests/test_profile_gate.py`) —
   every real adjudicated case from the 2026-08-03 pilot, copied from actual
   captured Bright Data output, with the human verdict as the assertion:
   Bridgeweave/DataWeave/Powerweave/I-WEAVE (reject), Prosperity Travels /
   NexGen Analytix (reject, former-employer trap), Capgemini Invent /
   Binance.US (keep + flag, not auto-decided), nine-Madan-Kumars-style
   no-structured-field cases (keep + flag).
2. **Backtest over every real contact this project has ever stored**
   (`tests/test_profile_gate_backtest.py`, parsing all `contact_batches/
   batch-*.md` evidence files — 438 contacts at the time this ADR was
   written) — asserts the gate auto-rejects **none** of them. This is the
   leak-rate-audit analogue: a false-reject here is a blocking bug, exactly
   ADR-0005's failure class recreated.
3. **Structural guarantee, not just a docstring claim** — a test asserts
   `"tier"` and `"domain"` never appear in `AXIS_CHECKS`, so the "advisory
   only" boundary can't silently erode as the module is edited later.

## Consequences

- The manual reading burden for the mechanical half of judging a batch
  (wrong-company noise, former-employer traps) is substantially reduced;
  the CLI's `search` subcommand now prints kept/dropped/flagged sections
  instead of one flat list, with dropped rows always visible and never
  silently vanished (`--raw` bypasses the gate entirely for an unfiltered
  view, since judgement must remain overridable per ADR-0005).
- A new maintenance surface exists: `app/config/contact_matching.py`'s
  vocabularies (tier markers, demotion markers, sister-entity suffixes) need
  the same kind of upkeep `categories.py`'s markers already get, and will
  drift the same way if a company's real title/name variants aren't fed
  back in.
- `rapidfuzz` (already a dependency, used by `scripts/dedup_jobs.py`) is the
  first tier-scoring implementation; no new dependency was added.
  Deliberately NOT sentence-transformers/embeddings — that would pull
  `torch`, this project's first ~1GB dependency, for a component whose
  output can only ever be advisory. `TierScorer` is a swappable protocol
  specifically so an embedding backend can replace this later without
  touching a caller, if the vocabulary approach demonstrably falls short.
- The two known-hard cases (Capgemini Invent, Binance.US) remain
  unresolved by design — they will keep surfacing as `sister_entity` flags
  requiring a human/agent call, not a growing false-accept or false-reject
  rate. That is the intended outcome, not a gap to close later.
