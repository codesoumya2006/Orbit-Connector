import logging
from typing import Any

import httpx

from app.providers.meta_ads.constants import META_GRAPH_BASE_URL

logger = logging.getLogger("orbit.meta_ads.account")


class MetaAdsAccountError(Exception):
    """Exception raised for Meta Ads account discovery and validation errors."""


def clean_ad_account_id(account_id: str) -> str:
    """Normalize Meta ad account ID into clean numeric digits.

    Strips 'act_' prefix, whitespace, and non-numeric characters.
    e.g. 'act_1234567890' -> '1234567890'.
    """
    raw = str(account_id).strip()
    if not raw:
        raise MetaAdsAccountError("Meta ad account ID cannot be empty.")
    if raw.startswith("act_"):
        raw = raw[len("act_") :]
    digits_only = "".join(ch for ch in raw if ch.isdigit())
    if not digits_only:
        raise MetaAdsAccountError(f"Invalid Meta ad account ID '{account_id}'. Must contain numeric digits.")
    return digits_only


def format_act_id(account_id: str) -> str:
    """Format numeric ad account ID with 'act_' prefix for Graph API endpoints.

    e.g. '1234567890' -> 'act_1234567890'.
    """
    return f"act_{clean_ad_account_id(account_id)}"


def list_accessible_ad_accounts(
    access_token: str,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """List all ad accounts directly or business-accessible to the authenticated user.

    GET https://graph.facebook.com/{version}/me/adaccounts
    Supports cursor-based pagination via paging.cursors.after or paging.next.
    """
    url = f"{META_GRAPH_BASE_URL}/me/adaccounts"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    params: dict[str, Any] = {
        "fields": "id,account_id,name,account_status,currency,timezone_name,business_name,is_prepay_account",
        "limit": 100,
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    accounts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    try:
        next_url: str | None = url
        current_params: dict[str, Any] | None = params

        while next_url:
            response = client.get(next_url, headers=headers, params=current_params)
            if response.status_code != 200:
                err_data = (
                    response.json()
                    if "application/json" in response.headers.get("content-type", "")
                    else {}
                )
                err_msg = err_data.get("error", {}).get("message") or response.text[:200]
                raise MetaAdsAccountError(
                    f"Failed to list Meta ad accounts ({response.status_code}): {err_msg}"
                )

            data = response.json()
            rows = data.get("data", [])

            for r in rows:
                raw_id = r.get("account_id") or r.get("id", "")
                if not raw_id:
                    continue
                clean_id = clean_ad_account_id(str(raw_id))
                if clean_id in seen_ids:
                    continue
                seen_ids.add(clean_id)

                accounts.append({
                    "ad_account_id": clean_id,
                    "account_id": clean_id,
                    "name": r.get("name") or f"Ad Account {clean_id}",
                    "account_status": int(r.get("account_status", 1)),
                    "currency": str(r.get("currency")).strip().upper() if r.get("currency") else None,
                    "timezone_name": r.get("timezone_name") or "UTC",
                    "business_name": r.get("business_name"),
                    "is_prepay": bool(r.get("is_prepay_account", False)),
                })

            # Cursor / Next page resolution via paging.next
            paging = data.get("paging", {})
            next_link = paging.get("next")

            if next_link and next_link != next_url:
                next_url = next_link
                current_params = None  # query params are embedded in paging.next
            else:
                next_url = None

        return accounts
    except httpx.RequestError as exc:
        raise MetaAdsAccountError(
            f"Network error listing Meta ad accounts: {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()


def get_ad_account_details(
    access_token: str,
    account_id: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Retrieve metadata for a specific Meta ad account.

    GET https://graph.facebook.com/{version}/act_{account_id}
    """
    clean_id = clean_ad_account_id(account_id)
    act_id = format_act_id(clean_id)
    url = f"{META_GRAPH_BASE_URL}/{act_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    params = {
        "fields": "id,account_id,name,account_status,currency,timezone_name,business_name,is_prepay_account"
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    try:
        response = client.get(url, headers=headers, params=params)
        if response.status_code != 200:
            err_data = (
                response.json()
                if "application/json" in response.headers.get("content-type", "")
                else {}
            )
            err_msg = err_data.get("error", {}).get("message") or response.text[:200]
            raise MetaAdsAccountError(
                f"Failed to retrieve ad account details for '{clean_id}' ({response.status_code}): {err_msg}"
            )

        r = response.json()
        return {
            "ad_account_id": clean_id,
            "name": r.get("name") or f"Ad Account {clean_id}",
            "account_status": int(r.get("account_status", 1)),
            "currency": str(r.get("currency")).strip().upper() if r.get("currency") else None,
            "timezone_name": r.get("timezone_name") or "UTC",
            "business_name": r.get("business_name"),
            "is_prepay": bool(r.get("is_prepay_account", False)),
        }
    except httpx.RequestError as exc:
        raise MetaAdsAccountError(
            f"Network error retrieving ad account details for '{clean_id}': {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()


def validate_ad_account_access(
    access_token: str,
    account_id: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Validate that the authenticated user has authorized access to the target ad account.

    Never trusts frontend-supplied account IDs without server-side validation.
    """
    clean_id = clean_ad_account_id(account_id)
    return get_ad_account_details(access_token=access_token, account_id=clean_id, client=client)

