"""
Renderer seam (ATS #2 / issue #42): the deterministic pipeline guarantees
ATS-parseability. We assert external behavior only — that the compiled PDF's
re-extracted text contains the load-bearing tokens, and that malformed input
fails loudly — never the intermediate LaTeX strings.

The compile tests need Tectonic + pdftotext; they skip (not fail) if Tectonic
isn't available, so the suite still runs in a bare environment.
"""
import pytest

from app.resume.render import (
    RenderError,
    render_latex,
    render_resume,
    tex_escape,
    _resolve_tectonic,
)
from app.resume.schema import EXAMPLE_MASTER_PATH, ResumeMaster, load_master


def _tectonic_available() -> bool:
    try:
        _resolve_tectonic()
        return True
    except RenderError:
        return False


needs_tectonic = pytest.mark.skipif(
    not _tectonic_available(), reason="Tectonic not installed"
)


# ---- pure, no-compile behavior --------------------------------------------

def test_tex_escape_handles_latex_specials():
    out = tex_escape(r"a & b 50% $x #1 c_d {e} |")
    for escaped in [r"\&", r"\%", r"\$", r"\#", r"\_", r"\{", r"\}", r"\textbar{}"]:
        assert escaped in out


def test_tex_escape_backslash_not_double_processed():
    # a literal backslash becomes \textbackslash{} exactly once, not recursively
    assert tex_escape("a\\b") == r"a\textbackslash{}b"


def test_render_latex_escapes_user_content():
    m = ResumeMaster.model_validate(
        {"contact": {"name": "A & B"}, "skills": [{"category": "Core", "items": ["C#"]}]}
    )
    tex = render_latex(m)
    assert r"A \& B" in tex          # ampersand escaped
    assert r"C\#" in tex             # hash escaped


def test_section_order_is_fixed_by_explicit_decision():
    """Summary, Skills, Education, Achievements, Experience, Projects — this
    exact order, per the user's 2026-08-06 decision (draft-outreach's email
    template assumes it). Projects last, after Experience, matching its
    relative position in the pre-2026-08-06 template. Not a styling default;
    do not let this drift back to a "logical" order without asking."""
    m = ResumeMaster.model_validate({
        "contact": {"name": "A"},
        "summary": "S",
        "skills": [{"category": "Core", "items": ["X"]}],
        "experience": [{"company": "ExpCo"}],
        "education": [{"institution": "EduCo"}],
        "projects": [{"name": "ProjCo"}],
        "achievements": [{"text": "AchText"}],
    })
    tex = render_latex(m)
    positions = {
        section: tex.index(marker)
        for section, marker in [
            ("Summary", r"\section{Summary}"),
            ("Skills", r"\section{Skills}"),
            ("Education", r"\section{Education}"),
            ("Projects", r"\section{Projects}"),
            ("Achievements", r"\section{Achievements}"),
            ("Experience", r"\section{Experience}"),
        ]
    }
    ordered = sorted(positions, key=positions.get)
    assert ordered == ["Summary", "Skills", "Education", "Achievements", "Experience", "Projects"]


# ---- full compile + round-trip (the real acceptance gate) -----------------

@needs_tectonic
def test_base_master_roundtrips(tmp_path):
    m = load_master(EXAMPLE_MASTER_PATH)
    res = render_resume(m, tmp_path, basename="base")
    assert res.pdf_path.exists()
    assert res.ok, f"tokens lost in PDF text extraction: {res.missing_tokens}"
    res.raise_if_failed()


@needs_tectonic
def test_roundtrip_finds_name_companies_skills(tmp_path):
    m = ResumeMaster.model_validate(
        {
            "contact": {"name": "Alex Morgan"},
            "skills": [{"category": "Core", "items": ["PySpark", "XGBoost"]}],
            "experience": [{"company": "Example Bank", "title": "Data Scientist", "bullets": ["Built a risk model."]}],
        }
    )
    res = render_resume(m, tmp_path, basename="mini")
    assert res.ok
    # every expected token really survives extraction
    assert res.missing_tokens == []


@needs_tectonic
def test_missing_token_is_reported_not_silently_passed(tmp_path, monkeypatch):
    # Force the round-trip to look for a token the PDF cannot contain.
    import app.resume.render as render_mod

    m = load_master(EXAMPLE_MASTER_PATH)
    orig = render_mod._expected_tokens
    monkeypatch.setattr(
        render_mod, "_expected_tokens", lambda master: orig(master) + ["ZZZ_NOT_IN_RESUME"]
    )
    res = render_resume(m, tmp_path, basename="fail")
    assert not res.ok
    assert "ZZZ_NOT_IN_RESUME" in res.missing_tokens
    with pytest.raises(RenderError):
        res.raise_if_failed()


def test_invalid_master_raises():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ResumeMaster.model_validate({"summary": "no contact"})
