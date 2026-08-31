from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from kcms.access import repository
from kcms.api.auth import current_user
from kcms.auth import repository as auth_repository
from kcms.shared.database import database

router = APIRouter(prefix="/api/v1")

MonthlyComments = Literal["UNDER_1K", "1K_TO_10K", "10K_TO_50K", "OVER_50K"]
TeamSize = Literal["JUST_ME", "2_TO_5", "6_TO_20", "OVER_20"]
RequestStatus = Literal["PENDING", "APPROVED", "DECLINED"]


class AccessRequestCreate(BaseModel):
    page_name: str = Field(min_length=2, max_length=200)
    monthly_comments: MonthlyComments
    team_size: TeamSize
    note: str | None = Field(default=None, max_length=1000)


class AccessRequest(BaseModel):
    id: str
    workspace_id: str
    page_name: str
    monthly_comments: str
    team_size: str
    note: str | None
    status: str
    decision_reason: str | None
    decided_at: datetime | None
    created_at: datetime


class AdminAccessRequest(AccessRequest):
    """Everything an administrator may see about a request.

    Comment content is absent by construction. The product specification
    forbids Platform Administrators from browsing customer comments through
    ordinary administration views, and a test asserts this shape holds.
    """

    workspace_name: str
    requester_name: str
    requester_email: str | None


class Decision(BaseModel):
    decision: Literal["APPROVED", "DECLINED"]
    reason: str | None = Field(default=None, max_length=500)


def _require_database() -> None:
    if not database.connected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")


async def require_platform_admin(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> dict[str, Any]:
    """Platform Administration comes from an environment allowlist reconciled at
    sign-in. It is never derived from anything in the request."""
    if not user.get("is_platform_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "platform administration required")
    return user


@router.post(
    "/access-requests",
    operation_id="createAccessRequest",
    response_model=AccessRequest,
    status_code=status.HTTP_201_CREATED,
)
async def create_access_request(
    body: AccessRequestCreate,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> AccessRequest:
    _require_database()
    async with database.acquire() as connection:
        workspace = await auth_repository.workspace_for_user(connection, user["id"])
        if not workspace:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
        if not workspace["is_sandbox"]:
            raise HTTPException(status.HTTP_409_CONFLICT, "this workspace is already approved")
        created = await repository.create_request(
            connection, workspace["id"], user["id"], body.page_name,
            body.monthly_comments, body.team_size, body.note,
        )
    return AccessRequest(**created)


@router.get(
    "/access-requests/mine",
    operation_id="getMyAccessRequest",
    response_model=AccessRequest | None,
)
async def get_my_access_request(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> AccessRequest | None:
    _require_database()
    async with database.acquire() as connection:
        workspace = await auth_repository.workspace_for_user(connection, user["id"])
        if not workspace:
            return None
        found = await repository.latest_for_workspace(connection, workspace["id"])
    return AccessRequest(**found) if found else None


@router.get(
    "/admin/access-requests",
    operation_id="listAccessRequests",
    response_model=list[AdminAccessRequest],
)
async def list_access_requests(
    _: Annotated[dict[str, Any], Depends(require_platform_admin)],
    request_status: Annotated[RequestStatus | None, Query(alias="status")] = None,
) -> list[AdminAccessRequest]:
    _require_database()
    async with database.acquire() as connection:
        rows = await repository.list_for_admin(connection, request_status)
    return [AdminAccessRequest(**row) for row in rows]


@router.post(
    "/admin/access-requests/{request_id}/decision",
    operation_id="decideAccessRequest",
    response_model=AccessRequest,
)
async def decide_access_request(
    request_id: str,
    body: Decision,
    admin: Annotated[dict[str, Any], Depends(require_platform_admin)],
) -> AccessRequest:
    # A decline the client cannot act on is a dead end, so a reason is required.
    if body.decision == "DECLINED" and not (body.reason or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "a reason is required when declining"
        )
    _require_database()
    async with database.acquire() as connection:
        decided = await repository.decide(
            connection, request_id, body.decision,
            (body.reason or "").strip() or None, admin["id"],
        )
    if decided is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "request is missing or already decided")
    return AccessRequest(**decided)
