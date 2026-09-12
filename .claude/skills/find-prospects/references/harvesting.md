# Stage A: harvesting candidate names

The goal of Stage A is **volume at zero cost**. Judgement happens in Stage B;
here you are only trying to get a large pool of plausible company names into
a file, cheaply, without wandering off-thesis.

## Directory-first, not person-first — the measurement

Two approaches were probed live with free `WebSearch` on 2026-08-05, while
designing this skill.

**Person-first** — `"Head of Data Science" Bangalore site:in.linkedin.com`

| | |
|---|---|
| results returned | 8 |
| real profiles | **3** |
| LinkedIn *jobs* index pages (noise) | 5 |
| companies yielded | ~3 |

**Directory-first** — `Bangalore fintech lending startups with data science teams 2026`

Returned Tracxn, Wellfound, GrowthList and 18startup listing pages — each one
`WebFetch`-able for dozens of names. Roughly **20–50 companies per
search+fetch pair.**

Directory-first wins by an order of magnitude per unit of effort, which is
why it is the default. Person-first stays available as a **supplementary
generator** for a thesis Stage A under-covers — reach for it when a thesis
returns thin, not as your opening move.

## The hard rule: trust titles and URLs, never the summarizer

In the person-first probe, the search summarizer reported:

> "Aditya Patel — Head of Data Science at MPL (Mobile Premier League) in
> Bengaluru, actively hiring for Data Scientists and ML Engineers."

The actual result it was summarizing read:

> `Aditya Patel - Software Engineer - Facebook | LinkedIn`

Different title, different company, and a hiring claim that appears nowhere
in the source. This is a fabricated attribution in the layer *between* you
and the search results.

**Therefore: read the result `title` and `url` fields. Treat the summarizer's
prose as unverified.** If a company name only exists in the prose and not in
a title, a URL, or a page you fetched, it is not evidence — either fetch the
page to confirm it or drop it.

### Three confirmed fabrications from the first live run (2026-08-05)

The rule above is not theoretical. In one batch of 33 candidates it caught
three separate false positives that would each have produced a bogus
qualified prospect:

1. **Moov** — the search summary asserted "Moov has offices in India,
   including locations in Bengaluru, Chennai, Delhi, Hyderabad, Kolkata,
   Mumbai, and Pune." No result *title* mentioned India. `moov.io/careers`
   states verbatim: **"Our team spans the U.S."**
2. **Hiro Systems** — summary asserted "Hiro has locations including
   Singapore and Bengaluru." `hiro.so/careers` lists 13 locations
   (Amsterdam, NYC, Buenos Aires, Innsbruck, SF, Monterrey, NC, Sarasota,
   Paris, CT, Montana, Toulouse, Maryland) — **no India**.
3. **Snap! Mobile** — a *name collision*, not a fabrication: the search
   returned Snap Inc./Snapchat's Mumbai office. Snap! Mobile is an unrelated
   school-fundraising payments company.

**The boilerplate tell.** That identical Indian-city list — "Bengaluru,
Chennai, Delhi, Hyderabad, Kolkata, Mumbai, and Pune" — surfaced again for
**Bloom Credit**, a different company entirely. It is page furniture on
BuiltIn-family sites (a location picker), not company data. If you see that
exact seven-city sequence, it is never evidence of an India office.

**Cheap defence:** one uncharged `WebFetch` of the company's own careers page
settles it. Two of the three above were killed that way in a single call.

The same rule applies to `WebFetch` output: it answers your prompt against
the page, so keep the prompt extractive ("list every company name appearing
in the listing table") rather than interpretive ("which of these companies
have data teams") — the latter invites the same fabrication, and it is Stage
B's job anyway.

## Aggregators that returned usable listing pages

Confirmed live 2026-08-05 for the India fintech query. Re-check rather than
assume — aggregators paywall and restructure constantly.

- **Tracxn** — sector- and city-scoped company lists, the most structured.
- **Wellfound** (formerly AngelList) — `/startups/l/<city>/<sector>` paths.
- **GrowthList** — funded-company lists with funding metadata.
- **18startup**, **KnowStartup**, **Inc42**, **YourStory** — listicles; more
  editorial, more stale, but they surface companies the structured
  aggregators miss.

Two more seams worth trying when a thesis is thin:

- **Funding announcements.** A Series A–C round is a strong proxy for "hiring
  capacity, no posting yet" — precisely the cold-email window.
- **"Alternatives to X" / "competitors of X"** pages, seeded from a company
  you already qualified. High precision, since the thesis is already proven.

## Bias you are accepting

Directory pages skew toward companies that are **funded, well-known, and
paying for visibility.** Quieter companies — often the ones where a cold
email actually gets read — are underrepresented.

This was an explicit trade, not an oversight. The mitigation, when you want
that long tail, is the person-first supplement plus the "alternatives to X"
seam above.

## Practical shape of a harvest

1. Run a seed query with `WebSearch`.
2. `WebFetch` each listing page it returned, prompting extractively for
   company names.
3. Append names, one per line, to a scratch file. Don't filter yet — dedup in
   step 3 is free and cheaper than your judgement.
4. Two or three seed queries per thesis is usually enough to exceed the
   20-qualified target after dedup and Stage B attrition.

Keep the exact query and the exact page URL — step 3 stores them on every row
as `source_query` / `source_url`, which is what makes "have we already mined
this directory?" a database question rather than something you have to
remember between sessions.
