import json
import secrets
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from kcms.api.auth import current_user
from kcms.auth import repository as auth_repository
from kcms.auth.security import hash_session_token
from kcms.billing.plans import PLAN_PAGE_LIMITS
from kcms.integrations import repository
from kcms.integrations.contracts import ProviderPage
from kcms.integrations.credentials import CredentialCipher, get_credential_cipher
from kcms.integrations.facebook import MetaClient, get_meta_client
from kcms.integrations.repository import PageAlreadyConnected, PageLimitReached
from kcms.moderation import repository as moderation_repository
from kcms.settings import settings
from kcms.shared.database import database

router = APIRouter(prefix="/api/v1/facebook")

_ALREADY_CONNECTED = (
    "This Facebook Page is already connected to another KCMS workspace. "
    "Disconnect it there first, or sign in to that workspace."
)

_LIMIT_REACHED = (
    "Your plan's Page limit is reached. Disconnect a Page or upgrade your plan "
    "to connect another one."
)

_TRIAL_EXPIRED = (
    "This free trial has expired. Upgrade the workspace to continue using Facebook actions."
)


class ManualPageConnection(BaseModel):
    page_access_token: str = Field(min_length=20, max_length=4096)


class PageConnection(BaseModel):
    state: Literal["CONNECTED"] = "CONNECTED"
    page_id: str
    page_name: str
    method: Literal["FACEBOOK_LOGIN", "MANUAL_TOKEN"]
    tasks: list[str]
    can_moderate: bool
    connected_at: datetime
    last_synced_at: datetime | None


class PageConnections(BaseModel):
    plan: Literal["TRIAL", "STARTER", "GROWTH"]
    page_limit: int
    connections: list[PageConnection]


class OAuthStart(BaseModel):
    authorization_url: str


class PageChoice(BaseModel):
    page_id: str
    page_name: str
    tasks: list[str]
    can_moderate: bool


class PageChoices(BaseModel):
    pages: list[PageChoice]


class SelectPage(BaseModel):
    page_id: str = Field(min_length=1, max_length=200)


def _public(row: dict[str, Any]) -> PageConnection:
    tasks = list(row["tasks"] or [])
    can_moderate = bool(
        {"PROFILE_PLUS_MODERATE", "PROFILE_PLUS_MANAGE", "PROFILE_PLUS_FULL_CONTROL"}
        .intersection(tasks)
    )
    return PageConnection(**{**row, "tasks": tasks}, can_moderate=can_moderate)


async def _workspace(connection, user: dict[str, Any]) -> dict[str, Any]:
    workspace = await auth_repository.workspace_for_user(connection, user["id"])
    if not workspace:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
    return workspace


def _require_active_trial(workspace: dict[str, Any]) -> None:
    if auth_repository.trial_expired(workspace):
        raise HTTPException(status.HTTP_403_FORBIDDEN, _TRIAL_EXPIRED)


async def _require_capacity(connection, workspace: dict[str, Any], page_id: str) -> None:
    """Reconnecting a Page you already hold never counts against the limit —
    only genuinely adding a new one does."""
    existing = await repository.get_page_connection(connection, workspace["id"], page_id)
    if existing:
        return
    count = await repository.count_page_connections(connection, workspace["id"])
    if count >= PLAN_PAGE_LIMITS[workspace["plan"]]:
        raise PageLimitReached(page_id)


@router.get(
    "/connections", operation_id="listFacebookConnections", response_model=PageConnections
)
async def list_connections(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> PageConnections:
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        found = await repository.list_page_connections(connection, workspace["id"])
    return PageConnections(
        plan=workspace["plan"],
        page_limit=PLAN_PAGE_LIMITS[workspace["plan"]],
        connections=[_public(row) for row in found],
    )


@router.post(
    "/connections/manual",
    operation_id="connectFacebookPageManually",
    response_model=PageConnection,
    status_code=status.HTTP_201_CREATED,
)
async def connect_manually(
    body: ManualPageConnection,
    user: Annotated[dict[str, Any], Depends(current_user)],
    meta: Annotated[MetaClient, Depends(get_meta_client)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> PageConnection:
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        _require_active_trial(workspace)
    try:
        page = await meta.validate_page_token(body.page_access_token)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    encrypted = cipher.seal(page.access_token)
    async with database.acquire() as connection:
        try:
            await _require_capacity(connection, workspace, page.page_id)
            row = await repository.add_page_connection(
                connection,
                workspace_id=workspace["id"],
                user_id=user["id"],
                page=page,
                method="MANUAL_TOKEN",
                credential_ciphertext=encrypted,
            )
        except PageAlreadyConnected as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, _ALREADY_CONNECTED) from exc
        except PageLimitReached as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, _LIMIT_REACHED) from exc
    return _public(row)


@router.post(
    "/oauth/start",
    operation_id="startFacebookAuthorization",
    response_model=OAuthStart,
    status_code=status.HTTP_201_CREATED,
)
async def start_authorization(
    user: Annotated[dict[str, Any], Depends(current_user)],
    meta: Annotated[MetaClient, Depends(get_meta_client)],
) -> OAuthStart:
    state = secrets.token_urlsafe(32)
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        _require_active_trial(workspace)
        await repository.create_oauth_attempt(
            connection,
            state_hash=hash_session_token(state),
            workspace_id=workspace["id"],
            user_id=user["id"],
        )
    return OAuthStart(authorization_url=meta.authorization_url(state))


def _connect_redirect(**query: str) -> RedirectResponse:
    target = f"{settings.public_frontend_url.rstrip('/')}/app/connect"
    return RedirectResponse(
        f"{target}?{urlencode(query)}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/oauth/callback", include_in_schema=False)
async def authorization_callback(
    meta: Annotated[MetaClient, Depends(get_meta_client)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Return the browser to KCMS whatever happens.

    Every failure here used to raise, which rendered JSON on the API's own
    domain: the operator never came back to the app, saw no explanation, and
    on navigating back found the connect screen exactly as before. Meta also
    calls this with `error` and no `code` when someone cancels, which did not
    even satisfy the signature.

    The reason travels as a short code rather than a message so the frontend
    can say it in the reader's own language.
    """
    if error or not code or not state:
        # Cancelling is the ordinary case here, not a fault.
        return _connect_redirect(facebook_error="denied" if error else "incomplete")

    state_hash = hash_session_token(state)
    async with database.acquire() as connection:
        attempt = await repository.oauth_attempt(connection, state_hash)
    if not attempt:
        return _connect_redirect(facebook_error="state_invalid")

    try:
        user_token = await meta.exchange_code(code)
        pages = await meta.list_pages(user_token)
    except ValueError:
        return _connect_redirect(facebook_error="exchange_failed")

    if not pages:
        return _connect_redirect(facebook_error="no_pages")

    encrypted = cipher.seal(json.dumps([asdict(page) for page in pages]))
    async with database.acquire() as connection:
        if not await repository.store_oauth_candidates(connection, state_hash, encrypted):
            return _connect_redirect(facebook_error="state_expired")

    return _connect_redirect(facebook_session=state)


async def _oauth_pages(
    state: str,
    user: dict[str, Any],
    cipher: CredentialCipher,
) -> tuple[dict[str, Any], list[ProviderPage]]:
    state_hash = hash_session_token(state)
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        attempt = await repository.oauth_attempt(
            connection,
            state_hash,
            workspace_id=workspace["id"],
            user_id=user["id"],
        )
    if not attempt or not attempt["candidate_ciphertext"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "authorization session not found")
    try:
        payload = json.loads(cipher.open(attempt["candidate_ciphertext"]))
        pages = [ProviderPage(**item) for item in payload]
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "authorization session unreadable",
        ) from exc
    return workspace, pages


@router.get(
    "/oauth/sessions/{state}",
    operation_id="listFacebookPageChoices",
    response_model=PageChoices,
)
async def list_page_choices(
    state: str,
    user: Annotated[dict[str, Any], Depends(current_user)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> PageChoices:
    _, pages = await _oauth_pages(state, user, cipher)
    return PageChoices(
        pages=[
            PageChoice(
                page_id=page.page_id,
                page_name=page.page_name,
                tasks=list(page.tasks),
                can_moderate=page.can_moderate,
            )
            for page in pages
        ]
    )


@router.post(
    "/oauth/sessions/{state}/selection",
    operation_id="selectFacebookPage",
    response_model=PageConnection,
    status_code=status.HTTP_201_CREATED,
)
async def select_page(
    state: str,
    body: SelectPage,
    user: Annotated[dict[str, Any], Depends(current_user)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> PageConnection:
    workspace, pages = await _oauth_pages(state, user, cipher)
    _require_active_trial(workspace)
    page = next((candidate for candidate in pages if candidate.page_id == body.page_id), None)
    if not page:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Page is not in this authorization",
        )
    async with database.acquire() as connection:
        try:
            await _require_capacity(connection, workspace, page.page_id)
            async with connection.transaction():
                row = await repository.add_page_connection(
                    connection,
                    workspace_id=workspace["id"],
                    user_id=user["id"],
                    page=page,
                    method="FACEBOOK_LOGIN",
                    credential_ciphertext=cipher.seal(page.access_token),
                )
                await repository.delete_oauth_attempt(connection, hash_session_token(state))
        except PageAlreadyConnected as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, _ALREADY_CONNECTED) from exc
        except PageLimitReached as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, _LIMIT_REACHED) from exc
    return _public(row)


@router.delete(
    "/connections/{page_id}",
    operation_id="disconnectFacebookPage",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def disconnect(
    page_id: str,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> None:
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        await repository.delete_page_connection(connection, workspace["id"], page_id)


class SyncResult(BaseModel):
    fetched: int
    imported: int
    page_id: str
    page_name: str
    last_synced_at: datetime | None


@router.post(
    "/connections/{page_id}/sync", operation_id="syncFacebookComments", response_model=SyncResult
)
async def sync_comments(
    page_id: str,
    user: Annotated[dict[str, Any], Depends(current_user)],
    meta: Annotated[MetaClient, Depends(get_meta_client)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> SyncResult:
    """Pull comments from the connected Page into this workspace.

    Re-syncing is safe: the provider's comment id is the primary key, so an
    already-imported comment keeps its verdict, actions and corrections.
    """
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        _require_active_trial(workspace)
        found = await repository.get_page_connection(connection, workspace["id"], page_id)
        if not found:
            raise HTTPException(status.HTTP_409_CONFLICT, "no Facebook Page is connected")
        stored = await repository.credential_for_page(connection, workspace["id"], page_id)
        if not stored:
            raise HTTPException(status.HTTP_409_CONFLICT, "no Facebook Page is connected")
        token = cipher.open(stored["credential_ciphertext"])

    try:
        comments = await meta.fetch_comments(stored["external_page_id"], token)
    except ValueError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    async with database.acquire() as connection:
        imported, to_delete, to_quarantine, to_hide_offensive = (
            await moderation_repository.ingest_provider_comments(
                connection,
                workspace["id"],
                stored["external_page_id"],
                comments,
                workspace["auto_delete_delay_minutes"],
                workspace["auto_hide_offensive"],
                workspace["keyword_allowlist"],
                workspace["keyword_blocklist"],
            )
        )
        await repository.mark_synced(connection, workspace["id"], page_id)
        refreshed = await repository.get_page_connection(connection, workspace["id"], page_id)

    # Harmful comments are removed from the Page without waiting for a
    # reviewer — deleted outright with no quarantine delay configured, or
    # hidden now with the delete scheduled for later otherwise. A Graph
    # failure is not fatal to the sync: the action is already recorded,
    # provider_applied stays false, and the comment is still in the queue for
    # a human to action by hand (which also cancels the pending schedule).
    for comment_id in to_delete:
        try:
            await meta.delete_comment(comment_id, token)
        except Exception:
            continue
        async with database.acquire() as connection:
            await moderation_repository.mark_action_applied(connection, comment_id, "DELETE")

    for comment_id in to_quarantine:
        try:
            await meta.set_comment_hidden(comment_id, token, hidden=True)
        except Exception:
            continue
        async with database.acquire() as connection:
            await moderation_repository.mark_action_applied(connection, comment_id, "HIDE")

    # Offensive comments this workspace auto-hides. Same Graph call as
    # quarantine, but never scheduled for deletion — a person still decides.
    for comment_id in to_hide_offensive:
        try:
            await meta.set_comment_hidden(comment_id, token, hidden=True)
        except Exception:
            continue
        async with database.acquire() as connection:
            await moderation_repository.mark_action_applied(connection, comment_id, "HIDE")

    return SyncResult(
        fetched=len(comments),
        imported=imported,
        page_id=found["page_id"],
        page_name=found["page_name"],
        last_synced_at=refreshed["last_synced_at"] if refreshed else None,
    )
