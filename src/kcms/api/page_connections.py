import json
import secrets
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from kcms.api.auth import current_user
from kcms.auth import repository as auth_repository
from kcms.auth.security import hash_session_token
from kcms.integrations import repository
from kcms.integrations.contracts import ProviderPage
from kcms.integrations.credentials import CredentialCipher, get_credential_cipher
from kcms.integrations.facebook import MetaClient, get_meta_client
from kcms.moderation import repository as moderation_repository
from kcms.settings import settings
from kcms.shared.database import database

router = APIRouter(prefix="/api/v1/facebook")


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


class PageConnectionState(BaseModel):
    state: Literal["NOT_CONNECTED", "CONNECTED"]
    page_id: str | None = None
    page_name: str | None = None
    method: Literal["FACEBOOK_LOGIN", "MANUAL_TOKEN"] | None = None
    tasks: list[str] = Field(default_factory=list)
    can_moderate: bool = False
    connected_at: datetime | None = None
    last_synced_at: datetime | None = None


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


@router.get(
    "/connection", operation_id="getFacebookConnection", response_model=PageConnectionState
)
async def get_connection(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> PageConnectionState:
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        found = await repository.get_page_connection(connection, workspace["id"])
    return PageConnectionState(**_public(found).model_dump()) if found else PageConnectionState(
        state="NOT_CONNECTED"
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
    try:
        page = await meta.validate_page_token(body.page_access_token)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    encrypted = cipher.seal(page.access_token)
    async with database.acquire() as connection:
        row = await repository.upsert_page_connection(
            connection,
            workspace_id=workspace["id"],
            user_id=user["id"],
            page=page,
            method="MANUAL_TOKEN",
            credential_ciphertext=encrypted,
        )
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
        await repository.create_oauth_attempt(
            connection,
            state_hash=hash_session_token(state),
            workspace_id=workspace["id"],
            user_id=user["id"],
        )
    return OAuthStart(authorization_url=meta.authorization_url(state))


@router.get("/oauth/callback", include_in_schema=False)
async def authorization_callback(
    code: str,
    state: str,
    meta: Annotated[MetaClient, Depends(get_meta_client)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> RedirectResponse:
    state_hash = hash_session_token(state)
    async with database.acquire() as connection:
        attempt = await repository.oauth_attempt(connection, state_hash)
    if not attempt:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "authorization state is invalid")
    try:
        user_token = await meta.exchange_code(code)
        pages = await meta.list_pages(user_token)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if not pages:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no authorized Pages found")
    encrypted = cipher.seal(json.dumps([asdict(page) for page in pages]))
    async with database.acquire() as connection:
        if not await repository.store_oauth_candidates(connection, state_hash, encrypted):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "authorization state expired")
    target = f"{settings.public_frontend_url.rstrip('/')}/app/connect?facebook_session={state}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


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
    page = next((candidate for candidate in pages if candidate.page_id == body.page_id), None)
    if not page:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Page is not in this authorization",
        )
    async with database.acquire() as connection:
        async with connection.transaction():
            row = await repository.upsert_page_connection(
                connection,
                workspace_id=workspace["id"],
                user_id=user["id"],
                page=page,
                method="FACEBOOK_LOGIN",
                credential_ciphertext=cipher.seal(page.access_token),
            )
            await repository.delete_oauth_attempt(connection, hash_session_token(state))
    return _public(row)


@router.delete(
    "/connection", operation_id="disconnectFacebookPage", status_code=status.HTTP_204_NO_CONTENT
)
async def disconnect(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> None:
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        await repository.delete_page_connection(connection, workspace["id"])


class SyncResult(BaseModel):
    fetched: int
    imported: int
    page_id: str
    page_name: str
    last_synced_at: datetime | None


@router.post("/sync", operation_id="syncFacebookComments", response_model=SyncResult)
async def sync_comments(
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
        found = await repository.get_page_connection(connection, workspace["id"])
        if not found:
            raise HTTPException(status.HTTP_409_CONFLICT, "no Facebook Page is connected")
        stored = await repository.credential_for_workspace(connection, workspace["id"])
        if not stored:
            raise HTTPException(status.HTTP_409_CONFLICT, "no Facebook Page is connected")
        token = cipher.open(stored["credential_ciphertext"])

    try:
        comments = await meta.fetch_comments(stored["external_page_id"], token)
    except ValueError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    async with database.acquire() as connection:
        imported = await moderation_repository.ingest_provider_comments(
            connection, workspace["id"], stored["external_page_id"], comments
        )
        await repository.mark_synced(connection, workspace["id"])
        refreshed = await repository.get_page_connection(connection, workspace["id"])

    return SyncResult(
        fetched=len(comments),
        imported=imported,
        page_id=found["page_id"],
        page_name=found["page_name"],
        last_synced_at=refreshed["last_synced_at"] if refreshed else None,
    )
