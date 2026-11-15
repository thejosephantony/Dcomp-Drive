"""Contratos HTTP da descoberta de nós.

Não são contratos de arquivos: prepare/commit e replicação virão em uma etapa posterior.
"""

from pydantic import BaseModel, Field, HttpUrl, model_validator


class DiskReport(BaseModel):
    disk_id: str = Field(min_length=1, max_length=128)
    capacity_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_usage(self) -> "DiskReport":
        if self.used_bytes > self.capacity_bytes:
            raise ValueError("used_bytes não pode exceder capacity_bytes")
        return self


class NodeRegistration(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    machine_id: str = Field(min_length=1, max_length=128)
    address: HttpUrl
    disks: list[DiskReport] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_disks(self) -> "NodeRegistration":
        ids = [disk.disk_id for disk in self.disks]
        if len(ids) != len(set(ids)):
            raise ValueError("disk_id duplicado")
        return self


class Heartbeat(BaseModel):
    disks: list[DiskReport] = Field(min_length=1)


class NodeStatus(BaseModel):
    node_id: str
    machine_id: str
    address: str
    state: str
    last_heartbeat: str
    capacity_bytes: int
    used_bytes: int


class ClusterStatus(BaseModel):
    nodes: list[NodeStatus]
    counts: dict[str, int]
