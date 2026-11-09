# Firebase Authentication Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Firebase Google authentication, secure multi-organization authorization, invitations, audit logging, and tenant-scoped connector APIs without losing existing connector data or workflows.

**Architecture:** Next.js uses Firebase Auth with Google and browser-local persistence, sending Firebase ID tokens to FastAPI as Bearer tokens. FastAPI verifies tokens with Firebase Admin and enforces UUID-based user, organization, membership, and role records in PostgreSQL. Alembic migrations preserve and remap existing organization data; every protected tenant operation is membership-scoped and required audit writes are transactional.

**Tech Stack:** Next.js 15, React 19, TypeScript, Firebase JS SDK, FastAPI, firebase-admin, SQLAlchemy 2, Alembic, PostgreSQL, pytest, frontend test runner.

---

## Plan constraints and prerequisites

- Follow `docs/FIREBASE_AUTH_DESIGN.md`, which the user approved.
- Preserve `backend/app/connectors/custom_rest.py` safety behavior and current connector management UX.
- Never read or overwrite `.env`; do not commit Firebase service-account JSON, tokens, credentials, or other secret values.
- Production cutover requires an operator-supplied Firebase UID whose account is enabled and email verified. No automatic first-user Owner assignment.
- Invitation creation requires a configured email sender. Do not expose invitation tokens through normal API responses or logs.
- Run existing tests before and after the affected changes. Existing workspace has no Git repository; do not make commits.
- No production migration may run until a backup is verified and all organization foreign-key references are inventoried.

## Task 1: Add focused test harnesses and capture baseline

**Files:**
- Modify: `frontend/package.json`
- Create: `frontend/tests/auth.test.tsx`
- Create: `backend/tests/test_firebase_auth.py`
- Read: `backend/tests/test_custom_connector.py`

**Step 1: Capture current backend baseline**

Run from `backend`:

```powershell
$env:PYTHONPATH="."
pytest -q
```

Expected: all existing custom connector tests pass.

**Step 2: Add frontend test tooling and minimal failing auth tests**

Add a maintained test runner and DOM utilities as dev dependencies. Add tests for auth loading, signed-out Google sign-in affordance, and signed-in sign-out affordance. Mock Firebase SDK boundaries rather than using real project credentials.

**Step 3: Run frontend tests**

Run from `frontend`:

```powershell
npm test -- --run
```

Expected: the new tests fail because the auth provider and UI are not implemented.

## Task 2: Establish Alembic and preserve the current schema

**Files:**
- Modify: `backend/requirements.txt`
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/script.py.mako`
- Create: `backend/alembic/versions/0001_existing_schema_baseline.py`
- Create: `backend/tests/test_migrations.py`

**Step 1: Write a baseline migration test**

Create a temporary database, apply the baseline, and assert that organizations, integration connections, and custom connectors exist with the current columns and foreign keys.

**Step 2: Run the targeted migration test**

Run from `backend`:

```powershell
$env:PYTHONPATH="."
pytest tests\test_migrations.py -q
```

Expected: fail until Alembic configuration and baseline exist.

**Step 3: Add Alembic and baseline current schema**

Support the configured PostgreSQL deployment and the existing SQLite local mode. Existing deployments must be schema-checked before stamping the baseline; never run `create_all` as a substitute for production migration history.

**Step 4: Re-run migration test**

Run the same command. Expected: baseline creates the current schema and the test passes.

## Task 3: Add UUID identity, memberships, invitations, and audit schema

**Files:**
- Modify: `backend/app/models/entities.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/0002_firebase_tenants_and_audit.py`
- Modify: `backend/tests/test_migrations.py`

**Step 1: Write migration preservation tests**

Seed a legacy `demo-org` organization and connector/custom-connector rows. Assert upgrade preserves row IDs and payloads, remaps every organization FK to UUID, stores `demo-org` as a legacy identifier, and creates the new tables/indexes/constraints.

**Step 2: Run the focused migration test**

Run from `backend`:

```powershell
$env:PYTHONPATH="."
pytest tests\test_migrations.py -q
```

Expected: the legacy remapping tests fail before the second migration exists.

**Step 3: Implement typed SQLAlchemy models and migration**

Use UUID primary keys for users, organizations, invitations; a unique indexed Firebase UID; composite membership primary key `(user_id, organization_id)`; a role check constraint; hashed invitation token uniqueness; and audit records with server-generated UTC event time plus separate Firebase `auth_time`.

**Step 4: Re-run the migration tests**

Expected: migration and data-preservation tests pass for each supported test database.

## Task 4: Implement Firebase verification and local identity resolution

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/app/core/config.py`
- Create: `backend/app/core/firebase_auth.py`
- Create: `backend/app/api/auth.py`
- Create: `backend/app/schemas/auth.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_firebase_auth.py`

**Step 1: Add failing dependency tests**

Cover missing/malformed Bearer token (401), invalid/expired/revoked token (401), successful UID resolution, Firebase/DB outage fail-closed behavior, and auth-time capture. Mock Firebase Admin; do not use `.env` values.

**Step 2: Run targeted auth tests**

```powershell
$env:PYTHONPATH="."
pytest tests\test_firebase_auth.py -q
```

Expected: failures until the Firebase dependency and auth routes are registered.

**Step 3: Implement Firebase Admin initialization and principal dependency**

Initialize once per process using project config and Application Default Credentials in production. Verify ID tokens including revocation on every protected request. Resolve or safely provision the local UUID user from verified Firebase claims. Return generic auth errors without logging raw tokens.

**Step 4: Re-run targeted auth tests**

Expected: all token and fail-closed tests pass.

## Task 5: Implement organizations, role authorization, invitations, and audits

**Files:**
- Create: `backend/app/core/authorization.py`
- Create: `backend/app/core/audit.py`
- Create: `backend/app/api/organizations.py`
- Create: `backend/app/schemas/organizations.py`
- Create: `backend/app/services/invitations.py`
- Create: `backend/app/services/email.py`
- Create: `backend/app/cli.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_authorization.py`
- Create: `backend/tests/test_organizations.py`
- Create: `backend/tests/test_invitations.py`
- Create: `backend/tests/test_audit.py`

**Step 1: Write failing permission and transaction tests**

Test Owner/Admin/Viewer permissions, new organization plus initial Owner atomicity, cross-organization denial, invitation email verification/single-use/expiry, audit rollback, and concurrent last-Owner removal/demotion.

**Step 2: Run the focused tests**

```powershell
$env:PYTHONPATH="."
pytest tests\test_authorization.py tests\test_organizations.py tests\test_invitations.py tests\test_audit.py -q
```

Expected: tests fail until policies and services are implemented.

**Step 3: Implement explicit authorization and audited services**

Enforce Owner/Admin/Viewer permissions server-side. Serialize owner changes by locking the organization row and recounting owners inside the transaction. Hash at least 256 bits of random invitation token material; accept only for the authenticated Firebase-verified matching email. Require email sender configuration before creating an invitation. Keep raw tokens and secrets out of responses/logs.

**Step 4: Add idempotent, explicit owner bootstrap**

Implement preflight/dry-run and bootstrap command. Verify the Firebase account exists, is enabled, and has verified email before assigning initial ownership. Do not assign automatically or run Firebase network calls inside Alembic.

**Step 5: Re-run focused tests**

Expected: all permissions, invitation, transaction, audit, and last-owner tests pass.

## Task 6: Scope every existing connector API

**Files:**
- Modify: `backend/app/api/routes.py`
- Modify: `backend/app/schemas/integrations.py`
- Modify: `backend/tests/test_custom_connector.py`
- Create: `backend/tests/test_connector_authorization.py`

**Step 1: Write failing route authorization tests**

Assert every list/create/delete/test route rejects unauthenticated calls, rejects non-member organization access, and enforces role permissions. Assert custom connector test secrets are neither persisted nor returned and mutations roll back if audit insertion fails.
Assert the public provider catalog contains no organization-derived connection counts.

**Step 2: Run route tests**

```powershell
$env:PYTHONPATH="."
pytest tests\test_connector_authorization.py tests\test_custom_connector.py -q
```

Expected: failures because current routes use `demo-org` defaults and are unprotected.

**Step 3: Move tenant routes under explicit organization context**

Remove implicit organization defaults and unscoped bypasses. Scope all existing connection and custom connector routes by organization, use the shared membership/role dependency, and audit connector lifecycle writes in the same DB transaction. Preserve encryption and outbound URL-safety behavior.
Keep `/providers` public only for static catalog metadata; remove its current globally aggregated `connection_count` field.

**Step 4: Re-run connector tests**

Expected: all authorization, tenant-isolation, secret-handling, and existing connector safety tests pass.

## Task 7: Add Firebase client auth and organization-aware dashboard

**Files:**
- Modify: `frontend/package.json`
- Create: `frontend/lib/firebase.ts`
- Create: `frontend/lib/api.ts`
- Create: `frontend/contexts/AuthProvider.tsx`
- Modify: `frontend/app/layout.tsx`
- Modify: `frontend/app/page.tsx`
- Modify: `frontend/components/Dashboard.tsx`
- Modify: `frontend/app/globals.css`
- Modify: `frontend/tests/auth.test.tsx`
- Create: `frontend/tests/api.test.ts`

**Step 1: Write failing frontend tests**

Test persistent signed-in state, Google sign-in, immediate local sign-out when logout audit API fails, organization selection for zero/one/multiple memberships, and token attachment/one-time refresh on 401.

**Step 2: Run frontend tests**

```powershell
npm test -- --run
```

Expected: new behavior tests fail until Firebase/provider/API/UI integration is implemented.

**Step 3: Implement Firebase and scoped API client**

Set `browserLocalPersistence`, keep Firebase SDK errors visible, attach the current ID token to protected requests, refresh at most once, and clear stale organization selection after membership changes. Keep role presentation subordinate to backend authorization.

**Step 4: Integrate dashboard without removing connector features**

Add signed-out and loading states, Google Sign-In, sign-out, organization selection/create/join UI, and role-aware controls. Keep the existing connector management flows and surface API errors.

**Step 5: Re-run frontend tests**

Expected: all auth, organization context, token refresh, and sign-out behavior tests pass.

## Task 8: Configure Docker, environment templates, and documentation

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `backend/.env.example`
- Modify: `frontend/Dockerfile`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`

**Step 1: Add only configuration names/placeholders**

Document public Firebase web-app config, backend Firebase project/credential strategy, and email sender configuration. Add appropriate Next.js build args. Never copy or print `.env` values.

**Step 2: Document operator procedures**

Include Firebase Google provider/authorized-domain setup, ADC/service identity, Alembic backup/preflight/upgrade, verified owner bootstrap, email setup, rollback, and local/test setup.

## Task 9: Run final regression and deployment checks

**Files:**
- All changed implementation and test files

**Step 1: Run backend suite**

```powershell
$env:PYTHONPATH="."
pytest -q
```

Expected: all auth, migration, authorization, audit, invitation, tenant-isolation, and pre-existing connector tests pass.

**Step 2: Run frontend tests and type/build checks**

```powershell
npm test -- --run
npx tsc --noEmit
npm run build
```

Expected: all tests pass, TypeScript emits no diagnostics, and Next.js production build succeeds.

**Step 3: Build Docker images**

Run from the project root:

```powershell
docker compose build
```

Expected: API and frontend images build with configuration supplied only through environment/build arguments.

**Step 4: Report deployment-only gates**

Do not claim live Google sign-in without a configured Firebase project. List the required verified owner UID, email provider, Firebase authorized domain, and audit retention/privacy decision before production cutover.
