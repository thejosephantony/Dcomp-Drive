"""API do controlador: descoberta, heartbeat e visibilidade inicial."""

import asyncio
import logging
import re
import time
import uuid

import httpx
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from controller.database import get_session
from controller.file_models import FileEntry, FileVersion, Replica, Directory
from controller.file_transfer import _storage_url
from controller.file_api import router as files_router, metadata
from controller.namespace_api import router as namespace_router
from controller.file_transfer import router as transfer_router
from controller.demo_api import router as demo_router
from controller.models import Disk, Node
from controller.monitor import monitor_forever, recalculate_status
from controller.repair import repair_forever
from controller.schemas import ClusterStatus, DiskStatus, Heartbeat, NodeInventory, NodeRegistration, NodeStatus

from controller.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor_task = asyncio.create_task(monitor_forever())
    repair_task = asyncio.create_task(repair_forever())
    try:
        yield
    finally:
        monitor_task.cancel()
        repair_task.cancel()
        for task in (monitor_task, repair_task):
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="DComp Drive Controller", version="0.2.0", lifespan=lifespan)
app.include_router(files_router)
app.include_router(transfer_router)
app.include_router(namespace_router)
app.include_router(demo_router)

_METRIC_START = time.monotonic()
_REQUEST_COUNTER = {"count": 0, "latency_sum_ms": 0.0}


@app.middleware("http")
async def record_http_latency(request, call_next):
    started = time.monotonic()
    try:
        return await call_next(request)
    finally:
        _REQUEST_COUNTER["count"] += 1
        _REQUEST_COUNTER["latency_sum_ms"] += (time.monotonic() - started) * 1000


def _write_disks(session: Session, node: Node, reports) -> None:
    # Não permitir que um disco já vinculado a outro nó seja sequestrado por id.
    ids = [item.disk_id for item in reports]
    existing = session.scalars(select(Disk).where(Disk.disk_id.in_(ids))).all()
    for disk in existing:
        if disk.node_id != node.node_id:
            raise HTTPException(409, detail=f"disco já registrado em outro nó: {disk.disk_id}")
    by_id = {d.disk_id: d for d in node.disks}
    reported = set(ids)
    for disk in list(node.disks):
        if disk.disk_id not in reported:
            session.delete(disk)
    for report in reports:
        disk = by_id.get(report.disk_id)
        if disk is None:
            disk = Disk(disk_id=report.disk_id, node_id=node.node_id)
            session.add(disk)
        disk.capacity_bytes = report.capacity_bytes
        disk.used_bytes = report.used_bytes


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/nodes/register")
def register(payload: NodeRegistration, session: Session = Depends(get_session)) -> dict[str, str]:
    node = session.get(Node, payload.node_id)
    if node is None:
        node = Node(
            node_id=payload.node_id,
            machine_id=payload.machine_id,
            address=str(payload.address),
            last_heartbeat=datetime.now(timezone.utc),
            state="HEALTHY",
        )
        session.add(node)
        # Os campos NOT NULL precisam estar preenchidos antes do primeiro flush.
        session.flush()
    else:
        node.machine_id = payload.machine_id
        node.address = str(payload.address)
        node.last_heartbeat = datetime.now(timezone.utc)
        if node.state != "DRAINING":
            node.state = "HEALTHY"
    _write_disks(session, node, payload.disks)
    session.commit()
    return {"node_id": node.node_id, "state": node.state}


@app.post("/nodes/{node_id}/heartbeat")
def heartbeat(node_id: str, payload: Heartbeat, session: Session = Depends(get_session)) -> dict[str, str]:
    node = session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, detail=f"nó não registrado: {node_id}")
    node.last_heartbeat = datetime.now(timezone.utc)
    if node.state != "DRAINING":
        node.state = "HEALTHY"
    _write_disks(session, node, payload.disks)
    session.commit()
    return {"node_id": node.node_id, "state": node.state}


@app.post("/nodes/{node_id}/drain")
def drain_node(node_id: str, session: Session = Depends(get_session)) -> dict[str, str]:
    """RF7: impede novas alocações e inicia migração assíncrona de cópias."""
    node = session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, detail="nó não registrado")
    if node.state == "DEAD":
        raise HTTPException(409, detail="nó indisponível para drain planejado")
    node.state = "DRAINING"
    session.commit()
    logger.warning("node_drain_started node=%s", node_id)
    return {"node_id": node_id, "state": "DRAINING"}


@app.post("/nodes/{node_id}/undrain")
def undrain_node(node_id: str, session: Session = Depends(get_session)) -> dict[str, str]:
    """Reabilita nó que concluiu o ensaio de drenagem planejada."""
    node = session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, detail="nó não registrado")
    if node.state != "DRAINING":
        raise HTTPException(409, detail="nó não está em DRAINING")
    node.state = "HEALTHY"
    session.commit()
    logger.info("node_undrained node=%s", node_id)
    return {"node_id": node_id, "state": "HEALTHY"}


@app.post("/nodes/{node_id}/reconcile")
def reconcile_node(
    node_id: str,
    payload: NodeInventory,
    session: Session = Depends(get_session),
) -> dict[str, int]:
    """RF6: valida cópias no nó que retornou e reaproveita apenas dados íntegros."""
    node = session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, detail="nó não registrado")
    if payload.disk_id not in {d.disk_id for d in node.disks}:
        raise HTTPException(409, detail="disco não registrado para o nó")
    base = _storage_url(node.address)
    restored = discarded = skipped = 0
    for physical in payload.objects:
        object_id = physical.object_id
        if re.fullmatch(r"[0-9a-f]{32}", object_id) is None:
            skipped += 1
            continue
        version = session.get(FileVersion, object_id)
        if version is None:
            # Um órfão desconhecido não é excluído sem política de coleta segura.
            skipped += 1
            continue
        entry = session.get(FileEntry, version.file_id)
        current = (
            version.status == "COMMITTED"
            and entry is not None and entry.current_version_id == object_id
        )
        replica = session.scalar(
            select(Replica)
            .where(Replica.version_id == object_id, Replica.node_id == node_id)
            .with_for_update()
        )
        if not current and version.status == "PENDING":
            skipped += 1
            continue

        if current:
            if node.state not in ("HEALTHY", "DRAINING"):
                skipped += 1
                continue
            try:
                check = httpx.get(f"{base}/objects/{object_id}/verify", timeout=5)
                valid = check.status_code == 200 and (
                    check.json().get("match") is True
                    and check.json().get("expected_sha256") == version.sha256
                    and check.json().get("size_bytes") == version.size_bytes
                )
            except (httpx.RequestError, ValueError):
                skipped += 1
                continue

            if not valid:
                if check.status_code == 500 and replica is not None:
                    replica.state = "CORRUPTED"
                    session.commit()
                    logger.warning("reconcile_corruption_confirmed node=%s object=%s", node_id, object_id)
                else:
                    skipped += 1
                continue

            if replica is not None and replica.state == "ACKED":
                skipped += 1
                continue
            all_acked = session.scalars(
                select(Replica).where(
                    Replica.version_id == object_id,
                    Replica.state == "ACKED",
                )
            ).all()
            live_nodes = {n.node_id: n for n in session.scalars(select(Node)).all()}
            live = [
                item for item in all_acked
                if item.node_id in live_nodes
                and live_nodes[item.node_id].state == "HEALTHY"
            ]
            if len(live) < version.replication_factor:
                if replica is None:
                    replica = Replica(
                        replica_id=uuid.uuid4().hex,
                        version_id=object_id,
                        node_id=node_id,
                        disk_id=payload.disk_id,
                        state="ACKED",
                    )
                    session.add(replica)
                else:
                    replica.state = "ACKED"
                replica.acked_at = datetime.now(timezone.utc)
                session.commit()
                restored += 1
                logger.info("reconcile_replica_reused node=%s object=%s", node_id, object_id)
                continue
            # Já existem N cópias saudáveis em outras máquinas; a antiga é obsoleta.
        try:
            gone = httpx.delete(f"{base}/objects/{object_id}", timeout=5)
        except httpx.RequestError:
            skipped += 1
            continue
        if gone.status_code not in (200, 404):
            skipped += 1
            continue
        if replica is not None:
            session.delete(replica)
            session.commit()
        discarded += 1
        logger.info("reconcile_obsolete_discarded node=%s object=%s", node_id, object_id)
    return {"restored": restored, "discarded": discarded, "skipped": skipped}


def _file_state_counts(session: Session) -> dict[str, int]:
    counts = {"HEALTHY": 0, "UNDER_REPLICATED": 0,
              "RECOVERING": 0, "UNAVAILABLE": 0}
    paths = session.scalars(
        select(FileEntry.path)
        .join(FileVersion, FileEntry.current_version_id == FileVersion.version_id)
        .where(FileVersion.status == "COMMITTED")
    ).all()
    for path in paths:
        state = metadata(path=path, session=session).status
        counts[state] = counts.get(state, 0) + 1
    return counts


@app.get("/metrics")
def metrics(session: Session = Depends(get_session)) -> dict:
    """Métricas simples em JSON, para auditoria de Parte 1 e testes da Parte 2."""
    from controller.telemetry import counters_snapshot
    elapsed = max(time.monotonic() - _METRIC_START, 0.001)
    counts = _file_state_counts(session)
    replicas = session.execute(
        select(Replica.state, func.count(Replica.replica_id))
        .group_by(Replica.state)
    ).all()
    total = _REQUEST_COUNTER["count"]
    return {
        "uptime_seconds": round(elapsed, 2),
        "requests_total": total,
        "request_latency_avg_ms": round(
            _REQUEST_COUNTER["latency_sum_ms"] / max(total, 1), 3
        ),
        "files_by_state": counts,
        "replicas_by_state": {key: value for key, value in replicas},
        "repairs_in_progress": sum(num for state, num in replicas if state == "REPAIRING"),
        **counters_snapshot(elapsed),
    }


@app.get("/status", response_model=ClusterStatus)
def status(session: Session = Depends(get_session)) -> ClusterStatus:
    # Atualiza o estado também na consulta, independentemente do timer.
    recalculate_status(session)
    nodes = session.scalars(select(Node).order_by(Node.node_id)).all()
    output: list[NodeStatus] = []
    counts = {"HEALTHY": 0, "SUSPECT": 0, "DEAD": 0, "DRAINING": 0}
    for node in nodes:
        counts[node.state] = counts.get(node.state, 0) + 1
        output.append(NodeStatus(
            node_id=node.node_id, machine_id=node.machine_id,
            address=node.address, state=node.state,
            last_heartbeat=node.last_heartbeat.isoformat(),
            capacity_bytes=sum(d.capacity_bytes for d in node.disks),
            used_bytes=sum(d.used_bytes for d in node.disks),
            disks=[
                DiskStatus(
                    disk_id=d.disk_id, capacity_bytes=d.capacity_bytes,
                    used_bytes=d.used_bytes,
                    free_bytes=max(0, d.capacity_bytes - d.used_bytes),
                ) for d in node.disks
            ],
        ))
    repairing = session.scalar(
        select(func.count(Replica.replica_id))
        .where(Replica.state == "REPAIRING")
    ) or 0
    return ClusterStatus(
        nodes=output, counts=counts,
        files_by_state=_file_state_counts(session),
        repairs_in_progress=repairing,
    )


# Interface web no mesmo domínio da API: dispensa CORS e servidor adicional.
WEB_ROOT = Path(__file__).resolve().parent / "web"
app.mount("/ui", StaticFiles(directory=str(WEB_ROOT)), name="dashboard-assets")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    """Painel do DComp Drive (uso de laboratório, sem autenticação)."""
    return FileResponse(WEB_ROOT / "index.html")


@app.post("/nodes/{node_id}/objects/{object_id}/corruption")
def report_corrupted_replica(
    node_id: str,
    object_id: str,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    """Registra corrupção física confirmada, liberando o reparador para recompor N.

    O registro do nó precisa existir e o metadado físico precisa estar marcado
    CORRUPTED. Não aceita denúncias arbitrárias de clientes como prova.
    O endpoint só deve ser acessível na rede de laboratório sem exposição pública.
    """
    if re.fullmatch(r"[0-9a-f]{32}", object_id) is None:
        raise HTTPException(422, detail="ID de objeto inválido")
    node = session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, detail="Storage Node não registrado")

    replica = session.scalar(
        select(Replica)
        .where(Replica.version_id == object_id, Replica.node_id == node_id)
        .with_for_update()
    )
    if replica is None:
        raise HTTPException(404, detail="Réplica desconhecida pelo controlador")
    if replica.state == "CORRUPTED":
        return {"status": "ALREADY_CORRUPTED", "object_id": object_id, "node_id": node_id}
    if replica.state != "ACKED":
        raise HTTPException(409, detail="Réplica não está confirmada")

    version = session.get(FileVersion, object_id)
    entry = session.get(FileEntry, version.file_id) if version is not None else None
    if (
        version is None or version.status != "COMMITTED"
        or entry is None or entry.current_version_id != object_id
    ):
        raise HTTPException(409, detail="Versão não é a versão publicada")

    try:
        address = _storage_url(node.address)
        response = httpx.get(f"{address}/objects/{object_id}/meta", timeout=5.0)
        response.raise_for_status()
        physical = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("corruption_validation_unavailable node=%s object=%s error=%s", node_id, object_id, exc)
        raise HTTPException(503, detail="Não foi possível confirmar a corrupção no Storage Node") from exc

    if (
        physical.get("object_id") != object_id
        or physical.get("sha256") != version.sha256
        or physical.get("size_bytes") != version.size_bytes
        or physical.get("status") != "CORRUPTED"
    ):
        raise HTTPException(409, detail="Storage Node não confirmou a corrupção física")

    replica.state = "CORRUPTED"
    session.commit()
    logging.getLogger(__name__).warning("replica_corruption_confirmed node=%s object=%s", node_id, object_id)
    return {"status": "CORRUPTED", "object_id": object_id, "node_id": node_id}
