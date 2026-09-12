"""Pure, evidence-bound hard-reject policy."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    reason_code: str
    evidence: str | None
    parsed_value: float | None
    threshold: float


def evaluate_screening(salary: dict | None, minimum: dict | None, maximum: dict | None) -> list[Finding]:
    findings: list[Finding] = []
    maximum_cash = (salary or {}).get("guaranteed_max_lpa")
    if maximum_cash is not None and maximum_cash < 25:
        findings.append(Finding("salary_below_25_lpa", (salary or {}).get("evidence"), maximum_cash, 25))
    min_years = (minimum or {}).get("years") if (minimum or {}).get("mandatory", True) else None
    max_years = (maximum or {}).get("years") if (maximum or {}).get("mandatory", True) else None
    if min_years is not None and min_years >= 8:
        findings.append(Finding("minimum_experience_8_or_more", (minimum or {}).get("evidence"), min_years, 8))
    if max_years is not None and max_years <= 2 and not (maximum_cash is not None and maximum_cash > 30):
        findings.append(Finding("maximum_experience_2_or_less", (maximum or {}).get("evidence"), max_years, 2))
    return findings
