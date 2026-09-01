# KCMS Backend

Khmer Comment Moderation System — API, classification and persistence.

**Live:** https://kcms-backend.onrender.com · **Health:** [`/api/v1/health`](https://kcms-backend.onrender.com/api/v1/health)

FastAPI modular monolith on Python 3.12. See
[`../kcms-planning/adr/0002-backend-runtime.md`](../kcms-planning/adr/0002-backend-runtime.md).

## Quick start

```bash
docker compose up -d                    # local Postgres on :5432
uv sync                                 # dependencies
uv run uvicorn kcms.app:app --reload    # http://127.0.0.1:8000
uv run pytest                           # tests
uv run ruff check .                      # lint
uv run python -m kcms.openapi_export     # regenerate openapi.json
```

Migrations run and seed data is inserted automatically on startup.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Database-aware health probe |
| `POST` | `/api/v1/pilot-requests` | Public pilot-access request; no account required |
| `GET` | `/api/v1/admin/pilot-requests` | Platform Administrator pilot queue |
| `POST` | `/api/v1/admin/pilot-requests/{id}/decision` | Approve or decline a pilot request |
| `GET/POST` | `/api/v1/setup-invitations/{token}` | Preview and accept a one-time owner setup link |
| `POST` | `/api/v1/auth/signup`, `/signin`, `/signout` | Email/password sessions |
| `GET` | `/api/v1/comments` | Searchable, filterable, stably paginated work list with source context |
| `POST` | `/api/v1/comments/{id}/actions` | Record `HIDE` / `LEAVE` / `UNHIDE`, returns history |
| `POST` | `/api/v1/comments/{id}/corrections` | Record what a human says the labels should be |
| `GET/DELETE` | `/api/v1/facebook/connection` | Read or disconnect the workspace Page connection |
| `POST` | `/api/v1/facebook/connections/manual` | Validate and connect an advanced Page access token |
| `POST` | `/api/v1/facebook/oauth/start` | Begin Facebook authorization for an approved workspace |
| `GET/POST` | `/api/v1/facebook/oauth/sessions/{state}` | List authorized Pages and confirm one Page |

`GET /api/v1/health` returns `200 READY/REACHABLE`, or `503 DEGRADED/UNREACHABLE`
when PostgreSQL cannot answer. It never returns exception or connection detail.
A failed database connection does **not** crash startup — the service boots and
reports `DEGRADED`, so a misconfiguration is diagnosable rather than a crash loop.

## How a comment is classified

```
comment ──▶ classifier ──▶ verdict (severity + target, each with confidence)
                              │
                              ▼
                         surfaced_reason
     triage · institution_sample · novel_language · uncertainty · cleared
```

Two independent axes, because automatic hiding is gated on a conjunction across
both. A single combined score cannot express *"certain it is harmful, unsure who
it is aimed at."*

| Axis | Values |
|---|---|
| Severity | `SAFE` · `OFFENSIVE` · `HARMFUL` |
| Target | `PERSON` · `INSTITUTION` · `NEITHER` |

**Criticism aimed at an institution is never routed for removal**, however
hostile. That is the error this product exists to avoid, and it has a regression
test.

**Unrecognised Khmer abstains rather than clearing.** A no-pattern-hit is not
evidence of safety — new slang must reach a human, or the matcher silently
clears exactly what it cannot read (`surfaced_reason = novel_language`).

## The classifier seam

One interface, two implementations. Nothing downstream knows which is running.

```python
classifier = PatternMatcher()          # now  — disclosed rules, v0.1
classifier = KhmerModel("./model_v1")  # later — trained transformer
```

Routing, queue, thresholds and storage all talk to
[`moderation/contracts.py`](src/kcms/moderation/contracts.py), never to a model.

> The current classifier is **not** Khmer NLP. Every accuracy claim about it is a
> claim about routing, not language understanding.

## Record model

Three record kinds, never derived from one another.

| Record | Is |
|---|---|
| **Verdict** | What the model asserted |
| **Action** | What happened to the comment (`HIDE`/`LEAVE`/`UNHIDE`) |
| **Correction** | What a human explicitly asserts the labels should be |

**Hiding a comment writes no Correction.** If moderator actions became training
labels, the model would drift toward suppression while the dashboard showed
*improving* agreement with humans.

Storage splits along the erasure boundary: `comment_content` is purged when a
commenter deletes at source; `verdict` and `action` are append-only and survive
that purge. Deleting speech removes the speech; it does not falsify the audit trail.

## Pilot onboarding

```
visitor submits request
        │
        ▼
Platform Administrator reviews it
        │ approved
        ▼
single-use setup link ──▶ client chooses their own password
        │
        ▼
approved workspace owner signs in
```

The backend never creates or emails a password. New clients receive a random,
seven-day, single-use setup URL and only its SHA-256 token hash is stored. If the
email already belongs to a KCMS account, approval upgrades that existing
workspace instead of creating a duplicate identity.

Transactional email is optional. When SMTP is unavailable, the administrative
response exposes the one-time setup URL to the Platform Administrator for manual
sharing and records `MANUAL_REQUIRED` in `notification_delivery`.

## Facebook Page connection

Approved Client workspaces have two credential-acquisition paths:

```text
Continue with Facebook -> choose authorized Page -> confirm
Connect with Page token -> validate Page identity and tasks
                                    |
                                    v
                   one encrypted Page Connection record
```

The manual path is advanced setup for development, troubleshooting, or assisted
onboarding. Both paths expose the same KCMS capabilities when Meta grants the
same Page tasks. Stored Page credentials are encrypted with Fernet and are never
returned to the browser. Disconnect deletes the provider credential.

This slice implements authorization, validation, Page selection, secure storage,
status, and disconnect. It has not yet been exercised against a live Meta app;
comment synchronization and provider-side hide/unhide remain external work.

### SMTP configuration

The implementation is provider-neutral SMTP. Recommended production values for
Resend are:

| Variable | Value |
|---|---|
| `PUBLIC_FRONTEND_URL` | `https://kcms-frontend.vercel.app` |
| `SMTP_HOST` | `smtp.resend.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USERNAME` | `resend` |
| `SMTP_PASSWORD` | Resend API key; secret |
| `SMTP_FROM_EMAIL` | A sender on the verified domain |
| `SMTP_FROM_NAME` | `KCMS` |

All four of host, username, password and sender email are required to enable
delivery. With any one absent, startup remains healthy and onboarding uses the
audited manual-link fallback.

## Layout

```
migrations/          forward-only SQL, applied in filename order
src/kcms/
├── api/             HTTP routing and transport schemas
├── auth/            identities, password hashing and sessions
├── pilot/           public requests and invitation acceptance
├── notifications/   provider-neutral email contract and SMTP adapter
├── integrations/    provider ports, Meta authorization and encrypted credentials
├── moderation/      classifier seam, pattern matcher, repository, seeds
├── shared/database/ asyncpg pool and migration runner
└── app.py           application factory
openapi.json         contract artifact — a test asserts byte equality
```

## Deployment

Render (Singapore). The service watches `main`, but its GitHub webhook currently
does not fire reliably; verify the commit and trigger one manual deploy when a
push produces no deployment.

| | |
|---|---|
| Build | `pip install uv && uv sync --frozen --no-dev` |
| Start | `uv run uvicorn kcms.app:app --host 0.0.0.0 --port $PORT` |
| Env | `DATABASE_URL`, `CORS_ORIGINS`, `PUBLIC_FRONTEND_URL`, `PLATFORM_ADMIN_EMAILS`, optional SMTP and Meta variables |

Environment variable changes require a redeploy to take effect.

> The free plan spins down after ~15 minutes idle; the first request then takes
> 30–60 seconds.

## Not yet built

Live Facebook verification, comment synchronization, provider-side hide/unhide,
a full moderation-history endpoint, fleet health and audit views for Platform
Administration, metrics with meaningful denominators, and rate limiting.

### Meta configuration

| Variable | Purpose |
|---|---|
| `META_GRAPH_VERSION` | Explicit supported Graph API version, for example `vXX.X` |
| `META_APP_ID` / `META_APP_SECRET` | KCMS Meta application credentials |
| `META_OAUTH_REDIRECT_URI` | Backend OAuth callback URL |
| `META_OAUTH_SCOPES` | Comma-separated Page permissions requested during authorization |
| `INTEGRATION_ENCRYPTION_KEY` | Fernet key used only for provider credentials |
