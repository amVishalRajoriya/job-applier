"""Shared fixtures. All data here is SYNTHETIC; the only web server used is the local test ATS."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBAGENT_LLM", "0")  # tests are deterministic; LLM paths use a fake client
os.environ.pop("ANTHROPIC_API_KEY", None)

from jobagent import pipeline  # noqa: E402
from jobagent import profile as P  # noqa: E402
from jobagent.config import load_settings  # noqa: E402
from jobagent.db import Database  # noqa: E402
from testportal.server import PortalServer  # noqa: E402

PROFILE_YAML = ROOT / "data" / "sample" / "profile_SYNTHETIC.yaml"


@pytest.fixture(scope="session")
def portal():
    with PortalServer() as srv:
        yield srv


@pytest.fixture
def reset_portal(portal):
    httpx.post(portal.base_url + "/_admin/reset")
    return portal


@pytest.fixture(scope="session")
def sample_files(tmp_path_factory):
    out = tmp_path_factory.mktemp("samples")
    sys.path.insert(0, str(ROOT / "data" / "sample"))
    import build_samples
    return {"pdf": build_samples.build_pdf(out), "docx": build_samples.build_docx(out),
            "md": ROOT / "data" / "sample" / "master_resume_SYNTHETIC.md"}


@pytest.fixture
def profile():
    return P.load_yaml(str(PROFILE_YAML))


@pytest.fixture
def env(tmp_path, monkeypatch, profile):
    from jobagent.security import generate_key
    monkeypatch.setenv("JOBAGENT_ENCRYPTION_KEY", generate_key())
    s = load_settings(tmp_path / "home")
    s.per_host_min_interval = 0
    s.browser_timeout_ms = 8000
    s.http_retries = 1
    db = Database(s.db_path)
    P.save(db, profile)
    return s, db, profile


def sources(base: str, board: str = "all") -> list[dict]:
    return [
        {"type": "greenhouse", "board_token": board, "employer": "unused", "base_url": base + "/gh", "name": "gh"},
        {"type": "lever", "site": "shopwell", "employer": "Shopwell (synthetic)", "base_url": base + "/lever"},
        {"type": "ashby", "organization": "visionary", "employer": "Visionary (synthetic)", "base_url": base + "/ashby"},
        {"type": "workday", "host": "paymint.example", "tenant": "paymint", "site": "External",
         "employer": "Paymint (synthetic)", "base_url": base + "/wd"},
        {"type": "careerpage", "urls": [base + "/careers/paymint"]},
        {"type": "feed", "url": base + "/feed.xml", "name": "rss"},
    ]


def job_id_by_employer(db: Database, employer_prefix: str) -> int:
    row = db.one("SELECT id FROM jobs WHERE employer LIKE ?", (employer_prefix + "%",))
    assert row, f"no job for {employer_prefix}"
    return row["id"]


@pytest.fixture
def prepared(env, reset_portal):
    """Discover + process a chosen set of test jobs. Usage: prepared('1001,1007')."""
    s, db, prof = env

    def run(board: str, extra_sources: bool = False):
        cfg = sources(reset_portal.base_url, board) if extra_sources else sources(reset_portal.base_url, board)[:1]
        pipeline.run_discovery(db, s, cfg)
        pipeline.screen_and_score(db, s, prof)
        return s, db, prof

    return run
