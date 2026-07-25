"""Cookie-login sidecar for the weather dashboard.

Streamlit has no auth of its own, so Caddy fronts it with forward_auth:
every request to weather.jakubwaller.eu is first sent here as
GET /auth/verify. A 2xx lets the request through to Streamlit; anything
else (here: a redirect to /auth/login) is returned to the browser
instead. The login page is a real HTML form — password managers can
autofill it — and sets a signed, HttpOnly cookie valid for 30 days.
Same pattern as elternschule-bot's web login.

Env: WEB_PASSWORD (the shared password), COOKIE_SECRET (signing key,
>=16 chars). Set COOKIE_INSECURE=1 for plain-http local dev.
"""

from __future__ import annotations

import html
import os
import secrets
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "wa_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_COOKIE_SALT = "wa-session-v1"

app = FastAPI(title="Weather dashboard login")


def _password() -> str:
    pw = os.environ.get("WEB_PASSWORD")
    if not pw:
        raise RuntimeError("WEB_PASSWORD env var must be set")
    return pw


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("COOKIE_SECRET")
    if not secret or len(secret) < 16:
        raise RuntimeError(
            "COOKIE_SECRET env var must be set to a string of at least 16 chars "
            "(generate one with `python -c 'import secrets;print(secrets.token_urlsafe(32))'`)"
        )
    return URLSafeTimedSerializer(secret, salt=_COOKIE_SALT)


def _is_authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        _serializer().loads(token, max_age=COOKIE_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _cookie_secure(request: Request) -> bool:
    if os.environ.get("COOKIE_INSECURE") == "1":
        return False
    return request.url.scheme == "https" or request.headers.get(
        "x-forwarded-proto", ""
    ).lower() == "https"


def _safe_next(target: str | None) -> str:
    # Only same-app absolute paths; rejects "//evil" and full URLs.
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


def _login_page(next_url: str, error: str | None = None,
                status_code: int = 200) -> HTMLResponse:
    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>Weather dashboard – login</title>
  <style>
    :root {{ color-scheme: light dark; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 24rem;
           margin: 4rem auto; padding: 0 1rem; line-height: 1.5; }}
    h1 {{ font-size: 1.3rem; margin: 0 0 1rem; }}
    form {{ display: grid; gap: 0.85rem; }}
    label {{ display: grid; gap: 0.3rem; font-size: 0.9rem; }}
    input {{ padding: 0.7rem 0.75rem; font-size: 16px; border: 1px solid #999;
            border-radius: 6px; background: inherit; color: inherit;
            width: 100%; min-height: 44px; }}
    button {{ padding: 0.9rem; font-size: 1rem; background: #111; color: white;
             border: 0; border-radius: 6px; cursor: pointer; min-height: 48px;
             font-weight: 600; }}
    .err {{ background: #fee; border: 1px solid #c00; padding: 0.75rem;
           border-radius: 6px; color: #900; margin-bottom: 1rem; }}
    .muted {{ color: #888; font-size: 0.85rem; margin-top: 1.25rem; }}
  </style>
</head>
<body>
  <h1>🌤️ Weather dashboard</h1>
  {err}
  <form method="post" action="/auth/login">
    <input type="hidden" name="next" value="{html.escape(next_url)}">
    <label>Password
      <input name="password" type="password" required autofocus
             autocomplete="current-password">
    </label>
    <button type="submit">Log in</button>
  </form>
  <p class="muted">You stay logged in for 30 days on this device.</p>
</body>
</html>"""
    return HTMLResponse(body, status_code=status_code)


@app.get("/auth/verify")
async def verify(request: Request) -> Response:
    if _is_authed(request):
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    # Caddy passes the original request path in X-Forwarded-Uri; send the
    # browser to the login form and back to that page afterwards.
    next_url = _safe_next(request.headers.get("x-forwarded-uri"))
    return RedirectResponse(
        f"/auth/login?next={quote(next_url, safe='')}",
        status_code=status.HTTP_302_FOUND,
    )


@app.get("/auth/login", response_model=None)
async def login_form(request: Request) -> HTMLResponse | RedirectResponse:
    next_url = _safe_next(request.query_params.get("next"))
    if _is_authed(request):
        return RedirectResponse(next_url, status_code=status.HTTP_303_SEE_OTHER)
    return _login_page(next_url)


@app.post("/auth/login", response_model=None)
async def login_submit(
    request: Request,
    password: str = Form(...),
    next_url: str = Form("/", alias="next"),
) -> RedirectResponse | HTMLResponse:
    next_url = _safe_next(next_url)
    if not secrets.compare_digest(password, _password()):
        return _login_page(next_url, error="Wrong password.", status_code=401)
    response = RedirectResponse(next_url, status_code=status.HTTP_303_SEE_OTHER)
    token = _serializer().dumps("authed")
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/",
    )
    return response


@app.post("/auth/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
