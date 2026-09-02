from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kcms.api.auth import current_user
from kcms.auth import repository as auth_repository
from kcms.moderation import repository as moderation_repository
from kcms.shared.database import database
from kcms.team import repository

router = APIRouter(prefix="/api/v1/settings")


class WorkspaceSettings(BaseModel):
    workspace_id: str
    workspace_name: str
    is_sandbox: bool
    your_role: str
    display_name: str
    # What is stored, not how the workspace was provisioned: the removal
    # control depends on samples existing, not on the sandbox flag.
    sample_comments: int


class RenameWorkspace(BaseModel):
    name: str = Field(min_length=2, max_length=80)


class RenameSelf(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)


def _require_database() -> None:
    if not database.connected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")


async def _settings(
    connection, workspace: dict[str, Any], display_name: str
) -> WorkspaceSettings:
    return WorkspaceSettings(
        workspace_id=workspace["id"],
        workspace_name=workspace["name"],
        is_sandbox=workspace["is_sandbox"],
        your_role=workspace["role"],
        display_name=display_name,
        sample_comments=await moderation_repository.count_sample_comments(
            connection, workspace["id"]
        ),
    )


@router.get("", operation_id="getSettings", response_model=WorkspaceSettings)
async def get_settings(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> WorkspaceSettings:
    _require_database()
    async with database.acquire() as connection:
        workspace = await auth_repository.workspace_for_user(connection, user["id"])
    if not workspace:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
    async with database.acquire() as connection:
        return await _settings(connection, workspace, user["display_name"])


@router.patch(
    "/workspace", operation_id="renameWorkspace", response_model=WorkspaceSettings
)
async def rename_workspace(
    body: RenameWorkspace,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> WorkspaceSettings:
    _require_database()
    async with database.acquire() as connection:
        workspace = await auth_repository.workspace_for_user(connection, user["id"])
        if not workspace:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
        # Renaming the shared workspace affects everyone in it, so it is an
        # owner action rather than something any member can do.
        if workspace["role"] != "owner":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only an owner can rename a workspace")
        await repository.rename_workspace(connection, workspace["id"], body.name)
        return await _settings(
            connection, {**workspace, "name": body.name.strip()}, user["display_name"]
        )


@router.patch("/me", operation_id="renameSelf", response_model=WorkspaceSettings)
async def rename_self(
    body: RenameSelf,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> WorkspaceSettings:
    """Anyone may change their own display name. It affects how future actions
    are attributed; past actions keep the name used at the time."""
    _require_database()
    async with database.acquire() as connection:
        await repository.rename_user(connection, user["id"], body.display_name)
        workspace = await auth_repository.workspace_for_user(connection, user["id"])
    if not workspace:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
    async with database.acquire() as connection:
        return await _settings(connection, workspace, body.display_name.strip())


class KeywordEntry(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
    # Only two severities carry an outcome, and neither of them is "safe":
    # HARMFUL is removed from the Page automatically, OFFENSIVE goes to a
    # person. A keyword can surface a comment; it can never clear one.
    severity: Literal["HARMFUL", "OFFENSIVE"]
    note: str | None = Field(default=None, max_length=280)
    created_at: datetime | None = None


class KeywordCreate(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
    severity: Literal["HARMFUL", "OFFENSIVE"]
    note: str | None = Field(default=None, max_length=280)


async def _keyword_workspace(connection, user: dict[str, Any]) -> dict[str, Any]:
    workspace = await auth_repository.workspace_for_user(connection, user["id"])
    if not workspace:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
    return workspace


@router.get("/keywords", operation_id="getKeywords", response_model=list[KeywordEntry])
async def get_keywords(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> list[KeywordEntry]:
    """This workspace's own keywords. Never another workspace's, and never the
    shipped defaults: those are not editable and not listed as if they were."""
    _require_database()
    async with database.acquire() as connection:
        workspace = await _keyword_workspace(connection, user)
        rows = await moderation_repository.list_workspace_keywords(
            connection, workspace["id"]
        )
    return [KeywordEntry(**row) for row in rows]


@router.post(
    "/keywords",
    operation_id="addKeyword",
    response_model=KeywordEntry,
    status_code=status.HTTP_201_CREATED,
)
async def add_keyword(
    body: KeywordCreate,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> KeywordEntry:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _keyword_workspace(connection, user)
        created = await moderation_repository.add_workspace_keyword(
            connection,
            workspace["id"],
            body.keyword,
            body.severity,
            body.note,
            user["id"],
        )
    if not created:
        raise HTTPException(status.HTTP_409_CONFLICT, "that keyword is already on the list")
    return KeywordEntry(**created)


@router.delete(
    "/keywords/{keyword}",
    operation_id="removeKeyword",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_keyword(
    keyword: str,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> None:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _keyword_workspace(connection, user)
        removed = await moderation_repository.remove_workspace_keyword(
            connection, workspace["id"], keyword
        )
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such keyword on this workspace")
