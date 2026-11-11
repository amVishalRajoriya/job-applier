# Job Applier: put it online and use it

This guide has two parts:

- **Part A** gets you your own web address for the app (about 5 minutes, free, done once).
- **Part B** walks through using the app, with a screenshot of every step.

Every screenshot is from the real app running in the same container that you will deploy. The person in them
is a made-up test candidate ("Asha Testcandidate"). In Part B steps 5 to 9 the jobs come from the app's
built-in **test job board**, because the machine that took the screenshots has no access to real job sites.
Your copy, once hosted, fetches the real boards you list.

---

## Part A: put it online (one time)

**Where it runs:** a free Hugging Face Space (2 CPUs, 16 GB of memory, no card needed). The app runs in a Docker
container that already has the Chromium browser it needs to fill application forms.

**Who can see your data:** the web address is public, but every page needs your password. Your resume and
details are stored encrypted inside the running container and nowhere else.

**What to know first:** the free Space keeps no disk. Everything you upload is **wiped when the Space restarts**
(after 48 hours with no visits, or whenever you redeploy). Treat it as a test bench: export what you want to keep
(Jobs page → Export CSV / JSON / HTML) and download tailored resumes from each job page.

The code is already in your private GitHub repository `amVishalRajoriya/job-applier`. A GitHub Action in it
creates the Space, sets the password on it and deploys the app. It needs two secrets from you.

### A1. Get a Hugging Face token

1. Sign up or log in at <https://huggingface.co> (free).
2. Open <https://huggingface.co/settings/tokens/new?tokenType=write>, name it `github-deploy`, press
   **Create token** and copy it (it starts with `hf_`).

### A2. Add two secrets to the GitHub repository

Open <https://github.com/amVishalRajoriya/job-applier/settings/secrets/actions/new> and add:

| Name | Value |
|---|---|
| `HF_TOKEN` | The Hugging Face token you just copied |
| `DASHBOARD_PASSWORD` | A long password you make up (16+ characters). You type it to open the app. |

Optional: a third secret `ANTHROPIC_API_KEY` with your Claude API key. Without it, cover letters and resume wording
use plain templates built only from your resume.

### A3. Deploy

1. Open <https://github.com/amVishalRajoriya/job-applier/actions/workflows/deploy-huggingface.yml> and press
   **Run workflow** → **Run workflow**. (Every later push to `main` redeploys by itself.)
2. When the run turns green (under a minute), open it. Its summary shows your **app address**, which looks like
   `https://<your-hf-username>-job-applier.hf.space`, and a link to the build log.
3. The first build on Hugging Face takes 3 to 5 minutes. Then open the app address directly (not the
   huggingface.co/spaces page; the login cookie only works on the direct address).

If the Space shows **Runtime error**, open its **Logs** tab. "Refusing to start" means the password secret did not
arrive: check `DASHBOARD_PASSWORD` and run the workflow again.

<details><summary>Alternative host: Render</summary>

The repository also contains `render.yaml`. On <https://render.com>: **New** → **Blueprint** → pick the
repository → set `JOBAGENT_DASHBOARD_TOKEN` when asked. Render's free plan has 512 MB of memory and sleeps
after 15 idle minutes, which also wipes your data, so Hugging Face is the better free choice.
</details>

---

## Part B: use the app

### 1. Log in

Open your Space address and type the password you set as `JOBAGENT_DASHBOARD_TOKEN`. You stay logged in on that
browser for 30 days. **Log out** is at the top right.

![Login page](guide/img/01_login.png)

### 2. Upload your resume

You land on **Setup**. Under **1. Master resume**, press **Choose File**, pick your resume (PDF with selectable
text, DOCX, TXT or MD), then press **Upload and read**.

![Empty setup page](guide/img/02_setup_empty.png)

The app shows what it read: each role with dates and number of bullets. **Check this list against your
resume.** Parsing is heuristic. If a role is merged, split or missing, fix it in step 4.

![Resume read: 3 roles, 9 bullets](guide/img/03_resume_read.png)

Tip: a scanned PDF (an image of text) cannot be read. Export your resume from Word or Google Docs as PDF or DOCX.

### 3. Fill in your details, skill levels and job boards

**2. Your details.** Name, email, phone and LinkedIn come from your resume. The orange line lists what is
still blank. Nothing is guessed: a blank field means applications that ask for it pause and ask you.
Country fields use two-letter codes, for example `IN=citizen` and `IN=no, US=yes`.

![Your details form](guide/img/04_details.png)

**3. Skill levels.** Give your honest level for each skill found on your resume. Scoring and the resume summary
use it.

![Skill levels](guide/img/05_skills.png)

**4. Job boards to watch.** Paste company careers URLs, one per line, optionally as `Employer | URL`.
Greenhouse (`boards.greenhouse.io/<company>`), Lever (`jobs.lever.co/<company>`), Ashby and Workday boards work,
as do careers pages that publish job data and RSS/JSON feeds. Leave **Mode** on *discovery* for now.
Press **Save details and boards**.

![Job boards](guide/img/05b_boards.png)

LinkedIn, Indeed and Naukri cannot be watched as boards (their terms forbid automated use). Section 5 of the
Setup page explains the safe alternative: forward their alert emails or save job pages.

### 4. (If needed) correct the parsed profile

**6. Check the parsed profile** at the bottom of Setup is a text editor with everything the app knows about you.
Fix titles, employers, dates or bullets here and press **Save profile**. You can also add true achievements that
are not on your resume with `on_master_resume: false`. The app never invents experience; it only reorders and
rewords what is here.

![Profile editor](guide/img/05c_profile_yaml.png)

### 5. Check your setup on the Run page

Open **Run**. **Before you start** should show green ticks for resume, details, encryption and boards.

![Run page checklist](guide/img/07_run_checklist.png)

### 6. Check job boards

Press **Check boards**. Each board is fetched once and nothing is stored. Each line says OK with a job count,
or FAIL with the reason. Fix or remove failing boards on Setup before going on.

![Check boards result](guide/img/08_check_boards.png)

If every board fails with "Name or service not known" or a timeout, the server cannot reach the job sites.
This is what that looks like:

![Boards unreachable](guide/img/99_unreachable_boards.png)

### 7. Find and prepare jobs

Press **Find jobs**. The live log shows each stage. The app finds new jobs, drops those that break your hard
constraints (location, sponsorship, salary floor, excluded employers), scores the rest against your resume,
makes a tailored resume and cover letter for each, and opens each application form read-only to list its
questions. With 10 to 20 jobs this takes about a minute. Only one run happens at a time.

![Find jobs done](guide/img/10_find_jobs_done.png)

### 8. Review the jobs

Open **Jobs**. Each row shows the status, whether it meets your criteria, the score before and after tailoring,
and the missing qualifications. Warnings such as *CAPTCHA present* or *page does not match* appear under the
status. The score is this tool's own rubric, not what the employer's system computes.

![Jobs list](guide/img/11_jobs_list.png)

Click a job title to open it.

![Job page](guide/img/12_job_top.png)

**Match score:** the score per category, what is missing, and transferable skills.

![Score breakdown](guide/img/13_job_score.png)

**Resume changes:** exactly what was changed for this job (reordered bullets, skills first) and the gaps it
did not paper over. The tailored resume and cover letter download from the **Files** links (DOCX, PDF, TXT).

![Resume changes](guide/img/14_resume_changes.png)

**Application answers:** every question on the employer's form, the answer the app will give, and where the
answer came from. Questions it must not answer for you (such as gender) show a dropdown. Pick your answer and
press **Save answer**.

![Application answers](guide/img/15_answers.png)

### 9. Dry run, then approve

Press **Dry run (never submits)**. The app opens the real application form, fills every field, uploads the
resume and cover letter, takes a screenshot, and stops before submitting. Any request that could send data
is blocked.

![Dry run log](guide/img/16_dry_run.png)

The **Last dry run** box lists every field and what was put in it. Open **Screenshot of the filled form** to see
the page itself. When it looks right, press **Approve documents and answers**.

![Dry run result](guide/img/16b_dry_run_screenshot.png)

### 10. Submit

Submitting is off until you allow it. On **Run**, choose **review: submit only applications I approve** and
press **Set mode**.

![Review mode](guide/img/17_review_mode.png)

Then press **Submit** on an approved job's page (it asks you to confirm), or **Submit N approved** on the Run page.
A job counts as **SUBMITTED** only when the employer's confirmation page shows a reference number. Otherwise
it is marked *SUBMISSION_UNVERIFIED* for you to check.

![Submitted with reference](guide/img/18_submitted.png)

**Start with one job.** Use your real resume, dry-run one real posting, read the filled form screenshot
carefully, and only then submit one. The Greenhouse and Lever form fillers have been tested on the test board
and on real posting data, but not yet by submitting to a real employer.

### 11. Keep your results

The free Space wipes its data on restart. Before you leave:

- **Export** (top bar): CSV, JSON or HTML of every job and status.
- On each job page, download the tailored resume and cover letter from **Files**.
- **Audit log** (top bar) records every action the app took.

---

## Quick reference

| I want to… | Go to |
|---|---|
| Change my details or boards | Setup → sections 2–4 → Save details and boards |
| Fix what the resume parser read | Setup → section 6 → Save profile |
| Find new jobs | Run → Find jobs |
| See why a job was skipped | Jobs → click the job → Screening |
| Fill a form without sending it | Job page → Dry run (never submits) |
| Allow submissions | Run → Mode: review → Set mode |
| Stop submissions | Run → Mode: discovery → Set mode |
| Turn on Claude-written wording | GitHub secret `ANTHROPIC_API_KEY` → run the deploy workflow again (the restart wipes data) |
