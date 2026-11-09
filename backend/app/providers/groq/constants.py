"""Groq official OpenAI-compatible API constants, models, and endpoints."""

from typing import Any

from app.core.config import settings

GROQ_API_BASE_URL: str = getattr(
    settings, "groq_api_base_url", "https://api.groq.com/openai/v1"
).rstrip("/")

GROQ_MODELS_ENDPOINT: str = f"{GROQ_API_BASE_URL}/models"
GROQ_CHAT_COMPLETIONS_ENDPOINT: str = f"{GROQ_API_BASE_URL}/chat/completions"

DEFAULT_GROQ_MODEL: str = getattr(
    settings, "groq_default_model", "llama-3.3-70b-versatile"
)

CURATED_GROQ_MODELS: list[dict[str, Any]] = [
    {
        "id": "llama-3.3-70b-versatile",
        "name": "Llama 3.3 70B Versatile",
        "description": "High-intelligence flagship model with 128k context.",
        "context_window": 131072,
    },
    {
        "id": "llama-3.1-8b-instant",
        "name": "Llama 3.1 8B Instant",
        "description": "Ultra-fast lightweight model for low latency applications.",
        "context_window": 131072,
    },
    {
        "id": "llama-3.1-70b-versatile",
        "name": "Llama 3.1 70B Versatile",
        "description": "Powerful instruction-tuned versatile model.",
        "context_window": 131072,
    },
    {
        "id": "mixtral-8x7b-32768",
        "name": "Mixtral 8x7B 32k",
        "description": "MoE architecture model with 32k context.",
        "context_window": 32768,
    },
    {
        "id": "gemma2-9b-it",
        "name": "Gemma 2 9B IT",
        "description": "Google's instruction-tuned Gemma model on Groq LPU.",
        "context_window": 8192,
    },
]
