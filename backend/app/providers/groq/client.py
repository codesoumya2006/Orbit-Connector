"""Groq official API client for model discovery and inference completion."""

from datetime import datetime, timezone
import logging
from typing import Any

import httpx

from app.providers.groq.constants import (
    CURATED_GROQ_MODELS,
    DEFAULT_GROQ_MODEL,
    GROQ_CHAT_COMPLETIONS_ENDPOINT,
    GROQ_MODELS_ENDPOINT,
)

logger = logging.getLogger("orbit.groq.client")


class GroqError(Exception):
    """Base exception for Groq API errors."""


class GroqAuthError(GroqError):
    """Raised for invalid or unauthorized Groq API keys (HTTP 401/403)."""


class GroqRateLimitError(GroqError):
    """Raised for Groq rate limits or quota exhaustion (HTTP 429)."""


class GroqModelError(GroqError):
    """Raised when the requested Groq model is invalid or unavailable."""


class GroqRequestError(GroqError):
    """Raised for network or server errors during Groq requests."""


def validate_groq_api_key(
    api_key: str,
    client: httpx.Client | None = None,
) -> list[str]:
    """Validate Groq API key by querying the official models endpoint.

    GET https://api.groq.com/openai/v1/models
    """
    clean_key = str(api_key).strip()
    if not clean_key:
        raise GroqAuthError("Groq API key cannot be empty.")

    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Accept": "application/json",
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    try:
        response = client.get(GROQ_MODELS_ENDPOINT, headers=headers)
        if response.status_code in (401, 403):
            raise GroqAuthError("Invalid or unauthorized Groq API key.")
        if response.status_code == 429:
            raise GroqRateLimitError("Groq API rate limit or quota exceeded.")
        if response.status_code >= 500:
            raise GroqRequestError(f"Groq service unavailable ({response.status_code}).")
        if response.status_code != 200:
            raise GroqRequestError(f"Groq API returned unexpected status {response.status_code}: {response.text[:200]}")

        data = response.json()
        models_data = data.get("data", [])
        model_ids = [m["id"] for m in models_data if isinstance(m, dict) and "id" in m]
        return model_ids
    except httpx.RequestError as exc:
        raise GroqRequestError(f"Network error validating Groq credentials: {type(exc).__name__}") from exc
    finally:
        if close_client:
            client.close()


def list_groq_models(
    api_key: str,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Retrieve all available models from the Groq API endpoint."""
    clean_key = str(api_key).strip()
    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Accept": "application/json",
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True

    try:
        response = client.get(GROQ_MODELS_ENDPOINT, headers=headers)
        if response.status_code in (401, 403):
            raise GroqAuthError("Unauthorized Groq API key.")
        if response.status_code == 429:
            raise GroqRateLimitError("Groq rate limit exceeded.")
        if response.status_code != 200:
            raise GroqRequestError(f"Failed to fetch Groq models ({response.status_code}).")

        data = response.json()
        raw_models = data.get("data", [])
        models: list[dict[str, Any]] = []

        curated_map = {m["id"]: m for m in CURATED_GROQ_MODELS}

        for m in raw_models:
            if not isinstance(m, dict) or "id" not in m:
                continue
            mid = m["id"]
            meta = curated_map.get(mid, {})
            models.append({
                "id": mid,
                "name": meta.get("name") or mid,
                "description": meta.get("description") or f"Groq LPU model {mid}",
                "owned_by": m.get("owned_by", "groq"),
                "context_window": meta.get("context_window", m.get("context_window", 8192)),
                "active": bool(m.get("active", True)),
            })

        # Ensure default model is present
        if not any(m["id"] == DEFAULT_GROQ_MODEL for m in models):
            models.insert(0, {
                "id": DEFAULT_GROQ_MODEL,
                "name": "Llama 3.3 70B Versatile",
                "description": "High-intelligence flagship model with 128k context.",
                "owned_by": "meta",
                "context_window": 131072,
                "active": True,
            })

        return models
    except httpx.RequestError as exc:
        raise GroqRequestError(f"Network error listing Groq models: {exc}") from exc
    finally:
        if close_client:
            client.close()


def execute_groq_completion(
    api_key: str,
    prompt: str,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    max_completion_tokens: int | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Execute a chat completion request against Groq LPU API.

    POST https://api.groq.com/openai/v1/chat/completions
    Uses max_completion_tokens as preferred by modern Groq and OpenAI specifications.
    """
    clean_key = str(api_key).strip()
    target_model = model or DEFAULT_GROQ_MODEL

    if not prompt or not prompt.strip():
        raise GroqRequestError("Prompt cannot be empty.")

    token_val = max_completion_tokens if max_completion_tokens is not None else (max_tokens if max_tokens is not None else 256)
    bounded_tokens = max(1, min(8192, int(token_val)))

    payload: dict[str, Any] = {
        "model": target_model,
        "messages": [
            {"role": "user", "content": prompt.strip()},
        ],
        "temperature": max(0.0, min(2.0, float(temperature))),
        "max_completion_tokens": bounded_tokens,
    }

    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30.0)
        close_client = True

    try:
        response = client.post(GROQ_CHAT_COMPLETIONS_ENDPOINT, json=payload, headers=headers)
        if response.status_code in (401, 403):
            raise GroqAuthError("Unauthorized Groq API key.")
        if response.status_code == 429:
            raise GroqRateLimitError("Groq rate limit exceeded.")
        if response.status_code == 404:
            raise GroqModelError(f"Model '{target_model}' not found on Groq API.")
        if response.status_code != 200:
            err_data = response.json() if "application/json" in response.headers.get("content-type", "") else {}
            err_msg = err_data.get("error", {}).get("message") or response.text[:200]
            raise GroqRequestError(f"Groq completion failed ({response.status_code}): {err_msg}")

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise GroqRequestError("Groq API returned empty completion choices.")

        first_choice = choices[0]
        content = first_choice.get("message", {}).get("content", "")
        finish_reason = first_choice.get("finish_reason", "stop")

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

        return {
            "model": data.get("model", target_model),
            "content": content,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "finish_reason": finish_reason,
        }
    except httpx.RequestError as exc:
        raise GroqRequestError(f"Network error executing Groq completion: {exc}") from exc
    finally:
        if close_client:
            client.close()

