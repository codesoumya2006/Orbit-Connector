# Firebase Authentication and Organization Authorization Design

**Status:** Approved design; implementation and database changes have not started.

## Goals and boundaries

- Add Google Sign-In using Firebase Authentication in the Next.js application.
- Use Firebase UID as the external identity and Firebase ID tokens for protected API requests.
- Enforce organization membership and Owner/Admin/Viewer permissions in FastAPI.
- Preserve existing connector-management behavior and existing organization/connector data.
- Do not add platform-admin privileges or trust a client-supplied organization ID without checking membership.

## Authentication and session flow

The frontend initializes Firebase Authentication with Google as the provider and explicitly selects `browserLocalPersistence`. An auth provider exposes loading and authenticated-user state, protects the dashboard, and supports sign-out. Firebase-managed refresh tokens remain in the browser SDK; the application does not persist ID or refresh tokens itself.

Protected API calls send `Authorization: Bearer <Firebase ID token>`. A shared API client obtains the current token, refreshes it when appropriate, and never retries indefinitely. FastAPI verifies each protected request with Firebase Admin, including signature, issuer, audience/project, expiration, and revocation. Verification failures fail closed. Firebase UID maps to a local user UUID. Firebase/DB unavailability must not cause authentication bypass.

The Firebase UID is not an organization authorization grant. FastAPI resolves the local user and the requested organization's membership and role for each scoped request. Frontend role checks are only presentation behavior.

## Relational schema

```text
users
  id                 UUID PRIMARY KEY
  firebase_uid       UNIQUE NOT NULL, indexed
  email              normalized profile value
  email_verified     BOOLEAN NOT NULL
  display_name       nullable profile value
  created_at         server-generated UTC
  updated_at         server-generated UTC

organizations
  id                 UUID PRIMARY KEY
  legacy_id          nullable UNIQUE value used to trace migrated IDs
  name               NOT NULL
  created_at         server-generated UTC

organization_memberships
  user_id            UUID FK -> users.id
  organization_id    UUID FK -> organizations.id
  role               CHECK IN ('owner', 'admin', 'viewer')
  created_at         server-generated UTC
  PRIMARY KEY (user_id, organization_id)
  indexes on user_id and organization_id

organization_invitations
  id                 UUID PRIMARY KEY
  organization_id    UUID FK -> organizations.id
  created_by_user_id UUID FK -> users.id
  normalized_email   NOT NULL
  role               CHECK IN ('owner', 'admin', 'viewer')
  token_hash         UNIQUE NOT NULL
  expires_at         NOT NULL
  accepted_at        nullable
  revoked_at         nullable
  created_at         server-generated UTC

audit_events
  id                 UUID PRIMARY KEY
  actor_user_id      nullable FK -> users.id
  organization_id    nullable FK -> organizations.id
  event_type         NOT NULL
  target_type        nullable
  target_id          nullable
  request_id         nullable
  auth_time          nullable Firebase authentication timestamp
  metadata           allow-listed, sanitized JSON
  created_at         server-generated UTC

integration_connections.organization_id -> organizations.id
custom_connectors.organization_id       -> organizations.id
```

```text
users 1 --- * organization_memberships * --- 1 organizations
users 1 --- * organization_invitations    * --- 1 organizations
users 1 --- * audit_events                * --- 0..1 organizations
organizations 1 --- * integration_connections
organizations 1 --- * custom_connectors
```

Future account, synchronization, credential, and data tables must carry an organization FK and use the same authorization service. Credential fields remain encrypted as today; secrets are never returned by APIs.

## Permission matrix

All organization permissions are checked in FastAPI against the authenticated user's membership.

| Capability | Owner | Admin | Viewer |
|---|---:|---:|---:|
| Read organization, member list, connector metadata, account/sync status | Yes | Yes | Yes |
| Read invitation records | Yes | Yes | No |
| Create, update, or delete connectors, account configuration, credentials, or sync jobs | Yes | Yes | No |
| Test a custom connector | Yes | Yes | No |
| Invite a Viewer | Yes | Yes | No |
| Invite or promote to Admin or Owner | Yes | No | No |
| Change/remove Viewer membership | Yes | Yes | No |
| Change/remove Admin or Owner membership | Yes, with last-owner protection | No | No |
| Read credentials or submitted secrets | No | No | No |

Any authenticated user may create a new organization; organization creation and the creator's initial Owner membership are one transaction. An existing Viewer cannot elevate their role in an organization they already belong to. Organization deletion and platform-admin capabilities are outside this milestone.

Every owner demotion/removal locks the organization row in a database transaction, counts owners after acquiring the lock, and rejects an operation that would leave zero owners. The same transaction records the required audit event.

## API and organization context

- `GET /api/v1/health` and the global provider catalog may remain public only for non-tenant provider metadata. It must not include connection counts or other organization-derived information.
- Add authenticated `GET /auth/me`, best-effort `POST /auth/logout`, and `GET/POST /organizations`.
- Organization resources use explicit scoped routes such as `/organizations/{organization_id}/connections` and `/organizations/{organization_id}/custom-connectors`.
- Add organization-scoped member and invitation list/create/revoke routes and authenticated `POST /invitations/accept`.
- Remove implicit `demo-org` defaults and all unscoped connector routes; old routes must not remain as authorization bypasses.
- An organization ID in a URL is only a selector. Each request must resolve membership and role before returning or mutating tenant data.
- Unknown or unjoined organizations return a response that does not disclose tenant existence; authenticated members lacking a required role receive 403.
- Connector test operations require Owner/Admin membership, do not persist submitted secrets, and never return them.
- Future account, sync, credential, and data endpoints follow the same scoped-route and membership-check pattern.
- Any organization-specific provider/connection counts are returned only through an authorized organization-scoped endpoint.

### Invitation security

Generate at least 256 bits of cryptographically random token material and store only its SHA-256 hash. Acceptance locks the invitation row and atomically checks that it is unused, unrevoked, unexpired, and addressed to the Firebase-verified normalized email of the authenticated user. Membership and audit rows commit together. The raw invitation URL is sent through a configured email adapter; it is not returned through normal API responses or written to logs.

No email provider is configured in the inspected project. Invitation delivery remains disabled until a provider, sender identity, and credentials are configured.

## Audit policy

Record server-observed authentication (`auth_time` separately from the server-generated UTC event timestamp), organization creation, invitation lifecycle, membership/role changes, and connector lifecycle changes. Mutation and required audit event share one transaction; failure to commit the audit event rolls back the mutation. Audit metadata is allow-listed. Never record authorization headers, ID/refresh tokens, invitation token values, API keys, or credential payloads.

The frontend attempts `POST /auth/logout` before clearing local state, but Firebase sign-out must proceed even if the API is unavailable. Logout audit is therefore best-effort.

## Safe migration and cutover

1. Add Alembic and a baseline matching the current `create_all` schema. Verify an existing database's schema before stamping it; new databases apply the baseline normally.
2. Take and verify a PostgreSQL backup. Inventory organizations and every referencing row before migration.
3. In a transactional migration, create the identity, membership, invitation, and audit tables; assign UUIDs to existing organizations; and remap all organization foreign keys while preserving connector IDs and row contents. Retain `legacy_id='demo-org'` for traceability. Abort on unexpected or missing references rather than dropping or orphaning data.
4. Before protected-route cutover, run a separate operator bootstrap command. It verifies the supplied Firebase UID exists, is enabled, and has a verified email, then creates the local user and Owner membership for the migrated demo organization. The command should support preflight/dry-run and be idempotent.
5. Do not cut over if the owner UID is absent or invalid. Do not assign ownership to the first user automatically. Protected access remains closed until owner membership is verified.
6. Validate row counts, foreign keys, UUID mappings, and owner membership; test restore/rollback before production cutover.

Alembic migrations do not call Firebase or other remote services. The verified initial Owner UID is an operator input required before cutover.

## Implementation sequence

1. Establish Alembic baseline, migration fixtures, and compatibility tests.
2. Add UUID identity/organization/membership/invitation/audit persistence and safe owner bootstrap.
3. Implement Firebase Admin token verification, local user resolution, role dependencies, invitation validation, last-owner locking, and transactional auditing.
4. Scope all existing connector APIs, remove demo defaults/unscoped routes, and test cross-organization denial and secret handling.
5. Add Firebase browser persistence, auth gate, Google Sign-In/out, organization picker/create/invitation flows, and token-aware API client without removing connector features.
6. Wire Docker/environment configuration and document Firebase setup, operator bootstrap, and email delivery configuration.
7. Verify migrations on PostgreSQL and the supported local DB; run backend authorization/invitation/audit/tenant-isolation tests, frontend tests/type-check/build, and Docker build. Live Google sign-in requires a configured Firebase project and is a deployment smoke test.

## Expected file scope

**Frontend:** update `frontend/package.json`, `frontend/app/layout.tsx`, `frontend/app/page.tsx`, `frontend/components/Dashboard.tsx`, `frontend/app/globals.css`, and `frontend/Dockerfile`; add `frontend/lib/firebase.ts`, `frontend/lib/api.ts`, `frontend/contexts/AuthProvider.tsx`, and focused auth/API tests.

**Backend:** update `backend/requirements.txt`, `backend/app/core/config.py`, `backend/app/models/entities.py`, `backend/app/models/__init__.py`, `backend/app/api/routes.py`, `backend/app/main.py`, and existing connector tests; add Firebase auth, authorization, audit, auth/organization routes and schemas, invitation/email and owner-bootstrap modules, Alembic configuration/migrations, and focused backend tests.

**Operations/docs:** update `docker-compose.yml`, `.env.example`, `backend/.env.example`, `README.md`, and `docs/ARCHITECTURE.md`. Do not read, overwrite, or commit the existing `.env`; do not commit Firebase service-account JSON or secret values.

## Assumptions and outstanding production decisions

- Firebase client config is public web-app config. Backend production credentials use Application Default Credentials; no service-account key is committed.
- PostgreSQL is production, with SQLite retained for local development/tests. Firebase or database failures fail closed.
- No scale target or availability SLO was supplied. Initial workload is assumed modest; benchmark revocation checks and indexed membership lookups at deployment load before setting an SLO.
- Before production, supply the verified Firebase UID for initial `demo-org` ownership, configure invitation email delivery, and define audit retention/privacy requirements.

## Decision log

1. Use Firebase ID-token Bearer requests and browser-local Firebase persistence rather than server session cookies.
2. Use Firebase UID as external identity and UUIDs for internal user, organization, and invitation keys.
3. Enforce tenant scope through per-request FastAPI membership and role checks; the client organization selection is not authorization.
4. Use a staged Alembic baseline and transactional UUID/FK remap; require an explicit verified owner bootstrap.
5. Use hashed, single-use, expiring invitations with verified-email matching and configured email delivery.
6. Protect the last Owner with transaction-level organization locking and owner-count validation.
7. Commit each audited mutation and its event atomically; keep secrets out of logs and audit metadata.
8. Make sign-out local and immediate even when best-effort logout auditing cannot reach the API.
