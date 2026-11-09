from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import re
from typing import Any

import httpx

from app.providers.meta_ads.account import clean_ad_account_id, format_act_id
from app.providers.meta_ads.constants import META_GRAPH_BASE_URL
from app.workers.local_worker import PermanentSyncError, RetryableSyncError

logger = logging.getLogger("orbit.meta_ads.insights")
DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Recognized Meta conversion action types for lead, purchase, and registration aggregation
CONVERSION_ACTION_TYPES = {
    "purchase",
    "omni_purchase",
    "lead",
    "omni_complete_registration",
    "complete_registration",
    "subscribe",
    "add_to_cart",
    "omni_add_to_cart",
    "initiate_checkout",
}


@dataclass(frozen=True)
class NormalizedMetaCampaignRow:
    """Normalized campaign-level daily insight row for Meta Ads.

    Metric definitions & sources:
    - report_date: date_start from Meta daily breakdown (ISO YYYY-MM-DD).
    - ad_account_id: Clean numeric Meta ad account ID.
    - campaign_id: Meta campaign ID.
    - campaign_name: Descriptive campaign name.
    - campaign_status: Campaign delivery/configured status.
    - impressions: Total number of times ads were viewed (Meta 'impressions').
    - reach: Number of unique people who saw ads at least once (Meta 'reach').
    - clicks: Number of clicks on ads (Meta 'clicks').
    - spend: Total estimated amount spent in major currency units as exact Decimal (Meta 'spend').
    - ctr: Click-through rate percentage (Meta 'ctr').
    - cpc: Cost per click in currency units (Meta 'cpc').
    - cpm: Cost per 1,000 impressions in currency units (Meta 'cpm').
    - frequency: Average number of times each person saw the ad (Meta 'frequency').
    - conversions: Total number of conversion actions (Meta 'actions').
    - conversions_value: Total value of conversion actions in currency units (Meta 'action_values').
    """

    report_date: str
    ad_account_id: str
    campaign_id: str
    campaign_name: str
    campaign_status: str
    impressions: int
    reach: int
    clicks: int
    spend: Decimal
    ctr: float
    cpc: Decimal
    cpm: Decimal
    frequency: float
    conversions: float
    conversions_value: Decimal


def resolve_report_dates(start_date: str, end_date: str) -> tuple[str, str]:
    """Resolve start_date and end_date into validated ISO YYYY-MM-DD strings.

    Supports ISO dates (2025-01-01) and relative keywords:
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


def parse_action_metrics(
    actions: list[dict[str, Any]] | None,
    action_values: list[dict[str, Any]] | None,
) -> tuple[float, Decimal]:
    """Parse Meta's structured actions and action_values arrays into total conversions and conversion value.

    Meta returns actions as: [{'action_type': 'purchase', 'value': '5'}, {'action_type': 'lead', 'value': '2'}]
    and action_values as: [{'action_type': 'purchase', 'value': '150.00'}]
    """
    total_conversions = 0.0
    total_value = Decimal("0.000000")

    if actions and isinstance(actions, list):
        # Prioritize key conversion types; if none match, aggregate all action counts
        has_specific_conversions = any(
            item.get("action_type") in CONVERSION_ACTION_TYPES for item in actions if isinstance(item, dict)
        )
        for item in actions:
            if not isinstance(item, dict):
                continue
            act_type = str(item.get("action_type", ""))
            try:
                val = float(item.get("value", 0.0))
            except (ValueError, TypeError):
                val = 0.0

            if has_specific_conversions:
                if act_type in CONVERSION_ACTION_TYPES:
                    total_conversions += val
            else:
                total_conversions += val

    if action_values and isinstance(action_values, list):
        for item in action_values:
            if not isinstance(item, dict):
                continue
            try:
                raw_val = item.get("value", 0)
                dec_val = Decimal(str(raw_val))
            except Exception:
                dec_val = Decimal("0.000000")
            total_value += dec_val

    return total_conversions, total_value


def fetch_meta_ads_campaign_insights(
    access_token: str,
    account_id: str | None = None,
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_ids: list[str] | None = None,
    client: httpx.Client | None = None,
    *,
    ad_account_id: str | None = None,
) -> list[NormalizedMetaCampaignRow]:
    """Fetch daily campaign-level reporting from Meta Insights API.

    GET https://graph.facebook.com/{version}/act_{account_id}/insights
    Supports cursor-based pagination and explicit rate limit / permanent error classification.
    """
    effective_account_id = account_id or ad_account_id
    if not effective_account_id:
        raise MetaAdsAccountError("Account ID must be provided.")
    clean_id = clean_ad_account_id(effective_account_id)
    act_id = format_act_id(clean_id)
    start_iso, end_iso = resolve_report_dates(start_date, end_date)

    url = f"{META_GRAPH_BASE_URL}/{act_id}/insights"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    params: dict[str, Any] = {
        "level": "campaign",
        "time_increment": "1",  # Daily granularity
        "time_range": json.dumps({"since": start_iso, "until": end_iso}),
        "fields": (
            "date_start,date_stop,account_id,campaign_id,campaign_name,"
            "objective,impressions,reach,clicks,spend,ctr,cpc,cpm,frequency,"
            "actions,action_values"
        ),
        "limit": 100,
    }

    if campaign_ids:
        params["filtering"] = json.dumps([
            {"field": "campaign.id", "operator": "IN", "value": campaign_ids}
        ])

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30.0)
        close_client = True

    normalized_rows: list[NormalizedMetaCampaignRow] = []

    try:
        next_url: str | None = url
        current_params: dict[str, Any] | None = params

        while next_url:
            response = client.get(next_url, headers=headers, params=current_params)

            if response.status_code == 200:
                data = response.json()
                rows = data.get("data", [])

                for r in rows:
                    report_date_val = str(r.get("date_start", start_iso))
                    campaign_id_val = str(r.get("campaign_id", "0"))
                    campaign_name_val = str(r.get("campaign_name") or f"Campaign {campaign_id_val}")
                    # Objective or status fallback
                    status_val = str(r.get("objective") or "ACTIVE")

                    try:
                        impressions = int(r.get("impressions", 0))
                    except (ValueError, TypeError):
                        impressions = 0

                    try:
                        reach = int(r.get("reach", 0))
                    except (ValueError, TypeError):
                        reach = 0

                    try:
                        clicks = int(r.get("clicks", 0))
                    except (ValueError, TypeError):
                        clicks = 0

                    # Exact Decimal financial precision for spend
                    raw_spend = r.get("spend", "0")
                    try:
                        spend = Decimal(str(raw_spend))
                    except Exception:
                        spend = Decimal("0.000000")

                    try:
                        ctr = float(r.get("ctr", 0.0))
                    except (ValueError, TypeError):
                        ctr = 0.0

                    try:
                        cpc = Decimal(str(r.get("cpc", "0.0")))
                    except Exception:
                        cpc = Decimal("0.000000")

                    try:
                        cpm = Decimal(str(r.get("cpm", "0.0")))
                    except Exception:
                        cpm = Decimal("0.000000")

                    try:
                        frequency = float(r.get("frequency", 0.0))
                    except (ValueError, TypeError):
                        frequency = 0.0

                    conversions, conversions_val = parse_action_metrics(
                        r.get("actions"), r.get("action_values")
                    )

                    normalized_rows.append(
                        NormalizedMetaCampaignRow(
                            report_date=report_date_val,
                            ad_account_id=clean_id,
                            campaign_id=campaign_id_val,
                            campaign_name=campaign_name_val[:255],
                            campaign_status=status_val[:64],
                            impressions=impressions,
                            reach=reach,
                            clicks=clicks,
                            spend=spend,
                            ctr=ctr,
                            cpc=cpc,
                            cpm=cpm,
                            frequency=frequency,
                            conversions=conversions,
                            conversions_value=conversions_val,
                        )
                    )

                # Cursor-based pagination loop via paging.next
                paging = data.get("paging", {})
                next_link = paging.get("next")

                if next_link and next_link != next_url:
                    next_url = next_link
                    current_params = None
                else:
                    break

            else:
                # Error classification
                err_json = (
                    response.json()
                    if "application/json" in response.headers.get("content-type", "")
                    else {}
                )
                err_detail = err_json.get("error", {})
                err_msg = err_detail.get("message") or response.text[:200]
                code = err_detail.get("code")
                subcode = err_detail.get("error_subcode")
                err_type = err_detail.get("type", "")

                # 1. Rate limits / User / App request throttling
                # Meta codes 17 (User request limit), 32 (Page limit), 613 (Calls limit), 80004 (Throttled)
                if code in (17, 32, 613, 80004) or response.status_code == 429:
                    raise RetryableSyncError(
                        f"Meta Ads rate limit reached ({code}): {err_msg}"
                    )

                # 2. Transient server errors
                if response.status_code in (500, 502, 503, 504) or code in (1, 2):
                    raise RetryableSyncError(
                        f"Meta Ads transient server error ({response.status_code}): {err_msg}"
                    )

                # 3. Permanent OAuth authorization / permission errors
                # Code 190 (Invalid/Expired OAuth token), subcode 463 (Expired), 467 (Invalid)
                if code == 190 or err_type == "OAuthException" or response.status_code in (401, 403):
                    raise PermanentSyncError(
                        f"Meta Ads permanent authorization failure (code {code}, subcode {subcode}): {err_msg}"
                    )

                raise PermanentSyncError(
                    f"Meta Ads Insights request failed ({response.status_code}): {err_msg}"
                )

        return normalized_rows

    except httpx.TimeoutException as exc:
        raise RetryableSyncError(f"Meta Ads API request timed out: {exc}") from exc
    except httpx.RequestError as exc:
        raise RetryableSyncError(
            f"Network error querying Meta Ads API: {type(exc).__name__}"
        ) from exc
    finally:
        if close_client:
            client.close()

