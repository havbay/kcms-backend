from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from kcms.api import comments, health
from kcms.moderation.repository import seed_if_empty
from kcms.settings import settings
from kcms.shared.database import database
from kcms.shared.database.migrate import apply_migrations


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A failed connection must not stop the service; /health reports DEGRADED.
    try:
        await database.connect(settings.database_url)
        async with database.acquire() as connection:
            await apply_migrations(connection)
            await seed_if_empty(connection)
    except Exception:
        pass
    yield
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
    app.include_router(comments.router)
    return app


app = create_app()
