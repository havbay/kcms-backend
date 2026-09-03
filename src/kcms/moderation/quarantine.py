"""The other half of quarantine auto-delete: actually deleting a comment
once its delay expires with no human intervention.

No task queue or scheduler exists anywhere in this codebase, so this is the
simplest thing that works: a DB-tracked due-list (`scheduled_deletion`) and a
plain asyncio loop that sweeps it. `sweep_once` is what has behaviour and
what tests call directly; `run_quarantine_sweep` is just the loop around it,
started from `app.py`'s lifespan.
"""

from __future__ import annotations

import asyncio
import logging

from kcms.integrations import repository as integrations_repository
from kcms.integrations.credentials import FernetCredentialCipher
from kcms.integrations.facebook import GraphMetaClient
from kcms.moderation import repository as moderation_repository
from kcms.settings import settings
from kcms.shared.database import database

logger = logging.getLogger("kcms.quarantine")


async def sweep_once() -> int:
    """Delete every comment whose quarantine delay has expired.

    Returns how many were actually deleted, mostly so tests have something
    to assert on. Not configured (no Meta credentials, no encryption key) or
    no database connection: a silent no-op, same as the request-scoped
    optional dependencies (`get_optional_meta_client` and friends) — a
    deployment without Facebook configured has nothing here to sweep anyway.
    """
    if not database.connected:
        return 0
    if not settings.meta_graph_version or not settings.integration_encryption_key:
        return 0

    meta = GraphMetaClient(
        settings.meta_graph_version,
        settings.meta_app_id,
        settings.meta_app_secret,
        settings.meta_oauth_redirect_uri,
        settings.meta_oauth_scopes,
        settings.meta_login_config_id,
    )
    cipher = FernetCredentialCipher(settings.integration_encryption_key)

    async with database.acquire() as connection:
        due = await moderation_repository.due_scheduled_deletions(connection)

    deleted = 0
    for row in due:
        comment_id = row["comment_id"]
        try:
            async with database.acquire() as connection:
                credential = await integrations_repository.credential_for_page(
                    connection, row["workspace_id"], row["page_id"]
                )
            if not credential:
                # The Page was disconnected since this was scheduled. Nothing
                # left to mirror to; drop the schedule so it stops being due.
                async with database.acquire() as connection:
                    await moderation_repository.pop_scheduled_deletion(connection, comment_id)
                continue

            token = cipher.open(credential["credential_ciphertext"])
            await meta.delete_comment(comment_id, token)

            async with database.acquire() as connection:
                await moderation_repository.record_action(
                    connection, comment_id, "DELETE", "system:auto-removal",
                    provider_applied=True,
                )
                await moderation_repository.pop_scheduled_deletion(connection, comment_id)
            deleted += 1
        except Exception:
            # One comment's failure — a transient Graph error, a decryption
            # problem — must not stop the rest, and must not drop the
            # schedule: it is simply due again on the next sweep.
            logger.exception("quarantine sweep failed to delete comment %s", comment_id)
    return deleted


async def run_quarantine_sweep(interval_seconds: int) -> None:
    """Sweep forever, `interval_seconds` apart, until cancelled."""
    while True:
        try:
            await sweep_once()
        except Exception:
            logger.exception("quarantine sweep tick failed")
        await asyncio.sleep(interval_seconds)
