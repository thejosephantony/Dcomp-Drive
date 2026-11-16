"""Modelos Pydantic para a API dos nós de armazenamento."""

import re
from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_OBJECT_ID = re.compile(r"^[a-zA-Z0-9_\-\.:]+$")


def validate_object_id_str(object_id: str) -> str:
    if not object_id or len(object_id) > 256 or not _SAFE_OBJECT_ID.fullmatch(object_id):
        raise ValueError("object_id inválido: deve conter apenas caracteres alfanuméricos, '-', '_', '.' ou ':' e até 256 caracteres")
    return object_id


class ObjectMetadata(BaseModel):
    object_id: str
    size_bytes: int = Field(ge=0)
    sha256: str
    created_at: str
    last_scrubbed_at: str | None = None
    status: str = "HEALTHY"

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value.lower()):
            raise ValueError("sha256 deve conter 64 dígitos hexadecimais")
        return value.lower()


class PutObjectResponse(BaseModel):
    object_id: str
    node_id: str
    disk_id: str
    sha256: str
    size_bytes: int
    controller_acked: bool = False


class ReplicateRequest(BaseModel):
    target_address: str | None = None
    source_address: str | None = None
    write_id: str | None = None


class ReplicateResponse(BaseModel):
    object_id: str
    action: str
    peer_address: str
    sha256: str
    size_bytes: int
    status: str = "SUCCESS"
    controller_acked: bool = False


class ScrubReport(BaseModel):
    node_id: str
    disk_id: str
    total_objects: int
    scanned_objects: int
    healthy_objects: int
    corrupted_objects: int
    corrupted_ids: list[str]
    duration_seconds: float


class StorageStatus(BaseModel):
    node_id: str
    machine_id: str
    disk_id: str
    address: str
    capacity_bytes: int
    used_bytes: int
    free_bytes: int
    usage_percent: float
    total_objects: int
    last_scrub: ScrubReport | None = None
