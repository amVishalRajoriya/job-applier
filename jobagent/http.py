"""Polite HTTP client: timeouts, bounded retries, per-host pacing and robots.txt."""
from __future__ import annotations

import os
import ssl
import threading
import time
from typing import Any
from urllib import robotparser
from urllib.parse import urlparse

import httpx


class FetchError(Exception):
    pass


class RobotsDisallowed(FetchError):
    pass


class PoliteClient:
    def __init__(self, user_agent: str, timeout: float = 20.0, retries: int = 3,
                 min_interval: float = 1.0, respect_robots: bool = True):
        cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        verify: Any = ssl.create_default_context(cafile=cafile) if cafile else True
        self.client = httpx.Client(timeout=timeout, follow_redirects=True, verify=verify,
                                   headers={"User-Agent": user_agent, "Accept": "application/json, text/html;q=0.9"})
        self.user_agent = user_agent
        self.retries = retries
        self.min_interval = min_interval
        self.respect_robots = respect_robots
        self._last: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def _pace(self, host: str) -> None:
        with self._lock:
            wait = self._last.get(host, 0) + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def robots_allows(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.client.get(base + "/robots.txt")
                if r.status_code >= 400:
                    rp = None  # no robots.txt: allowed
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def request(self, method: str, url: str, *, check_robots: bool = False, **kw: Any) -> httpx.Response:
        if check_robots and self.respect_robots and not self.robots_allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        host = urlparse(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self._pace(host)
            try:
                r = self.client.request(method, url, **kw)
            except httpx.HTTPError as e:
                last_exc = e
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last_exc = FetchError(f"HTTP {r.status_code} from {url}")
                    retry_after = r.headers.get("retry-after", "")
                    if retry_after.isdigit():
                        time.sleep(min(int(retry_after), 30))
                elif r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code} from {url}")
                else:
                    return r
            if attempt < self.retries:
                time.sleep(min(0.25 * 2 ** attempt, 4))
        raise FetchError(f"giving up on {url}: {last_exc}")

    def get_json(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, **kw).json()

    def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        return self.request("POST", url, json=body, **kw).json()

    def get_text(self, url: str, **kw: Any) -> str:
        return self.request("GET", url, **kw).text

    def close(self) -> None:
        self.client.close()
