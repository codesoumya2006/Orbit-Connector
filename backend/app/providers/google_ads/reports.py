from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging
import re
from typing import Any

import httpx

from app.providers.google_ads.constants import (
    GOOGLE_ADS_API_BASE_URL,
    GOOGLE_ADS_API_VERSION,
)
from app.workers.local_worker import PermanentSyncError, RetryableSyncError

logger = logging.getLogger("orbit.google_ads.reports")
DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class NormalizedCampaignRow:
    report_date: str
    customer_id: str
    campaign_id: str
    campaign_name: str
    campaign_status: str
    impressions: int
    clicks: int
    cost_micros: int  # authoritative integer micros
    cost: Decimal  # derived currency units as exact Decimal
    conversions: float
    conversions_value: float


def cost_micros_to_decimal(cost_micros: int) -> Decimal:
    """Convert integer cost_micros into exact Decimal currency units.

    1 currency unit = 1,000,000 micros.
    Uses exact Decimal division by Decimal("1000000") without rounding or truncation.
    e.g. 1 micro -> Decimal('0.000001')
         123456 micros -> Decimal('0.123456')
    """
    return Decimal(cost_micros) / Decimal("1000000")


def resolve_report_dates(start_date: str, end_date: str) -> tuple[str, str]:
    """Resolve start_date and end_date into validated ISO YYYY-MM-DD strings.

    Supports ISO dates (2025-01-01) and relative strings:
    'today', 'yesterday', '7daysago', '14daysago', '30daysago', '90daysago'.
    """
    now_utc = datetime.now(timezone.utc).date()

    def _resolve(val: str) -> date:
        cleaned = val.strip().lower()
        if DATE_REGEX.match(cleaned):
            return date.fromisoformat(cleaned)
        if cleaned == "today":
            return now_utc
        if cleaned == "yesterday":
            return now_utc - timedelta(days=1)
        if cleaned.endswith("daysago"):
            num_str = cleaned[:-7]
            if num_str.isdigit():
                return now_utc - timedelta(days=int(num_str))
        raise ValueError(
            f"Invalid date format '{val}'. Must be YYYY-MM-DD or relative keyword (e.g. today, yesterday, 30daysAgo)."
        )

    d_start = _resolve(start_date)
    d_end = _resolve(end_date)

    if d_start > d_end:
        raise ValueError(f"start_date '{start_date}' cannot be after end_date '{end_date}'.")

    return d_start.isoformat(), d_end.isoformat()


def build_gaql_campaign_query(
    start_date_iso: str,
    end_date_iso: str,
    campaign_ids: list[str] | None = None,
    limit: int | None = None,
) -> str:
    """Build a compliant GAQL query for campaign performance over a date range against Google Ads API v26.

    Selects segments.date, customer.id, campaign.id, campaign.name, campaign.status,
    metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, metrics.conversions_value.
    """
    query = (
        "SELECT "
        "segments.date, "
        "customer.id, "
        "campaign.id, "
        "campaign.name, "
        "campaign.status, "
        "metrics.impressions, "
        "metrics.clicks, "
        "metrics.cost_micros, "
        "metrics.conversions, "
        "metrics.conversions_value "
        "FROM campaign "
        f"WHERE segments.date BETWEEN '{start_date_iso}' AND '{end_date_iso}'"
    )

    if campaign_ids:
        clean_ids = [str(cid).strip() for cid in campaign_ids if str(cid).strip().isdigit()]
        if clean_ids:
            ids_str = ", ".join(f"'{cid}'" for cid in clean_ids)
            query += f" AND campaign.id IN ({ids_str})"

    query += " ORDER BY segments.date DESC"
    if limit is not None and limit > 0:
        query += f" LIMIT {min(limit, 10000)}"

    return query


def fetch_google_ads_campaign_report(
    access_token: str,
    developer_token: str,
    customer_id: str,
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    login_customer_id: str | None = None,
    campaign_ids: list[str] | None = None,
    client: httpx.Client | None = None,
) -> list[NormalizedCampaignRow]:
    """Execute campaign reporting query via Google Ads REST API v26 search with full pagination.

    Iterates through all pages using nextPageToken.
    Preserves cost_micros as authoritative integer and converts to exact Decimal for derived calculations.
    """
    if not developer_token:
        raise PermanentSyncError("Google Ads Developer Token is not configured.")

    start_iso, end_iso = resolve_report_dates(start_date, end_date)
    query = build_gaql_campaign_query(
        start_date_iso=start_iso,
        end_date_iso=end_iso,
        campaign_ids=campaign_ids,
    )

    clean_cid = str(customer_id).strip()
    if clean_cid.startswith("customers/"):
        clean_cid = clean_cid[len("customers/") :]
    clean_cid = "".join(ch for ch in clean_cid if ch.isdigit())

    url = f"{GOOGLE_ADS_API_BASE_URL}/customers/{clean_cid}/googleAds:search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if login_customer_id:
        clean_login = "".join(ch for ch in str(login_customer_id) if ch.isdigit())
        if clean_login and clean_login != clean_cid:
            headers["login-customer-id"] = clean_login

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30.0)
        close_client = True

    normalized_rows: list[NormalizedCampaignRow] = []
    page_token: str | None = None

    try:
        while True:
            body: dict[str, Any] = {"query": query}
            if page_token:
                body["pageToken"] = page_token

            response = client.post(url, headers=headers, json=body)

            if response.status_code == 200:
                data = response.json()
                results = data.get("results", [])

                for row in results:
                    campaign = row.get("campaign", {})
                    segments = row.get("segments", {})
                    metrics = row.get("metrics", {})
                    cust = row.get("customer", {})

                    raw_cost_micros = metrics.get("costMicros", 0)
                    try:
                        cost_micros = int(raw_cost_micros)
                    except (ValueError, TypeError):
                        cost_micros = 0

                    # Exact financial precision using Decimal without float conversion
                    cost_dec = cost_micros_to_decimal(cost_micros)

                    try:
                        impressions = int(metrics.get("impressions", 0))
                    except (ValueError, TypeError):
                        impressions = 0

                    try:
                        clicks = int(metrics.get("clicks", 0))
                    except (ValueError, TypeError):
                        clicks = 0

                    try:
                        conversions = float(Decimal(str(metrics.get("conversions", 0.0))))
                    except (ValueError, TypeError):
                        conversions = 0.0

                    try:
                        conversions_value = float(Decimal(str(metrics.get("conversionsValue", 0.0))))
                    except (ValueError, TypeError):
                        conversions_value = 0.0

                    campaign_id_val = str(campaign.get("id", "0"))
                    campaign_name_val = campaign.get("name") or f"Campaign {campaign_id_val}"
                    campaign_status_val = str(campaign.get("status", "UNSPECIFIED"))
                    report_date_val = str(segments.get("date", start_iso))
                    row_customer_id = str(cust.get("id") or clean_cid)

                    normalized_rows.append(
                        NormalizedCampaignRow(
                            report_date=report_date_val,
                            customer_id=row_customer_id,
                            campaign_id=campaign_id_val,
                            campaign_name=campaign_name_val[:255],
                            campaign_status=campaign_status_val[:64],
                            impressions=impressions,
                            clicks=clicks,
                            cost_micros=cost_micros,
                            cost=cost_dec,
                            conversions=conversions,
                            conversions_value=conversions_value,
                        )
                    )

                next_page_token = data.get("nextPageToken")
                if next_page_token and next_page_token != page_token:
                    page_token = next_page_token
                else:
                    break

            else:
                # Error Handling & Classification
                err_json = (
                    response.json()
                    if "application/json" in response.headers.get("content-type", "")
                    else {}
                )
                err_detail = err_json.get("error", {})
                err_msg = err_detail.get("message") or response.text[:200]
                status_str = err_detail.get("status", "")

                ads_errors = err_detail.get("details", [])
                error_codes: list[str] = []
                for d in ads_errors:
                    for e in d.get("errors", []):
                        error_code_dict = e.get("errorCode", {})
                        for k, v in error_code_dict.items():
                            error_codes.append(f"{k}.{v}")

                codes_joined = ", ".join(error_codes) if error_codes else ""
                error_desc = f"{status_str}: {err_msg}" if status_str else err_msg
                if codes_joined:
                    error_desc = f"{error_desc} [{codes_joined}]"

                # Classify developer token access-level errors explicitly
                if any(term in error_desc.upper() for term in ("DEVELOPER_TOKEN", "DEVELOPER_TOKEN_NOT_APPROVED", "DEVELOPER_TOKEN_PROHIBITED")):
                    raise PermanentSyncError(
                        f"Google Ads API permanent developer token error ({response.status_code}): {error_desc}"
                    )

                # Permanent client authorization / authentication errors
                if (
                    response.status_code in (400, 401, 403, 404)
                    or status_str in ("UNAUTHENTICATED", "PERMISSION_DENIED", "NOT_FOUND")
                    or any(
                        term in error_desc.upper()
                        for term in (
                            "AUTHENTICATION_ERROR",
                            "AUTHORIZATION_ERROR",
                            "NOT_ADS_USER",
                            "CUSTOMER_NOT_FOUND",
                            "LOGIN_CUSTOMER_ID",
                        )
                    )
                ):
                    raise PermanentSyncError(
                        f"Google Ads API permanent error ({response.status_code}): {error_desc}"
                    )

                # Retryable rate-limits or transient server errors
                if response.status_code in (429, 500, 502, 503, 504) or status_str in ("RESOURCE_EXHAUSTED", "INTERNAL_ERROR"):
                    raise RetryableSyncError(
                        f"Google Ads API transient quota/service error ({response.status_code}): {error_desc}"
                    )

                raise PermanentSyncError(
                    f"Google Ads API request failed ({response.status_code}): {error_desc}"
                )

        return normalized_rows

    except httpx.TimeoutException as exc:
        raise RetryableSyncError(f"Google Ads API request timed out: {exc}") from exc
    except httpx.RequestError as exc:
        raise RetryableSyncError(
            f"Network error querying Google Ads API: {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()
