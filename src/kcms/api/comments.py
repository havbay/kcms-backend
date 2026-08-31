from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from kcms.api.auth import current_user
from kcms.auth import repository as auth_repository
from kcms.moderation import repository
from kcms.shared.database import database

router = APIRouter(prefix="/api/v1")

ActionKind = Literal["LEAVE", "HIDE", "UNHIDE"]
SeverityLabel = Literal["SAFE", "OFFENSIVE", "HARMFUL"]
TargetLabel = Literal["PERSON", "INSTITUTION", "NEITHER"]


class WorkListItem(BaseModel):
    comment_id: str
    text: str
    author_ref: str
    posted_at: datetime
    severity: str | None
    severity_confidence: float | None
    target: str | None
    target_confidence: float | None
    abstain: bool | None
    surfaced_reason: str | None
    rationale: str | None
    model_version: str | None
    latest_action: str | None
    latest_actor: str | None
    latest_action_at: datetime | None
    corrected_severity: str | None
    corrected_target: str | None
    corrected_by: str | None
    corrected_at: datetime | None


class WorkList(BaseModel):
    items: list[WorkListItem]
    total: int
    limit: int
    offset: int


class ReasonCount(BaseModel):
    surfaced_reason: str
    count: int


class Summary(BaseModel):
    """Counts computed across the whole workspace, not from one page."""

    processed: int
    need_review: int
    reviewed: int
    pending: int
    left_visible: int
    hidden: int
    unhidden: int
    reasons: list[ReasonCount]


class ActionRequest(BaseModel):
    kind: ActionKind


class HistoryEntry(BaseModel):
    kind: str
    actor: str
    occurred_at: datetime


class CorrectionRequest(BaseModel):
    severity: SeverityLabel
    target: TargetLabel
    note: str | None = None


class CorrectionResponse(BaseModel):
    severity: str
    target: str
    actor: str
    note: str | None
    disagrees_with_model: str
    occurred_at: datetime


def _require_database() -> None:
    if not database.connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )


@router.get("/comments", operation_id="listComments", response_model=WorkList)
async def list_comments(
    user: Annotated[dict[str, Any], Depends(current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WorkList:
    _require_database()
    async with database.acquire() as connection:
        rows, total = await repository.fetch_work_list(
            connection, await _workspace_id(connection, user), limit, offset
        )
    return WorkList(
        items=[WorkListItem(**row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/comments/summary", operation_id="getWorkspaceSummary", response_model=Summary)
async def get_summary(user: Annotated[dict[str, Any], Depends(current_user)]) -> Summary:
    _require_database()
    async with database.acquire() as connection:
        summary = await repository.summarise_workspace(
            connection, await _workspace_id(connection, user)
        )
    return Summary(**summary)


@router.post(
    "/comments/{comment_id}/actions",
    operation_id="recordAction",
    response_model=list[HistoryEntry],
    status_code=status.HTTP_201_CREATED,
)
async def record_action(
    comment_id: str,
    body: ActionRequest,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> list[HistoryEntry]:
    """Records a moderation Action. Actions are append-only and reversible,
    and never become training labels."""
    _require_database()
    async with database.acquire() as connection:
        workspace_id = await _workspace_id(connection, user)
        # 404 rather than 403: a different status would confirm the comment
        # exists in someone else's workspace.
        if not await repository.comment_belongs_to(connection, comment_id, workspace_id):
            raise HTTPException(status_code=404, detail="comment not found")
        await repository.record_action(connection, comment_id, body.kind, user["display_name"])
        history = await repository.fetch_history(connection, comment_id)
    return [HistoryEntry(**entry) for entry in history]


@router.post(
    "/comments/{comment_id}/corrections",
    operation_id="recordCorrection",
    response_model=CorrectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_correction(
    comment_id: str,
    body: CorrectionRequest,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> CorrectionResponse:
    """Records what a human asserts the labels should be.

    A Correction is not an Action. Submitting one does not hide, unhide or
    otherwise change what happens to the comment.
    """
    _require_database()
    async with database.acquire() as connection:
        workspace_id = await _workspace_id(connection, user)
        if not await repository.comment_belongs_to(connection, comment_id, workspace_id):
            raise HTTPException(status_code=404, detail="comment not found")
        created = await repository.record_correction(
            connection, comment_id, body.severity, body.target, user["display_name"], body.note
        )
    if created is None:
        raise HTTPException(status_code=500, detail="correction was not stored")
    return CorrectionResponse(**created)


async def _workspace_id(connection, user: dict[str, Any]) -> str:
    workspace = await auth_repository.workspace_for_user(connection, user["id"])
    if not workspace:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
    return workspace["id"]
