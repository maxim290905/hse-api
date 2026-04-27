import base64
import hashlib
import os
import re
import secrets
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from . import config
from .exceptions import AuthError, NetworkError

# Keep excerpts short (300 chars) so auth errors stay readable in logs and avoid large payload leakage.
MAX_ERROR_TEXT_LENGTH = 300
HTML_TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)
SUPPORT_ID_RE = re.compile(r"support\s*id:\s*([A-Za-z0-9\-]+)", re.IGNORECASE)


class _LoginFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None

    def handle_starttag(self, tag, attrs):
        if self.action or tag.lower() != "form":
            return
        attrs_dict = dict(attrs)
        if attrs_dict.get("id") == "kc-form-login" and attrs_dict.get("action"):
            self.action = attrs_dict["action"]


def _truncate(text: str) -> str:
    if len(text) > MAX_ERROR_TEXT_LENGTH:
        return f"{text[:MAX_ERROR_TEXT_LENGTH]}..."
    return text


def _compact_text(text: str) -> str:
    compact = re.sub(r"<[^>]+>", " ", text)
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact


def _format_oidc_error(response: requests.Response, default_message: str) -> str:
    parts = [f"{default_message} (HTTP {response.status_code})"]
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        error = payload.get("error")
        description = payload.get("error_description")
        if error:
            parts.append(f"error={error}")
        if description:
            parts.append(f"description={description}")
    elif response.text:
        raw = response.text
        compact = _compact_text(raw)
        raw_lstripped = raw.lstrip().lower()
        is_html = (
            "html" in response.headers.get("Content-Type", "").lower()
            or raw_lstripped.startswith("<!doctype html")
            or raw_lstripped.startswith("<html")
        )

        if is_html:
            title_match = HTML_TITLE_RE.search(raw)
            support_match = SUPPORT_ID_RE.search(raw) or SUPPORT_ID_RE.search(compact)
            if title_match:
                parts.append(f"title={_truncate(title_match.group(1).strip())}")
            if support_match:
                parts.append(f"support_id={support_match.group(1)}")
            if compact:
                parts.append(f"details={_truncate(compact)}")
        elif compact:
            parts.append(_truncate(compact))

    return ": ".join(parts)


def _extract_auth_code_from_url(url: str) -> str | None:
    query = parse_qs(urlparse(url).query)
    code_values = query.get("code")
    if code_values:
        return code_values[0]
    return None


def _generate_pkce() -> tuple[str, str]:
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("utf-8")).digest()
    ).decode("utf-8").rstrip("=")
    return code_verifier, code_challenge


def _extract_login_form_action(html: str) -> str | None:
    parser = _LoginFormParser()
    parser.feed(html)
    return parser.action


def _follow_redirects_until_code(
    session: requests.Session, response: requests.Response, max_hops: int = 5
) -> str | None:
    current = response
    code = _extract_auth_code_from_url(current.url)
    if code:
        return code

    for _ in range(max_hops):
        location = current.headers.get("Location")
        if not location:
            return None
        next_url = urljoin(current.url, location)
        code = _extract_auth_code_from_url(next_url)
        if code:
            return code
        try:
            current = session.get(next_url, allow_redirects=False, timeout=10)
        except requests.RequestException as e:
            raise NetworkError(str(e)) from e
    return None


def _authorization_code_grant(email: str, password: str):
    client_id = os.getenv("OIDC_CLIENT_ID", config.OIDC_AUTH_CLIENT_ID)
    redirect_uri = os.getenv("OIDC_REDIRECT_URI", config.OIDC_AUTH_REDIRECT_URI)
    scope = os.getenv("OIDC_SCOPE", config.OIDC_AUTH_SCOPE)
    code_verifier, code_challenge = _generate_pkce()

    session = requests.Session()
    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }

    login_data = {
        "username": email,
        "password": password,
        "credentialId": "",
    }

    try:
        auth_page = session.get(
            config.OIDC_AUTH_URL, params=auth_params, allow_redirects=True, timeout=10
        )
    except requests.RequestException as e:
        raise NetworkError(str(e)) from e

    login_form_action = _extract_login_form_action(auth_page.text or "")
    if not login_form_action:
        code = _extract_auth_code_from_url(auth_page.url)
        if not code:
            raise AuthError(
                _format_oidc_error(auth_page, "Login form not found and no auth code received")
            )
    else:
        form_action_url = urljoin(auth_page.url, login_form_action)
        try:
            login_response = session.post(
                form_action_url, data=login_data, allow_redirects=False, timeout=10
            )
        except requests.RequestException as e:
            raise NetworkError(str(e)) from e

        code = _follow_redirects_until_code(session, login_response)
        if not code:
            if login_response.status_code in (401, 403):
                raise AuthError("Authentication failed: invalid credentials")
            raise AuthError(_format_oidc_error(login_response, "Authentication failed"))

    token_data = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    try:
        token_response = session.post(config.OIDC_TOKEN_URL, data=token_data, timeout=10)
    except requests.RequestException as e:
        raise NetworkError(str(e)) from e

    if token_response.status_code != 200:
        raise AuthError(_format_oidc_error(token_response, "Token exchange failed"))

    token_json = token_response.json()
    access_token = token_json.get("access_token")
    refresh_token = token_json.get("refresh_token")
    if not access_token:
        raise AuthError("Token exchange failed: missing access_token")
    return access_token, refresh_token


def password_grant(email: str, password: str):
    return _authorization_code_grant(email, password)


def refresh_grant(refresh_token: str) -> str:
    client_id = os.getenv("OIDC_CLIENT_ID", config.OIDC_AUTH_CLIENT_ID)
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    r = requests.post(config.OIDC_TOKEN_URL, data=data, timeout=10)
    if r.status_code != 200:
        raise AuthError(_format_oidc_error(r, "Refresh failed"))
    return r.json()["access_token"]
