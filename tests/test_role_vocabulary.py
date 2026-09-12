from types import SimpleNamespace

import pytest

from app.config.categories import is_on_slice
from app.pipeline.relevance import role_ok


@pytest.mark.parametrize(
    ("search_term", "title"),
    [
        # AI engineering titles approved from the role-drop audit.
        ("ai engineer", "AI Developer"),
        ("ai engineer", "Agentic Software Engineer"),
        ("ai engineer", "AI-Enabled Software Engineer"),
        ("ai engineer", "AIML Principal Consultant"),
        ("ai engineer", "AI Lead/Senior Developer"),
        ("ai engineer", "AI Native Engineer, Growth Marketing"),
        ("ai engineer", "Python AIML Developer"),
        ("ai engineer", "AI Product Engineer"),
        ("ai engineer", "Python AI-ML Developer"),
        ("ai engineer", "AI Research Engineer"),
        ("ai engineer", "AI/ML Engineer"),
        ("ai engineer", "AI Software Engineer"),
        ("ai engineer", "AI Solution Engineer"),
        ("ai engineer", "Founding Engineer - AI"),
        ("ai engineer", "Python /AI Developer"),
        ("ai engineer", "Python AI Developer-S"),
        ("ai engineer", "AI Agent Developer"),
        ("ai engineer", "AI Agentic Engineer"),
        ("ai engineer", "AI Applications Engineer"),
        ("ai engineer", "AI Specialist"),
        ("ai engineer", "AI Researcher"),
        ("ai engineer", "AI Research Scientist"),
        ("ai engineer", "AI Scientist"),
        ("ai engineer", "Artificial Intelligence Researcher"),
        # ML engineering titles approved from the role-drop audit.
        ("machine learning engineer", "Lead I - ML Engineering"),
        ("machine learning engineer", "ML Software Developer, ML Technology"),
        ("machine learning engineer", "Forward Deployed Engineer"),
        ("machine learning engineer", "Software Engineer, ML Engineering"),
        # Risk titles approved from the role-drop audit.
        ("credit risk", "Risk Manager"),
        ("credit risk", "Analyst - Consumer Risk"),
        ("credit risk", "DVP - Portfolio Risk"),
        ("credit risk", "Associate / Senior Associate - Risk"),
        # Data-engineering title approved from the role-drop audit.
        ("data engineer", "SQL Developer"),
    ],
)
def test_approved_role_titles_pass_the_gate_and_their_search_slice(
    search_term: str, title: str
):
    job = SimpleNamespace(title=title)

    assert role_ok(job)
    assert is_on_slice(title, search_term)


@pytest.mark.parametrize(
    ("search_term", "title"),
    [
        ("ai engineer", "Senior AI Solutions Engineer"),
        ("ai engineer", "AI Application Engineer"),
        ("ai engineer", "AI Agent Engineer"),
        ("ai engineer", "AI Platform Engineer"),
        ("ai engineer", "AI Architect"),
        ("machine learning engineer", "Forward Deployment Engineer"),
        ("machine learning engineer", "ML Software Engineer"),
        ("credit risk", "Associate: Risk"),
        ("credit risk", "Portfolio Risk Officer"),
        ("credit risk", "Consumer Risk Specialist"),
        ("data engineer", "T-SQL Developer"),
        ("data engineer", "PL/SQL Developer"),
        ("data engineer", "Database Developer"),
        ("data engineer", "ETL Developer"),
        ("data engineer", "Snowflake Developer"),
        ("data engineer", "Databricks Engineer"),
        ("data engineer", "PySpark Developer"),
        ("data engineer", "Data Platform Engineer"),
    ],
)
def test_close_role_variants_also_match_the_expected_search_slice(
    search_term: str, title: str
):
    assert role_ok(SimpleNamespace(title=title))
    assert is_on_slice(title, search_term)


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer",
        "Growth Marketing Manager",
        "Product Manager, AI Strategy",
        "Enterprise Risk Consultant",
        "Environment Manager - Risk Management and GCO",
        "SQL Account Executive",
        "Forward Deployed Sales Engineer",
    ],
)
def test_broad_neighboring_titles_do_not_become_role_matches(title: str):
    assert not role_ok(SimpleNamespace(title=title))
