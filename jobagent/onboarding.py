"""First-run setup: turn a resume plus the candidate's own answers into a profile and a source list.

Used by `jobagent setup` (interactive or from a details file) and by the dashboard's /setup page.
Nothing here guesses a personal fact: a field the candidate leaves blank stays empty, and
applications that need it pause for the candidate.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml

from .profile import Profile, SkillEntry
from .skills import SKILLS, register_custom_skills

# The fields a resume does not contain. Same keys as the details file (jobagent/my_details_template.yaml).
QUESTIONS: list[dict[str, str]] = [
    {"key": "contact.name", "kind": "text", "prompt": "Full name as it should appear on applications"},
    {"key": "contact.email", "kind": "text", "prompt": "Email for applications"},
    {"key": "contact.phone", "kind": "text", "prompt": "Phone number with country code"},
    {"key": "contact.location", "kind": "text", "prompt": "Current city and country"},
    {"key": "contact.linkedin", "kind": "text", "prompt": "LinkedIn URL (optional)"},
    {"key": "contact.github", "kind": "text", "prompt": "GitHub URL (optional)"},
    {"key": "work_authorization.authorized", "kind": "country_text",
     "prompt": "Countries where you can work without a new visa, with your status (e.g. IN=citizen, AE=resident)"},
    {"key": "work_authorization.requires_sponsorship", "kind": "country_bool",
     "prompt": "Do you need visa sponsorship, per country you would apply in? (e.g. IN=no, US=yes, GB=yes)"},
    {"key": "salary.currency", "kind": "text", "prompt": "Salary currency (e.g. INR, USD)"},
    {"key": "salary.expected", "kind": "number", "prompt": "Expected salary per year, as a number (optional)"},
    {"key": "salary.minimum", "kind": "number", "prompt": "Minimum acceptable salary per year (optional, used only to skip jobs)"},
    {"key": "salary.answer_text", "kind": "text",
     "prompt": "Exact wording to use when a form asks your salary expectation (e.g. 'Negotiable, 35-40 LPA')"},
    {"key": "notice_period_days", "kind": "int", "prompt": "Notice period in days"},
    {"key": "availability_date", "kind": "text", "prompt": "Earliest start date YYYY-MM-DD (optional)"},
    {"key": "preferences.target_roles", "kind": "list", "prompt": "Target job titles, comma separated"},
    {"key": "preferences.adjacent_roles", "kind": "list", "prompt": "Adjacent titles you would also consider (optional)"},
    {"key": "preferences.locations", "kind": "list", "prompt": "Locations you would work in, comma separated (e.g. Bengaluru, Remote India)"},
    {"key": "preferences.remote", "kind": "list", "prompt": "Work arrangements you accept: remote, hybrid, onsite"},
    {"key": "preferences.willing_to_relocate", "kind": "bool", "prompt": "Willing to relocate? (yes/no)"},
    {"key": "preferences.employment_types", "kind": "list", "prompt": "Employment types (e.g. full-time, contract)"},
    {"key": "preferences.excluded_employers", "kind": "list", "prompt": "Employers to never apply to (optional)"},
    {"key": "preferences.excluded_keywords", "kind": "list", "prompt": "Skip jobs whose title mentions any of these (optional)"},
]

REMOTE = ("remote", "hybrid", "onsite")


# ---- value parsing ----------------------------------------------------------------------------

def _bool(v: Any) -> bool | None:
    if isinstance(v, bool) or v is None:
        return v
    t = str(v).strip().lower()
    if t in ("y", "yes", "true", "1"):
        return True
    if t in ("n", "no", "false", "0"):
        return False
    if t == "":
        return None
    raise ValueError(f"expected yes or no, got {v!r}")


def _country_map(v: Any, as_bool: bool) -> dict:
    if isinstance(v, dict):
        items = v.items()
    else:
        items = []
        for part in re.split(r"[,;\n]", str(v or "")):
            if part.strip():
                k, _, val = part.partition("=")
                if not val:
                    raise ValueError(f"write countries as CODE=value, e.g. IN=citizen (got {part.strip()!r})")
                items.append((k, val))
    out = {}
    for k, val in items:
        code = str(k).strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", code):
            raise ValueError(f"use two-letter country codes such as IN, US, GB (got {k!r})")
        if as_bool:
            b = _bool(val)
            if b is not None:
                out[code] = b
        elif str(val).strip():
            out[code] = str(val).strip()
    return out


def parse_value(kind: str, v: Any) -> Any:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if kind == "text":
        return str(v).strip()
    if kind == "int":
        return int(str(v).strip())
    if kind == "number":
        return float(str(v).replace(",", "").strip())
    if kind == "bool":
        return _bool(v)
    if kind == "list":
        items = v if isinstance(v, list) else re.split(r"[,;\n]", str(v))
        return [str(x).strip() for x in items if str(x).strip()]
    if kind == "country_text":
        return _country_map(v, False)
    if kind == "country_bool":
        return _country_map(v, True)
    raise ValueError(kind)


def _get(data: dict, dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = data
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


# ---- applying details to the profile ------------------------------------------------------------

def apply_details(profile: Profile, details: dict) -> Profile:
    """Merge the candidate's answers into the profile. Blank answers leave the profile unchanged."""
    data = profile.model_dump()
    for q in QUESTIONS:
        raw = _get(details, q["key"])
        val = parse_value(q["kind"], raw)
        if val is None or val == [] or val == {}:
            continue
        if q["key"] == "preferences.remote":
            bad = [x for x in val if x.lower() not in REMOTE]
            if bad:
                raise ValueError(f"preferences.remote accepts {REMOTE}, got {bad}")
            val = [x.lower() for x in val]
        _set(data, q["key"], val)
    for question, ans in (details.get("approved_answers") or {}).items():
        if str(ans).strip():
            data.setdefault("approved_answers", {})[str(question).strip()] = str(ans).strip()
    prof = Profile.model_validate(data)
    for name, level in (details.get("skills") or {}).items():
        if not level:
            continue
        entry = prof.skill_entry(name)
        if entry:
            entry.proficiency = level
        else:
            # the candidate is declaring a skill that is not on the resume; it stays off the master resume
            prof.skills.append(SkillEntry(name=name, proficiency=level, on_master_resume=False))
    return Profile.model_validate(prof.model_dump())


def merge_resume(old: Profile, parsed: Profile) -> Profile:
    """A newly parsed resume replaces the evidence; the candidate's personal answers and skill levels are kept."""
    data = parsed.model_dump()
    prev = old.model_dump()
    for k in ("work_authorization", "salary", "notice_period_days", "availability_date", "preferences",
              "approved_answers"):
        data[k] = prev[k]
    data["contact"] = {**{k: v for k, v in prev["contact"].items() if v},
                       **{k: v for k, v in data["contact"].items() if v}}
    levels = {x.name.lower(): x.proficiency for x in old.skills}
    for sk in data["skills"]:
        sk["proficiency"] = levels.get(sk["name"].lower(), sk["proficiency"])
    return Profile.model_validate(data)


def unanswered(profile: Profile) -> list[str]:
    """Personal fields still empty, as details-file keys, for the candidate to fill in."""
    data = profile.model_dump()
    out = []
    for q in QUESTIONS:
        if "optional" in q["prompt"]:
            continue
        v = _get(data, q["key"])
        if v in (None, "", [], {}):
            out.append(q["key"])
    if profile.salary.expected is None and profile.salary.answer_text:
        out = [k for k in out if k != "salary.expected"]
    if profile.salary.answer_text is None and profile.salary.expected is not None:
        out = [k for k in out if k != "salary.answer_text"]
    return out


def unknown_skills(profile: Profile) -> list[str]:
    """Profile skills the built-in taxonomy does not know; register them so job ads mentioning them match."""
    known = {k.lower() for k in SKILLS} | {a.lower() for v in SKILLS.values() for a in v}
    return sorted({s.name for s in profile.skills if s.name.lower() not in known})


# ---- boards ------------------------------------------------------------------------------------

def _employer_from_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def board_from_url(url: str, employer: str | None = None) -> tuple[dict | None, str | None]:
    """Map a careers URL to a source config. Returns (config, None) or (None, reason it is not usable)."""
    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    u = urlparse(url)
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    if host.endswith("linkedin.com") or host.endswith("indeed.com") or ".indeed." in host:
        return None, (f"{host}: LinkedIn and Indeed cannot be automated (their terms forbid it). Add jobs you find "
                      "there to a manual jobs file instead")
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io") and parts:
        token = parts[0]
        return {"type": "greenhouse", "board_token": token, "employer": employer or _employer_from_slug(token)}, None
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        return {"type": "lever", "site": parts[0], "employer": employer or _employer_from_slug(parts[0])}, None
    if host == "jobs.ashbyhq.com" and parts:
        return {"type": "ashby", "organization": parts[0], "employer": employer or _employer_from_slug(parts[0])}, None
    m = re.fullmatch(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", host)
    if m:
        tenant = m.group(1)
        site_parts = [p for p in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
        if not site_parts:
            return None, f"{url}: Workday URL needs the career site name after the host (e.g. /External)"
        return {"type": "workday", "host": host, "tenant": tenant, "site": site_parts[0],
                "employer": employer or _employer_from_slug(tenant)}, None
    if re.search(r"(\.rss|\.xml|/feed/?|\.json)$", u.path, re.I):
        return {"type": "feed", "url": url, "name": host, **({"employer": employer} if employer else {})}, None
    if host:
        return {"type": "careerpage", "urls": [url], **({"employer": employer} if employer else {})}, None
    return None, f"{url}: not a URL"


def boards_from_entries(entries: list[Any]) -> tuple[list[dict], list[str]]:
    """Entries are URLs, 'Employer | URL' strings, or {url, employer} dicts."""
    sources, problems = [], []
    for e in entries or []:
        if isinstance(e, dict):
            url, emp = str(e.get("url", "")), e.get("employer")
        else:
            text = str(e).strip()
            if not text or text.startswith("#"):
                continue
            emp, _, url = text.rpartition("|") if "|" in text else ("", "", text)
            emp = emp.strip() or None
        cfg, problem = board_from_url(url.strip(), emp)
        if cfg:
            if cfg not in sources:
                sources.append(cfg)
        else:
            problems.append(problem)
    return sources, problems


def source_to_entry(cfg: dict) -> list[str]:
    """Inverse of board_from_url, for showing configured sources as editable URL lines."""
    t, emp = cfg.get("type"), cfg.get("employer")
    urls = {"greenhouse": lambda: [f"https://boards.greenhouse.io/{cfg['board_token']}"],
            "lever": lambda: [f"https://jobs.lever.co/{cfg['site']}"],
            "ashby": lambda: [f"https://jobs.ashbyhq.com/{cfg['organization']}"],
            "workday": lambda: [f"https://{cfg['host']}/{cfg['site']}"],
            "careerpage": lambda: list(cfg.get("urls", [])),
            "feed": lambda: [cfg["url"]]}.get(t)
    if urls is None or cfg.get("base_url"):
        return []
    return [f"{emp} | {u}" if emp else u for u in urls()]


def form_rows(profile: Profile) -> list[dict]:
    data = profile.model_dump()
    return [{**q, "value": _show(_get(data, q["key"]))} for q in QUESTIONS]


def update_config(config_path: Path, sources: list[dict] | None = None, custom_skills: list[str] | None = None,
                  mode: str | None = None) -> dict:
    """Rewrite config.yaml with the new sources, custom skills and mode, keeping every other setting."""
    data = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    data = data or {}
    if sources is not None:
        data["sources"] = sources
    if custom_skills:
        data["custom_skills"] = sorted(set(data.get("custom_skills") or []) | set(custom_skills))
    if mode:
        if mode == "auto":
            raise ValueError("setup never turns on auto mode; grant consent with `jobagent consent grant` first")
        data["mode"] = mode
    config_path.write_text("# jobagent configuration (updated by `jobagent setup`). Secrets never go here.\n"
                           + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    if custom_skills:
        register_custom_skills(data["custom_skills"])
    return data


# ---- interactive wizard ------------------------------------------------------------------------

def _show(v: Any) -> str:
    if isinstance(v, dict):
        return ", ".join(f"{k}={'yes' if x is True else 'no' if x is False else x}" for k, x in v.items())
    if isinstance(v, list):
        return ", ".join(map(str, v))
    if isinstance(v, bool):
        return "yes" if v else "no"
    return "" if v is None else str(v)


def interview(profile: Profile, ask: Callable[[str], str] = input,
              say: Callable[[str], None] = print) -> tuple[dict, list[str]]:
    """Ask every personal question; Enter keeps the current value. Returns (details, board entries)."""
    details: dict = {}
    current = profile.model_dump()
    say("Press Enter to keep the value in [brackets]. Leave a question blank if it does not apply; "
        "nothing is ever guessed for you.")
    for q in QUESTIONS:
        cur = _show(_get(current, q["key"]))
        while True:
            raw = ask(f"{q['prompt']}" + (f" [{cur}]" if cur else "") + ": ").strip()
            if not raw:
                break
            try:
                parse_value(q["kind"], raw)
                _set(details, q["key"], raw)
                break
            except ValueError as e:
                say(f"  {e}")
    say("Skill levels: for each skill, type expert, advanced, intermediate, beginner or exposure (Enter keeps it).")
    for s in profile.skills:
        while True:
            raw = ask(f"  {s.name} [{s.proficiency}]: ").strip().lower()
            if not raw:
                break
            if raw in ("expert", "advanced", "intermediate", "beginner", "exposure"):
                details.setdefault("skills", {})[s.name] = raw
                break
            say("  use expert, advanced, intermediate, beginner or exposure")
    say("Company job boards to watch: paste one careers URL per line (Greenhouse, Lever, Ashby, Workday, "
        "a careers page or a feed). Optionally 'Employer | URL'. Blank line to finish.")
    boards = []
    while True:
        raw = ask("  board URL: ").strip()
        if not raw:
            break
        boards.append(raw)
    return details, boards


def details_template() -> str:
    return (Path(__file__).resolve().parent / "my_details_template.yaml").read_text()
