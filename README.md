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
python -m pytest                         # 43 tests, about 70 seconds
```

`pip install -e .` also installs a `jobagent` command; the examples below use `python -m jobagent.cli`.

## Environment variables

See [.env.example](.env.example). Secrets live only in the environment: the encryption key, the optional
`ANTHROPIC_API_KEY` and the optional dashboard token. No passwords are stored anywhere; the agent never
logs in to job sites.

## Uploading the master resume and completing the profile

1. Parse the resume into a draft (PDF with a text layer, DOCX, TXT or MD):
   `python -m jobagent.cli profile parse-resume ~/resume.pdf --out profile.yaml`
2. Open `profile.yaml` and check every field. Parsing is heuristic, so fix titles, employers, dates and bullets.
3. Fill in what a resume does not say. The agent never infers these and pauses any application that needs them:
   `work_authorization` (per country), `requires_sponsorship`, `salary` (or `answer_text`), `notice_period_days`,
   `preferences` (target and adjacent roles, locations, remote, relocation, excluded employers), and
   `approved_answers` for questions you are happy to pre-answer (for example "how did you hear about us", and
   optional demographic questions if you want a fixed "decline to answer").
4. Set each skill's `proficiency` (`expert`, `advanced`, `intermediate`, `beginner`, `exposure`). Scoring weights
   credit by proficiency; listing a skill does not count as proof.
5. Add verified evidence that is not on your master resume with `on_master_resume: false` (extra achievements,
   projects, coursework). The optimizer may surface these when a job needs them, and never anything else.
6. Import: `python -m jobagent.cli profile import profile.yaml`, then delete the plaintext YAML or keep it somewhere safe.
   The stored copy is encrypted with `JOBAGENT_ENCRYPTION_KEY`.

`data/sample/profile_SYNTHETIC.yaml` shows every field with synthetic values.

## Daily use

```bash
python -m jobagent.cli discover          # fetch from sources in config.yaml
python -m jobagent.cli process           # screen, score, optimize, prepare documents and answers
python -m jobagent.cli jobs              # list; add --status READY_FOR_REVIEW
python -m jobagent.cli show 12           # score breakdown, changes, answers, blockers
python -m jobagent.cli answer 12 "Question text" "Your answer"
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
   been verified yet (see below). Leave it out until you have watched a supervised run succeed (`JOBAGENT_HEADFUL=1`).
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
| LinkedIn, Indeed | Not automated: their terms forbid scraping and automated applying, and neither offers a job-search API to individuals | Manual | Use a `manual` source file for jobs you find there; the agent still scores, tailors and tracks them. |

`python -m jobagent.cli sources` prints the live capability registry.

## Safety properties (all covered by tests)

- Truth guard: every tailored bullet traces to a profile bullet; new numbers, new skills, new named entities,
  ownership upgrades ("led", "owned") and proficiency inflation are rejected. Model output goes through the same guard.
- Job descriptions and web pages are untrusted data: instruction-like text is flagged, model prompts fence it,
  and the model is not used for jobs that contain it. The model never decides eligibility, scores or submission.
- Navigation is limited to the application's own host (and the ATS's domains). Redirects elsewhere abort the run.
- One application per job: idempotency key, duplicate URL check, portal "already applied" detection, and a crash
  between clicking submit and recording the result becomes `SUBMISSION_UNVERIFIED` instead of a resubmission.
- CAPTCHAs, logins and MFA are never bypassed; the application pauses for you.
- Profile encrypted at rest; per-application isolated browser contexts; append-only audit log; documents directory is `0700`.

## Known limitations

- Live job sites were not reachable from the build environment, so no connector or form adapter has been run
  against a real employer yet. Treat Greenhouse and Lever submission as untested until a supervised run passes.
- The model integration (`claude-opus-5-5`, with server-side refusal fallback) is implemented and tested with
  fake clients, but no real API call was made because no API key was available. Without a key the agent uses
  deterministic templates.
- Resume parsing is heuristic; always review the draft. Scanned PDFs without a text layer are rejected.
- Requirement extraction uses a curated skill taxonomy (`jobagent/skills.py`); skills outside it are matched
  only through responsibility text overlap. Extend the taxonomy for niche fields.
- The rubric is this tool's own and is not any employer's ATS score.
- The generic form adapter handles label-based HTML forms. Multi-page wizards, custom widgets and file pickers
  driven by JavaScript frameworks will pause for manual completion.
- PDF resume output is not generated (DOCX, TXT and HTML are); most ATS accept DOCX.
- Single-user, single-machine design (SQLite). Run one scheduler at a time.

## Sample end-to-end run

`python scripts/demo_e2e.py` runs the whole flow against the local test ATS with the synthetic candidate and
writes [docs/sample_run/](docs/sample_run/): the run log, a tailored resume and cover letter, and CSV/JSON/HTML
reports. Every employer and person in it is synthetic. The local test ATS (`testportal/`) can also be started on
its own with `python -m testportal.server 8765`.
