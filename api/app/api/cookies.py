"""Session and CSRF cookies.

- Session cookie: HttpOnly, SameSite=Lax, Path=/. Behind HTTPS it is Secure
  and uses the ``__Host-`` prefix (no Domain, cannot be set by subdomains).
  It only holds a random token; the server-side row decides validity, so
  logout/revoke/expiry take effect even if the browser keeps the cookie.
- CSRF cookie ``ras_csrf``: readable by the web page, echoed back in the
  ``X-CSRF-Token`` header on every state-changing request.
"""

from __future__ import annotations

from fastapi import Response

from app.config import Settings

CSRF_COOKIE = "ras_csrf"
CSRF_HEADER = "X-CSRF-Token"


def session_cookie_name(settings: Settings) -> str:
    return "__Host-ras_session" if settings.http.cookie_secure else "ras_session"


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        session_cookie_name(settings),
        token,
        max_age=int(settings.auth.session_absolute.total_seconds()),
        path="/",
        secure=settings.http.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def set_csrf_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        token,
        path="/",
        secure=settings.http.cookie_secure,
        httponly=False,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        session_cookie_name(settings),
        path="/",
        secure=settings.http.cookie_secure,
        httponly=True,
        samesite="lax",
    )
