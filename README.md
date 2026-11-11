# jobagent: truthful job search and application agent

Finds jobs on supported boards, screens them against the candidate's hard constraints, scores the resume
against each job with a transparent rubric, tailors the resume using only verified evidence, prepares
cover letters and screening answers from candidate-approved data, and submits applications only when the
candidate's authorization policy allows it. A submission counts as done only when the confirmation page
shows a success message and a reference number.

Stack and design: see [ARCHITECTURE.md](ARCHITECTURE.md). Test evidence: [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md).
Sample run (synthetic data): [docs/sample_run/RUN_LOG.md](docs/sample_run/RUN_LOG.md).

## Setup

```bash
cd job-applier
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # pinned versions, tested together
python -m playwright install chromium    # or set JOBAGENT_CHROMIUM_PATH to an existing Chromium
cp .env.example .env                     # fill in, never commit
python -m jobagent.cli keygen            # paste the output into JOBAGENT_ENCRYPTION_KEY in .env
set -a; . ./.env; set +a
python -m jobagent.cli init              # creates $JOBAGENT_HOME/config.yaml and the database
python -m pytest                         # 59 tests, about 2 minutes
```

`pip install -e .` also installs a `jobagent` command; the examples below use `python -m jobagent.cli`.

## Environment variables

See [.env.example](.env.example). Secrets live only in the environment: the encryption key, the optional
`ANTHROPIC_API_KEY` and the optional dashboard token. No passwords are stored anywhere; the agent never
logs in to job sites.

## Put it online

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/amVishalRajoriya/job-applier?quickstart=1)

One click runs the real app in a private GitHub Codespace. To run it on a free public URL (Hugging Face Spaces, password-protected) and for a step-by-step guide with
screenshots, see [docs/HOSTING_GUIDE.md](docs/HOSTING_GUIDE.md) (also as [PDF](docs/Job_Applier_Guide.pdf)).
The container is defined in `Dockerfile`; `.github/workflows/deploy-huggingface.yml` deploys it.

## Quick start: the browser front end

Everything can be done from the dashboard, with no commands after the first one:

- macOS or Linux: `./start.sh`
- Windows: double-click `start.bat` (written for Windows but not yet run on it)

On the first run the script creates a virtual environment and installs the dependencies and Chromium. It also
writes `.env` with a new encryption key; back that file up, because the saved profile cannot be read without
the key. It then opens http://127.0.0.1:8000 in your browser. The dashboard listens only on your own computer.

1. **Setup** page: upload your resume, fill in your details and skill levels, paste the job board URLs, and
   correct anything the parser misread.
2. **Run** page: a checklist shows what is still missing. Then:
   - **Check boards** fetches each board once and stores nothing;
   - **Find jobs** discovers, screens, scores and prepares documents;
   - the live log shows progress, and one run happens at a time.
3. **Jobs** page: open a job to see the score breakdown, resume changes, cover letter and form answers.
   Answer any open questions, then press **Dry run (never submits)**. It fills the real form and shows each
   field plus a screenshot; tick "show the browser" to watch it. Then press **Approve**.
4. Switch the mode to **review** on the Run page. Submit one job from its page, or all approved jobs from the
   Run page. A job counts as submitted only when the employer's confirmation page shows a reference.

Auto-apply cannot be turned on from the browser; it needs `jobagent consent grant`.
Screenshots (synthetic data): [Run](docs/ui/ui_run.png), [Job and dry run](docs/ui/ui_job.png), [Setup](docs/ui/ui_setup.png).

## First-run setup

Pick one of three ways. All of them store the profile encrypted and write your boards into `config.yaml`.

**A. Details file (recommended).**
```bash
python -m jobagent.cli setup --template my_details.yaml       # blank file with every question explained
# fill in my_details.yaml: work authorization and sponsorship per country, salary, notice period,
# relocation, target roles and locations, skill levels, pre-approved answers, board URLs, mode
python -m jobagent.cli setup --resume ~/resume.pdf --details my_details.yaml
```

**B. Guided questions.** `python -m jobagent.cli setup --resume ~/resume.pdf` asks each question in the
terminal. Press Enter to skip one.

**C. Dashboard.** `python -m jobagent.cli dashboard`, open http://127.0.0.1:8000/setup, then upload the resume
and fill in the form. The same page has a YAML editor for correcting what the parser read.

What setup does:
- The resume is parsed (PDF with a text layer, DOCX, TXT or MD). Parsing is heuristic, so check the roles,
  dates and bullets it prints. Fix them with `profile show` and `profile import`, or with the dashboard's editor.
- Blank answers stay blank. Nothing is inferred, and any application that needs a blank field pauses for you.
- Skill levels (`expert`, `advanced`, `intermediate`, `beginner`, `exposure`) drive scoring. A skill you declare
  that is not on your resume is kept off the master resume.
- Board URLs are turned into sources:

  | URL | Source |
  |---|---|
  | `boards.greenhouse.io/<co>` | Greenhouse |
  | `jobs.lever.co/<co>` | Lever |
  | `jobs.ashbyhq.com/<co>` | Ashby |
  | `<co>.wdN.myworkdayjobs.com/<Site>` | Workday |
  | `.rss`, `.xml` or `.json` | Feed |
  | Any other careers page | Careers page (read only when it publishes JobPosting data) |

  LinkedIn, Indeed and Naukri URLs are not boards and are refused here; see
  [LinkedIn, Indeed and Naukri.com](#linkedin-indeed-and-naukricom) for how those sites work.
- Skills the built-in taxonomy does not know are added to `custom_skills` in `config.yaml`, so job ads that
  mention them are matched.
- Setup can set `mode` to `discovery` or `review`. It never turns on auto-apply.
- Running setup again with a new resume keeps your answers and skill levels.

To add true achievements that are not on your resume, use `profile show > p.yaml`. Add them with
`on_master_resume: false`, then run `profile import p.yaml`. The optimizer may use these when a job needs them,
and never anything else. `data/sample/profile_SYNTHETIC.yaml` shows every profile field with synthetic values.

## Going live safely (do these in order)

1. `python -m jobagent.cli check-sources` fetches each configured board once and prints what it found. It
   stores nothing. This is the first real test of the Greenhouse, Lever, Ashby and Workday connectors.
2. `python -m jobagent.cli llm-check` (optional) makes one small Claude API call if `ANTHROPIC_API_KEY` is set.
   Without a key, the agent uses deterministic templates.
3. `python -m jobagent.cli discover` and then `python -m jobagent.cli process`.
4. `python -m jobagent.cli dry-run <job_id> --headful` opens the real application page and runs the same
   checks as a submission (employer, title, closed, CAPTCHA, login, multi-step). It fills every field it can,
   uploads the resume, saves a screenshot and a report under `documents/job_<id>/dry_run_<time>/`, and keeps
   the browser open until you press Enter. **It never submits.** The adapter runs read-only:
   - every request that could send data (any method other than GET, HEAD or OPTIONS, from the page or its
     scripts) is aborted and listed in the report;
   - the submit method refuses to run;
   - the job's status is not changed.
5. Compare the filled form with what you would have typed. When several dry runs on a site look right, use
   review mode (`approve`, then `apply`) for real submissions on that site.
6. Only after supervised review-mode submissions succeed on a site should you consider auto-apply for it.
   Auto-apply needs a consent that names `untested_live`.

## Daily use

```bash
python -m jobagent.cli check-sources     # test each source, store nothing
python -m jobagent.cli discover          # fetch from sources in config.yaml
python -m jobagent.cli process           # screen, score, optimize, prepare documents and answers
python -m jobagent.cli jobs              # list; add --status READY_FOR_REVIEW
python -m jobagent.cli show 12           # score breakdown, changes, answers, blockers
python -m jobagent.cli answer 12 "Question text" "Your answer"
python -m jobagent.cli dry-run 12 --headful   # fill the real form, screenshot, never submit
python -m jobagent.cli approve 12        # review mode
python -m jobagent.cli apply             # submits approved applications
python -m jobagent.cli track 12 --status INTERVIEW --follow-up 2026-11-01 --response "Phone screen booked"
python -m jobagent.cli report --format html --out report.html
python -m jobagent.cli dashboard         # http://127.0.0.1:8000
python -m jobagent.cli run --loop --interval 3600   # or call `run` from cron
```

## Operating modes

| Mode | What happens |
|---|---|
| `discovery` (default) | Discover, screen and score. Nothing is submitted. |
| `review` | Documents and answers are prepared; each application is submitted only after you approve it (`approve` or the dashboard button). |
| `auto` | Submits without asking, only inside a consent you granted. Anything with an unknown fact, a new question, a CAPTCHA, a login, a sensitive or consequential question you have not pre-approved, a score under your minimum, or a site outside your list still stops at the review checkpoint. |

## Enabling authorized auto-apply

1. Run in review mode first and confirm a few submissions on each site you want to automate.
2. Grant consent with explicit limits (you must type `I AGREE`):
   ```bash
   python -m jobagent.cli consent grant --sources greenhouse lever \
     --domains job-boards.greenhouse.io jobs.lever.co --min-score 70 --max-per-day 5 \
     --apply-status tested_local untested_live
   ```
   `untested_live` is required for real Greenhouse or Lever forms because live submission on those sites has not
   been verified yet (see below). Leave it out until dry runs and supervised review-mode submissions have succeeded on that site.
3. Set `mode: auto` in `config.yaml` (or `JOBAGENT_MODE=auto`).
4. `python -m jobagent.cli consent show` prints the active consent; `consent revoke` turns it off immediately.

## Supported sources and limitations

| Source | Discovery | Applying | Tested |
|---|---|---|---|
| Greenhouse | Public Job Board API | Generic form adapter | Against local fixtures in Greenhouse's response shape. Not run against the live API or a live form from this environment (outbound access was blocked). |
| Lever | Public Postings API | Generic form adapter | Same as Greenhouse. |
| Ashby | Public Job Posting API | Manual | Discovery against fixtures only. |
| Workday | Public career-site search endpoint | Manual (Workday needs an account and often MFA) | Discovery against fixtures only. |
| Company career pages | schema.org `JobPosting` JSON-LD, robots.txt respected | Manual unless the link is a supported ATS | Fixtures. |
| RSS / JSON feeds | Standard feeds | Manual unless the link is a supported ATS | Fixtures. |
| LinkedIn, Indeed, Naukri.com | Never scraped. Your job-alert emails (`job_alerts`) and pages you save from your own browser (`saved_pages`) | Assisted: you submit on the site; the agent prepares a kit and records the result. If a listing links straight to a Greenhouse or Lever form, that form is used (review mode) | `untested_live`. Parsers tested on SYNTHETIC fixtures only (`tests/fixtures/jobsites/`). |

`python -m jobagent.cli sources` prints the live capability registry.

## LinkedIn, Indeed and Naukri.com

**The constraints, plainly.** All three sites forbid scraping and automated access in their terms of use. Most
pages sit behind a login, and they use CAPTCHAs and bot detection. Accounts that look automated get restricted
or banned, and LinkedIn in particular enforces this. None of them offers a job-search API to individuals
(Indeed retired its public one). So this agent never logs in to them, never searches or crawls them, never fills
or submits their forms, and nothing in `config.yaml` can turn that on: these domains are hard-blocked in the
apply policy, in the form adapter and in auto-apply consent.

What it does instead, using only what you already have:

1. **Job-alert emails** (recommended). Turn on job alerts on each site. The agent reads the alerts:
   - from a folder of saved `.eml` files or an `.mbox` export: `{type: job_alerts, folder: ~/JobAlerts}`, or
   - read-only from your mailbox over IMAP:
     `{type: job_alerts, imap: {host: imap.gmail.com, user: you@gmail.com, password_env: JOBAGENT_IMAP_PASSWORD}}`.
     Use an app password in that environment variable. The agent opens the mailbox read-only, looks only at mail
     from linkedin.com, indeed.com and naukri.com from the last 14 days (`since_days`), and never marks, moves
     or deletes anything.
   Only mail from those sites' own domains is trusted (`require_known_sender: true`), so a look-alike email
   cannot add jobs. Alerts carry only a summary, so scores for alert-only jobs are rough.
2. **Saved job pages.** Open a job in your own browser, signed in, and save it (Ctrl+S / Cmd+S, "Webpage,
   complete" or single-file `.mhtml`) into the saved-pages folder: `{type: saved_pages, folder: ~/JobPages}`.
   This gives the full description. A page that turns out to be a login wall or CAPTCHA is reported and skipped.
3. **Pasted URLs.** `python -m jobagent.cli add-urls URL...` (or the Setup page) keeps a to-do list of job
   pages to save. Nothing is fetched. `check-sources` and `discover` list the URLs still waiting for a page.
4. **Supervised capture (optional, riskier).** `python -m jobagent.cli capture` opens a visible browser window
   with its own profile. You sign in yourself; it loads each queued URL once and saves the page only when you
   press Enter. It never clicks, types, scrolls, searches or applies, and stops after 25 pages. It is still
   automation software and the sites can detect it, so saving pages yourself (step 2) is the safe default.
   Set `JOBAGENT_BROWSER_CHANNEL=chrome` to use your installed Chrome.

The same job from an alert and a saved page is merged into one. Jobs are then screened, scored and tailored
like any other, under the same truth guard.

**Applying.** On the job's dashboard page, or with `python -m jobagent.cli assist JOB_ID`, you get an
application kit: the tailored resume and cover letter, and answers to the questions these sites usually ask
(Naukri's current/expected CTC, notice period, experience, and so on), each taken from your verified profile.
Anything not in your profile, such as current CTC unless you set `salary.current_text`, is marked "needs you",
never guessed. The CLI opens the job in your normal browser. You submit, then click "I submitted it myself"
(or `assist JOB_ID --submitted --reference REF`). It is recorded as `SUBMITTED` with `verified = no`, because
the agent did not see the confirmation itself.

**Tested how.** The cloud environment this was built in cannot reach these sites, and their terms forbid
scraping, so every page and email in `tests/fixtures/jobsites/` was written by hand after the sites' public
structure (schema.org `JobPosting` data, URL patterns, long-standing element ids). The real pages and emails
change often. Before relying on it, save two or three real alerts and pages into your folders and run
`check-sources`: it shows what was read and what was skipped.

## Safety properties (all covered by tests)

- Truth guard: every tailored bullet traces to a profile bullet; new numbers, new skills, new named entities,
  ownership upgrades ("led", "owned") and proficiency inflation are rejected. Model output goes through the same guard.
- Job descriptions and web pages are untrusted data: instruction-like text is flagged, model prompts fence it,
  and the model is not used for jobs that contain it. The model never decides eligibility, scores or submission.
- Navigation is limited to the application's own host (and the ATS's domains). Redirects elsewhere abort the run.
- One application per job: idempotency key, duplicate URL check, portal "already applied" detection, and a crash
  between clicking submit and recording the result becomes `SUBMISSION_UNVERIFIED` instead of a resubmission.
- CAPTCHAs, logins and MFA are never bypassed; the application pauses for you.
- LinkedIn, Indeed and Naukri.com are never automated: discovery reads only your alert emails and saved pages,
  and applications there are submitted by you (`tests/test_jobsites.py`).
- Profile encrypted at rest; per-application isolated browser contexts; append-only audit log; documents directory is `0700`.

## Known limitations

- **Live job sites were not reachable from the build environment.** Its proxy refused outbound connections
  (rechecked 2026-10-22), so no connector or form adapter has been run against a real employer yet. Greenhouse,
  Lever, Ashby and Workday discovery and Greenhouse/Lever form filling stay marked untested until
  `check-sources` and `dry-run` succeed on your machine.
- The model integration (`claude-opus-5-5`, with server-side refusal fallback) is tested only with fake clients.
  No API key was available. `llm-check` verifies it with your key. Without a key the agent uses deterministic
  templates.
- Resume parsing is heuristic. It is tested on two layouts (the synthetic master resume as PDF, DOCX and MD,
  and `data/sample/alt_layout_resume_SYNTHETIC.txt`). Always review the parsed roles. Scanned PDFs without a
  text layer are rejected.
- Requirement extraction uses a curated skill taxonomy (`jobagent/skills.py`). It can be extended with
  `custom_skills` in `config.yaml`, which setup fills from your own skill list.
- The rubric is this tool's own and is not any employer's ATS score.
- The generic form adapter handles single-page, label-based HTML forms.
  - A form with only a Next or Continue button (a multi-step wizard) pauses with that reason, so you finish it
    yourself using the prepared documents.
  - Custom JavaScript widgets may not fill. A dry run shows which ones before anything is submitted.
- The dry-run guard intercepts requests the browser routes, from the page and its frames. WebSocket messages and
  service-worker requests are not intercepted. The application pages it is meant for do not submit that way, but
  watch the headful window.
- PDF resumes are rendered with Chromium (`pdf_output: true`). Forms receive the DOCX unless you set
  `upload_format: pdf`.
- Single-user, single-machine design (SQLite). Run one scheduler at a time.

## Sample end-to-end run

`python scripts/demo_e2e.py` runs the whole flow against the local test ATS with the synthetic candidate and
writes [docs/sample_run/](docs/sample_run/): the run log, a tailored resume and cover letter, and CSV/JSON/HTML
reports. Every employer and person in it is synthetic. The local test ATS (`testportal/`) can also be started on
its own with `python -m testportal.server 8765`.
