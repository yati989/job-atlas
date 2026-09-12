"""
No-signal location audit (T9, issue #18): ranks sources by how many jobs the
relevance gate dropped on the location axis with reason "no_signal" — i.e. an
onsite/hybrid job with no Bangalore marker in its captured location text.

This is a *capture* signal, not a relevance verdict: a no-signal drop usually
means the connector failed to capture location at all (empty/missing
location_raw), not that the job is confirmed non-Bangalore. Sources at the
top of this list are the strongest candidates for a connector-side
location-capture bug worth fixing at the source, rather than the gate
legitimately excluding an out-of-scope job.

Foreign-remote drops ("foreign") are NOT counted here — those are the gate
correctly excluding an explicitly non-India remote job, not a capture gap.

IMPORTANT: this reads a pipeline run's LOG OUTPUT, not the jobs table.
Dropped jobs are never stored (drop-at-ingest, ADR-0002), so there is no way
to recover per-job drop detail from the database after the fact — the
per-source "location_detail: foreign=N no_signal=M" line that
runner.gate_and_upsert() already logs on every run is the only place this
data exists. Point this script at that run's log file.

Usage:
    python -m app.pipeline.run_all > run.log 2>&1
    python -m scripts.audit_no_signal_locations run.log
"""
import argparse
import re

_LINE_RE = re.compile(
    r"Fetched \d+ jobs from (?P<source>[\w.]+), kept \d+ .*"
    r"location_detail: foreign=(?P<foreign>\d+) no_signal=(?P<no_signal>\d+)"
)


def parse_log(path: str) -> dict[str, dict[str, int]]:
    per_source: dict[str, dict[str, int]] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _LINE_RE.search(line)
            if not m:
                continue
            source = m.group("source")
            # Last line per source in the log wins (final per-source
            # totals) — earlier per-instance runner bugs aside, the runner
            # gates each source exactly once per run.
            per_source[source] = {
                "foreign": int(m.group("foreign")),
                "no_signal": int(m.group("no_signal")),
            }
    return per_source


def run(log_path: str) -> None:
    per_source = parse_log(log_path)
    if not per_source:
        print(f"No 'location_detail' lines found in {log_path} — nothing to audit.")
        return

    rows = [(src, stats) for src, stats in per_source.items() if stats["no_signal"] > 0]
    if not rows:
        print("No no_signal location drops found in this run — nothing to audit.")
        return

    rows.sort(key=lambda kv: kv[1]["no_signal"], reverse=True)

    print(f"{'source':25s} {'no_signal':>10s} {'foreign':>10s}")
    print("-" * 50)
    for source, stats in rows:
        print(f"{source:25s} {stats['no_signal']:>10d} {stats['foreign']:>10d}")

    print()
    print("Highest no_signal counts are the strongest candidates for a")
    print("connector-side location-capture bug worth investigating first.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_path", help="Path to a run_all.py / run_headed_sources.py log file")
    args = parser.parse_args()
    run(args.log_path)


if __name__ == "__main__":
    main()
