from fastapi import APIRouter, Response, status

from kcms.api.schemas import DatabaseStatus, HealthResponse, ServiceStatus
from kcms.settings import settings
from kcms.shared.database import database

router = APIRouter()

SERVICE_NAME = "kcms-backend"


@router.get(
    "/api/v1/health",
    operation_id="getHealth",
    response_model=HealthResponse,
    summary="Database-aware service health",
    responses={503: {"model": HealthResponse, "description": "Database unreachable"}},
)
async def get_health(response: Response) -> HealthResponse:
    reachable = await database.is_reachable()
    if not reachable:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            service=SERVICE_NAME,
            status=ServiceStatus.DEGRADED,
            database=DatabaseStatus.UNREACHABLE,
            contract_version=settings.contract_version,
        )
    return HealthResponse(
        service=SERVICE_NAME,
        status=ServiceStatus.READY,
        database=DatabaseStatus.REACHABLE,
        contract_version=settings.contract_version,
    )
