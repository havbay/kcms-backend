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
| `GET` | `/api/v1/comments` | Moderation work list with verdicts |
| `POST` | `/api/v1/comments/{id}/actions` | Record `HIDE` / `LEAVE` / `UNHIDE`, returns history |

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
| **Correction** | What a human explicitly asserts the labels should be *(not yet built)* |

**Hiding a comment writes no Correction.** If moderator actions became training
labels, the model would drift toward suppression while the dashboard showed
*improving* agreement with humans.

Storage splits along the erasure boundary: `comment_content` is purged when a
commenter deletes at source; `verdict` and `action` are append-only and survive
that purge. Deleting speech removes the speech; it does not falsify the audit trail.

## Layout

```
migrations/          forward-only SQL, applied in filename order
src/kcms/
├── api/             HTTP routing and transport schemas
├── moderation/      classifier seam, pattern matcher, repository, seeds
├── shared/database/ asyncpg pool and migration runner
└── app.py           application factory
openapi.json         contract artifact — a test asserts byte equality
```

## Deployment

Render (Singapore). Pushing to `main` auto-deploys.

| | |
|---|---|
| Build | `pip install uv && uv sync --frozen --no-dev` |
| Start | `uv run uvicorn kcms.app:app --host 0.0.0.0 --port $PORT` |
| Env | `DATABASE_URL`, `CORS_ORIGINS`, `PYTHON_VERSION` |

Environment variable changes require a redeploy to take effect.

> The free plan spins down after ~15 minutes idle; the first request then takes
> 30–60 seconds.

## Not yet built

Authentication · Corrections · Request Access · real Facebook ingestion ·
workspaces and team management · metrics · pagination · rate limiting.
