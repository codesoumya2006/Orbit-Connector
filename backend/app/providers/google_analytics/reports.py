from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Any

import httpx

from app.workers.local_worker import PermanentSyncError, RetryableSyncError

DATA_API_BASE_URL = "https://analyticsdata.googleapis.com/v1beta"

DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RELATIVE_DATE_TERMS = {"today", "yesterday", "7daysago", "14daysago", "30daysago", "90daysago"}


@dataclass(frozen=True)
class NormalizedReportRow:
    report_date: str
    channel_group: str
    sessions: int
    active_users: int
    event_count: int


def validate_date_range(start_date: str, end_date: str) -> None:
    """Validate GA4 date range inputs to prevent malformed or invalid queries."""
    s_clean = start_date.strip().lower()
    e_clean = end_date.strip().lower()

    if not s_clean or not e_clean:
        raise ValueError("start_date and end_date must be provided.")

    s_is_iso = bool(DATE_REGEX.match(s_clean))
    e_is_iso = bool(DATE_REGEX.match(e_clean))

    if not s_is_iso and s_clean not in RELATIVE_DATE_TERMS:
        raise ValueError(f"Invalid start_date '{start_date}'. Must be YYYY-MM-DD or standard relative term (e.g. 30daysAgo).")

    if not e_is_iso and e_clean not in RELATIVE_DATE_TERMS:
        raise ValueError(f"Invalid end_date '{end_date}'. Must be YYYY-MM-DD or standard relative term (e.g. today).")

    if s_is_iso and e_is_iso:
        d_start = date.fromisoformat(s_clean)
        d_end = date.fromisoformat(e_clean)
        if d_start > d_end:
            raise ValueError(f"start_date '{start_date}' cannot be after end_date '{end_date}'.")


def normalize_date_string(raw_val: str) -> str:
    """Convert GA4 YYYYMMDD date format into ISO YYYY-MM-DD."""
    cleaned = raw_val.strip()
    if len(cleaned) == 8 and cleaned.isdigit():
        return f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:]}"
    if DATE_REGEX.match(cleaned):
        return cleaned
    return cleaned


def fetch_ga4_report(
    access_token: str,
    property_id: str,
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    limit: int = 10000,
    client: httpx.Client | None = None,
) -> list[NormalizedReportRow]:
    """Execute properties.runReport against the Google Analytics Data API and normalize results."""
    validate_date_range(start_date, end_date)

    clean_prop_id = property_id if property_id.startswith("properties/") else f"properties/{property_id}"
    url = f"{DATA_API_BASE_URL}/{clean_prop_id}:runReport"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    body = {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [
            {"name": "date"},
            {"name": "sessionDefaultChannelGroup"},
        ],
        "metrics": [
            {"name": "sessions"},
            {"name": "activeUsers"},
            {"name": "eventCount"},
        ],
        "limit": min(max(1, limit), 50000),
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    try:
        response = client.post(url, headers=headers, json=body)

        if response.status_code == 200:
            data = response.json()
            rows_data = data.get("rows", [])
            normalized_rows: list[NormalizedReportRow] = []

            for row in rows_data:
                dim_values = row.get("dimensionValues", [])
                metric_values = row.get("metricValues", [])

                raw_date = dim_values[0].get("value", "1970-01-01") if len(dim_values) > 0 else "1970-01-01"
                channel_group = dim_values[1].get("value", "(unassigned)") if len(dim_values) > 1 else "(unassigned)"

                sessions_str = metric_values[0].get("value", "0") if len(metric_values) > 0 else "0"
                active_users_str = metric_values[1].get("value", "0") if len(metric_values) > 1 else "0"
                event_count_str = metric_values[2].get("value", "0") if len(metric_values) > 2 else "0"

                normalized_rows.append(
                    NormalizedReportRow(
                        report_date=normalize_date_string(raw_date),
                        channel_group=channel_group[:128],
                        sessions=int(sessions_str) if sessions_str.isdigit() else 0,
                        active_users=int(active_users_str) if active_users_str.isdigit() else 0,
                        event_count=int(event_count_str) if event_count_str.isdigit() else 0,
                    )
                )

            return normalized_rows

        # Error handling & classification
        err_json = response.json() if "application/json" in response.headers.get("content-type", "") else {}
        err_detail = err_json.get("error", {})
        err_code = err_detail.get("code") or response.status_code
        err_msg = err_detail.get("message") or response.text[:200]
        status_str = err_detail.get("status", "")

        # 401 / 403 permanent authorization / configuration issues
        if response.status_code in (401, 403) or status_str in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
            raise PermanentSyncError(
                f"Google Analytics authentication/permission failure ({response.status_code}): {err_msg}"
            )

        # 429 quota exhaustion or 5xx transient server error
        if response.status_code in (429, 500, 502, 503, 504) or status_str == "RESOURCE_EXHAUSTED":
            raise RetryableSyncError(
                f"Google Analytics transient quota/service error ({response.status_code}): {err_msg}"
            )

        # Other 4xx permanent client errors
        raise PermanentSyncError(
            f"Google Analytics Data API request rejected ({response.status_code}): {err_msg}"
        )

    except httpx.TimeoutException as exc:
        raise RetryableSyncError(f"Google Analytics Data API request timed out: {exc}") from exc
    except httpx.RequestError as exc:
        raise RetryableSyncError(f"Network error querying Google Analytics Data API: {type(exc).__name__}") from exc
    finally:
        if close_client:
            client.close()

