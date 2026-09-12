from datetime import datetime, timezone

import pytest

from app.collectors.feed.weworkremotely import WeWorkRemotelyConnector


def _card(
    slug: str,
    title: str,
    *,
    company: str = "Acme",
    age: str = "3d",
    headquarters: str = "Remote",
    labels: tuple[str, ...] = ("Full-Time", "Anywhere in the World"),
) -> str:
    label_html = "".join(
        f'<p class="new-listing__categories__category">{label}</p>' for label in labels
    )
    return f"""
    <a class="listing-link--unlocked" href="/remote-jobs/{slug}">
      <div class="new-listing">
        <span class="new-listing__header__title__text">{title}</span>
        <p class="new-listing__header__icons__date">{age}</p>
        <p class="new-listing__company-name">{company}</p>
        <p class="new-listing__company-headquarters">{headquarters}</p>
        <div class="new-listing__categories">{label_html}</div>
      </div>
    </a>
    """


def _search_page(cards: str, count: int) -> str:
    return f'<span data-job-filter-count>{count}</span>{cards}'


def _detail(
    *,
    region: str = "Anywhere in the World",
    date_posted: str = "2026-08-11 12:40:35 UTC",
) -> str:
    return f"""
    <script type="application/ld+json">
    {{
      "@context":"https://schema.org",
      "@type":"JobPosting",
      "title":"Data Scientist",
      "description":"&lt;p&gt;Full model-building description&lt;/p&gt;",
      "datePosted":"{date_posted}",
      "employmentType":"Full-Time",
      "jobLocationType":"TELECOMMUTE",
      "baseSalary":{{"currency":"USD","value":{{"minValue":"0","maxValue":"0","unitText":"YEAR"}}}}
    }}
    </script>
    <span class="box box--region">{region}</span>
    """


def test_search_parser_preserves_card_fields():
    connector = WeWorkRemotelyConnector(["data scientist"], max_age_days=None)
    page = _search_page(
        _card(
            "data-scientist",
            "Data Scientist",
            labels=("Featured", "Full-Time", "$100,000 or more USD", "India"),
        ),
        1,
    )

    card = connector._parse_search("data scientist", page)[0]

    assert card["title"] == "Data Scientist"
    assert card["company"] == "Acme"
    assert card["location"] == "India"
    assert card["employment_type"] == "Full-Time"
    assert card["salary"] == "$100,000 or more USD"
    assert card["search_terms"] == {"data scientist"}


def test_search_parser_fails_loudly_on_truncation():
    connector = WeWorkRemotelyConnector(["ai engineer"])

    with pytest.raises(RuntimeError, match="advertised 2 jobs but returned 1"):
        connector._parse_search(
            "ai engineer",
            _search_page(_card("ai", "AI Engineer"), 2),
        )


def test_detail_parses_exact_supported_fields_without_zero_salary():
    connector = WeWorkRemotelyConnector(["data scientist"])

    detail = connector._parse_detail(_detail(region="India"))

    assert detail["description"] == "Full model-building description"
    assert detail["posted_at"] == datetime(2026, 8, 11, 12, 40, 35, tzinfo=timezone.utc)
    assert detail["employment_type"] == "Full-Time"
    assert detail["location"] == "India"
    assert detail["salary"] is None


def test_rss_enrichment_preserves_exact_date_region_and_description():
    connector = WeWorkRemotelyConnector(["data scientist"])
    feed = """
    <rss><channel><item>
      <title>Acme: Data Scientist</title>
      <link>https://weworkremotely.com/remote-jobs/acme-data-scientist</link>
      <guid>https://weworkremotely.com/remote-jobs/acme-data-scientist</guid>
      <pubDate>Tue, 11 Aug 2026 12:40:35 +0000</pubDate>
      <region>India</region>
      <description>&lt;p&gt;RSS full description&lt;/p&gt;</description>
    </item></channel></rss>
    """

    detail = connector._parse_feed(feed)[
        "https://weworkremotely.com/remote-jobs/acme-data-scientist"
    ]

    assert detail["description"] == "RSS full description"
    assert detail["location"] == "India"
    assert detail["posted_at"] == datetime(2026, 8, 11, 12, 40, 35, tzinfo=timezone.utc)


def test_fetch_deduplicates_searches_and_details_only_title_viable_jobs(monkeypatch):
    connector = WeWorkRemotelyConnector(
        ["data scientist", "ai engineer"],
        max_age_days=None,
    )
    pages = {
        "data scientist": _search_page(
            _card("shared", "Data Scientist") + _card("sales", "Sales Engineer"),
            2,
        ),
        "ai engineer": _search_page(
            _card("shared", "Data Scientist") + _card("ai", "AI Engineer", labels=("Contract", "India")),
            2,
        ),
    }
    detail_calls = []
    monkeypatch.setattr(connector, "_fetch_search", lambda term: pages[term])
    monkeypatch.setattr(connector, "_fetch_feed", lambda _category: "<rss><channel/></rss>")

    def fetch_detail(url: str) -> str:
        detail_calls.append(url)
        return _detail(region="India")

    monkeypatch.setattr(connector, "_fetch_detail", fetch_detail)

    jobs = connector.fetch()

    assert {job.external_job_id for job in jobs} == {
        "/remote-jobs/shared",
        "/remote-jobs/sales",
        "/remote-jobs/ai",
    }
    assert sorted(detail_calls) == [
        "https://weworkremotely.com/remote-jobs/ai",
        "https://weworkremotely.com/remote-jobs/shared",
    ]
    by_id = {job.external_job_id: job for job in jobs}
    assert by_id["/remote-jobs/shared"].description_raw == "Full model-building description"
    assert by_id["/remote-jobs/sales"].description_raw is None
    assert by_id["/remote-jobs/ai"].location_raw == "India"
    assert by_id["/remote-jobs/shared"].raw_payload["search_terms"] == [
        "ai engineer",
        "data scientist",
    ]
