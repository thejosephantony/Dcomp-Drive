"""Plano de controle dos arquivos: preparar, registrar acks e publicar versões.

IMPORTANTE: /ack é provisório para testes do contrato. Nesta etapa ele recebe
um relato, não faz verificação remota de bytes. O storage real deve fornecer
um comprovante confiável (ou ser consultado diretamente pelo controlador)
antes de uma gravação ser considerada íntegra em produção.
"""
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from controller.database import get_session
from controller.file_models import Directory, FileEntry, FileVersion, Replica
from controller.file_schemas import (
    FileMetadataResponse, PrepareRequest, PrepareResponse, ReplicaAck,
    ReplicaTarget, WriteStatus, validate_remote_file_path,
)
from controller.models import Node

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def time_limit() -> int:
    return max(5, int(os.getenv("WRITE_TIMEOUT_SECONDS", "300")))


def required_replicas(request: PrepareRequest) -> int:
    configured = int(os.getenv("REPLICATION_FACTOR", "3"))
    n = request.replication_factor if request.replication_factor is not None else configured
    if not 1 <= n <= 32:
        raise HTTPException(status_code=422, detail="N deve estar entre 1 e 32")
    return n


def choose_targets(session: Session, size_bytes: int, replicas: int, now: datetime) -> list[ReplicaTarget]:
    """Uma máquina e um disco diferentes por réplica; respeita capacidade e reservas.

    O lock das linhas de nós serializa a alocação quando o banco é PostgreSQL.
    """
    max_usage = int(os.getenv("MAX_DISK_USAGE_PERCENT", "90"))
    timeout = float(os.getenv("NODE_TIMEOUT_SECONDS", "6"))
    if not 1 <= max_usage <= 100:
        raise HTTPException(500, detail="MAX_DISK_USAGE_PERCENT inválido")

    nodes = session.scalars(
        select(Node).order_by(Node.node_id).with_for_update()
    ).all()
    reservations = dict(
        session.execute(
            select(Replica.disk_id, func.sum(FileVersion.size_bytes))
            .join(FileVersion, Replica.version_id == FileVersion.version_id)
            .where(
                or_(
                    and_(
                        FileVersion.status == "PENDING",
                        FileVersion.expires_at > now,
                    ),
                    Replica.state == "REPAIRING",
                )
            )
            .group_by(Replica.disk_id)
        ).all()
    )
    candidates: list[tuple[int, ReplicaTarget]] = []
    for node in nodes:
        if node.state != "HEALTHY" or (now - aware(node.last_heartbeat)).total_seconds() > timeout:
            continue
        for disk in node.disks:
            limit = disk.capacity_bytes * max_usage // 100
            free = limit - disk.used_bytes - int(reservations.get(disk.disk_id) or 0)
            if free >= size_bytes:
                candidates.append((
                    free,
                    ReplicaTarget(
                        node_id=node.node_id, machine_id=node.machine_id,
                        disk_id=disk.disk_id, address=node.address,
                    ),
                ))
    candidates.sort(key=lambda item: (-item[0], item[1].node_id, item[1].disk_id))
    selected: list[ReplicaTarget] = []
    machine_ids: set[str] = set()
    for _, target in candidates:
        if target.machine_id in machine_ids:
            continue
        selected.append(target)
        machine_ids.add(target.machine_id)
        if len(selected) == replicas:
            return selected
    raise HTTPException(
        status_code=507,
        detail=f"destinos insuficientes: {len(selected)} máquina(s) elegível(is), necessárias {replicas}",
    )


def load_version(session: Session, write_id: str) -> FileVersion:
    version = session.get(FileVersion, write_id)
    if version is None:
        raise HTTPException(404, detail="operação de escrita inexistente")
    return version


def expire_if_needed(session: Session, version: FileVersion, now: datetime) -> None:
    if version.status == "PENDING" and aware(version.expires_at) <= now:
        version.status = "EXPIRED"
        session.commit()
        raise HTTPException(409, detail="operação de escrita expirada")


def write_state(session: Session, version: FileVersion) -> WriteStatus:
    count = session.scalar(
        select(func.count(Replica.replica_id))
        .where(Replica.version_id == version.version_id, Replica.state == "ACKED")
    ) or 0
    return WriteStatus(
        write_id=version.version_id,
        status=version.status,
        acked=count,
        required=version.replication_factor,
    )


@router.post("/prepare", response_model=PrepareResponse, status_code=201)
def prepare(payload: PrepareRequest, session: Session = Depends(get_session)) -> PrepareResponse:
    now = utcnow()
    parent = payload.path.rsplit("/", 1)[0] or "/"
    if session.get(Directory, parent) is None:
        raise HTTPException(404, detail="diretório pai não existe")

    n = required_replicas(payload)
    targets = choose_targets(session, payload.size_bytes, n, now)

    entry = session.scalar(
        select(FileEntry).where(FileEntry.path == payload.path).with_for_update()
    )
    if entry is None:
        entry = FileEntry(file_id=uuid.uuid4().hex, path=payload.path)
        session.add(entry)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(409, detail="caminho alterado por outra requisição") from exc

    latest = session.scalars(
        select(FileVersion)
        .where(FileVersion.file_id == entry.file_id)
        .order_by(FileVersion.version.desc())
        .with_for_update()
    ).first()
    if latest is not None and latest.status == "PENDING":
        if aware(latest.expires_at) > now:
            raise HTTPException(409, detail="já existe uma escrita pendente para o caminho")
        latest.status = "EXPIRED"

    version = FileVersion(
        version_id=uuid.uuid4().hex,
        file_id=entry.file_id,
        version=(latest.version + 1 if latest else 1),
        status="PENDING",
        size_bytes=payload.size_bytes,
        sha256=payload.sha256,
        replication_factor=n,
        created_at=now,
        expires_at=now + timedelta(seconds=time_limit()),
    )
    session.add(version)
    for target in targets:
        session.add(Replica(
            replica_id=uuid.uuid4().hex,
            version_id=version.version_id,
            node_id=target.node_id,
            disk_id=target.disk_id,
            state="RESERVED",
        ))
    session.commit()
    return PrepareResponse(
        write_id=version.version_id,
        path=entry.path,
        version=version.version,
        desired_replicas=n,
        expires_at=version.expires_at,
        targets=targets,
    )


@router.post("/uploads/{write_id}/ack", response_model=WriteStatus)
def acknowledge(write_id: str, payload: ReplicaAck, session: Session = Depends(get_session)) -> WriteStatus:
    version = load_version(session, write_id)
    if version.status != "PENDING":
        raise HTTPException(409, detail="operação de escrita não está pendente")
    expire_if_needed(session, version, utcnow())
    if payload.sha256 != version.sha256 or payload.size_bytes != version.size_bytes:
        raise HTTPException(409, detail="hash ou tamanho diverge do arquivo preparado")
    replica = session.scalar(
        select(Replica).where(
            Replica.version_id == version.version_id,
            Replica.node_id == payload.node_id,
            Replica.disk_id == payload.disk_id,
        ).with_for_update()
    )
    if replica is None:
        raise HTTPException(404, detail="réplica não foi reservada nesta operação")
    if replica.state != "ACKED":
        replica.state = "ACKED"
        replica.acked_at = utcnow()
        session.commit()
    return write_state(session, version)


@router.post("/uploads/{write_id}/commit", response_model=WriteStatus)
def commit(write_id: str, session: Session = Depends(get_session)) -> WriteStatus:
    version = session.scalar(
        select(FileVersion)
        .where(FileVersion.version_id == write_id).with_for_update()
    )
    if version is None:
        raise HTTPException(404, detail="operação de escrita inexistente")
    entry = session.scalar(
        select(FileEntry).where(FileEntry.file_id == version.file_id).with_for_update()
    )
    if version.status == "COMMITTED" and entry.current_version_id == write_id:
        return write_state(session, version)
    if version.status != "PENDING":
        raise HTTPException(409, detail="operação não está pendente")
    expire_if_needed(session, version, utcnow())
    count = session.scalar(
        select(func.count(Replica.replica_id))
        .where(Replica.version_id == version.version_id, Replica.state == "ACKED")
    ) or 0
    if count != version.replication_factor:
        raise HTTPException(409, detail=f"faltam confirmações: {count}/{version.replication_factor}")
    # Este commit valida os ACKs registrados, não os bytes dos nós.
    version.status = "COMMITTED"
    entry.current_version_id = version.version_id
    session.commit()
    return write_state(session, version)


@router.post("/uploads/{write_id}/abort", response_model=WriteStatus)
def abort(write_id: str, session: Session = Depends(get_session)) -> WriteStatus:
    version = load_version(session, write_id)
    if version.status != "PENDING":
        raise HTTPException(409, detail="somente uma escrita pendente pode ser abortada")
    version.status = "ABORTED"
    session.commit()
    return write_state(session, version)


@router.get("/uploads/{write_id}", response_model=WriteStatus)
def upload_status(write_id: str, session: Session = Depends(get_session)) -> WriteStatus:
    return write_state(session, load_version(session, write_id))


@router.get("/metadata", response_model=FileMetadataResponse)
def metadata(path: str = Query(...), session: Session = Depends(get_session)) -> FileMetadataResponse:
    try:
        validate_remote_file_path(path)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    entry = session.scalar(select(FileEntry).where(FileEntry.path == path))
    if entry is None or entry.current_version_id is None:
        raise HTTPException(404, detail="arquivo não encontrado ou ainda não confirmado")
    version = session.get(FileVersion, entry.current_version_id)
    if version is None or version.status != "COMMITTED":
        raise HTTPException(409, detail="versão atual inconsistente")
    replicas = session.scalars(
        select(Replica).where(
            Replica.version_id == version.version_id, Replica.state == "ACKED"
        )
    ).all()
    healthy: list[ReplicaTarget] = []
    now = utcnow()
    timeout = float(os.getenv("NODE_TIMEOUT_SECONDS", "6"))
    for replica in replicas:
        node = session.get(Node, replica.node_id)
        if (node is not None and node.state == "HEALTHY"
            and (now - aware(node.last_heartbeat)).total_seconds() <= timeout):
            healthy.append(ReplicaTarget(
                node_id=node.node_id, machine_id=node.machine_id,
                disk_id=replica.disk_id, address=node.address,
            ))
    state = (
        "HEALTHY" if len(healthy) >= version.replication_factor else
        "UNDER_REPLICATED" if healthy else "UNAVAILABLE"
    )
    return FileMetadataResponse(
        path=entry.path, version=version.version, sha256=version.sha256,
        size_bytes=version.size_bytes,
        replication_factor=version.replication_factor,
        status=state, replicas=healthy,
    )
