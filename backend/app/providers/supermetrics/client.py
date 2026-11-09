"""Supermetrics Enterprise v2 API HTTP client, authentication validation, and query execution."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import logging
import re
from typing import Any

import httpx

from app.providers.supermetrics.constants import (
    DEFAULT_FIELDS,
    SUPERMETRICS_LOGINS_ENDPOINT,
    SUPERMETRICS_METADATA_ENDPOINT,
    SUPERMETRICS_QUERY_ENDPOINT,
    SUPPORTED_DATA_SOURCES,
    get_supermetrics_login_accounts_endpoint,
)
from app.workers.local_worker import PermanentSyncError, RetryableSyncError

logger = logging.getLogger("orbit.supermetrics.client")
DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SupermetricsError(Exception):
    """Base exception for Supermetrics API errors."""


class SupermetricsAuthError(PermanentSyncError):
    """Raised for authentication or permission failures with Supermetrics."""


class SupermetricsRateLimitError(RetryableSyncError):
    """Raised for Supermetrics rate limiting (HTTP 429 or quota limit)."""


class SupermetricsDataError(Exception):
    """Raised for query, date, login, or schema issues."""


@dataclass(frozen=True)
class NormalizedSupermetricsRow:
    """Normalized marketing data row from Supermetrics query."""

    report_date: str
    ds_id: str
    ds_account_id: str
    campaign_id: str
    campaign_name: str
    impressions: int
    clicks: int
    cost: Decimal
    conversions: float
    conversions_value: Decimal
    currency: str
    raw_metrics_json: dict[str, Any]


def resolve_date_range(start_date: str, end_date: str) -> tuple[str, str]:
    """Resolve and validate start_date and end_date strings into ISO YYYY-MM-DD format."""
    now_utc = datetime.now(timezone.utc).date()

    def _resolve(val: str) -> date:
        cleaned = str(val).strip().lower()
        if DATE_REGEX.match(cleaned):
            return date.fromisoformat(cleaned)
        if cleaned == "today":
            return now_utc
        if cleaned == "yesterday":
            return now_utc - timedelta(days=1)
        if cleaned.endswith("daysago"):
            digits = cleaned[:-7]
            if digits.isdigit():
                return now_utc - timedelta(days=int(digits))
        raise ValueError(
            f"Invalid date '{val}'. Must be YYYY-MM-DD or relative keyword (e.g. today, yesterday, 30daysAgo)."
        )

    d_start = _resolve(start_date)
    d_end = _resolve(end_date)
    if d_start > d_end:
        raise ValueError(f"start_date '{start_date}' cannot be after end_date '{end_date}'.")

    return d_start.isoformat(), d_end.isoformat()


def validate_supermetrics_api_key(
    api_key: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Validate Supermetrics API key by probing the official logins endpoint.

    GET https://api.supermetrics.com/enterprise/v2/ds/logins
    """
    cleaned_key = str(api_key).strip()
    if not cleaned_key:
        raise SupermetricsAuthError("Supermetrics API key cannot be empty.")

    headers = {
        "Authorization": f"Bearer {cleaned_key}",
        "Accept": "application/json",
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    try:
        response = client.get(SUPERMETRICS_LOGINS_ENDPOINT, headers=headers)
        if response.status_code in (401, 403):
            raise SupermetricsAuthError(
                f"Supermetrics authentication failed ({response.status_code}). Invalid or revoked API key."
            )
        if response.status_code == 429:
            raise SupermetricsRateLimitError("Supermetrics API rate limit reached.")
        if response.status_code >= 500:
            raise RetryableSyncError(f"Supermetrics service temporarily unavailable ({response.status_code}).")

        if response.status_code != 200:
            res_meta = client.get(SUPERMETRICS_METADATA_ENDPOINT, headers=headers)
            if res_meta.status_code in (401, 403):
                raise SupermetricsAuthError("Supermetrics authentication rejected.")
            if res_meta.status_code not in (200, 204):
                raise SupermetricsError(f"Supermetrics API key validation returned unexpected status {response.status_code}.")

        try:
            data = response.json() if "application/json" in response.headers.get("content-type", "") else {}
        except Exception:
            data = {}

        return {
            "valid": True,
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "metadata": data,
        }
    except httpx.RequestError as exc:
        raise RetryableSyncError(f"Network error verifying Supermetrics API key: {type(exc).__name__}") from exc
    finally:
        if close_client:
            client.close()


def list_supermetrics_data_sources(
    api_key: str,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """List available marketing data source connectors supported by Supermetrics."""
    validate_supermetrics_api_key(api_key=api_key, client=client)
    return SUPPORTED_DATA_SOURCES


def list_supermetrics_logins(
    api_key: str,
    ds_id: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """List authenticated logins for the API key, optionally filtered by ds_id.

    GET https://api.supermetrics.com/enterprise/v2/ds/logins
    Handles pagination using documented pagination fields (meta.pagination.next / cursor).
    """
    cleaned_key = str(api_key).strip()
    validate_supermetrics_api_key(api_key=cleaned_key, client=client)

    headers = {
        "Authorization": f"Bearer {cleaned_key}",
        "Accept": "application/json",
    }
    params: dict[str, Any] = {}
    if ds_id:
        params["ds_id"] = str(ds_id).strip().upper()

    close_client = False
    if client is None:
        client = httpx.Client(timeout=20.0)
        close_client = True

    logins: list[dict[str, Any]] = []
    url: str | None = SUPERMETRICS_LOGINS_ENDPOINT

    try:
        pages_fetched = 0
        while url and pages_fetched < 20:
            pages_fetched += 1
            response = client.get(url, params=params if pages_fetched == 1 else None, headers=headers)
            if response.status_code in (401, 403):
                raise SupermetricsAuthError(f"Supermetrics login listing unauthorized ({response.status_code}).")
            if response.status_code == 429:
                raise SupermetricsRateLimitError("Supermetrics rate limit encountered listing logins.")
            if response.status_code >= 500:
                raise RetryableSyncError(f"Supermetrics server error listing logins ({response.status_code}).")
            if response.status_code != 200:
                break

            data = response.json()
            raw_items = data.get("data", [])
            if not isinstance(raw_items, list):
                raw_items = []

            for item in raw_items:
                if isinstance(item, dict):
                    lid = str(item.get("login_id") or item.get("id") or "").strip()
                    item_ds = str(item.get("ds_id") or ds_id or "").strip().upper()
                    if lid:
                        logins.append({
                            "login_id": lid,
                            "ds_id": item_ds,
                            "username": item.get("username") or item.get("user") or item.get("email"),
                            "display_name": item.get("display_name") or item.get("name") or lid,
                        })

            # Check for next page link in meta.pagination
            meta = data.get("meta", {})
            pagination = meta.get("pagination") or {}
            next_link = pagination.get("next") or meta.get("next")
            if next_link and isinstance(next_link, str) and next_link.startswith("http"):
                url = next_link
            else:
                url = None

        return logins
    except httpx.RequestError as exc:
        raise RetryableSyncError(f"Network error listing Supermetrics logins: {exc}") from exc
    finally:
        if close_client:
            client.close()


def list_supermetrics_login_accounts(
    api_key: str,
    login_id: str,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """List accessible accounts for a specific login under Supermetrics Enterprise API.

    GET https://api.supermetrics.com/enterprise/v2/ds/login/{login_id}/accounts
    Handles pagination using meta.pagination.next.
    """
    cleaned_key = str(api_key).strip()
    cleaned_login = str(login_id).strip()
    if not cleaned_login:
        raise SupermetricsDataError("login_id cannot be empty.")

    headers = {
        "Authorization": f"Bearer {cleaned_key}",
        "Accept": "application/json",
    }
    endpoint = get_supermetrics_login_accounts_endpoint(cleaned_login)

    close_client = False
    if client is None:
        client = httpx.Client(timeout=20.0)
        close_client = True

    accounts: list[dict[str, Any]] = []
    url: str | None = endpoint

    try:
        pages_fetched = 0
        while url and pages_fetched < 20:
            pages_fetched += 1
            response = client.get(url, headers=headers)
            if response.status_code in (401, 403):
                raise SupermetricsAuthError(
                    f"Supermetrics unauthorized listing accounts for login '{cleaned_login}'."
                )
            if response.status_code == 429:
                raise SupermetricsRateLimitError("Supermetrics rate limit listing login accounts.")
            if response.status_code == 404:
                logger.warning("Supermetrics login '%s' not found or has no accounts.", cleaned_login)
                break
            if response.status_code >= 500:
                raise RetryableSyncError(
                    f"Supermetrics server error listing login accounts ({response.status_code})."
                )
            if response.status_code != 200:
                break

            data = response.json()
            raw_accounts = data.get("data", [])
            if not isinstance(raw_accounts, list):
                raw_accounts = data.get("accounts", []) or []

            for acct in raw_accounts:
                if isinstance(acct, dict):
                    aid = str(acct.get("account_id") or acct.get("id") or "").strip()
                    aname = str(acct.get("account_name") or acct.get("name") or aid).strip()
                    if aid:
                        accounts.append({
                            "account_id": aid,
                            "account_name": aname,
                            "login_id": cleaned_login,
                            "currency": acct.get("currency"),
                        })

            # Check next page in pagination
            meta = data.get("meta", {})
            pagination = meta.get("pagination") or {}
            next_link = pagination.get("next") or meta.get("next")
            if next_link and isinstance(next_link, str) and next_link.startswith("http"):
                url = next_link
            else:
                url = None

        return accounts
    except httpx.RequestError as exc:
        raise RetryableSyncError(f"Network error listing accounts for login '{cleaned_login}': {exc}") from exc
    finally:
        if close_client:
            client.close()


def list_supermetrics_accounts(
    api_key: str,
    ds_id: str,
    login_id: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """List accessible accounts for a given data source using documented login -> accounts sequence.

    1. If login_id provided: GET /enterprise/v2/ds/login/{login_id}/accounts
    2. Else: GET /enterprise/v2/ds/logins?ds_id={ds_id} -> for each login, GET /enterprise/v2/ds/login/{login_id}/accounts
    """
    clean_ds = str(ds_id).strip().upper()
    validate_supermetrics_api_key(api_key=api_key, client=client)

    if login_id:
        accounts = list_supermetrics_login_accounts(api_key=api_key, login_id=login_id, client=client)
        for a in accounts:
            a["ds_id"] = clean_ds
        return accounts

    # Sequence: List logins for ds_id -> list accounts for each login
    logins = list_supermetrics_logins(api_key=api_key, ds_id=clean_ds, client=client)

    all_accounts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for login in logins:
        lid = login.get("login_id")
        if not lid:
            continue
        try:
            sub_accounts = list_supermetrics_login_accounts(api_key=api_key, login_id=lid, client=client)
            for a in sub_accounts:
                aid = a["account_id"]
                if aid not in seen_ids:
                    seen_ids.add(aid)
                    a["ds_id"] = clean_ds
                    all_accounts.append(a)
        except Exception as exc:
            logger.warning("Failed to fetch accounts for Supermetrics login %s: %s", lid, exc)
            continue

    return all_accounts


def _parse_row_dict(
    row: dict[str, Any],
    clean_ds: str,
    target_accounts: str,
    start_iso: str,
) -> NormalizedSupermetricsRow | None:
    """Parse and normalize a single dictionary row from Supermetrics query response."""
    # Find keys case-insensitively
    lowered = {str(k).lower(): v for k, v in row.items()}

    r_date = str(
        lowered.get("date")
        or lowered.get("report_date")
        or lowered.get("day")
        or start_iso
    ).strip()
    if not DATE_REGEX.match(r_date):
        r_date = start_iso

    r_acct = str(
        lowered.get("account_id")
        or lowered.get("account")
        or lowered.get("ds_account_id")
        or target_accounts
    ).strip()

    r_camp_id = str(
        lowered.get("campaign_id")
        or lowered.get("campaignid")
        or lowered.get("campaign_name")
        or lowered.get("campaign")
        or "all"
    ).strip()

    r_camp_name = str(
        lowered.get("campaign_name")
        or lowered.get("campaignname")
        or lowered.get("campaign")
        or f"Campaign {r_camp_id}"
    ).strip()

    try:
        impressions = int(float(lowered.get("impressions") or lowered.get("views") or 0))
    except (ValueError, TypeError):
        impressions = 0

    try:
        clicks = int(float(lowered.get("clicks") or 0))
    except (ValueError, TypeError):
        clicks = 0

    raw_cost = lowered.get("cost") or lowered.get("spend") or 0
    try:
        cost = Decimal(str(raw_cost))
    except Exception:
        cost = Decimal("0.000000")

    try:
        conversions = float(lowered.get("conversions") or lowered.get("leads") or 0.0)
    except (ValueError, TypeError):
        conversions = 0.0

    raw_conv_val = (
        lowered.get("conversions_value")
        or lowered.get("conversion_value")
        or lowered.get("conversionvalue")
        or 0
    )
    try:
        conversions_value = Decimal(str(raw_conv_val))
    except Exception:
        conversions_value = Decimal("0.000000")

    currency = str(
        lowered.get("currency")
        or lowered.get("currencycode")
        or lowered.get("currency_code")
        or "USD"
    ).strip().upper()

    return NormalizedSupermetricsRow(
        report_date=r_date,
        ds_id=clean_ds,
        ds_account_id=r_acct,
        campaign_id=r_camp_id,
        campaign_name=r_camp_name,
        impressions=impressions,
        clicks=clicks,
        cost=cost,
        conversions=conversions,
        conversions_value=conversions_value,
        currency=currency,
        raw_metrics_json=row,
    )


def query_supermetrics_data(
    api_key: str,
    ds_id: str,
    ds_accounts: str | list[str],
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    fields: list[str] | None = None,
    max_rows: int = 1000,
    client: httpx.Client | None = None,
) -> list[NormalizedSupermetricsRow]:
    """Execute query against Supermetrics Enterprise v2 endpoint and normalize rows.

    POST https://api.supermetrics.com/enterprise/v2/query/data/json
    Supports both dictionary objects and matrix/array response structures.
    """
    clean_key = str(api_key).strip()
    clean_ds = str(ds_id).strip().upper()
    start_iso, end_iso = resolve_date_range(start_date, end_date)

    if isinstance(ds_accounts, list):
        target_accounts: str = ",".join(str(a).strip() for a in ds_accounts if a)
    else:
        target_accounts = str(ds_accounts).strip() or "list.all_accounts"

    query_fields = fields or DEFAULT_FIELDS

    payload = {
        "ds_id": clean_ds,
        "ds_accounts": target_accounts,
        "start_date": start_iso,
        "end_date": end_iso,
        "fields": query_fields,
        "max_rows": max_rows,
    }

    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=45.0)
        close_client = True

    try:
        response = client.post(SUPERMETRICS_QUERY_ENDPOINT, json=payload, headers=headers)

        if response.status_code in (401, 403):
            raise SupermetricsAuthError(
                f"Supermetrics authentication failed ({response.status_code}): Invalid or expired API key."
            )
        if response.status_code == 429:
            raise SupermetricsRateLimitError("Supermetrics rate limit exceeded. Please backoff and retry.")
        if response.status_code >= 500:
            raise RetryableSyncError(
                f"Supermetrics upstream server error ({response.status_code}): {response.text[:200]}"
            )
        if response.status_code != 200:
            err_data = response.json() if "application/json" in response.headers.get("content-type", "") else {}
            err_msg = err_data.get("error", {}).get("message") or response.text[:200]
            if "invalid" in err_msg.lower() or "not found" in err_msg.lower():
                raise PermanentSyncError(f"Supermetrics query rejected ({response.status_code}): {err_msg}")
            raise RetryableSyncError(f"Supermetrics query failed ({response.status_code}): {err_msg}")

        data = response.json()
        raw_rows = data.get("data", [])
        if not isinstance(raw_rows, list):
            raw_rows = []

        # Determine column headers for matrix format if present
        meta = data.get("meta", {})
        columns: list[str] = meta.get("columns") or meta.get("fields") or []

        normalized: list[NormalizedSupermetricsRow] = []

        # Check if first row is a header row in matrix format
        start_index = 0
        if raw_rows and isinstance(raw_rows[0], list):
            if not columns:
                first_row = raw_rows[0]
                if all(isinstance(x, str) for x in first_row):
                    columns = [str(x) for x in first_row]
                    start_index = 1
                else:
                    columns = query_fields

        for idx in range(start_index, len(raw_rows)):
            row_item = raw_rows[idx]
            if isinstance(row_item, dict):
                norm = _parse_row_dict(row_item, clean_ds, target_accounts, start_iso)
                if norm:
                    normalized.append(norm)
            elif isinstance(row_item, list):
                # Map array elements to column headers
                row_dict = {}
                for col_idx, col_name in enumerate(columns):
                    if col_idx < len(row_item):
                        row_dict[col_name] = row_item[col_idx]
                norm = _parse_row_dict(row_dict, clean_ds, target_accounts, start_iso)
                if norm:
                    normalized.append(norm)

        return normalized
    except httpx.RequestError as exc:
        raise RetryableSyncError(f"Network error executing Supermetrics query: {type(exc).__name__}") from exc
    finally:
        if close_client:
            client.close()
