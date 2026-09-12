<h1 align="center">Job Atlas</h1>
<p align="center"><strong>Your AI-assisted job search, built for India.</strong></p>
<p align="center">Find opportunities. Understand the requirements. Choose where to go next.</p>

<p align="center">
  <a href="#get-started">Get started</a> ·
  <a href="#how-your-search-works">Search stages</a> ·
  <a href="#what-you-provide">What you provide</a> ·
  <a href="#choose-a-workflow">Workflows</a> ·
  <a href="#your-dashboard-and-workbook">Your results</a>
</p>

![Job Atlas turns your role, experience, and location preferences into a reviewed job search, company context, and a shortlist. Keep your results in a local dashboard and Excel workbook, then choose optional preparation.](docs/assets/job-search-hero.svg)

<p align="center">
  <a href="https://pypi.org/project/job-atlas/"><img alt="Install from PyPI" src="https://img.shields.io/pypi/v/job-atlas?style=flat-square&amp;color=27665a"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-16352f?style=flat-square"></a>
  <img alt="Status: public beta" src="https://img.shields.io/badge/Status-Public%20beta-e4b363?style=flat-square">
  <img alt="Python 3.11 or newer" src="https://img.shields.io/badge/Python-3.11%2B-263849?style=flat-square">
  <img alt="Windows, macOS and Linux" src="https://img.shields.io/badge/Windows%20%C2%B7%20macOS%20%C2%B7%20Linux-Local%20workspace-263849?style=flat-square">
</p>

## What is Job Atlas?

**Job Atlas is an open-source tool that helps you run a job search with your coding agent.** Describe the work you want, review a proposed search, and bring listings from multiple job boards into one place. The agent helps interpret job requirements, research employers, and prepare for the opportunities you choose.

The Python application handles collection, saved results, the dashboard, and exports. Its **skills** are instructions your coding agent follows to coordinate the work and make evidence-based judgments. You work in the agent's chat, browse results in your local dashboard, and review a portable Excel workbook.

The current public beta supports **jobs in India and remote roles open to applicants in India**. You can configure different professions, experience ranges, Indian cities, and work arrangements. Coverage varies by board and profession; the search plan explains which sources fit your request.

You can use it to:

- **Find and compare jobs** across supported boards, with original links and screening reasons.
- **Understand jobs and companies** through structured requirements, employer context, and optional salary or workplace research.
- **Choose your shortlist** in conversation or through the exported workbook.
- **Prepare for selected roles** with truthful tailored resumes, relevant LinkedIn profile links, and separate interview practice.
- **Draft outreach when requested**, through a separate workflow with its own setup and Gmail review step.

Job Atlas is for job seekers comfortable using a terminal and a coding agent that can read local skills, run commands, and research on the web. Your agent supplies the AI reasoning; its account and usage costs are separate from Job Atlas.

## Get started

### 1. Install the application

You'll need **Python 3.11+**, a compatible coding agent, and an internet connection. Job Atlas runs on **Windows, macOS, and Linux** and uses a private local SQLite database by default.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run these commands in your terminal:

```bash
uv tool install job-atlas
job-atlas setup
job-atlas doctor
```

This installs the published package in its own environment. **You do not need to clone this repository.** `setup` creates your private workspace under `~/.job-atlas` and installs seven user-facing skills in `~/.agents/skills`. Here, `~` means your user home directory.

**Restart your coding agent after setup** so it can discover the installed skills. If you already use another Python installer, `pipx install job-atlas` or `pip install job-atlas` are alternatives; run the same setup commands afterward.

### 2. Start a search in your coding agent

Enter this prompt in the agent's chat, replacing the preferences with your own:

```text
$full-pipeline
Find digital marketing jobs for someone with 3 years of experience.
Include onsite or hybrid roles in Pune and Mumbai, plus remote roles
open to applicants in India. Search the last 14 days.
Propose a small set of suitable boards and show me the plan first.
Start with jobs, company context, and an Excel workbook.
```

The agent shows you the roles, locations, work arrangements, job types, named sources, setup requirements, and amount of search work. Review and accept that plan before collection begins. It can propose a broader second pass if the first search leaves useful coverage gaps.

**A resume is optional for the initial search.** Add it when you want tailoring or resume-grounded interview practice. The basic guided search also works without Gmail or a paid research provider; optional features have their own prerequisites below.

### 3. Open your workspace

Run this in another terminal:

```bash
job-atlas-dashboard
```

Open **<http://127.0.0.1:8501>**, choose your search, and use **Refresh dashboard** to see the latest saved progress. When the workbook is ready, review it and tell the agent which optional next steps you want.

[Detailed setup and command reference →](docs/technical-guide.md#quick-start)

## What you provide

Start with ordinary language. The agent can turn your preferences into the structured search profile and help prepare the other inputs when needed.

| When you want to… | Provide… | Example |
|---|---|---|
| **Start a search** | Target roles, experience, locations, and work arrangements. | “Business analyst, 2–4 years, Hyderabad or Pune, hybrid or India-eligible remote.” |
| **Refine the search** | Related titles to include, roles to exclude, job types, posting period, and any preferred boards. | “Include product analyst; exclude sales. Include internships.” |
| **Choose jobs** | Keep all eligible results, name a manual shortlist, describe filters, or return the reviewed workbook. | “Use only the jobs I left Approved in this file.” |
| **Tailor resumes** | A reviewed resume master containing your real experience, education, projects, and skills, plus your chosen jobs. | “Use this confirmed master for these three shortlisted roles.” |
| **Add deeper research or profile links** | The selected research scope, required provider access, and a spending or provider-call limit. | “Show the companies and proposed cost before starting.” |
| **Practise an interview** | A role or topic; optionally a job description, stored job, resume, project, and duration. | “A 30-minute interview for this data analyst job, grounded in my project.” |
| **Prepare outreach** | A known company or stored prospect, recipient details or an approved contact-search scope, and truthful candidate context. | “Prepare Gmail drafts for this recipient list using my confirmed resume.” |

For tailored resumes, ask the agent to organize your existing resume into a **resume master**: a private, structured record of your true background that you review once and reuse. You can also start from the [neutral example](examples/resume-master.example.yaml). The default location is `~/.job-atlas/resume-master.yaml`; the example's content must be replaced with your own.

**Search defaults:** the posting period is 14 days, configurable from 1 to 30 days. Internships and part-time jobs are excluded unless you explicitly include them. Sources are asked for the requested period where supported; older jobs they return can remain eligible and show their actual age. Missing dates remain unknown.

## How your search works

`full-pipeline` coordinates the stages below. Basic job and company analysis is part of preparing a reviewable shortlist. Deeper research, resume tailoring, and profile discovery are choices you make separately.

![Six stages: review a search plan; collect and screen jobs; build job and company context; optionally add deeper research; shortlist and export; optionally tailor resumes or find profile links. Interview practice and outreach drafting are separate companion workflows.](docs/assets/job-search-workflow.svg)

| Stage | What happens | What you review or receive |
|---|---|---|
| **1. Plan your search** | The agent translates your preferences into a proposed source plan and explains its coverage and prerequisites. | The exact search to accept or revise before it runs. |
| **2. Collect and screen jobs** | Selected boards are searched, duplicates are consolidated, and jobs are reviewed against your intended work, experience, job types, and location. | Eligible jobs, rejected results with reasons, unresolved cases, and source failures shown separately. |
| **3. Understand jobs and companies** | `enrich-jobs` extracts supported requirements. `enrich-companies` builds employer identity, business context, and relevant team or function evidence. | Experience, education, qualifications, skills, company profiles, and visible gaps in the evidence. This is called **Phase A** in the technical docs. |
| **4. Add deeper research — optional** | After you approve the scope and provider budget, company research can add available Glassdoor ratings, work-life-balance evidence, or AmbitionBox India salary estimates. | Additional evidence for comparing employers before your final selection. Missing evidence stays unknown. This is **Phase B**. |
| **5. Shortlist and export** | Choose all eligible jobs, a manual subset, or reviewed filters. Save the selection and export the search workbook. | Jobs, company context, screening outcomes, and an editable follow-up decision for each shortlisted job. |
| **6. Prepare selected opportunities — optional** | Choose resume tailoring, LinkedIn profile discovery, or both. The agent works only on the selected jobs or companies and can export the updated results. | Tailored resume PDFs with match reports, and/or relevant LinkedIn URLs with supporting context. |

You can stop after reviewing the jobs and workbook, then return to preparation later. Saved progress lets the agent reuse completed collection and evidence after an interruption; partial or failed work remains visible.

## Choose a workflow

These **seven skills are installed by `job-atlas setup`**. Enter a skill name with `$` in your coding agent, followed by your request. During a guided search, the agent invokes the relevant supporting skills for you.

| Skill | Use it for | Input and result |
|---|---|---|
| [`full-pipeline`](.claude/skills/full-pipeline/SKILL.md) | The guided search from planning through review and export. | Your search preferences → collected jobs, analysis, shortlist, and workbook, with optional preparation. |
| [`enrich-jobs`](.claude/skills/enrich-jobs/SKILL.md) | Understanding the requirements of collected jobs. | Saved job descriptions → supported experience, education, qualifications, and hard/soft skills. |
| [`enrich-companies`](.claude/skills/enrich-companies/SKILL.md) | Understanding the employers behind the jobs. | Saved companies and posting evidence → company context; optional approved research adds market evidence. |
| [`tailor-resumes`](.claude/skills/tailor-resumes/SKILL.md) | Preparing resumes for your chosen jobs. | Selected jobs + confirmed resume master → tailored PDFs, an explainable match score, and a gap report. |
| [`find-profile-links`](.claude/skills/find-profile-links/SKILL.md) | Finding relevant people at selected companies. | Confirmed search selection + provider budget → LinkedIn profile links and professional relevance evidence. |
| [`mock-interview`](.claude/skills/mock-interview/SKILL.md) | Live interview practice, including without a previous search. | A role, JD, job, resume, project, or topic → one question at a time, a debrief, and private practice history. |
| [`draft-outreach`](.claude/skills/draft-outreach/SKILL.md) | Preparing separately requested outreach. | One recipient or a bounded list + candidate/company context → reviewable Gmail drafts. Requires the standalone setup described below. |

**Resume tailoring preserves your real background.** It can reorder, emphasize, and reword supported content. Requirements you cannot substantiate remain gaps in the report. The match score describes coverage of the job's requirements; it is not a hiring probability.

To practise for a role without collecting jobs first:

```text
$mock-interview
Run a 30-minute interview for this data analyst job description.
Use my attached resume and project as context. Ask one question at a time
and give me feedback at the end.
```

### Additional repository workflows

The repository also includes [`find-prospects`](.claude/skills/find-prospects/SKILL.md), which researches prospective employers beyond collected vacancies, and [`find-contacts`](.claude/skills/find-contacts/SKILL.md), which finds relevant people and possible email addresses for explicitly requested outreach. Prospect discovery currently focuses on data, ML, analytics, and credit-risk functions with India eligibility.

These additional skills are **not installed by the default package setup**. Some standalone enrichment, one-off JD tailoring, and outreach paths also use the separately configured legacy database. `draft-outreach` needs the optional Gmail dependencies and Google authorization; if recipient addresses are missing, it also needs `find-contacts`. See [standalone workflow prerequisites](docs/skills.md#standalone-workflow-prerequisites) before using those paths.

The guided search's people stage stores **LinkedIn links only**. Contact-email discovery and Gmail drafting belong to the separately invoked outreach workflow. You review and send drafts in Gmail yourself.

[All skills, including source-maintenance tools →](docs/skills.md)

## Your dashboard and workbook

### Browse the local dashboard

The dashboard reads the same saved search data as your agent. Its four views help you move from progress to decisions:

| View | What you can inspect |
|---|---|
| **Pipeline** | Source progress, screening outcomes, and completed, running, skipped, or incomplete stages. |
| **Job insights** | The mix of sources, work arrangements, skills, and hiring companies. |
| **Explore jobs & companies** | Job requirements, employer context, available salary evidence, and original links. |
| **Other outputs** | Your saved selection, LinkedIn profile links, and tailored resume records. |

Choose **All searches** for a combined view and cumulative workbook. Dashboard filters change what you see; to change the saved shortlist, tell your agent or use the review workbook.

### Review the Excel workbook

A per-search workbook includes **Start here**, **All collected**, **Jobs**, **Companies**, **Needs review**, **Shortlist**, **LinkedIn profile links**, and **Resume artifacts**. Optional-output sheets can be empty when those stages are skipped. Resume records point to the generated files; the PDFs remain separate artifacts.

To choose jobs for later preparation:

1. Open **Start here**, then **Shortlist**.
2. Each shortlisted job begins with its follow-up decision set to **Approved**. Change unwanted jobs to **Declined** using the dropdown.
3. Keep the original rows and identifiers intact; edit only the decision dropdowns. Save the workbook.
4. Give the complete saved file to your coding agent and ask it to import your choices.
5. Choose whether to tailor resumes, find LinkedIn profile links, or do both for the resulting shortlist.

**Approved means included in your follow-up selection.** Importing the workbook does not itself start paid research, resume generation, applications, or outreach.

[Dashboard and workbook details →](docs/technical-guide.md#dashboard)

## Where it searches

The current source catalog contains **19 integrated sources**. A search uses the sources selected in your reviewed plan; availability and results depend on the board, role, location, and access requirements.

| India-focused boards | Broader and specialist boards | Remote-job sources |
|---|---|---|
| Naukri | LinkedIn | Himalayas |
| Foundit | Indeed | We Work Remotely |
| Instahyre | Glassdoor | Working Nomads |
| IIMJobs | Wellfound | |
| Cutshort | Built In | |
| Shine | eFinancialCareers | |
| TimesJobs | Talent.com | |
| ZipRecruiter India | Hacker News Hiring | |

**City, India-wide, and remote searches are different choices.** City searches check the worksite for onsite and hybrid roles; India-wide searches cover eligible locations across the country; remote searches check whether applicants in India are eligible. A global “remote” listing alone does not establish that eligibility.

Most sources use direct requests. Cutshort requires a saved candidate session; Talent.com and ZipRecruiter can require Chromium; **Indeed opens a visible browser and needs you available for sign-in or site checks**. Some sources support only particular roles or cannot resolve every city. The plan shows those limitations before you commit to the search.

[Source requirements](docs/technical-guide.md#active-sources) · [Location examples](docs/technical-guide.md#choose-a-city-anywhere-in-india-or-remote)

## Optional setup and costs

Set up each extra when you choose a feature that needs it.

| Feature | Additional requirement |
|---|---|
| **Browser-backed job boards** | Run `job-atlas browser install` for Chromium when your plan calls for it. Attended sources also need a visible desktop session. |
| **Deeper company research** | Provider access for the selected source, such as Bright Data for Glassdoor, plus an approved research scope and call limit. |
| **LinkedIn profile discovery** | Bright Data access and a separately approved company scope and provider-call limit. |
| **Resume PDF generation** | Your reviewed resume master, Tectonic for rendering, and `pdftotext` for checking the generated PDF's text. |
| **Gmail drafts** | The `gmail` package extra, a Google OAuth desktop client, Gmail authorization, and the [standalone workflow prerequisites](docs/skills.md#standalone-workflow-prerequisites). |

Use `job-atlas auth setup` to configure optional provider credentials and `job-atlas auth status` to check what is available. Keep keys and OAuth files in the private local workspace.

Job Atlas is MIT-licensed. Your coding-agent service and any paid research providers have their own charges. Agent-driven analysis uses your current agent's reasoning; paid provider work is scoped and budgeted separately. Skipping a paid stage leaves its unavailable evidence marked as unknown.

## Privacy and your decisions

- **Your workspace stays local by default.** Search profiles, the database, resume master, and interview history live under `~/.job-atlas`; exports go to the location you choose.
- **Online work still reaches external services.** Selected job boards, your coding-agent provider, and optional research providers process the requests or context sent to them. Gmail receives drafts when you explicitly use that integration.
- **You choose the work.** Review the search plan, company-research budget, final shortlist, and optional next steps. Saved selections keep later work tied to the jobs you chose.
- **You review the evidence.** Missing requirements, uncertain locations, unavailable salaries, and incomplete sources stay visible. Generated resumes and drafts need your review before use.
- **You submit applications and send messages.** Job Atlas does not auto-apply, and its outreach workflow creates drafts without sending mail.

[Privacy model →](docs/PRIVACY.md)

## Keep it working

To upgrade an installation made with uv, then refresh its managed skills:

```bash
uv tool upgrade job-atlas
job-atlas skills install
job-atlas doctor
```

Restart your coding agent afterward. See [uv's tool guide](https://docs.astral.sh/uv/guides/tools/#upgrading-tools) for installer details and the [release notes](https://github.com/yati989/job-atlas/releases) for changes.

| If you see… | Try… |
|---|---|
| **`job-atlas` is not found after a uv install** | Run `uv tool update-shell`, then open a new terminal. |
| **A skill is missing in your agent** | Run `job-atlas skills status`, then `job-atlas skills install`, and restart the agent. |
| **An existing skill cannot be overwritten** | Preserve your customized or unrelated skill and resolve the name conflict before reinstalling. |
| **A browser prerequisite is missing** | Run `job-atlas browser install` if the selected source needs Chromium. |
| **A source fails or returns no suitable jobs** | Check its result and screening reasons in **Pipeline**. Ask the agent to review the source, role, or location coverage before expanding the search. |
| **Work was interrupted** | Ask the agent to resume the same saved search. Use **Refresh dashboard** to inspect the latest progress. |

This is a public beta. Boards can change or block access, search filters can be incomplete, and some listings may be broad or promoted. Integration with a source does not guarantee that it is reachable in every run or covers every profession. Company estimates are evidence to review, not promises about an offer. Respect each board's terms and access controls.

[Current limitations](docs/technical-guide.md#current-limitations) · [Responsible use](docs/technical-guide.md#safety-and-responsible-use)

## Contribute or report an issue

Found a broken source or a confusing step? [Open an issue](https://github.com/yati989/job-atlas/issues) with your operating system, installation method, command or workflow, expected result, and a redacted error or public example. Keep resumes, search exports, credentials, and other personal data out of issues and commits.

Contributors can start with the [technical guide](docs/technical-guide.md), [architecture](docs/technical-guide.md#architecture), [testing instructions](docs/technical-guide.md#testing), [source-onboarding guide](docs/technical-guide.md#adding-a-source), and [design decisions](docs/adr/). Repository working rules are in [AGENTS.md](AGENTS.md).

[MIT license](LICENSE) · [PyPI package](https://pypi.org/project/job-atlas/) · [Releases](https://github.com/yati989/job-atlas/releases) · [Skills catalog](docs/skills.md)
