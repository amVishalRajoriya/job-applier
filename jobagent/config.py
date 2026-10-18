"""Runtime settings: environment variables first, then an optional config.yaml.

Secrets (API keys, encryption key, dashboard token) only ever come from the
environment. Everything else (weights, thresholds, sources) may live in
config.yaml so it can be reviewed and versioned.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_WEIGHTS = {
    "required_skills": 25.0,
    "responsibilities": 25.0,
    "experience": 20.0,
    "education": 10.0,
    "domain": 10.0,
    "preferred": 10.0,
}

MODES = ("discovery", "review", "auto")


@dataclass
class Settings:
    home: Path
    mode: str = "discovery"
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    optimize_below: float = 50.0
    target_min: float = 70.0
    target_max: float = 80.0
    max_job_age_days: int = 60
    relevance_threshold: float = 0.25
    sources: list[dict[str, Any]] = field(default_factory=list)
    extra_allowed_domains: list[str] = field(default_factory=list)
    http_timeout: float = 20.0
    http_retries: int = 3
    per_host_min_interval: float = 1.0
    user_agent: str = "jobagent/0.1 (personal job search assistant; respects robots.txt)"
    browser_headless: bool = True
    browser_timeout_ms: int = 15000
    max_apply_attempts: int = 3
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"
    llm_enabled: bool = False

    @property
    def db_path(self) -> Path:
        return self.home / "jobagent.db"

    @property
    def documents_dir(self) -> Path:
        return self.home / "documents"

    @property
    def browser_dir(self) -> Path:
        return self.home / "browser"

    def ensure_dirs(self) -> None:
        for d in (self.home, self.documents_dir, self.browser_dir):
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass


def load_settings(home: str | Path | None = None, config_file: str | Path | None = None) -> Settings:
    home = Path(home or os.environ.get("JOBAGENT_HOME", "var")).resolve()
    s = Settings(home=home)
    cfg_path = Path(config_file or os.environ.get("JOBAGENT_CONFIG", home / "config.yaml"))
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text()) or {}
        for key, value in data.items():
            if key == "weights":
                s.weights.update({k: float(v) for k, v in value.items()})
            elif hasattr(s, key) and key != "home":
                setattr(s, key, value)
    mode = os.environ.get("JOBAGENT_MODE")
    if mode:
        s.mode = mode
    if s.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {s.mode!r}")
    if os.environ.get("JOBAGENT_HEADFUL") == "1":
        s.browser_headless = False
    s.anthropic_model = os.environ.get("JOBAGENT_ANTHROPIC_MODEL", s.anthropic_model)
    s.llm_enabled = bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("JOBAGENT_LLM", "1") != "0"
    s.ensure_dirs()
    return s
