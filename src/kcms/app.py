import asyncio
import logging
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from kcms.api import (
    auth,
    comments,
    health,
    page_connections,
    pilot_requests,
    settings_routes,
    team,
)
from kcms.moderation.quarantine import run_quarantine_sweep
from kcms.settings import settings
from kcms.shared.database import database
from kcms.shared.database.migrate import apply_migrations

logger = logging.getLogger("kcms")


def _scrub_sentry_event(event: dict, hint: dict) -> dict:
    """Keep provider credentials and customer comment content out of Sentry."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            for key in ("authorization", "Authorization", "cookie", "Cookie"):
                headers.pop(key, None)
    return event


if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        send_default_pii=False,
        request_bodies="never",
        traces_sample_rate=0.1,
        before_send=_scrub_sentry_event,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A failed connection must not stop the service; /health reports DEGRADED.
    sweep_task: asyncio.Task | None = None
    try:
        await database.connect(settings.database_url)
        async with database.acquire() as connection:
            applied = await apply_migrations(connection)
        logger.info("database ready (migrations=%s)", applied)
        # Only started once the database is actually reachable — without one
        # there is nothing for the sweep to read.
        sweep_task = asyncio.create_task(
            run_quarantine_sweep(settings.quarantine_sweep_interval_seconds)
        )
    except Exception as exc:
        # Startup must not crash: /health reports DEGRADED instead. But the
        # reason has to be visible, or a misconfigured DATABASE_URL is
        # indistinguishable from a missing one.
        logger.warning(
            "database unavailable at startup: %s: %s", type(exc).__name__, exc
        )
    yield
    if sweep_task is not None:
        sweep_task.cancel()
        await asyncio.gather(sweep_task, return_exceptions=True)
    await database.disconnect()


def create_app() -> FastAPI:
    app = FastAPI(
        title="KCMS Backend",
        version=settings.contract_version,
        openapi_version="3.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(comments.router)
    app.include_router(page_connections.router)
    app.include_router(pilot_requests.router)
    app.include_router(team.router)
    app.include_router(settings_routes.router)
    return app


app = create_app()
