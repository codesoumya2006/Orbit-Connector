# Implementation roadmap

1. **Identity and tenant boundary:** Firebase login, server-side ID token verification, organizations/memberships, RBAC, tenant-scoped repositories and PostgreSQL RLS.
2. **Google Analytics vertical slice:** OAuth state/PKCE, account/property discovery, explicit property selection, encrypted refresh token, worker fetch, checkpoint and dashboard status.
3. **Google Ads and Meta Ads:** provider authorization, developer/business permissions, account discovery, reporting adapters and quota/rate-limit handling.
4. **Custom connector v2:** schema definition, field mapping, cursor/page pagination, test preview, per-tenant quotas, secrets rotation, safe outbound egress proxy.
5. **Supermetrics and Groq:** Supermetrics source ingestion and optional Groq-powered reporting assistant (not a core data connector).
6. **Warehouse and sync:** GCS raw archive, BigQuery staging/analytics, Cloud Tasks, scheduler dispatcher, idempotent worker, retries, backfills and sync history.
7. **Production readiness:** Alembic, KMS, audit logs, observability, backups, tenant isolation tests, E2E tests and deployment IaC.
