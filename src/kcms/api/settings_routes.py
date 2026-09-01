from typing import Annotated, Any

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
