"""The connect pages: rendering, and the headers every response carries (D4).

- ``Referrer-Policy: no-referrer``: the page URL holds the link token, and the next hops are
  Composio and Google.
- ``Cache-Control: no-store``: on a shared phone, the back button must not bring back a page
  holding a live token.
- A CSP of ``default-src 'none'``, since the page loads nothing. Allowed back in: its one inline
  ``<style>``, by hash; ``form-action 'self' https:``, because Chrome checks ``form-action`` on
  every redirect of a form submission and ours redirects to Composio and on to Google; and
  nothing else. ``frame-ancestors 'none'`` and ``X-Frame-Options`` stop anyone framing the
  button.

Templates are autoescaped with ``StrictUndefined``. Copy is in
:mod:`personal_organizer.onboarding.page_text`.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Final

from fastapi import Response
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.status import HTTP_200_OK, HTTP_303_SEE_OTHER

from personal_organizer.onboarding.page_text import (
    BILINGUAL,
    page_language,
    page_text,
    text_direction,
)

#: The whole stylesheet. It has no quotes, angle brackets or ampersands, so autoescaping leaves
#: it byte-identical and the hash below is the hash of what the browser sees.
PAGE_CSS: Final = (
    ":root{color-scheme:light dark;--paper:#eef2ef;--ink:#16302a;--muted:#4f625d;"
    "--accent:#0b6e5f;--on-accent:#fff;--stub:#fff;--rule:#9db3ac}"
    "@media (prefers-color-scheme:dark){:root{--paper:#0f1f1b;--ink:#e4ede9;--muted:#a3b6b0;"
    "--accent:#46c4aa;--on-accent:#06201a;--stub:#162b26;--rule:#3d5a52}}"
    "*{box-sizing:border-box}"
    "body{margin:0;background:var(--paper);color:var(--ink);"
    "font:1.0625rem/1.55 system-ui,-apple-system,sans-serif}"
    "main{max-width:26rem;margin:0 auto;padding:3rem 1.25rem 2rem}"
    "h1{font-size:1.75rem;line-height:1.2;letter-spacing:-.01em;margin:0 0 .75rem}"
    "p{margin:0 0 1rem}"
    ".who{margin:2rem 0;padding:1rem 1.25rem;background:var(--stub);"
    "border:2px dashed var(--rule);border-radius:.75rem;color:var(--muted);font-size:.95rem}"
    ".who b{display:block;color:var(--ink);font-size:3rem;line-height:1.1;font-weight:800;"
    "letter-spacing:.12em;font-variant-numeric:tabular-nums}"
    "form{margin-top:2rem}"
    "button{display:block;width:100%;padding:1rem 1.25rem;border:0;border-radius:.75rem;"
    "background:var(--accent);color:var(--on-accent);font:inherit;font-weight:700;"
    "font-size:1.125rem;cursor:pointer}"
    "button:active{filter:brightness(.9)}"
    "button:focus-visible{outline:3px solid var(--accent);outline-offset:3px}"
    ".note{margin-top:1rem;font-size:.9rem;color:var(--muted)}"
    "section+section{margin-top:2.5rem;border-top:1px solid var(--rule);padding-top:2rem}"
)


def _sha256(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


CONTENT_SECURITY_POLICY: Final = "; ".join(
    (
        "default-src 'none'",
        f"style-src 'sha256-{_sha256(PAGE_CSS)}'",
        "form-action 'self' https:",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
}

_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _html(template: str, status_code: int, **context: object) -> HTMLResponse:
    rendered = _templates.get_template(template).render(css=PAGE_CSS, **context)
    return HTMLResponse(rendered, status_code=status_code, headers=SECURITY_HEADERS)


def connect_page(*, token: str, language: str, phone_suffix: str | None) -> HTMLResponse:
    """The one-button page. ``phone_suffix`` names the number the link was issued to."""
    lang = page_language(language)
    text = page_text("connect", lang)
    return _html(
        "connect.html",
        HTTP_200_OK,
        lang=lang,
        direction=text_direction(lang),
        title=text["title"],
        text=text,
        token=token,
        phone_suffix=phone_suffix,
    )


def message_page(key: str, *, status_code: int, language: str | None = None) -> HTMLResponse:
    """A title and a line. With no ``language``, both languages, Hebrew first."""
    languages: tuple[str, ...] = BILINGUAL if language is None else (page_language(language),)
    sections = [
        {"lang": lang, "direction": text_direction(lang), **page_text(key, lang)}
        for lang in languages
    ]
    first = sections[0]
    return _html(
        "message.html",
        status_code,
        lang=first["lang"],
        direction=first["direction"],
        title=first["title"],
        sections=sections,
    )


def redirect(url: str) -> Response:
    """303 to ``url``; the policy headers on the redirect keep the token off the next hop."""
    return Response(status_code=HTTP_303_SEE_OTHER, headers={**SECURITY_HEADERS, "Location": url})


__all__ = [
    "CONTENT_SECURITY_POLICY",
    "PAGE_CSS",
    "SECURITY_HEADERS",
    "connect_page",
    "message_page",
    "redirect",
]
