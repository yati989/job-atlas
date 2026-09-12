from pathlib import Path
import json
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from app.companies.ambitionbox import (
    AmbitionBoxCollector,
    AmbitionBoxObservation,
    AmbitionBoxPolicy,
    AmbitionBoxTarget,
    NaukriCompanyJudgment,
    NaukriTaxonomyResolver,
    load_naukri_company_judgments,
)


FIXTURE = (
    Path(__file__).parent / "fixtures" / "company_profile_ambitionbox_salaries.html"
).read_text()
DETAIL_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "company_profile_ambitionbox_salary_detail.html"
).read_text()


class Response:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class Transport:
    def __init__(self, responses, clock=None):
        self.responses = iter(responses)
        self.clock = clock
        self.calls = []

    def get(self, url):
        self.calls.append((url, self.clock() if self.clock else None))
        return next(self.responses)

    def close(self):
        pass


class SlowTransport(Transport):
    def __init__(self, responses, clock, response_seconds):
        super().__init__(responses, clock)
        self.response_seconds = response_seconds

    def get(self, url):
        self.calls.append((url, self.clock()))
        self.clock.now += self.response_seconds
        return next(self.responses)


class ConcurrentTaxonomyTransport:
    def __init__(self, parties):
        self.barrier = threading.Barrier(parties)
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def get(self, url):
        query = parse_qs(urlsplit(url).query)["astext"][0]
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.barrier.wait(timeout=2)
        with self.lock:
            self.active -= 1
        return company_search_response((query, f"canonical-{query.casefold()}"))

    def close(self):
        pass


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def target(company_id=1, url="https://www.ambitionbox.com/salaries/ecolab-salaries"):
    return AmbitionBoxTarget(
        company_id=company_id,
        company_name="Ecolab",
        salary_role="Data Scientist",
        slug="ecolab",
        salary_url=url,
    )


def policy(**overrides):
    values = {
        "initial_interval_seconds": 1.5,
        "minimum_interval_seconds": 1.0,
        "maximum_interval_seconds": 12.0,
        "cooldown_seconds": 60.0,
        "success_window": 20,
        "acceleration_seconds": 0.1,
        "max_attempts": 2,
        "max_direct_role_requests": 5,
    }
    values.update(overrides)
    return AmbitionBoxPolicy(**values)


def test_default_policy_uses_faster_guarded_dispatch_intervals():
    default = AmbitionBoxPolicy()

    assert default.initial_interval_seconds == 1.0
    assert default.minimum_interval_seconds == 0.75
    assert default.acceleration_seconds == 0.05
    assert default.cooldown_seconds == 60.0
    assert default.max_attempts == 2


def company_search_response(*companies):
    return Response(
        200,
        json.dumps(
            {
                "resultList": {
                    "merged": [
                        {
                            "type": "company",
                            "name": [name],
                            "tagTwo": [slug],
                            "id": [str(index)],
                        }
                        for index, (name, slug) in enumerate(companies, start=1)
                    ],
                },
            },
        ),
    )


def test_naukri_taxonomy_resolves_canonical_slugs_concurrently_in_input_order():
    transport = ConcurrentTaxonomyTransport(parties=2)
    resolver = NaukriTaxonomyResolver(transport=transport, workers=2)
    targets = [
        AmbitionBoxTarget(
            company_id=index,
            company_name=name,
            salary_role="Data Scientist",
            slug=name.casefold(),
            salary_url=f"https://example.test/{name.casefold()}",
        )
        for index, name in enumerate(("Alpha", "Beta"), start=1)
    ]

    resolutions = resolver.resolve(targets)

    assert transport.max_active == 2
    assert [item.target.company_id for item in resolutions] == [1, 2]
    assert [item.target.slug for item in resolutions] == [
        "canonical-alpha",
        "canonical-beta",
    ]
    assert [item.target.salary_url for item in resolutions] == [
        "https://www.ambitionbox.com/salaries/canonical-alpha-salaries",
        "https://www.ambitionbox.com/salaries/canonical-beta-salaries",
    ]


def test_naukri_taxonomy_requires_judgment_for_a_merely_similar_company():
    taxonomy_transport = Transport(
        [
            company_search_response(
                ("Fedex Kinkos", "fedex-kinkos"),
                ("FedEx Express", "fedex-express"),
            ),
        ],
    )
    resolver = NaukriTaxonomyResolver(transport=taxonomy_transport)
    fedex = AmbitionBoxTarget(
        company_id=7,
        company_name="FedEx",
        salary_role="Data Scientist",
        slug="fedex",
        salary_url="https://www.ambitionbox.com/salaries/fedex-salaries",
    )

    resolution = resolver.resolve([fedex])[0]

    assert resolution.status == "review_required"
    assert resolution.target is None
    assert resolution.evidence["candidates"] == [
        {"name": "Fedex Kinkos", "slug": "fedex-kinkos"},
        {"name": "FedEx Express", "slug": "fedex-express"},
    ]


def test_parent_brand_candidate_still_requires_explicit_judgment():
    taxonomy_transport = Transport(
        [
            company_search_response(),
            company_search_response(("Lear Corporation", "lear-corporation")),
        ],
    )
    resolver = NaukriTaxonomyResolver(transport=taxonomy_transport)
    lear_labs = AmbitionBoxTarget(
        company_id=9,
        company_name="Lear Labs",
        salary_role="Data Scientist",
        slug="lear-labs",
        salary_url="https://www.ambitionbox.com/salaries/lear-labs-salaries",
    )

    resolution = resolver.resolve([lear_labs])[0]

    assert resolution.status == "review_required"
    assert resolution.target is None
    assert resolution.evidence["candidates"] == [
        {"name": "Lear Corporation", "slug": "lear-corporation"},
    ]


def test_naukri_taxonomy_uses_explicit_agent_judgment_not_lexical_rank():
    taxonomy_transport = Transport(
        [
            company_search_response(
                ("Fedex Kinkos", "fedex-kinkos"),
                ("FedEx Express", "fedex-express"),
            ),
        ],
    )
    resolver = NaukriTaxonomyResolver(
        transport=taxonomy_transport,
        judgments={
            7: NaukriCompanyJudgment(
                accepted_slug="fedex-express",
                reason="FedEx Express is the observed employer identity.",
            ),
        },
    )
    fedex = AmbitionBoxTarget(
        company_id=7,
        company_name="FedEx",
        salary_role="Data Scientist",
        slug="fedex",
        salary_url="https://www.ambitionbox.com/salaries/fedex-salaries",
    )

    resolution = resolver.resolve([fedex])[0]

    assert taxonomy_transport.calls == []
    assert resolution.status == "resolved"
    assert resolution.target.slug == "fedex-express"
    assert resolution.evidence["judgment"] == {
        "decision": "accepted",
        "reason": "FedEx Express is the observed employer identity.",
    }


def test_rejected_naukri_judgment_prevents_ambitionbox_requests():
    taxonomy_transport = Transport(
        [company_search_response(("SLB Realty", "slb-realty"))],
    )
    resolver = NaukriTaxonomyResolver(
        transport=taxonomy_transport,
        judgments={
            8: NaukriCompanyJudgment(
                accepted_slug=None,
                reason="No candidate is Schlumberger/SLB.",
            ),
        },
    )
    salary_transport = Transport([])
    collector = AmbitionBoxCollector(
        transport=salary_transport,
        taxonomy_resolver=resolver,
    )
    slb = AmbitionBoxTarget(
        company_id=8,
        company_name="SLB",
        salary_role="Data Scientist",
        slug="slb",
        salary_url="https://www.ambitionbox.com/salaries/slb-salaries",
    )

    observation = list(collector.collect_salaries([slb]))[0]

    assert taxonomy_transport.calls == []
    assert salary_transport.calls == []
    assert observation.status == "missing"
    assert observation.evidence["resolution"]["status"] == "rejected"
    assert observation.evidence["resolution"]["judgment"]["reason"] == (
        "No candidate is Schlumberger/SLB."
    )


def test_reviewed_naukri_judgments_load_by_company_id(tmp_path):
    artifact = tmp_path / "judgments.json"
    artifact.write_text(
        json.dumps(
            {
                "judgments": [
                    {
                        "company_id": 7,
                        "accepted_slug": "fedex-express",
                        "reason": "Verified employer identity.",
                    },
                    {
                        "company_id": 8,
                        "accepted_slug": None,
                        "reason": "No safe match.",
                    },
                ],
            },
        ),
    )

    judgments = load_naukri_company_judgments(artifact)

    assert judgments == {
        7: NaukriCompanyJudgment(
            accepted_slug="fedex-express",
            reason="Verified employer identity.",
        ),
        8: NaukriCompanyJudgment(
            accepted_slug=None,
            reason="No safe match.",
        ),
    }


def test_salary_only_collection_skips_broad_page_and_uses_canonical_role_url():
    taxonomy_transport = Transport(
        [company_search_response(("Ecolab", "ecolab-canonical"))],
    )
    resolver = NaukriTaxonomyResolver(transport=taxonomy_transport, workers=4)
    salary_transport = Transport([Response(200, DETAIL_FIXTURE)])
    collector = AmbitionBoxCollector(
        transport=salary_transport,
        policy=policy(),
        taxonomy_resolver=resolver,
    )

    observation = list(collector.collect_salaries([target()]))[0]

    assert [call[0] for call in salary_transport.calls] == [
        "https://www.ambitionbox.com/salaries/"
        "ecolab-canonical-salaries/data-scientist",
    ]
    assert observation.salary_lpa == 12.6
    assert observation.overall_rating is None
    assert observation.wlb_rating is None
    assert observation.evidence["resolution"]["resolved_slug"] == (
        "ecolab-canonical"
    )


def test_salary_only_collection_caps_ranked_role_requests_at_four():
    taxonomy_transport = Transport(
        [company_search_response(("Ecolab", "ecolab"))],
    )
    resolver = NaukriTaxonomyResolver(transport=taxonomy_transport, workers=4)
    salary_transport = Transport([
        Response(404), Response(404), Response(404), Response(404),
    ])
    collector = AmbitionBoxCollector(
        transport=salary_transport,
        taxonomy_resolver=resolver,
    )

    observation = list(collector.collect_salaries([target()]))[0]

    assert [call[0] for call in salary_transport.calls] == [
        f"{target().salary_url}/data-scientist",
        f"{target().salary_url}/machine-learning-engineer",
        f"{target().salary_url}/artificial-intelligence-engineer",
        f"{target().salary_url}/software-engineer",
    ]
    assert observation.status == "missing"
    assert observation.salary_lpa is None


def test_salary_page_alone_supplies_ratings_and_role_salary():
    transport = Transport([Response(200, FIXTURE)])
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observations = list(collector.collect([target()]))

    assert [call[0] for call in transport.calls] == [target().salary_url]
    assert len(observations) == 1
    observation = observations[0]
    assert observation.status == "ok"
    assert observation.overall_rating == 3.9
    assert observation.wlb_rating == 3.5
    assert observation.salary_lpa == 12.6
    assert observation.evidence["selected_role"] == "Data Scientist"


def test_verified_ey_identity_alias_accepts_ernst_and_young():
    ey_target = AmbitionBoxTarget(
        company_id=1,
        company_name="EY",
        salary_role="Data Scientist",
        slug="ey",
        salary_url="https://www.ambitionbox.com/salaries/ey-salaries",
    )
    ernst_and_young_page = FIXTURE.replace('"Ecolab"', '"Ernst & Young"')
    transport = Transport([Response(200, ernst_and_young_page)])
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observation = list(collector.collect([ey_target]))[0]

    assert observation.status == "ok"
    assert observation.salary_lpa == 12.6
    assert observation.evidence["company"] == "Ernst & Young"


def test_observed_ambitionbox_company_is_accepted_and_recorded():
    fedex_target = AmbitionBoxTarget(
        company_id=1,
        company_name="FedEx",
        salary_role="Data Scientist",
        slug="fedex",
        salary_url="https://www.ambitionbox.com/salaries/fedex-salaries",
    )
    fedex_express_page = FIXTURE.replace('"Ecolab"', '"FedEx Express"')
    transport = Transport([Response(200, fedex_express_page)])
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observation = list(collector.collect([fedex_target]))[0]

    assert observation.status == "ok"
    assert observation.salary_lpa == 12.6
    assert observation.evidence["company"] == "FedEx Express"
    assert observation.evidence["requested_company"] == "FedEx"
    assert observation.evidence["identity_match"] is False


def test_broad_404_resolves_canonical_slug_with_company_search():
    unitedhealth_target = AmbitionBoxTarget(
        company_id=1,
        company_name="UnitedHealth Group",
        salary_role="Data Scientist",
        slug="unitedhealth-group",
        salary_url=(
            "https://www.ambitionbox.com/salaries/"
            "unitedhealth-group-salaries"
        ),
    )
    unitedhealth_page = FIXTURE.replace('"Ecolab"', '"UnitedHealth"')
    transport = Transport(
        [
            Response(404),
            company_search_response(
                ("Optum Global Solutions", "optum-global-solutions"),
                ("UnitedHealth", "unitedhealth"),
            ),
            Response(200, unitedhealth_page),
        ],
    )
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observation = list(collector.collect([unitedhealth_target]))[0]

    assert [call[0] for call in transport.calls] == [
        unitedhealth_target.salary_url,
        (
            "https://taxonomy-suggest.naukri.com/suggest/abcommonsuggest"
            "?astext=UnitedHealth%20Group&appId=112&category=ab_company"
            "&limit=25&callback=_1684833062882"
            "&resultField=tagOne,tagTwo,tagThree,tagFour,tagFive,tagSix,"
            "tagSeven,tagEight,type,id,name&matchValueFlag=true"
            "&fuzzyEnableFlag=true"
        ),
        "https://www.ambitionbox.com/salaries/unitedhealth-salaries",
    ]
    assert observation.status == "ok"
    assert observation.salary_lpa == 12.6
    assert observation.evidence["company"] == "UnitedHealth"
    assert observation.evidence["resolution"] == {
        "status": "resolved",
        "method": "ambitionbox_search",
        "query": "UnitedHealth Group",
        "requested_company": "UnitedHealth Group",
        "requested_slug": "unitedhealth-group",
        "requested_url": unitedhealth_target.salary_url,
        "resolved_company": "UnitedHealth",
        "resolved_slug": "unitedhealth",
        "resolved_url": (
            "https://www.ambitionbox.com/salaries/unitedhealth-salaries"
        ),
        "search_attempts": [
            {"query": "UnitedHealth Group", "candidate_count": 2},
        ],
    }


def test_broad_404_uses_one_parent_brand_search_when_exact_search_is_empty():
    amazon_science_target = AmbitionBoxTarget(
        company_id=1,
        company_name="Amazon Science",
        salary_role="Data Scientist",
        slug="amazon-science",
        salary_url=(
            "https://www.ambitionbox.com/salaries/amazon-science-salaries"
        ),
    )
    amazon_page = FIXTURE.replace('"Ecolab"', '"Amazon"')
    transport = Transport(
        [
            Response(404),
            company_search_response(),
            company_search_response(("Amazon", "amazon")),
            Response(200, amazon_page),
        ],
    )
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observation = list(collector.collect([amazon_science_target]))[0]

    assert [call[0] for call in transport.calls] == [
        amazon_science_target.salary_url,
        (
            "https://taxonomy-suggest.naukri.com/suggest/abcommonsuggest"
            "?astext=Amazon%20Science&appId=112&category=ab_company"
            "&limit=25&callback=_1684833062882"
            "&resultField=tagOne,tagTwo,tagThree,tagFour,tagFive,tagSix,"
            "tagSeven,tagEight,type,id,name&matchValueFlag=true"
            "&fuzzyEnableFlag=true"
        ),
        (
            "https://taxonomy-suggest.naukri.com/suggest/abcommonsuggest"
            "?astext=Amazon&appId=112&category=ab_company"
            "&limit=25&callback=_1684833062882"
            "&resultField=tagOne,tagTwo,tagThree,tagFour,tagFive,tagSix,"
            "tagSeven,tagEight,type,id,name&matchValueFlag=true"
            "&fuzzyEnableFlag=true"
        ),
        "https://www.ambitionbox.com/salaries/amazon-salaries",
    ]
    assert observation.status == "ok"
    assert observation.salary_lpa == 12.6
    assert observation.evidence["resolution"]["method"] == "parent_brand_search"
    assert observation.evidence["resolution"]["query"] == "Amazon"
    assert observation.evidence["resolution"]["search_attempts"] == [
        {"query": "Amazon Science", "candidate_count": 0},
        {"query": "Amazon", "candidate_count": 1},
    ]


def test_broad_page_uses_new_software_engineer_role_fallback():
    broad_with_software_salary = FIXTURE.replace(
        '"Data Scientist"',
        '"Software Engineer"',
    )
    transport = Transport([Response(200, broad_with_software_salary)])
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observation = list(collector.collect([target()]))[0]

    assert [call[0] for call in transport.calls] == [target().salary_url]
    assert observation.status == "ok"
    assert observation.salary_lpa == 12.6
    assert observation.evidence["selected_role"] == "Software Engineer"


@pytest.mark.parametrize(
    ("requested_role", "available_role"),
    [
        ("ML Engineer - Advanced Analytics", "Machine Learning Engineer"),
        ("Data Engineer, Advanced Analytics", "Data Engineer"),
    ],
)
def test_specific_role_beats_generic_analytics_keyword(
    requested_role,
    available_role,
):
    specific_target = AmbitionBoxTarget(
        **{
            **target().__dict__,
            "salary_role": requested_role,
        },
    )
    broad_with_specific_role = FIXTURE.replace(
        '"Data Scientist"',
        f'"{available_role}"',
    )
    transport = Transport(
        [Response(200, broad_with_specific_role), *(Response(404) for _ in range(4))],
    )
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observation = list(collector.collect([specific_target]))[0]

    assert [call[0] for call in transport.calls] == [target().salary_url]
    assert observation.salary_lpa == 12.6
    assert observation.evidence["selected_role"] == available_role


def test_software_engineer_target_uses_its_exact_broad_role():
    software_target = AmbitionBoxTarget(
        company_id=1,
        company_name="Ecolab",
        salary_role="Software Engineer",
        slug="ecolab",
        salary_url=target().salary_url,
    )
    broad_with_software_salary = FIXTURE.replace(
        '"Data Scientist"',
        '"Software Engineer"',
    )
    transport = Transport([Response(200, broad_with_software_salary)])
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observation = list(collector.collect([software_target]))[0]

    assert [call[0] for call in transport.calls] == [target().salary_url]
    assert observation.salary_lpa == 12.6
    assert observation.evidence["selected_role"] == "Software Engineer"


def test_missing_broad_role_uses_one_paced_direct_role_request():
    clock = Clock()
    broad_without_compatible_role = FIXTURE.replace(
        '"Data Scientist"', '"Marketing Manager"',
    )
    transport = Transport(
        [Response(200, broad_without_compatible_role), Response(200, DETAIL_FIXTURE)],
        clock,
    )
    collector = AmbitionBoxCollector(
        transport=transport,
        policy=policy(),
        monotonic=clock,
        sleep=clock.sleep,
    )

    observation = list(collector.collect([target()]))[0]

    assert [call[0] for call in transport.calls] == [
        target().salary_url,
        f"{target().salary_url}/data-scientist",
    ]
    assert [call[1] for call in transport.calls] == [0.0, 1.5]
    assert observation.status == "ok"
    assert observation.overall_rating == 3.9
    assert observation.wlb_rating == 3.5
    assert observation.salary_lpa == 12.6
    assert observation.evidence["selected_role"] == "Data Scientist"
    assert observation.evidence["salary_lookup"] == {
        "status": "ok",
        "url": f"{target().salary_url}/data-scientist",
        "salary_count": 42,
    }


def test_known_successful_broad_observation_skips_directly_to_salary_lookup():
    clock = Clock()
    known_broad = AmbitionBoxObservation(
        company_id=1,
        status="ok",
        overall_rating=3.9,
        wlb_rating=3.5,
        salary_lpa=None,
        evidence={
            "status": "ok",
            "company": "Ecolab",
            "url": target().salary_url,
            "slug": "ecolab",
            "selected_role": None,
        },
    )
    recovery_target = AmbitionBoxTarget(
        **{
            **target().__dict__,
            "known_broad_observation": known_broad,
        },
    )
    transport = Transport([Response(200, DETAIL_FIXTURE)], clock)
    collector = AmbitionBoxCollector(
        transport=transport,
        policy=policy(),
        monotonic=clock,
        sleep=clock.sleep,
    )

    observation = list(collector.collect([recovery_target]))[0]

    assert transport.calls == [
        (f"{target().salary_url}/data-scientist", 0.0),
    ]
    assert clock.sleeps == []
    assert observation.status == "ok"
    assert observation.overall_rating == 3.9
    assert observation.wlb_rating == 3.5
    assert observation.salary_lpa == 12.6


def test_direct_salary_lookup_follows_role_ranking_until_salary_is_found():
    clock = Clock()
    known_broad = AmbitionBoxObservation(
        company_id=1,
        status="ok",
        overall_rating=3.9,
        wlb_rating=3.5,
        salary_lpa=None,
        evidence={
            "status": "ok",
            "company": "Ecolab",
            "url": target().salary_url,
            "slug": "ecolab",
            "selected_role": None,
        },
    )
    recovery_target = AmbitionBoxTarget(
        **{
            **target().__dict__,
            "known_broad_observation": known_broad,
        },
    )
    machine_learning_detail = DETAIL_FIXTURE.replace(
        "Data Scientist",
        "Machine Learning Engineer",
    ).replace("data-scientist", "machine-learning-engineer")
    transport = Transport(
        [Response(404), Response(200, machine_learning_detail)],
        clock,
    )
    collector = AmbitionBoxCollector(
        transport=transport,
        policy=policy(),
        monotonic=clock,
        sleep=clock.sleep,
    )

    observation = list(collector.collect([recovery_target]))[0]

    assert transport.calls == [
        (f"{target().salary_url}/data-scientist", 0.0),
        (f"{target().salary_url}/machine-learning-engineer", 1.5),
    ]
    assert observation.status == "ok"
    assert observation.salary_lpa == 12.6
    assert observation.evidence["selected_role"] == "Machine Learning Engineer"


def test_direct_salary_lookup_accepts_only_the_profile_it_queried():
    known_broad = AmbitionBoxObservation(
        company_id=1,
        status="ok",
        overall_rating=3.9,
        wlb_rating=3.5,
        salary_lpa=None,
        evidence={
            "status": "ok",
            "company": "Ecolab",
            "url": target().salary_url,
            "slug": "ecolab",
            "selected_role": None,
        },
    )
    recovery_target = AmbitionBoxTarget(
        **{
            **target().__dict__,
            "known_broad_observation": known_broad,
        },
    )
    machine_learning_detail = DETAIL_FIXTURE.replace(
        "Data Scientist",
        "Machine Learning Engineer",
    ).replace("data-scientist", "machine-learning-engineer")
    transport = Transport(
        [
            Response(200, machine_learning_detail),
            Response(200, machine_learning_detail),
        ],
    )
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observation = list(collector.collect([recovery_target]))[0]

    assert [call[0] for call in transport.calls] == [
        f"{target().salary_url}/data-scientist",
        f"{target().salary_url}/machine-learning-engineer",
    ]
    assert observation.salary_lpa == 12.6
    assert observation.evidence["selected_role"] == "Machine Learning Engineer"


def test_null_direct_salary_bound_continues_to_the_next_ranked_role():
    known_broad = AmbitionBoxObservation(
        company_id=1,
        status="ok",
        overall_rating=3.9,
        wlb_rating=3.5,
        salary_lpa=None,
        evidence={
            "status": "ok",
            "company": "Ecolab",
            "url": target().salary_url,
            "slug": "ecolab",
            "selected_role": None,
        },
    )
    recovery_target = AmbitionBoxTarget(
        **{
            **target().__dict__,
            "known_broad_observation": known_broad,
        },
    )
    null_salary_detail = DETAIL_FIXTURE.replace(
        '"typicalMinCtc": "1100000"',
        '"typicalMinCtc": null',
    )
    machine_learning_detail = DETAIL_FIXTURE.replace(
        "Data Scientist",
        "Machine Learning Engineer",
    ).replace("data-scientist", "machine-learning-engineer")
    transport = Transport(
        [Response(200, null_salary_detail), Response(200, machine_learning_detail)],
    )
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observation = list(collector.collect([recovery_target]))[0]

    assert observation.status == "ok"
    assert observation.salary_lpa == 12.6
    assert observation.evidence["selected_role"] == "Machine Learning Engineer"
    assert [
        item["status"]
        for item in observation.evidence["salary_lookup"]["role_attempts"]
    ] == ["missing", "ok"]


def test_direct_salary_lookup_uses_the_fourth_ranked_profile():
    clock = Clock()
    known_broad = AmbitionBoxObservation(
        company_id=1,
        status="ok",
        overall_rating=3.9,
        wlb_rating=3.5,
        salary_lpa=None,
        evidence={
            "status": "ok",
            "company": "Ecolab",
            "url": target().salary_url,
            "slug": "ecolab",
            "selected_role": None,
        },
    )
    recovery_target = AmbitionBoxTarget(
        **{
            **target().__dict__,
            "known_broad_observation": known_broad,
        },
    )
    software_engineer_detail = DETAIL_FIXTURE.replace(
        "Data Scientist",
        "Software Engineer",
    ).replace("data-scientist", "software-engineer")
    transport = Transport(
        [
            Response(404),
            Response(404),
            Response(404),
            Response(200, software_engineer_detail),
        ],
        clock,
    )
    collector = AmbitionBoxCollector(
        transport=transport,
        policy=policy(),
        monotonic=clock,
        sleep=clock.sleep,
    )

    observation = list(collector.collect([recovery_target]))[0]

    assert transport.calls == [
        (f"{target().salary_url}/data-scientist", 0.0),
        (f"{target().salary_url}/machine-learning-engineer", 1.5),
        (f"{target().salary_url}/artificial-intelligence-engineer", 3.0),
        (f"{target().salary_url}/software-engineer", 4.5),
    ]
    assert observation.status == "ok"
    assert observation.salary_lpa == 12.6
    assert observation.evidence["selected_role"] == "Software Engineer"
    assert observation.evidence["salary_lookup"]["status"] == "ok"
    assert len(observation.evidence["salary_lookup"]["role_attempts"]) == 4


def test_direct_salary_lookup_uses_the_fifth_ranked_profile():
    known_broad = AmbitionBoxObservation(
        company_id=1,
        status="ok",
        overall_rating=3.9,
        wlb_rating=3.5,
        salary_lpa=None,
        evidence={
            "status": "ok",
            "company": "Ecolab",
            "url": target().salary_url,
            "slug": "ecolab",
            "selected_role": None,
        },
    )
    ai_target = AmbitionBoxTarget(
        **{
            **target().__dict__,
            "salary_role": "AI Engineer",
            "known_broad_observation": known_broad,
        },
    )
    data_engineer_detail = DETAIL_FIXTURE.replace(
        "Data Scientist",
        "Data Engineer",
    ).replace("data-scientist", "data-engineer")
    transport = Transport(
        [
            Response(404),
            Response(404),
            Response(404),
            Response(404),
            Response(200, data_engineer_detail),
        ],
    )
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observation = list(collector.collect([ai_target]))[0]

    assert [call[0] for call in transport.calls] == [
        f"{target().salary_url}/artificial-intelligence-engineer",
        f"{target().salary_url}/machine-learning-engineer",
        f"{target().salary_url}/data-scientist",
        f"{target().salary_url}/software-engineer",
        f"{target().salary_url}/data-engineer",
    ]
    assert observation.salary_lpa == 12.6
    assert observation.evidence["selected_role"] == "Data Engineer"
    assert len(observation.evidence["salary_lookup"]["role_attempts"]) == 5


def test_known_broad_observation_with_unroutable_role_makes_no_request():
    known_broad = AmbitionBoxObservation(
        company_id=1,
        status="ok",
        overall_rating=3.9,
        wlb_rating=3.5,
        salary_lpa=None,
        evidence={
            "status": "ok",
            "company": "Ecolab",
            "url": target().salary_url,
            "slug": "ecolab",
            "selected_role": None,
        },
    )
    recovery_target = AmbitionBoxTarget(
        company_id=1,
        company_name="Ecolab",
        salary_role="Quantitative Analyst",
        slug="ecolab",
        salary_url=target().salary_url,
        known_broad_observation=known_broad,
    )
    transport = Transport([])
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observation = list(collector.collect([recovery_target]))[0]

    assert transport.calls == []
    assert observation.status == "ok"
    assert observation.overall_rating == 3.9
    assert observation.salary_lpa is None
    assert observation.evidence["salary_lookup"]["status"] == "missing"
    assert observation.evidence["salary_lookup"]["retryable"] is False


def test_missing_direct_role_is_terminal_but_preserves_broad_ratings():
    broad_without_compatible_role = FIXTURE.replace(
        '"Data Scientist"', '"Marketing Manager"',
    )
    transport = Transport(
        [
            Response(200, broad_without_compatible_role),
            Response(404),
            Response(404),
            Response(404),
            Response(404),
            Response(404),
            Response(200, FIXTURE),
        ],
    )
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observations = list(
        collector.collect(
            [
                target(1),
                target(2, "https://www.ambitionbox.com/salaries/ecolab-2-salaries"),
            ],
        ),
    )

    assert [call[0] for call in transport.calls] == [
        target().salary_url,
        f"{target().salary_url}/data-scientist",
        f"{target().salary_url}/machine-learning-engineer",
        f"{target().salary_url}/artificial-intelligence-engineer",
        f"{target().salary_url}/software-engineer",
        f"{target().salary_url}/data-engineer",
        "https://www.ambitionbox.com/salaries/ecolab-2-salaries",
    ]
    assert [item.status for item in observations] == ["ok", "ok"]
    assert observations[0].overall_rating == 3.9
    assert observations[0].wlb_rating == 3.5
    assert observations[0].salary_lpa is None
    salary_lookup = observations[0].evidence["salary_lookup"]
    assert salary_lookup["status"] == "missing"
    assert salary_lookup["retryable"] is False
    assert [item["role"] for item in salary_lookup["role_attempts"]] == [
        "Data Scientist",
        "Machine Learning Engineer",
        "Artificial Intelligence Engineer",
        "Software Engineer",
        "Data Engineer",
    ]
    assert all(
        item["status"] == "missing"
        for item in salary_lookup["role_attempts"]
    )


def test_repeated_403_on_direct_role_preserves_ratings_and_opens_global_circuit():
    clock = Clock()
    broad_without_compatible_role = FIXTURE.replace(
        '"Data Scientist"', '"Marketing Manager"',
    )
    transport = Transport(
        [Response(200, broad_without_compatible_role), Response(403), Response(403)],
        clock,
    )
    collector = AmbitionBoxCollector(
        transport=transport,
        policy=policy(),
        monotonic=clock,
        sleep=clock.sleep,
    )

    observations = list(
        collector.collect(
            [
                target(1),
                target(2, "https://www.ambitionbox.com/salaries/ecolab-2-salaries"),
            ],
        ),
    )

    assert [call[0] for call in transport.calls] == [
        target().salary_url,
        f"{target().salary_url}/data-scientist",
        f"{target().salary_url}/data-scientist",
    ]
    assert [call[1] for call in transport.calls] == [0.0, 1.5, 61.5]
    assert clock.sleeps == [1.5, 60.0]
    assert [item.status for item in observations] == ["error", "deferred"]
    assert observations[0].overall_rating == 3.9
    assert observations[0].wlb_rating == 3.5
    assert observations[0].salary_lpa is None
    assert observations[0].evidence["salary_lookup"]["status"] == "error"
    assert observations[0].evidence["salary_lookup"]["retryable"] is True
    assert observations[1].overall_rating is None


def test_dispatch_is_globally_paced_and_accelerates_after_success_window():
    clock = Clock()
    transport = Transport(
        [Response(200, FIXTURE), Response(200, FIXTURE), Response(200, FIXTURE)],
        clock,
    )
    collector = AmbitionBoxCollector(
        transport=transport,
        policy=policy(success_window=2, acceleration_seconds=0.5),
        monotonic=clock,
        sleep=clock.sleep,
    )
    targets = [target(index, f"https://example.test/{index}") for index in range(3)]

    observations = list(collector.collect(targets))

    assert [item.status for item in observations] == ["ok", "ok", "ok"]
    assert [call[1] for call in transport.calls] == [0.0, 1.5, 2.5]
    assert clock.sleeps == [1.5, 1.0]


def test_dispatch_interval_is_measured_from_request_start_not_response_end():
    clock = Clock()
    transport = SlowTransport(
        [Response(200, FIXTURE), Response(200, FIXTURE)],
        clock,
        response_seconds=0.4,
    )
    collector = AmbitionBoxCollector(
        transport=transport,
        policy=policy(initial_interval_seconds=1.0),
        monotonic=clock,
        sleep=clock.sleep,
    )

    observations = list(
        collector.collect(
            [target(1, "https://example.test/1"), target(2, "https://example.test/2")],
        ),
    )

    assert [item.status for item in observations] == ["ok", "ok"]
    assert [call[1] for call in transport.calls] == [0.0, 1.0]
    assert clock.sleeps == [0.6]


def test_repeated_403_opens_global_circuit_and_defers_unrequested_targets():
    clock = Clock()
    transport = Transport([Response(403), Response(403)], clock)
    collector = AmbitionBoxCollector(
        transport=transport,
        policy=policy(),
        monotonic=clock,
        sleep=clock.sleep,
    )
    targets = [target(index, f"https://example.test/{index}") for index in range(3)]

    observations = list(collector.collect(targets))

    assert [call[0] for call in transport.calls] == [
        "https://example.test/0",
        "https://example.test/0",
    ]
    assert clock.sleeps == [60.0]
    assert [item.status for item in observations] == [
        "error",
        "deferred",
        "deferred",
    ]
    assert "HTTP 403" in observations[0].evidence["error"]


def test_404_uses_one_bounded_search_then_remains_terminal_without_a_match():
    transport = Transport(
        [Response(404), company_search_response(), Response(200, FIXTURE)],
    )
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observations = list(
        collector.collect([target(1, "https://example.test/missing"), target(2)])
    )

    assert [item.status for item in observations] == ["missing", "ok"]
    assert len(transport.calls) == 3
    assert "taxonomy-suggest.naukri.com" in transport.calls[1][0]


def test_duplicate_salary_urls_are_fetched_once_and_parsed_per_target():
    transport = Transport([Response(200, FIXTURE)])
    collector = AmbitionBoxCollector(transport=transport, policy=policy())

    observations = list(collector.collect([target(1), target(2)]))

    assert len(transport.calls) == 1
    assert [item.company_id for item in observations] == [1, 2]
    assert all(item.status == "ok" for item in observations)
