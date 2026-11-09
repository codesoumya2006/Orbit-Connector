# Orbit Connector Hub

A production-hardened multi-tenant integration hub and dashboard for connecting analytics, advertising, and custom data sources. Built with **Next.js 15 (React 19)**, **FastAPI (Python 3.12)**, **PostgreSQL**, **Firebase Authentication**, **Alembic**, and **Fernet Credential Encryption**.

## Security & Architecture Highlights

- **Authentication & User Provisioning**: Firebase Google Sign-In with browser-local persistence. FastAPI verifies ID tokens with `check_revoked=True` and provisions/resolves local users.
- **Strict Multi-Tenant Organization Context**: Requests establish organization context via path, `X-Organization-ID` header, or legacy GET query allowlist. Conflicting selectors return HTTP 400. Anti-enumeration returns HTTP 404 for unjoined organizations.
- **RBAC Matrix**: Owner, Admin, and Viewer roles with explicit permission boundaries. Demoting or removing the last organization Owner is strictly forbidden with row-level serialization.
- **Invitation Lifecycles**: 7-day expiration, SHA-256 hashed single-use tokens, caller verified email match requirement.
- **Tenant-Scoped Connectors**: All connections and custom connectors are strictly scoped to the caller's organization. IDOR attempts fail closed with HTTP 404.
- **Encrypted Credential Storage & SSRF Protection**: Credentials encrypted at rest with Fernet. Raw secrets and tokens are redacted from responses and audit logs. Custom REST endpoints enforce HTTPS, public-host DNS resolution, and redirect disabling.
- **Transactional Audit Logging**: Mutations (`organization.created`, `auth.first_observed`, `membership.*`, `invitation.*`, `connection.*`, `custom_connector.*`, `auth.logout`) record server-generated UTC audit events within the same database transaction. Insertion failure triggers automatic rollback.

## 1. Run locally (Docker Desktop)

Requirements: Docker Desktop with Linux containers.

**Windows PowerShell:**

```powershell
Copy-Item .env.example .env
# Set CREDENTIAL_ENCRYPTION_KEY and Firebase client/project variables in .env
docker compose up --build
```

Endpoints:
- Dashboard: http://localhost:3000
- API health: http://localhost:8000/api/v1/health
- OpenAPI docs: http://localhost:8000/docs

## 2. Run without Docker

**Terminal 1 — Backend:**

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DATABASE_URL="sqlite:///./connector_hub.db"
$env:FRONTEND_ORIGIN="http://localhost:3000"
$env:CREDENTIAL_ENCRYPTION_KEY="<generate with cryptography.fernet.Fernet.generate_key()>"
$env:FIREBASE_PROJECT_ID="<your-firebase-project-id>"
py -3.12 -m uvicorn app.main:app --reload --port 8000
```

**Terminal 2 — Frontend:**

```powershell
cd frontend
npm install
$env:NEXT_PUBLIC_API_URL="http://localhost:8000/api/v1"
$env:NEXT_PUBLIC_FIREBASE_API_KEY="<your-api-key>"
$env:NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN="<project>.firebaseapp.com"
$env:NEXT_PUBLIC_FIREBASE_PROJECT_ID="<project-id>"
$env:NEXT_PUBLIC_FIREBASE_APP_ID="<app-id>"
npm run dev
```

## 3. Test Suites

**Backend Tests (65 tests):**

```powershell
cd backend
py -3.12 -m pytest -v
```

**Frontend Tests (23 tests) & Build:**

```powershell
cd frontend
npm test -- --run
npx tsc --noEmit
npm run build
```

## 3. Add a custom connector (implemented)

1. Open **Add a connector → Bring your own API**.
2. Enter a readable name, e.g. `Shopify Analytics`.
3. Enter the API's HTTPS base URL, e.g. `https://api.example.com`, and a GET endpoint path, e.g. `/v1/status`.
4. Choose `No authentication`, `Bearer token`, `API key in header`, or `Basic authentication`.
5. Add optional JSON headers, click **Test connection**, review the HTTP status/response preview, then **Save connector**.
6. The saved custom connector appears in **Your connections**.

The backend requires `CREDENTIAL_ENCRYPTION_KEY` and encrypts the supplied secret before storing it; it refuses to store a credential if the key is missing or invalid. The secret is never returned by the list endpoint. The current custom connector tester supports a GET request and JSON/text response preview. Dataset field mapping, pagination profiles, sync schedules and ingestion are not wired yet.

### Custom connector API

- `GET /api/v1/custom-connectors`
- `POST /api/v1/custom-connectors/test`
- `POST /api/v1/custom-connectors`
- `DELETE /api/v1/custom-connectors/{id}`

Example test body:

```json
{
  "base_url": "https://api.example.com",
  "endpoint_path": "/v1/status",
  "auth_type": "bearer",
  "secret": "YOUR_TOKEN",
  "headers": {"X-Client-Version": "1.0"}
}
```

## 4. Add another first-party provider

1. Add its metadata to `backend/app/connectors/registry.py`.
2. Implement the provider adapter in a new module under `backend/app/connectors/`.
3. Implement provider-specific credential validation, account discovery, pagination, rate-limit handling and incremental fetch methods.
4. Add contract tests under `backend/tests/`.
5. Register the OAuth/API-key flow and account selection in the integration API and UI.
6. Only then change its connection status to `connected` after a verified provider callback/test.

Keep provider-specific API response shapes inside adapters. Normalize them in a separate ingestion/normalization layer rather than coupling the UI to provider payloads.

## 5. Project map

```text
connector-hub/
├── backend/
│   ├── app/api/                 # FastAPI routes
│   ├── app/connectors/           # Provider catalog + custom REST adapter
│   ├── app/core/                 # Settings, DB session, encryption
│   ├── app/models/               # SQLAlchemy entities
│   ├── app/schemas/              # Request validation
│   └── tests/                    # Starter safety tests
├── frontend/
│   ├── app/                      # Next.js app + global styling
│   └── components/               # Dashboard and connection wizard
├── docs/                         # Architecture and security notes
├── docker-compose.yml
└── .env.example
```

## 6. Security and production checklist

- **Do not expose this starter publicly as-is.** The demo workspace ID is fixed and API routes currently do not verify Firebase ID tokens or enforce membership/RBAC. Add Firebase Admin token verification and derive organization scope from the verified session before multi-customer use.
- Add PostgreSQL row-level security and tests proving cross-tenant reads/writes fail. Never trust a client-supplied `organization_id` in production.
- Generate a unique Fernet key for local development. For production, replace the starter encryption helper with Cloud KMS envelope encryption; store key material in Secret Manager, rotate keys, and never log credentials.
- Custom connector URL validation requires HTTPS, blocks non-public DNS results, disallows redirects and pins the endpoint to the configured hostname. Production must additionally enforce outbound network egress controls and defend against DNS rebinding at connection time.
- Add request rate limits, per-tenant quotas, audit events, secret rotation/revocation and credential deletion policies.
- Use Alembic migrations instead of `Base.metadata.create_all` in production.
- Add OAuth state/PKCE, callback validation, encrypted refresh tokens and provider-specific scope tracking before enabling Google/Meta OAuth.
- Add Cloud Tasks + worker idempotency, checkpointing, retries and dead-letter handling before enabling scheduled ingestion.

## 7. Tests

```powershell
cd backend
$env:PYTHONPATH="."
pytest -q
```
