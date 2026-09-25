"""Guardrails: where the agent may go, and what must never reach disk. (Which steps are risky is the AI's call.)"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def domain_allowed(url: str, allowed: list[str]) -> bool:
    parts = urlsplit(url)
    if parts.scheme in ("about", "data", "blob", "chrome-error"):
        return True
    host = (parts.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


def default_domain(url: str) -> str:
    """Default allowlist: the start URL's site and its subdomains (www.shop.com -> shop.com, so accounts.shop.com works).
    Other sites (e.g. an SSO provider) must be allowed explicitly with --allow."""
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


# Redaction: applied to every string written to logs, results and intervention requests.
_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "•••-••-••••"),  # SSN
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "•••@•••"),  # email
    (re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"), "•••-•••-••••"),  # US phone
    (re.compile(r"(?<![\w.$,])\d{5,19}(?![\d,]*\.\d)(?!\w)"), lambda m: "•" * (len(m[0]) - 4) + m[0][-4:]),  # account/card numbers -> last 4
]


def redact(text: str, secrets: list[str] = ()) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[secret]")
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


def redact_obj(obj, secrets: list[str] = ()):
    if isinstance(obj, str):
        return redact(obj, secrets)
    if isinstance(obj, dict):
        return {k: v if k in _STRUCTURAL else redact_obj(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, secrets) for v in obj]
    return obj


_STRUCTURAL = {"ts", "run_id", "run_dir", "screenshot", "a11y_snapshot", "step", "failed_step", "capability", "version"}
