# Code audit: job applier (2026-11-06)

Scope: the code in this folder at commit `36c9b22` (HEAD of `master`, clean working tree). I covered the three job sites
(LinkedIn, Indeed, Naukri: `jobagent/sources/jobsites.py`), the shared parsing helpers, screening, the apply flow
(`jobagent/apply/*`, `answers.py`), config and `.env` handling, storage (`db.py`, `normalize.py`, `profile.py`), the
dashboard and the CLI run loop. **No code was changed.** The only files added are this document and
`docs/audit_2026-11-06/test_audit_probes.py`, which holds the throwaway tests used to confirm the bugs.

## Fix status (2026-11-06, same day)

Fixed in the commit after this audit, each with a regression test in `tests/test_audit_regressions.py` (the probes
were turned into real tests with the assertion inverted; 40 of them fail on `36c9b22` and all pass now). Full suite:
**170 passed**.

* **Fixed:** H1, H2, H3, H4, H5, M1, M2, M3, M4, M5, M6, M7, M8, M9, M10, M11, M12, M13, L1, L5 (employer match
  only), L7, L8.
* **Not fixed yet:** M14 (shared SQLite connection in the dashboard), L2 (linked company name in LinkedIn alerts,
  needs a real alert sample), L3 (config validation), L4 (setup form `mode` value), L5 daily-limit counting, L6
  (launcher reinstall), L9 (dry-run GET beacons).
* Notes on behaviour changes: salary and employment type guessed from free text now give a *warning* instead of a
  hard reject (structured fields still reject); a job whose preparation errors is parked as `PREPARATION_FAILED`
  and retried on the next `process`/`run`; work-authorization questions that mix authorization and sponsorship
  with no clear polarity, and visa questions that don't ask about *needing* sponsorship, now go to the candidate.
* M2 and M3 are verified only against new test-portal variants (`?variant=fetch`, `?variant=thanks_offsite`), not
  live Greenhouse/Lever.

## How to read this

* **Verified (ran)**: I reproduced it by running code in a scratch copy, usually with a probe test in
  `docs/audit_2026-11-06/test_audit_probes.py`. Each probe *passes while the bug is present*.
* **Inferred (read)**: I found it by reading the code. I did not reproduce it, usually because it needs a live site,
  a real browser failure or Windows.
* Severity: **High** means the agent submits a wrong answer, or silently rejects or loses jobs. **Medium** means wrong
  records, crashes, or features that break under realistic input. **Low** means rough edges.

## Test suite result (verified)

`python -m pytest -q` ran **106 passed in 70.6 s** on Python 3.13.18, using a fresh venv with the pinned
`requirements.txt`. The suite is green. Every issue below sits in a path the suite does not exercise, because the
fixtures are SYNTHETIC and tidy.

Environment note (not a code bug): the cloud container's system Python did not have the pinned packages (no `fastapi`,
`playwright`, `pytest`, `anthropic` or `cryptography`), so `python -m pytest` fails there until you run
`pip install -r requirements.txt`. `start.sh` handles this on your machine.

Not verifiable from here: live LinkedIn, Indeed, Naukri, Greenhouse or Lever pages (the cloud proxy blocks them), real
Claude API calls (no key), and `start.bat` on Windows.

## Summary

| # | Severity | Area | Issue | How found |
|---|---|---|---|---|
| H1 | High | Answers | "Authorized to work … without sponsorship?" gets the opposite answer | Verified |
| H2 | High | Answers | "How many years of experience do you have with Java?" gets total years (8), not Java years (1) | Verified |
| H3 | High | Parsing | Salary parser invents INR salaries from text like "2-3 languages", then hard-rejects the job | Verified |
| H4 | High | Parsing | Any "intern…" word ("internal", "international") marks a job as internship and hard-rejects it | Verified |
| H5 | High | Pipeline | One error while preparing a job aborts the whole batch and strands that job forever | Verified |
| M1 | Medium | Apply | Playwright errors other than timeouts escape `submit()` | Verified |
| M2 | Medium | Apply | Forms that submit without a page navigation are always recorded as unverified | Inferred |
| M3 | Medium | Apply | A redirect to another domain *after* submit records a sent application as FAILED | Inferred |
| M4 | Medium | Apply | Reference-number regex treats ordinary words as a confirmation reference | Verified |
| M5 | Medium | Storage | Different alert jobs with an unparsed company merge into one job | Verified |
| M6 | Medium | Storage | Saving the full page after an alert never re-scores the job or fixes the employer | Verified |
| M7 | Medium | Screening | Salary filter ignores the pay interval (hourly or monthly vs annual minimum) | Inferred |
| M8 | Medium | Job sites | One email or page with an unknown charset makes the whole source fail | Verified |
| M9 | Medium | Naukri | Pasted `?jobId=` URLs drop out of the URL queue | Verified |
| M10 | Medium | Indeed | Tracking-host links produce a wrong canonical URL (`cts.indeed.com/viewjob…`) | Verified |
| M11 | Medium | Job sites | `unwrap()` replaces a real job link that carries a `url=` parameter | Verified |
| M12 | Medium | Dashboard | Setting a tracking status the state machine disallows returns HTTP 500 | Verified |
| M13 | Medium | CLI | `jobagent run --loop` dies on the first exception and never reloads config | Inferred |
| M14 | Medium | Storage | One SQLite connection is shared across the dashboard's worker threads | Inferred |
| L1 | Low | Answers | Over-broad question rules ("mobile" → phone, "joining" → notice period, …) | Verified (2 of 4) |
| L2 | Low | LinkedIn alerts | A linked company name ends the job block, so company and location are lost | Inferred |
| L3 | Low | Config | config.yaml is not validated; typos are ignored; relative `JOBAGENT_HOME`; comments get wiped | Inferred |
| L4 | Low | Dashboard | Setup form writes any `mode` value, and the next start then fails | Inferred |
| L5 | Low | Policy | Excluded employers use exact string match; daily limit counts manual submissions | Inferred |
| L6 | Low | Launchers | Dependencies are never reinstalled after `requirements.txt` changes | Inferred |
| L7 | Low | Parsing | "12,00,000 - 18,00,000 INR" (currency after the numbers) is not parsed | Verified |
| L8 | Low | Naukri | naukrigulf.com job URLs are refused when pasted | Verified |
| L9 | Low | Dry run | The read-only guard still lets GET beacons and WebSockets through | Inferred |

Fix order I'd suggest: H1, H2 (wrong answers can reach employers in auto mode), then H3, H4, H5 (good jobs silently
disappear), then M1 to M4 (submission records you rely on), then the rest.

---

## High

### H1. Work-authorization questions that mention sponsorship get inverted answers (Verified)

**Where:** `jobagent/answers.py:29-30` (rule order) and `answers.py:115-118`.

**What:** `classify()` checks `RULES` in order, and the `sponsorship` rule (`sponsor|visa`) comes before
`work_authorization`. A question like *"Are you legally authorized to work in India without visa sponsorship?"* is
classified as `sponsorship`. The sponsorship branch answers "do you **need** sponsorship?", which for the synthetic
Indian citizen is **No**. That is the opposite of the right answer to the question asked (Yes). It is marked
`answered`, so it is not flagged for review. In auto mode it would be submitted.

**Evidence:**
```
Are you legally authorized to work in India without visa sponsorship? -> sponsorship No answered
Can you work in India without requiring visa sponsorship?            -> sponsorship No answered
```
Probe: `test_probe_answers_inverted_and_wrong_years`.

**Proposed fix:**
1. Check `work_authorization` before `sponsorship`, and add `can you work|able to work` to its pattern.
2. Treat polarity explicitly. If the question contains both an authorization phrase and `without (visa )?sponsor`,
   answer "authorized and no sponsorship needed" (Yes only if authorized *and* `requires_sponsorship[cc]` is False).
3. If a question matches both rules and polarity is unclear, return `needs_input` instead of guessing.
4. Add regression tests for the five or six common phrasings (Greenhouse and Lever use several).

### H2. Skill-specific "years of experience" questions get total years (Verified)

**Where:** `jobagent/answers.py:142-151`.

**What:** the skill extractor regex only matches `experience (with|in|using) X`. The very common phrasing *"How many
years of experience do you have with Java?"* doesn't match ("experience do you have with"). The code then falls back
to total career years, **8**, where the profile says Java is **1** year. The answer is a false claim and is marked
`answered`.

**Evidence:**
```
How many years of experience do you have with Java? -> 8 answered (computed from experience dates)
Years of experience with Java                      -> 1 answered (skills.Java.years)
```

**Proposed fix:** look for any known skill anywhere in the question (`extract_skills(question)` from `skills.py`). If
one skill is found, answer from `skill_entry(...).years` or return `needs_input`. Only fall back to total years when
no skill is named. If two or more skills are named, return `needs_input`.

### H3. Salary parser invents salaries and the filter hard-rejects the job (Verified)

**Where:** `jobagent/sources/base.py:165-187` (`_SAL`, unit group `(k|lpa|l)` with no word boundary), used by
`filtering.py:197-199`.

**What:** any range followed by a word that starts with "l" is read as lakhs: "2 to 3 **l**anguages", "2-3
**l**ibraries", "3 to 4 **l**ocations", "own 2-3 **l**arge services". The record gets `salary_max = 300000 INR`, and
`screen()` then disqualifies the job because 3 lakh is below the candidate minimum (30 lakh in the sample profile).
This hits Naukri and Indeed saved pages without JSON-LD and all alert emails, because those go through
`parse_salary()` on free text. The rejection is silent and permanent, which contradicts the module's rule that
unknowns become warnings.

**Evidence:**
```
'Must speak 2 to 3 languages'               -> (200000.0, 300000.0, 'INR')
'Python 2-3 libraries'                      -> (200000.0, 300000.0, 'INR')
'Team of 5 - 10 Engineers in 3 to 4 locations' -> (300000.0, 400000.0, 'INR')
```
Alert-email probe: disqualifiers included `salary max 300,000 INR below candidate minimum 3,000,000`.

**Proposed fix:**
* Change the unit group to `(?P<ka>k|lpa|lakhs?|lacs?|l)\b` (and the same for `kb`), so `l` must be a whole token.
* Treat a salary as "parsed from free text" (store `raw["salary_source"]="text"`). When it is below the minimum, add a
  **warning**, not a disqualifier. Keep hard rejection for structured salaries (JSON-LD `baseSalary`, Lever
  `salaryRange`).
* Tests: the strings above should return `(None, None, None)`, while "₹ 12-18 Lacs P.A." and "15 - 25 LPA" still parse.

### H4. "internal" or "international" marks a job as an internship and rejects it (Verified)

**Where:** `jobagent/sources/base.py:151-162` (`"internship": r"intern(ship)?"`), used by `filtering.py:139-141`.

**What:** there is no word boundary, so "work with **intern**al teams" or "an **intern**ational payments company"
gives `employment_type = "internship"` whenever the text doesn't also say "full-time" (checked first). With
`employment_types: [full-time]`, `screen()` hard-rejects the job. Naukri and Indeed pages without JSON-LD and every
alert email go through this (`jobsites.py:380`, `jobsites.py:566`).

**Evidence:** `normalize_employment_type("Work with internal teams") == "internship"`. The alert probe was
disqualified with `employment type internship not in preferences ['full-time']`.

**Proposed fix:** use `r"\bintern(ship)?s?\b"`. Also, when the type was *inferred from free text* rather than a
structured field, a mismatch should be a warning, not a disqualifier. Add tests for "internal", "international" and
"we mentor interns".

### H5. One error while preparing a job aborts the batch and strands the job (Verified)

**Where:** `jobagent/pipeline.py:80-116` (only `TruthViolation` is caught, at lines 113-115), with
`pipeline.py:81` (only `DISCOVERED` jobs are ever picked up).

**What:**
* Any other exception in `_score_and_prepare` stops the whole `screen_and_score` run, so the remaining jobs are not
  processed in that run. Examples: `template_cover_letter` raising `ValueError` (`documents.py:167-168`), a
  python-docx or disk error, an `IndexError` in `draft_cover_letter` when the resume has no roles (`llm.py:134`), an
  unexpected SDK error type.
* The job being processed is left in `SCORED`, `OPTIMIZATION_REQUIRED` or `OPTIMIZED`. Those states are never
  selected again, so the job is stuck and nothing tells you.
* Even the handled case strands jobs: `TruthViolation` is raised inside `optimize()` (`optimizer.py:283-285`), before
  any score is stored, so the job stays in `SCREENED` forever.

**Evidence:** probe `test_probe_exception_aborts_batch_and_strands_job` makes the first cover letter fail. After the
crash, the statuses were `['SCORED', 'DISCOVERED' ×11]`. After a rerun, job 1 was **still `SCORED`** while the others
moved on.

**Proposed fix:**
* Wrap each job in `try/except Exception`, record `{"job": id, "error": ...}` in `stats["errors"]`, write an
  audit entry, and continue with the next job.
* On failure, move the job to a recoverable state: add a `PREPARATION_FAILED` status (or reuse `FAILED` with
  `retry_status="prepare_failed"`) with a transition back to `SCREENED`/`SCORED`. Add a `jobagent reprocess JOB_ID`
  command or dashboard button that re-runs preparation.
* Also select jobs stuck in `SCREENED`/`SCORED`/`OPTIMIZATION_REQUIRED`/`OPTIMIZED` with no `applications` row on
  the next run.

---

## Medium

### M1. Playwright errors other than timeouts escape `submit()` (Verified)

**Where:** `jobagent/apply/engine.py:219-224` catches only the builtin `TimeoutError`. `adapters.py:232-241`
converts only `PWTimeout`. `_fill_form`, `a.fields()`, `a.invalid_required()` and `a.result()` can also raise
`playwright.Error`.

**What:** if the browser crashes or an element detaches during fill (before the click), the exception leaves
`engine.submit()`. In the CLI it ends the command. In the dashboard the task shows "stopped with an error". The job
stays `APPROVED`, which is recoverable but noisy. If it happens during or after the click, `retry_status` is already
`in_progress`, and the next run correctly marks it `SUBMISSION_UNVERIFIED`. That is safe, but the reason recorded is
"interrupted" rather than the real error. A `select_option(label=…)` mismatch waits out the full 15 s timeout and then
raises `PWTimeout`, which is not caught on the fill path either.

**Evidence:** probe `test_probe_playwright_error_escapes_submit`. After an injected `PWError`, the state was
`APPROVED / in_progress` and the exception propagated. The next run returned `SUBMISSION_UNVERIFIED`.

**Proposed fix:** in the attempt loop, catch `playwright.sync_api.Error` separately. Before `retry_status=in_progress`,
treat it as `TransientError` (retry, then FAILED). After it, call `_record(... "SUBMISSION_UNVERIFIED" ...)`
immediately with the error text and evidence. Also catch `PWTimeout` in `GenericFormAdapter.fill()` and raise
`NeedsHuman(f"could not set {f.label}")`.

### M2. Forms that submit by script are always recorded as unverified (Inferred)

**Where:** `jobagent/apply/adapters.py:237-240` uses `page.expect_navigation(...)` around the click.

**What:** many hosted ATS forms submit by `fetch`/XHR and swap the DOM in place without a page navigation. The newer
Greenhouse `job-boards.greenhouse.io` and Lever both behave this way, though I couldn't check them live from here.
For those, `expect_navigation` times out after 15 s, so every real application would be recorded as
`SUBMISSION_UNVERIFIED` ("no response after submit"), even when the success message is on screen. The local test ATS
always navigates, so the tests don't see this.

**Proposed fix:** after the click, wait for whichever comes first: a navigation, a `SUCCESS`/`DUPLICATE`/error text
appearing (`page.wait_for_function` on `document.body.innerText`), or the form going away. Then call `result()` as
usual. Add a test-portal variant that submits by `fetch()` and renders the confirmation in place.

### M3. A redirect to another domain after submitting records the application as FAILED (Inferred)

**Where:** `adapters.py:241` (`_check_domain()` after the click) raises `FatalError`, which `engine.py:246-248` turns
into `_fail()`, setting `FAILED` with `retry_status=not_retryable`.

**What:** some employers send applicants to a thank-you page on their own site after a Greenhouse or Lever
submission. The click has already happened (`retry_status` was `in_progress`), but the record says FAILED with
"navigation blocked". That is wrong, and a FAILED job can later be re-approved and resubmitted. Test 13b only covers a
redirect *before* the form.

**Proposed fix:** in `submit()`, after the click, do not raise on a domain violation. Return normally and let the
engine record `SUBMISSION_UNVERIFIED` with the blocked URL in the evidence ("redirected to X after submit; check
your email").

### M4. Reference-number regex accepts ordinary words, so an unconfirmed submission is marked verified (Verified)

**Where:** `jobagent/apply/adapters.py:101` (`REFERENCE`, case-insensitive, `[A-Z0-9][A-Z0-9-]{3,}`).

**What:** "Thank you for your application. **Reference checks** happen after interviews." gives reference `checks`.
With the success message present, `engine.py:227-228` records **SUBMITTED, verified=1, reference "checks"**.

**Proposed fix:** require at least one digit in the captured reference
(`(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{3,}`), match case-sensitively for the value, and require a `:`/`#`/"is" between
the label and the value.

### M5. Different alert jobs with an unparsed company merge into one job (Verified)

**Where:** `jobagent/normalize.py:60-62` (dedup key = employer|title|location) with `jobsites.py:564` (company
defaults to `"Unknown employer"`, location often empty).

**What:** two different LinkedIn jobs titled "Backend Engineer" from alerts where the company line didn't parse both
get the key `unknown employer|backend engineer|`. The second becomes a "duplicate" of the first: it is not created,
and its source row points at the first job. So you never see the second job.

**Evidence:** probe `test_probe_unknown_employer_alerts_merge` merged LinkedIn jobs 4000000001 and 4000000002 into
job 1.

**Proposed fix:** when the employer is unknown, or the source is `job_alerts`/`saved_pages`, include the site job id
in the key (for example `basis += "|" + rec.source_id`) or skip the fuzzy key-based merge. Match only on
`source_id` or the canonical URL.

### M6. Saving the full page after an alert never re-scores the job or fixes the employer (Verified)

**Where:** `jobagent/normalize.py:101-120` (merge) and `pipeline.py:81` (only `DISCOVERED` is screened).

**What:** the alert note tells you to *"Save the job page … for the full description and a better score."* When you
do, the saved page merges into the existing job (same `source_id`), but:
* the job is not re-screened or re-scored, so its status, score, documents and answers still come from the summary;
* `employer` and `title` are not in `_MERGE_FIELDS`, so a job stored as "Unknown employer" keeps that name. Your
  `excluded_employers` list (where you'd put your current employer) is then never checked against the real name;
* the description is replaced only if the new one is 20% longer than "summary + note".

**Evidence:** probe `test_probe_alert_then_saved_page_never_rescored`: same job, status stayed `READY_FOR_REVIEW`,
employer stayed "Unknown employer" while the page said "Paymint (synthetic)".

**Proposed fix:** in `upsert_job`, if the stored row came from a summary-only source (`raw.summary_only`) and the new
record is a full page:
* overwrite description, employer and title (with an audit entry);
* reset the status to `DISCOVERED` if nothing was approved or submitted yet, so the next `process` re-screens and
  re-prepares it.

Never reset a job that has `approved_at` or `submitted_at`.

### M7. Salary filter ignores the pay interval (Inferred)

**Where:** `jobagent/filtering.py:197-199`. Sources do fill `salary_interval` (`web.py:60`, `ats.py:78`).

**What:** a Lever posting with `salaryRange {interval: "per-month"}` or a JSON-LD `unitText: "HOUR"` is compared
as-is against the annual minimum (`Salary.interval = "year"`). Monthly and hourly salaries are always below an annual
minimum, so those jobs get hard-rejected.

**Proposed fix:** normalize to the candidate's interval before comparing (hour ×2080, week ×52, month ×12). If the
interval is unknown or unrecognised, warn instead of disqualifying.

### M8. One email or page with an unknown charset makes the whole source fail (Verified)

**Where:** `jobagent/sources/jobsites.py:449` (`payload.decode(part.get_content_charset() …)`), the same pattern at
`jobsites.py:404` for `.mhtml`, and `SavedPagesSource.fetch` at `jobsites.py:684`, which only catches
`PageBlocked, ValueError`.

**What:** real mail sometimes declares charsets Python doesn't know (`unknown-8bit`, `x-user-defined`, typos).
`LookupError` is not a `ValueError`, so one such message ends `JobAlertSource.fetch()` and every other alert in the
folder or IMAP box is lost for that run. The same goes for an unreadable file (`OSError`) in the saved-pages folder.

**Evidence:** `parse_alert_email(<charset=unknown-8bit>)` → `LookupError: unknown encoding: unknown-8bit` (probe
`test_probe_bad_charset_raises`).

**Proposed fix:** add a helper `_decode(payload, charset)` that tries the declared charset, falls back to utf-8 with
`errors="replace"`, and catches `LookupError`. In both sources, catch `Exception` per message or file, add it to
`problems`, and continue.

### M9. Naukri `?jobId=` URLs drop out of the URL queue (Verified)

**Where:** `jobagent/sources/jobsites.py:139-141` builds the canonical URL `https://www.naukri.com/job-listings-<id>`.
`_NAUKRI_LISTING` (`jobsites.py:110`) needs a slug before the id, so `identify()` can't read that URL back.
`queued_urls()` (`jobsites.py:704-707`) re-identifies each stored line.

**What:** `add_urls()` accepts the URL and writes the canonical form, but when it reads the file back the URL is
dropped. It never shows as "pending", and the page you save for it can't be matched to it.

**Evidence:** `add_urls(q, ["https://www.naukri.com/job-detail?jobId=123456789012"])` added 1, then
`queued_urls(q) == []` (probe `test_probe_naukri_queue_roundtrip`).

**Proposed fix:** store the URL as given, or make the canonical form something `identify()` accepts
(`https://www.naukri.com/job-detail?jobId=<id>`). Better still, make `_NAUKRI_LISTING` accept `job-listings-<id>`
with no slug. Add a round-trip test: `identify(identify(u).url) == identify(u)` for every URL shape in the existing
tests.

### M10. Indeed tracking-host links produce a wrong canonical URL (Verified)

**Where:** `jobagent/sources/jobsites.py:132`.

**What:** any host with two or more dots is kept, so alert links on Indeed's click-tracking hosts give
`https://cts.indeed.com/viewjob?jk=…` (also `engage.`, `click.` style hosts). That is not a job page, so the "open the
job" link in the apply kit may not work. The same job seen from `in.indeed.com` gets a different `original_url`
(dedup still works through `source_id`).

**Evidence:** `identify("https://cts.indeed.com/v3/abc?jk=0123456789abcdef").url == "https://cts.indeed.com/viewjob?jk=0123456789abcdef"`.

**Proposed fix:** keep only known job hosts (`www`, or a two-letter country subdomain such as `in.`/`uk.`, or a country
TLD such as `indeed.co.in`). Map everything else to `www.indeed.com`.

### M11. `unwrap()` replaces a real job link that carries a `url=` parameter (Verified)

**Where:** `jobagent/sources/jobsites.py:84-105`.

**What:** `unwrap()` follows any `url`/`q`/`u`/`r`/`target`/`redirect…` parameter on *any* host, including the job
sites themselves. A LinkedIn job link with a tracking parameter that contains another URL is replaced by that URL,
and the job disappears from the alert.

**Evidence:** `identify("https://www.linkedin.com/comm/jobs/view/3812345678/?url=https%3A%2F%2Fexample.com")` returns
`None`.

**Proposed fix:** first try `identify()` on the URL as given, and only unwrap when the host is not a job site. Or
restrict unwrapping to known wrapper hosts (`google.com/url`, `*.safelinks.protection.outlook.com`, `l.facebook.com`,
known click-trackers).

### M12. Setting a disallowed tracking status in the dashboard returns HTTP 500 (Verified)

**Where:** `jobagent/web/app.py:214-217` calls `db.set_status()` without handling `InvalidTransition`.

**What:** choosing "INTERVIEW" or "OFFER" for a job that is not SUBMITTED gives an "Internal Server Error" page. This
is easy to hit for LinkedIn, Indeed and Naukri jobs when you forgot to click "mark as submitted" first.

**Evidence:** probe `test_probe_tracking_invalid_transition_500`: READY_FOR_REVIEW → INTERVIEW returned 500.

**Proposed fix:** catch `InvalidTransition` and redirect back with `?err=` ("mark it as submitted first"). Or, for
INTERVIEW and OFFER on an unsubmitted job, offer to record the manual submission first. Also return 404 for an
unknown `job_id` in `/tracking` and `/submit` (`app.py:128` indexes `None` if the mode check passes).

### M13. `jobagent run --loop` dies on the first exception and never reloads config (Inferred)

**Where:** `jobagent/cli.py:379-390`.

**What:** there is no `try/except` around an iteration, so any error from discovery, processing (see H5) or apply (see
M1) ends the unattended loop. `settings` is loaded once, so mode or source changes made in the dashboard are not
picked up until you restart.

**Proposed fix:** catch exceptions per iteration, log and audit them, and continue. Reload `load_settings()` (and the
profile) at the start of each iteration.

### M14. One SQLite connection is shared across the dashboard's worker threads (Inferred)

**Where:** `jobagent/db.py:166` (`check_same_thread=False`) with `web/app.py:25` (one `Database` for the whole app).
FastAPI runs the sync `def` endpoints in a thread pool.

**What:** two overlapping requests share one connection and transaction. One request's `commit()` or `rollback()` in
`Database.tx()` can commit or roll back the other's half-done writes. With one user this is rare, but the job page
polls tasks while forms are posted. Background tasks correctly use their own connection (`web/tasks.py:79`).

**Proposed fix:** open a connection per request (a FastAPI dependency that creates `Database(settings.db_path)` and
closes it), or guard `Database` methods with a `threading.RLock` held for the whole `tx()` block.

---

## Low

### L1. Some question rules are too broad (Verified for two)

**Where:** `jobagent/answers.py:24-40`.

* "Do you have experience building **mobile** apps?" → `phone` → fills your phone number (verified). On a Yes/No
  select it becomes `needs_input`, but a text box would get the number.
* "Are you open to **joining** an early-stage startup?" → `notice_period` → "60 days" (verified).
* "…payroll or **compensation** systems?" → `salary`, and "Do you have a **degree** in CS?" → education level
  (inferred, same mechanism).

**Fix:** anchor these rules (`^(phone|mobile)( number)?\b|contact number`, `notice period|when can you (start|join)`,
`expected (salary|ctc|compensation)`, `highest (level of )?(education|degree)`). When a question is a yes/no select and
the rule produces a non-yes/no value, return `needs_input`.

### L2. A linked company name in a LinkedIn alert drops company and location (Inferred)

**Where:** `jobagent/sources/jobsites.py:507-511`.

Any link that is not a job link ends the current job block. In alerts where the company name links to
`linkedin.com/company/...`, the company and location text after it are lost. The SYNTHETIC fixtures have no company
links, so the tests pass. **Fix:** only end a block on links to other hosts or on unsubscribe/footer links. Treat
same-site non-job links inside a block as text.

### L3. Config is not validated (Inferred)

**Where:** `jobagent/config.py:89-98`, `onboarding.py:280-297`.

* Unknown keys in `config.yaml` are silently ignored, so a typo like `max_age_days` does nothing. Values are not
  type-checked (`weights: null` crashes with `AttributeError`).
* `JOBAGENT_HOME=./var` (the `.env.example` default) is resolved against the current directory, so running the CLI
  from another folder silently creates a new, empty home.
* `update_config()` rewrites `config.yaml` with `yaml.safe_dump`, which deletes every comment from the template, and
  writes non-atomically (a crash mid-write leaves a truncated file).
* `JOBAGENT_MODE` in `.env` silently overrides the mode set from the dashboard on the next start.

**Fix:** validate settings with a pydantic model and warn on unknown keys. Resolve a relative `JOBAGENT_HOME` against
the project folder (where `.env` lives). Write config to a temp file, then `os.replace`. Show "mode is pinned by
JOBAGENT_MODE" on the Run page.

### L4. The setup form writes any `mode` value (Inferred)

**Where:** `jobagent/web/app.py:329-336`.

`/setup/details` passes the posted `mode` to `update_config()`, which only refuses `auto`. Any other value (for
example a typo) is written to `config.yaml` and `settings.mode`, and the next `load_settings()` then raises
`mode must be one of …`, so the dashboard won't start. **Fix:** accept only `discovery`/`review` here, as
`/settings/mode` does.

### L5. Policy checks are loose (Inferred)

**Where:** `jobagent/apply/policy.py:131` and `policy.py:138`.

* `excluded_employers` in the auto-apply consent is compared by exact string, unlike the screening check, which uses
  `norm_employer`. "PayU" and "PayU Payments Pvt Ltd" don't match. **Fix:** compare `norm_employer` values.
* The daily limit counts every row with `submitted_at` in the last 24 hours, including ones you submitted yourself on
  LinkedIn, Indeed or Naukri, so manual work uses up the auto quota. **Fix:** count only rows whose verification was
  done by the agent (`verification.by` is absent, or `actor='agent'`).

### L6. Launchers never reinstall dependencies (Inferred)

**Where:** `start.sh:11-15`, `start.bat:9-13`.

`.venv/.installed` is created once, so a later change to `requirements.txt` (for example a Playwright bump) is never
installed. **Fix:** store a hash of `requirements.txt` in `.installed` and reinstall when it changes. `start.bat` is
still untested on Windows (per project notes).

### L7. Currency written after the numbers is not parsed (Verified)

**Where:** `jobagent/sources/base.py:165-166`.

"Salary: 12,00,000 - 18,00,000 INR" gives `(None, None, None)`. **Fix:** allow an optional currency token after the
second number.

### L8. naukrigulf.com job URLs are refused when pasted (Verified)

**Where:** `jobagent/sources/jobsites.py:135`. `site_of()` recognises naukrigulf.com (line 155), but `identify()`
doesn't, so pasted Naukri Gulf job URLs are rejected as "not a LinkedIn/Indeed/Naukri job URL". **Fix:** add the
`-jid-<digits>` pattern, or say clearly in the UI that Naukri Gulf isn't supported.

### L9. The dry-run guard lets GET requests and WebSockets through (Inferred)

**Where:** `jobagent/apply/adapters.py:129-137`.

Read-only mode aborts every non-GET request, but GET beacons (analytics, autosave endpoints that use query strings)
and WebSocket traffic still go out. That is acceptable for "nothing is submitted", but the dry-run report should not
claim that no data could leave the page. **Fix:** in read-only mode, also abort cross-origin GETs to hosts outside
`allowed`, and reword the report line.

---

## What looks solid

* Hard stops for LinkedIn, Indeed and Naukri are enforced in three independent places (`capability_for`,
  `allowed_domains_for`, and `GenericFormAdapter.__init__`/`open`), and auto-apply consent can't include them.
* Idempotency around the submit click (`retry_status=in_progress` → `SUBMISSION_UNVERIFIED` on the next run) works
  as designed (seen in the M1 probe).
* Secrets are never written to `config.yaml`. The dashboard's IMAP setting rejects anything that isn't an
  environment-variable name. The audit log is append-only by trigger. File downloads are confined to `documents_dir`.
* Exported HTML is escaped (`report.py:81-92`).

## Reproducing

```
cd job-applier
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python -m pytest -q                                   # 106 passed
cp docs/audit_2026-11-06/test_audit_probes.py tests/test_zz_audit_probes.py
python -m pytest -q tests/test_zz_audit_probes.py -s  # 12 passed = every probed bug is present
rm tests/test_zz_audit_probes.py
```
