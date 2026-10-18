# Sample end-to-end run (SYNTHETIC TEST DATA, local test ATS only)

## 1. Parse the master resume
Parsed 3 roles, 9 bullets, 12 skills. Fields left for the candidate: work_authorization.authorized, work_authorization.requires_sponsorship, salary.expected, notice_period_days, preferences.willing_to_relocate
Imported the verified synthetic profile (encrypted at rest).

## 2. Discover
```
{
  "fetched": 18,
  "new_jobs": 15,
  "merged_duplicates": 3,
  "errors": []
}
```
## 3. Screen, score, optimize, prepare
```
{
  "screened": 15,
  "passed": 11,
  "scored": 11,
  "optimized": 1,
  "ready": 10,
  "closed": 1,
  "errors": []
}
```
| Job | Employer | Status | Baseline | Final | Outcome / reason |
|---|---|---|---|---|---|
| 15 | Expired Co (synthetic) | SCREENED |  |  | not relevant to target roles (relevance 0.00) |
| 14 | Visionary (synthetic) | SCREENED |  |  | not relevant to target roles (relevance 0.00) |
| 13 | Shopwell (synthetic) | READY_FOR_REVIEW | 49.6 | 67.4 | below_target_honest |
| 12 | Promptco (synthetic) | READY_FOR_REVIEW | 65.6 | 65.6 | not_required |
| 11 | Mismatchco (synthetic) | READY_FOR_REVIEW | 66.6 | 66.6 | not_required |
| 10 | Redirecto (synthetic) | READY_FOR_REVIEW | 66.6 | 66.6 | not_required |
| 9 | Closedco (synthetic) | CLOSED | 66.6 | 66.6 | not_required |
| 8 | Vagueco (synthetic) | READY_FOR_REVIEW | 66.6 | 66.6 | not_required |
| 7 | Formerr (synthetic) | READY_FOR_REVIEW | 66.6 | 66.6 | not_required |
| 6 | Flakyco (synthetic) | READY_FOR_REVIEW | 66.6 | 66.6 | not_required |
| 5 | Captchaco (synthetic) | READY_FOR_REVIEW | 66.6 | 66.6 | not_required |
| 4 | Quickcash (synthetic) | SCREENED |  |  | likely fraudulent listing: Pay a registration fee; registration fee; Interview via Telegram; Send your bank de |
| 3 | Statesider (synthetic) | SCREENED |  |  | location New York, NY, United States is outside preferred locations and candidate will not relocate; requires  |
| 2 | Ledgerly (synthetic) | READY_FOR_REVIEW | 54.8 | 54.8 | not_required |
| 1 | Paymint (synthetic) | READY_FOR_REVIEW | 66.6 | 66.6 | not_required |

### Optimization detail: job 13 (Shopwell, synthetic)

Baseline 49.6 -> final 67.4 (below_target_honest).
- add_project proj1: relevant verified project (+12.0)
  - after: Event-driven ledger (personal project)
- add_bullet exp1: verified achievement not on the master resume matches the job (+5.8)
  - after: Added Prometheus metrics and Grafana dashboards for payment API latency and error rates.
- summary summary: summary restated from verified facts relevant to the job
  - after: Senior Software Engineer with 8 years of experience in payments, working with Python, REST APIs, Prometheus, Grafana and FastAPI.
- reorder_bullets exp1: most relevant evidence first
  - after: exp1.b6, exp1.b1, exp1.b4, exp1.b2, exp1.b3
- reorder_skills skills: requested skills first
Remaining gaps (not invented): skill: GraphQL; skill: Terraform; preferred: Experience with distributed systems; preferred: Go; domain: ecommerce

## 4. Review mode: candidate answers the unexpected question and approves
Answered for job 2: 'Describe your experience maintaining COBOL mainframe batch systems'

## 5. Apply
- job 1: **submitted** -> SUBMITTED, reference TP-5001
- job 2: **submitted** -> SUBMITTED, reference TP-5002
- job 5: **needs_human** -> READY_FOR_REVIEW (CAPTCHA present)
- job 6: **submitted** -> SUBMITTED, reference TP-5003
- job 7: **submitted** -> SUBMITTED, reference TP-5004
- job 8: **unverified** -> SUBMISSION_UNVERIFIED (could not recognise the page after submit)
- job 10: **failed** -> FAILED (blocked navigation to unapproved domain 'localhost')
- job 11: **failed** -> FAILED (page does not match 'Backend Engineer' at 'Mismatchco (synthetic)' (employer ok=False, title ok=False))
- job 12: **submitted** -> SUBMITTED, reference TP-5006
- job 13: **submitted** -> SUBMITTED, reference TP-5007

## 6. Duplicate protection
Re-submitting job 1: duplicate_blocked

## 7. Auto-apply needs explicit consent
Without consent, job 5: auto-apply has no active candidate consent
With consent, CAPTCHA job 5: CAPTCHA present

## 8. Final summary
```
{
  "jobs_discovered": 15,
  "meets_criteria": 11,
  "by_status": {
    "CLOSED": 1,
    "FAILED": 2,
    "READY_FOR_REVIEW": 1,
    "SCREENED": 4,
    "SUBMISSION_UNVERIFIED": 1,
    "SUBMITTED": 6
  },
  "verified_submissions": 6
}
```

Audit log entries: 125
