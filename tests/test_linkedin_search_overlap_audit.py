from scripts.audit_linkedin_search_overlap import summarize_sets


def test_overlap_summary_reports_exclusive_contribution_and_removal_coverage():
    summary = summarize_sets(
        {
            "data scientist": {"1", "2", "3"},
            "data analyst": {"2", "3", "4"},
            "ai engineer": {"3"},
        }
    )

    assert summary["raw_occurrences"] == 7
    assert summary["unique_jobs"] == 4
    assert summary["duplicate_occurrences"] == 3
    assert summary["duplicate_rate_pct"] == 42.9
    by_name = {row["name"]: row for row in summary["groups"]}
    assert by_name["data scientist"]["exclusive_jobs"] == 1
    assert by_name["data analyst"]["exclusive_jobs"] == 1
    assert by_name["ai engineer"]["exclusive_jobs"] == 0
    assert by_name["ai engineer"]["coverage_without_pct"] == 100.0
