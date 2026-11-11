from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"

class Settings(BaseSettings):
    app_name: str = "Connector Hub API"
    environment: str = "local"
    database_url: str = "sqlite:///./connector_hub.db"
    frontend_origin: str = "http://localhost:3000"
    credential_encryption_key: str = ""
    firebase_project_id: str = ""
    firebase_credentials_path: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/oauth/google/callback"
    google_ads_developer_token: str = ""
    google_ads_redirect_uri: str = "http://localhost:8000/api/v1/oauth/google-ads/callback"
    google_ads_api_version: str = "v26"
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_redirect_uri: str = "http://localhost:8000/api/v1/oauth/meta-ads/callback"
    meta_ads_api_version: str = "v27.0"
    supermetrics_api_base_url: str = "https://api.supermetrics.com/enterprise/v2"
    groq_api_base_url: str = "https://api.groq.com/openai/v1"
    groq_default_model: str = "llama-3.3-70b-versatile"
    bigquery_project_id: str = ""
    gcs_bucket: str = ""
    model_config = SettingsConfigDict(env_file=(str(ROOT_ENV_PATH), ".env"), extra="ignore")

settings = Settings()
