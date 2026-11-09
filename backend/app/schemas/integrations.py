from pydantic import BaseModel, Field, HttpUrl
from typing import Literal

class ConnectionCreate(BaseModel):
    organization_id: str | None = None
    provider_key: str
    display_name: str
    connection_type: str = "oauth"

class CustomConnectorCreate(BaseModel):
    organization_id: str | None = None
    name: str = Field(min_length=2, max_length=160)
    base_url: HttpUrl
    endpoint_path: str = "/"
    auth_type: Literal["none", "bearer", "api_key_header", "basic"] = "none"
    secret: str | None = None
    secret_label: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

class CustomConnectorTest(BaseModel):
    base_url: HttpUrl
    endpoint_path: str = "/"
    auth_type: Literal["none", "bearer", "api_key_header", "basic"] = "none"
    secret: str | None = None
    secret_label: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
