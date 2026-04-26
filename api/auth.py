import requests
import re
from . import config
from .exceptions import AuthError, NetworkError

# Keep excerpts short (300 chars) so auth errors stay readable in logs and avoid large payload leakage.
MAX_ERROR_TEXT_LENGTH = 300
HTML_TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)
SUPPORT_ID_RE = re.compile(r"support\s*id:\s*([A-Za-z0-9\-]+)", re.IGNORECASE)


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
        is_html = "html" in response.headers.get("Content-Type", "").lower() or "<html" in raw.lower()

        if is_html:
            title_match = HTML_TITLE_RE.search(raw)
            support_match = SUPPORT_ID_RE.search(compact)
            if title_match:
                parts.append(f"title={_truncate(title_match.group(1).strip())}")
            if support_match:
                parts.append(f"support_id={support_match.group(1)}")
            if compact:
                parts.append(f"details={_truncate(compact)}")
        elif compact:
            parts.append(_truncate(compact))

    return ": ".join(parts)


def password_grant(email: str, password: str):
    data = {
        "client_id": "app-x-ios",
        "username": email,
        "password": password,
        "grant_type": "password",
    }
    try:
        r = requests.post(config.OIDC_TOKEN_URL, data=data, timeout=10)
    except requests.RequestException as e:
        raise NetworkError(str(e)) from e

    if r.status_code != 200:
        raise AuthError(_format_oidc_error(r, "Authentication failed"))

    j = r.json()
    return j["access_token"], j["refresh_token"]


def refresh_grant(refresh_token: str) -> str:
    data = {
        "client_id": "app-x-ios",
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    r = requests.post(config.OIDC_TOKEN_URL, data=data, timeout=10)
    if r.status_code != 200:
        raise AuthError(_format_oidc_error(r, "Refresh failed"))
    return r.json()["access_token"]
