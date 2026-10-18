"""Browser adapters for application forms.

The generic form adapter reads label-based HTML forms (the pattern used by
hosted ATS forms and the local test ATS). It never solves CAPTCHAs, never
logs in, and stops on anything it does not understand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from playwright.sync_api import Error as PWError, Page, TimeoutError as PWTimeout

from ..normalize import norm_employer, norm_title
from ..security import domain_allowed, host_of
from ..text import overlap


class ApplyError(Exception):
    pass


class TransientError(ApplyError):
    """Worth retrying: timeouts, 5xx, 'session expired, try again'."""


class NeedsHuman(ApplyError):
    """Stop and ask the candidate: CAPTCHA, login/MFA, unexpected question, consent, unsupported portal."""

    def __init__(self, reason: str, questions: list[dict] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.questions = questions or []


class FatalError(ApplyError):
    """Do not retry: job closed, wrong employer/role, navigation outside approved domains."""

    def __init__(self, reason: str, closed: bool = False):
        super().__init__(reason)
        self.closed = closed


@dataclass
class FormField:
    selector: str
    label: str
    type: str
    required: bool
    options: list[str] = field(default_factory=list)
    name: str = ""


FIELDS_JS = r"""
() => {
  const form = document.querySelector('form#application-form') || document.querySelector('form');
  if (!form) return [];
  const out = []; const seenRadio = new Set();
  const labelFor = (el) => {
    let t = '';
    if (el.id) { const l = form.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = l.innerText; }
    if (!t && el.closest('label')) t = el.closest('label').innerText;
    if (!t) t = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || '';
    return t.trim();
  };
  for (const el of form.querySelectorAll('input, select, textarea')) {
    let type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden','submit','button','image','reset'].includes(type)) continue;
    if (el.tagName === 'SELECT') type = 'select';
    if (el.tagName === 'TEXTAREA') type = 'textarea';
    let label = labelFor(el);
    let options = [];
    let selector = el.id ? '#' + CSS.escape(el.id) : `[name="${el.name}"]`;
    if (type === 'radio') {
      if (seenRadio.has(el.name)) continue;
      seenRadio.add(el.name);
      const group = form.querySelectorAll(`input[type=radio][name="${el.name}"]`);
      options = [...group].map(r => labelFor(r));
      const fs = el.closest('fieldset'); const legend = fs && fs.querySelector('legend');
      label = legend ? legend.innerText.trim() : el.name;
      selector = `[name="${el.name}"]`;
    }
    if (type === 'select') options = [...el.options].filter(o => o.value !== '').map(o => o.text.trim());
    const required = el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label);
    out.push({selector, label: label.replace(/\s*\*\s*$/, ''), type, required, options, name: el.name || ''});
  }
  return out;
}
"""

CAPTCHA_SELECTORS = ["iframe[src*='recaptcha']", "iframe[title*='captcha' i]", ".g-recaptcha", ".h-captcha",
                     "[data-sitekey]", ".cf-turnstile", "iframe[src*='hcaptcha']", "iframe[src*='challenges.cloudflare']"]
CLOSED = re.compile(r"no longer accepting applications|position has been filled|job (posting )?(is )?closed|"
                    r"no longer available|this job has expired|job not found", re.I)
LOGIN = re.compile(r"sign in to (continue|apply)|create an account to apply|enter (the )?verification code|"
                   r"one[- ]time (pass)?code|two[- ]factor", re.I)
SUCCESS = re.compile(r"application (submitted|received|complete)|thank you for (applying|your application)|"
                     r"we('ve| have) received your application", re.I)
REFERENCE = re.compile(r"(?:reference|confirmation (?:number|no\.?|#|id)|application (?:id|number))\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})", re.I)
DUPLICATE = re.compile(r"already (applied|submitted)|previously applied|duplicate application", re.I)
RETRYABLE = re.compile(r"session (has )?expired|please try again|temporar(il)?y unavailable|something went wrong", re.I)


class GenericFormAdapter:
    name = "generic_form"

    def __init__(self, page: Page, allowed_domains: list[str], timeout_ms: int = 15000):
        self.page = page
        self.allowed = allowed_domains
        self.timeout = timeout_ms
        self.violations: list[str] = []
        self.page.set_default_timeout(timeout_ms)
        page.on("request", self._watch)
        page.route("**/*", self._guard)

    def _watch(self, request) -> None:
        # redirects are not routed, so record every main-frame navigation target as well
        if request.is_navigation_request() and request.frame == self.page.main_frame \
                and not domain_allowed(request.url, self.allowed):
            self.violations.append(request.url)

    def _guard(self, route, request) -> None:
        if request.is_navigation_request() and request.frame == self.page.main_frame \
                and not domain_allowed(request.url, self.allowed):
            route.abort("blockedbyclient")
        else:
            route.continue_()

    def _check_domain(self) -> None:
        if self.violations:
            raise FatalError(f"blocked navigation to unapproved domain {host_of(self.violations[0])!r}")
        if not domain_allowed(self.page.url, self.allowed):
            raise FatalError(f"page navigated to unapproved domain {host_of(self.page.url)!r}")

    def open(self, url: str) -> None:
        if not domain_allowed(url, self.allowed):
            raise FatalError(f"application URL host {host_of(url)!r} is not an approved domain")
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded")
        except PWTimeout as e:
            raise TransientError(f"timeout opening {url}") from e
        except PWError as e:
            self._check_domain()
            if "ERR_BLOCKED" in str(e):
                raise FatalError(f"navigation blocked: {e}") from e
            raise TransientError(f"navigation error: {str(e).splitlines()[0]}") from e
        self._check_domain()
        status = resp.status if resp else 0
        if status >= 500 or status == 429:
            raise TransientError(f"HTTP {status} from application page")
        if status in (404, 410):
            raise FatalError(f"application page returned HTTP {status}", closed=True)

    def body_text(self) -> str:
        try:
            return self.page.inner_text("body")
        except PWError:
            return ""

    def check_open(self) -> None:
        text = self.body_text()
        if CLOSED.search(text):
            raise FatalError("the job is no longer accepting applications", closed=True)
        if self.page.locator("form").count() == 0:
            raise NeedsHuman("no application form found on the page (unsupported portal)")

    def verify_identity(self, employer: str, title: str) -> None:
        text = self.body_text()
        emp_ok = bool(norm_employer(employer)) and f" {norm_employer(employer)} " in f" {norm_employer(text)} "
        title_ok = overlap(norm_title(title), norm_title(text[:2000])) >= 0.6
        if not (emp_ok and title_ok):
            raise FatalError(f"page does not match {title!r} at {employer!r} (employer ok={bool(emp_ok)}, title ok={title_ok})")

    def detect_blockers(self) -> list[str]:
        found = []
        for sel in CAPTCHA_SELECTORS:
            if self.page.locator(sel).count():
                found.append("CAPTCHA present")
                break
        if self.page.locator("input[type=password]").count() or LOGIN.search(self.body_text()):
            found.append("login, account creation or MFA required")
        return found

    def fields(self) -> list[FormField]:
        return [FormField(**f) for f in self.page.evaluate(FIELDS_JS)]

    def fill(self, f: FormField, value: str) -> None:
        loc = self.page.locator(f.selector).first
        if f.type == "select":
            loc.select_option(label=value)
        elif f.type == "checkbox":
            if value.lower() in ("yes", "true", "1", "checked"):
                loc.check()
        elif f.type == "radio":
            self.page.locator(f.selector).nth(f.options.index(value)).check()
        else:
            loc.fill(value)

    def upload(self, f: FormField, path: str) -> None:
        self.page.locator(f.selector).first.set_input_files(path)

    def invalid_required(self) -> list[str]:
        return self.page.evaluate("""() => {
          const form = document.querySelector('form#application-form') || document.querySelector('form');
          return [...form.querySelectorAll('input,select,textarea')].filter(e => !e.checkValidity())
                 .map(e => e.id || e.name);
        }""")

    def submit(self) -> None:
        btn = self.page.locator("button[type=submit], input[type=submit]").first
        try:
            with self.page.expect_navigation(wait_until="domcontentloaded"):
                btn.click()
        except PWTimeout as e:
            raise TimeoutError("no response after clicking submit") from e
        self._check_domain()

    def result(self) -> dict:
        text = self.body_text()
        ref = REFERENCE.search(text)
        if DUPLICATE.search(text):
            kind = "duplicate"
        elif SUCCESS.search(text):
            kind = "success"
        elif RETRYABLE.search(text):
            kind = "retryable_error"
        elif self.page.locator(".error, [role=alert], .field-error").count():
            kind = "form_error"
        else:
            kind = "unknown"
        return {"kind": kind, "reference": ref.group(1) if ref else None, "url": self.page.url, "text": text[:2000]}


ADAPTERS = {"generic_form": GenericFormAdapter}
