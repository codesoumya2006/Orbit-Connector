import logging
import urllib.parse
from typing import Any

import httpx

from app.core.config import settings
from app.providers.meta_ads.constants import (
    META_ADS_API_VERSION,
    META_ADS_SCOPE,
    META_OAUTH_DIALOG_URL,
    META_OAUTH_TOKEN_URL,
)

logger = logging.getLogger("orbit.meta_ads.oauth")


class MetaAdsOAuthError(Exception):
    """Exception raised for Meta Ads OAuth flow errors."""


def build_meta_ads_authorization_url(state: str) -> str:
    """Build Meta OAuth 2.0 authorization dialog URL with state and scope.

    Requests 'ads_read' scope for read-only advertising reports and ad accounts.
    """
    if not settings.meta_app_id:
        raise MetaAdsOAuthError("META_APP_ID is not configured in backend environment.")
    if not settings.meta_redirect_uri:
        raise MetaAdsOAuthError("META_REDIRECT_URI is not configured in backend environment.")

    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": settings.meta_redirect_uri,
        "state": state,
        "scope": META_ADS_SCOPE,
        "response_type": "code",
    }
    return f"{META_OAUTH_DIALOG_URL}?{urllib.parse.urlencode(params)}"


def exchange_authorization_code(
    code: str, client: httpx.Client | None = None
) -> dict[str, Any]:
    """Exchange authorization code for a short-lived user access token.

    Calls Meta Graph API:
    GET https://graph.facebook.com/{version}/oauth/access_token
    """
    if not settings.meta_app_id or not settings.meta_app_secret:
        raise MetaAdsOAuthError("Meta App ID and Secret must be configured for OAuth token exchange.")

    params = {
        "client_id": settings.meta_app_id,
        "client_secret": settings.meta_app_secret,
        "redirect_uri": settings.meta_redirect_uri,
        "code": code,
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    try:
        response = client.get(META_OAUTH_TOKEN_URL, params=params)
        if response.status_code != 200:
            err_data = (
                response.json()
                if "application/json" in response.headers.get("content-type", "")
                else {}
            )
            err_msg = err_data.get("error", {}).get("message") or response.text[:200]
            raise MetaAdsOAuthError(
                f"Failed to exchange authorization code ({response.status_code}): {err_msg}"
            )
        data = response.json()
        if "access_token" not in data:
            raise MetaAdsOAuthError("Meta token response missing 'access_token'.")
        return data
    except httpx.RequestError as exc:
        raise MetaAdsOAuthError(
            f"Network error during Meta OAuth code exchange: {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()


def exchange_for_long_lived_token(
    short_lived_token: str, client: httpx.Client | None = None
) -> dict[str, Any]:
    """Exchange a short-lived user access token for a long-lived user access token (~60 days validity).

    Meta does not support standard refresh tokens for user access tokens.
    Instead, long-lived tokens are generated via grant_type=fb_exchange_token.
    """
    if not settings.meta_app_id or not settings.meta_app_secret:
        raise MetaAdsOAuthError("Meta App ID and Secret must be configured for long-lived token exchange.")

    params = {
        "grant_type": "fb_exchange_token",
        "client_id": settings.meta_app_id,
        "client_secret": settings.meta_app_secret,
        "fb_exchange_token": short_lived_token,
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    try:
        response = client.get(META_OAUTH_TOKEN_URL, params=params)
        if response.status_code != 200:
            err_data = (
                response.json()
                if "application/json" in response.headers.get("content-type", "")
                else {}
            )
            err_msg = err_data.get("error", {}).get("message") or response.text[:200]
            raise MetaAdsOAuthError(
                f"Failed to exchange for long-lived Meta token ({response.status_code}): {err_msg}"
            )
        data = response.json()
        if "access_token" not in data:
            raise MetaAdsOAuthError("Meta long-lived token response missing 'access_token'.")
        return data
    except httpx.RequestError as exc:
        raise MetaAdsOAuthError(
            f"Network error exchanging for long-lived Meta token: {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()


def revoke_meta_token(access_token: str, client: httpx.Client | None = None) -> bool:
    """Revoke Meta user permissions for the current app.

    DELETE https://graph.facebook.com/{version}/me/permissions?access_token={access_token}
    """
    url = f"https://graph.facebook.com/{META_ADS_API_VERSION}/me/permissions"
    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    try:
        response = client.delete(url, params={"access_token": access_token})
        return response.status_code in (200, 204)
    except Exception as exc:
        logger.warning("Failed to revoke Meta permissions: %s", exc)
        return False
    finally:
        if close_client:
            client.close()

