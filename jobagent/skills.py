"""Skill taxonomy: canonical names, aliases, implications, related skills, domains, role families.

The taxonomy is deliberately conservative. "Implies" means using the left skill
necessarily involves the right one (Django implies Python). "Related" means a
transferable, adjacent skill: it earns partial credit and is reported as
transferable, never as the skill itself.
"""
from __future__ import annotations

import re
from functools import lru_cache

# canonical -> aliases (lowercase). Canonical names are also matched.
SKILLS: dict[str, list[str]] = {
    "Python": ["python3", "python 3"], "Java": [], "Kotlin": [], "Scala": [], "Go": ["golang"],
    "JavaScript": ["javascript", "js", "ecmascript"], "TypeScript": ["typescript", "ts"],
    "C++": ["cpp"], "C#": ["csharp", ".net c#"], "Ruby": [], "PHP": [], "Rust": [], "R": ["r language"],
    "SQL": ["structured query language", "t-sql", "pl/sql"], "Bash": ["shell scripting", "bash scripting", "shell"],
    "Swift": [], "Objective-C": [],
    "Django": [], "Flask": [], "FastAPI": [], "Spring Boot": ["spring", "springboot"], "Node.js": ["node", "nodejs", "node.js"],
    "Express": ["express.js", "expressjs"], "Ruby on Rails": ["rails"], ".NET": ["dotnet", "asp.net"],
    "React": ["react.js", "reactjs"], "Next.js": ["nextjs"], "Vue": ["vue.js", "vuejs"], "Angular": ["angularjs"],
    "HTML": ["html5"], "CSS": ["css3", "sass", "scss"], "Redux": [],
    "REST APIs": ["rest", "restful", "rest api", "restful apis", "rest apis", "restful api", "api design"],
    "GraphQL": [], "gRPC": [], "Microservices": ["microservice", "micro-services", "service-oriented architecture", "soa"],
    "PostgreSQL": ["postgres", "postgresql"], "MySQL": [], "MongoDB": ["mongo"], "Redis": [], "Cassandra": [],
    "DynamoDB": [], "Elasticsearch": ["elastic search", "opensearch"], "Snowflake": [], "BigQuery": [],
    "Redshift": [], "Oracle": ["oracle db"], "SQL Server": ["mssql", "microsoft sql server"],
    "Kafka": ["apache kafka"], "RabbitMQ": [], "Spark": ["apache spark", "pyspark"], "Airflow": ["apache airflow"],
    "Hadoop": [], "dbt": [], "ETL": ["elt", "data pipelines", "data pipeline"],
    "AWS": ["amazon web services", "ec2", "s3", "lambda", "aws lambda"], "GCP": ["google cloud", "google cloud platform"],
    "Azure": ["microsoft azure"], "Docker": ["containers", "containerization"], "Kubernetes": ["k8s", "eks", "gke", "aks"],
    "Terraform": [], "CloudFormation": [], "Ansible": [], "CI/CD": ["ci/cd pipelines", "continuous integration", "continuous delivery", "continuous deployment"],
    "Jenkins": [], "GitHub Actions": [], "GitLab CI": [], "Git": ["github", "gitlab", "version control"],
    "Linux": ["unix"], "Prometheus": [], "Grafana": [], "Datadog": [], "Observability": ["monitoring", "alerting"],
    "Machine Learning": ["ml", "machine-learning"], "Deep Learning": [], "NLP": ["natural language processing"],
    "Computer Vision": [], "LLMs": ["large language models", "llm", "generative ai", "genai"],
    "TensorFlow": [], "PyTorch": [], "scikit-learn": ["sklearn", "scikit learn"], "Pandas": [], "NumPy": [],
    "Statistics": ["statistical analysis", "statistical modeling"], "A/B Testing": ["ab testing", "experimentation", "a/b tests"],
    "Data Analysis": ["data analytics", "analytics"], "Data Visualization": ["dashboards", "dashboarding"],
    "Tableau": [], "Power BI": ["powerbi"], "Looker": [], "Excel": ["microsoft excel", "spreadsheets"],
    "Agile": ["scrum", "kanban"], "Jira": [], "Product Management": ["product manager"], "Roadmapping": ["product roadmap", "roadmaps"],
    "Stakeholder Management": ["stakeholder communication", "cross-functional collaboration", "cross-functional"],
    "System Design": ["distributed systems", "scalable systems", "architecture design"],
    "Unit Testing": ["unit tests", "test automation", "automated testing", "pytest", "junit", "tdd"],
    "Selenium": [], "Playwright": [], "Security": ["application security", "appsec", "infosec"],
    "OAuth": ["oauth2", "openid connect", "oidc"], "PCI DSS": ["pci", "pci-dss"],
    "Payments": ["payment processing", "payment gateway", "payment systems"], "Fraud Detection": ["fraud prevention", "risk models"],
    "Mentoring": ["mentorship", "coaching engineers"], "Technical Leadership": ["tech lead", "led a team", "team lead"],
    "Figma": [], "UX Research": ["user research"], "Salesforce": [], "SAP": [],
    "Communication": ["written communication", "verbal communication"],
}

IMPLIES: dict[str, list[str]] = {
    "Django": ["Python"], "Flask": ["Python"], "FastAPI": ["Python"], "Pandas": ["Python"], "NumPy": ["Python"],
    "scikit-learn": ["Python", "Machine Learning"], "PyTorch": ["Python", "Deep Learning", "Machine Learning"],
    "TensorFlow": ["Deep Learning", "Machine Learning"], "Spark": ["ETL"], "Airflow": ["ETL"], "dbt": ["SQL", "ETL"],
    "Spring Boot": ["Java"], "Node.js": ["JavaScript"], "Express": ["Node.js", "JavaScript"], "React": ["JavaScript"],
    "Next.js": ["React", "JavaScript"], "Vue": ["JavaScript"], "Angular": ["TypeScript"], "Redux": ["React"],
    "TypeScript": ["JavaScript"], "Ruby on Rails": ["Ruby"], "PostgreSQL": ["SQL"], "MySQL": ["SQL"],
    "SQL Server": ["SQL"], "Oracle": ["SQL"], "Redshift": ["SQL"], "BigQuery": ["SQL"], "Snowflake": ["SQL"],
    "Kubernetes": ["Docker"], "GitHub Actions": ["CI/CD"], "GitLab CI": ["CI/CD"], "Jenkins": ["CI/CD"],
    "Deep Learning": ["Machine Learning"], "NLP": ["Machine Learning"], "Computer Vision": ["Machine Learning"],
    "Prometheus": ["Observability"], "Grafana": ["Observability"], "Datadog": ["Observability"],
    "Tableau": ["Data Visualization"], "Power BI": ["Data Visualization"], "Looker": ["Data Visualization"],
}

RELATED_GROUPS: list[list[str]] = [
    ["AWS", "GCP", "Azure"], ["PostgreSQL", "MySQL", "SQL Server", "Oracle"], ["MongoDB", "DynamoDB", "Cassandra"],
    ["Kafka", "RabbitMQ"], ["Java", "Kotlin", "Scala", "C#"], ["TensorFlow", "PyTorch"],
    ["React", "Vue", "Angular"], ["Terraform", "CloudFormation", "Ansible"],
    ["Jenkins", "GitHub Actions", "GitLab CI"], ["Snowflake", "BigQuery", "Redshift"],
    ["Django", "Flask", "FastAPI"], ["Tableau", "Power BI", "Looker"], ["Prometheus", "Datadog", "Grafana"],
    ["Selenium", "Playwright"], ["REST APIs", "GraphQL", "gRPC"], ["Python", "Ruby"], ["Go", "Rust", "C++"],
]

DOMAINS: dict[str, list[str]] = {
    "fintech": ["fintech", "financial services", "banking", "lending", "credit", "neobank", "wealth", "trading"],
    "payments": ["payments", "payment gateway", "card processing", "merchant", "acquiring", "upi", "checkout", "pci"],
    "ecommerce": ["e-commerce", "ecommerce", "retail", "marketplace", "online store"],
    "healthcare": ["healthcare", "health tech", "healthtech", "clinical", "hospital", "patient", "hipaa"],
    "saas": ["saas", "b2b software", "enterprise software"],
    "adtech": ["adtech", "advertising", "programmatic", "ad serving"],
    "logistics": ["logistics", "supply chain", "shipping", "fleet"],
    "edtech": ["edtech", "education technology", "learning platform"],
    "gaming": ["gaming", "game studio", "video games"],
    "security": ["cybersecurity", "security operations", "threat detection"],
    "ai": ["artificial intelligence", "machine learning platform", "ai products", "llm"],
    "insurance": ["insurance", "insurtech", "underwriting", "claims"],
    "telecom": ["telecom", "telecommunications", "5g"],
}

ROLE_FAMILIES: dict[str, list[str]] = {
    "backend": ["backend", "back-end", "back end", "server", "api engineer", "platform engineer"],
    "fullstack": ["full stack", "full-stack", "fullstack"],
    "frontend": ["frontend", "front-end", "front end", "ui engineer", "web developer"],
    "software": ["software engineer", "software developer", "sde", "developer", "programmer", "engineer"],
    "data_eng": ["data engineer", "analytics engineer", "etl developer"],
    "data_sci": ["data scientist", "machine learning engineer", "ml engineer", "applied scientist", "ai engineer"],
    "analyst": ["data analyst", "business analyst", "product analyst", "analyst"],
    "devops": ["devops", "sre", "site reliability", "infrastructure engineer", "cloud engineer"],
    "pm": ["product manager", "product owner", "program manager"],
    "qa": ["qa", "quality assurance", "test engineer", "sdet"],
    "design": ["designer", "ux", "ui/ux"],
    "management": ["engineering manager", "head of engineering", "director of engineering", "vp engineering"],
    "sales": ["account executive", "sales", "business development"],
}

ADJACENT_FAMILIES = {
    "backend": {"fullstack", "software", "devops", "data_eng"},
    "fullstack": {"backend", "frontend", "software"},
    "frontend": {"fullstack", "software"},
    "software": {"backend", "fullstack", "frontend", "devops", "data_eng", "qa"},
    "data_eng": {"backend", "data_sci", "software", "analyst"},
    "data_sci": {"data_eng", "analyst"},
    "analyst": {"data_sci", "data_eng"},
    "devops": {"backend", "software"},
    "qa": {"software"},
    "management": {"backend", "software"},
}


def _alias_index() -> list[tuple[re.Pattern, str]]:
    pairs: list[tuple[str, str]] = []
    for canon, aliases in SKILLS.items():
        pairs.append((canon.lower(), canon))
        pairs.extend((a.lower(), canon) for a in aliases)
    # longest aliases first so "spring boot" wins over "spring"
    pairs.sort(key=lambda p: -len(p[0]))
    out = []
    for alias, canon in pairs:
        if alias == "r":
            rx = re.compile(r"(?<![\w-])R(?![\w-])(?=\s*(?:,|/|\)|programming|language|studio))")
        elif alias == "go":
            rx = re.compile(r"(?<![\w-])Go(?![\w-])(?=\s*(?:,|/|\)|\(|lang\b|programming|language|microservices|services|and\b|or\b))")
        elif alias in ("rest", "spring", "node"):
            # common English words: only match the capitalised / acronym technical form
            form = {"rest": "REST", "spring": "Spring", "node": "Node"}[alias]
            rx = re.compile(r"(?<![\w.+#/-])" + re.escape(form) + r"(?![\w+#-])")
        else:
            rx = re.compile(r"(?<![\w.+#/-])" + re.escape(alias) + r"(?![\w+#-])", re.I)
        out.append((rx, canon))
    return out


@lru_cache(maxsize=1)
def _index():
    return _alias_index()


def register_custom_skills(entries) -> None:
    """Add skills the taxonomy lacks (config `custom_skills`): a list of names, or {name: [aliases]}."""
    items = entries.items() if isinstance(entries, dict) else ((e, []) for e in entries or [])
    changed = False
    for name, aliases in items:
        name = str(name).strip()
        if not name or len(name) < 2:
            continue
        existing = next((k for k in SKILLS if k.lower() == name.lower()), None)
        if existing is None:
            SKILLS[name] = [str(a).lower() for a in aliases or []]
            changed = True
        else:
            new = [str(a).lower() for a in aliases or [] if str(a).lower() not in SKILLS[existing]]
            if new:
                SKILLS[existing] = list(SKILLS[existing]) + new
                changed = True
    if changed:
        _index.cache_clear()


def extract_skills(text: str) -> set[str]:
    """Canonical skills explicitly mentioned in text."""
    found: set[str] = set()
    if not text:
        return found
    for rx, canon in _index():
        if rx.search(text):
            found.add(canon)
    return found


def implied_closure(skills: set[str]) -> set[str]:
    out = set(skills)
    frontier = list(skills)
    while frontier:
        s = frontier.pop()
        for t in IMPLIES.get(s, []):
            if t not in out:
                out.add(t)
                frontier.append(t)
    return out


def related(skill: str) -> set[str]:
    out: set[str] = set()
    for g in RELATED_GROUPS:
        if skill in g:
            out.update(x for x in g if x != skill)
    return out


def canonical(name: str) -> str:
    found = extract_skills(name)
    if len(found) == 1:
        return next(iter(found))
    for canon in SKILLS:
        if canon.lower() == name.strip().lower():
            return canon
    return name.strip()


def domains_in(text: str) -> set[str]:
    t = (text or "").lower()
    return {d for d, kws in DOMAINS.items() if any(re.search(r"(?<![\w-])" + re.escape(k) + r"(?![\w-])", t) for k in kws)}


def role_families(title: str) -> set[str]:
    t = (title or "").lower()
    fams = {f for f, kws in ROLE_FAMILIES.items() if any(k in t for k in kws)}
    if "software" in fams and fams & {"backend", "frontend", "fullstack", "data_eng", "devops", "qa"}:
        fams.discard("software")
    return fams
