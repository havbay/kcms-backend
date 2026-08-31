from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field

from kcms.api.access_requests import require_platform_admin
from kcms.api.auth import Session, _as_auth_user
from kcms.notifications.contracts import Notification, NotificationSender
from kcms.notifications.smtp import DisabledNotificationSender, SmtpNotificationSender
from kcms.pilot import repository
from kcms.settings import settings
from kcms.shared.database import database

router = APIRouter(prefix="/api/v1")

PilotStatus = Literal["PENDING", "APPROVED", "DECLINED"]


class PilotRequestCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    organization: str = Field(min_length=2, max_length=120)
    email: EmailStr
    facebook_page: str = Field(min_length=2, max_length=300)
    note: str | None = Field(default=None, max_length=1000)


class PilotRequestReceipt(BaseModel):
    id: str
    status: str
    message: str


class AdminPilotRequest(BaseModel):
    id: str
    name: str
    organization: str
    email: str
    facebook_page: str
    note: str | None
    status: str
    decision_reason: str | None
    decided_at: datetime | None
    created_at: datetime
    delivery_status: str | None = None


class PilotDecision(BaseModel):
    decision: Literal["APPROVED", "DECLINED"]
    reason: str | None = Field(default=None, max_length=500)


class PilotDecisionResult(AdminPilotRequest):
    invitation_url: str | None
    delivery_status: str


class SetupInvitationPreview(BaseModel):
    organization: str
    email: str
    expires_at: datetime


class SetupInvitationAccept(BaseModel):
    display_name: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=8, max_length=200)


def _require_database() -> None:
    if not database.connected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")


def get_notification_sender() -> NotificationSender:
    if not settings.smtp_configured:
        return DisabledNotificationSender()
    return SmtpNotificationSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        from_email=settings.smtp_from_email,
        from_name=settings.smtp_from_name,
        timeout=settings.smtp_timeout_seconds,
    )


async def _record_delivery(
    request_id: str,
    recipient: str,
    notification: Notification,
    delivery_status: str,
    detail: str | None,
) -> None:
    async with database.acquire() as connection:
        await repository.record_delivery(
            connection,
            request_id=request_id,
            recipient=recipient,
            kind=notification.kind,
            status=delivery_status,
            detail=detail,
        )


@router.post(
    "/pilot-requests",
    operation_id="createPilotRequest",
    response_model=PilotRequestReceipt,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_pilot_request(
    body: PilotRequestCreate,
    sender: Annotated[NotificationSender, Depends(get_notification_sender)],
) -> PilotRequestReceipt:
    _require_database()
    async with database.acquire() as connection:
        created = await repository.create_request(
            connection,
            name=body.name,
            organization=body.organization,
            email=str(body.email),
            facebook_page=body.facebook_page,
            note=body.note,
        )
    notification = Notification(
        recipient=str(body.email),
        subject="KCMS received your pilot request",
        text=(
            f"Hello {body.name.strip()},\n\n"
            "We received your KCMS pilot request. Our team will review your "
            "Facebook Page and contact you before access is approved.\n\n"
            "No password or account has been created for you yet.\n\nKCMS"
        ),
        kind="PILOT_REQUEST_RECEIVED",
    )
    result = await sender.send(notification)
    await _record_delivery(
        created["id"], str(body.email), notification, result.status, result.detail
    )
    return PilotRequestReceipt(
        id=created["id"],
        status=created["status"],
        message="Your request was received and will be reviewed.",
    )


@router.get(
    "/admin/pilot-requests",
    operation_id="listPilotRequests",
    response_model=list[AdminPilotRequest],
)
async def list_pilot_requests(
    _: Annotated[dict[str, Any], Depends(require_platform_admin)],
    request_status: Annotated[PilotStatus | None, Query(alias="status")] = None,
) -> list[AdminPilotRequest]:
    _require_database()
    async with database.acquire() as connection:
        rows = await repository.list_for_admin(connection, request_status)
    return [AdminPilotRequest(**row) for row in rows]


@router.post(
    "/admin/pilot-requests/{request_id}/decision",
    operation_id="decidePilotRequest",
    response_model=PilotDecisionResult,
)
async def decide_pilot_request(
    request_id: str,
    body: PilotDecision,
    admin: Annotated[dict[str, Any], Depends(require_platform_admin)],
    sender: Annotated[NotificationSender, Depends(get_notification_sender)],
) -> PilotDecisionResult:
    if body.decision == "DECLINED" and not (body.reason or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "a reason is required when declining"
        )
    _require_database()
    async with database.acquire() as connection:
        decided = await repository.decide(
            connection,
            request_id=request_id,
            decision=body.decision,
            reason=(body.reason or "").strip() or None,
            admin_id=admin["id"],
        )
    if not decided:
        raise HTTPException(status.HTTP_409_CONFLICT, "request is missing or already decided")

    invitation_url = None
    if decided["token"]:
        invitation_url = (
            f"{settings.public_frontend_url.rstrip('/')}/setup/{decided['token']}"
        )

    if body.decision == "APPROVED" and invitation_url:
        subject = "Set up your KCMS pilot account"
        text = (
            f"Hello {decided['name']},\n\nYour KCMS pilot request has been approved. "
            "Create your own password using this single-use link within seven days:\n\n"
            f"{invitation_url}\n\nKCMS will never email you a password.\n\nKCMS"
        )
        kind = "PILOT_APPROVED"
    elif body.decision == "APPROVED":
        subject = "Your KCMS workspace is approved"
        text = (
            f"Hello {decided['name']},\n\nYour existing KCMS workspace has been approved. "
            f"Sign in at {settings.public_frontend_url.rstrip('/')}/sign-in.\n\nKCMS"
        )
        kind = "PILOT_APPROVED"
    else:
        subject = "Update on your KCMS pilot request"
        text = (
            f"Hello {decided['name']},\n\nWe cannot approve your pilot request yet.\n\n"
            f"Reason: {decided['decision_reason']}\n\n"
            "You may reply to our team for clarification.\n\nKCMS"
        )
        kind = "PILOT_DECLINED"

    notification = Notification(decided["email"], subject, text, kind)
    delivery = await sender.send(notification)
    await _record_delivery(
        request_id, decided["email"], notification, delivery.status, delivery.detail
    )
    public = {key: value for key, value in decided.items() if key not in {"token", "existing_user"}}
    return PilotDecisionResult(
        **public,
        invitation_url=invitation_url,
        delivery_status=delivery.status,
    )


@router.get(
    "/setup-invitations/{token}",
    operation_id="previewSetupInvitation",
    response_model=SetupInvitationPreview,
)
async def preview_setup_invitation(token: str) -> SetupInvitationPreview:
    _require_database()
    async with database.acquire() as connection:
        found = await repository.preview_setup_invitation(connection, token)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invitation is invalid or expired")
    return SetupInvitationPreview(**found)


@router.post(
    "/setup-invitations/{token}/accept",
    operation_id="acceptSetupInvitation",
    response_model=Session,
)
async def accept_setup_invitation(token: str, body: SetupInvitationAccept) -> Session:
    _require_database()
    async with database.acquire() as connection:
        accepted = await repository.accept_setup_invitation(
            connection,
            token=token,
            display_name=body.display_name,
            password=body.password,
        )
    if not accepted:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "invitation is invalid, expired, used, or account exists"
        )
    session_token, user = accepted
    return Session(token=session_token, user=_as_auth_user(user))
