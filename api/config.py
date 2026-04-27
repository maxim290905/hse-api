from urllib.parse import urljoin

API_V2_URL = "https://api.hseapp.ru/v2/"
API_V3_URL = "https://api.hseapp.ru/v3/"
API_DEFAULT_URL = "https://api.hseapp.ru/"
OIDC_AUTH_URL = "https://saml.hse.ru/realms/hse/protocol/openid-connect/auth"
OIDC_TOKEN_URL = "https://saml.hse.ru/realms/hse/protocol/openid-connect/token"
OIDC_AUTH_CLIENT_ID = "elk"
OIDC_AUTH_REDIRECT_URI = "https://lk.hse.ru/api/keycloak-auth/"
OIDC_AUTH_SCOPE = "openid"


def url(base: str, path: str) -> str:
    return urljoin(base, path)


DEFAULT_TIMEOUT = 10  # seconds
