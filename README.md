# KCMS Backend

FastAPI modular monolith. See `../kcms-planning/adr/0002-backend-runtime.md`.

```bash
docker compose up -d          # local Postgres
uv sync                       # dependencies
uv run uvicorn kcms.app:app --reload
uv run pytest                 # tests
uv run python -m kcms.openapi_export   # regenerate the contract artifact
```

`GET /api/v1/health` returns `200 READY/REACHABLE`, or `503 DEGRADED/UNREACHABLE`
when PostgreSQL cannot answer. It never returns exception or connection detail.
