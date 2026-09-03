from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from kcms.api.auth import current_user
from kcms.auth import repository as auth_repository
from kcms.shared.database import database
from kcms.team import repository

router = APIRouter(prefix="/api/v1/team")

Role = Literal["owner", "member"]


class Member(BaseModel):
    user_id: str
    display_name: str
    email: str | None
    role: str
    created_at: datetime


class Invitation(BaseModel):
    token_hash: str
    role: str
    expires_at: datetime
    created_at: datetime


class CreatedInvitation(BaseModel):
    # Returned once. Only its hash is stored, so it cannot be shown again.
    token: str
    role: str
    expires_at: datetime


class InvitationPreview(BaseModel):
    workspace_name: str
    role: str
    expires_at: datetime


class Team(BaseModel):
    workspace_id: str
    workspace_name: str
    your_role: str
    members: list[Member]
    invitations: list[Invitation]


class CreateInvitation(BaseModel):
    role: Role = "member"


class JoinedWorkspace(BaseModel):
    workspace_id: str
    workspace_name: str


def _require_database() -> None:
    if not database.connected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")


async def _membership(connection, user: dict[str, Any]) -> dict[str, Any]:
    workspace = await auth_repository.workspace_for_user(connection, user["id"])
    if not workspace:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
    return workspace


def _require_owner(workspace: dict[str, Any]) -> None:
    if workspace["role"] != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only an owner can do that")


@router.get("", operation_id="getTeam", response_model=Team)
async def get_team(user: Annotated[dict[str, Any], Depends(current_user)]) -> Team:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _membership(connection, user)
        members = await repository.list_members(connection, workspace["id"])
        # Only owners manage invitations, so only owners are shown them.
        invitations = (
            await repository.list_invitations(connection, workspace["id"])
            if workspace["role"] == "owner"
            else []
        )
    return Team(
        workspace_id=workspace["id"],
        workspace_name=workspace["name"],
        your_role=workspace["role"],
        members=[Member(**m) for m in members],
        invitations=[Invitation(**i) for i in invitations],
    )


@router.post(
    "/invitations",
    operation_id="createInvitation",
    response_model=CreatedInvitation,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    body: CreateInvitation,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> CreatedInvitation:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _membership(connection, user)
        _require_owner(workspace)
        token, created = await repository.create_invitation(
            connection, workspace["id"], user["id"], body.role
        )
    return CreatedInvitation(token=token, role=created["role"], expires_at=created["expires_at"])


@router.delete(
    "/invitations/{token_hash}",
    operation_id="revokeInvitation",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_invitation(
    token_hash: str,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> None:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _membership(connection, user)
        _require_owner(workspace)
        # Scoped to this workspace, so a hash from elsewhere is a 404.
        if not await repository.revoke_invitation(connection, workspace["id"], token_hash):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "invitation not found")


@router.get(
    "/invitations/{token}/preview",
    operation_id="previewInvitation",
    response_model=InvitationPreview,
)
async def preview_invitation(token: str) -> InvitationPreview:
    """Public: someone following a link needs to know what they are joining
    before signing in. Reveals the workspace name and role, nothing else."""
    _require_database()
    async with database.acquire() as connection:
        found = await repository.peek_invitation(connection, token)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invitation is invalid or expired")
    return InvitationPreview(**found)


@router.post(
    "/invitations/{token}/accept",
    operation_id="acceptInvitation",
    response_model=JoinedWorkspace,
)
async def accept_invitation(
    token: str,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> JoinedWorkspace:
    _require_database()
    async with database.acquire() as connection:
        joined = await repository.accept_invitation(connection, token, user["id"])
    if joined is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "invitation is invalid, expired, or already used"
        )
    return JoinedWorkspace(workspace_id=joined["id"], workspace_name=joined["name"])


@router.delete(
    "/members/{member_id}",
    operation_id="removeMember",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    member_id: str,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> None:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _membership(connection, user)
        _require_owner(workspace)
        problem = await repository.remove_member(connection, workspace["id"], member_id)
    if problem == "NOT_FOUND":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    if problem == "LAST_OWNER":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the last owner cannot be removed; promote someone else first",
        )


@router.delete(
    "/membership",
    operation_id="leaveWorkspace",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def leave_workspace(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> None:
    """Leave the workspace you are currently in.

    Removing a member is an owner action, which left anyone who joined a
    workspace unable to get out of it without asking the person who owns it.
    That is the wrong shape: a workspace holds another company's real Facebook
    comments, and someone who wants no further part in it should not need
    permission to stop.

    The last owner still cannot leave. A workspace nobody can administer is
    unrecoverable through the product, so that guard is the same one that
    protects removing a member.
    """
    _require_database()
    async with database.acquire() as connection:
        workspace = await _membership(connection, user)
        problem = await repository.remove_member(connection, workspace["id"], user["id"])
    if problem == "LAST_OWNER":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "you are the last owner of this workspace; promote someone else before leaving",
        )
    if problem == "NOT_FOUND":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "you are not a member of this workspace")
