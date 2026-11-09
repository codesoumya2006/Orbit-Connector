import pytest
from app.connectors.custom_rest import validate_public_https, ConnectorSafetyError, build_auth

def test_rejects_http():
    with pytest.raises(ConnectorSafetyError): validate_public_https("http://example.com")

def test_rejects_localhost():
    with pytest.raises(ConnectorSafetyError): validate_public_https("https://localhost")

def test_bearer_auth():
    assert build_auth("bearer", "abc", None)["Authorization"] == "Bearer abc"
