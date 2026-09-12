import re
from .types import Approval

_RULE = re.compile(r"^G([1-4])\s+(all|top\s+\d+|0)$", re.I)

def parse_approval(text: str, *, maximum_target: int) -> Approval:
    parts = [part.strip() for part in text.split(";") if part.strip()]
    if not parts or not parts[0].lower().startswith("target "):
        raise ValueError("approval must begin 'target N; G1 all|top N|0; ...'")
    target = int(parts[0].split()[1])
    if target < 0 or target > maximum_target:
        raise ValueError(f"target must be between 0 and {maximum_target}")
    limits: dict[int, int | None] = {}
    for part in parts[1:]:
        match = _RULE.match(part)
        if not match: raise ValueError(f"invalid approval rule: {part!r}")
        limits[int(match.group(1))] = None if match.group(2).lower() == "all" else (0 if match.group(2) == "0" else int(match.group(2).split()[1]))
    if set(limits) != {1, 2, 3, 4}: raise ValueError("approval must specify G1, G2, G3, and G4")
    return Approval(target, limits, text)
