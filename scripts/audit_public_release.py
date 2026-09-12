"""Fail a release when the repository contains private or machine-local data."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


_EXCLUDED_PARTS = {".git", ".pytest_cache", "__pycache__", ".venv", "venv"}
_BINARY_SUFFIXES = {
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pyc",
    ".xlsx",
    ".zip",
}
_PRIVATE_PATHS = {
    ".env",
    ".gmail_token.json",
    ".google_sheets_token.json",
    "app/resume/master.yaml",
    "credentials.env",
    "credentials.json",
    "gmail-token.json",
    "google-oauth-client.json",
}
_EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})(?![\w.-])")
_ABSOLUTE_USER_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s\"']+")
_EXAMPLE_EMAIL_DOMAINS = {"example.com", "example.net", "example.org"}
_RULE_ORDER = {
    "private-resume-path": 0,
    "private-credential-path": 1,
    "personal-email": 2,
    "absolute-user-path": 3,
}


@dataclass(frozen=True)
class AuditFinding:
    path: str
    rule: str
    detail: str


def _iter_public_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        if any(part in _EXCLUDED_PARTS for part in path.relative_to(root).parts):
            continue
        yield path


def audit_public_tree(root: Path) -> list[AuditFinding]:
    """Return deterministic release-blocking findings beneath ``root``."""
    findings: list[AuditFinding] = []
    for path in _iter_public_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in _PRIVATE_PATHS:
            rule = "private-resume-path" if relative == "app/resume/master.yaml" else "private-credential-path"
            findings.append(AuditFinding(relative, rule, "private artifact must not be tracked"))

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        personal_emails = sorted(
            {
                email
                for email in _EMAIL_RE.findall(content)
                if not _is_example_domain(email.rsplit("@", 1)[1])
            }
        )
        if personal_emails:
            findings.append(
                AuditFinding(relative, "personal-email", ", ".join(personal_emails))
            )
        absolute_paths = sorted(set(_ABSOLUTE_USER_PATH_RE.findall(content)))
        if absolute_paths:
            findings.append(
                AuditFinding(relative, "absolute-user-path", ", ".join(absolute_paths))
            )

    return sorted(
        findings,
        key=lambda finding: (finding.path, _RULE_ORDER[finding.rule], finding.detail),
    )


def _is_example_domain(domain: str) -> bool:
    normalized = domain.lower()
    return normalized in _EXAMPLE_EMAIL_DOMAINS or normalized.endswith(".example")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    findings = audit_public_tree(args.root.resolve())
    for finding in findings:
        print(f"{finding.path}: {finding.rule}: {finding.detail}")
    if findings:
        print(f"FAIL: {len(findings)} public-release finding(s)")
        return 1
    print("PASS: public tree contains no blocked private artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
