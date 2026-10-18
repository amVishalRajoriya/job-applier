"""Build configured sources and run discovery with per-source error isolation."""
from __future__ import annotations

from typing import Any

from ..http import PoliteClient
from .ats import AshbySource, GreenhouseSource, LeverSource, WorkdaySource
from .base import CAPABILITIES, JobRecord, Source
from .web import CareerPageSource, FeedSource, ManualSource

SOURCE_TYPES: dict[str, type[Source]] = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "workday": WorkdaySource,
    "careerpage": CareerPageSource,
    "feed": FeedSource,
    "manual": ManualSource,
}


def build_sources(configs: list[dict[str, Any]], http: PoliteClient) -> list[Source]:
    out = []
    for cfg in configs:
        cfg = dict(cfg)
        kind = cfg.pop("type")
        if kind in ("linkedin", "indeed"):
            raise ValueError(f"{kind} cannot be automated: {CAPABILITIES[kind].discovery}. "
                             "Use a `manual` source file for jobs you found there.")
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
