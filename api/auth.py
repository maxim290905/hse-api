import requests
from . import config
from .exceptions import AuthError, NetworkError

# Keep excerpts short (300 chars) so auth errors stay readable in logs and avoid large payload leakage.
MAX_ERROR_TEXT_LENGTH = 300


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
        text = response.text
        if len(text) > MAX_ERROR_TEXT_LENGTH:
            text = f"{text[:MAX_ERROR_TEXT_LENGTH]}..."
        parts.append(text)

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
