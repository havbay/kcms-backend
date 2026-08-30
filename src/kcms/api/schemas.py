from enum import StrEnum

from pydantic import BaseModel, Field


class ServiceStatus(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"


class DatabaseStatus(StrEnum):
    REACHABLE = "REACHABLE"
    UNREACHABLE = "UNREACHABLE"


class HealthResponse(BaseModel):
    service: str = Field(examples=["kcms-backend"])
    status: ServiceStatus
    database: DatabaseStatus
    contract_version: str = Field(examples=["1.0.0"])
