"""Cloudflare Python Worker entry point for the KCMS FastAPI application."""

import os

from workers import asgi, env

from kcms.cloudflare_runtime import (
    database_url_from_hyperdrive,
    environment_from_bindings,
    run_scheduled_sweep,
)

for key, value in environment_from_bindings(env, include_hyperdrive=False).items():
    os.environ[key] = value

from kcms.app import app  # noqa: E402  (settings must be populated before import)
from kcms.moderation.quarantine import sweep_once  # noqa: E402
from kcms.settings import settings  # noqa: E402
from kcms.shared.database import database  # noqa: E402

FastAPIEntrypoint = asgi.entrypoint(app)


class Default(FastAPIEntrypoint):
    async def fetch(self, request):
        settings.database_url = database_url_from_hyperdrive(self.env.HYPERDRIVE)
        # Startup runs inside whichever request reached the isolate first, so
        # the address captured there must not be reused for later requests.
        database.set_dsn(settings.database_url)
        return await super().fetch(request)

    async def scheduled(self, controller, env=None, ctx=None) -> None:
        # The runtime passes None for the env argument; bindings live on
        # self.env, as in fetch. Reading the argument made every cron
        # invocation raise AttributeError, so the sweep never ran.
        settings.database_url = database_url_from_hyperdrive(self.env.HYPERDRIVE)
        database.set_dsn(settings.database_url)
        await run_scheduled_sweep(database, settings, sweep_once)
