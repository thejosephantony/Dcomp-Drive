"""API do controlador: descoberta, heartbeat e visibilidade inicial."""

import asyncio
import logging
import re

import httpx
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from controller.database import get_session
from controller.file_models import FileEntry, FileVersion, Replica
from controller.file_transfer import _storage_url
from controller.file_api import router as files_router
from controller.namespace_api import router as namespace_router
from controller.file_transfer import router as transfer_router
from controller.models import Disk, Node
from controller.monitor import monitor_forever, recalculate_status
from controller.repair import repair_forever
from controller.schemas import ClusterStatus, Heartbeat, NodeRegistration, NodeStatus

logging.basicConfig(level=logging.INFO)
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
    node.state = "HEALTHY"
    _write_disks(session, node, payload.disks)
    session.commit()
    return {"node_id": node.node_id, "state": node.state}


@app.get("/status", response_model=ClusterStatus)
def status(session: Session = Depends(get_session)) -> ClusterStatus:
    # Atualiza o estado também na consulta, independentemente do timer.
    recalculate_status(session)
    nodes = session.scalars(select(Node).order_by(Node.node_id)).all()
    output: list[NodeStatus] = []
    counts = {"HEALTHY": 0, "SUSPECT": 0, "DEAD": 0}
    for node in nodes:
        counts[node.state] = counts.get(node.state, 0) + 1
        output.append(NodeStatus(
            node_id=node.node_id, machine_id=node.machine_id,
            address=node.address, state=node.state,
            last_heartbeat=node.last_heartbeat.isoformat(),
            capacity_bytes=sum(d.capacity_bytes for d in node.disks),
            used_bytes=sum(d.used_bytes for d in node.disks),
        ))
    return ClusterStatus(nodes=output, counts=counts)


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
