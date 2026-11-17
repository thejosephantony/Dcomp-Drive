"""Reconstrução automática de réplicas após perda de nós do cluster.

Fluxo de um reparo:
  (1) em transação SQL, reivindicar um destino exclusivo REPAIRING;
  (2) ler uma origem HEALTHY, verificar SHA-256 e gravar novo objeto;
  (3) reler o destino, validar hash/tamanho e tornar a réplica ACKED.

A reivindicação persiste antes da transferência e possui prazo de expiração:
uma interrupção do controlador não bloqueia reparos futuros indefinidamente.

Escopo inicial: versões atuais COMMITTED, um reparo por ciclo, objetos
limitados por MAX_TRANSFER_BYTES; somente nós cadastrados e endereços
explicitamente autorizados. Não elimina fisicamente réplicas antigas.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import os
import uuid

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from controller.database import SessionLocal
from controller.file_models import FileEntry, FileVersion, Replica
from controller.file_transfer import _limit, _storage_url, _timeout
from controller.models import Node

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepairJob:
    version_id: str
    replica_id: str
    sha256: str
    size_bytes: int
    sources: tuple[tuple[str, str], ...]
    target_node_id: str
    target_disk_id: str
    target_address: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _alive(node: Node, now: datetime) -> bool:
    return (
        node.state == "HEALTHY"
        and (now - _aware(node.last_heartbeat)).total_seconds()
        <= float(os.getenv("NODE_TIMEOUT_SECONDS", "6"))
    )


def _claim_timeout() -> int:
    return max(60, int(os.getenv("REPAIR_CLAIM_SECONDS", "180")))


def _reserved_bytes(session: Session, now: datetime) -> dict[str, int]:
    """Reservas pendentes do protocolo normal + reparos em andamento."""
    return {
        disk_id: int(amount)
        for disk_id, amount in session.execute(
            select(Replica.disk_id, func.sum(FileVersion.size_bytes))
            .join(FileVersion, FileVersion.version_id == Replica.version_id)
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
    }


def _claim_one(session: Session, now: datetime | None = None) -> RepairJob | None:
    """Reserva atomicamente um novo destino, se existir uma versão deficitária.

    No PostgreSQL, FOR UPDATE SKIP LOCKED serializa a seleção por versão
    entre múltiplos processos de controlador. SQLite ignora este lock.
    """
    now = now or utcnow()
    max_usage = int(os.getenv("MAX_DISK_USAGE_PERCENT", "90"))
    if not 1 <= max_usage <= 100:
        raise ValueError("MAX_DISK_USAGE_PERCENT inválido")

    versions = session.scalars(
        select(FileVersion)
        .join(FileEntry, FileEntry.current_version_id == FileVersion.version_id)
        .where(FileVersion.status == "COMMITTED")
        .order_by(FileVersion.created_at, FileVersion.version_id)
        .with_for_update(skip_locked=True)
    ).all()
    nodes = {n.node_id: n for n in session.scalars(select(Node)).all()}
    changed = False
    reservations = _reserved_bytes(session, now)

    for version in versions:
        if version.size_bytes > _limit():
            continue
        replicas = session.scalars(
            select(Replica)
            .where(Replica.version_id == version.version_id)
            .order_by(Replica.node_id)
        ).all()

        # Interrompeu o controlador durante uma transferência? Recupera a
        # reivindicação expirada, sem apagar objetos já publicados.
        expired_ids: set[str] = set()
        for replica in replicas:
            if replica.state != "REPAIRING":
                continue
            started = _aware(replica.acked_at) if replica.acked_at else None
            if started is None or (now - started).total_seconds() > _claim_timeout():
                session.delete(replica)
                expired_ids.add(replica.replica_id)
                changed = True
                reservations[replica.disk_id] = max(
                    0, reservations.get(replica.disk_id, 0) - version.size_bytes
                )
        if changed:
            session.flush()
        replicas = [r for r in replicas if r.replica_id not in expired_ids]

        healthy = [
            replica for replica in replicas
            if replica.state == "ACKED"
            and replica.node_id in nodes
            and _alive(nodes[replica.node_id], now)
        ]
        active = [
            replica for replica in replicas
            if replica.state == "REPAIRING"
            and replica.node_id in nodes
            and _alive(nodes[replica.node_id], now)
        ]
        if len(healthy) >= version.replication_factor:
            continue
        if not healthy or len(healthy) + len(active) >= version.replication_factor:
            continue

        # Uma origem só serve se for um nó confiável no inventário da rede.
        sources = []
        for replica in healthy:
            try:
                sources.append((replica.node_id, _storage_url(nodes[replica.node_id].address)))
            except Exception:
                logger.warning("repair_source_not_allowed node=%s", replica.node_id)
        if not sources:
            continue

        occupied_nodes = {r.node_id for r in replicas}
        occupied_machines = {
            nodes[r.node_id].machine_id
            for r in healthy + active
            if r.node_id in nodes
        }
        candidates: list[tuple[int, str, str, str]] = []
        for node in nodes.values():
            if not _alive(node, now) or node.node_id in occupied_nodes:
                continue
            if node.machine_id in occupied_machines:
                continue
            try:
                address = _storage_url(node.address)
            except Exception:
                continue
            for disk in node.disks:
                free = (
                    disk.capacity_bytes * max_usage // 100
                    - disk.used_bytes
                    - reservations.get(disk.disk_id, 0)
                )
                if free >= version.size_bytes:
                    candidates.append((free, node.node_id, disk.disk_id, address))
        if not candidates:
            continue
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        _, node_id, disk_id, address = candidates[0]
        replica = Replica(
            replica_id=uuid.uuid4().hex,
            version_id=version.version_id,
            node_id=node_id,
            disk_id=disk_id,
            state="REPAIRING",
            acked_at=now,  # prazo da reivindicação; no ACK vira data real
        )
        session.add(replica)
        session.commit()
        logger.warning(
            "repair_claimed version=%s target=%s sources=%s",
            version.version_id, node_id, [n for n, _ in sources],
        )
        return RepairJob(
            version_id=version.version_id,
            replica_id=replica.replica_id,
            sha256=version.sha256,
            size_bytes=version.size_bytes,
            sources=tuple(sources),
            target_node_id=node_id,
            target_disk_id=disk_id,
            target_address=address,
        )

    if changed:
        session.commit()
    return None


async def _copy_verified(job: RepairJob) -> bool:
    """Valida origem e destino; não confia apenas no status HTTP do PUT."""
    payload: bytes | None = None
    async with httpx.AsyncClient(timeout=_timeout()) as client:
        for node_id, address in job.sources:
            try:
                resp = await client.get(f"{address}/objects/{job.version_id}")
                if resp.status_code != 200:
                    continue
                if (
                    len(resp.content) != job.size_bytes
                    or hashlib.sha256(resp.content).hexdigest() != job.sha256
                ):
                    logger.warning("repair_source_corrupt version=%s node=%s", job.version_id, node_id)
                    continue
                payload = resp.content
                break
            except httpx.RequestError:
                logger.warning("repair_source_unreachable version=%s node=%s", job.version_id, node_id)

        if payload is None:
            return False

        try:
            target = f"{job.target_address}/objects/{job.version_id}"
            stored = await client.put(
                target,
                content=payload,
                headers={"X-Expected-Sha256": job.sha256},
            )
            if stored.status_code != 201:
                logger.warning("repair_put_rejected node=%s code=%s", job.target_node_id, stored.status_code)
                return False
            report = stored.json()
            if (
                report.get("node_id") != job.target_node_id
                or report.get("disk_id") != job.target_disk_id
                or report.get("sha256") != job.sha256
                or report.get("size_bytes") != job.size_bytes
            ):
                return False
            verified = await client.get(target)
            if (
                verified.status_code != 200
                or len(verified.content) != job.size_bytes
                or hashlib.sha256(verified.content).hexdigest() != job.sha256
            ):
                logger.warning("repair_destination_corrupt version=%s target=%s", job.version_id, job.target_node_id)
                return False
            return True
        except (httpx.RequestError, ValueError):
            logger.exception("repair_transfer_failed version=%s target=%s", job.version_id, job.target_node_id)
            return False


def _finish_one(session: Session, job: RepairJob, success: bool) -> bool:
    """Só publica a cópia validada se o claim ainda pertencer ao job."""
    replica = session.scalar(
        select(Replica)
        .where(Replica.replica_id == job.replica_id)
        .with_for_update()
    )
    if replica is None or replica.state != "REPAIRING":
        return False
    version = session.get(FileVersion, job.version_id)
    entry = session.get(FileEntry, version.file_id) if version is not None else None
    node = session.get(Node, job.target_node_id)

    if (
        not success
        or version is None
        or entry is None
        or entry.current_version_id != job.version_id
        or version.status != "COMMITTED"
        or node is None
        or not _alive(node, utcnow())
    ):
        session.delete(replica)
        session.commit()
        return False

    replica.state = "ACKED"
    replica.acked_at = utcnow()

    # O nó que morreu pode retornar com dados antigos; não assumir que
    # continuam íntegros. Mantemos bytes em disco para limpeza futura.
    nodes = {n.node_id: n for n in session.scalars(select(Node)).all()}
    other_replicas = session.scalars(
        select(Replica).where(
            Replica.version_id == job.version_id,
            Replica.replica_id != job.replica_id,
            Replica.state == "ACKED",
        )
    ).all()
    for old in other_replicas:
        previous = nodes.get(old.node_id)
        if previous is None or not _alive(previous, utcnow()):
            old.state = "LOST"

    session.commit()
    logger.warning(
        "repair_completed version=%s node=%s",
        job.version_id, job.target_node_id,
    )
    return True


def _corrupted_cleanup_targets(job: RepairJob) -> list[tuple[str, str]]:
    """Descobre réplicas corrompidas após a restauração de N cópias válidas."""
    with SessionLocal() as session:
        version = session.get(FileVersion, job.version_id)
        if version is None or version.status != "COMMITTED":
            return []
        entry = session.get(FileEntry, version.file_id)
        if entry is None or entry.current_version_id != job.version_id:
            return []
        replicas = session.scalars(
            select(Replica).where(Replica.version_id == job.version_id)
        ).all()
        nodes = {node.node_id: node for node in session.scalars(select(Node)).all()}
        now = utcnow()
        count = sum(
            1 for replica in replicas
            if replica.state == "ACKED"
            and replica.node_id in nodes
            and _alive(nodes[replica.node_id], now)
        )
        if count < version.replication_factor:
            return []
        targets = []
        for replica in replicas:
            if replica.state != "CORRUPTED":
                continue
            node = nodes.get(replica.node_id)
            if node is None or not _alive(node, now):
                continue
            try:
                targets.append((replica.replica_id, _storage_url(node.address)))
            except Exception:
                logger.warning("corrupt_cleanup_address_rejected node=%s", replica.node_id)
        return targets


def _forget_discarded_replica(job: RepairJob, replica_id: str) -> None:
    """Remove o vínculo corrompido após exclusão física confirmada."""
    with SessionLocal() as session:
        replica = session.scalar(
            select(Replica).where(
                Replica.replica_id == replica_id,
                Replica.version_id == job.version_id,
                Replica.state == "CORRUPTED",
            ).with_for_update()
        )
        if replica is None:
            return
        session.delete(replica)
        session.commit()


async def _cleanup_corrupted_after_repair(job: RepairJob) -> None:
    """Descarta bytes defeituosos só depois de uma nova réplica íntegra existir."""
    targets = await asyncio.to_thread(_corrupted_cleanup_targets, job)
    if not targets:
        return
    async with httpx.AsyncClient(timeout=_timeout()) as client:
        for replica_id, address in targets:
            try:
                response = await client.delete(f"{address}/objects/{job.version_id}")
                if response.status_code in (200, 404):
                    await asyncio.to_thread(_forget_discarded_replica, job, replica_id)
                    logger.info(
                        "corrupted_replica_discarded version=%s address=%s",
                        job.version_id, address,
                    )
                else:
                    logger.warning(
                        "corrupted_replica_cleanup_failed version=%s http=%d",
                        job.version_id, response.status_code,
                    )
            except (httpx.RequestError, ValueError):
                logger.exception(
                    "corrupted_replica_cleanup_error version=%s address=%s",
                    job.version_id, address,
                )


async def repair_once() -> bool:
    """Tenta um reparo físico; True somente após verificar a nova réplica."""
    job = await asyncio.to_thread(_claim_with_session)
    if job is None:
        return False

    verified = False
    try:
        verified = await _copy_verified(job)
        repaired = await asyncio.to_thread(_finish_with_session, job, verified)
        if repaired:
            try:
                await _cleanup_corrupted_after_repair(job)
            except Exception:
                logger.exception("corrupted_replica_cleanup_unexpected version=%s", job.version_id)
        return repaired
    except asyncio.CancelledError:
        await asyncio.to_thread(_finish_with_session, job, False)
        raise
    except Exception:
        logger.exception("repair_failed version=%s", job.version_id)
        await asyncio.to_thread(_finish_with_session, job, False)
        return False


def _claim_with_session() -> RepairJob | None:
    with SessionLocal() as session:
        return _claim_one(session)


def _finish_with_session(job: RepairJob, verified: bool) -> bool:
    with SessionLocal() as session:
        return _finish_one(session, job, verified)


async def repair_forever() -> None:
    """Roda continuamente em segundo plano, sem bloquear o event loop."""
    interval = max(0.5, float(os.getenv("REPAIR_INTERVAL_SECONDS", "2")))
    while True:
        await asyncio.sleep(interval)
        try:
            await repair_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("repair_monitor_failed")
