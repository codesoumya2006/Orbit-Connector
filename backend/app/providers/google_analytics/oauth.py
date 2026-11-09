import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GA4_READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"


class GoogleOAuthError(Exception):
    """Base error for Google OAuth failures."""


def generate_pkce() -> tuple[str, str]:
    """Generate a high-entropy PKCE code verifier and S256 code challenge."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_google_authorization_url(state: str, code_challenge: str) -> str:
    """Construct the Google OAuth 2.0 authorization URL with PKCE and offline access."""
    if not settings.google_client_id:
        raise GoogleOAuthError("GOOGLE_CLIENT_ID is not configured in backend environment.")

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": GA4_READONLY_SCOPE,
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
    """Exchange authorization code and PKCE verifier for Google tokens."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise GoogleOAuthError("Google OAuth credentials are not configured on the backend.")

    payload = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": settings.google_redirect_uri,
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    try:
        response = client.post(GOOGLE_TOKEN_ENDPOINT, data=payload)
        if response.status_code != 200:
            error_data = response.json() if "application/json" in response.headers.get("content-type", "") else {}
            error_desc = error_data.get("error_description") or error_data.get("error") or response.text[:200]
            raise GoogleOAuthError(f"Google token exchange failed ({response.status_code}): {error_desc}")

        token_data = response.json()
        if not token_data.get("access_token"):
            raise GoogleOAuthError("Google token response did not contain an access_token.")
        return token_data
    except httpx.RequestError as exc:
        raise GoogleOAuthError(f"Network error communicating with Google token endpoint: {type(exc).__name__}") from exc
    finally:
        if close_client:
            client.close()


def refresh_access_token(
    refresh_token: str,
    client: httpx.Client | None = None,
) -> str:
    """Use a stored refresh token to obtain a fresh short-lived access token."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise GoogleOAuthError("Google OAuth credentials are not configured on the backend.")

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
            error_data = response.json() if "application/json" in response.headers.get("content-type", "") else {}
            error_code = error_data.get("error")
            error_desc = error_data.get("error_description") or error_code or response.text[:200]
            if error_code in ("invalid_grant", "unauthorized_client"):
                raise GoogleOAuthError(f"Permanent OAuth failure ({error_code}): {error_desc}")
            raise GoogleOAuthError(f"Failed to refresh access token ({response.status_code}): {error_desc}")

        token_data = response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise GoogleOAuthError("Refreshed token response did not contain an access_token.")
        return access_token
    except httpx.RequestError as exc:
        raise GoogleOAuthError(f"Network error refreshing Google token: {type(exc).__name__}") from exc
    finally:
        if close_client:
            client.close()

