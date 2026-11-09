from decimal import Decimal
import logging
from typing import Any

import httpx

from app.providers.google_ads.constants import (
    GOOGLE_ADS_API_BASE_URL,
    GOOGLE_ADS_API_VERSION,
)

logger = logging.getLogger("orbit.google_ads.customer")


class GoogleAdsCustomerError(Exception):
    """Exception raised for Google Ads customer discovery/query errors."""


def clean_customer_id(customer_id: str) -> str:
    """Normalize Google Ads customer ID into a clean digit-only string.

    Strips 'customers/' prefix, hyphens, and whitespace.
    e.g. 'customers/123-456-7890' -> '1234567890'.
    """
    cid = str(customer_id).strip()
    if cid.startswith("customers/"):
        cid = cid[len("customers/") :]
    digits_only = "".join(ch for ch in cid if ch.isdigit())
    if not digits_only:
        raise GoogleAdsCustomerError(f"Invalid Google Ads customer ID '{customer_id}'. Must contain numeric digits.")
    return digits_only


def list_accessible_customers(
    access_token: str,
    developer_token: str,
    client: httpx.Client | None = None,
) -> list[str]:
    """List resource names of directly accessible Google Ads accounts for the authenticated user.

    Calls CustomerService.ListAccessibleCustomers:
    GET https://googleads.googleapis.com/{version}/customers:listAccessibleCustomers

    Returns list of cleaned numeric customer IDs (e.g. ['1234567890']).
    Never sends 'login-customer-id' header.
    """
    if not developer_token:
        raise GoogleAdsCustomerError("Google Ads Developer Token is required for customer discovery.")

    url = f"{GOOGLE_ADS_API_BASE_URL}/customers:listAccessibleCustomers"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "Accept": "application/json",
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    try:
        response = client.get(url, headers=headers)
        if response.status_code != 200:
            err_data = (
                response.json()
                if "application/json" in response.headers.get("content-type", "")
                else {}
            )
            err_msg = (
                err_data.get("error", {}).get("message")
                or err_data.get("message")
                or response.text[:200]
            )
            raise GoogleAdsCustomerError(
                f"Failed to list accessible customers ({response.status_code}): {err_msg}"
            )

        data = response.json()
        resource_names = data.get("resourceNames", [])
        return [clean_customer_id(rn) for rn in resource_names]
    except httpx.RequestError as exc:
        raise GoogleAdsCustomerError(
            f"Network error listing accessible Google Ads customers: {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()


def get_customer_details(
    access_token: str,
    developer_token: str,
    customer_id: str,
    login_customer_id: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Retrieve metadata for a specific customer using GoogleAdsService.search with GAQL.

    Google Ads API v26 CustomerService does not expose a GET customer details method.
    Instead, GoogleAdsService.search against the 'customer' resource is used:
    POST https://googleads.googleapis.com/{version}/customers/{customer_id}/googleAds:search

    GAQL Query:
    SELECT
      customer.id,
      customer.descriptive_name,
      customer.currency_code,
      customer.time_zone,
      customer.manager,
      customer.test_account
    FROM customer
    LIMIT 1

    Headers:
    - Authorization: Bearer <access_token>
    - developer-token: <developer_token>
    - Content-Type: application/json
    - Accept: application/json
    - login-customer-id: <login_customer_id> (only when accessing a client through an MCC)
    """
    clean_id = clean_customer_id(customer_id)
    url = f"{GOOGLE_ADS_API_BASE_URL}/customers/{clean_id}/googleAds:search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if login_customer_id:
        clean_login = clean_customer_id(login_customer_id)
        if clean_login != clean_id:
            headers["login-customer-id"] = clean_login

    query = (
        "SELECT "
        "customer.id, "
        "customer.descriptive_name, "
        "customer.currency_code, "
        "customer.time_zone, "
        "customer.manager, "
        "customer.test_account "
        "FROM customer "
        "LIMIT 1"
    )

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    try:
        response = client.post(url, headers=headers, json={"query": query})
        if response.status_code != 200:
            err_data = (
                response.json()
                if "application/json" in response.headers.get("content-type", "")
                else {}
            )
            err_detail = err_data.get("error", {})
            err_msg = (
                err_detail.get("message")
                or err_data.get("message")
                or response.text[:200]
            )
            raise GoogleAdsCustomerError(
                f"Failed to query customer details for '{clean_id}' ({response.status_code}): {err_msg}"
            )

        data = response.json()
        results = data.get("results", [])
        if not results:
            raise GoogleAdsCustomerError(
                f"Customer '{clean_id}' returned no customer metadata records."
            )

        c = results[0].get("customer", {})
        return {
            "customer_id": clean_customer_id(str(c.get("id") or clean_id)),
            "descriptive_name": str(c.get("descriptiveName") or f"Account {clean_id}"),
            "currency_code": c.get("currencyCode"),
            "time_zone": c.get("timeZone"),
            "is_manager": bool(c.get("manager", False)),
            "test_account": bool(c.get("testAccount", False)),
            "login_customer_id": clean_customer_id(login_customer_id) if login_customer_id else None,
        }
    except httpx.RequestError as exc:
        raise GoogleAdsCustomerError(
            f"Network error querying customer details for '{clean_id}': {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()


def list_client_accounts_for_manager(
    access_token: str,
    developer_token: str,
    manager_customer_id: str,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Enumerate all client accounts linked beneath a manager account using customer_client GAQL relationship.

    Sends 'login-customer-id: {manager_customer_id}'.
    Discovers all enabled descendants across all levels in the manager's subtree.
    Supports full pagination via nextPageToken.
    """
    clean_manager = clean_customer_id(manager_customer_id)
    url = f"{GOOGLE_ADS_API_BASE_URL}/customers/{clean_manager}/googleAds:search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "login-customer-id": clean_manager,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    query = (
        "SELECT "
        "customer_client.client_customer, "
        "customer_client.level, "
        "customer_client.manager, "
        "customer_client.descriptive_name, "
        "customer_client.currency_code, "
        "customer_client.time_zone, "
        "customer_client.id, "
        "customer_client.status, "
        "customer_client.test_account "
        "FROM customer_client "
        "WHERE customer_client.status = 'ENABLED'"
    )

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    client_accounts: list[dict[str, Any]] = []
    page_token: str | None = None

    try:
        while True:
            body: dict[str, Any] = {"query": query}
            if page_token:
                body["pageToken"] = page_token

            response = client.post(url, headers=headers, json=body)
            if response.status_code != 200:
                err_data = (
                    response.json()
                    if "application/json" in response.headers.get("content-type", "")
                    else {}
                )
                err_msg = (
                    err_data.get("error", {}).get("message")
                    or err_data.get("message")
                    or response.text[:200]
                )
                raise GoogleAdsCustomerError(
                    f"customer_client search failed for manager '{clean_manager}' ({response.status_code}): {err_msg}"
                )

            data = response.json()
            results = data.get("results", [])

            for row in results:
                cc = row.get("customerClient", {})
                level = int(cc.get("level", 0))

                # level 0 is the manager account itself
                if level == 0:
                    continue

                status = cc.get("status", "ENABLED")
                if status != "ENABLED":
                    continue

                raw_id = cc.get("id") or cc.get("clientCustomer", "")
                if not raw_id:
                    continue
                cid = clean_customer_id(str(raw_id))
                descriptive_name = cc.get("descriptiveName") or f"Account {cid}"
                is_sub_manager = bool(cc.get("manager", False))
                is_test_account = bool(cc.get("testAccount", False))

                client_accounts.append({
                    "customer_id": cid,
                    "descriptive_name": descriptive_name,
                    "currency_code": cc.get("currencyCode"),
                    "time_zone": cc.get("timeZone"),
                    "is_manager": is_sub_manager,
                    "test_account": is_test_account,
                    "level": level,
                    "login_customer_id": clean_manager,
                })

            next_page_token = data.get("nextPageToken")
            if next_page_token and next_page_token != page_token:
                page_token = next_page_token
            else:
                break

        return client_accounts
    except httpx.RequestError as exc:
        raise GoogleAdsCustomerError(
            f"Network error querying client accounts for manager '{clean_manager}': {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()


def discover_accessible_customers(
    access_token: str,
    developer_token: str,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Discover all directly accessible Google Ads accounts and all client accounts linked beneath managers.

    Handles advertiser accounts, manager accounts, and multi-level manager client hierarchies.
    De-duplicates accounts while preserving login_customer_id routing:
    - Direct access relationships take precedence (login_customer_id is None).
    - Client accounts linked under managers preserve their login_customer_id.
    - Deeply nested descendants across multiple levels (level 1, level 2, etc.) are included.
    """
    direct_customer_ids = list_accessible_customers(
        access_token=access_token,
        developer_token=developer_token,
        client=client,
    )

    discovered_map: dict[str, dict[str, Any]] = {}
    direct_id_set = set(direct_customer_ids)

    # 1. First populate direct accounts (they do not require login-customer-id)
    for cid in direct_customer_ids:
        try:
            details = get_customer_details(
                access_token=access_token,
                developer_token=developer_token,
                customer_id=cid,
                client=client,
            )
            # Direct accounts have no login_customer_id
            details["login_customer_id"] = None
            discovered_map[cid] = details
        except GoogleAdsCustomerError as exc:
            logger.warning("Could not fetch details for direct customer %s: %s", cid, exc)

    # 2. For each direct manager account, discover all child clients linked beneath it
    for cid in direct_customer_ids:
        direct_account = discovered_map.get(cid)
        if direct_account and direct_account.get("is_manager"):
            try:
                child_clients = list_client_accounts_for_manager(
                    access_token=access_token,
                    developer_token=developer_token,
                    manager_customer_id=cid,
                    client=client,
                )
                for child in child_clients:
                    child_id = child["customer_id"]
                    # If this customer was already discovered directly, DO NOT replace its direct access
                    if child_id in direct_id_set:
                        continue
                    # If not yet discovered, or if found at a shallower level, record it
                    if child_id not in discovered_map:
                        discovered_map[child_id] = child
                    elif "level" in child and "level" in discovered_map[child_id]:
                        if child["level"] < discovered_map[child_id]["level"]:
                            discovered_map[child_id] = child
            except GoogleAdsCustomerError as exc:
                logger.warning("Could not enumerate hierarchy for manager %s: %s", cid, exc)

    return list(discovered_map.values())


def validate_customer_access(
    access_token: str,
    developer_token: str,
    target_customer_id: str,
    target_login_customer_id: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Validate that the authenticated Google Ads user has authorized access to a target customer.

    Never blindly trusts a client-submitted login-customer-id.
    Validates either direct accessibility or authorized manager hierarchy.
    Distinguishes manager accounts from advertiser accounts.
    """
    clean_target = clean_customer_id(target_customer_id)
    direct_ids = list_accessible_customers(
        access_token=access_token,
        developer_token=developer_token,
        client=client,
    )
    direct_id_set = set(direct_ids)

    # Case 1: Client supplied a login_customer_id (manager account)
    if target_login_customer_id:
        clean_login = clean_customer_id(target_login_customer_id)
        if clean_login == clean_target:
            clean_login = None

    if target_login_customer_id:
        clean_login = clean_customer_id(target_login_customer_id)
        # Verify clean_login is accessible to the user (either directly in direct_ids or via accessible hierarchy)
        if clean_login not in direct_id_set:
            login_authorized = False
            for mgr_id in direct_ids:
                try:
                    sub_clients = list_client_accounts_for_manager(
                        access_token=access_token,
                        developer_token=developer_token,
                        manager_customer_id=mgr_id,
                        client=client,
                    )
                    for sc in sub_clients:
                        if sc["customer_id"] == clean_login and sc["is_manager"]:
                            login_authorized = True
                            break
                    if login_authorized:
                        break
                except Exception:
                    continue

            if not login_authorized:
                raise GoogleAdsCustomerError(
                    f"Unauthorized login_customer_id '{clean_login}': Manager account is not accessible to this user."
                )

        # Now verify that target_customer_id is accessible under clean_login
        try:
            details = get_customer_details(
                access_token=access_token,
                developer_token=developer_token,
                customer_id=clean_target,
                login_customer_id=clean_login,
                client=client,
            )
            details["login_customer_id"] = clean_login
            return details
        except GoogleAdsCustomerError as exc:
            raise GoogleAdsCustomerError(
                f"Customer '{clean_target}' is not accessible under manager account '{clean_login}': {exc}"
            ) from exc

    # Case 2: No login_customer_id was supplied
    # Check if target is directly accessible
    if clean_target in direct_id_set:
        details = get_customer_details(
            access_token=access_token,
            developer_token=developer_token,
            customer_id=clean_target,
            login_customer_id=None,
            client=client,
        )
        details["login_customer_id"] = None
        return details

    # Case 3: Target is not directly accessible, search accessible managers for a valid path
    for mgr_id in direct_ids:
        try:
            child_clients = list_client_accounts_for_manager(
                access_token=access_token,
                developer_token=developer_token,
                manager_customer_id=mgr_id,
                client=client,
            )
            for child in child_clients:
                if child["customer_id"] == clean_target:
                    details = get_customer_details(
                        access_token=access_token,
                        developer_token=developer_token,
                        customer_id=clean_target,
                        login_customer_id=mgr_id,
                        client=client,
                    )
                    details["login_customer_id"] = mgr_id
                    return details
        except Exception:
            continue

    raise GoogleAdsCustomerError(
        f"Customer '{clean_target}' is not accessible to the authenticated Google Ads user."
    )
