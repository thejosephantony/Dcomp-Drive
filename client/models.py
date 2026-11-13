"""Estruturas de dados trocadas entre a CLI e o backend (mock hoje, controlador
real depois). Os estados abaixo replicam o enum definido no plano (seção 03)."""

from dataclasses import dataclass, field
from enum import Enum


class FileState(str, Enum):
    PENDING = "PENDING"
    HEALTHY = "HEALTHY"
    UNDER_REPLICATED = "UNDER_REPLICATED"
    RECOVERING = "RECOVERING"
    UNAVAILABLE = "UNAVAILABLE"


class NodeState(str, Enum):
    HEALTHY = "HEALTHY"
    SUSPECT = "SUSPECT"
    DEAD = "DEAD"
    DRAINING = "DRAINING"


@dataclass
class ReplicaInfo:
    node_id: str
    state: NodeState


@dataclass
class FileMetadata:
    path: str
    version: int
    size: int
    sha256: str
    state: FileState
    replicas: list[ReplicaInfo] = field(default_factory=list)


@dataclass
class DirEntry:
    name: str
    kind: str  # "file" ou "dir"
    size: int = 0
    state: str = ""


@dataclass
class NodeInfo:
    node_id: str
    state: NodeState
    capacity_bytes: int
    used_bytes: int


@dataclass
class ClusterStatus:
    nodes: list[NodeInfo]
    files_by_state: dict[str, int]
