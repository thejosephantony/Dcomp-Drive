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


def validate_sha256_digest(value: str) -> str:
    """Valida e normaliza um hash SHA-256 (64 dígitos hexadecimais em minúsculo)."""
    if not value or not isinstance(value, str):
        raise ValueError("sha256 não pode ser vazio")
    cleaned = value.strip().lower()
    if not _SHA256.fullmatch(cleaned):
        raise ValueError("sha256 deve conter exatamente 64 dígitos hexadecimais minúsculos")
    return cleaned


class ObjectMetadata(BaseModel):
    object_id: str
    size_bytes: int = Field(ge=0)
    sha256: str
    created_at: str
    last_scrubbed_at: str | None = None
    status: str = "HEALTHY"
    encryption: str = "none"  # "none" (legado) ou "AES-256-GCM"

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return validate_sha256_digest(value)


class ObjectVerifyResponse(BaseModel):
    object_id: str
    status: str
    expected_sha256: str
    computed_sha256: str
    size_bytes: int
    match: bool


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
    expected_sha256: str | None = None
    expected_size_bytes: int | None = None


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
    timestamp: str | None = None


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
