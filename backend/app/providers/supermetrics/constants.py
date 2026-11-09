"""Supermetrics API enterprise v2 constants, standard connectors, and endpoints."""

from app.core.config import settings

SUPERMETRICS_API_BASE_URL: str = getattr(
    settings, "supermetrics_api_base_url", "https://api.supermetrics.com/enterprise/v2"
).rstrip("/")

SUPERMETRICS_QUERY_ENDPOINT: str = f"{SUPERMETRICS_API_BASE_URL}/query/data/json"
SUPERMETRICS_METADATA_ENDPOINT: str = f"{SUPERMETRICS_API_BASE_URL}/ds/metadata"
SUPERMETRICS_LOGINS_ENDPOINT: str = f"{SUPERMETRICS_API_BASE_URL}/ds/logins"
SUPERMETRICS_LOGIN_BASE_ENDPOINT: str = f"{SUPERMETRICS_API_BASE_URL}/ds/login"


def get_supermetrics_login_accounts_endpoint(login_id: str) -> str:
    """Return documented endpoint for accounts under a specific login: GET /enterprise/v2/ds/login/{login_id}/accounts."""
    clean_id = str(login_id).strip()
    return f"{SUPERMETRICS_LOGIN_BASE_ENDPOINT}/{clean_id}/accounts"

# Common marketing data sources supported on Supermetrics Enterprise v2
SUPPORTED_DATA_SOURCES: list[dict[str, str]] = [
    {
        "ds_id": "GAWA",
        "name": "Google Analytics 4",
        "category": "Web Analytics",
        "description": "Standard GA4 web and app events, conversions, sessions, and user metrics.",
    },
    {
        "ds_id": "FB",
        "name": "Facebook / Meta Ads",
        "category": "Paid Social",
        "description": "Campaign, ad set, and ad level performance, spend, impressions, and conversions.",
    },
    {
        "ds_id": "AW",
        "name": "Google Ads",
        "category": "Paid Search",
        "description": "Search, display, and video campaigns cost, clicks, impressions, and conversions.",
    },
    {
        "ds_id": "LI",
        "name": "LinkedIn Ads",
        "category": "Paid Social",
        "description": "B2B sponsored content, lead gen, and sponsored messaging analytics.",
    },
    {
        "ds_id": "TIK",
        "name": "TikTok Ads",
        "category": "Paid Social",
        "description": "TikTok for Business advertising performance, spend, and video metrics.",
    },
    {
        "ds_id": "AC",
        "name": "Microsoft Advertising",
        "category": "Paid Search",
        "description": "Bing Network search and audience ads campaign analytics.",
    },
    {
        "ds_id": "AA",
        "name": "Amazon Ads",
        "category": "Ecommerce Advertising",
        "description": "Sponsored products, sponsored brands, and display ad campaign performance.",
    },
]

DEFAULT_FIELDS: list[str] = [
    "date",
    "campaign_id",
    "campaign_name",
    "impressions",
    "clicks",
    "cost",
    "conversions",
    "conversions_value",
    "currency",
]

