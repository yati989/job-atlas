import pandas as pd

from scripts import verify_dashboard_queries


def test_decision_run_verifier_covers_every_public_progress_read_model(monkeypatch):
    called = []

    def options(_session):
        called.append("decision_run_options")
        return pd.DataFrame([{
            "run_id": "run-1", "state": "preparing", "since_at": object(),
            "cutoff_at": object(), "started_at": object(), "telemetry": "complete",
        }])

    def default(frame):
        called.append("default_decision_run_id")
        return frame.iloc[0]["run_id"]

    def progress(_session, _run_id):
        called.append("decision_run_progress")
        return pd.DataFrame([{
            "stage": "fetch_jobs", "status": "running", "input": 2,
            "advanced": 1, "dropped": 0, "failed": 0, "pending": 1,
            "attempt": 1, "attempt_history": [], "updated_at": object(),
            "telemetry": "complete",
        }])

    def funnel(_session, _run_id):
        called.append("decision_run_job_funnel")
        return pd.DataFrame([{
            "stage": "collection_deduplication", "from": "Raw fetched",
            "to": "Collection-unique", "input": 2, "advanced": 1,
            "dropped": 1, "failed": 0, "pending": 0, "available": True,
        }])

    def drilldown(_session, _run_id, *, stage_name):
        called.append("decision_run_job_funnel_drilldown")
        assert stage_name == "collection_deduplication"
        return pd.DataFrame(columns=[
            "record_id", "outcome", "reason", "source", "external_job_id",
            "job_url", "apply_url",
        ])

    def company_funnel(_session, _run_id):
        called.append("decision_run_company_funnel")
        return pd.DataFrame([{"stage": "company_deduplication", "companies": 1,
                              "eligible_jobs": 1, "advanced": 1, "dropped": 0,
                              "failed": 0, "pending": 0}])

    def company_drilldown(_session, _run_id, *, stage_name):
        called.append("decision_run_company_funnel_drilldown")
        assert stage_name == "company_deduplication"
        return pd.DataFrame(columns=["company_id", "outcome", "reason", "company_evidence", "source", "job_url", "apply_url"])

    def tailoring_funnel(_session, _run_id):
        called.append("decision_run_resume_tailoring_funnel")
        return pd.DataFrame([{"stage": "resume_tailoring", "input": 1, "tailored": 0,
                              "dropped": 0, "failed": 0, "pending": 1, "available": True}])

    def tailoring_drilldown(_session, _run_id):
        called.append("decision_run_resume_tailoring_drilldown")
        return pd.DataFrame(columns=["posting_version_id", "outcome", "reason", "source", "job_url", "apply_url"])

    def contact_funnel(_session, _run_id):
        called.append("decision_run_contact_funnel")
        return pd.DataFrame([{"input": 1, "enriched": 0, "exhausted": 0,
                              "no_match": 0, "excluded": 0, "failed": 0, "pending": 1}])

    def contact_drilldown(_session, _run_id):
        called.append("decision_run_contact_funnel_drilldown")
        return pd.DataFrame(columns=["company_id", "search_group", "outcome", "reused_contact_ids", "new_contact_ids", "coverage"])

    def outreach_funnel(_session, _run_id):
        called.append("decision_run_outreach_funnel")
        return pd.DataFrame([{"stage": "draft_preparation", "input": 1, "prepared": 0,
                              "dropped": 0, "failed": 0, "pending": 1, "available": True}])

    def outreach_drilldown(_session, _run_id, *, stage_name):
        called.append("decision_run_outreach_funnel_drilldown")
        assert stage_name == "draft_preparation"
        return pd.DataFrame(columns=["contact_id", "outreach_draft_id", "outcome", "email", "job_url", "apply_url"])

    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_options", options)
    monkeypatch.setattr(verify_dashboard_queries.queries, "default_decision_run_id", default)
    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_progress", progress)
    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_job_funnel", funnel)
    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_job_funnel_drilldown", drilldown)
    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_company_funnel", company_funnel)
    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_company_funnel_drilldown", company_drilldown)
    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_resume_tailoring_funnel", tailoring_funnel)
    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_resume_tailoring_drilldown", tailoring_drilldown)
    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_contact_funnel", contact_funnel)
    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_contact_funnel_drilldown", contact_drilldown)
    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_outreach_funnel", outreach_funnel)
    monkeypatch.setattr(verify_dashboard_queries.queries, "decision_run_outreach_funnel_drilldown", outreach_drilldown)
    monkeypatch.setattr(verify_dashboard_queries, "check", lambda *_args: None)

    verify_dashboard_queries.verify_decision_run_read_models(object())

    assert tuple(called) == verify_dashboard_queries.DECISION_RUN_READ_MODEL_FUNCTIONS
