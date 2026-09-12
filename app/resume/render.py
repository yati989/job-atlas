"""
Deterministic resume renderer — the seam that *guarantees* ATS-parseability.

Pure function, no LLM: a `ResumeMaster` (base or tailored) -> single-column
ATS-safe LaTeX (Jinja2) -> PDF (Tectonic) -> a **pdftotext round-trip** that
re-extracts the PDF's text and confirms the load-bearing content (name,
companies, surfaced skills) actually survived. A visually fine PDF whose text
extracts scrambled is an ATS failure, so the round-trip — not the compile — is
the real acceptance gate.

Nothing here reads the DB or the network (beyond Tectonic fetching its own LaTeX
packages on first run). Reused by both the DB path and the ad-hoc path.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader

from app.resume.schema import ResumeMaster

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "ats_resume.tex.j2"

# LaTeX special characters -> their escaped forms. Backslash is in the class too;
# the single regex pass means no double-escaping of inserted backslashes.
_TEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "|": r"\textbar{}",
}
_TEX_RE = re.compile(r"[\\&%$#_{}~^|]")


def tex_escape(value) -> str:
    """Escape LaTeX specials in arbitrary user content."""
    return _TEX_RE.sub(lambda m: _TEX_SPECIALS[m.group()], str(value))


# A minimal, renderer-agnostic emphasis markup: **text** marks a span for
# bold. Not a LaTeX concern (ResumeMaster stays content-only) — the same
# markup is understood by app/outreach/gmail.py's HTML conversion, so a
# tailored bullet or email line only needs to be authored once. `*` is not a
# LaTeX special character, so tex_escape() never touches it — safe to apply
# the bold regex AFTER escaping, on the escaped text.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def tex_bold(value) -> str:
    """Like tex_escape, but converts **word** spans into \\textbf{word} after
    escaping — lets narrative content (bullets, achievements, summary) mark
    specific numbers or tools for emphasis. Only apply this filter to
    narrative fields; single-line fields (company/institution names, dates)
    use the plain `tex` filter, since bold markup isn't expected there."""
    escaped = tex_escape(value)
    return _BOLD_RE.sub(lambda m: r"\textbf{" + m.group(1) + "}", escaped)


@dataclass
class RenderResult:
    tex_path: Path
    pdf_path: Path
    ok: bool
    missing_tokens: list[str] = field(default_factory=list)

    def raise_if_failed(self) -> "RenderResult":
        if not self.ok:
            raise RenderError(
                "pdftotext round-trip failed — these tokens did not survive PDF "
                f"text extraction (ATS would miss them): {self.missing_tokens}"
            )
        return self


class RenderError(RuntimeError):
    pass


def _jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        block_start_string="<%",
        block_end_string="%>",
        variable_start_string="<<",
        variable_end_string=">>",
        comment_start_string="<#",
        comment_end_string="#>",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        keep_trailing_newline=True,
    )
    env.filters["tex"] = tex_escape
    env.filters["texb"] = tex_bold
    return env


def render_latex(master: ResumeMaster) -> str:
    """Fill the ATS-safe template from a master. Deterministic, no I/O.

    Renders the typed pydantic objects (not `model_dump()` dicts) so template
    attribute access resolves to fields — e.g. `group.items` is the skill list,
    not a dict's `.items` method.
    """
    template = _jinja_env().get_template(TEMPLATE_NAME)
    context = {name: getattr(master, name) for name in type(master).model_fields}
    return template.render(**context)


def _resolve_tectonic() -> str:
    """Prefer `tectonic` on PATH; fall back to the repo-local tools/ binary."""
    found = shutil.which("tectonic")
    if found:
        return found
    repo_root = Path(__file__).resolve().parents[2]
    for name in ("tectonic.exe", "tectonic"):
        candidate = repo_root / "tools" / name
        if candidate.exists():
            return str(candidate)
    raise RenderError(
        "Tectonic not found. Install it (https://tectonic-typesetting.github.io) "
        "so a `tectonic` binary is on PATH, or drop it in the repo's tools/ dir."
    )


def _expected_tokens(master: ResumeMaster) -> list[str]:
    """The load-bearing content whose survival proves ATS-parseability."""
    tokens = [master.contact.name]
    tokens += [e.company for e in master.experience]
    for group in master.skills:
        tokens += group.items
    tokens += [ed.institution for ed in master.education]
    # de-dupe, drop empties, keep order
    seen, out = set(), []
    for t in tokens:
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _roundtrip_missing(pdf_path: Path, tokens: list[str]) -> list[str]:
    """Extract text with pdftotext and return the expected tokens NOT found."""
    if not shutil.which("pdftotext"):
        raise RenderError("pdftotext not found — required for the ATS round-trip gate.")
    # Plain (non -layout) extraction: closer to how an ATS parser actually reads
    # a PDF's content stream (reading order, not visual columns), and -layout's
    # column-preservation can retain a soft-hyphen line break inside a word.
    proc = subprocess.run(
        ["pdftotext", str(pdf_path), "-"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RenderError(f"pdftotext failed: {proc.stderr.strip()}")
    # Normalise whitespace so line-wrapping / hyphen-free breaks don't cause
    # false misses; compare on collapsed single-spaced text.
    extracted = re.sub(r"\s+", " ", proc.stdout)
    missing = []
    for t in tokens:
        norm = re.sub(r"\s+", " ", t).strip()
        if norm and norm not in extracted:
            missing.append(t)
    return missing


def render_resume(
    master: ResumeMaster,
    out_dir: Path | str,
    basename: str = "resume",
    tectonic_bin: Optional[str] = None,
) -> RenderResult:
    """Render a master to a PDF and verify it round-trips through pdftotext.

    Writes `<basename>.tex` and `<basename>.pdf` into `out_dir`. Returns a
    `RenderResult`; call `.raise_if_failed()` to make a scrambled extraction a
    hard error.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tex_path = out_dir / f"{basename}.tex"
    pdf_path = out_dir / f"{basename}.pdf"
    tex_path.write_text(render_latex(master), encoding="utf-8")

    tectonic = tectonic_bin or _resolve_tectonic()
    proc = subprocess.run(
        [tectonic, "--outdir", str(out_dir), "--keep-logs", str(tex_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not pdf_path.exists():
        raise RenderError(f"Tectonic compile failed:\n{proc.stderr.strip()}")

    missing = _roundtrip_missing(pdf_path, _expected_tokens(master))
    return RenderResult(
        tex_path=tex_path, pdf_path=pdf_path, ok=(not missing), missing_tokens=missing
    )
