"""Encryption at rest, domain allowlisting and untrusted-content handling."""
from __future__ import annotations

import html
import os
import re
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken


class SecurityError(Exception):
    pass


# ---- encryption ------------------------------------------------------------

def _fernet() -> Fernet | None:
    key = os.environ.get("JOBAGENT_ENCRYPTION_KEY")
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise SecurityError("JOBAGENT_ENCRYPTION_KEY is not a valid Fernet key; "
                            "generate one with `python -m jobagent.cli keygen`") from e


def generate_key() -> str:
    return Fernet.generate_key().decode()


def encrypt(data: bytes) -> tuple[bytes, bool]:
    f = _fernet()
    if f is None:
        return data, False
    return f.encrypt(data), True


def decrypt(blob: bytes, encrypted: bool) -> bytes:
    if not encrypted:
        return blob
    f = _fernet()
    if f is None:
        raise SecurityError("profile is encrypted but JOBAGENT_ENCRYPTION_KEY is not set")
    try:
        return f.decrypt(blob)
    except InvalidToken as e:
        raise SecurityError("cannot decrypt profile: wrong JOBAGENT_ENCRYPTION_KEY") from e


# ---- domains ----------------------------------------------------------------

def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def domain_allowed(url: str, allowed: set[str] | list[str]) -> bool:
    """True if the URL's host equals or is a subdomain of an allowed domain."""
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    for d in allowed:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def require_allowed(url: str, allowed: set[str] | list[str]) -> None:
    if not domain_allowed(url, allowed):
        raise SecurityError(f"navigation to {host_of(url) or url!r} is not in the approved domain list")


# ---- untrusted text -----------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")

INJECTION_PATTERNS = [
    r"ignore (all|any|the)? ?(previous|prior|above) (instructions|prompts?)",
    r"disregard (all|any|the)? ?(previous|prior|above)",
    r"you are (now )?(an? )?(ai|assistant|language model|chatgpt|claude)",
    r"system prompt",
    r"(as an? )?ai (agent|assistant)[, ]+(you must|please|always)",
    r"(must|should) (rate|score) (this|the) (candidate|applicant|resume) (as )?(100|perfect|highly)",
    r"add (the following|these) (skills|keywords) to (your|the) resume",
    r"submit (your|the) (password|credentials|ssn|social security|bank)",
]
_INJ = [re.compile(p, re.I) for p in INJECTION_PATTERNS]


def html_to_text(raw: str | None) -> str:
    """Convert (possibly entity-escaped) HTML into plain text, keeping list structure."""
    if not raw:
        return ""
    text = html.unescape(raw)
    if "&lt;" in raw and "<" not in raw:
        text = html.unescape(text)
    text = re.sub(r"(?i)<\s*(br|/p|/div|/h[1-6]|/ul|/ol)\s*/?>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n- ", text)
    text = re.sub(r"(?i)<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", " ", text, flags=re.S)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    return clean_text(text)


def clean_text(text: str) -> str:
    text = _CTRL.sub("", text.replace(" ", " "))
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def detect_injection(text: str) -> list[str]:
    """Return the suspicious phrases found in untrusted text (job descriptions, web pages)."""
    hits = []
    for rx in _INJ:
        m = rx.search(text or "")
        if m:
            hits.append(m.group(0))
    return hits


def fence_untrusted(label: str, text: str) -> str:
    """Wrap untrusted content for a model prompt so it is read as data, not instructions."""
    safe = (text or "").replace("</untrusted", "&lt;/untrusted")
    return f'<untrusted source="{label}">\n{safe}\n</untrusted>'
