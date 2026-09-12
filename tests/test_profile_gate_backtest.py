"""
Backtest: replay the profile relevance gate over every contact a human
already accepted across this project's real batches, and assert it drops
none of them (ADR-0006 verification step #4 — the leak-rate-audit analogue
for the profile gate, mirroring `scripts/audit_leak_rate.py`'s role for the
jobs gate).

Evidence files (`contact_batches/batch-*.md`) store a PARAPHRASED "Text
judged" line, not the raw Bright Data snippet — the agent rewrote it in
dash-separated prose when building the evidence trail, not middle-dot
(`·`) separated like a real SerpResult. So most of these will fall through
`parse_structured_field` to `no_signal` rather than exercising the
structural company-prefix match — that's fine and expected; `no_signal`
passes `company_ok` (keep + flag), which is exactly the invariant this
backtest checks. The stronger, structural-match tests against REAL raw
snippets live in test_profile_gate.py's real-case block.
"""
import re
from pathlib import Path

import pytest

from app.contacts.profile_relevance import ProfileCandidate, company_ok, shape_ok

EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "contact_batches"

_COMPANY_HEADING_RE = re.compile(r"^## (.+?)\s+\(")
_CONTACT_HEADING_RE = re.compile(r"^### (.+?) — (.+)$")
_PROFILE_LINE_RE = re.compile(r"^\-\s+\*\*Profile:\*\*\s+(\S+)")
_JUDGED_TEXT_RE = re.compile(r"^\-\s+\*\*Text judged:\*\*\s+(.+)$")


def _parse_evidence_file(path: Path) -> list[ProfileCandidate]:
    """Extract every judged contact as a ProfileCandidate. Best-effort line
    parser matching `agentic_batch.write_evidence`'s fixed output shape."""
    candidates: list[ProfileCandidate] = []
    company: str | None = None
    name: str | None = None
    title: str | None = None
    url: str | None = None

    for line in path.read_text(encoding="utf-8").splitlines():
        m = _COMPANY_HEADING_RE.match(line)
        if m:
            company = m.group(1).strip()
            continue
        m = _CONTACT_HEADING_RE.match(line)
        if m:
            name, title = m.group(1).strip(), m.group(2).strip()
            url = None
            continue
        m = _PROFILE_LINE_RE.match(line)
        if m:
            url = m.group(1).strip()
            continue
        m = _JUDGED_TEXT_RE.match(line)
        if m and company and name and title and url:
            candidates.append(ProfileCandidate(
                full_name=name, title=title, url=url,
                snippet=m.group(1).strip(), target_company=company,
            ))
    return candidates


def _all_real_candidates() -> list[ProfileCandidate]:
    out: list[ProfileCandidate] = []
    for path in sorted(EVIDENCE_DIR.glob("batch-*.md")):
        out.extend(_parse_evidence_file(path))
    return out


@pytest.mark.skipif(
    not EVIDENCE_DIR.exists() or not any(EVIDENCE_DIR.glob("*.md")),
    reason="no evidence files in this checkout",
)
def test_gate_never_drops_a_real_human_accepted_contact():
    candidates = _all_real_candidates()
    assert len(candidates) > 100, (
        f"only parsed {len(candidates)} candidates from evidence files — "
        "parser likely drifted from write_evidence's format, fix before trusting this backtest"
    )

    rejected = [c for c in candidates if not (shape_ok(c) and company_ok(c))]
    assert rejected == [], (
        f"gate would have auto-rejected {len(rejected)} contact(s) a human already accepted: "
        + ", ".join(f"{c.full_name} @ {c.target_company}" for c in rejected[:10])
    )


@pytest.mark.skipif(not EVIDENCE_DIR.exists(), reason="no evidence files in this checkout")
def test_backtest_parses_the_expected_evidence_files():
    """Sanity check on the parser itself — every batch file this repo ships
    should contribute at least one candidate, so a silently-broken parser
    can't make the drop-count assertion above vacuously true."""
    for path in sorted(EVIDENCE_DIR.glob("batch-*.md")):
        evidence = path.read_text(encoding="utf-8")
        if not evidence.startswith("# Contact batch "):
            continue
        candidates = _parse_evidence_file(path)
        explicitly_empty = any(marker in evidence for marker in (
            "_No contacts found._",
            "_Not searched:",
        ))
        assert candidates or explicitly_empty, (
            f"{path.name}: parser extracted zero contacts from a non-empty batch — "
            "check the format"
        )
