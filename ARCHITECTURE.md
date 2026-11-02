# Architecture and milestones

## Stack decision

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ (tested on 3.13) | Best library support for document parsing, Playwright and the Anthropic SDK; one language end to end. |
| Persistence | SQLite (stdlib `sqlite3`, WAL mode) | Zero-ops, single-user tool, file-level backup; schema in `jobagent/db.py`. Swappable later for Postgres. |
| Web dashboard | FastAPI + Jinja2, server-rendered | No JS build chain; binds to 127.0.0.1 by default with an optional bearer token. |
| Browser automation | Playwright (Chromium) | Robust waits, isolated contexts per run, screenshots as submission evidence. |
| Language model | Anthropic Python SDK (`claude-opus-5-5`), optional | Used only for drafting text (bullet rewrites, cover letters) and requirement extraction hints. Every model output passes the deterministic truth guard. The system is fully functional without an API key. |
| Documents | `pypdf`, `python-docx` (input), `python-docx` + HTML/TXT (output) | ATS-friendly single-column DOCX. |
| Secrets | Environment variables; Fernet (`cryptography`) encryption of the profile at rest | No passwords are ever stored; logins happen manually in an isolated persistent browser profile. |
| Tests | `pytest`, a local test ATS (`testportal/`) served by uvicorn | Every connector and the application flow is exercised against local fixtures. |

## Module map

```
jobagent/
  config.py         settings from env + config.yaml (weights, mode, thresholds)
  db.py             schema, repositories, status transitions, idempotency keys
  audit.py          append-only audit log (who/what/why, before/after)
  security.py       encryption, domain allowlist, untrusted-text handling, injection detection
  http.py           polite HTTP client: timeouts, retries, per-host rate limit, robots.txt
  profile.py        candidate profile schema, load/validate, encrypted storage
  resume_parser.py  PDF/DOCX/TXT/MD master resume -> structured resume
  skills.py         skill taxonomy, aliases, implications, domains, role families
  sources/          job discovery connectors + capability registry
                    (jobsites.py: LinkedIn/Indeed/Naukri from alert emails + saved pages; never fetches those sites)
  capture.py        opt-in supervised page capture in a visible browser the candidate controls
  normalize.py      normalization and cross-source deduplication
  requirements.py   JD -> typed requirements (required/preferred/responsibility/eligibility)
  filtering.py      eligibility + relevance + fraud/expiry screening
  scoring.py        deterministic, explainable rubric with configurable weights
  optimizer.py      evidence-based resume tailoring (never invents)
  truth_guard.py    verifies every tailored claim traces to profile evidence
  documents.py      resume rendering (DOCX/TXT/HTML), cover letters
  answers.py        screening-question answering from candidate-approved data only
  llm.py            Claude integration (optional), always behind truth_guard
  apply/            application engine, authorization policy, Playwright adapters
                    (assisted.py: kit + candidate-recorded submission for LinkedIn/Indeed/Naukri)
  pipeline.py       discover -> screen -> score -> optimize -> prepare
  report.py         CSV / JSON / HTML export
  web/              dashboard
  cli.py            command line entry point
testportal/         local fake ATS + job board APIs used by tests and the demo
```

Model decisions (text drafting) are separated from deterministic rules (eligibility, scoring, authorization, submission, verification). The model never decides whether to apply, never sees credentials, and its output is rejected unless the truth guard can trace every claim to evidence.

## Milestones

1. Profile + resume ingestion, DB, audit log.
2. Sources (Greenhouse, Lever, Ashby, Workday, JSON-LD career pages, RSS/JSON feeds, manual import) + dedup.
3. Requirements parser, eligibility filter, scoring rubric.
4. Truthful optimizer + truth guard + document generation + answers.
5. Application engine + Playwright adapter + local test ATS + verification.
6. Dashboard, reports, scheduler/retries.
7. Acceptance test suite, docs, sample end-to-end run.
