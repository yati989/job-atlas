"""Small evidence packets for pre-approval salary/experience screening."""
from __future__ import annotations

import re
from typing import Any


_SCREENING_MARKERS = re.compile(
    r"(?i)(?:₹|\binr\b|\brs\.?\b|\blpa\b|\blakh|salary|compensation|\bctc\b|"
    r"experience|\byears?\b|\byrs?\b|fresher)"
)


def screening_brief(snapshot: dict[str, Any], *, max_chars: int = 1800) -> dict[str, Any]:
    """Keep only passages capable of supporting the two hard-reject rules."""
    raw = str(snapshot.get("description_raw") or "")
    chunks = [
        " ".join(chunk.split())
        for chunk in re.split(r"[\r\n]+|(?<=[.!?])\s+", raw)
        if chunk.strip()
    ]
    matches = [chunk for chunk in chunks if _SCREENING_MARKERS.search(chunk)]
    selected: list[str] = []
    used = 0
    for chunk in matches:
        remaining = max_chars - used
        if remaining <= 0:
            break
        piece = chunk[:remaining]
        selected.append(piece)
        used += len(piece) + 1
    return {
        "title": snapshot.get("title"),
        "salary_raw": snapshot.get("salary_raw"),
        "screening_evidence": "\n".join(selected),
        "description_chars": len(raw),
        "screening_evidence_chars": sum(len(item) for item in selected),
        "screening_evidence_truncated": len(matches) > len(selected),
    }
