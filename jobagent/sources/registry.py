"""Build configured sources and run discovery with per-source error isolation."""
from __future__ import annotations

from typing import Any

from ..http import PoliteClient
from .ats import AshbySource, GreenhouseSource, LeverSource, WorkdaySource
from .base import CAPABILITIES, JobRecord, Source
from .jobsites import JobAlertSource, SavedPagesSource
from .web import CareerPageSource, FeedSource, ManualSource

SOURCE_TYPES: dict[str, type[Source]] = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "workday": WorkdaySource,
    "careerpage": CareerPageSource,
    "feed": FeedSource,
    "manual": ManualSource,
    "job_alerts": JobAlertSource,
    "saved_pages": SavedPagesSource,
}


def build_sources(configs: list[dict[str, Any]], http: PoliteClient) -> list[Source]:
    out = []
    for cfg in configs:
        cfg = dict(cfg)
        kind = cfg.pop("type")
        if kind in ("linkedin", "indeed", "naukri"):
            raise ValueError(f"{kind} is not fetched directly: {CAPABILITIES[kind].discovery}. Add a `job_alerts` "
                             "source (your alert emails) or a `saved_pages` source (pages you saved) instead.")
        if kind not in SOURCE_TYPES:
            raise ValueError(f"unknown source type {kind!r}; known: {sorted(SOURCE_TYPES)}")
        out.append(SOURCE_TYPES[kind](http, **cfg))
    return out


def discover(sources: list[Source]) -> tuple[list[JobRecord], list[dict[str, str]]]:
    jobs: list[JobRecord] = []
    errors: list[dict[str, str]] = []
    for s in sources:
        try:
            jobs.extend(s.fetch())
        except Exception as e:  # one broken source must not stop the others
            errors.append({"source": s.name, "error": f"{type(e).__name__}: {e}"})
    return jobs, errors
