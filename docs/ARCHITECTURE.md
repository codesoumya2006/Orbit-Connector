# Architecture overview

## Current runnable slice

```text
Next.js dashboard
   │ HTTP / JSON
   ▼
FastAPI (/api/v1)
   ├── Provider catalog ── Google Analytics / Google Ads / Meta Ads / Supermetrics / Groq
   ├── Connection records
   └── Custom REST connector wizard
          ├── HTTPS + public-host validation
          ├── GET endpoint test (redirects disabled)
          └── Fernet-encrypted credential storage
   │
   ▼
PostgreSQL (Docker) / SQLite (local quick run)
```

## Target architecture (next milestones)

Firebase Authentication → FastAPI token verification → organization membership/RBAC → connector registry → OAuth/API credential manager → explicit external-account selection → Cloud Tasks → provider workers → raw GCS archive → validation/normalization → BigQuery → reporting UI.

PostgreSQL remains the source of truth for organizations, users, memberships, integration connections, external identities/accounts, encrypted credential metadata, sync jobs, runs and checkpoints. BigQuery stores analytical facts; GCS stores replayable raw provider payloads. Redis is optional, not the source of truth.

## Extension contract

A provider adapter should implement credential validation, account discovery, paginated/incremental fetch, refresh/re-auth handling and disconnect/revoke. Keep auth adapters separate from data adapters. A customer-defined connector should eventually support a declared schema, pagination mode, cursor extraction, rate limits, retry policy and field mapping; never execute customer-supplied Python or arbitrary code inside the API process.
