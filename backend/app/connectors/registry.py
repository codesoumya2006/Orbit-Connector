from dataclasses import dataclass

@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    category: str
    description: str
    auth_type: str
    setup_state: str = "ready"

PROVIDERS = [
    Provider("google_analytics", "Google Analytics 4", "Analytics", "Website traffic, events and conversions.", "OAuth 2.0"),
    Provider("google_ads", "Google Ads", "Advertising", "Campaign, cost and conversion reporting.", "OAuth 2.0 + developer token"),
    Provider("meta_ads", "Meta Ads", "Advertising", "Facebook and Instagram campaign insights.", "Meta OAuth"),
    Provider("supermetrics", "Supermetrics", "Data pipeline", "Bring supported marketing sources through Supermetrics.", "API credentials"),
    Provider("groq", "Groq", "AI service", "Optional LLM service for future reporting assistants.", "API key"),
]

def list_providers():
    return [p.__dict__ for p in PROVIDERS]

def get_provider(key: str):
    return next((p for p in PROVIDERS if p.key == key), None)
