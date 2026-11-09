from typing import Any

import httpx

ADMIN_API_BASE_URL = "https://analyticsadmin.googleapis.com/v1beta"


class GoogleAnalyticsAdminError(Exception):
    """Base error for Google Analytics Admin API failures."""


def list_accounts_and_properties(
    access_token: str,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Retrieve all accessible GA4 accounts and properties for the authenticated account."""
    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    discovered: list[dict[str, Any]] = []
    page_token: str | None = None

    try:
        while True:
            params: dict[str, Any] = {"pageSize": 200}
            if page_token:
                params["pageToken"] = page_token

            response = client.get(
                f"{ADMIN_API_BASE_URL}/accountSummaries",
                headers=headers,
                params=params,
            )

            if response.status_code != 200:
                err_data = response.json() if "application/json" in response.headers.get("content-type", "") else {}
                err_msg = err_data.get("error", {}).get("message") or response.text[:200]
                raise GoogleAnalyticsAdminError(
                    f"Google Analytics Admin API request failed ({response.status_code}): {err_msg}"
                )

            data = response.json()
            summaries = data.get("accountSummaries", [])
            for acc in summaries:
                acc_name = acc.get("displayName", "Unnamed Account")
                acc_id = acc.get("account", "")
                for prop in acc.get("propertySummaries", []):
                    prop_id = prop.get("property", "")
                    prop_display = prop.get("displayName", "Unnamed Property")
                    discovered.append(
                        {
                            "property_id": prop_id,
                            "display_name": prop_display,
                            "account_id": acc_id,
                            "account_name": acc_name,
                        }
                    )

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return discovered
    except httpx.RequestError as exc:
        raise GoogleAnalyticsAdminError(
            f"Network error querying Google Analytics Admin API: {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()


def verify_property_accessible(
    access_token: str,
    target_property_id: str,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Validate that the target property is genuinely accessible to this Google account.

    Never trust a client-supplied property ID without checking access.
    """
    normalized_target = target_property_id if target_property_id.startswith("properties/") else f"properties/{target_property_id}"
    all_properties = list_accounts_and_properties(access_token=access_token, client=client)
    for p in all_properties:
        if p["property_id"] == normalized_target or p["property_id"].replace("properties/", "") == target_property_id:
            return p
    return None

