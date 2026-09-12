---
name: tailor-resumes
description: Generate a tailored, ATS-safe resume + honest match score for relevant jobs — for a batch of DB jobs, or for an ad-hoc link/pasted JD (which now also lands in Postgres and gets the same tailored_resumes row). Agent reasoning, no external LLM API call.
---

# Tailor Resumes

Produces, for each relevant job, a **tailored resume** (the user's true content
reordered/re-emphasized/reworded to mirror the JD's vocabulary), a **match
score**, and a **match report** with a two-bucket gap list. The tailoring
*judgment* is done by the agent in-session — like `enrich-jobs`, there is **no
external LLM API call** and no separate bill. Deterministic work (rendering the
PDF, the round-trip gate, persistence) is done by `app/resume/`.

**There is one process, not two.** Whether the job comes from the daily
batch or from a pasted link/JD, it always ends the same way: a real `jobs`
row, enriched, with a `tailored_resumes` row and PDF. The only thing that
differs is *how the job row is obtained* — see "Getting a job row" below.

## The hard integrity rule (read first)

Tailoring may **reorder, re-emphasize, and reword** content that is already true
and present in the resume master. It may **never** invent experience, skills,
employers, dates, or metrics. The score never rises by fabrication. A JD
requirement the master genuinely cannot support is **reported as a real gap**,
never written into the resume. If you cannot cover a requirement with true
content, that is the correct, honest outcome — not a prompt to embellish.

## Inputs

- **Resume master**: `app/resume/schema.load_master()` → the true source content.
- **DB path**: relevant jobs whose requirements are already decomposed by
  enrichment (`job_skills`, `experience_*_years`, `education_requirement`,
  `qualification_other`). Never re-extract these from raw text — read them via
  `app/resume/tailor.job_requirements()`.

## Public guided-run mode

When invoked by `full-pipeline`, never use `app.db.session` or the legacy
approved Decision Run. Read only the immutable public selection scope:

```bash
job-atlas tailoring-context --scope-id <scope-id> \
  --private-home <private-home>
```

This reports exact posting versions as `needs_tailoring` or `reused`, using
the frozen JD snapshot plus Phase A requirements stored in the same public
SQLite database. Produce the compact plan and complete gap report using the
truthfulness rules below, then render/persist each required result with:

```bash
job-atlas save-tailored --scope-id <scope-id> \
  --posting-version-id <version-id> --tailoring-plan <plan.json> \
  --master <resume-master.yaml> --score <score> --gap-report <gap.json> \
  --private-home <private-home>
```

The command refuses posting versions outside the scope, applies the plan to
the confirmed master, runs the existing PDF text-extraction gate, and records
the exact posting material hash for repeat-safe reuse. The older batch/ad-hoc
instructions below are standalone compatibility modes only.

## Getting a job row

**Batch mode** (daily run over already-ingested relevant jobs):

**Approved decision-run mode:** load the immutable approved scope and use
`app.decision_runs.resume_funnel.freeze_resume_tailoring_manifest(session, run_id)`
and then use `app.decision_runs.resume_funnel.versions_needing_tailoring(session,
run_id)`. That selector skips reusable and already-terminal IDs while retaining
the frozen denominator. Tailor every returned
posting version; never replace this with a `since` or pending-status query.
For each returned version, read requirements through
`python -m scripts.tailor_resume --requirements --compact --run-id <run-id>
--job-id <job-id>` so the
reasoning and render use the frozen JD rather than today's mutable `jobs` row.
That adapter returns only posting-version snapshot facts: if structured
experience, education, qualification, or skill fields were not frozen there,
they remain empty and you reason from the frozen `description_raw`; never fill
them from mutable `Job`/`JobSkill` enrichment after approval.
If validation or rendering fails, call `report_resume_tailoring_failure(...)`
with reason `validation_failed` or `render_failed` and an explicit `excluded`
or `non_blocking` disposition, commit that result, and continue processing the
company's other approved jobs.

For a full-pipeline batch, the coordinator may shard tailoring-plan reasoning
across up to three workers by disjoint frozen posting-version IDs. Each worker
owns a distinct plan/gap artifact path and returns its result; it does not
report aggregate stage progress. The coordinator serially runs the
render/persist adapter and reports terminal outcomes so concurrent writers
cannot overwrite the same stage snapshot. Never assign the same job or output
directory to two workers.

```python
from app.db.session import get_session
from app.resume.schema import load_master
from app.resume.tailor import select_jobs_needing_resume, job_requirements

master = load_master()
with get_session() as session:
    jobs = select_jobs_needing_resume(session, limit=25)
```

`select_jobs_needing_resume` returns enriched, relevant jobs with **no current
`tailored_resumes` row** (re-running is safe — done jobs are skipped; to
re-tailor a specific job, delete its row first). Takes an optional
`since: datetime` to additionally require `first_seen_at >= since` — used
by `full-pipeline` to scope to one run's ingestion batch; leave it unset
here, since standalone `tailor-resumes` exists to work down the whole
backlog, not one run's slice of it.

**Ad-hoc mode** (a pasted link or raw JD text — from the user, not the daily
batch): **always check existence first**, then ensure a row exists:

```python
from app.resume.tailor import get_or_create_adhoc_job, job_requirements

with get_session() as session:
    job = get_or_create_adhoc_job(
        session, url=url, jd_text=jd_text, title=title, company=company,
    )
    session.commit()
```

`get_or_create_adhoc_job` **always checks for an existing match first**
(`find_job_by_url` — exact URL, then LinkedIn-job-id fallback for share/
search-results links) and returns that row as-is if found — nothing is
re-inserted or duplicated. Only when no match exists does it insert a new
`jobs` row (via `app.pipeline.upsert.upsert_job`, the same path every
connector uses): a LinkedIn `url` gets `source="linkedin"` + the embedded
numeric job id, so a later real connector scrape of the same posting updates
this row rather than duplicating it; anything else gets `source="adhoc"` with
a content hash as a stable id (idempotent on re-paste).

- **You need `jd_text` to create a new row.** If the link is one you can fetch
  directly, do so; if it's bot-blocked (Cloudflare/login) or you have no JD
  text at all, **ask the user to paste the JD text** — do not launch browser
  automation, that's out of scope for this tool.
- Once you have a `job`, everything downstream is identical to batch mode.

## Shared steps (batch and ad-hoc alike)

1. **Ensure it's enriched.** If `job.enrichment_status != "done"` (true for
   any freshly-inserted ad-hoc job, and possible for an existing-but-unscraped
   match), run the `/enrich-jobs` skill on that job **first** — don't improvise
   a one-off requirement decomposition. This writes `job_skills`/
   `experience_*_years`/`education_requirement`/`qualification_other` exactly
   as any other job gets them, so `job_requirements()` returns the same
   canonical structure every job is reasoned over:

   ```python
   reqs = job_requirements(session, job)
   ```

2. **Reason (no API call):**
   - Read the job's decomposed requirements and the master.
   - **Decompose the JD requirements** into must-haves (required skills, minimum
     years, required degree, core responsibilities) and nice-to-haves.
   - **Build a compact tailoring plan**: start from the true master and express
     only changed text and list selection/reordering as
     `app.resume.plan.ResumeTailoringPlan`. Do not reproduce unchanged resume
     content in agent output. Reorder experience bullets and skills so the
     JD's most relevant true content leads;
     reword true bullets to mirror the JD's exact terminology *only where the
     underlying work genuinely was that thing*; trim clearly-irrelevant bullets.
     Keep it the same `ResumeMaster` shape. Add nothing not in the master.
   - **Bold key numbers and tools**: wrap the 1–3 most relevant metrics/tools
     per bullet in `**double asterisks**` (e.g. `**600+ features**`,
     `**XGBoost**`) — `app.resume.render.tex_bold` renders this as real
     `\textbf{}` in the PDF. Same convention `/draft-outreach` uses for the
     email body, so content authored once looks right on both surfaces.
   - **Score and split gaps** (see below).

3. **Persist + render** (deterministic, includes the ATS round-trip gate):

   ```bash
   python -m scripts.tailor_resume --tailoring-plan plan.json \
     --job-id <id> --score <score> --gap-report gap.json \
     --run-id <run-id> --failure-disposition excluded
   ```

   The command applies the plan to the reviewed master, renders the ATS-safe
   PDF, **fails loudly if it doesn't
   round-trip through pdftotext**, and upserts one row per job (idempotent) —
   used for every job now, batch or ad-hoc, since both are real `jobs` rows.
   In approved mode it resolves and persists the immutable posting-version JD
   and material hash itself; a caller-provided mutable JD cannot replace them.

   **Terminal progress is mandatory for every approved ID.** On a caught
   validation/render exception, persist `Failed` through
   `report_resume_tailoring_failure` before continuing. `Dropped` is normally
   zero and is never an error shortcut: it is allowed only after the user
   explicitly revokes that posting's approval, recorded through
   `report_resume_tailoring_drop(..., reason="approval_revoked_for_posting")`.
   Any unavailable renderer, bad gap report, or other implementation problem
   remains `Failed`, with a disposition; it must not silently reduce the
   approved denominator.

   The deterministic render+persist step is also exposed as a CLI, handy when
   you've written the tailored master to a file:
   `python -m scripts.tailor_resume --tailoring-plan plan.json --job-id <id> --score <s> --gap-report g.json`
   (and `--requirements --job-id <id>` dumps a job's decomposed requirements).
   Approved runs additionally pass `--run-id <id> --failure-disposition
   excluded|non_blocking`; the CLI commits validation/render failures live.

4. **Report back**: how many tailored, the score distribution, and (in batch
   mode) how many relevant jobs still lack a resume.

## Scoring model (explainable, must-have weighted)

The score must be defensible item-by-item — never a gestalt number.

- Classify each JD requirement as **must-have** or **nice-to-have**.
- A requirement is **covered** only if true master content evidences it; record
  *where* (which role/bullet).
- Suggested formula (apply judgment; guard against divide-by-zero):
  `score = round(100 * (0.8 * covered_must/total_must + 0.2 * covered_nice/total_nice))`
  — if a category has no items, redistribute its weight to the other.

## Gap report shape (`gap_report`, stored as JSON)

```json
{
  "score": 82,
  "must_haves":   [{"requirement": "PySpark", "covered": true,  "evidence": "Kotak811: 600+ vars in PySpark"}],
  "nice_to_haves":[{"requirement": "Tableau", "covered": true,  "evidence": "Navi: DPD30+ payout automation"}],
  "surfaceable_gaps": [{"requirement": "credit scorecards", "note": "true content existed in master; now surfaced/reworded to match JD"}],
  "real_gaps":        [{"requirement": "5+ yrs", "note": "candidate has 4+ yrs; cannot be closed by editing — real gap"}]
}
```

- **Surfaceable gap** — evidence *was* in the master, just not surfaced for this
  JD. These should be auto-closed by your tailoring, so ideally few survive here.
- **Real gap** — genuinely absent from the user's background. "Make the score
  perfect" here means *actually acquiring the thing*, not faking it. Report it
  so the user can address it in a cover letter, upskill, or skip the role.

**Completeness rule (enforced, not optional)**: every requirement marked
`"covered": false` in `must_haves`/`nice_to_haves` MUST appear in
`surfaceable_gaps` or `real_gaps` under its **exact** requirement string — never
merged with another requirement or renamed/paraphrased (e.g. "Communication"
and "Stakeholder management" are two separate gap entries, never combined into
one). `save_tailored` calls `app.resume.tailor.validate_gap_report` and raises
`GapReportError` if any uncovered item is missing a gap entry, any gap entry
doesn't match a real requirement name, or a `covered: true` item is also listed
as a gap — fix the report and retry rather than persisting an incomplete one.

## Integrity spot-check (do every run)

Before finishing, confirm no bullet in any tailored resume asserts a skill,
employer, or role absent from `app/resume/master.yaml`. If one slipped in,
remove it and re-render — a true gap is the correct outcome, not a fabricated
line.
