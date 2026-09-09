"""Platform Administrator read-only operations dashboard."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from kcms.admin import repository
from kcms.api.auth import require_platform_admin
from kcms.billing.plans import PLAN_PAGE_LIMITS
from kcms.shared.database import database

router = APIRouter(prefix="/api/v1/admin")

WorkspaceStatus = Literal["SANDBOX", "TRIAL", "ACTIVE", "EXPIRED", "SUSPENDED"]
PageHealth = Literal["HEALTHY", "STALE", "NEVER_SYNCED", "SUSPENDED"]
Plan = Literal["TRIAL", "STARTER", "GROWTH"]


class AdminWorkspaceSummary(BaseModel):
    id: str
    name: str
    plan: str
    status: WorkspaceStatus
    is_sandbox: bool
    is_suspended: bool
    page_limit: int
    trial_expires_at: datetime | None
    created_at: datetime
    member_count: int
    page_count: int
    comments_processed: int
    comments_processed_7d: int
    pending_comments: int
    replies_sent_7d: int
    reply_failures_7d: int
    auto_reply_enabled: bool
    latest_activity_at: datetime | None


class AdminWorkspaceList(BaseModel):
    items: list[AdminWorkspaceSummary]
    total: int
    limit: int
    offset: int
    generated_at: datetime


class AdminOverview(BaseModel):
    total_workspaces: int
    active_workspaces: int
    trial_workspaces: int
    expired_workspaces: int
    suspended_workspaces: int
    connected_pages: int
    integration_attention: int
    comments_processed_7d: int
    replies_sent_7d: int
    reply_failures_7d: int
    pending_access_requests: int
    generated_at: datetime
    recent_workspaces: list[AdminWorkspaceSummary]


class AdminMember(BaseModel):
    id: str
    display_name: str
    role: str
    created_at: datetime


class AdminPage(BaseModel):
    page_id: str
    page_name: str
    method: str
    tasks: list[str]
    connected_at: datetime
    last_synced_at: datetime | None
    status: PageHealth


class AdminWorkspaceMetrics(BaseModel):
    comments_processed: int
    comments_processed_7d: int
    pending_comments: int
    reviewed_comments: int
    replies_sent_7d: int
    reply_failures_7d: int


class AdminWorkspaceDetail(AdminWorkspaceSummary):
    owner_name: str | None
    members: list[AdminMember]
    pages: list[AdminPage]
    metrics: AdminWorkspaceMetrics


class AdminIntegration(BaseModel):
    page_id: str
    page_name: str
    method: str
    tasks: list[str]
    connected_at: datetime
    last_synced_at: datetime | None
    workspace_id: str
    workspace_name: str
    status: PageHealth


class AdminIntegrationHealth(BaseModel):
    items: list[AdminIntegration]
    total: int
    healthy: int
    attention: int
    generated_at: datetime


class AdminPlan(BaseModel):
    plan: Plan
    page_limit: int
    workspace_count: int
    suspended_count: int


class AdminPlanOverview(BaseModel):
    plans: list[AdminPlan]
    generated_at: datetime


class AdminWorkspaceEntitlementPatch(BaseModel):
    plan: Plan | None = None
    suspended: bool | None = None


class AdminAccount(BaseModel):
    id: str
    email: str
    display_name: str
    created_at: datetime
    active_session_count: int
    latest_session_at: datetime | None


class AdminAccess(BaseModel):
    admins: list[AdminAccount]
    role_management: Literal["deployment_allowlist"]
    mfa: Literal["not_configured"]
    generated_at: datetime


class AdminSessionRevocation(BaseModel):
    user_id: str
    revoked_sessions: int


class AdminAuditEvent(BaseModel):
    id: int
    action: str
    target_type: str
    target_id: str
    metadata: dict[str, Any]
    created_at: datetime
    actor: str


def _require_database() -> None:
    if not database.connected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")


def _summary(row: dict[str, Any]) -> AdminWorkspaceSummary:
    return AdminWorkspaceSummary(
        **row,
        page_limit=PLAN_PAGE_LIMITS[row["plan"]],
    )


async def _detail(connection, workspace_id: str) -> AdminWorkspaceDetail | None:
    row = await repository.get_workspace(connection, workspace_id)
    if row is None:
        return None
    owner_name = await connection.fetchval(
        """SELECT u.display_name
             FROM membership m JOIN app_user u ON u.id = m.user_id
            WHERE m.workspace_id = $1 AND m.role = 'owner'
            ORDER BY m.created_at, u.id LIMIT 1""",
        workspace_id,
    )
    members = await repository.list_members(connection, workspace_id)
    pages = await repository.list_pages(connection, workspace_id)
    metrics = await repository.workspace_metrics(connection, workspace_id)
    return AdminWorkspaceDetail(
        **_summary(row).model_dump(),
        owner_name=owner_name,
        members=[AdminMember(**member) for member in members],
        pages=[AdminPage(**page) for page in pages],
        metrics=AdminWorkspaceMetrics(**metrics),
    )


@router.get(
    "/overview",
    operation_id="getAdminOverview",
    response_model=AdminOverview,
)
async def get_admin_overview(
    _: Annotated[dict[str, Any], Depends(require_platform_admin)],
) -> AdminOverview:
    _require_database()
    async with database.acquire() as connection:
        counts = await repository.overview(connection)
        recent = await repository.list_workspaces(connection, limit=6, offset=0)
    return AdminOverview(
        **counts,
        generated_at=datetime.now(UTC),
        recent_workspaces=[_summary(row) for row in recent],
    )


@router.get(
    "/workspaces",
    operation_id="listAdminWorkspaces",
    response_model=AdminWorkspaceList,
)
async def get_admin_workspaces(
    _: Annotated[dict[str, Any], Depends(require_platform_admin)],
    query: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminWorkspaceList:
    _require_database()
    async with database.acquire() as connection:
        rows = await repository.list_workspaces(
            connection, query=query, limit=limit, offset=offset
        )
        total = await connection.fetchval(
            """SELECT COUNT(*) FROM workspace
                WHERE ($1::TEXT IS NULL OR name ILIKE $1 OR id ILIKE $1)""",
            f"%{query.strip()}%" if query and query.strip() else None,
        )
    return AdminWorkspaceList(
        items=[_summary(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
        generated_at=datetime.now(UTC),
    )


@router.get(
    "/workspaces/{workspace_id}",
    operation_id="getAdminWorkspace",
    response_model=AdminWorkspaceDetail,
)
async def get_admin_workspace(
    workspace_id: str,
    _: Annotated[dict[str, Any], Depends(require_platform_admin)],
) -> AdminWorkspaceDetail:
    _require_database()
    async with database.acquire() as connection:
        detail = await _detail(connection, workspace_id)
    if detail is None:
        # Keep resource enumeration behavior aligned with the rest of the
        # API: a missing workspace is not disclosed to a caller.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
    return detail


@router.get(
    "/integrations",
    operation_id="getAdminIntegrationHealth",
    response_model=AdminIntegrationHealth,
)
async def get_admin_integration_health(
    _: Annotated[dict[str, Any], Depends(require_platform_admin)],
    integration_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminIntegrationHealth:
    allowed = {"HEALTHY", "STALE", "NEVER_SYNCED", "SUSPENDED"}
    if integration_status is not None and integration_status not in allowed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid integration status")
    _require_database()
    async with database.acquire() as connection:
        items = await repository.list_integrations(
            connection, status=integration_status, limit=limit, offset=offset
        )
        total = await connection.fetchval("SELECT COUNT(*) FROM page_connection")
        healthy = await connection.fetchval(
            """SELECT COUNT(*) FROM page_connection p JOIN workspace w ON w.id = p.workspace_id
               WHERE w.is_suspended = FALSE AND p.last_synced_at IS NOT NULL
                 AND p.last_synced_at >= NOW() - INTERVAL '24 hours'"""
        )
        attention = total - healthy
    return AdminIntegrationHealth(
        items=[AdminIntegration(**item) for item in items],
        total=total,
        healthy=healthy,
        attention=attention,
        generated_at=datetime.now(UTC),
    )


@router.get(
    "/plans",
    operation_id="getAdminPlanOverview",
    response_model=AdminPlanOverview,
)
async def get_admin_plan_overview(
    _: Annotated[dict[str, Any], Depends(require_platform_admin)],
) -> AdminPlanOverview:
    _require_database()
    async with database.acquire() as connection:
        counts = await repository.plan_counts(connection)
    known = {row["plan"]: row for row in counts}
    plans = [
        AdminPlan(
            plan=plan,
            page_limit=PLAN_PAGE_LIMITS[plan],
            workspace_count=known.get(plan, {}).get("workspace_count", 0),
            suspended_count=known.get(plan, {}).get("suspended_count", 0),
        )
        for plan in ("TRIAL", "STARTER", "GROWTH")
    ]
    return AdminPlanOverview(plans=plans, generated_at=datetime.now(UTC))


@router.patch(
    "/workspaces/{workspace_id}/entitlement",
    operation_id="patchAdminWorkspaceEntitlement",
    response_model=AdminWorkspaceDetail,
)
async def patch_admin_workspace_entitlement(
    workspace_id: str,
    body: AdminWorkspaceEntitlementPatch,
    admin: Annotated[dict[str, Any], Depends(require_platform_admin)],
) -> AdminWorkspaceDetail:
    if body.plan is None and body.suspended is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no entitlement change supplied")
    _require_database()
    async with database.acquire() as connection:
        updated = await repository.update_workspace_entitlement(
            connection,
            workspace_id=workspace_id,
            actor_user_id=admin["id"],
            plan=body.plan,
            suspended=body.suspended,
        )
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
        detail = await _detail(connection, workspace_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
    return detail


@router.get(
    "/access",
    operation_id="getAdminAccess",
    response_model=AdminAccess,
)
async def get_admin_access(
    _: Annotated[dict[str, Any], Depends(require_platform_admin)],
) -> AdminAccess:
    _require_database()
    async with database.acquire() as connection:
        admins = await repository.list_platform_admins(connection)
    return AdminAccess(
        admins=[AdminAccount(**admin) for admin in admins],
        role_management="deployment_allowlist",
        mfa="not_configured",
        generated_at=datetime.now(UTC),
    )


@router.post(
    "/access/{user_id}/revoke-sessions",
    operation_id="revokeAdminSessions",
    response_model=AdminSessionRevocation,
)
async def revoke_admin_sessions(
    user_id: str,
    admin: Annotated[dict[str, Any], Depends(require_platform_admin)],
) -> AdminSessionRevocation:
    _require_database()
    async with database.acquire() as connection:
        exists = await connection.fetchval(
            "SELECT 1 FROM app_user WHERE id = $1 AND is_platform_admin = TRUE", user_id
        )
        if not exists:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "admin account not found")
        revoked = await repository.revoke_user_sessions(connection, user_id)
        await repository.record_audit(
            connection,
            actor_user_id=admin["id"],
            action="REVOKE_ADMIN_SESSIONS",
            target_type="platform_admin",
            target_id=user_id,
            metadata={"revoked_sessions": revoked},
        )
    return AdminSessionRevocation(user_id=user_id, revoked_sessions=revoked)


@router.get(
    "/audit-log",
    operation_id="listAdminAuditLog",
    response_model=list[AdminAuditEvent],
)
async def list_admin_audit_log(
    _: Annotated[dict[str, Any], Depends(require_platform_admin)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminAuditEvent]:
    _require_database()
    async with database.acquire() as connection:
        rows = await repository.list_audit_events(connection, limit=limit, offset=offset)
    return [AdminAuditEvent(**row) for row in rows]
