"""Safe starter for customer-defined REST API connectors.

Only HTTPS public hosts are accepted. Redirects are disabled to reduce SSRF risk.
For production, also enforce network egress policies and resolve/re-check DNS at connection time.
"""
import ipaddress, socket
from urllib.parse import urlparse, urljoin
import httpx

class ConnectorSafetyError(ValueError): pass

def validate_public_https(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConnectorSafetyError("Custom connectors must use an https:// URL.")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        raise ConnectorSafetyError("Private or local network hosts are not allowed.")
    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ConnectorSafetyError("The host could not be resolved.") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ConnectorSafetyError("Private, loopback, link-local and reserved IPs are not allowed.")
    return parsed

def build_auth(auth_type: str, secret: str | None, label: str | None):
    headers = {}
    auth = None
    if auth_type == "bearer":
        if not secret: raise ValueError("A bearer token is required.")
        headers["Authorization"] = f"Bearer {secret}"
    elif auth_type == "api_key_header":
        if not secret or not label: raise ValueError("API key and header name are required.")
        if label.lower() in {"host", "content-length", "connection", "transfer-encoding"}:
            raise ValueError("That header name is not allowed.")
        headers[label] = secret
    elif auth_type == "basic":
        if not secret or ":" not in secret: raise ValueError("For Basic auth, enter username:password.")
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
    elif auth_type != "none":
        raise ValueError("Unsupported authentication type.")
    return headers

async def test_rest_endpoint(base_url: str, endpoint_path: str, auth_type: str, secret: str | None, secret_label: str | None, custom_headers: dict):
    validate_public_https(base_url)
    target = urljoin(base_url.rstrip("/") + "/", endpoint_path.lstrip("/"))
    parsed_target = validate_public_https(target)
    if parsed_target.hostname != urlparse(base_url).hostname:
        raise ConnectorSafetyError("Endpoint must stay on the configured host.")
    headers = {"Accept": "application/json", **build_auth(auth_type, secret, secret_label)}
    for key, value in custom_headers.items():
        if key.lower() in {"host", "content-length", "connection", "transfer-encoding", "authorization"}:
            raise ValueError(f"Header '{key}' cannot be overridden.")
        headers[key] = value
    timeout = httpx.Timeout(12.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.get(target, headers=headers)
    content_type = response.headers.get("content-type", "")
    preview = response.text[:1200]
    return {"success": response.is_success, "http_status": response.status_code, "content_type": content_type, "preview": preview, "message": "Endpoint responded successfully." if response.is_success else f"Endpoint returned HTTP {response.status_code}."}
