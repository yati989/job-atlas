# ADR 0015: Bounded agent parallelism

Full-pipeline agent work uses bounded fan-out over immutable Decision Run IDs.
The coordinator may use at most three workers for independent reasoning or
external-I/O shards, with each worker owning disjoint posting versions,
companies, company/functions, or contacts and a distinct artifact path.

The coordinator is the single writer for manifest freezes, aggregate stage
reports, shared checkpoints, Gmail Draft posting, and final-report delivery.
Workers never share SQLAlchemy sessions or ORM objects. This avoids lost stage
snapshots while preserving useful overlap:

- pre-approval screening extraction is sharded, then Glassdoor and AmbitionBox
  Phase B run as concurrent source lanes;
- post-approval full job enrichment, resume tailoring, and company Phase A
  overlap;
- contact research starts when Phase A becomes terminal and may overlap
  unfinished job/resume work;
- draft-copy reasoning is sharded only after the resume/contact join.

Browser-source sessions, AmbitionBox's global pacing, Gmail provider calls,
and final self-report delivery remain serial. A failed worker is joined and
reported for its frozen IDs; it does not cancel unrelated shards.
