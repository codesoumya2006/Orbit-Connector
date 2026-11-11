import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.security import decrypt_secret
from app.providers.google_ads.constants import GOOGLE_ADS_SCOPE

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"


class GoogleAdsOAuthError(Exception):
    """Base error for Google Ads OAuth failures."""


def resolve_developer_token(config: dict[str, Any] | None) -> str:
    """Resolve Google Ads developer token from encrypted connection config or backend env."""
    if config and config.get("developer_token_encrypted"):
        try:
            token = decrypt_secret(config["developer_token_encrypted"])
            if token and token.strip():
                return token.strip()
        except Exception:
            pass
    return (settings.google_ads_developer_token or "").strip()


def generate_pkce() -> tuple[str, str]:
    """Generate a high-entropy PKCE code verifier and S256 code challenge."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_google_ads_authorization_url(state: str, code_challenge: str) -> str:
    """Construct the Google Ads OAuth 2.0 authorization URL with PKCE and offline access."""
    if not settings.google_client_id:
        raise GoogleAdsOAuthError("GOOGLE_CLIENT_ID is not configured in backend environment.")

    redirect_uri = settings.google_ads_redirect_uri or "http://localhost:8000/api/v1/oauth/google-ads/callback"

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_ADS_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_authorization_code(
    code: str,
    code_verifier: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Exchange authorization code and PKCE verifier for Google Ads tokens."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise GoogleAdsOAuthError("Google OAuth credentials are not configured on the backend.")

    redirect_uri = settings.google_ads_redirect_uri or "http://localhost:8000/api/v1/oauth/google-ads/callback"

    payload = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    try:
        response = client.post(GOOGLE_TOKEN_ENDPOINT, data=payload)
        if response.status_code != 200:
            error_data = (
                response.json()
                if "application/json" in response.headers.get("content-type", "")
                else {}
            )
            error_desc = (
                error_data.get("error_description")
                or error_data.get("error")
                or response.text[:200]
            )
            raise GoogleAdsOAuthError(
                f"Google Ads token exchange failed ({response.status_code}): {error_desc}"
            )

        token_data = response.json()
        if not token_data.get("access_token"):
            raise GoogleAdsOAuthError(
                "Google Ads token response did not contain an access_token."
            )
        return token_data
    except httpx.RequestError as exc:
        raise GoogleAdsOAuthError(
            f"Network error communicating with Google token endpoint: {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()


def refresh_access_token(
    refresh_token: str,
    client: httpx.Client | None = None,
) -> str:
    """Use a stored refresh token to obtain a fresh short-lived access token."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise GoogleAdsOAuthError("Google OAuth credentials are not configured on the backend.")

    payload = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    try:
        response = client.post(GOOGLE_TOKEN_ENDPOINT, data=payload)
        if response.status_code != 200:
            error_data = (
                response.json()
                if "application/json" in response.headers.get("content-type", "")
                else {}
            )
            error_code = error_data.get("error")
            error_desc = (
                error_data.get("error_description") or error_code or response.text[:200]
            )
            if error_code in ("invalid_grant", "unauthorized_client"):
                raise GoogleAdsOAuthError(
                    f"Permanent OAuth failure ({error_code}): {error_desc}"
                )
            raise GoogleAdsOAuthError(
                f"Failed to refresh access token ({response.status_code}): {error_desc}"
            )

        token_data = response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise GoogleAdsOAuthError(
                "Refreshed token response did not contain an access_token."
            )
        return access_token
    except httpx.RequestError as exc:
        raise GoogleAdsOAuthError(
            f"Network error refreshing Google Ads token: {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()


def revoke_token(token: str, client: httpx.Client | None = None) -> bool:
    """Revoke a token (access or refresh token) with Google OAuth."""
    close_client = False
    if client is None:
        client = httpx.Client(timeout=5.0)
        close_client = True

    try:
        response = client.post(
            GOOGLE_REVOKE_ENDPOINT,
            params={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return response.status_code == 200
    except Exception:
        return False
    finally:
        if close_client:
            client.close()
