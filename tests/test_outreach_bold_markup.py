"""
The **bold** markup convention shared between the resume PDF (app.resume.
render.tex_bold) and outreach emails (app.outreach.gmail.body_to_html /
strip_bold_markup). One notation, two renderers — tested independently
since each has its own escaping order to get right.
"""
from app.outreach.gmail import body_to_html, strip_bold_markup


def test_strip_bold_markup_removes_markers_keeps_content():
    assert strip_bold_markup("Built **XGBoost** models") == "Built XGBoost models"


def test_strip_bold_markup_handles_multiple_spans():
    assert strip_bold_markup("**A** and **B**") == "A and B"


def test_strip_bold_markup_is_a_no_op_without_markers():
    assert strip_bold_markup("Plain text") == "Plain text"


def test_body_to_html_converts_bold_spans():
    assert body_to_html("Built **XGBoost** models") == "Built <b>XGBoost</b> models"


def test_body_to_html_converts_newlines_to_br():
    assert body_to_html("Line one\nLine two") == "Line one<br>\nLine two"


def test_body_to_html_escapes_before_bolding():
    """A literal '<' or '&' in the drafted copy must not break the HTML —
    escape happens first, so '**a < b**' becomes bold text containing an
    entity, never a broken/injected tag."""
    out = body_to_html("**a < b & c**")
    assert out == "<b>a &lt; b &amp; c</b>"


def test_body_to_html_does_not_escape_the_bold_tags_themselves():
    out = body_to_html("**word**")
    assert "<b>word</b>" in out
    assert "&lt;b&gt;" not in out
