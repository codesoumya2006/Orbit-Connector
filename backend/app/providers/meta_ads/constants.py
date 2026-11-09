from app.core.config import settings

META_ADS_API_VERSION = settings.meta_ads_api_version
META_GRAPH_BASE_URL = f"https://graph.facebook.com/{META_ADS_API_VERSION}"
META_OAUTH_DIALOG_URL = f"https://www.facebook.com/{META_ADS_API_VERSION}/dialog/oauth"
META_OAUTH_TOKEN_URL = f"https://graph.facebook.com/{META_ADS_API_VERSION}/oauth/access_token"
META_ADS_SCOPE = "ads_read"

