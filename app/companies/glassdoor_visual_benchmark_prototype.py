"""PROTOTYPE: benchmark 100 Glassdoor profiles with 10 visible parallel tabs.

Question: Is one visible persistent Chrome session, with bounded concurrency,
fast and stable enough for large-scale company profiling?

Run: .venv/bin/python -m app.companies.glassdoor_visual_benchmark_prototype
The benchmark reads employer IDs from Postgres but performs no database writes.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from statistics import mean, median
from time import perf_counter

from patchright.async_api import async_playwright
from sqlalchemy import text

from app.db.session import SessionLocal


SAMPLE_SIZE = 100
CONCURRENCY = 10
PAGE_TIMEOUT_SECONDS = 45


def _companies() -> list[dict]:
    session = SessionLocal()
    try:
        rows = session.execute(text("""
            SELECT DISTINCT ON (employer_id)
                employer_id,
                employer_name
            FROM (
                SELECT
                    raw_payload::jsonb #>> '{jobview,header,employer,id}' AS employer_id,
                    COALESCE(
                        raw_payload::jsonb #>> '{jobview,header,employer,shortName}',
                        raw_payload::jsonb #>> '{jobview,header,employer,name}',
                        raw_payload::jsonb #>> '{jobview,header,employerNameFromSearch}'
                    ) AS employer_name,
                    last_seen_at
                FROM jobs
                WHERE source = 'glassdoor'
            ) employers
            WHERE employer_id IS NOT NULL AND employer_name IS NOT NULL
            ORDER BY employer_id, last_seen_at DESC
            LIMIT :limit
        """), {"limit": SAMPLE_SIZE}).all()
    finally:
        session.close()

    result = []
    for employer_id, name in rows:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
        result.append({
            "name": name,
            "overview": (
                f"https://www.glassdoor.co.in/Overview/Working-at-{slug}-"
                f"EI_IE{employer_id}.11%2C{11 + len(slug)}.htm"
            ),
            "salary": (
                f"https://www.glassdoor.co.in/Salary/{slug}-Salaries-"
                f"E{employer_id}.htm"
            ),
        })
    return result


async def _load(page, kind: str, url: str) -> dict:
    started = perf_counter()
    navigation_error = None
    try:
        await page.goto(url, timeout=60_000, wait_until="domcontentloaded")
    except Exception as exc:
        navigation_error = type(exc).__name__

    ready = False
    data_found = False
    challenged = False
    deadline = perf_counter() + PAGE_TIMEOUT_SECONDS
    title = ""
    while perf_counter() < deadline:
        try:
            title = await page.title()
            body = await page.locator("body").inner_text(timeout=5_000)
            combined = f"{title} {body}".lower()
            challenged = any(
                phrase in combined
                for phrase in (
                    "verify you are human", "security check", "captcha", "access denied",
                )
            )
            if kind == "overview":
                data_found = await page.locator("script").evaluate_all(
                    "els => els.some(el => el.textContent.includes('overallRating'))"
                )
                ready = data_found
            else:
                data_found = await page.locator('[data-test="salary-item"]').count() > 0
                ready = data_found or (
                    "salary" in title.lower() and len(body) > 1_000 and not challenged
                )
            if ready or challenged:
                break
        except Exception:
            pass
        await asyncio.sleep(0.5)

    return {
        "seconds": round(perf_counter() - started, 1),
        "ready": ready,
        "data_found": data_found,
        "challenged": challenged,
        "error": navigation_error,
        "title": title,
    }


async def _benchmark_company(context, semaphore, index: int, company: dict) -> dict:
    async with semaphore:
        page = await context.new_page()
        started = perf_counter()
        try:
            overview = await _load(page, "overview", company["overview"])
            salary = await _load(page, "salary", company["salary"])
        finally:
            await page.close()
        result = {
            "company": company["name"],
            "seconds": round(perf_counter() - started, 1),
            "overview": overview,
            "salary": salary,
        }
        print(
            f"[{index:03d}/{SAMPLE_SIZE}] {company['name']}: "
            f"{result['seconds']:.1f}s "
            f"overview={'ok' if overview['data_found'] else 'missing'} "
            f"salary={'ok' if salary['data_found'] else 'missing'} "
            f"challenge={overview['challenged'] or salary['challenged']}",
            flush=True,
        )
        return result


def _percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[round((len(values) - 1) * fraction)]


async def main() -> None:
    companies = _companies()
    profile_dir = Path.home() / "job_agent" / "glassdoor-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    wall_started = perf_counter()
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            channel="chrome",
        )
        try:
            semaphore = asyncio.Semaphore(CONCURRENCY)
            results = await asyncio.gather(*(
                _benchmark_company(context, semaphore, index, company)
                for index, company in enumerate(companies, 1)
            ))
        finally:
            await context.close()

    durations = [result["seconds"] for result in results]
    challenged = [
        result["company"] for result in results
        if result["overview"]["challenged"] or result["salary"]["challenged"]
    ]
    summary = {
        "sample_size": len(results),
        "concurrency": CONCURRENCY,
        "total_wall_seconds": round(perf_counter() - wall_started, 1),
        "mean_company_seconds": round(mean(durations), 1),
        "median_company_seconds": round(median(durations), 1),
        "p95_company_seconds": round(_percentile(durations, 0.95), 1),
        "overview_data_found": sum(r["overview"]["data_found"] for r in results),
        "salary_data_found": sum(r["salary"]["data_found"] for r in results),
        "challenges": len(challenged),
        "challenged_companies": challenged,
        "incomplete_companies": [
            result["company"] for result in results
            if not result["overview"]["ready"] or not result["salary"]["ready"]
        ],
    }
    print(f"SUMMARY {summary}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
