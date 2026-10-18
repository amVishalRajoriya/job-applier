"""Run the local test ATS in a background thread (tests) or the foreground (`python -m testportal.server`)."""
from __future__ import annotations

import socket
import sys
import threading
import time

import httpx
import uvicorn

from .app import app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PortalServer:
    def __init__(self, port: int | None = None):
        self.port = port or free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "PortalServer":
        self.thread.start()
        for _ in range(100):
            try:
                httpx.get(self.base_url + "/robots.txt", timeout=0.5, trust_env=False)
                return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("test portal did not start")

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    uvicorn.run(app, host="127.0.0.1", port=port)
