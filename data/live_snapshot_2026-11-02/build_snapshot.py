"""Builds board-API-shaped JSON from REAL public postings read on 2026-11-02.

Exact: ids, titles, locations, dates, URLs, team, workplace type.
Condensed: description text. It was read through Claude's web reader, which returns a faithful summary
and bullet list of each posting rather than the raw HTML, so wording is not verbatim.
"""
import json
from pathlib import Path

OUT = Path(__file__).parent


def html(summary, required, preferred=(), resp=()):
    parts = [f"<h3>About the role</h3><p>{summary}</p>"]
    if resp:
        parts.append("<h3>Responsibilities</h3><ul>" + "".join(f"<li>{x}</li>" for x in resp) + "</ul>")
    parts.append("<h3>Requirements</h3><ul>" + "".join(f"<li>{x}</li>" for x in required) + "</ul>")
    if preferred:
        parts.append("<h3>Nice to have</h3><ul>" + "".join(f"<li>{x}</li>" for x in preferred) + "</ul>")
    return "".join(parts)


gh = {
 "razorpay": ("Razorpay", [
  dict(id=4746983005, title="Senior Software Development Engineer", location={"name": "Bengaluru"},
       updated_at="2026-10-30T07:46:55-04:00", first_published="2026-10-30T07:46:55-04:00",
       absolute_url="https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited/jobs/4746983005",
       content=html("Razorpay, a full-stack fintech company, is hiring a Senior Software Development Engineer in Bengaluru to build payment infrastructure at scale, with a focus on product thinking, code quality and AI-augmented development.",
        ["3+ years of experience in technology", "Proficiency in any one of PHP, Python, Django, Golang, Java or C++",
         "Strong product design sense, focused on the user", "Good understanding of REST APIs and the web in general",
         "Ability to build a feature from scratch and drive it to completion independently", "Clean, well-structured code with awareness of performance and time complexity",
         "Hands-on use of AI coding assistants and critical review of AI-generated code", "Strong low-level design depth"],
        ["Side projects or open-source contributions on GitHub", "Prior experience at a product company",
         "Hands-on experience building with LLMs or agentic frameworks", "Designing systems for scalability, reliability and fault tolerance"],
        ["Scale Razorpay's payment infrastructure", "Shape REST APIs used by developers worldwide", "Drive product and design discussions"]))
  , dict(id=4746948005, title="Lead Security Engineer", location={"name": "Bengaluru"},
       updated_at="2026-10-27T02:49:29-04:00", first_published="2026-10-27T02:49:29-04:00",
       absolute_url="https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited/jobs/4746948005",
       content=html("Product Security Lead at Razorpay in Bengaluru, building Secure Code Review and Threat Modeling capabilities and a self-serve design review program.",
        ["5-8 years of experience performing Secure Code Review and Design Reviews", "Experience working with and extending Static Analysis tools",
         "Intermediate development skills, preferably in Golang or Python", "Understanding of Microservices and Cloud architecture",
         "In-depth understanding of OWASP Top 10 vulnerabilities", "Engineering degree in Computer Science or a related field"],
        ["Semgrep experience", "Master's degree"]))
  , dict(id=4730552005, title="DevOps Enginer", location={"name": "Malaysia"},
       updated_at="2026-09-28T07:02:10-04:00", first_published="2026-09-03T05:17:10-04:00",
       absolute_url="https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited/jobs/4730552005",
       content=html("Permanent full-time DevOps role in Razorpay's Kuala Lumpur office, Malaysia, Monday to Friday, building and maintaining cloud infrastructure.",
        ["1-3 years of hands-on DevOps experience", "Cloud infrastructure experience, preferably AWS", "Docker and Kubernetes",
         "Building AI agents or agentic workflows for DevOps", "Puppet, Chef or Ansible", "Scripting in Shell, Ruby, Perl, Python or Golang",
         "Linux production troubleshooting", "Terraform"],
        ["MariaDB/MySQL", "CI/CD, logging and monitoring stacks", "Open-source contributions"]))]),
 "stripe": ("Stripe", [
  dict(id=6042172, title="Backend Engineer, Core Technology", location={"name": "US-Remote, Chicago, Seattle, San Francisco"},
       updated_at="2026-09-10T13:11:58-04:00", first_published="2024-06-14T18:04:50-04:00",
       absolute_url="https://stripe.com/jobs/search?gh_jid=6042172",
       content=html("Backend engineering role on Stripe Infrastructure, responsible for reliability, scale, performance and cost of Stripe's systems. Locations: US-Remote, Chicago, Seattle, San Francisco (United States).",
        ["Write high-quality code in languages such as Go, Java, or C/C++", "2-12+ years of professional hands-on software development experience",
         "Experience with commonly used data structures and algorithms", "Hands-on experience building large-scale distributed systems"],
        ["Experience optimizing end-to-end performance of distributed systems"]))
  , dict(id=8247127, title="AR Analyst", location={"name": "Bangalore"},
       updated_at="2026-10-09T05:52:10-04:00", first_published="2026-10-09T05:52:10-04:00",
       absolute_url="https://stripe.com/jobs/search?gh_jid=8247127",
       content=html("AR Analyst on Stripe's Finance Operations team in Bangalore, onboarding custom contracts into billing systems and supporting flux analysis.",
        ["4-5 years of financial operations or billing experience, plus a degree in Business Administration or Finance",
         "Strong knowledge of G Suite, Salesforce and MS Office", "Analyzing contracts and large data sets"],
        ["Knowledge of payments and the fintech industry"]))]),
 "databricks": ("Databricks", [
  dict(id=7439442002, title="Engineering Manager - Backend", location={"name": "Bengaluru, India"},
       updated_at="2026-10-11T08:47:24-04:00", first_published="2024-05-07T03:11:26-04:00",
       absolute_url="https://databricks.com/company/careers/open-positions/job?gh_jid=7439442002",
       content=html("One of the first backend engineering managers on the Databricks India team in Bengaluru, building infrastructure for resource management and cloud-agnostic services.",
        ["10+ years of experience with large-scale distributed systems", "Extensive experience as a software engineering leader building teams from the ground up",
         "Experience managing a team of strong software engineers", "BS or higher in Computer Science or a related field"]))]),
}
for token, (company, jobs) in gh.items():
    for j in jobs:
        j["company_name"] = company
    (OUT / f"greenhouse_{token}.json").write_text(json.dumps({"jobs": jobs, "meta": {"total": len(jobs)}}, indent=1))


def lever(id_, text, team, loc, created, desc, req, pref=(), site=""):
    url = f"https://jobs.lever.co/{site}/{id_}"
    return dict(id=id_, text=text, categories={"team": team, "location": loc, "commitment": "Full-time"},
                workplaceType="onsite", createdAt=created, hostedUrl=url, applyUrl=url + "/apply", descriptionPlain=desc,
                lists=[{"text": "Requirements", "content": "".join(f"<li>{x}</li>" for x in req)}]
                + ([{"text": "Nice to have", "content": "".join(f"<li>{x}</li>" for x in pref)}] if pref else []))


lv = {
 "fampay": [lever("01750a61-a53b-433f-8d8c-bfb5a18417f8", "Data Engineer (SDE 2)", "Data", "Bengaluru", 1787294201969,
   "Fam (previously FamPay), an Indian payments app, is hiring a Data Engineer II to build a scalable data lakehouse with sub-minute latency and unified batch and streaming compute.",
   ["3-5 years in data engineering with distributed and cloud-native systems", "Expert Python, PySpark and SQL",
    "Hands-on AWS (S3, EKS, MSK) and Infrastructure-as-Code", "Airflow or Temporal experience", "Proficiency with AI coding tools",
    "Writing technical design documents", "Domain modeling: facts, dimensions, SCDs"],
   ["Real-time CDC with Debezium or PeerDB and Kafka", "Iceberg, Delta or Hudi table formats", "Flink ETL/ELT", "Tuning Trino or ClickHouse"], "fampay")],
 "cred": [lever("9a746e68-0858-4190-9144-54b19a2cbf2c", "cloud security engineer", "cloud and data security", "bengaluru", 1769676380859,
   "CRED is hiring a cloud security engineer in Bengaluru.",
   ["2-6 years of experience in information security", "Fundamentals in computer networks, Linux, containers and secure cloud architecture",
    "Strong programming skills in Python, Go or Bash", "Hands-on cloud security experience in AWS, GCP or Azure",
    "Building and automating custom security tools", "Understanding of microservices, CI/CD pipelines and securing containerized workloads",
    "Least privilege, defence-in-depth and zero trust", "Vulnerability management, patching cycles and CVSS"], (), "cred")],
 "zeta": [lever("dad16204-8051-4517-9e50-966f329045b5", "Cloud Network Engineer II", "Engineering", "Hyderabad", 1787732026874,
   "Zeta, a banking technology company, is hiring a Cloud Network Engineer II in Hyderabad, India (onsite) for its Global SRE team.",
   ["Bachelor's or Master's degree in Computer Science or IT", "2-3 years in Cloud Networking, Infrastructure Automation or DevOps",
    "Hands-on AWS (mandatory), Linux and Terraform", "Kubernetes experience", "Python or Bash scripting for automation",
    "Cloud networking: DNS, load balancers, VPC, security groups", "CI/CD, Git, Helm"], ["Amazon EKS experience"], "zeta")],
}
for site, posts in lv.items():
    (OUT / f"lever_{site}.json").write_text(json.dumps(posts, indent=1))
print("wrote", sorted(p.name for p in OUT.glob("*.json")))
