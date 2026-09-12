"""
Profile-relevance-gate verdict audit (raised 2026-08-05): what fraction of
kept contact candidates are unambiguous ("clean") structural company matches
vs. flagged (`sister_entity`/`no_signal`, ambiguous company identity) vs.
structurally dropped (`shape`/`company`) — the number needed to judge whether
an "auto-store clean matches, only escalate flags to the agent" change to
`find-contacts` is worth building, and how much judgement-call volume it
would actually save.

No historical data existed to answer this at the time it was asked: the
Bright Data call ledger (`logs/brightdata_calls.jsonl`) only logs a raw
result *count*, and the 438 contacts in `contact_batches/*.md` are stored as
paraphrased text that can't be re-gated. `profile_relevance.log_gate_verdict`
now appends one aggregate line per `search` CLI call to
`logs/gate_verdicts.jsonl`, going forward — this script reads that log back.

Usage:
    python -m scripts.audit_gate_verdicts
    python -m scripts.audit_gate_verdicts --since 2026-08-05
"""
import argparse
import json
from collections import Counter
from pathlib import Path

GATE_VERDICT_LOG = Path(__file__).resolve().parent.parent / "logs" / "gate_verdicts.jsonl"


def load_entries(since: str | None = None) -> list[dict]:
    if not GATE_VERDICT_LOG.exists():
        return []
    entries = []
    with GATE_VERDICT_LOG.open(encoding="utf-8") as fh:
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
        return "(no gate-verdict entries logged yet -- run a find-contacts batch first)"

    clean = sum(e["kept_clean"] for e in entries)
    flagged_reasons: Counter = Counter()
    for e in entries:
        for reason, count in e.get("kept_flagged", {}).items():
            flagged_reasons[reason] += count
    flagged = sum(flagged_reasons.values())
    dropped_shape = sum(e["dropped_shape"] for e in entries)
    dropped_company = sum(e["dropped_company"] for e in entries)

    kept_total = clean + flagged
    seen_total = kept_total + dropped_shape + dropped_company

    lines = [
        f"{len(entries)} search calls logged, {seen_total} candidate rows seen",
        "",
        f"  clean (no judgement needed if auto-store lands): {clean:5d}  ({_pct(clean, seen_total)})",
        f"  flagged (still needs a judgement call):          {flagged:5d}  ({_pct(flagged, seen_total)})",
    ]
    for reason, count in flagged_reasons.most_common():
        lines.append(f"      - {reason}: {count}")
    lines += [
        f"  dropped, shape axis:                             {dropped_shape:5d}  ({_pct(dropped_shape, seen_total)})",
        f"  dropped, company axis:                           {dropped_company:5d}  ({_pct(dropped_company, seen_total)})",
        "",
        f"  of KEPT rows, {_pct(clean, kept_total)} are clean -- that's the judgement-call volume "
        "an auto-store-clean-matches change would remove.",
    ]
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
