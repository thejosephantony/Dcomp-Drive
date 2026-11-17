"""Contratos REST da preparação/validação de gravações."""
import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_remote_file_path(path: str) -> str:
    if (
        not path.startswith("/")
        or path == "/"
        or path.endswith("/")
        or len(path) > 1024
        or "\\" in path
        or "\x00" in path
        or "//" in path
        or any(part in ("", ".", "..") for part in path.split("/")[1:])
        or path != path.strip()
    ):
        raise ValueError("caminho remoto absoluto e canônico é obrigatório")
    return path


class PrepareRequest(BaseModel):
    path: str = Field(min_length=2, max_length=1024)
    size_bytes: int = Field(ge=0)
    sha256: str
    replication_factor: int | None = Field(default=None, ge=1, le=32)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_remote_file_path(value)

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 deve conter 64 dígitos hexadecimais minúsculos")
        return value


class ReplicaTarget(BaseModel):
    node_id: str
    machine_id: str
    disk_id: str
    address: str


class PrepareResponse(BaseModel):
    write_id: str
    path: str
    version: int
    desired_replicas: int
    expires_at: datetime
    targets: list[ReplicaTarget]


class ReplicaAck(BaseModel):
    node_id: str = Field(min_length=1)
    disk_id: str = Field(min_length=1)
    sha256: str
    size_bytes: int = Field(ge=0)

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 inválido")
        return value


class WriteStatus(BaseModel):
    write_id: str
    status: str
    acked: int
    required: int


class FileMetadataResponse(BaseModel):
    path: str
    version: int
    created_at: datetime | None = None
    version_id: str | None = None  # ID físico para CLI de transferência direta
    sha256: str
    size_bytes: int
    replication_factor: int
    status: str
    replicas: list[ReplicaTarget]
