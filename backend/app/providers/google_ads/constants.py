from app.core.config import settings

# Centralized Google Ads API version and endpoint URLs
GOOGLE_ADS_API_VERSION = settings.google_ads_api_version
GOOGLE_ADS_API_BASE_URL = f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}"
GOOGLE_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"

