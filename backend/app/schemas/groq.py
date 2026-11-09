from typing import Any
from pydantic import BaseModel, Field


class GroqConfigRequest(BaseModel):
    api_key: str = Field(..., min_length=1, description="Groq API key")
    selected_model: str | None = Field(None, max_length=128)
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(None, ge=1, le=8192)
    max_completion_tokens: int | None = Field(None, ge=1, le=8192)


class GroqConfigResponse(BaseModel):
    status: str
    connection_id: str
    selected_model: str
    has_secret: bool = True
    message: str = "Groq API key validated and securely stored."


class GroqModel(BaseModel):
    id: str
    name: str
    description: str | None = None
    owned_by: str | None = None
    context_window: int = 8192
    active: bool = True


class GroqModelListResponse(BaseModel):
    models: list[GroqModel]


class GroqModelSelectRequest(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=128)


class GroqCompletionRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=10000)
    model: str | None = Field(None, max_length=128)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    max_tokens: int | None = Field(None, ge=1, le=8192)
    max_completion_tokens: int | None = Field(None, ge=1, le=8192)


class GroqCompletionResponse(BaseModel):
    model: str
    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    finish_reason: str


class GroqDisconnectResponse(BaseModel):
    status: str
    disconnected: bool

