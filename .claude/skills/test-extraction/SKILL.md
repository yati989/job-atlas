---
name: test-extraction
description: Pull a small sample of jobs with real description text, run enrichment extraction on them, and present results alongside their original source links so the user can manually verify quality before trusting a larger run
---

# Test Extraction

A quality-evaluation workflow, distinct from `/enrich-jobs` (which writes
enrichment results to the DB for real). This skill exists to answer "is
the extraction actually good?" on a small, inspectable sample — never
skip straight to a full-batch run on a new source or after a prompt/schema
change without doing this first.

## When to use

- Before running `/enrich-jobs` on a new job source for the first time
  (a connector whose `description_raw` scraping hasn't been validated).
- After changing the extraction prompt/schema/fields.
- After switching extraction method (e.g. external API model vs.
  in-session agent reasoning) to confirm quality didn't regress.
- Whenever the user explicitly asks to spot-check extraction quality.

## Steps

1. **Confirm description text is available.** Most connectors only
   scrape title/company/location/URL — NOT full description text (this
   was true for LinkedIn's production connector; description scraping
   required opening each job's detail page separately, which the base
   connector doesn't do). If the target source's `description_raw` is
   null in the DB, that has to be fixed first — either build a one-off
   sample puller that opens each job's detail page for text, or fix the
   connector itself, before extraction can be tested at all.

2. **Pull a small sample** (10-50 jobs — enough for a real read on
   quality without being expensive to review). Apply the project's
   standard filter methodology (see `feedback-job-search-filter-methodology`
   memory / `/filtered-search` skill) if pulling fresh rather than reusing
   existing DB rows, so the sample reflects real target-role jobs, not
   noise.

3. **Run extraction on the sample** using whichever method is currently
   the plan (in-session agent reasoning per `/enrich-jobs`, or an
   external API script) — do NOT write results to the live `jobs` /
   `companies` tables. Keep results in a scratch file
   (`extraction_results.json` or similar), not the DB, since this is a
   test, not a production run.

4. **Also run the company-level aggregate pass** on any companies with
   2+ postings in the sample — this is the stronger, more informative
   signal (per the finding that pain_points aggregate across a company's
   postings, not per-job) and worth checking early since a single-posting
   company gives much weaker signal.


5. **Flag known gaps proactively, don't wait to be asked**: fields that
   came back null across most/all of the sample (investigate whether it's
   a genuine absence in the source text — e.g. LinkedIn's anonymous job
   page has no employee-count data at all, confirmed by dumping the raw
   page text — vs. an extraction/prompt failure); fields that only partly
   parsed (malformed JSON, garbled characters); and any field where a
   *scraped* value would be more reliable than an *inferred* one (e.g.
   LinkedIn's guest page has a real `Industries` text field sitting
   right next to the description — prefer that over LLM-inferred
   industry when it's available).

6. **Self-judge the sample and proceed autonomously.** Don't wait for the
   user to manually verify each sample — assess quality yourself against
   the criteria in step 6 (nulls that make sense vs. nulls that look like
   failures, malformed/garbled output rate, scraped-vs-inferred field
   conflicts) and decide whether it's good enough to move on to
   `/enrich-jobs`. If quality is clearly poor (e.g. most rows failed to
   parse, or fields are obviously wrong), fix the prompt/schema/source and
   re-test before proceeding — don't push bad extractions into the DB.
   Only surface results to the user as a status update, not a gate to
   wait on.
