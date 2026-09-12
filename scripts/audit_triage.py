"""
Triage-layer shadow audit (ADR-0007) — sibling of `audit_gate_verdicts.py`
(company-identity gate) and `audit_leak_rate.py` (jobs gate).

Reads `logs/triage_shadow.jsonl` (one line per KEPT candidate,
`triage.log_triage_shadow`) and reports:
  - decision distribution (AUTO_ACCEPT / ESCALATE / AUTO_REJECT)
  - per-marker firing counts for FUNCTION_OUT_OF_SCOPE — the input to
    deciding whether a marker has earned a place in OUT_OF_SCOPE_ARMED
    (contact_function.py's evidence-earned-exception pattern)
  - the AUTO_ACCEPT rate, as a fraction of kept rows

This script does NOT judge correctness — shadow mode logs a verdict, not a
verified one. Whether an AUTO_ACCEPT verdict was actually right requires
comparing it against what the agent independently stored for that same
candidate that batch, which stays a manual review step (see the plan's
rollout stages) until enough batches exist to make it worth automating.

Usage:
    python -m scripts.audit_triage
    python -m scripts.audit_triage --since 2026-08-05
"""
import argparse
import json
from collections import Counter
from pathlib import Path

TRIAGE_SHADOW_LOG = Path(__file__).resolve().parent.parent / "logs" / "triage_shadow.jsonl"


def load_entries(since: str | None = None) -> list[dict]:
    if not TRIAGE_SHADOW_LOG.exists():
        return []
    entries = []
    with TRIAGE_SHADOW_LOG.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if since and entry.get("ts", "") < since:
                continue
            entries.append(entry)
    return entries


def summarize(entries: list[dict]) -> str:
    if not entries:
        return "(no triage-shadow entries logged yet -- run a find-contacts batch with TRIAGE_MODE=shadow first)"

    decisions = Counter(e["decision"] for e in entries)
    total = len(entries)

    lines = [
        f"{total} kept candidates triaged",
        "",
        f"  AUTO_ACCEPT: {decisions['AUTO_ACCEPT']:5d}  ({_pct(decisions['AUTO_ACCEPT'], total)})",
        f"  ESCALATE:    {decisions['ESCALATE']:5d}  ({_pct(decisions['ESCALATE'], total)})",
        f"  AUTO_REJECT: {decisions['AUTO_REJECT']:5d}  ({_pct(decisions['AUTO_REJECT'], total)})",
    ]

    out_of_scope_hits = Counter()
    for e in entries:
        for marker in e.get("fn_out_of_scope", []):
            out_of_scope_hits[marker] += 1
    if out_of_scope_hits:
        lines.append("")
        lines.append("  out-of-scope marker firings (candidates for OUT_OF_SCOPE_ARMED once reviewed):")
        for marker, count in out_of_scope_hits.most_common():
            lines.append(f"      {marker}: {count}")

    tier_dist = Counter(e["tier"] for e in entries if e["decision"] == "AUTO_ACCEPT")
    if tier_dist:
        lines.append("")
        lines.append("  AUTO_ACCEPT tier distribution:")
        for tier, count in tier_dist.most_common():
            lines.append(f"      {tier}: {count}")

    untrusted = sum(1 for e in entries if not e.get("headline_trusted"))
    if untrusted:
        lines.append("")
        lines.append(f"  {untrusted} candidate(s) had an untrusted headline (no 'Name - ' boundary found) -- never AUTO_ACCEPT/REJECT-eligible")

    return "\n".join(lines)


def _pct(n: int, total: int) -> str:
    return f"{100 * n / total:.0f}%" if total else "n/a"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="ISO date/datetime, only count entries at or after this")
    args = parser.parse_args()

    entries = load_entries(since=args.since)
    print(summarize(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
